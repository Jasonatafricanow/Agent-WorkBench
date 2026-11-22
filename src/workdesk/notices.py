"""Deterministic notices and the daily digest (no model call needed).

Quiet by default: one notice per subject, kind and due value. Kinds:
- official due today / within N days; soft due passed while the task is open;
- a commitment past due (who owes whom);
- a human request past its due date;
- approvals past expiry are closed and the blocked session is told;
- one daily digest at the owner's local digest time: routine (batch) requests,
  waiting items, overdue work, stalled tasks and overloaded days.

Settings live in the owner's `user_profiles.work_preferences_json`:
`{"digest_time": "09:00", "due_soon_days": 3, "quiet_hours": {...}}`.
"""
from __future__ import annotations

import json
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from .contracts import ActorContext
from .insights import schedule_load, stagnation
from .storage import write_transaction


def _local(ts: str, tz: ZoneInfo) -> datetime:
    return datetime.fromisoformat(ts[:-1] + "+00:00").astimezone(tz)


def _notice(conn, ws: str, key: str, recipient: str, text: str, now: str, task_id: str | None = None) -> bool:
    if conn.execute("SELECT 1 FROM notices_sent WHERE workspace_id=? AND dedupe_key=?", (ws, key)).fetchone():
        return False
    conn.execute("INSERT INTO notices_sent VALUES(?,?,?)", (ws, key, now))
    conn.execute(
        """INSERT OR IGNORE INTO delivery_outbox(id,workspace_id,task_id,recipient_principal_id,channel,payload_json,
             dedupe_key,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?)""",
        (f"dlv-out-{uuid.uuid4().hex}", ws, task_id, recipient, "telegram",
         json.dumps({"text": text, "kind": key.split(":")[0]}, ensure_ascii=False), f"notice:{ws}:{key}", now, now))
    return True


def sweep(database: str | Path, *, now: str) -> dict:
    sent: list[str] = []
    with write_transaction(database) as conn:
        workspaces = conn.execute(
            """SELECT w.id, w.owner_id, COALESCE(p.timezone, w.timezone) AS tz, p.work_preferences_json AS prefs
               FROM workspaces w LEFT JOIN user_profiles p ON p.workspace_id=w.id AND p.principal_id=w.owner_id""").fetchall()
        for ws in workspaces:
            prefs = json.loads(ws["prefs"] or "{}")
            tz = ZoneInfo(ws["tz"])
            today = _local(now, tz).date()
            soon_days = int(prefs.get("due_soon_days", 3))
            horizon = (datetime.fromisoformat(now[:-1]) + timedelta(days=soon_days)).strftime("%Y-%m-%dT%H:%M:%SZ")
            wid, owner = ws["id"], ws["owner_id"]
            for t in conn.execute(
                    """SELECT id,title,assignee_id,official_due_at,official_due_local,soft_due_at FROM tasks
                       WHERE workspace_id=? AND state NOT IN ('DONE','CANCELLED')""", (wid,)).fetchall():
                who = t["assignee_id"] or owner
                if t["official_due_at"] and t["official_due_at"] >= now:
                    due_day = _local(t["official_due_at"], tz).date()
                    label = t["official_due_local"] or _local(t["official_due_at"], tz).strftime("%m-%d %H:%M")
                    if due_day == today:
                        if _notice(conn, wid, f"due_today:{t['id']}:{t['official_due_at']}", who,
                                   f"今天官方截止：{t['title']}（{label}）", now, t["id"]):
                            sent.append("due_today")
                    elif t["official_due_at"] <= horizon:
                        if _notice(conn, wid, f"due_soon:{t['id']}:{t['official_due_at']}", who,
                                   f"{(due_day - today).days} 天后官方截止：{t['title']}（{label}）", now, t["id"]):
                            sent.append("due_soon")
                if t["soft_due_at"] and t["soft_due_at"] < now:
                    if _notice(conn, wid, f"soft_overdue:{t['id']}:{t['soft_due_at']}", who,
                               f"内部节点已过：{t['title']}（soft due {_local(t['soft_due_at'], tz).strftime('%m-%d')}）",
                               now, t["id"]):
                        sent.append("soft_overdue")
            for c in conn.execute(
                    """SELECT c.*, pr.display_name AS promisor_name FROM commitments c
                       JOIN principals pr ON pr.id=c.promisor_id
                       WHERE c.workspace_id=? AND c.status='open' AND c.due_at IS NOT NULL AND c.due_at<?""",
                    (wid, now)).fetchall():
                text = (f"你答应的事已过期：{c['promise_text']}" if c["promisor_id"] == owner
                        else f"{c['promisor_name']} 答应的事已过期，要不要催：{c['promise_text']}")
                if _notice(conn, wid, f"commitment_overdue:{c['id']}", owner, text, now, c["task_id"]):
                    sent.append("commitment_overdue")
            for h in conn.execute(
                    """SELECT * FROM human_action_requests WHERE workspace_id=? AND status IN ('pending','in_progress')
                       AND due_at IS NOT NULL AND due_at<?""", (wid, now)).fetchall():
                if _notice(conn, wid, f"human_overdue:{h['id']}", h["assignee_principal_id"],
                           f"待你处理的事已过期限：{h['title']}", now, h["task_id"]):
                    sent.append("human_overdue")
            for a in conn.execute(
                    """SELECT * FROM approval_requests WHERE workspace_id=? AND status='pending'
                       AND expires_at IS NOT NULL AND expires_at<=?""", (wid, now)).fetchall():
                conn.execute("UPDATE approval_requests SET status='expired', decided_at=? WHERE id=?", (now, a["id"]))
                conn.execute(
                    """INSERT OR IGNORE INTO wake_events(id,workspace_id,task_id,origin,origin_event_id,kind,source_ref,
                         deferred_ref,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?)""",
                    (f"wake-{uuid.uuid4().hex}", wid, a["task_id"], "workbench.approval", a["id"], "approval_decided",
                     f"workdesk://{wid}/approvals/{a['id']}", a["host_deferred_ref"], now, now))
                sent.append("approval_expired")
            sent.extend(_digest(conn, database, wid, owner, prefs, tz, now))
    return {"sent": sent}


def _digest(conn, database, ws: str, owner: str, prefs: dict, tz: ZoneInfo, now: str) -> list[str]:
    local_now = _local(now, tz)
    digest_time = prefs.get("digest_time", "09:00")
    if local_now.strftime("%H:%M") < digest_time:
        return []
    key = f"digest:{local_now.date().isoformat()}"
    if conn.execute("SELECT 1 FROM notices_sent WHERE workspace_id=? AND dedupe_key=?", (ws, key)).fetchone():
        return []
    batch = conn.execute(
        """SELECT title FROM human_action_requests WHERE workspace_id=? AND assignee_principal_id=?
           AND status IN ('pending','in_progress') AND urgency='batch' ORDER BY created_at LIMIT 10""", (ws, owner)).fetchall()
    waiting = conn.execute("SELECT COUNT(*) FROM tasks WHERE workspace_id=? AND state='WAITING'", (ws,)).fetchone()[0]
    inbox = conn.execute("SELECT COUNT(*) FROM tasks WHERE workspace_id=? AND state='INBOX'", (ws,)).fetchone()[0]
    overdue = conn.execute(
        """SELECT COUNT(*) FROM tasks WHERE workspace_id=? AND state NOT IN ('DONE','CANCELLED')
           AND ((soft_due_at IS NOT NULL AND soft_due_at<?) OR (official_due_at IS NOT NULL AND official_due_at<?))""",
        (ws, now, now)).fetchone()[0]
    owner_actor = ActorContext(ws, owner, "owner", "cli")
    # stagnation/schedule_load open their own read connections; safe under WAL while we hold the write lock
    stuck = stagnation(database, actor=owner_actor, now=now) if database != ":memory:" else []
    load = schedule_load(database, actor=owner_actor, now=now, days=7) if database != ":memory:" else {"overloaded_days": []}
    lines = []
    if batch:
        lines.append("待你决定：\n" + "\n".join(f"· {r['title']}" for r in batch))
    if inbox:
        lines.append(f"新进来未受理：{inbox} 件")
    if waiting:
        lines.append(f"在等别人：{waiting} 件")
    if overdue:
        lines.append(f"已过期限：{overdue} 件")
    if stuck:
        lines.append("停滞或反复延期：" + "、".join(s["title"] for s in stuck[:5]))
    if load["overloaded_days"]:
        lines.append("到期扎堆的日子：" + "、".join(load["overloaded_days"]))
    conn.execute("INSERT INTO notices_sent VALUES(?,?,?)", (ws, key, now))
    if not lines:
        return []
    _notice(conn, ws, f"digest-msg:{local_now.date().isoformat()}", owner,
            f"工作台日报 {local_now.strftime('%m-%d')}\n\n" + "\n\n".join(lines), now)
    return ["digest"]
