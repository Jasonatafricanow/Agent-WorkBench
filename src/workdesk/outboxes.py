"""Delivery outbox (messages to people) and memory pointer queue.

Delivery: a send whose result is unclear is `unknown`, never `sent`, and is not
retried automatically. Before a message is handed to the carrier it is
re-checked: a stale follow-up (task closed, dependency resolved) is cancelled,
and quiet hours postpone it without dropping it.

Memory: per the owner's 11-17 decision the handling Agent writes MR-Mem itself
at wrap-up; this queue only holds pointers for retry when MR-Mem was down or
for later offline curation.
"""
from __future__ import annotations

import json
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable
from zoneinfo import ZoneInfo

from .errors import InvalidOperation
from .storage import utc_now, write_transaction


def _plus(now: str, seconds: int) -> str:
    return (datetime.fromisoformat(now[:-1]) + timedelta(seconds=seconds)).strftime("%Y-%m-%dT%H:%M:%SZ")


def quiet_until(now: str, prefs: dict, tz_name: str) -> str | None:
    """If `now` is inside the recipient's quiet hours, return the UTC time they end."""
    quiet = prefs.get("quiet_hours") or {}
    start, end = quiet.get("start"), quiet.get("end")
    if not start or not end:
        return None
    zone = ZoneInfo(tz_name)
    local = datetime.fromisoformat(now[:-1] + "+00:00").astimezone(zone)
    s = local.replace(hour=int(start[:2]), minute=int(start[3:]), second=0, microsecond=0)
    e = local.replace(hour=int(end[:2]), minute=int(end[3:]), second=0, microsecond=0)
    if s <= e:
        inside, ends = s <= local < e, e
    else:  # spans midnight, e.g. 23:00-08:00
        inside = local >= s or local < e
        ends = e if local < e else e + timedelta(days=1)
    if not inside:
        return None
    return ends.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


class DeliveryOutbox:
    def __init__(self, database: str | Path, *, clock: Callable[[], str] = utc_now):
        self._db, self._clock = str(database), clock

    def enqueue(self, *, workspace_id: str, recipient_id: str, channel: str, payload: dict,
                dedupe_key: str, task_id: str | None = None, reminder_id: str | None = None) -> dict:
        now = self._clock()
        if not dedupe_key:
            raise InvalidOperation("dedupe_key is required")
        with write_transaction(self._db) as conn:
            existing = conn.execute("SELECT id,status FROM delivery_outbox WHERE dedupe_key=?", (dedupe_key,)).fetchone()
            if existing:
                return {"delivery_id": existing["id"], "status": existing["status"], "duplicate": True}
            delivery_id = f"dlv-out-{uuid.uuid4().hex}"
            conn.execute(
                """INSERT INTO delivery_outbox(id,workspace_id,task_id,reminder_id,recipient_principal_id,channel,
                     payload_json,dedupe_key,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?)""",
                (delivery_id, workspace_id, task_id, reminder_id, recipient_id, channel,
                 json.dumps(payload, ensure_ascii=False), dedupe_key, now, now))
            return {"delivery_id": delivery_id, "status": "pending"}

    def claim(self, *, workspace_id: str, limit: int = 10, lease_seconds: int = 120) -> list[dict]:
        now = self._clock()
        out = []
        with write_transaction(self._db) as conn:
            conn.execute("""UPDATE delivery_outbox SET status='unknown', lease_until=NULL, updated_at=?
                            WHERE workspace_id=? AND status='leased' AND lease_until<=?""", (now, workspace_id, now))
            rows = conn.execute(
                """SELECT o.*, t.state AS task_state, r.status AS reminder_status, d.status AS dep_status,
                          p.work_preferences_json AS prefs, COALESCE(p.timezone, w.timezone) AS tz
                   FROM delivery_outbox o JOIN workspaces w ON w.id=o.workspace_id
                   LEFT JOIN tasks t ON t.workspace_id=o.workspace_id AND t.id=o.task_id
                   LEFT JOIN reminders r ON r.workspace_id=o.workspace_id AND r.id=o.reminder_id
                   LEFT JOIN dependencies d ON d.workspace_id=o.workspace_id AND d.id=r.dependency_id
                   LEFT JOIN user_profiles p ON p.workspace_id=o.workspace_id AND p.principal_id=o.recipient_principal_id
                   WHERE o.workspace_id=? AND o.status='pending' AND (o.not_before IS NULL OR o.not_before<=?)
                   ORDER BY o.created_at LIMIT ?""", (workspace_id, now, limit)).fetchall()
            for row in rows:
                # Only follow-ups go stale; a report such as "task X is done" must still reach the person.
                stale = row["reminder_id"] is not None and (
                    row["task_state"] in ("DONE", "CANCELLED") or row["reminder_status"] == "cancelled"
                    or row["dep_status"] in ("resolved", "cancelled"))
                if stale:
                    conn.execute("UPDATE delivery_outbox SET status='cancelled', updated_at=? WHERE id=?", (now, row["id"]))
                    continue
                later = quiet_until(now, json.loads(row["prefs"] or "{}"), row["tz"])
                if later:
                    conn.execute("UPDATE delivery_outbox SET not_before=?, updated_at=? WHERE id=?", (later, now, row["id"]))
                    continue
                conn.execute("""UPDATE delivery_outbox SET status='leased', attempts=attempts+1, lease_until=?,
                                updated_at=? WHERE id=?""", (_plus(now, lease_seconds), now, row["id"]))
                out.append({"delivery_id": row["id"], "recipient_id": row["recipient_principal_id"],
                            "channel": row["channel"], "payload": json.loads(row["payload_json"]),
                            "task_id": row["task_id"]})
        return out

    def settle(self, *, workspace_id: str, delivery_id: str, status: str,
               provider_message_id: str | None = None) -> None:
        # "simulated": a local CLI run printed it; nothing was delivered, so it can never serve as a receipt.
        if status not in ("sent", "failed", "unknown", "simulated"):
            raise InvalidOperation("status must be sent, failed, unknown or simulated")
        if status == "simulated":
            status, provider_message_id = "cancelled", "cli-simulated"
        if status == "sent" and not provider_message_id:
            raise InvalidOperation("sent needs the provider message id")
        with write_transaction(self._db) as conn:
            conn.execute(
                """UPDATE delivery_outbox SET status=?, provider_message_id=?, lease_until=NULL, updated_at=?
                   WHERE workspace_id=? AND id=? AND status IN ('leased','unknown')""",
                (status, provider_message_id, self._clock(), workspace_id, delivery_id))

    def report(self, *, workspace_id: str) -> dict:
        with write_transaction(self._db) as conn:
            counts = {r["status"]: r["n"] for r in conn.execute(
                "SELECT status, COUNT(*) AS n FROM delivery_outbox WHERE workspace_id=? GROUP BY status", (workspace_id,))}
            unknown = [dict(r) for r in conn.execute(
                """SELECT id,recipient_principal_id,channel,task_id,updated_at FROM delivery_outbox
                   WHERE workspace_id=? AND status='unknown' ORDER BY updated_at""", (workspace_id,))]
        return {"counts": counts, "needs_human_check": unknown}


class MemoryPointerQueue:
    def __init__(self, database: str | Path, *, clock: Callable[[], str] = utc_now):
        self._db, self._clock = str(database), clock

    def enqueue(self, *, workspace_id: str, task_event_id: str, scope: str, source_ref: str) -> dict:
        now = self._clock()
        with write_transaction(self._db) as conn:
            existing = conn.execute("SELECT id FROM memory_outbox WHERE task_event_id=?", (task_event_id,)).fetchone()
            if existing:
                return {"pointer_id": existing["id"], "duplicate": True}
            pointer_id = f"mem-{uuid.uuid4().hex}"
            conn.execute(
                """INSERT INTO memory_outbox(id,task_event_id,workspace_id,scope,source_ref,created_at,updated_at)
                   VALUES(?,?,?,?,?,?,?)""", (pointer_id, task_event_id, workspace_id, scope, source_ref, now, now))
            return {"pointer_id": pointer_id}

    def claim(self, *, workspace_id: str, limit: int = 20, lease_seconds: int = 300) -> list[dict[str, Any]]:
        now = self._clock()
        with write_transaction(self._db) as conn:
            conn.execute("""UPDATE memory_outbox SET status='pending', lease_until=NULL WHERE workspace_id=?
                            AND status='leased' AND lease_until<=?""", (workspace_id, now))
            rows = conn.execute(
                """SELECT m.id, m.task_event_id, m.scope, m.source_ref, e.task_id, e.kind, e.reason, e.after_json,
                          e.occurred_at FROM memory_outbox m JOIN task_events e
                     ON e.workspace_id=m.workspace_id AND e.id=m.task_event_id
                   WHERE m.workspace_id=? AND m.status='pending' ORDER BY m.created_at LIMIT ?""",
                (workspace_id, limit)).fetchall()
            for row in rows:
                conn.execute("""UPDATE memory_outbox SET status='leased', attempts=attempts+1, lease_until=?,
                                updated_at=? WHERE id=?""", (_plus(now, lease_seconds), now, row["id"]))
            return [dict(r) for r in rows]

    def settle(self, *, workspace_id: str, pointer_id: str, status: str) -> None:
        if status not in ("sent", "failed", "dead"):
            raise InvalidOperation("status must be sent, failed or dead")
        with write_transaction(self._db) as conn:
            conn.execute("""UPDATE memory_outbox SET status=?, lease_until=NULL, updated_at=?
                            WHERE workspace_id=? AND id=?""",
                         ("pending" if status == "failed" else status, self._clock(), workspace_id, pointer_id))
