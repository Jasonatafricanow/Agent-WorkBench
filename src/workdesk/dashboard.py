"""Server-rendered dashboard: Work, Calendar, Timeline, Projects, Life.

No scripts. Signed in with the read-only dashboard password every page is a GET and nothing here writes.
Signed in with the owner token, the Work and Task pages also carry the owner's forms (owner_ui.py), which
post to /owner/ops; rendering itself still never writes. Business data comes through TaskService / `views` (workspace + role checked);
the Life view reads Facetide through `LifeStateAdapter` and is shown to the workspace owner only.
"""
from __future__ import annotations

import json
from contextlib import closing
from datetime import date, datetime, timedelta, timezone
from html import escape
from urllib.parse import urlencode
from zoneinfo import ZoneInfo

from . import views
from .errors import Forbidden
from .insights import schedule_load, stagnation
from .storage import connect
from .owner_ui import FORM_CSS
from .task_service import SqliteTaskService

CSS = """
:root{--bg:#f6f5f2;--card:#fff;--ink:#1d1d1b;--muted:#6b6a66;--line:#e3e1dc;--accent:#2f5d8a;--warn:#a5482b;--ok:#3d7a4f;--soft:#8a6d2f}
@media (prefers-color-scheme:dark){:root{--bg:#161615;--card:#1f1f1d;--ink:#ecebe7;--muted:#9c9a94;--line:#33322f;--accent:#7fa8d1;--warn:#e08a6d;--ok:#7cbf8e;--soft:#d6b46f}}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--ink);font:15px/1.5 system-ui,-apple-system,"PingFang SC","Microsoft YaHei",sans-serif}
main{max-width:1100px;margin:0 auto;padding:16px}h1{font-size:20px;margin:4px 0 2px}h2{font-size:15px;margin:0 0 8px;color:var(--muted);font-weight:600}
nav{display:flex;flex-wrap:wrap;gap:4px;margin:0 0 12px;border-bottom:1px solid var(--line)}nav a{padding:6px 12px;border:0;border-bottom:2px solid transparent;color:var(--muted)}
nav a.on{color:var(--ink);border-bottom-color:var(--accent);font-weight:600}
.sub{color:var(--muted);font-size:13px;margin-bottom:12px}.grid{display:grid;gap:12px;grid-template-columns:repeat(auto-fit,minmax(300px,1fr))}
section{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:12px 14px}
.focus{display:grid;gap:8px;grid-template-columns:repeat(auto-fit,minmax(220px,1fr));margin-bottom:12px}
.slot{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:10px 12px}.slot b{display:block;font-size:12px;color:var(--accent);letter-spacing:.04em}
ul{list-style:none;margin:0;padding:0}li{padding:6px 0;border-top:1px solid var(--line)}li:first-child{border-top:0}
.meta{color:var(--muted);font-size:12px}a{color:inherit;text-decoration:none;border-bottom:1px dotted var(--line)}.warn{color:var(--warn)}.ok{color:var(--ok)}.empty{color:var(--muted);font-size:13px}
.tag{display:inline-block;font-size:11px;padding:0 6px;border-radius:9px;border:1px solid var(--line);margin-right:6px;color:var(--muted)}
.tag.official{color:var(--warn);border-color:var(--warn)}.tag.soft{color:var(--soft);border-color:var(--soft)}.tag.follow{color:var(--accent);border-color:var(--accent)}
.pills{display:flex;gap:6px;flex-wrap:wrap;margin-bottom:10px}.pills a{padding:2px 10px;border:1px solid var(--line);border-radius:12px;font-size:13px}.pills a.on{border-color:var(--accent);color:var(--accent)}
form.filters{display:flex;gap:8px;flex-wrap:wrap;align-items:center;margin-bottom:12px;font-size:13px}form.filters select,form.filters button{font:inherit;font-size:13px;padding:3px 6px;background:var(--card);color:var(--ink);border:1px solid var(--line);border-radius:6px}
.bar{height:8px;background:var(--line);border-radius:4px;overflow:hidden}.bar i{display:block;height:100%;background:var(--accent)}
table{width:100%;border-collapse:collapse;font-size:14px}td,th{padding:6px 4px;border-top:1px solid var(--line);text-align:left}th{color:var(--muted);font-weight:600;font-size:12px}
.num{font-variant-numeric:tabular-nums;text-align:right;white-space:nowrap}
.lines{display:grid;gap:12px}.line h2{display:flex;flex-wrap:wrap;gap:6px;align-items:baseline;color:var(--ink);font-size:16px;margin:0 0 2px}
.line .tag{font-weight:400}.line ol{list-style:none;margin:8px 0 0;padding:0 0 0 14px;border-left:2px solid var(--line)}
.line .day{position:relative;padding:2px 0 8px}.line .day:before{content:"";position:absolute;left:-20px;top:8px;width:10px;height:10px;border-radius:50%;background:var(--card);border:2px solid var(--accent)}
.line .day>b{font-size:13px;color:var(--accent)}.line .step{padding:3px 0 0;border:0}.line .step .when{color:var(--muted);font-variant-numeric:tabular-nums;margin-right:6px}
"""

NAV = (("work", "/", "工作台"), ("calendar", "/calendar", "日历"), ("timeline", "/timeline", "时间线"),
       ("projects", "/projects", "项目池"), ("life", "/life", "生活"))

KIND_LABELS = {
    "created": "创建", "patched": "修改", "delayed": "延期", "state_changed": "状态", "progress": "进展",
    "focus_set": "放入焦点", "focus_cleared": "移出焦点", "dependency_added": "开始等待", "dependency_resolved": "等待解除",
    "reminder_set": "设提醒", "reminder_cancelled": "取消提醒", "source_linked": "关联消息", "source_edited": "原消息被编辑",
    "human_action_requested": "请人处理", "human_action_responded": "人已回复", "responsibility_changed": "委托变化",
    "official_due_set": "官方截止", "commitment_recorded": "记下承诺", "commitment_updated": "承诺变化",
    "deliverable_registered": "登记交付物", "deliverable_submitted": "交付", "deliverable_acknowledged": "对方已收到",
    "deliverable_accepted": "交付已验收", "deliverable_rejected": "交付被退回", "deliverable_review_required": "交付需修改",
    "checkpoint_saved": "工作书签", "watch_added": "外部监控", "external_change": "外部变化", "requester_note": "请求方备注",
    "handoff": "交接",
}
STATE_LABELS = {"INBOX": "新进", "READY": "可做", "DOING": "进行中", "WAITING": "等待", "BLOCKED": "阻塞",
                "DONE": "完成", "CANCELLED": "取消"}
e = lambda s: escape(str(s if s is not None else ""))


def _ctx(actor, database):
    with closing(connect(database)) as conn:
        tz = ZoneInfo(conn.execute("SELECT timezone FROM workspaces WHERE id=?", (actor.workspace_id,)).fetchone()[0])
        names = {r[0]: r[1] for r in conn.execute(
            """SELECT p.id, p.display_name FROM principals p JOIN memberships m ON m.principal_id=p.id
               WHERE m.workspace_id=?""", (actor.workspace_id,))}
    return tz, names


def _local(ts: str | None, tz: ZoneInfo, fmt="%m-%d %H:%M") -> str:
    """UTC instant ('...Z' or '...+00:00', as Facetide writes it) shown in the workspace's local time."""
    if not ts:
        return ""
    try:
        moment = datetime.fromisoformat(ts[:-1] + "+00:00" if ts.endswith("Z") else ts)
    except ValueError:
        return str(ts)
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return moment.astimezone(tz).strftime(fmt)


def _utc(day: date, tz: ZoneInfo) -> str:
    return datetime(day.year, day.month, day.day, tzinfo=tz).astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _today(now: str, tz: ZoneInfo) -> date:
    return datetime.fromisoformat(now[:-1] + "+00:00").astimezone(tz).date()


def _plus_seconds(ts: str, seconds: int) -> str:
    return (datetime.fromisoformat(ts[:-1]) + timedelta(seconds=seconds)).strftime("%Y-%m-%dT%H:%M:%SZ")


def range_window(kind: str, now: str, tz: ZoneInfo) -> tuple[date, date]:
    """Local [first day, day after last) for today / week (Mon-Sun) / month."""
    today = _today(now, tz)
    if kind == "today":
        return today, today + timedelta(days=1)
    if kind == "month":
        first = today.replace(day=1)
        return first, (first + timedelta(days=32)).replace(day=1)
    monday = today - timedelta(days=today.weekday())
    return monday, monday + timedelta(days=7)


def _list(items, render, empty="（无）") -> str:
    if not items:
        return f'<p class="empty">{empty}</p>'
    return "<ul>" + "".join(f"<li>{render(i)}</li>" for i in items) + "</ul>"


def _page(title: str, active: str, body: str, now: str, tz: ZoneInfo, *, sub: str = "", owner: bool = False) -> str:
    tabs = "".join(f"<a href='{href}' class='{'on' if key == active else ''}'>{label}</a>" for key, href, label in NAV)
    stamp = _local(now, tz, "%Y-%m-%d %H:%M")
    return (f"<!doctype html><html lang=zh-CN><head><meta charset=utf-8>"
            f"<meta name=viewport content='width=device-width,initial-scale=1'><title>{e(title)}</title>"
            f"<style>{CSS}{FORM_CSS if owner else ''}</style></head><body><main><h1>{e(title)}</h1>"
            f"<div class=sub>{'主人模式' if owner else '只读视图'} · {stamp}{' · ' + sub if sub else ''}</div><nav>{tabs}</nav>{body}</main></body></html>")


def _pills(base: str, current: str, options, extra: dict) -> str:
    links = []
    for key, label in options:
        query = urlencode({"range": key, **{k: v for k, v in extra.items() if v}})
        links.append(f"<a href='{base}?{e(query)}' class='{'on' if key == current else ''}'>{label}</a>")
    return f"<div class=pills>{''.join(links)}</div>"


def _filters(base: str, actor, database, current: dict, extra: dict) -> str:
    """Plain GET form; the values only narrow the caller's own view (checked again in `views`)."""
    with closing(connect(database)) as conn:
        projects = conn.execute("SELECT id, title FROM projects WHERE workspace_id=? ORDER BY title",
                                (actor.workspace_id,)).fetchall()
        requesters = conn.execute(
            """SELECT p.id, p.display_name FROM memberships m JOIN principals p ON p.id=m.principal_id
               WHERE m.workspace_id=? AND m.role='requester' ORDER BY p.display_name""", (actor.workspace_id,)).fetchall()

    def select(name, label, rows):
        opts = "".join(f"<option value='{e(r[0])}' {'selected' if current.get(name) == r[0] else ''}>{e(r[1])}</option>"
                       for r in rows)
        return f"<label>{label} <select name={name}><option value=''>全部</option>{opts}</select></label>"
    hidden = "".join(f"<input type=hidden name='{e(k)}' value='{e(v)}'>" for k, v in extra.items() if v)
    return (f"<form class=filters method=get action='{base}'>{hidden}{select('project', '项目', projects)}"
            f"{select('requester', '请求人', requesters)}<button type=submit>筛选</button></form>")


def _event_line(ev, tz, names) -> str:
    if ev["type"] == "agent_run":
        return (f"<a href='/task/{e(ev['task_id'])}'>{e(ev['title'])}</a>：Agent 执行 {e(ev['kind'])} · {e(ev['status'])}"
                f"<div class=meta>{_local(ev['at'], tz)}</div>")
    src = f" · 来源 {e(ev['source_ref'])}" if ev.get("source_ref") else ""
    return (f"<a href='/task/{e(ev['task_id'])}'>{e(ev['title'])}</a>：{KIND_LABELS.get(ev['kind'], e(ev['kind']))}"
            f"<div class=meta>{_local(ev['at'], tz)} · {e(names.get(ev['actor_id'], ev['actor_id']))}"
            f"{' · ' + e(ev['reason']) if ev.get('reason') else ''}{src}</div>")


# ----------------------------------------------------------------------------------- Work
def render_dashboard(tasks, actor, database: str, now: str, *, owner_html: str = "") -> str:
    tz, names = _ctx(actor, database)
    ov = tasks.overview(actor=actor, limit=30)
    since = (datetime.fromisoformat(now[:-1]) - timedelta(days=3)).strftime("%Y-%m-%dT%H:%M:%SZ")
    recent = [i for i in views.timeline(database, actor=actor, start=since, end=_plus_seconds(now, 1), limit=40)["items"]
              if i["type"] == "task_event"]
    stuck = stagnation(database, actor=actor, now=now)
    load = schedule_load(database, actor=actor, now=now)

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
        ("需要我处理（Human Inbox）", _list(ov["my_human_actions"], lambda h: f"{e(h['title'])}<div class=meta>"
                                                                    f"{e(h['request_kind'])} · {e(h['urgency'])}"
                                                                    f"{' · 截止 ' + _local(h['due_at'], tz) if h['due_at'] else ''}</div>")),
        ("新进来的（INBOX）", _list(ov["inbox"], task_line)),
        ("进行中 / 可做", _list(ov["doing"] + ov["ready"], task_line)),
        ("等待中", _list(ov["waiting"], waiting_line)),
        ("阻塞", _list(ov["blocked"], task_line)),
        ("逾期", _list(ov["overdue"], lambda t: f'<span class="warn">{task_line(t)}</span>')),
        ("承诺", _list(ov["commitments_open"], lambda c: f"{e(c['promise_text'])}<div class=meta>"
                                                          f"{'我欠' if c['direction'] == 'owner_owes' else '欠我' if c['direction'] == 'owed_to_owner' else '他人'}"
                                                          f" · {e(c.get('due_local') or _local(c['due_at'], tz))}</div>")),
        ("交付物", _list(ov["deliverables_pending"], lambda d: f"{e(d['title'])}<div class=meta>{e(d['status'])} · 第 {d['revision']} 版</div>")),
        ("最近完成（7 天）", _list(ov["recently_done"], task_line)),
        ("即将催促/提醒", _list(ov["upcoming_followups"], lambda r: f"<a href='/task/{e(r['task_id'])}'>{e(r['title'])}</a>"
                                                         f"<div class=meta>{_local(r['due_at'], tz)} · 给 {e(names.get(r['recipient_principal_id'], ''))}"
                                                         f"{' · 还会重复 ' + str(r['repeats_left']) + ' 次' if r['repeats_left'] else ''}</div>")),
        ("最近变化（3 天）", _list(recent, lambda ev: _event_line(ev, tz, names), empty="没有新变化")),
        ("停滞与反复延期", _list(stuck, lambda s: f"{e(s['title'])}<div class=meta>延期 {len(s['delays'])} 次"
                                                f"{' · 等待自 ' + _local(s['waiting_since'], tz) if s['waiting_since'] else ''}</div>")),
        ("排期过载的日子", _list([d for d in load["days"] if d["overloaded"]],
                              lambda d: f"{e(d['date'])}<div class=meta>工作量 {d['load']} 项</div>", empty="没有")),
        ("排期建议（只动 soft due，需你确认）", _list(load.get("suggestions", []),
                                         lambda s: f"<a href='/task/{e(s['task_id'])}'>{e(s['title'])}</a>"
                                                   f"<div class=meta>{e(s['from'])} → {e(s['to'])} · {e(s['basis'])}</div>",
                                         empty="没有")),
    ]
    focus = "".join(focus_slot(n, ov["focus"].get(n)) for n in ("NOW", "NEXT1", "NEXT2"))
    body = f"{owner_html}<div class=focus>{focus}</div><div class=grid>" + "".join(
        f"<section><h2>{title}</h2>{content}</section>" for title, content in sections) + "</div>"
    return _page("工作台", "work", body, now, tz, owner=bool(owner_html))


# ------------------------------------------------------------------------------- Calendar
CAL_TAGS = {"official_due": ("official", "官方 DDL"), "soft_due": ("soft", "Soft Due"), "planned": ("", "计划"),
            "follow_up": ("follow", "跟进"), "human_action": ("", "待人处理")}


def render_calendar(tasks, actor, database: str, now: str, *, range_kind: str = "week",
                    project: str | None = None, requester: str | None = None, owner: bool = False) -> str:
    tz, names = _ctx(actor, database)
    range_kind = range_kind if range_kind in ("week", "month") else "week"
    first, after = range_window(range_kind, now, tz)
    cal = views.calendar(database, actor=actor, start=_utc(first, tz), end=_utc(after, tz),
                         project_id=project, requester_id=requester)
    load = schedule_load(database, actor=actor, now=now, days=max(1, (after - _today(now, tz)).days + 1))
    weight = {d["date"]: d for d in load["days"]}
    by_day = {d["date"]: d["items"] for d in cal["days"]}

    def item(i):
        cls, label = CAL_TAGS[i["type"]]
        target = f"/task/{e(i['task_id'])}" if i.get("task_id") else "#"
        date_only = i["type"] == "official_due" and len(i.get("local_date") or "") == 10 and \
            _local(i["at"], tz, "%H:%M:%S") == "23:59:59"
        when = "" if date_only else _local(i["at"], tz, "%H:%M")
        extra = f" · 给 {e(names.get(i.get('recipient_id'), ''))}" if i["type"] == "follow_up" else ""
        return f"<span class='tag {cls}'>{label}</span><a href='{target}'>{e(i['title'])}</a><span class=meta> {when}{extra}</span>"

    rows, day = [], first
    while day < after:
        key = day.isoformat()
        info = weight.get(key)
        badge = (f" <span class='{'warn' if info['overloaded'] else 'meta'}'>工作量 {info['load']}</span>" if info else "")
        if by_day.get(key) or range_kind == "week":
            rows.append(f"<section><h2>{key} 周{'一二三四五六日'[day.weekday()]}{badge}</h2>"
                        f"{_list(by_day.get(key, []), item, empty='空')}</section>")
        day += timedelta(days=1)
    head = _pills("/calendar", range_kind, (("week", "本周"), ("month", "本月")), {"project": project, "requester": requester})
    form = _filters("/calendar", actor, database, {"project": project, "requester": requester}, {"range": range_kind})
    body = head + form + (f"<div class=grid>{''.join(rows)}</div>" if rows else '<p class="empty">这段时间没有期限和提醒</p>')
    return _page("日历", "calendar", body, now, tz, sub="官方 DDL 不会被排期建议移动；Soft Due 的调整只是建议", owner=owner)


# ------------------------------------------------------------------------------- Timeline
def render_timeline(tasks, actor, database: str, now: str, *, range_kind: str = "week",
                    project: str | None = None, requester: str | None = None, owner: bool = False) -> str:
    tz, names = _ctx(actor, database)
    range_kind = range_kind if range_kind in ("today", "week", "month") else "week"
    first, after = range_window(range_kind, now, tz)
    tl = views.timeline(database, actor=actor, start=_utc(first, tz), end=_utc(after, tz),
                        project_id=project, requester_id=requester)
    sections = "".join(_line_card(line, tz, names) for line in tl["lines"])
    head = _pills("/timeline", range_kind, (("today", "今天"), ("week", "本周"), ("month", "本月")),
                  {"project": project, "requester": requester})
    form = _filters("/timeline", actor, database, {"project": project, "requester": requester}, {"range": range_kind})
    body = head + form + (f"<div class=lines>{sections}</div>" if sections else '<p class="empty">这段时间没有记录</p>')
    return _page("时间线", "timeline", body, now, tz, sub="一个任务一条线，同一天同一次操作合成一步", owner=owner)


def _stage_label(stage: dict) -> str:
    if stage.get("type") == "agent_run":
        return f"Agent 执行 {e(stage.get('kind'))} · {e(stage.get('status'))}"
    label = KIND_LABELS.get(stage["kind"], e(stage["kind"]))
    if stage.get("to_state"):
        return f"{label}：{STATE_LABELS.get(stage['to_state'], e(stage['to_state']))}"
    return label


def _line_card(line: dict, tz, names) -> str:
    def step(s):
        stages = " → ".join(_stage_label(x) for x in s["stages"])
        reasons = "；".join(e(r) for r in s["reasons"])
        who = names.get(s["actor_id"], s["actor_id"]) if s["actor_id"] != "agent-run" else "Agent"
        src = f" · 来源 {e(s['source_ref'])}" if s.get("source_ref") else ""
        return (f"<li class=step><span class=when>{_local(s['at'], tz, '%H:%M')}</span>{stages}"
                f"<div class=meta>{e(who)}{' · ' + reasons if reasons else ''}{src}</div></li>")
    days = "".join(f"<li class=day><b>{e(d['date'][5:])}</b><ul>{''.join(step(s) for s in d['steps'])}</ul></li>"
                   for d in line["days"])
    first, last = _local(line["first_at"], tz, "%m-%d"), _local(line["last_at"], tz, "%m-%d")
    span = first if first == last else f"{first} → {last}"
    earlier = " · 接着更早的记录" if line["earlier"] else ""
    state = STATE_LABELS.get(line.get("state"), line.get("state") or "")
    return (f"<section class=line><h2><a href='/task/{e(line['task_id'])}'>{e(line['title'])}</a>"
            f"<span class=tag>{e(state)}</span></h2><div class=meta>{span}{earlier}</div><ol>{days}</ol></section>")


# ------------------------------------------------------------------------------- Projects
def render_projects(tasks, actor, database: str, now: str, *, project: str | None = None,
                    requester: str | None = None, owner: bool = False) -> str:
    tz, names = _ctx(actor, database)
    pool = views.projects(database, actor=actor, project_id=project, requester_id=requester)["projects"]

    def card(p):
        done, total = p["progress"]["done"], p["progress"]["total"]
        pct = int(100 * done / total) if total else 0
        counts = " · ".join(f"{STATE_LABELS.get(k, k)} {v}" for k, v in sorted(p["counts"].items()))
        due = _local(p["next_official_due"], tz, "%m-%d") if p["next_official_due"] else "—"
        rows = "".join(
            f"<tr><td><a href='/task/{e(t['id'])}'>{e(t['title'])}</a></td><td>{STATE_LABELS.get(t['state'], t['state'])}</td>"
            f"<td>{e(names.get(t.get('executor_id'), t.get('executor_id') or ''))}</td>"
            f"<td class=num>{e(t.get('official_due_local') or _local(t['official_due_at'], tz, '%m-%d'))}</td></tr>"
            for t in p["tasks"])
        table = (f"<table><tr><th>任务</th><th>状态</th><th>执行人</th><th class=num>官方期限</th></tr>{rows}</table>"
                 if rows else '<p class="empty">没有任务</p>')
        blocked = f" · <span class=warn>阻塞 {p['blocked']}</span>" if p["blocked"] else ""
        return (f"<section><h2>{e(p['title'])}</h2><div class=meta>进度 {done}/{total} · 最近官方期限 {due}{blocked}</div>"
                f"<div class=bar><i style='width:{pct}%'></i></div><div class=meta>{counts}</div>{table}</section>")

    form = _filters("/projects", actor, database, {"project": project, "requester": requester}, {})
    body = form + (f"<div class=grid>{''.join(card(p) for p in pool)}</div>" if pool else '<p class="empty">没有项目</p>')
    return _page("项目池", "projects", body, now, tz, owner=owner)


# ----------------------------------------------------------------------------------- Life
def render_life(actor, database: str, now: str, adapter, *, owner: bool = False) -> str:
    """Owner only. Shows Facetide's confirmed state as Facetide reports it; WorkBench keeps no copy
    beyond the adapter's display cache."""
    with closing(connect(database)) as conn:
        if SqliteTaskService._role(conn, actor) != "owner":
            raise Forbidden("the life view is for the workspace owner only")
    tz, _ = _ctx(actor, database)
    state = adapter.summary() if adapter else {"status": "not_configured", "summary": None, "fetched_at": None}
    status = state["status"]
    notes = {"fresh": "", "stale": f"Facetide 暂时连不上，显示 {_local(state['fetched_at'], tz)} 的数据",
             "unavailable": "Facetide 暂时连不上，还没有可显示的数据", "not_configured": "还没有接入 Facetide"}
    s = state.get("summary")
    if not s:
        body = f'<section><p class="empty">{notes[status]}</p></section>'
        return _page("生活", "life", body, now, tz, sub="来源：Facetide（只读）", owner=owner)
    latest, previous = s.get("latest"), s.get("previous")
    scale = s.get("score_scale") or {"min": 0, "max": 10}
    domain_names = {d["domain_id"]: d["name"] for d in s.get("domains", [])}
    fmt = lambda v: "—" if v is None else f"{v:g}"

    def score_rows():
        out = []
        for d in s.get("domains", []):
            now_v = latest["domain_scores"].get(d["domain_id"]) if latest else None
            prev_v = previous["domain_scores"].get(d["domain_id"]) if previous else None
            delta = "" if now_v is None or prev_v is None else (
                f"<span class='{'ok' if now_v > prev_v else 'warn' if now_v < prev_v else 'meta'}'>"
                f"{'+' if now_v > prev_v else ''}{now_v - prev_v:g}</span>")
            span = (scale["max"] - scale["min"]) or 1
            pct = 0 if now_v is None else int(100 * (now_v - scale["min"]) / span)
            out.append(f"<tr><td>{e(d['name'])}</td><td style='width:40%'><div class=bar><i style='width:{pct}%'></i></div></td>"
                       f"<td class=num>{fmt(now_v)}</td><td class=num>{fmt(prev_v)}</td><td class=num>{delta}</td></tr>")
        return "".join(out)

    def period(r):
        return f"{e(r['period_start'])} ~ {e(r['period_end'])}（确认于 {e(_local(r['confirmed_at'], tz))}）"

    if latest:
        prev_line = f"<br>上期 {period(previous)}" if previous else "<br>上期：没有"
        wheel = (f"<section><h2>人生轮 · 七领域</h2><div class=meta>本期 {period(latest)}{prev_line}</div>"
                 f"<table><tr><th>领域</th><th></th><th class=num>本期</th><th class=num>上期</th><th class=num>变化</th></tr>"
                 f"{score_rows()}</table><div class=meta>满分 {fmt(scale['max'])}；没打分的领域显示 —，不做推断</div></section>")
    else:
        wheel = '<section><h2>人生轮</h2><p class="empty">还没有确认过的评估</p></section>'
    axes = "".join(
        f"<section><h2>{title}</h2>"
        f"{('<p>' + e(latest[key]) + '</p>') if latest and latest.get(key) else '<p class=empty>（本期没写）</p>'}</section>"
        for key, title in (("personal_growth", "成长"), ("self_realization", "自我实现")))
    goals = _list(s.get("goals", []), lambda g: f"{e(g['title'])}<div class=meta>{e(g['status'])} · 够了的标准：{e(g['enough_condition'])}"
                                                 f" · {e('、'.join(domain_names.get(d, d) for d in g['domain_ids']))}</div>",
                  empty="没有目标")
    body = f"<div class=grid>{wheel}{axes}<section><h2>长期目标</h2>{goals}</section></div>"
    sub = "来源：Facetide（只读）" + (f" · {notes[status]}" if notes[status] else f" · 更新于 {_local(state['fetched_at'], tz)}")
    return _page("生活", "life", body, now, tz, sub=sub, owner=owner)


# ----------------------------------------------------------------------------------- Task
def render_task(tasks, actor, database: str, task_id: str, now: str, *, owner_panel=None, notice: str = "") -> str:
    """owner_panel(task, focus_slot, tz) -> HTML of the owner's forms, or None for the read-only page."""
    tz, names = _ctx(actor, database)
    view = tasks.read(actor=actor, task_id=task_id)
    t = view["task"]
    panel = ""
    if owner_panel is not None:
        with closing(connect(database)) as conn:
            row = conn.execute("SELECT slot FROM task_focus WHERE workspace_id=? AND task_id=?",
                               (actor.workspace_id, t["id"])).fetchone()
        panel = owner_panel(dict(t), row["slot"] if row else None, tz)
    facts = [("状态", STATE_LABELS.get(t["state"], t["state"])), ("下一步", t["next_action"]), ("目标", t["goal"]),
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
    body = (f"{notice}{panel}<div class=grid><section><h2>概况</h2><ul>{rows}</ul></section><section><h2>在等</h2>{deps}</section>"
            f"<section><h2>工作书签</h2>{checkpoint}</section><section><h2>历史</h2>{events}</section></div>")
    return _page(t["title"], "work", body, now, tz, owner=owner_panel is not None)
