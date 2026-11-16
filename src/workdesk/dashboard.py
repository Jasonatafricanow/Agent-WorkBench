"""Server-rendered read-only dashboard (no scripts, no write paths)."""
from __future__ import annotations

import json
from datetime import datetime
from html import escape
from zoneinfo import ZoneInfo

from .insights import schedule_load, stagnation
from .storage import connect
from contextlib import closing

CSS = """
:root{--bg:#f6f5f2;--card:#fff;--ink:#1d1d1b;--muted:#6b6a66;--line:#e3e1dc;--accent:#2f5d8a;--warn:#a5482b;--ok:#3d7a4f}
@media (prefers-color-scheme:dark){:root{--bg:#161615;--card:#1f1f1d;--ink:#ecebe7;--muted:#9c9a94;--line:#33322f;--accent:#7fa8d1;--warn:#e08a6d;--ok:#7cbf8e}}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--ink);font:15px/1.5 system-ui,-apple-system,"PingFang SC","Microsoft YaHei",sans-serif}
main{max-width:1100px;margin:0 auto;padding:16px}h1{font-size:20px;margin:4px 0 2px}h2{font-size:15px;margin:0 0 8px;color:var(--muted);font-weight:600}
.sub{color:var(--muted);font-size:13px;margin-bottom:16px}.grid{display:grid;gap:12px;grid-template-columns:repeat(auto-fit,minmax(300px,1fr))}
section{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:12px 14px}
.focus{display:grid;gap:8px;grid-template-columns:repeat(auto-fit,minmax(220px,1fr));margin-bottom:12px}
.slot{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:10px 12px}.slot b{display:block;font-size:12px;color:var(--accent);letter-spacing:.04em}
ul{list-style:none;margin:0;padding:0}li{padding:6px 0;border-top:1px solid var(--line)}li:first-child{border-top:0}
.meta{color:var(--muted);font-size:12px}.warn{color:var(--warn)}.ok{color:var(--ok)}.empty{color:var(--muted);font-size:13px}
"""


def _local(ts: str | None, tz: ZoneInfo) -> str:
    if not ts:
        return ""
    return datetime.fromisoformat(ts[:-1] + "+00:00").astimezone(tz).strftime("%m-%d %H:%M")


def _list(items, render, empty="（无）") -> str:
    if not items:
        return f'<p class="empty">{empty}</p>'
    return "<ul>" + "".join(f"<li>{render(i)}</li>" for i in items) + "</ul>"


def render_dashboard(tasks, actor, database: str, now: str) -> str:
    with closing(connect(database)) as conn:
        tz = ZoneInfo(conn.execute("SELECT timezone FROM workspaces WHERE id=?", (actor.workspace_id,)).fetchone()[0])
        names = {r[0]: r[1] for r in conn.execute("SELECT id, display_name FROM principals")}
    ov = tasks.overview(actor=actor, limit=30)
    news = tasks.changes_since(actor=actor, view="dashboard", limit=60, mark_seen=True)
    stuck = stagnation(database, actor=actor, now=now)
    load = schedule_load(database, actor=actor, now=now)
    e = lambda s: escape(str(s or ""))

    def task_line(t):
        due = t.get("official_due_local") or _local(t.get("official_due_at"), tz)
        soft = _local(t.get("soft_due_at"), tz)
        bits = [f"官方 {e(due)}" if due else "", f"soft {e(soft)}" if soft else "", e(t.get("next_action"))]
        return f"{e(t['title'])}<div class=meta>{' · '.join(b for b in bits if b)}</div>"

    def focus_slot(name, t):
        body = task_line(t) if t else '<span class="empty">空</span>'
        return f'<div class="slot"><b>{name}</b>{body}</div>'

    def waiting_line(t):
        on = "；".join(f"{e(d['awaited_outcome'])}（{e(names.get(d['responsible_principal_id'], '—'))}）"
                      for d in t.get("waiting_on", []))
        return f"{e(t['title'])}<div class=meta>等：{on or '—'}</div>"

    sections = [
        ("需要我处理", _list(ov["my_human_actions"], lambda h: f"{e(h['title'])}<div class=meta>"
                                                          f"{e(h['request_kind'])} · {e(h['urgency'])}"
                                                          f"{' · 截止 ' + _local(h['due_at'], tz) if h['due_at'] else ''}</div>")),
        ("新进来的（INBOX）", _list(ov["inbox"], task_line)),
        ("等待中", _list(ov["waiting"], waiting_line)),
        ("阻塞", _list(ov["blocked"], task_line)),
        ("逾期", _list(ov["overdue"], lambda t: f'<span class="warn">{task_line(t)}</span>')),
        ("承诺", _list(ov["commitments_open"], lambda c: f"{e(c['promise_text'])}<div class=meta>"
                                                          f"{'我欠' if c['direction'] == 'owner_owes' else '欠我' if c['direction'] == 'owed_to_owner' else '他人'}"
                                                          f" · {e(c.get('due_local') or _local(c['due_at'], tz))}</div>")),
        ("交付物", _list(ov["deliverables_pending"], lambda d: f"{e(d['title'])}<div class=meta>{e(d['status'])} · 第 {d['revision']} 版</div>")),
        ("可做", _list(ov["ready"] + ov["doing"], task_line)),
        ("自上次查看以来", _list(news["events"], lambda ev: f"{e(ev['title'])}：{e(ev['kind'])}<div class=meta>"
                                                         f"{_local(ev['occurred_at'], tz)} · {e(names.get(ev['actor_id'], ev['actor_id']))}"
                                                         f"{' · ' + e(ev['reason']) if ev['reason'] else ''}</div>",
                                 empty="没有新变化")),
        ("停滞与反复延期", _list(stuck, lambda s: f"{e(s['title'])}<div class=meta>延期 {len(s['delays'])} 次"
                                                f"{' · 等待自 ' + _local(s['waiting_since'], tz) if s['waiting_since'] else ''}</div>")),
        ("排期过载的日子", _list([d for d in load["days"] if d["overloaded"]],
                              lambda d: f"{e(d['date'])}<div class=meta>{len(d['items'])} 项到期</div>", empty="没有")),
    ]
    focus = "".join(focus_slot(n, ov["focus"].get(n)) for n in ("NOW", "NEXT1", "NEXT2"))
    body = "".join(f"<section><h2>{title}</h2>{content}</section>" for title, content in sections)
    stamp = datetime.fromisoformat(now[:-1] + "+00:00").astimezone(tz).strftime("%Y-%m-%d %H:%M")
    return (f"<!doctype html><html lang=zh-CN><head><meta charset=utf-8>"
            f"<meta name=viewport content='width=device-width,initial-scale=1'><title>工作台</title>"
            f"<style>{CSS}</style></head><body><main><h1>工作台</h1>"
            f"<div class=sub>只读视图 · {stamp}</div><div class=focus>{focus}</div>"
            f"<div class=grid>{body}</div></main></body></html>")
