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
from .panel_base import PB_CSS, hub
from .task_service import SqliteTaskService

CSS = """
:root{--bg:var(--pb-bg);--card:var(--pb-panel);--ink:var(--pb-ink);--muted:var(--pb-muted);--line:var(--pb-line);--accent:var(--pb-accent);--warn:var(--pb-warn);--ok:var(--pb-ok);--soft:var(--pb-warn)}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--ink);font:var(--pb-fs)/1.55 var(--pb-font)}
main{max-width:1100px;margin:0 auto;padding:16px}h1{font-size:20px;margin:4px 0 2px}h2{font-size:15px;margin:0 0 8px;color:var(--muted);font-weight:600}
nav{display:flex;flex-wrap:wrap;gap:4px;margin:0 0 12px;border-bottom:1px solid var(--line)}nav a{padding:6px 12px;border:0;border-bottom:2px solid transparent;color:var(--muted)}
nav a.on{color:var(--ink);border-bottom-color:var(--accent);font-weight:600}
.sub{color:var(--muted);font-size:13px;margin-bottom:12px}.grid{display:grid;gap:12px;grid-template-columns:repeat(auto-fit,minmax(300px,1fr))}
section{background:var(--card);border:1px solid var(--line);border-radius:var(--pb-r-lg);padding:12px 14px}
.focus{display:grid;gap:8px;grid-template-columns:repeat(auto-fit,minmax(220px,1fr));margin-bottom:12px}
.slot{background:var(--card);border:1px solid var(--line);border-radius:var(--pb-r-lg);padding:10px 12px}.slot b{display:block;font-size:12px;color:var(--accent);letter-spacing:.04em}
ul{list-style:none;margin:0;padding:0}li{padding:6px 0;border-top:1px solid var(--line)}li:first-child{border-top:0}
.meta{color:var(--muted);font-size:12px}a{color:inherit;text-decoration:none;border-bottom:1px dotted var(--line)}.warn{color:var(--warn)}.ok{color:var(--ok)}.empty{color:var(--muted);font-size:13px}
.tag{display:inline-block;font-size:11px;padding:0 6px;border-radius:9px;border:1px solid var(--line);margin-right:6px;color:var(--muted)}
.tag.official{color:var(--warn);border-color:var(--warn)}.tag.soft{color:var(--soft);border-color:var(--soft)}.tag.follow{color:var(--accent);border-color:var(--accent)}
.pills{display:flex;gap:6px;flex-wrap:wrap;margin-bottom:10px}.pills a{padding:2px 10px;border:1px solid var(--line);border-radius:12px;font-size:13px}.pills a.on{border-color:var(--accent);color:var(--accent)}
form.filters{display:flex;gap:8px;flex-wrap:wrap;align-items:center;margin-bottom:12px;font-size:13px}form.filters select,form.filters button{font:inherit;font-size:13px;padding:3px 6px;background:var(--card);color:var(--ink);border:1px solid var(--line);border-radius:6px}
.bar{height:8px;background:var(--line);border-radius:4px;overflow:hidden}.bar i{display:block;height:100%;background:var(--accent)}
table{width:100%;border-collapse:collapse;font-size:14px}td,th{padding:6px 4px;border-top:1px solid var(--line);text-align:left}th{color:var(--muted);font-weight:600;font-size:12px}
.num{font-family:var(--pb-mono);font-variant-numeric:tabular-nums;text-align:right;white-space:nowrap}
.lines{display:grid;gap:12px}.line h2{display:flex;flex-wrap:wrap;gap:6px;align-items:baseline;color:var(--ink);font-size:16px;margin:0 0 2px}
.line .tag{font-weight:400}.line ol{list-style:none;margin:8px 0 0;padding:0 0 0 14px;border-left:2px solid var(--line)}
.line .day{position:relative;padding:2px 0 8px}.line .day:before{content:"";position:absolute;left:-20px;top:8px;width:10px;height:10px;border-radius:50%;background:var(--card);border:2px solid var(--accent)}
.line .day>b{font-size:13px;color:var(--accent)}.line .step{padding:3px 0 0;border:0}.line .step .when{color:var(--muted);font-variant-numeric:tabular-nums;margin-right:6px}
main.wide{max-width:1360px}
.mail{display:grid;grid-template-columns:320px minmax(0,1fr);background:var(--card);border:1px solid var(--line);border-radius:var(--pb-r-lg);min-height:calc(100vh - 190px);overflow:hidden}
.rail{border-right:1px solid var(--line);padding:0 0 12px;max-height:calc(100vh - 190px);overflow:auto}
.rail h4{display:flex;justify-content:space-between;font-size:12px;font-weight:600;color:var(--muted);margin:14px 16px 4px}
.rail h4.bad{color:var(--pb-bad)}.rail h4 .n{font-family:var(--pb-mono);font-weight:400}
.rail a.it{display:block;padding:7px 16px 7px 13px;border:0;border-left:3px solid transparent;color:inherit}
.rail a.it:hover{background:var(--pb-hover)}
.rail a.it .t{display:block;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.rail a.it .m{display:block;font-size:12px;color:var(--muted);white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.rail a.it.w .m{color:var(--warn)}.rail a.it.b .m{color:var(--pb-bad)}
.rail a.it.sel{background:var(--pb-accent-soft);border-left-color:var(--accent)}
.rail .none{font-size:12px;color:var(--muted);padding:2px 16px}
.rail details.done summary{cursor:pointer;font-size:12px;color:var(--muted);margin:16px 16px 4px}
.rail details.done a.it .t{color:var(--muted);text-decoration:line-through;text-decoration-color:var(--line)}
.quick{padding:12px;border-bottom:1px solid var(--line);background:var(--bg)}
.quick form{display:flex;flex-wrap:wrap;gap:6px;margin:0}
.quick input,.quick select,.quick button{font:inherit;font-size:14px;height:34px;padding:0 10px;background:var(--card);color:var(--ink);border:1px solid var(--line);border-radius:var(--pb-r)}
.quick input[name=title]{flex:1 1 100%}.quick select{flex:1 1 0;min-width:0}
.quick button.primary{background:var(--accent);border-color:var(--accent);color:var(--pb-accent-ink);font-weight:600;cursor:pointer}
.quick details{flex:1 1 100%;font-size:12px;color:var(--muted)}.quick details summary{cursor:pointer;padding:2px 0}
.quick details .more{display:flex;flex-wrap:wrap;gap:6px;margin-top:6px}.quick details .more>*{flex:1 1 140px}
.quick label{display:flex;flex-direction:column;gap:2px}
.pane{padding:22px 28px;border:0;border-radius:0;min-width:0}
.pane .head{display:flex;justify-content:space-between;gap:16px;align-items:flex-start;margin-bottom:12px}
.pane h2{font-size:22px;color:var(--ink);margin:0 0 6px;font-weight:600}
.pane .who{display:flex;flex-wrap:wrap;gap:4px 18px;font-size:13px;color:var(--muted)}.pane .who b{color:var(--ink);font-weight:600}
.chips{display:flex;gap:6px}
.chip{display:inline-block;font-size:12px;line-height:20px;padding:0 7px;border-radius:4px;border:1px solid var(--line);color:var(--muted);white-space:nowrap}
.chip.do{color:var(--accent);border-color:var(--accent)}.chip.warn{color:var(--warn);border-color:var(--warn)}
.chip.ok{color:var(--ok);border-color:var(--ok)}.chip.bad{color:var(--pb-bad);border-color:var(--pb-bad)}
.rail .chip{font-size:11px;line-height:16px;padding:0 4px;margin-right:2px}
.waitbox{display:flex;gap:14px;align-items:center;border:1px solid var(--warn);border-radius:var(--pb-r-lg);padding:12px 14px;margin:4px 0 18px}
.waitbox .big{font:600 22px var(--pb-mono);color:var(--warn);white-space:nowrap}
dl.facts{display:grid;grid-template-columns:repeat(auto-fit,minmax(180px,1fr));gap:10px 24px;margin:0 0 18px}
dl.facts dt{font-size:12px;color:var(--muted)}dl.facts dd{margin:0}
.sub-h{font-size:13px;color:var(--muted);font-weight:600;margin:20px 0 8px}
ol.thread{list-style:none;margin:0;padding:0 0 0 22px;position:relative}
ol.thread:before{content:"";position:absolute;left:6px;top:6px;bottom:6px;width:2px;background:var(--line)}
ol.thread li.ev{position:relative;padding:0 0 14px;border:0}
ol.thread li.ev:before{content:"";position:absolute;left:-21px;top:5px;width:10px;height:10px;border-radius:50%;background:var(--card);border:2px solid var(--accent)}
ol.thread li.ev.w:before{border-color:var(--warn)}ol.thread li.ev.ok:before{border-color:var(--ok)}
ol.thread li.ev.now:before{background:var(--warn);border-color:var(--warn)}
ol.thread .when{font:12px var(--pb-mono);color:var(--muted)}ol.thread .what{font-weight:500}
.slots{display:grid;grid-template-columns:repeat(3,1fr);gap:8px;margin-bottom:16px}
.slots .slot{display:block;border:1px solid var(--line);border-radius:var(--pb-r);padding:8px 10px;color:inherit}
.slots .slot b{display:block;font-size:12px;color:var(--accent)}.slots .slot.none{color:var(--muted)}
.quiet{font-size:13px;color:var(--muted);border:1px dashed var(--line);border-radius:var(--pb-r);padding:10px 12px}
section.alert{border-color:var(--warn);margin-bottom:10px}section.alert h3{font-size:14px;color:var(--warn);margin:0 0 6px}
.pane section.owner h2{font-size:13px;color:var(--muted)}
.pane details.edit summary{cursor:pointer;font-size:13px;color:var(--muted);margin:10px 0 4px}
.pane section.owner{margin-top:22px;border-top:1px solid var(--line);border-radius:0;border-left:0;border-right:0;border-bottom:0;padding:16px 0 0}
@media (max-width:860px){.mail{display:flex;flex-direction:column;min-height:0}.rail{border-right:0;border-bottom:1px solid var(--line);max-height:none}
  .mail.has-sel .pane{order:-1;border-bottom:1px solid var(--line)}.pane{padding:16px}.slots{grid-template-columns:1fr}main{padding:12px}}
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


def _page(title: str, active: str, body: str, now: str, tz: ZoneInfo, *, sub: str = "", owner: bool = False,
          wide: bool = False) -> str:
    tabs = "".join(f"<a href='{href}' class='{'on' if key == active else ''}'>{label}</a>" for key, href, label in NAV)
    stamp = _local(now, tz, "%Y-%m-%d %H:%M")
    return (f"<!doctype html><html lang=zh-CN><head><meta charset=utf-8>"
            f"<meta name=viewport content='width=device-width,initial-scale=1'><title>{e(title)}</title>"
            f"<style>{PB_CSS}{CSS}{FORM_CSS if owner else ''}</style></head><body>{hub('workbench')}<main{' class=wide' if wide else ''}><h1>{e(title)}</h1>"
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
# Direction C「往来」(2026-11-22): like a mail client. Left, every open task grouped by whose hands it is in
# (me / a person / an outside condition / the inbox); right, the selected task's whole line and what can be done
# with it, or, with nothing selected, what needs attention today. /task/<id> is the same page with that task open.
def _age(ts: str | None, now: str) -> str:
    if not ts:
        return ""
    try:
        then = datetime.fromisoformat(ts[:-1] + "+00:00" if ts.endswith("Z") else ts)
        if then.tzinfo is None:
            then = then.replace(tzinfo=timezone.utc)
        sec = (datetime.fromisoformat(now[:-1] + "+00:00") - then).total_seconds()
    except ValueError:
        return ""
    if sec < 3600:
        return f"{max(1, int(sec // 60))} 分钟"
    if sec < 86400 * 2:
        return f"{int(sec // 3600)} 小时"
    return f"{int(sec // 86400)} 天"


def _holder(deps, actor, names) -> str | None:
    """Whose answer an open wait is for: a person's name, or None for an outside condition. Waits recorded
    before the person was a member carry no responsible id; an outcome that starts with their name still counts."""
    people = {pid: n for pid, n in names.items() if pid != actor.principal_id and n}
    for d in deps:
        if d.get("holder_name"):
            return d["holder_name"]
        who = d.get("responsible_principal_id")
        if who in people:
            return people[who]
        for n in people.values():
            if (d.get("awaited_outcome") or "").startswith(n):
                return n
    return None


def _rail(ov, actor, names, tz, now, selected, quick_add) -> str:
    slot_of = {t["id"]: s for s, t in ov["focus"].items() if t}

    def item(t, meta, cls=""):
        sel = " sel" if t["id"] == selected else ""
        slot = f"<span class='chip do'>{e(slot_of[t['id']])}</span> " if t["id"] in slot_of else ""
        return (f"<a class='it{cls}{sel}' href='/task/{e(t['id'])}'><span class=t>{slot}{e(t['title'])}</span>"
                f"<span class=m>{meta}</span></a>")

    def due_or_age(t):
        soft = _local(t.get("soft_due_at"), tz, "%m-%d")
        due = t.get("official_due_local") or _local(t.get("official_due_at"), tz, "%m-%d")
        if due:
            return f"官方截止 {e(due)}"
        return f"期望 {e(soft)}" if soft else f"{_age(t.get('created_at'), now)}前记下"

    groups: list[tuple[str, str, list[str]]] = []
    mine = ov["doing"] + ov["ready"]
    groups.append(("我在做", "", [item(t, f"{STATE_LABELS.get(t['state'], '')} · {due_or_age(t)}") for t in mine]))
    by_person: dict[str, list[str]] = {}
    outside: list[str] = []
    for t in ov["waiting"]:
        since = min((d.get("created_at") or "" for d in t.get("waiting_on", [])), default="") or t.get("updated_at")
        holder = _holder(t.get("waiting_on", []), actor, names)
        if holder:
            by_person.setdefault(holder, []).append(item(t, f"等{e(holder)}回复 · {_age(since, now)}", " w"))
        else:
            what = "；".join(e(d["awaited_outcome"]) for d in t.get("waiting_on", [])) or "等待中"
            outside.append(item(t, f"等{what} · {_age(since, now)}", " w"))
    for person, rows in by_person.items():
        groups.append((f"在{e(person)}手上", "", rows))
    if outside:
        groups.append(("等外部条件", "", outside))
    if ov["blocked"]:
        groups.append(("阻塞", " bad", [item(t, e(t.get("next_action") or "阻塞中"), " b") for t in ov["blocked"]]))
    if ov["overdue"]:
        groups.append(("逾期", " bad", [item(t, due_or_age(t), " b") for t in ov["overdue"]]))
    groups.append(("收件箱", "", [item(t, due_or_age(t)) for t in ov["inbox"]]))

    html = [quick_add]
    for title, cls, rows in groups:
        body = "".join(rows) or "<div class=none>（空）</div>"
        html.append(f"<h4 class='g{cls}'><span>{title}</span><span class=n>{len(rows)}</span></h4>{body}")
    done = "".join(item(t, "完成") for t in ov["recently_done"])
    if done:
        html.append(f"<details class=done><summary>最近 7 天完成 {len(ov['recently_done'])} 件</summary>{done}</details>")
    return "<aside class=rail>" + "".join(html) + "</aside>"


def _today_pane(ov, recent, stuck, load, tz, names) -> str:
    focus = [(s, ov["focus"].get(s)) for s in ("NOW", "NEXT1", "NEXT2")]
    if any(t for _, t in focus):
        slots = "".join(f"<a class=slot href='/task/{e(t['id'])}'><b>{s}</b>{e(t['title'])}</a>" if t else
                        f"<div class='slot none'><b>{s}</b>空</div>" for s, t in focus)
        focus_html = f"<div class=slots>{slots}</div>"
    else:
        focus_html = "<p class=quiet>还没有放进 NOW 的事。从左边点开一件，在下面设为 NOW。</p>"
    attention = [
        ("待审批", ov["my_approvals"], lambda a: f"{e(a['request_summary'])}<div class=meta>/approve {e(a['id'])}"
                                              f"{' · 到期 ' + _local(a['expires_at'], tz) if a['expires_at'] else ''}</div>"),
        ("结果不明、需要核对", ov["unknown_outcomes"], lambda u: f"{e(u['kind'])} · {e(u['id'])}"
                                                         f"<div class=meta>{_local(u['updated_at'], tz)}</div>"),
        ("需要我处理", ov["my_human_actions"], lambda h: f"{e(h['title'])}<div class=meta>{e(h['request_kind'])} · "
                                                    f"{e(h['urgency'])}{' · 截止 ' + _local(h['due_at'], tz) if h['due_at'] else ''}</div>"),
        ("承诺", ov["commitments_open"], lambda c: f"{e(c['promise_text'])}<div class=meta>"
                                                  f"{'我欠' if c['direction'] == 'owner_owes' else '欠我' if c['direction'] == 'owed_to_owner' else '他人'}"
                                                  f" · {e(c.get('due_local') or _local(c['due_at'], tz))}</div>"),
        ("交付物", ov["deliverables_pending"], lambda d: f"{e(d['title'])}<div class=meta>{e(d['status'])} · 第 {d['revision']} 版</div>"),
        ("即将催促 / 提醒", ov["upcoming_followups"], lambda r: f"<a href='/task/{e(r['task_id'])}'>{e(r['title'])}</a>"
                                                       f"<div class=meta>{_local(r['due_at'], tz)} · 给 {e(names.get(r['recipient_principal_id'], ''))}</div>"),
        ("停滞与反复延期", stuck, lambda s: f"{e(s['title'])}<div class=meta>延期 {len(s['delays'])} 次</div>"),
        ("排期过载的日子", [d for d in load["days"] if d["overloaded"]],
         lambda d: f"{e(d['date'])}<div class=meta>工作量 {d['load']} 项</div>"),
        ("排期建议（只动期望完成，需你确认）", load.get("suggestions", []),
         lambda s: f"<a href='/task/{e(s['task_id'])}'>{e(s['title'])}</a><div class=meta>{e(s['from'])} → {e(s['to'])} · {e(s['basis'])}</div>"),
    ]
    full = [(t, items, r) for t, items, r in attention if items]
    quiet = [t for t, items, _ in attention if not items]
    alerts = "".join(f"<section class=alert><h3>{t}</h3>{_list(items, r)}</section>" for t, items, r in full)
    calm = f"<p class=quiet>今天没有：{'、'.join(quiet)}。</p>" if quiet else ""
    changes = _list(recent[:12], lambda ev: (
        f"<a href='/task/{e(ev['task_id'])}'>{e(ev['title'])}</a> · {KIND_LABELS.get(ev['kind'], e(ev['kind']))}"
        f"<div class=meta>{_local(ev['at'], tz)}{' · ' + e(ev['reason']) if ev.get('reason') else ''}</div>"),
        empty="三天里没有新变化")
    return (f"<div class=head><h2>今天</h2></div>{focus_html}{alerts}{calm}"
            f"<h3 class=sub-h>最近三天的变化</h3>{changes}")


SOURCE_WORDS = (("tg://", "Telegram"), ("owner-web:", "网页工作台"), ("claude-code:", "Claude Code"),
                ("xiyue-automation:", "溪月定时任务"), ("xiyue_todo", "溪月旧待办"))


def _source_word(ref: str | None) -> str:
    for prefix, word in SOURCE_WORDS:
        if ref and ref.startswith(prefix):
            return word
    return ""


def _task_pane(view, actor, names, tz, now, panel) -> str:
    t = view["task"]
    open_deps = [d for d in view["dependencies"] if d.get("status") == "open"]
    holder = _holder(open_deps, actor, names)
    state = STATE_LABELS.get(t["state"], t["state"])
    chip = {"WAITING": "warn", "DOING": "do", "READY": "do", "DONE": "ok", "BLOCKED": "bad"}.get(t["state"], "")
    focus = f"<span class='chip do'>{e(view['focus'][0])}</span>" if view.get("focus") else ""
    first_src = next((ev["source_ref"] for ev in view["events"] if ev.get("source_ref")), "")
    who = [("负责", holder or "我"), ("请求", names.get(t["requester_id"], "")), ("来自", _source_word(first_src))]
    who_html = "".join(f"<span>{k} <b>{e(v)}</b></span>" for k, v in who if v)
    wait = ""
    if t["state"] == "WAITING" and open_deps:
        since = min(d["created_at"] for d in open_deps)
        what = "；".join(e(d["awaited_outcome"]) for d in open_deps)
        wait = (f"<div class=waitbox><span class=big>{_age(since, now)}</span><div><b>等{what}</b>"
                f"<div class=meta>{_local(since, tz)} 开始等{' · 工作 bot 上线后会自动提醒对方' if holder else ''}</div></div></div>")
    facts = [("下一步", t["next_action"]), ("目标", t["goal"]), ("期望完成", _local(t["soft_due_at"], tz)),
             ("官方截止", t.get("official_due_local") or _local(t["official_due_at"], tz)), ("已做", t["done_summary"]),
             ("优先级", t.get("priority"))]
    facts_html = "".join(f"<div><dt>{k}</dt><dd>{e(v)}</dd></div>" for k, v in facts if v not in (None, ""))
    steps = []
    for ev in view["events"]:
        cls = " w" if ev["kind"] in ("dependency_added", "human_action_requested") else ""
        cls = " ok" if ev["kind"] in ("dependency_resolved", "human_action_responded", "deliverable_accepted") else cls
        src = _source_word(ev.get("source_ref"))
        steps.append(f"<li class='ev{cls}'><div class=when>{_local(ev['occurred_at'], tz)} · "
                     f"{e(names.get(ev['actor_id'], ev['actor_id']))}{' · ' + src if src else ''}</div>"
                     f"<div class=what>{KIND_LABELS.get(ev['kind'], e(ev['kind']))}"
                     f"{'：' + e(ev['reason']) if ev['reason'] else ''}</div></li>")
    if t["state"] == "WAITING" and open_deps:
        steps.append("<li class='ev now'><div class=when>现在</div><div class=what>还没有回音</div></li>")
    chk = view.get("latest_checkpoint")
    bookmark = (f"<h3 class=sub-h>工作书签</h3><p>{e(chk['next_action'])}</p><div class=meta>已完成 "
                f"{e(', '.join(json.loads(chk['completed_steps_json'])))} · 未核实 {e(', '.join(json.loads(chk['unverified_json'])))}</div>"
                if chk else "")
    return (f"<div class=head><div><h2>{e(t['title'])}</h2><div class=who>{who_html}</div></div>"
            f"<div class=chips>{focus}<span class='chip {chip}'>{e(state)}</span></div></div>{wait}"
            f"{'<dl class=facts>' + facts_html + '</dl>' if facts_html else ''}"
            f"<h3 class=sub-h>经过</h3><ol class=thread>{''.join(steps)}</ol>{bookmark}{panel}")


def render_dashboard(tasks, actor, database: str, now: str, *, owner_html: str = "", selected: str | None = None,
                     owner_panel=None, notice: str = "") -> str:
    """owner_html: the owner's quick-add form (empty on the read-only view). With `selected`, the right pane is
    that task; owner_panel(task, focus_slot, tz) -> its owner forms, or None on the read-only view."""
    tz, names = _ctx(actor, database)
    ov = tasks.overview(actor=actor, limit=30)
    title = "工作台"
    if selected:
        view = tasks.read(actor=actor, task_id=selected)
        panel = ""
        if owner_panel is not None:
            slot = view["focus"][0] if view.get("focus") else None
            opened = [d for d in view["dependencies"] if d.get("status") == "open"]
            panel = owner_panel(dict(view["task"], open_dependencies=opened,
                                     holder=_holder(opened, actor, names)), slot, tz)
        right = _task_pane(view, actor, names, tz, now, panel)
    else:
        since = (datetime.fromisoformat(now[:-1]) - timedelta(days=3)).strftime("%Y-%m-%dT%H:%M:%SZ")
        recent = [i for i in views.timeline(database, actor=actor, start=since, end=_plus_seconds(now, 1), limit=40)["items"]
                  if i["type"] == "task_event"]
        right = _today_pane(ov, recent, stagnation(database, actor=actor, now=now),
                            schedule_load(database, actor=actor, now=now), tz, names)
    body = (f"<div class='mail{' has-sel' if selected else ''}'>{_rail(ov, actor, names, tz, now, selected, owner_html)}"
            f"<section class=pane>{notice}{right}</section></div>")
    return _page(title, "work", body, now, tz, owner=bool(owner_html) or owner_panel is not None, wide=True)


# ----------------------------------------------------------------------------------- Task
def render_task(tasks, actor, database: str, task_id: str, now: str, *, owner_panel=None, notice: str = "",
                owner_html: str = "") -> str:
    """The work page with this task open on the right."""
    return render_dashboard(tasks, actor, database, now, owner_html=owner_html, selected=task_id,
                            owner_panel=owner_panel, notice=notice)


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
