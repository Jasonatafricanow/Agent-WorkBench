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
.owner input[type=text],.owner textarea{flex:1 1 220px;min-width:0}.owner button{cursor:pointer}.owner button.primary{background:var(--accent);color:#fff;border-color:var(--accent)}
.owner button.danger{color:var(--warn);border-color:var(--warn)}.owner .row{display:flex;flex-wrap:wrap;gap:6px}
.notice{padding:8px 12px;border-radius:8px;margin-bottom:12px;border:1px solid var(--line)}.notice.bad{color:var(--warn);border-color:var(--warn)}.notice.good{color:var(--ok);border-color:var(--ok)}
"""


def new_task_form(csrf: str, back: str = "/") -> str:
    priorities = "".join(f"<option value={p}{' selected' if p == 2 else ''}>优先级 {p}</option>" for p in range(5))
    return (
        "<section class=owner><h2>新建任务（我自己的）</h2>"
        f"<form method=post action=/owner/ops>{_hidden(csrf, back, action='create')}"
        "<input type=text name=title placeholder='要做什么' required maxlength=200>"
        "<input type=text name=next_action placeholder='下一步（可空）' maxlength=400>"
        "<label class=meta>期望完成 <input type=datetime-local name=soft_due></label>"
        f"<select name=priority>{priorities}</select>"
        "<select name=state><option value=READY>可做</option><option value=DOING>马上做</option><option value=INBOX>先放收件箱</option></select>"
        "<select name=focus><option value=''>不设焦点</option><option value=NOW>设为 NOW</option>"
        "<option value=NEXT1>设为 NEXT1</option><option value=NEXT2>设为 NEXT2</option></select>"
        "<button class=primary>新建</button></form></section>")


def _post(csrf: str, back: str, inner: str, **fields) -> str:
    return f"<form method=post action=/owner/ops>{_hidden(csrf, back, **fields)}{inner}</form>"


def task_panel(csrf: str, task: dict, focus_slot: str | None, tz: ZoneInfo) -> str:
    """Moves allowed from the task's state (WAITING is left out: it needs a dependency, which the work bot
    or Xiyue records), focus slots, editing and a progress note. Closing, reopening and blocking ask for a reason."""
    tid, version, state = task["id"], task["version"], task["state"]
    back = f"/task/{tid}"
    common = {"task_id": tid, "version": version}
    moves = sorted(TRANSITIONS.get(state, ()) - {"WAITING"}, key=list(STATE_LABELS).index)
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
    edit = _post(csrf, back,
                 f"<input type=text name=title value='{e(task['title'])}' required maxlength=200>"
                 f"<input type=text name=next_action value='{e(task.get('next_action'))}' placeholder='下一步' maxlength=400>"
                 f"<label class=meta>期望完成 <input type=datetime-local name=soft_due value='{_utc_to_input(task.get('soft_due_at'), tz)}'></label>"
                 f"<select name=priority>{priorities}</select>"
                 f"<select name=delay_kind><option value=''>（往后推时选原因）</option>{delay}</select>"
                 "<input type=text name=reason placeholder='往后推的原因' maxlength=400>"
                 "<button>保存修改</button>", action="edit", **common)
    progress = _post(csrf, back, "<input type=text name=text placeholder='记一笔进展' required maxlength=1000>"
                                 "<button>记下</button>", action="progress", **common)
    return (f"<section class=owner><h2>我来处理</h2><div class=row>{buttons}</div>{reasoned}"
            f"<div class=meta>焦点</div><div class=row>{focus}</div>{edit}{progress}</section>")


def notice(query: dict) -> str:
    if query.get("err"):
        return f"<div class='notice bad'>没有执行：{e(query['err'])}</div>"
    if query.get("ok"):
        return f"<div class='notice good'>{e(query['ok'])}</div>"
    return ""


def operations(form: dict, tz: ZoneInfo) -> tuple[list[dict], str]:
    """A submitted form -> (TaskService operations, message on success)."""
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
        due = _local_input_to_utc(form.get("soft_due", ""), tz)
        if due:
            op["soft_due_at"] = due
        ops = [op]
        if form.get("focus") in ("NOW", "NEXT1", "NEXT2"):
            ops.append({"op": "set_focus", "slot": form["focus"], "task_id": "$new", **base, "reason": "主人设焦点"})
        return ops, "已新建"
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
        fields = {"title": form.get("title", ""), "next_action": form.get("next_action") or None,
                  "priority": int(form.get("priority") or 2),
                  "soft_due_at": _local_input_to_utc(form.get("soft_due", ""), tz)}
        op = {"op": "patch_task", "task_id": form.get("task_id"), "expected_version": version, "fields": fields, **base,
              "reason": reason or "主人在网页工作台修改"}
        if form.get("delay_kind"):
            op["delay_kind"] = form["delay_kind"]
        return [op], "已保存"
    if action == "progress":
        return [{"op": "append_progress", "task_id": form.get("task_id"), "expected_version": version,
                 "text": form.get("text", ""), **base, "reason": "主人记进展"}], "已记下"
    raise InvalidOperation("unknown action", field="action")
