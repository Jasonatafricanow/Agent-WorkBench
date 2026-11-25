"""The owner's own write path on the dashboard: create tasks and move them along without the work bot.

Design (DEV_LOG §2.3 rule 9, corrected 11-18): the owner manages their own work directly; requesters come
in through the work bot. Signing in to the dashboard with the OWNER token (not the read-only dashboard
password) adds these forms. Each POST must carry the page's CSRF value and, when the browser sends one, an
Origin of this same host. The owner's direct action is itself the evidence: operations go to TaskService
as the owner with actor source "owner", recorded as explicit_user with source_ref "owner-web:<nonce>" and a
reason, so the event trail still says who did what, where and why. The nonce is the idempotency key, so a
resubmitted form replays its receipt instead of acting twice.
"""
from __future__ import annotations

import hashlib
import hmac
import secrets
from datetime import datetime, timezone
from html import escape
from zoneinfo import ZoneInfo

from .errors import InvalidOperation
from .task_service import DELAY_KINDS, TRANSITIONS

STATE_LABELS = {"INBOX": "收件箱", "READY": "可做", "DOING": "进行中", "WAITING": "等待", "BLOCKED": "阻塞",
                "DONE": "完成", "CANCELLED": "取消"}
DELAY_LABELS = {"external_wait": "等外部", "reprioritized": "调整优先级", "underestimated": "低估了工作量",
                "blocked": "被阻塞", "unavailable": "没空", "scope_changed": "范围变了", "other": "其他"}


def e(value) -> str:
    return escape("" if value is None else str(value), quote=True)


def csrf_value(owner_token: str) -> str:
    return hmac.new(owner_token.encode(), b"workbench-owner-form", hashlib.sha256).hexdigest()


def _hidden(csrf: str, back: str, **extra) -> str:
    fields = {"csrf": csrf, "nonce": secrets.token_hex(12), "back": back, **extra}
    return "".join(f"<input type=hidden name={k} value='{e(v)}'>" for k, v in fields.items())


def _local_input_to_utc(value: str, tz: ZoneInfo) -> str | None:
    """'2026-11-20' (end of that day) or '2026-11-20T15:00' in the workspace's timezone -> UTC instant."""
    value = (value or "").strip()
    if not value:
        return None
    try:
        moment = (datetime.fromisoformat(value + "T23:59") if len(value) == 10 else datetime.fromisoformat(value))
    except ValueError as exc:
        raise InvalidOperation("日期格式不对", field="due") from exc
    return moment.replace(tzinfo=tz).astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _utc_to_input(value: str | None, tz: ZoneInfo) -> str:
    if not value:
        return ""
    return datetime.fromisoformat(value[:-1] + "+00:00").astimezone(tz).strftime("%Y-%m-%dT%H:%M")


FORM_CSS = """
.owner{border-color:var(--accent)}.owner h2{color:var(--accent)}
.owner form{display:flex;flex-wrap:wrap;gap:8px;align-items:center;margin:6px 0}
.owner input,.owner select,.owner textarea,.owner button{font:inherit;font-size:14px;padding:6px 8px;min-height:36px;background:var(--card);color:var(--ink);border:1px solid var(--line);border-radius:6px}
.owner input[type=text],.owner textarea{flex:1 1 220px;min-width:0}.owner button{cursor:pointer}.owner button.primary{background:var(--accent);color:var(--pb-accent-ink);border-color:var(--accent)}
.owner button.danger{color:var(--warn);border-color:var(--warn)}.owner .row{display:flex;flex-wrap:wrap;gap:6px}
form.inline-act{display:flex;flex-wrap:wrap;gap:6px;align-items:center;margin:6px 0 0}
form.inline-act input{font:inherit;font-size:13px;padding:3px 8px;min-height:28px;background:var(--card);color:var(--ink);border:1px solid var(--line);border-radius:5px;flex:1 1 160px;min-width:0}
form.inline-act button{font:inherit;font-size:12px;padding:3px 10px;min-height:28px;background:var(--card);color:var(--ink);border:1px solid var(--line);border-radius:5px;cursor:pointer}
form.inline-act button.primary{background:var(--accent);color:var(--pb-accent-ink);border-color:var(--accent)}
form.inline-act button.danger{color:var(--warn);border-color:var(--warn)}
.notice{padding:8px 12px;border-radius:8px;margin-bottom:12px;border:1px solid var(--line)}.notice.bad{color:var(--warn);border-color:var(--warn)}.notice.good{color:var(--ok);border-color:var(--ok)}
"""


def assignable(conn, workspace_id: str) -> list[tuple[str, str]]:
    """People the owner can hand a new task to: the workspace's other active human members."""
    return [(r[0], r[1]) for r in conn.execute(
        """SELECT p.id, p.display_name FROM memberships m JOIN principals p ON p.id=m.principal_id
           WHERE m.workspace_id=? AND m.active=1 AND p.active=1 AND p.kind='human' AND m.role!='owner'
           ORDER BY p.display_name""", (workspace_id,))]


def _who_input(people, recent, placeholder: str) -> str:
    names = list(dict.fromkeys([n for _, n in people] + list(recent)))
    options = "".join(f"<option value='{e(n)}'>" for n in names)
    return (f"<input type=text name=holder list=wb-holders maxlength=80 placeholder='{e(placeholder)}' aria-label='谁来做'>"
            f"<datalist id=wb-holders>{options}</datalist>")


def _project_select(projects: list[tuple[str, str]], selected: str | None = None, empty: str = "不归入项目") -> str:
    options = "".join(
        f"<option value='{e(pid)}'{' selected' if selected == pid else ''}>{e(title)}</option>"
        for pid, title in projects)
    return f"<select name=project_id aria-label='所属项目'><option value=''>{empty}</option>{options}</select>"


def today_focus_button(csrf: str, task_id: str, back: str = "/") -> str:
    return (f"<form method=post action=/owner/ops class=inline-act>"
            f"{_hidden(csrf, back, action='focus', slot='NOW', task_id=task_id)}"
            "<button>设为 NOW</button></form>")


def approval_buttons(csrf: str, approval_id: str, back: str = "/") -> str:
    return (f"<form method=post action=/owner/ops class=inline-act>"
            f"{_hidden(csrf, back, action='approval', approval_id=approval_id)}"
            "<button class=primary name=approve value=1>批准</button>"
            "<button class=danger name=approve value=0>拒绝</button></form>")


def human_action_form(csrf: str, request_id: str, back: str = "/") -> str:
    return (f"<form method=post action=/owner/ops class=inline-act>"
            f"{_hidden(csrf, back, action='human_reply', human_action_id=request_id)}"
            "<input type=text name=reply placeholder='答复说明（可选）' maxlength=400>"
            "<button class=primary name=status value=completed>确认处理</button>"
            "<button class=danger name=status value=declined>拒绝</button></form>")


def quick_add_form(csrf: str, back: str = "/", people: list[tuple[str, str]] = (), recent: list[str] = (),
                   projects: list[tuple[str, str]] = ()) -> str:
    """The work page's one-line add: a title and who does it; the rest folds away. Handing it to someone works like
    sending a mail: the task stays on the owner's board in WAITING until that person answers."""
    assign = _who_input(people, recent, "交给谁（可空）")
    priorities = "".join(f"<option value={p}{' selected' if p == 2 else ''}>优先级 {p}</option>" for p in range(5))
    ask = "<input type=text name=ask placeholder='交给别人时：要对方做什么' maxlength=400>"
    proj = _project_select(projects) if projects else ""
    return (
        f"<div class=quick><form method=post action=/owner/ops>{_hidden(csrf, back, action='create')}"
        "<input type=text name=title placeholder='新建或指派：要做什么' required maxlength=200 aria-label='要做什么'>"
        f"{assign}<button class=primary>记下</button>"
        "<details><summary>更多：下一步、截止、项目、状态、焦点</summary><div class=more>"
        f"{ask}<input type=text name=next_action placeholder='下一步（可空）' maxlength=400>"
        "<label>期望完成<input type=datetime-local name=soft_due></label>"
        "<label>官方截止<input type=date name=official_due></label>"
        f"{proj}<select name=priority>{priorities}</select>"
        "<select name=state><option value=INBOX>先放收件箱</option><option value=READY>可做</option><option value=DOING>马上做</option></select>"
        "<select name=focus><option value=''>不设焦点</option><option value=NOW>设为 NOW</option>"
        "<option value=NEXT1>设为 NEXT1</option><option value=NEXT2>设为 NEXT2</option></select>"
        "</div></details></form></div>")


def _post(csrf: str, back: str, inner: str, **fields) -> str:
    return f"<form method=post action=/owner/ops>{_hidden(csrf, back, **fields)}{inner}</form>"


def task_panel(csrf: str, task: dict, focus_slot: str | None, tz: ZoneInfo) -> str:
    """Moves allowed from the task's state (WAITING is left out: it needs a dependency, which the work bot
    or Xiyue records), focus slots, editing and a progress note. Closing, reopening and blocking ask for a reason."""
    tid, version, state = task["id"], task["version"], task["state"]
    back = f"/task/{tid}"
    common = {"task_id": tid, "version": version}
    moves = sorted(TRANSITIONS.get(state, ()) - {"WAITING"}, key=list(STATE_LABELS).index)
    if state == "WAITING" and task.get("open_dependencies"):
        moves = [m for m in moves if m not in ("READY", "DOING")]  # "收回自己做" also ends the wait
    closed = state in ("DONE", "CANCELLED")
    needs_reason = moves if closed else [m for m in moves if m in ("DONE", "CANCELLED", "BLOCKED")]
    plain = [m for m in moves if m not in needs_reason]

    buttons = "".join(_post(csrf, back, f"<button>改为{STATE_LABELS[m]}</button>", action="move", to=m, **common)
                      for m in plain)
    reasoned = ""
    if needs_reason:
        options = "".join(f"<option value={m}>{('重新打开为' if closed else '') + STATE_LABELS[m]}</option>"
                          for m in needs_reason)
        extra = {"reopen": "1"} if closed else {}
        reasoned = _post(csrf, back, f"<select name=to>{options}</select>"
                                     "<input type=text name=reason placeholder='原因（必填）' required maxlength=400>"
                                     "<button class=danger>确定</button>", action="move", **common, **extra)
    focus = "".join(_post(csrf, back, f"<button{' class=primary' if focus_slot == slot else ''}>{slot}</button>",
                          action="focus", slot=slot, task_id=tid) for slot in ("NOW", "NEXT1", "NEXT2"))
    if focus_slot:
        focus += _post(csrf, back, "<button>取消焦点</button>", action="unfocus", slot=focus_slot)
    if closed:
        return (f"<section class=owner><h2>我来处理</h2>{reasoned}</section>")
    priorities = "".join(f"<option value={p}{' selected' if p == task.get('priority') else ''}>优先级 {p}</option>"
                         for p in range(5))
    delay = "".join(f"<option value={k}>{DELAY_LABELS[k]}</option>" for k in sorted(DELAY_KINDS))
    official_val = (task.get("official_due_local") or _utc_to_input(task.get("official_due_at"), tz))[:10]
    proj_select = _project_select(task.get("projects", ()), task.get("project_id"), "未归入项目") if task.get("projects") else ""
    extra_edit = {"has_project": "1"} if task.get("projects") else {}
    edit = "<details class=edit><summary>修改标题、下一步、截止、项目、优先级</summary>" + _post(csrf, back,
                 f"<input type=text name=title value='{e(task['title'])}' required maxlength=200>"
                 f"<input type=text name=next_action value='{e(task.get('next_action'))}' placeholder='下一步' maxlength=400>"
                 f"<label class=meta>期望完成 <input type=datetime-local name=soft_due value='{_utc_to_input(task.get('soft_due_at'), tz)}'></label>"
                 f"<label class=meta>官方截止 <input type=date name=official_due value='{e(official_val)}'></label>"
                 f"{proj_select}<select name=priority>{priorities}</select>"
                 f"<select name=delay_kind><option value=''>（往后推时选原因）</option>{delay}</select>"
                 "<input type=text name=reason placeholder='往后推的原因' maxlength=400>"
                 "<button>保存修改</button>", action="edit", prev_official_due=official_val, **extra_edit, **common) + "</details>"
    progress = _post(csrf, back, "<input type=text name=text placeholder='记一笔进展（对方的回复也记在这）' required maxlength=1000>"
                                 "<button>记下</button>", action="progress", **common)
    open_deps = ",".join(d["id"] for d in task.get("open_dependencies", []))
    holder = task.get("holder")
    hand = _post(csrf, back,
                 _who_input(task.get("people", ()), task.get("recent", ()), "交给谁") +
                 "<input type=text name=ask placeholder='要对方做什么（可空）' maxlength=400>"
                 f"<button class=primary>{'改交给他' if holder else '交给他'}</button>",
                 action="handoff", state=state, cancel=open_deps, title=task["title"], **common)
    take = (_post(csrf, back, f"<button>收回自己做</button>", action="takeback", cancel=open_deps, **common)
            if state == "WAITING" and open_deps else "")
    who = (f"<div class=meta>现在在 {e(holder)} 手上</div>" if holder else "<div class=meta>交给别人（任何名字都行，不必是成员）</div>")
    return (f"<section class=owner><h2>处理</h2>{progress}{who}{hand}<div class=row>{take}{buttons}</div>{reasoned}"
            f"<div class=meta>焦点</div><div class=row>{focus}</div>{edit}</section>")


def _holder_of(form: dict, people: dict[str, str] | None) -> tuple[str | None, str] | None:
    """(member id or None, name) from the form: `assign_to` (a member id) or `holder` (any name; a member's name
    counts as that member)."""
    people = people or {}
    to = (form.get("assign_to") or "").strip()
    if to:
        if to not in people:
            raise InvalidOperation("只能指派给工作区里的人", field="assign_to")
        return to, people[to]
    name = (form.get("holder") or "").strip()[:80]
    if not name:
        return None
    member = next((pid for pid, n in people.items() if n == name), None)
    return member, name


def _hand_to(holder, task_ref: str, title: str, ask: str, base: dict, transition_ref: str, *,
             already_waiting: bool = False) -> list[dict]:
    """Wait on `holder`. A member also gets a request (sent once a bot is connected); anyone else is just a name."""
    member, name = holder
    wait = {"op": "add_dependency", "ref": "wait", "task_id": task_ref, "kind": "external_response",
            "awaited_outcome": f"{name}{'回复' if member else '那边办好'}" + (f"：{ask}" if ask else ""),
            "holder_name": name, **base, "reason": f"交给{name}"}
    if member:
        wait["responsible_principal_id"] = member
    ops = [wait]
    if member:
        message = "\n".join(filter(None, [f"有件事想请你处理：{title}" if title else "有件事想请你处理", ask,
                                           "（直接回复这条消息就好）"]))
        ops.append({"op": "request_human_action", "task_id": task_ref, "assignee_principal_id": member,
                    "request_kind": "execution", "title": title or name, "message": message, "urgency": "notify",
                    "dependency_id": "$wait", "request": {"ask": ask} if ask else {}, **base, "reason": f"交给{name}"})
    if not already_waiting:
        ops.append({"op": "transition", "task_id": transition_ref, "to": "WAITING", **base, "reason": f"等{name}"})
    return ops


def notice(query: dict) -> str:
    if query.get("err"):
        return f"<div class='notice bad'>没有执行：{e(query['err'])}</div>"
    if query.get("ok"):
        return f"<div class='notice good'>{e(query['ok'])}</div>"
    return ""


def operations(form: dict, tz: ZoneInfo, people: dict[str, str] | None = None) -> tuple[list[dict], str]:
    """A submitted form -> (TaskService operations, message on success). `people`: assignable id -> name."""
    action = form.get("action")
    ref = f"owner-web:{form.get('nonce', '')}"
    base = {"evidence_type": "explicit_user", "source_ref": ref}
    reason = (form.get("reason") or "").strip()
    version = int(form["version"]) if form.get("version", "").isdigit() else None
    if action == "create":
        op = {"op": "create_task", "ref": "new", "title": form.get("title", ""), "state": form.get("state") or "READY",
              "priority": int(form.get("priority") or 2), **base, "reason": "主人在网页工作台新建"}
        if form.get("next_action"):
            op["next_action"] = form["next_action"]
        if form.get("project_id"):
            op["project_id"] = form["project_id"]
        due = _local_input_to_utc(form.get("soft_due", ""), tz)
        if due:
            op["soft_due_at"] = due
        ops = [op]
        official = (form.get("official_due") or "").strip()
        if official:
            ops.append({"op": "set_official_due", "task_id": "$new", "local": official, **base,
                        "reason": "主人在网页工作台设置官方截止"})
        holder = _holder_of(form, people)
        if holder:
            op["state"] = "READY"
            ops += _hand_to(holder, "$new", op["title"].strip(), (form.get("ask") or "").strip(), base, "$new")
        if form.get("focus") in ("NOW", "NEXT1", "NEXT2"):
            ops.append({"op": "set_focus", "slot": form["focus"], "task_id": "$new", **base, "reason": "主人设焦点"})
        return ops, (f"已新建，交给{holder[1]}" if holder else "已新建")
    if action in ("handoff", "takeback"):
        tid = form.get("task_id")
        cancels = [d for d in (form.get("cancel") or "").split(",") if d]
        ops = [{"op": "resolve_dependency", "dependency_id": d, "resolution": "cancelled", **base,
                "reason": "改交给别人" if action == "handoff" else "主人收回自己做"} for d in cancels]
        if action == "takeback":
            if not cancels:
                raise InvalidOperation("这件事没有在等别人", field="cancel")
            ops.append({"op": "transition", "task_id": tid, "to": "READY", **base, "reason": "主人收回自己做"})
            ops[0]["expected_version"] = version
            return ops, "已收回，由你自己做"
        holder = _holder_of(form, people)
        if not holder:
            raise InvalidOperation("写上交给谁", field="holder")
        state = form.get("state")
        if state == "INBOX":
            ops.append({"op": "transition", "task_id": tid, "to": "READY", **base, "reason": f"交给{holder[1]}"})
        ops += _hand_to(holder, tid, form.get("title") or "", (form.get("ask") or "").strip(), base, tid,
                        already_waiting=(state == "WAITING"))
        ops[0]["expected_version"] = version
        return ops, f"已交给{holder[1]}"
    if action == "move":
        target = form.get("to")
        if (target in ("DONE", "CANCELLED", "BLOCKED") or form.get("reopen")) and not reason:
            raise InvalidOperation("完成、取消、阻塞或重新打开要写原因", field="reason")
        op = {"op": "transition", "task_id": form.get("task_id"), "expected_version": version, "to": target, **base,
              "reason": reason or f"主人改为 {STATE_LABELS.get(target, target)}"}
        return [op], f"已改为{STATE_LABELS.get(target, target)}"
    if action == "focus":
        return [{"op": "set_focus", "slot": form.get("slot"), "task_id": form.get("task_id"), **base,
                 "reason": "主人设焦点"}], f"已设为 {form.get('slot')}"
    if action == "unfocus":
        return [{"op": "set_focus", "slot": form.get("slot"), "task_id": None, **base, "reason": "主人取消焦点"}], "已取消焦点"
    if action == "edit":
        tid = form.get("task_id")
        fields = {"title": form.get("title", ""), "next_action": form.get("next_action") or None,
                  "priority": int(form.get("priority") or 2),
                  "soft_due_at": _local_input_to_utc(form.get("soft_due", ""), tz)}
        if form.get("has_project") == "1" or "project_id" in form:
            fields["project_id"] = form.get("project_id") or None
        prev_official = (form.get("prev_official_due") or "").strip()
        new_official = (form.get("official_due") or "").strip()
        if prev_official and not new_official:
            fields["official_due_at"] = None
        op = {"op": "patch_task", "task_id": tid, "expected_version": version, "fields": fields, **base,
              "reason": reason or "主人在网页工作台修改"}
        if form.get("delay_kind"):
            op["delay_kind"] = form["delay_kind"]
        ops = [op]
        if new_official and new_official != prev_official:
            off_op = {"op": "set_official_due", "task_id": tid, "local": new_official, **base,
                      "reason": reason or "主人在网页工作台修改官方截止"}
            if form.get("delay_kind"):
                off_op["delay_kind"] = form["delay_kind"]
            ops.append(off_op)
        return ops, "已保存"
    if action == "human_reply":
        reply = (form.get("reply") or "").strip()
        status = form.get("status") or "completed"
        op = {"op": "respond_human_action", "human_action_id": form.get("human_action_id"), "status": status,
              "response": {"text": reply} if reply else {}, **base,
              "reason": reply or ("主人确认处理" if status == "completed" else "主人拒绝")}
        return [op], ("已确认处理" if status == "completed" else "已拒绝请求")
    if action == "progress":
        return [{"op": "append_progress", "task_id": form.get("task_id"), "expected_version": version,
                 "text": form.get("text", ""), **base, "reason": "主人记进展"}], "已记下"
    raise InvalidOperation("unknown action", field="action")
