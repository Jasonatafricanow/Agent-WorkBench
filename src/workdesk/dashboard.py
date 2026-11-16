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
.meta{color:var(--muted);font-size:12px}a{color:inherit;text-decoration:none;border-bottom:1px dotted var(--line)}.warn{color:var(--warn)}.ok{color:var(--ok)}.empty{color:var(--muted);font-size:13px}
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
    # Read-only: the page never advances a "seen" watermark; it shows the latest changes instead.
    with closing(connect(database)) as conn:
        recent = [dict(r) for r in conn.execute(
            """SELECT e.task_id, t.title, e.kind, e.actor_id, e.reason, e.occurred_at FROM task_events e
               JOIN tasks t ON t.workspace_id=e.workspace_id AND t.id=e.task_id
               WHERE e.workspace_id=? ORDER BY e.recorded_at DESC, e.rowid DESC LIMIT 40""",
            (actor.workspace_id,))]
    stuck = stagnation(database, actor=actor, now=now)
    load = schedule_load(database, actor=actor, now=now)
    e = lambda s: escape(str(s or ""))

    def task_line(t):
        due = t.get("official_due_local") or _local(t.get("official_due_at"), tz)
        soft = _local(t.get("soft_due_at"), tz)
        bits = [f"官方 {e(due)}" if due else "", f"soft {e(soft)}" if soft else "", e(t.get("next_action"))]
        return f"<a href='/task/{e(t['id'])}'>{e(t['title'])}</a><div class=meta>{' · '.join(b for b in bits if b)}</div>"

    def focus_slot(name, t):
        body = task_line(t) if t else '<span class="empty">空</span>'
        return f'<div class="slot"><b>{name}</b>{body}</div>'

    def waiting_line(t):
        on = "；".join(f"{e(d['awaited_outcome'])}（{e(names.get(d['responsible_principal_id'], '—'))}）"
                      for d in t.get("waiting_on", []))
        return f"<a href='/task/{e(t['id'])}'>{e(t['title'])}</a><div class=meta>等：{on or '—'}</div>"

    sections = [
        ("待审批", _list(ov["my_approvals"], lambda a: f"{e(a['request_summary'])}<div class=meta>/approve {e(a['id'])}"
                                                    f"{' · 到期 ' + _local(a['expires_at'], tz) if a['expires_at'] else ''}</div>",
                         empty="没有")),
        ("结果不明、需要核对", _list(ov["unknown_outcomes"], lambda u: f"{e(u['kind'])} · {e(u['id'])}"
                                                               f"<div class=meta>{_local(u['updated_at'], tz)}</div>", empty="没有")),
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
        ("本周到期", _list(ov["due_this_week"], task_line)),
        ("即将催促/提醒", _list(ov["upcoming_followups"], lambda r: f"<a href='/task/{e(r['task_id'])}'>{e(r['title'])}</a>"
                                                         f"<div class=meta>{_local(r['due_at'], tz)} · 给 {e(names.get(r['recipient_principal_id'], ''))}"
                                                         f"{' · 还会重复 ' + str(r['repeats_left']) + ' 次' if r['repeats_left'] else ''}</div>")),
        ("可做", _list(ov["ready"] + ov["doing"], task_line)),
        ("最近变化", _list(recent, lambda ev: f"<a href='/task/{e(ev['task_id'])}'>{e(ev['title'])}</a>：{KIND_LABELS.get(ev['kind'], e(ev['kind']))}<div class=meta>"
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


KIND_LABELS = {
    "created": "创建", "patched": "修改", "delayed": "延期", "state_changed": "状态", "progress": "进展",
    "focus_set": "放入焦点", "focus_cleared": "移出焦点", "dependency_added": "开始等待", "dependency_resolved": "等待解除",
    "reminder_set": "设提醒", "reminder_cancelled": "取消提醒", "source_linked": "关联消息", "source_edited": "原消息被编辑",
    "human_action_requested": "请人处理", "human_action_responded": "人已回复", "responsibility_changed": "委托变化",
    "official_due_set": "官方截止", "commitment_recorded": "记下承诺", "commitment_updated": "承诺变化",
    "deliverable_registered": "登记交付物", "checkpoint_saved": "工作书签", "watch_added": "外部监控",
    "external_change": "外部变化", "requester_note": "请求方备注",
}


def render_task(tasks, actor, database: str, task_id: str, now: str) -> str:
    with closing(connect(database)) as conn:
        tz = ZoneInfo(conn.execute("SELECT timezone FROM workspaces WHERE id=?", (actor.workspace_id,)).fetchone()[0])
        names = {r[0]: r[1] for r in conn.execute("SELECT id, display_name FROM principals")}
    view = tasks.read(actor=actor, task_id=task_id)
    t, e = view["task"], (lambda s: escape(str(s or "")))
    facts = [("状态", t["state"]), ("下一步", t["next_action"]), ("目标", t["goal"]),
             ("官方截止", t.get("official_due_local") or _local(t["official_due_at"], tz)),
             ("soft due", _local(t["soft_due_at"], tz)), ("已做", t["done_summary"]),
             ("请求人", names.get(t["requester_id"], "")), ("版本", t["version"])]
    rows = "".join(f"<li><b>{k}</b> {e(v)}</li>" for k, v in facts if v)
    events = _list(list(reversed(view["events"])), lambda ev: (
        f"{KIND_LABELS.get(ev['kind'], e(ev['kind']))}{'：' + e(ev['reason']) if ev['reason'] else ''}"
        f"<div class=meta>{_local(ev['occurred_at'], tz)} · {e(names.get(ev['actor_id'], ev['actor_id']))}"
        f" · {e(ev['evidence_type'])}{' · ' + e(ev['source_ref']) if ev['source_ref'] else ''}</div>"))
    deps = _list(view["dependencies"], lambda d: f"{e(d['awaited_outcome'])}<div class=meta>{e(d['status'])}"
                                              f" · {e(names.get(d['responsible_principal_id'], ''))}</div>")
    chk = view.get("latest_checkpoint")
    checkpoint = (f"{e(chk['next_action'])}<div class=meta>已完成 {e(', '.join(json.loads(chk['completed_steps_json'])))}"
                  f" · 未核实 {e(', '.join(json.loads(chk['unverified_json'])))}</div>") if chk else '<p class="empty">（无）</p>'
    body = (f"<section><h2>概况</h2><ul>{rows}</ul></section><section><h2>在等</h2>{deps}</section>"
            f"<section><h2>工作书签</h2>{checkpoint}</section><section><h2>历史</h2>{events}</section>")
    return (f"<!doctype html><html lang=zh-CN><head><meta charset=utf-8>"
            f"<meta name=viewport content='width=device-width,initial-scale=1'><title>{e(t['title'])}</title>"
            f"<style>{CSS}</style></head><body><main><div class=sub><a href='/'>← 工作台</a></div>"
            f"<h1>{e(t['title'])}</h1><div class=grid>{body}</div></main></body></html>")
