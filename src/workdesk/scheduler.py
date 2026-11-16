"""Business timers: due reminders, recurring schedules and external watches.

Timers never call a model. Each firing re-checks its condition and, in the same
transaction, records its outcome and enqueues exactly one wake event for the
Host. A stale reminder (task closed, dependency resolved) is cancelled instead.
"""
from __future__ import annotations

import calendar
import json
import uuid
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from .errors import InvalidOperation
from .storage import write_transaction

SYSTEM_PRINCIPAL = "workbench-system"
OPEN_STATES = ("INBOX", "READY", "DOING", "WAITING", "BLOCKED")


def _new(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4().hex}"


def local_to_utc(local: str, tz_name: str) -> str:
    naive = datetime.fromisoformat(local)
    return naive.replace(tzinfo=ZoneInfo(tz_name)).astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def advance(local: str, frequency: str, interval_n: int, anchor_day: int | None = None) -> str:
    """Next local occurrence. Monthly targets `anchor_day` (default: the current day),
    clamped to the month length, so a 31st schedule goes 01-31, 02-28, 03-31."""
    current = datetime.fromisoformat(local)
    if frequency == "daily":
        nxt = current + timedelta(days=interval_n)
    elif frequency == "weekly":
        nxt = current + timedelta(weeks=interval_n)
    elif frequency == "monthly":
        month_index = current.month - 1 + interval_n
        year, month = current.year + month_index // 12, month_index % 12 + 1
        day = min(anchor_day or current.day, calendar.monthrange(year, month)[1])
        nxt = current.replace(year=year, month=month, day=day)
    else:
        raise InvalidOperation("frequency must be daily, weekly or monthly")
    return nxt.isoformat(timespec="minutes")


def validate_schedule(frequency: str, interval_n: int, local_time: str, start_local_date: str,
                      tz_name: str) -> tuple[str, str]:
    if frequency not in ("daily", "weekly", "monthly"):
        raise InvalidOperation("frequency must be daily, weekly or monthly", field="frequency")
    if type(interval_n) is not int or interval_n < 1:
        raise InvalidOperation("interval must be a positive integer", field="interval")
    try:
        ZoneInfo(tz_name)
        first_local = datetime.combine(date.fromisoformat(start_local_date),
                                       datetime.strptime(local_time, "%H:%M").time())
    except Exception as exc:  # noqa: BLE001 - surface any parse problem as invalid input
        raise InvalidOperation("bad schedule start, local_time (HH:MM) or timezone") from exc
    local = first_local.isoformat(timespec="minutes")
    return local, local_to_utc(local, tz_name)


def _ensure_system(conn, now: str) -> None:
    conn.execute("INSERT OR IGNORE INTO principals(id,display_name,kind,created_at) VALUES(?,?,?,?)",
                 (SYSTEM_PRINCIPAL, "WorkBench system", "service", now))


def _wake(conn, ws: str, task_id: str | None, origin: str, origin_event_id: str, kind: str,
          source_ref: str, now: str) -> str:
    if not source_ref or not str(source_ref).strip():
        raise InvalidOperation("a wake event needs a source_ref")
    existing = conn.execute(
        "SELECT id FROM wake_events WHERE workspace_id=? AND origin=? AND origin_event_id=?",
        (ws, origin, origin_event_id)).fetchone()
    if existing:
        return existing[0]
    wake_id = _new("wake")
    conn.execute(
        """INSERT INTO wake_events(id,workspace_id,task_id,origin,origin_event_id,kind,source_ref,created_at,updated_at)
           VALUES(?,?,?,?,?,?,?,?,?)""",
        (wake_id, ws, task_id, origin, origin_event_id, kind, source_ref, now, now))
    return wake_id


def _task_event(conn, ws: str, task_id: str, actor: str, kind: str, after: dict, reason: str,
                evidence: str, source_ref: str | None, now: str) -> None:
    conn.execute(
        """INSERT INTO task_events(id,workspace_id,task_id,actor_id,kind,after_json,reason,evidence_type,
             source_ref,occurred_at,recorded_at) VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
        (_new("evt"), ws, task_id, actor, kind, json.dumps(after, ensure_ascii=False, sort_keys=True),
         reason, evidence, source_ref, now, now))


def _bump(conn, ws: str, task_id: str, now: str) -> None:
    conn.execute("UPDATE tasks SET version=version+1, updated_at=? WHERE workspace_id=? AND id=?",
                 (now, ws, task_id))


def fire_due(database: str | Path, *, now: str, batch: int = 100) -> dict:
    """Fire every due reminder and schedule occurrence once; safe to run concurrently."""
    fired, cancelled, scheduled = [], [], []
    with write_transaction(database) as conn:
        _ensure_system(conn, now)
        reminders = conn.execute(
            """SELECT r.*, t.state AS task_state, d.status AS dep_status FROM reminders r
               JOIN tasks t ON t.workspace_id=r.workspace_id AND t.id=r.task_id
               LEFT JOIN dependencies d ON d.workspace_id=r.workspace_id AND d.id=r.dependency_id
               WHERE r.status='scheduled' AND r.due_at<=? ORDER BY r.due_at, r.id LIMIT ?""",
            (now, batch)).fetchall()
        for r in reminders:
            still = (r["task_state"] in OPEN_STATES) and (
                r["condition"] == "always" or r["condition"] == "if_open" or (
                    r["condition"] == "if_still_waiting" and r["task_state"] in ("WAITING", "BLOCKED")
                    and (r["dependency_id"] is None or r["dep_status"] == "open")))
            if r["condition"] == "always":
                still = True
            if not still:
                conn.execute("UPDATE reminders SET status='cancelled', updated_at=? WHERE id=?", (now, r["id"]))
                cancelled.append(r["id"])
                continue
            wake_id = _wake(conn, r["workspace_id"], r["task_id"], "workbench.timer", r["id"], "timer_fired",
                            f"workdesk://{r['workspace_id']}/reminders/{r['id']}", now)
            conn.execute("UPDATE reminders SET status='sent', attempts=attempts+1, updated_at=? WHERE id=?",
                         (now, r["id"]))
            fired.append({"reminder_id": r["id"], "wake_event_id": wake_id})
            if r["repeat_every_days"] and (r["repeats_left"] or 0) > 0:
                # "every Wednesday until they answer": the next one is a new row in the same series,
                # so cancelling the series (dependency resolved, task closed) stops all of it.
                nxt = (datetime.fromisoformat(r["due_at"][:-1]) + timedelta(days=r["repeat_every_days"])
                       ).strftime("%Y-%m-%dT%H:%M:%SZ")
                series = r["series_id"] or r["id"]
                conn.execute(
                    """INSERT OR IGNORE INTO reminders(id,workspace_id,task_id,dependency_id,due_at,timezone,kind,condition,
                         recipient_principal_id,channel,dedupe_key,created_at,updated_at,repeat_every_days,repeats_left,series_id)
                       VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (_new("rem"), r["workspace_id"], r["task_id"], r["dependency_id"], nxt, r["timezone"], r["kind"],
                     r["condition"], r["recipient_principal_id"], r["channel"], f"{series}:{nxt}", now, now,
                     r["repeat_every_days"], r["repeats_left"] - 1, series))

        rules = conn.execute(
            "SELECT * FROM schedule_rules WHERE active=1 AND next_fire_at<=? ORDER BY next_fire_at LIMIT ?",
            (now, batch)).fetchall()
        for rule in rules:
            scheduled.extend(_fire_rule(conn, rule, now))
    return {"reminders_fired": fired, "reminders_cancelled": cancelled, "schedule_occurrences": scheduled}


def _fire_rule(conn, rule, now: str) -> list[dict]:
    out = []
    local, at, count = rule["next_fire_local"], rule["next_fire_at"], rule["occurrences"]
    template = json.loads(rule["template_json"])
    ws = rule["workspace_id"]
    owner = conn.execute("SELECT owner_id FROM workspaces WHERE id=?", (ws,)).fetchone()[0]
    active = 1
    anchor = rule["anchor_day"]
    # After downtime only the most recent missed occurrence becomes work; older ones are skipped.
    skipped = 0
    for _ in range(1000):
        nxt_local = advance(local, rule["frequency"], rule["interval_n"], anchor)
        if local_to_utc(nxt_local, rule["timezone"]) > now or at > now:
            break
        local, at, skipped = nxt_local, local_to_utc(nxt_local, rule["timezone"]), skipped + 1
    for _ in range(1):
        if at > now:
            break
        if conn.execute("SELECT 1 FROM schedule_occurrences WHERE rule_id=? AND occurrence_local=?",
                        (rule["id"], local)).fetchone() is None:
            task_id = _new("task")
            title = f"{template.get('title') or rule['title']}（{local[:10]}）"
            conn.execute(
                """INSERT INTO tasks(id,workspace_id,title,goal,next_action,state,priority,assignee_id,
                     soft_due_at,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
                (task_id, ws, title, template.get("goal", ""), template.get("next_action", ""),
                 "READY", int(template.get("priority", 2)), owner,
                 at if template.get("due_at_occurrence") else None, now, now))
            _task_event(conn, ws, task_id, rule["created_by_principal_id"], "created",
                        {"schedule_rule_id": rule["id"], "occurrence_local": local},
                        "recurring schedule occurrence", "system", f"workdesk://{ws}/schedules/{rule['id']}", now)
            wake_id = _wake(conn, ws, task_id, "workbench.schedule", f"{rule['id']}:{local}", "timer_fired",
                            f"workdesk://{ws}/schedules/{rule['id']}/{local}", now)
            conn.execute("INSERT INTO schedule_occurrences VALUES(?,?,?,?,?)",
                         (rule["id"], local, task_id, wake_id, now))
            out.append({"rule_id": rule["id"], "occurrence_local": local, "task_id": task_id,
                        "wake_event_id": wake_id, "skipped_before": skipped})
            count += 1
        local = advance(local, rule["frequency"], rule["interval_n"], anchor)
        at = local_to_utc(local, rule["timezone"])
        if (rule["max_occurrences"] is not None and count >= rule["max_occurrences"]) or (
                rule["until_at"] is not None and at > rule["until_at"]):
            active = 0
            break
    conn.execute(
        """UPDATE schedule_rules SET next_fire_local=?, next_fire_at=?, occurrences=?, active=?, updated_at=?
           WHERE id=?""", (local, at, count, active, now, rule["id"]))
    return out


def observe_watch(database: str | Path, *, workspace_id: str, watch_id: str, observed_value: str,
                  source_ref: str, now: str) -> dict:
    """Called by a trusted adapter (GitHub poller, webhook). A match wakes the Host once
    and, if the watch guards a dependency, resolves it with verified-tool evidence."""
    if not source_ref or not str(source_ref).strip():
        raise InvalidOperation("source_ref is required")
    with write_transaction(database) as conn:
        _ensure_system(conn, now)
        watch = conn.execute("SELECT * FROM external_watches WHERE workspace_id=? AND id=?",
                             (workspace_id, watch_id)).fetchone()
        if watch is None:
            raise InvalidOperation("unknown watch")
        if watch["status"] != "watching":
            return {"status": watch["status"], "changed": False}
        conn.execute("UPDATE external_watches SET last_observed_value=?, updated_at=? WHERE id=?",
                     (observed_value, now, watch_id))
        if observed_value != watch["expected_value"]:
            return {"status": "watching", "matched": False}
        conn.execute("UPDATE external_watches SET status='matched', updated_at=? WHERE id=?", (now, watch_id))
        result = {"status": "matched", "matched": True}
        if watch["dependency_id"]:
            dep = conn.execute("SELECT status FROM dependencies WHERE id=?", (watch["dependency_id"],)).fetchone()
            if dep and dep["status"] == "open":
                conn.execute("UPDATE dependencies SET status='resolved', resolved_source_ref=?, resolved_at=? WHERE id=?",
                             (source_ref, now, watch["dependency_id"]))
                conn.execute(
                    """UPDATE reminders SET status='cancelled', updated_at=? WHERE workspace_id=? AND status='scheduled'
                       AND dependency_id=?""", (now, workspace_id, watch["dependency_id"]))
                result["dependency_resolved"] = watch["dependency_id"]
        if watch["task_id"]:
            _task_event(conn, workspace_id, watch["task_id"], SYSTEM_PRINCIPAL, "external_change",
                        {"watch_id": watch_id, "observed": observed_value,
                         "dependency_resolved": result.get("dependency_resolved")},
                        f"{watch['source_kind']} reached {observed_value}", "verified_tool", source_ref, now)
            _bump(conn, workspace_id, watch["task_id"], now)
        result["wake_event_id"] = _wake(conn, workspace_id, watch["task_id"], "workbench.watch",
                                        f"{watch_id}:{observed_value}", "external_change", source_ref, now)
        return result
