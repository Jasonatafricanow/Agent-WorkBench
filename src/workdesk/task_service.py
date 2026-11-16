"""Transactional SQLite implementation of the WorkBench TaskService.

One `apply` batch is all-or-nothing: task snapshots, append-only task events,
responsibility changes, human action requests, wake events and the command
receipt commit in a single `BEGIN IMMEDIATE` transaction.

Authority rules enforced here (not in the model or the tool facade):
- the caller's role is re-read from `memberships` on every call; a stale or
  forged ActorContext fails closed;
- a `requester` (e.g. an external collaborator on Telegram) only sees tasks they
  requested, gets a reduced view, and can only file requests/notes; a task that
  is not visible is reported exactly like a missing one;
- DONE / CANCELLED / reopen and changing an existing official due need strong
  evidence (`explicit_user` or `verified_tool`) plus a source_ref and reason;
- moving a due date later needs a delay kind and reason;
- delegation can only narrow an owner-approved, versioned work-class profile,
  and only the human owner may change responsibility or autonomy.

The Host owns Runs, tool permissions and side effects; nothing here calls a model,
sends a message or grants a tool.
"""
from __future__ import annotations

import hashlib
import json
import sqlite3
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from .contracts import ActorContext
from .delegation import AutonomyLevel, DelegationEnvelope, route_action
from .errors import (
    EvidenceRequired, Forbidden, IdempotencyConflict, InvalidOperation,
    NotFound, VersionConflict, WorkbenchError,
)
from .storage import read_snapshot, utc_now, write_transaction

EVIDENCE = frozenset({"explicit_user", "verified_tool", "reported_third_party", "inferred", "system"})
STRONG_EVIDENCE = frozenset({"explicit_user", "verified_tool"})
READ_ALL_ROLES = frozenset({"owner", "editor", "viewer", "scheduler"})
WRITE_ROLES = frozenset({"owner", "editor"})
STATES = ("INBOX", "READY", "DOING", "WAITING", "BLOCKED", "DONE", "CANCELLED")
OPEN_STATES = frozenset(STATES[:5])
TRANSITIONS = {
    "INBOX": {"READY", "DOING", "CANCELLED"},
    "READY": {"DOING", "WAITING", "BLOCKED", "DONE", "CANCELLED"},
    "DOING": {"READY", "WAITING", "BLOCKED", "DONE", "CANCELLED"},
    "WAITING": {"READY", "DOING", "BLOCKED", "DONE", "CANCELLED"},
    "BLOCKED": {"READY", "DOING", "WAITING", "DONE", "CANCELLED"},
    "DONE": {"READY"},
    "CANCELLED": {"READY"},
}
DELAY_KINDS = frozenset({
    "external_wait", "reprioritized", "underestimated", "blocked",
    "unavailable", "scope_changed", "other",
})
LEVELS = tuple(level.name.lower() for level in AutonomyLevel)
PATCHABLE = ("title", "goal", "next_action", "priority", "soft_due_at",
             "planned_start_at", "official_due_at", "project_id", "assignee_id")
DUE_FIELDS = ("soft_due_at", "official_due_at")
REQUESTER_OPS = frozenset({"create_task", "append_progress", "request_human_action", "link_inbox"})
# Ops that only append related records; they bump the task version but do not
# overwrite task fields, so the caller need not hold the latest version.
NO_VERSION_CHECK = frozenset({"set_focus", "link_inbox", "respond_human_action"})
MAX_TEXT = 4000


def _utc(value: Any, field: str) -> str:
    if not isinstance(value, str) or not value.endswith("Z"):
        raise InvalidOperation(f"{field} must be canonical UTC ending in Z", field=field)
    try:
        parsed = datetime.fromisoformat(value[:-1] + "+00:00")
    except ValueError as exc:
        raise InvalidOperation(f"{field} is not a valid UTC timestamp", field=field) from exc
    return parsed.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _opt_utc(value: Any, field: str) -> str | None:
    return None if value is None else _utc(value, field)


def _local_to_utc(local_at: Any, tz_name: Any) -> str:
    if not isinstance(local_at, str) or not isinstance(tz_name, str):
        raise InvalidOperation("local_at and timezone are required")
    try:
        zone = ZoneInfo(tz_name)
    except (ZoneInfoNotFoundError, ValueError) as exc:
        raise InvalidOperation("unknown IANA timezone", field="timezone") from exc
    try:
        naive = datetime.fromisoformat(local_at)
    except ValueError as exc:
        raise InvalidOperation("local_at must be YYYY-MM-DDTHH:MM[:SS]", field="local_at") from exc
    if naive.tzinfo is not None:
        raise InvalidOperation("local_at must not carry an offset; pass timezone", field="local_at")
    return naive.replace(tzinfo=zone).astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _plus_days(now: str, days: int) -> str:
    from datetime import timedelta
    return (datetime.fromisoformat(now[:-1]) + timedelta(days=days)).strftime("%Y-%m-%dT%H:%M:%SZ")


def _text(value: Any, field: str, *, required: bool = False) -> str:
    if value is None and not required:
        return ""
    if not isinstance(value, str) or (required and not value.strip()):
        raise InvalidOperation(f"{field} is required", field=field)
    if len(value) > MAX_TEXT:
        raise InvalidOperation(f"{field} too long", field=field)
    return value.strip()


def _str_list(value: Any, field: str) -> list[str]:
    if value is None:
        return []
    if not isinstance(value, list) or any(not isinstance(v, str) or not v.strip() for v in value):
        raise InvalidOperation(f"{field} must be a list of nonempty strings", field=field)
    return sorted(set(value))


def _obj(value: Any, field: str) -> dict:
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise InvalidOperation(f"{field} must be an object", field=field)
    return value


def _dumps(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _payload_hash(actor: ActorContext, operations: list[dict]) -> str:
    body = {"workspace": actor.workspace_id, "principal": actor.principal_id, "ops": operations}
    return hashlib.sha256(_dumps(body).encode("utf-8")).hexdigest()


class SqliteTaskService:
    """Implements `contracts.TaskService` on the WorkBench SQL schema."""

    def __init__(self, database: str | Path, *, clock: Callable[[], str] = utc_now,
                 new_id: Callable[[str], str] | None = None):
        self._database = str(database)
        self._clock = clock
        self._new_id = new_id or (lambda prefix: f"{prefix}-{uuid.uuid4().hex}")

    # ------------------------------------------------------------------ access
    @staticmethod
    def _role(conn: sqlite3.Connection, actor: ActorContext) -> str:
        row = conn.execute(
            """SELECT m.role FROM memberships m JOIN principals p ON p.id=m.principal_id
               WHERE m.workspace_id=? AND m.principal_id=? AND m.active=1 AND p.active=1""",
            (actor.workspace_id, actor.principal_id),
        ).fetchone()
        if row is None or row["role"] != actor.role:
            raise Forbidden("actor is not an active member with this role")
        return row["role"]

    @staticmethod
    def _visible_task(conn, actor: ActorContext, role: str, task_id: Any) -> sqlite3.Row:
        if not isinstance(task_id, str) or not task_id.strip():
            raise InvalidOperation("task_id is required", field="task_id")
        row = conn.execute("SELECT * FROM tasks WHERE workspace_id=? AND id=?",
                           (actor.workspace_id, task_id)).fetchone()
        if row is None:
            raise NotFound("task not found")
        if role in READ_ALL_ROLES:
            return row
        if role == "requester" and row["requester_id"] == actor.principal_id:
            return row
        raise NotFound("task not found")

    @staticmethod
    def _workspace(conn, workspace_id: str) -> sqlite3.Row:
        return conn.execute("SELECT * FROM workspaces WHERE id=?", (workspace_id,)).fetchone()

    # -------------------------------------------------------------------- reads
    def _denied(self, actor: ActorContext, action: str, code: str) -> None:
        """Write the refusal in its own transaction (the failed one rolls back). No content, no target id."""
        import threading
        def write():
            try:
                with write_transaction(self._database) as conn:
                    conn.execute("INSERT INTO access_denials VALUES(?,?,?,?,?)",
                                 (actor.workspace_id, actor.principal_id, action, code, self._clock()))
            except Exception:  # noqa: BLE001 - auditing must never break the caller
                pass
        # The failing apply still holds the write lock; record after it is released.
        threading.Timer(0.05, write).start()

    def read(self, *, actor: ActorContext, task_id: str) -> dict[str, Any]:
        with read_snapshot(self._database) as conn:
            role = self._role(conn, actor)
            try:
                task = self._visible_task(conn, actor, role, task_id)
            except NotFound:
                self._denied(actor, "read", "NOT_FOUND")
                raise
            if role == "requester":
                return {"view": "requester", "task": self._requester_view(task),
                        "notes": [dict(r) for r in conn.execute(
                            """SELECT kind,reason,occurred_at FROM task_events
                               WHERE workspace_id=? AND task_id=? AND actor_id=?
                               ORDER BY recorded_at,id""",
                            (actor.workspace_id, task_id, actor.principal_id))]}
            ws = actor.workspace_id
            return {
                "view": "full",
                "task": dict(task),
                "focus": [r["slot"] for r in conn.execute(
                    "SELECT slot FROM task_focus WHERE workspace_id=? AND task_id=?", (ws, task_id))],
                "dependencies": [dict(r) for r in conn.execute(
                    "SELECT * FROM dependencies WHERE workspace_id=? AND task_id=? ORDER BY created_at,id",
                    (ws, task_id))],
                "reminders": [dict(r) for r in conn.execute(
                    """SELECT * FROM reminders WHERE workspace_id=? AND task_id=?
                       AND status!='cancelled' ORDER BY due_at,id""", (ws, task_id))],
                "responsibility": self._row_dict(conn.execute(
                    "SELECT * FROM task_responsibilities WHERE workspace_id=? AND task_id=?",
                    (ws, task_id)).fetchone()),
                "human_actions": [dict(r) for r in conn.execute(
                    """SELECT * FROM human_action_requests WHERE workspace_id=? AND task_id=?
                       ORDER BY created_at,id""", (ws, task_id))],
                "commitments": [dict(r) for r in conn.execute(
                    "SELECT * FROM commitments WHERE workspace_id=? AND task_id=? ORDER BY created_at,id",
                    (ws, task_id))],
                "deliverables": [dict(r) for r in conn.execute(
                    "SELECT * FROM deliverables WHERE workspace_id=? AND task_id=? ORDER BY created_at,id",
                    (ws, task_id))],
                "latest_checkpoint": self._row_dict(conn.execute(
                    """SELECT * FROM work_checkpoints WHERE workspace_id=? AND task_id=?
                       ORDER BY created_at DESC, rowid DESC LIMIT 1""", (ws, task_id)).fetchone()),
                "events": [dict(r) for r in conn.execute(
                    """SELECT * FROM (SELECT rowid AS seq, * FROM task_events WHERE workspace_id=? AND task_id=?
                       ORDER BY recorded_at DESC, seq DESC LIMIT 100) ORDER BY recorded_at, seq""",
                    (ws, task_id))],
            }

    def overview(self, *, actor: ActorContext, limit: int) -> dict[str, Any]:
        now = self._clock()
        with read_snapshot(self._database) as conn:
            role = self._role(conn, actor)
            ws = actor.workspace_id
            if role == "requester":
                rows = conn.execute(
                    """SELECT * FROM tasks WHERE workspace_id=? AND requester_id=?
                       ORDER BY updated_at DESC, id LIMIT ?""", (ws, actor.principal_id, limit))
                return {"view": "requester", "tasks": [self._requester_view(r) for r in rows]}

            def tasks(where: str, args: tuple = (), order: str = "priority, updated_at DESC, id"):
                return [dict(r) for r in conn.execute(
                    f"SELECT * FROM tasks WHERE workspace_id=? AND {where} ORDER BY {order} LIMIT ?",
                    (ws, *args, limit))]

            focus = {r["slot"]: self._row_dict(conn.execute(
                         "SELECT id,title,state,next_action,soft_due_at,official_due_at,version "
                         "FROM tasks WHERE workspace_id=? AND id=?", (ws, r["task_id"])).fetchone())
                     for r in conn.execute(
                         "SELECT slot,task_id FROM task_focus WHERE workspace_id=? AND task_id IS NOT NULL", (ws,))}
            owner_id = self._workspace(conn, ws)["owner_id"]
            waiting = tasks("state='WAITING'")
            for task in waiting:
                task["waiting_on"] = [dict(r) for r in conn.execute(
                    """SELECT id,kind,awaited_outcome,responsible_principal_id,created_at
                       FROM dependencies WHERE workspace_id=? AND task_id=? AND status='open'""",
                    (ws, task["id"]))]
            return {
                "view": "full",
                "now": now,
                "focus": {slot: focus.get(slot) for slot in ("NOW", "NEXT1", "NEXT2")},
                "inbox": tasks("state='INBOX'", order="created_at, id"),
                "ready": tasks("state='READY'"),
                "doing": tasks("state='DOING'"),
                "waiting": waiting,
                "blocked": tasks("state='BLOCKED'"),
                "overdue": tasks(
                    "state NOT IN ('DONE','CANCELLED') AND ((soft_due_at IS NOT NULL AND soft_due_at<?)"
                    " OR (official_due_at IS NOT NULL AND official_due_at<?))", (now, now),
                    order="COALESCE(official_due_at, soft_due_at), id"),
                "recently_done": tasks("state='DONE' AND completed_at>=?", (_plus_days(now, -7),),
                                       order="completed_at DESC, id"),
                "due_this_week": tasks(
                    "state NOT IN ('DONE','CANCELLED') AND COALESCE(official_due_at, soft_due_at) BETWEEN ? AND ?",
                    (now, _plus_days(now, 7)), order="COALESCE(official_due_at, soft_due_at), id"),
                "upcoming_followups": [dict(r) for r in conn.execute(
                    """SELECT r.id, r.task_id, t.title, r.due_at, r.kind, r.recipient_principal_id, r.repeats_left
                       FROM reminders r JOIN tasks t ON t.workspace_id=r.workspace_id AND t.id=r.task_id
                       WHERE r.workspace_id=? AND r.status='scheduled' ORDER BY r.due_at LIMIT ?""", (ws, limit))],
                "commitments_open": [dict(r) | {"direction": "owner_owes" if r["promisor_id"] == owner_id
                                                else "owed_to_owner" if r["promisee_id"] == owner_id
                                                else "third_party"}
                                     for r in conn.execute(
                    """SELECT id,task_id,promisor_id,promisee_id,promise_text,due_at,due_local FROM commitments
                       WHERE workspace_id=? AND status='open' ORDER BY due_at IS NULL, due_at LIMIT ?""",
                    (ws, limit))],
                "deliverables_pending": [dict(r) for r in conn.execute(
                    """SELECT id,task_id,title,status,revision,submitted_at FROM deliverables
                       WHERE workspace_id=? AND status NOT IN ('ACCEPTED') ORDER BY updated_at DESC LIMIT ?""",
                    (ws, limit))],
                "my_approvals": [dict(r) for r in conn.execute(
                    """SELECT id,task_id,tool_name,request_summary,created_at,expires_at FROM approval_requests
                       WHERE workspace_id=? AND approver_id=? AND status='pending' ORDER BY created_at LIMIT ?""",
                    (ws, actor.principal_id, limit))],
                "unknown_outcomes": [dict(r) for r in conn.execute(
                    """SELECT id,kind,task_id,updated_at FROM wake_events WHERE workspace_id=? AND status='unknown'
                       ORDER BY updated_at LIMIT ?""", (ws, limit))] + [dict(r) for r in conn.execute(
                    """SELECT id,'delivery' AS kind,task_id,updated_at FROM delivery_outbox WHERE workspace_id=?
                       AND status='unknown' ORDER BY updated_at LIMIT ?""", (ws, limit))],
                "my_human_actions": [dict(r) for r in conn.execute(
                    """SELECT * FROM human_action_requests WHERE workspace_id=? AND assignee_principal_id=?
                       AND status IN ('pending','in_progress') ORDER BY
                       CASE urgency WHEN 'interrupt' THEN 0 WHEN 'notify' THEN 1 ELSE 2 END,
                       due_at IS NULL, due_at, created_at LIMIT ?""", (ws, actor.principal_id, limit))],
            }

    def changes_since(self, *, actor: ActorContext, view: str = "default", limit: int = 200,
                      mark_seen: bool = False) -> dict[str, Any]:
        """Events the caller has not seen in this view; empty means nothing changed."""
        if not isinstance(view, str) or not view.strip():
            raise InvalidOperation("view is required")
        now = self._clock()
        with write_transaction(self._database) as conn:
            role = self._role(conn, actor)
            ws = actor.workspace_id
            mark = conn.execute(
                "SELECT last_seen_seq FROM view_watermarks WHERE workspace_id=? AND principal_id=? AND view=?",
                (ws, actor.principal_id, view)).fetchone()
            since = mark["last_seen_seq"] if mark else 0
            scope, args = ("", ()) if role in READ_ALL_ROLES else (
                "AND t.requester_id=? AND (e.actor_id=? OR e.kind IN ('created','state_changed','official_due_set')"
                " OR (e.kind IN ('patched','delayed') AND json_extract(e.after_json,'$.official_due_at') IS NOT NULL))",
                (actor.principal_id, actor.principal_id))
            rows = conn.execute(
                f"""SELECT e.rowid AS seq, e.task_id, t.title, e.kind, e.actor_id, e.reason, e.after_json,
                          e.evidence_type, e.occurred_at
                    FROM task_events e JOIN tasks t ON t.workspace_id=e.workspace_id AND t.id=e.task_id
                    WHERE e.workspace_id=? AND e.rowid>? {scope} ORDER BY e.rowid LIMIT ?""",
                (ws, since, *args, limit)).fetchall()
            events = [dict(r) for r in rows]
            if role == "requester":
                for event in events:
                    event.pop("after_json")
                    event.pop("evidence_type")
                    if event["actor_id"] != actor.principal_id:
                        event["reason"], event["actor_id"] = "", None
            last = events[-1]["seq"] if events else since
            if mark_seen and last > since:
                conn.execute(
                    """INSERT INTO view_watermarks(workspace_id,principal_id,view,last_seen_seq,updated_at)
                       VALUES(?,?,?,?,?) ON CONFLICT(workspace_id,principal_id,view)
                       DO UPDATE SET last_seen_seq=excluded.last_seen_seq, updated_at=excluded.updated_at""",
                    (ws, actor.principal_id, view, last, now))
            by_kind: dict[str, int] = {}
            for event in events:
                by_kind[event["kind"]] = by_kind.get(event["kind"], 0) + 1
            return {"since_seq": since, "until_seq": last, "unchanged": not events,
                    "counts": by_kind, "events": events}

    def evaluate_action(self, *, actor: ActorContext, task_id: str, action: str,
                        triggered_conditions: list[str] | tuple[str, ...] = ()) -> dict[str, Any]:
        """Host-side routing check for a proposed business action (not a tool grant)."""
        with read_snapshot(self._database) as conn:
            role = self._role(conn, actor)
            if role == "requester":
                raise Forbidden("requesters cannot evaluate delegated actions")
            self._visible_task(conn, actor, role, task_id)
            resp = conn.execute("SELECT * FROM task_responsibilities WHERE workspace_id=? AND task_id=?",
                                (actor.workspace_id, task_id)).fetchone()
            if resp is None or resp["status"] == "revoked":
                return {"route": "out_of_scope", "reason": "no active delegation for this task"}
            if resp["status"] == "paused":
                return {"route": "human_execution_required", "reason": "delegation paused by owner"}
            if resp["current_executor_id"] != actor.principal_id:
                return {"route": "out_of_scope", "reason": "caller is not the current executor of this task"}
            profile = self._profile(conn, actor.workspace_id, resp["profile_id"]) if resp["profile_id"] else None
            if profile is not None and profile["status"] != "active":
                return {"route": "human_approval_required",
                        "reason": "the work-class profile changed; the owner must re-confirm this delegation"}
            envelope = self._envelope(resp, profile)
            route = route_action(envelope, action=action, triggered_conditions=triggered_conditions)
            return {"route": route.value, "executor_id": resp["current_executor_id"],
                    "autonomy_level": resp["autonomy_level"], "profile_version": resp["profile_version"]}

    # ------------------------------------------------------------------- writes
    def apply(self, *, actor: ActorContext, idempotency_key: str,
              operations: list[dict[str, Any]]) -> list[dict[str, Any]]:
        if not isinstance(idempotency_key, str) or not idempotency_key.strip():
            raise InvalidOperation("idempotency_key required")
        payload_hash = _payload_hash(actor, operations)
        with write_transaction(self._database) as conn:
            prior = conn.execute(
                "SELECT payload_hash,result_json FROM command_receipts WHERE workspace_id=? AND idempotency_key=?",
                (actor.workspace_id, idempotency_key)).fetchone()
            if prior is not None:
                if prior["payload_hash"] != payload_hash:
                    raise IdempotencyConflict("idempotency key reused with a different payload")
                return json.loads(prior["result_json"])
            role = self._role(conn, actor)
            batch = _Batch(self, conn, actor, role, self._clock())
            results = []
            for index, op in enumerate(operations):
                try:
                    results.append(batch.run(op))
                except WorkbenchError as exc:
                    exc.details.setdefault("op_index", index)
                    if exc.code in ("FORBIDDEN", "NOT_FOUND"):
                        self._denied(actor, f"apply:{op.get('op')}", exc.code)
                    raise
            batch.finish(results)
            conn.execute(
                """INSERT INTO command_receipts(workspace_id,idempotency_key,payload_hash,result_json,created_at)
                   VALUES(?,?,?,?,?)""",
                (actor.workspace_id, idempotency_key, payload_hash, _dumps(results), batch.now))
            return results

    # ------------------------------------------------------------------ helpers
    @staticmethod
    def _row_dict(row) -> dict | None:
        return None if row is None else dict(row)

    @staticmethod
    def _requester_view(row) -> dict:
        keys = ("id", "title", "goal", "state", "official_due_at", "created_at", "updated_at")
        return {k: row[k] for k in keys}

    @staticmethod
    def _profile(conn, workspace_id: str, profile_id: str) -> sqlite3.Row:
        return conn.execute("SELECT * FROM delegation_profiles WHERE workspace_id=? AND id=?",
                            (workspace_id, profile_id)).fetchone()

    @staticmethod
    def _envelope(resp, profile) -> DelegationEnvelope:
        load = lambda row, key: frozenset(json.loads(row[key])) if row is not None else frozenset()
        level = AutonomyLevel[resp["autonomy_level"].upper()]
        return DelegationEnvelope(
            work_class=resp["work_class"],
            autonomy_level=level,
            profile_max_level=AutonomyLevel[profile["max_autonomy_level"].upper()] if profile else level,
            profile_allowed_actions=load(profile, "allowed_actions_json"),
            profile_reversible_actions=load(profile, "reversible_actions_json"),
            profile_review_actions=load(profile, "review_actions_json"),
            profile_approval_conditions=load(profile, "mandatory_approval_conditions_json"),
            allowed_actions=frozenset(json.loads(resp["effective_allowed_actions_json"])),
            reversible_actions=frozenset(json.loads(resp["effective_reversible_actions_json"])),
            approval_conditions=frozenset(json.loads(resp["effective_approval_conditions_json"])),
            review_actions=frozenset(json.loads(resp["effective_review_actions_json"])),
            policy_version=resp["profile_version"],
        )


class _Batch:
    """State for one atomic apply call."""

    def __init__(self, service: SqliteTaskService, conn, actor: ActorContext, role: str, now: str):
        self.s, self.c, self.actor, self.role, self.now = service, conn, actor, role, now
        self.ws = actor.workspace_id
        self.refs: dict[str, str] = {}
        self.touched: dict[str, int | None] = {}  # task_id -> version at batch start (None = created)
        self.owner_id = service._workspace(conn, self.ws)["owner_id"]

    # ---------------------------------------------------------------- plumbing
    def run(self, op: dict) -> dict:
        name = op.get("op")
        handler = getattr(self, f"op_{name}", None) if isinstance(name, str) else None
        if handler is None:
            raise InvalidOperation(f"unsupported operation: {name}")
        if self.role == "requester" and name not in REQUESTER_OPS:
            raise Forbidden("requesters may only file requests and notes")
        if self.role not in WRITE_ROLES and self.role != "requester":
            raise Forbidden("read-only role")
        evidence = op.get("evidence_type")
        if evidence not in EVIDENCE:
            raise InvalidOperation("evidence_type is required", field="evidence_type")
        result = handler(op, evidence)
        result["op"] = name
        return result

    def ref(self, value: Any) -> Any:
        if isinstance(value, str) and value.startswith("$"):
            if value[1:] not in self.refs:
                raise InvalidOperation(f"unknown batch reference {value}")
            return self.refs[value[1:]]
        return value

    def bind(self, op: dict, value: str) -> None:
        name = op.get("ref")
        if name is not None:
            if not isinstance(name, str) or not name or name in self.refs:
                raise InvalidOperation("ref must be a unique nonempty string within the batch")
            self.refs[name] = value

    def task(self, op: dict, *, check_version: bool = True) -> sqlite3.Row:
        task_id = self.ref(op.get("task_id"))
        row = self.s._visible_task(self.c, self.actor, self.role, task_id)
        if row["id"] not in self.touched:
            if check_version:
                expected = op.get("expected_version")
                if type(expected) is not int:
                    raise InvalidOperation("expected_version is required", field="expected_version")
                if expected != row["version"]:
                    raise VersionConflict("task changed since it was read",
                                          task_id=row["id"], current_version=row["version"])
            self.touched[row["id"]] = row["version"]
        return row

    def event(self, task_id: str, kind: str, op: dict, evidence: str, *,
              before: dict | None = None, after: dict | None = None) -> str:
        event_id = self.s._new_id("evt")
        occurred = _opt_utc(op.get("occurred_at"), "occurred_at") or self.now
        self.c.execute(
            """INSERT INTO task_events(id,workspace_id,task_id,actor_id,kind,before_json,after_json,
                 reason,evidence_type,source_ref,occurred_at,recorded_at)
               VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""",
            (event_id, self.ws, task_id, self.actor.principal_id, kind, _dumps(before or {}),
             _dumps(after or {}), _text(op.get("reason"), "reason"), evidence,
             op.get("source_ref"), occurred, self.now))
        return event_id

    def strong(self, op: dict, evidence: str, what: str) -> None:
        if evidence not in STRONG_EVIDENCE or not op.get("source_ref") or not _text(op.get("reason"), "reason"):
            raise EvidenceRequired(f"{what} needs explicit_user/verified_tool evidence, source_ref and reason")

    def owner_only(self, what: str, op: dict | None = None) -> None:
        if self.role != "owner":
            raise Forbidden(f"only the workspace owner may {what}")
        if op is not None:
            self.cites_message(op, f"{what}", sender=self.actor.principal_id)

    def cites_message(self, op: dict, what: str, *, sender: str | None = None, any_human: bool = False,
                      ref_field: str = "source_ref") -> str | None:
        """A human decision made through a chat Host must point at an inbox message that person really
        sent; otherwise the model could invent "the owner said yes". The trusted CLI is exempt.
        Returns the sender principal of the cited message."""
        if self.actor.source == "cli":
            return sender
        ref = op.get(ref_field)
        row = ref and self.c.execute(
            """SELECT i.sender_principal_id, p.kind FROM inbox_messages i JOIN principals p ON p.id=i.sender_principal_id
               WHERE i.workspace_id=? AND (i.raw_source_ref=? OR EXISTS (SELECT 1 FROM inbox_revisions r
                    WHERE r.inbox_id=i.id AND r.raw_source_ref=?))""", (self.ws, ref, ref)).fetchone()
        ok = row is not None and ((sender is not None and row["sender_principal_id"] == sender)
                                  or (any_human and row["kind"] == "human"))
        if not ok:
            raise EvidenceRequired(f"{what} must cite a message that person actually sent ({ref_field})")
        return row["sender_principal_id"]

    def role_of(self, principal_id: str) -> str | None:
        row = self.c.execute("SELECT role FROM memberships WHERE workspace_id=? AND principal_id=? AND active=1",
                             (self.ws, principal_id)).fetchone()
        return row["role"] if row else None

    def project(self, project_id: Any) -> str | None:
        if project_id is None:
            return None
        if not self.c.execute("SELECT 1 FROM projects WHERE workspace_id=? AND id=?", (self.ws, project_id)).fetchone():
            raise InvalidOperation("project_id is not a project of this workspace", field="project_id")
        return project_id

    def member(self, principal_id: Any, field: str, roles: tuple[str, ...] | None = None) -> str:
        row = self.c.execute(
            """SELECT m.role FROM memberships m JOIN principals p ON p.id=m.principal_id
               WHERE m.workspace_id=? AND m.principal_id=? AND m.active=1 AND p.active=1""",
            (self.ws, principal_id)).fetchone()
        if row is None:
            raise InvalidOperation(f"{field} is not an active workspace member", field=field)
        if roles is not None and row["role"] not in roles:
            raise InvalidOperation(f"{field} must be one of the workspace's own people ({', '.join(roles)})", field=field)
        return principal_id

    def finish(self, results: list[dict]) -> None:
        versions = {}
        for task_id, start in self.touched.items():
            if start is None:
                versions[task_id] = 1
                continue
            updated = self.c.execute(
                "UPDATE tasks SET version=version+1, updated_at=? WHERE workspace_id=? AND id=? AND version=?",
                (self.now, self.ws, task_id, start)).rowcount
            if updated != 1:
                raise VersionConflict("task changed during the batch", task_id=task_id)
            versions[task_id] = start + 1
        for result in results:
            if result.get("task_id") in versions:
                result["version"] = versions[result["task_id"]]

    # ------------------------------------------------------------------- tasks
    def op_create_task(self, op: dict, evidence: str) -> dict:
        task_id = self.s._new_id("task")
        if self.role == "requester":
            requester, assignee, state = self.actor.principal_id, self.owner_id, "INBOX"
            if any(op.get(k) is not None for k in ("soft_due_at", "planned_start_at", "assignee_id", "state")):
                raise Forbidden("requesters cannot set internal planning fields")
        else:
            requester = op.get("requester_id")
            if requester is not None:
                self.member(requester, "requester_id")
            assignee = self.member(op.get("assignee_id") or self.owner_id, "assignee_id", ("owner", "editor"))
            state = op.get("state", "INBOX")
            if state not in ("INBOX", "READY", "DOING"):
                raise InvalidOperation("new tasks start in INBOX, READY or DOING", field="state")
        priority = op.get("priority", 2)
        if type(priority) is not int or not 0 <= priority <= 4:
            raise InvalidOperation("priority must be 0..4", field="priority")
        values = {
            "title": _text(op.get("title"), "title", required=True),
            "goal": _text(op.get("goal"), "goal"),
            "next_action": _text(op.get("next_action"), "next_action"),
            "state": state, "priority": priority,
            "official_due_at": _opt_utc(op.get("official_due_at"), "official_due_at"),
            "soft_due_at": _opt_utc(op.get("soft_due_at"), "soft_due_at"),
            "planned_start_at": _opt_utc(op.get("planned_start_at"), "planned_start_at"),
            "project_id": self.project(op.get("project_id")), "requester_id": requester, "assignee_id": assignee,
        }
        cols = ", ".join(values)
        self.c.execute(
            f"INSERT INTO tasks(id,workspace_id,{cols},created_at,updated_at) "
            f"VALUES(?,?,{', '.join('?' * len(values))},?,?)",
            (task_id, self.ws, *values.values(), self.now, self.now))
        self.touched[task_id] = None
        self.bind(op, task_id)
        event_id = self.event(task_id, "created", op, evidence, after=values)
        return {"task_id": task_id, "event_id": event_id}

    def op_patch_task(self, op: dict, evidence: str) -> dict:
        task = self.task(op)
        fields = _obj(op.get("fields"), "fields")
        unknown = set(fields) - set(PATCHABLE)
        if not fields or unknown:
            raise InvalidOperation(f"patchable fields: {', '.join(PATCHABLE)}", field="fields")
        before, after = {}, {}
        for key, value in fields.items():
            if key in ("soft_due_at", "planned_start_at", "official_due_at"):
                value = _opt_utc(value, key)
            elif key == "priority":
                if type(value) is not int or not 0 <= value <= 4:
                    raise InvalidOperation("priority must be 0..4", field="priority")
            elif key == "assignee_id":
                value = self.member(value, "assignee_id", ("owner", "editor"))
            elif key == "project_id":
                value = self.project(value or None)
            else:
                value = _text(value, key, required=(key == "title"))
            if value != task[key]:
                before[key], after[key] = task[key], value
        if not after:
            return {"task_id": task["id"], "changed": False}
        if "official_due_at" in after and task["official_due_at"] is not None:
            self.strong(op, evidence, "changing an existing official due")
        delayed = [k for k in DUE_FIELDS if k in after and task[k] is not None
                   and (after[k] is None or after[k] > task[k])]
        if delayed:
            if op.get("delay_kind") not in DELAY_KINDS or not _text(op.get("reason"), "reason"):
                raise InvalidOperation("moving a due date later needs delay_kind and reason",
                                       field="delay_kind", allowed=sorted(DELAY_KINDS))
            after["delay_kind"] = op["delay_kind"]
        assignments = {k: v for k, v in after.items() if k != "delay_kind"}
        if "official_due_at" in assignments:  # a raw instant replaces any local-date record
            assignments.update(official_due_local=None, official_due_timezone=None,
                               official_due_precision="datetime" if assignments["official_due_at"] else None)
        self.c.execute(
            f"UPDATE tasks SET {', '.join(f'{k}=?' for k in assignments)} WHERE workspace_id=? AND id=?",
            (*assignments.values(), self.ws, task["id"]))
        kind = "delayed" if delayed else "patched"
        return {"task_id": task["id"], "event_id": self.event(task["id"], kind, op, evidence,
                                                              before=before, after=after)}

    def op_set_official_due(self, op: dict, evidence: str) -> dict:
        """Record an official deadline as the source stated it (date or local date-time)."""
        task = self.task(op)
        tz_name = op.get("timezone") or self.s._workspace(self.c, self.ws)["timezone"]
        local = op.get("local")
        if not isinstance(local, str):
            raise InvalidOperation("local is required (YYYY-MM-DD or YYYY-MM-DDTHH:MM)", field="local")
        precision = "date" if len(local) == 10 else "datetime"
        # A date-only deadline counts as due at the end of that local day, never at local midnight.
        due_at = _local_to_utc(local + "T23:59:59" if precision == "date" else local, tz_name)
        if task["official_due_at"] is not None and due_at != task["official_due_at"]:
            self.strong(op, evidence, "changing an existing official due")
            if due_at > task["official_due_at"] and op.get("delay_kind") not in DELAY_KINDS:
                raise InvalidOperation("moving a due date later needs delay_kind and reason",
                                       field="delay_kind", allowed=sorted(DELAY_KINDS))
        before = {k: task[k] for k in ("official_due_at", "official_due_local", "official_due_timezone",
                                       "official_due_precision")}
        after = {"official_due_at": due_at, "official_due_local": local,
                 "official_due_timezone": tz_name, "official_due_precision": precision}
        self.c.execute(
            f"UPDATE tasks SET {', '.join(f'{k}=?' for k in after)} WHERE workspace_id=? AND id=?",
            (*after.values(), self.ws, task["id"]))
        kind = "delayed" if task["official_due_at"] and due_at > task["official_due_at"] else "official_due_set"
        if kind == "delayed":
            after = dict(after, delay_kind=op["delay_kind"])
        suggestion = None if task["soft_due_at"] else self.suggest_soft_due(due_at, tz_name)
        if suggestion:
            after = dict(after, suggested_soft_due=suggestion)
        return {"task_id": task["id"], "official_due_at": due_at, "suggested_soft_due": suggestion,
                "event_id": self.event(task["id"], kind, op, evidence, before=before, after=after)}

    def suggest_soft_due(self, official_due_at: str, tz_name: str) -> dict | None:
        """Owner's rule, e.g. {"soft_due_rule": {"days_before": 3, "skip_weekends": true, "at": "18:00"}}.
        Only a suggestion with its basis; it becomes soft_due when someone patches it in."""
        row = self.c.execute(
            "SELECT work_preferences_json FROM user_profiles WHERE workspace_id=? AND principal_id=?",
            (self.ws, self.owner_id)).fetchone()
        rule = json.loads(row["work_preferences_json"]).get("soft_due_rule") if row else None
        if not rule:
            return None
        from datetime import timedelta
        zone = ZoneInfo(tz_name)
        day = datetime.fromisoformat(official_due_at[:-1] + "+00:00").astimezone(zone).date()
        left = int(rule.get("days_before", 3))
        while left > 0:
            day -= timedelta(days=1)
            if rule.get("skip_weekends", True) and day.weekday() >= 5:
                continue
            left -= 1
        local = f"{day.isoformat()}T{rule.get('at', '18:00')}"
        return {"soft_due_at": _local_to_utc(local, tz_name), "local": local,
                "basis": f"官方截止前 {rule.get('days_before', 3)} 个{'工作日' if rule.get('skip_weekends', True) else '天'}"}

    # ------------------------------------------------- commitments/deliverables
    def op_record_commitment(self, op: dict, evidence: str) -> dict:
        """Who owes whom what by when. Inferred promises are not recorded as commitments."""
        task = self.task(op)
        if evidence == "inferred":
            raise EvidenceRequired("a commitment must be stated, not inferred")
        promisor = op.get("promisor_id")
        promisee = op.get("promisee_id")
        for pid, field in ((promisor, "promisor_id"), (promisee, "promisee_id")):
            if pid is not None and self.c.execute("SELECT 1 FROM principals WHERE id=?", (pid,)).fetchone() is None:
                raise InvalidOperation(f"unknown {field}", field=field)
        if promisor is None:
            raise InvalidOperation("promisor_id is required", field="promisor_id")
        due_at = due_local = tz_name = None
        if op.get("due_local"):
            tz_name = op.get("timezone") or self.s._workspace(self.c, self.ws)["timezone"]
            due_local = op["due_local"]
            due_at = _local_to_utc(due_local + "T23:59:59" if len(due_local) == 10 else due_local, tz_name)
        commitment_id = self.s._new_id("com")
        self.c.execute(
            """INSERT INTO commitments(id,workspace_id,task_id,promisor_id,promisee_id,promise_text,due_at,
                 due_local,due_timezone,evidence_type,source_ref,created_at,updated_at)
               VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (commitment_id, self.ws, task["id"], promisor, promisee,
             _text(op.get("promise_text"), "promise_text", required=True), due_at, due_local, tz_name,
             evidence, op.get("source_ref"), self.now, self.now))
        self.bind(op, commitment_id)
        direction = "owner_owes" if promisor == self.owner_id else ("owed_to_owner" if promisee == self.owner_id else "third_party")
        return {"task_id": task["id"], "commitment_id": commitment_id, "direction": direction,
                "event_id": self.event(task["id"], "commitment_recorded", op, evidence, after={
                    "commitment_id": commitment_id, "promisor_id": promisor, "promisee_id": promisee,
                    "due_at": due_at, "direction": direction})}

    def op_update_commitment(self, op: dict, evidence: str) -> dict:
        cid = self.ref(op.get("commitment_id"))
        row = self.c.execute("SELECT * FROM commitments WHERE workspace_id=? AND id=?", (self.ws, cid)).fetchone()
        if row is None:
            raise NotFound("commitment not found")
        task = self.task(dict(op, task_id=row["task_id"]))
        status = op.get("status")
        if status not in ("kept", "broken", "rescheduled", "withdrawn"):
            raise InvalidOperation("status must be kept, broken, rescheduled or withdrawn", field="status")
        if row["status"] != "open":
            raise InvalidOperation(f"commitment is already {row['status']}")
        if evidence == "inferred" or not op.get("source_ref"):
            raise EvidenceRequired("closing a commitment needs a non-inferred source_ref")
        self.c.execute("UPDATE commitments SET status=?, version=version+1, updated_at=? WHERE id=?",
                       (status, self.now, cid))
        result = {"task_id": task["id"], "commitment_id": cid, "status": status}
        if status == "rescheduled" and op.get("new_due_local"):
            follow = dict(op, op="record_commitment", task_id=task["id"], promisor_id=row["promisor_id"],
                          promisee_id=row["promisee_id"], promise_text=row["promise_text"],
                          due_local=op["new_due_local"], ref=None)
            follow.pop("ref")
            result["new_commitment_id"] = self.op_record_commitment(follow, evidence)["commitment_id"]
        result["event_id"] = self.event(task["id"], "commitment_updated", op, evidence,
                                        before={"status": "open"}, after={"commitment_id": cid, "status": status})
        return result

    DELIVERABLE_FLOW = {
        "DRAFT": {"SUBMITTED"},
        "SUBMITTED": {"ACKNOWLEDGED", "REVIEW_REQUIRED", "ACCEPTED", "REJECTED"},
        "ACKNOWLEDGED": {"REVIEW_REQUIRED", "ACCEPTED", "REJECTED"},
        "REVIEW_REQUIRED": {"SUBMITTED"},
        "REJECTED": {"SUBMITTED"},
        "ACCEPTED": set(),
    }

    def op_register_deliverable(self, op: dict, evidence: str) -> dict:
        task = self.task(op)
        deliverable_id = self.s._new_id("dlv")
        values = {"title": _text(op.get("title"), "title", required=True),
                  "artifact_ref": _text(op.get("artifact_ref"), "artifact_ref", required=True)}
        self.c.execute(
            """INSERT INTO deliverables(id,workspace_id,task_id,title,artifact_ref,created_at,updated_at)
               VALUES(?,?,?,?,?,?,?)""",
            (deliverable_id, self.ws, task["id"], *values.values(), self.now, self.now))
        self.bind(op, deliverable_id)
        return {"task_id": task["id"], "deliverable_id": deliverable_id,
                "event_id": self.event(task["id"], "deliverable_registered", op, evidence,
                                       after={"deliverable_id": deliverable_id, **values})}

    def op_update_deliverable(self, op: dict, evidence: str) -> dict:
        """Sent is not received; received is not accepted. Each step needs its own evidence."""
        did = self.ref(op.get("deliverable_id"))
        row = self.c.execute("SELECT * FROM deliverables WHERE workspace_id=? AND id=?", (self.ws, did)).fetchone()
        if row is None:
            raise NotFound("deliverable not found")
        task = self.task(dict(op, task_id=row["task_id"]))
        status = op.get("status")
        if status not in self.DELIVERABLE_FLOW.get(row["status"], set()):
            raise InvalidOperation(f"deliverable cannot move {row['status']} -> {status}", field="status")
        sets = {"status": status}
        if status == "SUBMITTED":
            if evidence != "verified_tool" or not op.get("receipt_ref"):
                raise EvidenceRequired("SUBMITTED needs a verified delivery receipt (receipt_ref)")
            if self.actor.source != "cli" and not self.c.execute(
                    """SELECT 1 FROM delivery_outbox WHERE workspace_id=? AND id=? AND status='sent'
                       AND provider_message_id IS NOT NULL""", (self.ws, op["receipt_ref"])).fetchone():
                raise EvidenceRequired("receipt_ref must be a delivery the carrier confirmed as sent")
            sets.update(submit_receipt_ref=op["receipt_ref"], submitted_at=self.now,
                        revision=row["revision"] + (1 if row["status"] in ("REVIEW_REQUIRED", "REJECTED") else 0),
                        artifact_ref=op.get("artifact_ref") or row["artifact_ref"])
        else:
            if evidence not in STRONG_EVIDENCE or not op.get("source_ref"):
                raise EvidenceRequired(f"{status} needs the recipient's own message as source_ref")
            # Outcomes count as promotion evidence, so they must come from a real person's message.
            self.cites_message(op, f"marking a deliverable {status}", any_human=True)
            if status == "ACKNOWLEDGED":
                sets["acknowledged_at"] = self.now
            if status in ("ACCEPTED", "REJECTED", "REVIEW_REQUIRED"):
                sets["acceptance_source_ref"] = op["source_ref"]
            if status == "ACCEPTED":
                sets["accepted_at"] = self.now
        self.c.execute(
            f"UPDATE deliverables SET {', '.join(f'{k}=?' for k in sets)}, version=version+1, updated_at=? WHERE id=?",
            (*sets.values(), self.now, did))
        return {"task_id": task["id"], "deliverable_id": did, "status": status,
                "event_id": self.event(task["id"], "deliverable_" + status.lower(), op, evidence,
                                       before={"status": row["status"]}, after={"deliverable_id": did, **sets})}

    def op_save_checkpoint(self, op: dict, evidence: str) -> dict:
        """Resume point for the next person/Agent; unverified items stay labelled as such."""
        task = self.task(op, check_version=False)
        checkpoint_id = self.s._new_id("chk")
        self.c.execute(
            """INSERT INTO work_checkpoints(id,workspace_id,task_id,task_version,author_id,verified_at,repo_ref,
                 artifact_refs_json,completed_steps_json,unverified_json,next_action,blockers_json,source_ref,created_at)
               VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (checkpoint_id, self.ws, task["id"], task["version"], self.actor.principal_id,
             _opt_utc(op.get("verified_at"), "verified_at") or self.now, op.get("repo_ref"),
             _dumps(_str_list(op.get("artifact_refs"), "artifact_refs")),
             _dumps(_str_list(op.get("completed_steps"), "completed_steps")),
             _dumps(_str_list(op.get("unverified"), "unverified")),
             _text(op.get("next_action"), "next_action", required=True),
             _dumps(_str_list(op.get("blockers"), "blockers")), op.get("source_ref"), self.now))
        return {"task_id": task["id"], "checkpoint_id": checkpoint_id,
                "event_id": self.event(task["id"], "checkpoint_saved", op, evidence,
                                       after={"checkpoint_id": checkpoint_id})}

    # ------------------------------------------------------ schedules/watches
    def op_create_schedule(self, op: dict, evidence: str) -> dict:
        """Recurring work, e.g. "month-end: pull the due-list and email the boss"."""
        from .scheduler import local_to_utc, validate_schedule
        tz_name = op.get("timezone") or self.s._workspace(self.c, self.ws)["timezone"]
        interval_n = op.get("interval", 1)
        local, at = validate_schedule(op.get("frequency"), interval_n, op.get("local_time", ""),
                                      op.get("start_local_date", ""), tz_name)
        max_occ = op.get("max_occurrences")
        if max_occ is not None and (type(max_occ) is not int or max_occ < 1):
            raise InvalidOperation("max_occurrences must be a positive integer")
        until_at = local_to_utc(op["until_local"], tz_name) if op.get("until_local") else None
        schedule_id = self.s._new_id("sch")
        template = _obj(op.get("template"), "template")
        if template.get("delegate"):
            self.owner_only("attach a delegation to a recurring schedule", op)
            spec = _obj(template["delegate"], "template.delegate")
            if spec.get("autonomy_level") not in LEVELS:
                raise InvalidOperation("template.delegate.autonomy_level is required", allowed=list(LEVELS))
            self.member(spec.get("executor_id"), "template.delegate.executor_id")
        self.c.execute(
            """INSERT INTO schedule_rules(id,workspace_id,title,template_json,timezone,frequency,interval_n,
                 local_time,next_fire_local,next_fire_at,max_occurrences,until_at,created_by_principal_id,
                 created_at,updated_at,anchor_day) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (schedule_id, self.ws, _text(op.get("title"), "title", required=True), _dumps(template), tz_name,
             op["frequency"], interval_n, op["local_time"], local, at, max_occ, until_at,
             self.actor.principal_id, self.now, self.now,
             int(op.get("anchor_day") or local[8:10]) if op["frequency"] == "monthly" else None))
        self.bind(op, schedule_id)
        return {"schedule_id": schedule_id, "next_fire_local": local, "next_fire_at": at}

    def op_set_schedule_active(self, op: dict, evidence: str) -> dict:
        sid = self.ref(op.get("schedule_id"))
        active = op.get("active")
        if type(active) is not bool:
            raise InvalidOperation("active must be true or false", field="active")
        changed = self.c.execute("UPDATE schedule_rules SET active=?, updated_at=? WHERE workspace_id=? AND id=?",
                                 (1 if active else 0, self.now, self.ws, sid)).rowcount
        if not changed:
            raise NotFound("schedule not found")
        return {"schedule_id": sid, "active": active}

    def op_add_watch(self, op: dict, evidence: str) -> dict:
        task = self.task(op)
        dep_id = self.ref(op.get("dependency_id"))
        if dep_id is not None and self.c.execute(
                "SELECT 1 FROM dependencies WHERE workspace_id=? AND id=? AND task_id=? AND status='open'",
                (self.ws, dep_id, task["id"])).fetchone() is None:
            raise InvalidOperation("dependency_id must be an open dependency of this task")
        watch_id = self.s._new_id("wat")
        values = {"source_kind": _text(op.get("source_kind"), "source_kind", required=True),
                  "target_ref": _text(op.get("target_ref"), "target_ref", required=True),
                  "expected_value": _text(op.get("expected_value"), "expected_value", required=True)}
        self.c.execute(
            """INSERT INTO external_watches(id,workspace_id,task_id,dependency_id,source_kind,target_ref,
                 expected_value,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?)""",
            (watch_id, self.ws, task["id"], dep_id, *values.values(), self.now, self.now))
        self.bind(op, watch_id)
        return {"task_id": task["id"], "watch_id": watch_id,
                "event_id": self.event(task["id"], "watch_added", op, evidence,
                                       after={"watch_id": watch_id, "dependency_id": dep_id, **values})}

    # --------------------------------------------- proposals, projects, handoff
    def op_propose_change(self, op: dict, evidence: str) -> dict:
        """When the evidence is not enough to change something (e.g. an official due someone mentioned),
        file a decision for the owner with the proposed values instead of overwriting."""
        task = self.task(op, check_version=False)
        fields = _obj(op.get("fields"), "fields")
        if not fields or set(fields) - set(PATCHABLE) - {"official_due_local"}:
            raise InvalidOperation("fields must be patchable task fields", field="fields")
        request_id = self.s._new_id("hum")
        self.c.execute(
            """INSERT INTO human_action_requests(id,workspace_id,task_id,requested_by_principal_id,assignee_principal_id,
                 request_kind,title,request_json,urgency,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
            (request_id, self.ws, task["id"], self.actor.principal_id, self.owner_id, "decision",
             _text(op.get("title"), "title") or f"确认修改：{task['title']}",
             _dumps({"kind": "proposed_change", "fields": fields,
                     "current": {k: task[k] for k in fields if k in task.keys()},
                     "reason": _text(op.get("reason"), "reason"), "source_ref": op.get("source_ref")}),
             self.urgency(op), self.now, self.now))
        self.announce(task["id"], self.owner_id, self.urgency(op), _text(op.get("title"), "title")
                      or f"确认修改：{task['title']}", request_id)
        return {"task_id": task["id"], "human_action_id": request_id,
                "event_id": self.event(task["id"], "change_proposed", op, evidence,
                                       after={"human_action_id": request_id, "fields": fields})}

    def op_create_project(self, op: dict, evidence: str) -> dict:
        project_id = self.s._new_id("prj")
        self.c.execute(
            "INSERT INTO projects(id,workspace_id,title,goal,created_at,updated_at) VALUES(?,?,?,?,?,?)",
            (project_id, self.ws, _text(op.get("title"), "title", required=True), _text(op.get("goal"), "goal"),
             self.now, self.now))
        self.bind(op, project_id)
        return {"project_id": project_id}

    def op_update_project(self, op: dict, evidence: str) -> dict:
        pid = self.ref(op.get("project_id"))
        row = self.c.execute("SELECT * FROM projects WHERE workspace_id=? AND id=?", (self.ws, pid)).fetchone()
        if row is None:
            raise NotFound("project not found")
        state = op.get("state", row["state"])
        if state not in ("active", "paused", "done", "cancelled"):
            raise InvalidOperation("state must be active, paused, done or cancelled", field="state")
        self.c.execute("UPDATE projects SET title=?, goal=?, state=?, updated_at=? WHERE id=?",
                       (_text(op.get("title"), "title") or row["title"],
                        _text(op.get("goal"), "goal") if op.get("goal") is not None else row["goal"],
                        state, self.now, pid))
        return {"project_id": pid, "state": state}

    def op_handoff(self, op: dict, evidence: str) -> dict:
        """The current executor (an Agent) passes the task to another Agent at the same level or lower,
        leaving a checkpoint. Raising autonomy or handing to a human stays the owner's call."""
        task = self.task(op)
        resp = self.c.execute("SELECT * FROM task_responsibilities WHERE workspace_id=? AND task_id=?",
                              (self.ws, task["id"])).fetchone()
        if resp is None or resp["status"] != "active" or resp["current_executor_id"] != self.actor.principal_id:
            raise Forbidden("only the current executor of an active delegation can hand off")
        target = self.member(op.get("to_executor_id"), "to_executor_id")
        if not self._is_agent(target) or target == self.actor.principal_id:
            raise InvalidOperation("handoff goes to another Agent; handing to a human is the owner's decision")
        next_action = _text(op.get("next_action"), "next_action", required=True)
        self.op_save_checkpoint(dict(op, next_action=next_action), evidence)
        self.c.execute(
            """UPDATE task_responsibilities SET current_executor_id=?, version=version+1, updated_by_principal_id=?,
                 updated_at=? WHERE workspace_id=? AND task_id=?""",
            (target, self.actor.principal_id, self.now, self.ws, task["id"]))
        profile = self.s._profile(self.c, self.ws, resp["profile_id"]) if resp["profile_id"] else None
        self._responsibility_event(task["id"], "delegated", dict(op, reason=_text(op.get("reason"), "reason") or "handoff"),
                                   resp, target, resp["autonomy_level"], profile)
        wake_id = self.s._new_id("wake")
        self.c.execute(
            """INSERT INTO wake_events(id,workspace_id,task_id,origin,origin_event_id,kind,source_ref,created_at,updated_at)
               VALUES(?,?,?,?,?,?,?,?,?)""",
            (wake_id, self.ws, task["id"], "workbench.handoff", self.s._new_id("ho"), "agent_handoff",
             f"workdesk://{self.ws}/tasks/{task['id']}/handoff", self.now, self.now))
        return {"task_id": task["id"], "to_executor_id": target, "wake_event_id": wake_id,
                "event_id": self.event(task["id"], "handoff", op, evidence,
                                       after={"from": self.actor.principal_id, "to": target})}

    def op_transition(self, op: dict, evidence: str) -> dict:
        task = self.task(op)
        target, current = op.get("to"), task["state"]
        if target not in TRANSITIONS.get(current, ()):
            raise InvalidOperation(f"cannot move {current} -> {target}", field="to")
        if target in ("DONE", "CANCELLED") or current in ("DONE", "CANCELLED"):
            self.strong(op, evidence, f"{current} -> {target}")
        if target == "WAITING" and not self.c.execute(
                "SELECT 1 FROM dependencies WHERE workspace_id=? AND task_id=? AND status='open'",
                (self.ws, task["id"])).fetchone():
            raise InvalidOperation("WAITING needs an open dependency (add_dependency first)")
        if target == "BLOCKED" and not _text(op.get("reason"), "reason"):
            raise InvalidOperation("BLOCKED needs a reason", field="reason")
        completed = self.now if target == "DONE" else (None if current == "DONE" else task["completed_at"])
        self.c.execute("UPDATE tasks SET state=?, completed_at=? WHERE workspace_id=? AND id=?",
                       (target, completed, self.ws, task["id"]))
        if target in ("DONE", "CANCELLED"):
            self._close_task(task["id"], cancel_dependencies=(target == "CANCELLED"))
        return {"task_id": task["id"], "event_id": self.event(
            task["id"], "state_changed", op, evidence, before={"state": current}, after={"state": target})}

    def _close_task(self, task_id: str, *, cancel_dependencies: bool) -> None:
        self.c.execute("""UPDATE reminders SET status='cancelled', updated_at=?
                          WHERE workspace_id=? AND task_id=? AND status='scheduled'""",
                       (self.now, self.ws, task_id))
        self.c.execute("DELETE FROM task_focus WHERE workspace_id=? AND task_id=?", (self.ws, task_id))
        self.c.execute("""UPDATE human_action_requests SET status='cancelled', updated_at=?
                          WHERE workspace_id=? AND task_id=? AND status IN ('pending','in_progress')""",
                       (self.now, self.ws, task_id))
        if cancel_dependencies:
            self.c.execute("""UPDATE dependencies SET status='cancelled', resolved_at=?
                              WHERE workspace_id=? AND task_id=? AND status='open'""",
                           (self.now, self.ws, task_id))

    def op_append_progress(self, op: dict, evidence: str) -> dict:
        task = self.task(op, check_version=(self.role != "requester"))
        text = _text(op.get("text"), "text", required=True)
        if self.role == "requester":
            note = dict(op, reason=text)
            return {"task_id": task["id"], "event_id": self.event(task["id"], "requester_note", note, evidence)}
        summary = (task["done_summary"] + "\n" + text).strip() if task["done_summary"] else text
        after = {"done_summary": summary}
        if op.get("next_action") is not None:
            after["next_action"] = _text(op.get("next_action"), "next_action")
        self.c.execute(
            f"UPDATE tasks SET {', '.join(f'{k}=?' for k in after)} WHERE workspace_id=? AND id=?",
            (*after.values(), self.ws, task["id"]))
        return {"task_id": task["id"], "event_id": self.event(
            task["id"], "progress", op, evidence,
            before={k: task[k] for k in after}, after={"progress": text, **after})}

    def op_set_focus(self, op: dict, evidence: str) -> dict:
        self.owner_only("set NOW/NEXT focus", op)
        slot = op.get("slot")
        if slot not in ("NOW", "NEXT1", "NEXT2"):
            raise InvalidOperation("slot must be NOW, NEXT1 or NEXT2", field="slot")
        previous = self.c.execute("SELECT task_id FROM task_focus WHERE workspace_id=? AND slot=?",
                                  (self.ws, slot)).fetchone()
        target = None
        if op.get("task_id") is not None:
            target = self.task(op, check_version=False)
            if target["state"] in ("DONE", "CANCELLED"):
                raise InvalidOperation("closed tasks cannot take a focus slot")
            self.c.execute("DELETE FROM task_focus WHERE workspace_id=? AND task_id=?", (self.ws, target["id"]))
        self.c.execute("DELETE FROM task_focus WHERE workspace_id=? AND slot=?", (self.ws, slot))
        if target is not None:
            self.c.execute("INSERT INTO task_focus(workspace_id,slot,task_id,actor_id,updated_at) VALUES(?,?,?,?,?)",
                           (self.ws, slot, target["id"], self.actor.principal_id, self.now))
        result = {"slot": slot, "task_id": target["id"] if target else None}
        if previous and previous["task_id"] and (target is None or previous["task_id"] != target["id"]):
            self.touched.setdefault(previous["task_id"], self.c.execute(
                "SELECT version FROM tasks WHERE workspace_id=? AND id=?",
                (self.ws, previous["task_id"])).fetchone()["version"])
            self.event(previous["task_id"], "focus_cleared", op, evidence, before={"slot": slot})
        if target is not None:
            result["event_id"] = self.event(target["id"], "focus_set", op, evidence, after={"slot": slot})
        return result

    # ------------------------------------------------------- waiting/reminders
    def op_add_dependency(self, op: dict, evidence: str) -> dict:
        task = self.task(op)
        kind = op.get("kind")
        if kind not in ("external_response", "prerequisite_task", "artifact", "permission", "other"):
            raise InvalidOperation("unsupported dependency kind", field="kind")
        responsible = op.get("responsible_principal_id")
        if responsible is not None and self.c.execute(
                "SELECT 1 FROM principals WHERE id=?", (responsible,)).fetchone() is None:
            raise InvalidOperation("unknown responsible principal", field="responsible_principal_id")
        related = self.ref(op.get("related_task_id"))
        if related is not None:
            self.s._visible_task(self.c, self.actor, self.role, related)
        dep_id = self.s._new_id("dep")
        values = {"kind": kind, "awaited_outcome": _text(op.get("awaited_outcome"), "awaited_outcome", required=True),
                  "responsible_principal_id": responsible, "related_task_id": related}
        self.c.execute(
            """INSERT INTO dependencies(id,workspace_id,task_id,kind,awaited_outcome,
                 responsible_principal_id,related_task_id,created_at) VALUES(?,?,?,?,?,?,?,?)""",
            (dep_id, self.ws, task["id"], *values.values(), self.now))
        self.bind(op, dep_id)
        return {"task_id": task["id"], "dependency_id": dep_id,
                "event_id": self.event(task["id"], "dependency_added", op, evidence,
                                       after={"dependency_id": dep_id, **values})}

    def op_resolve_dependency(self, op: dict, evidence: str) -> dict:
        dep_id = self.ref(op.get("dependency_id"))
        dep = self.c.execute("SELECT * FROM dependencies WHERE workspace_id=? AND id=?",
                             (self.ws, dep_id)).fetchone()
        if dep is None:
            raise NotFound("dependency not found")
        task = self.task(dict(op, task_id=dep["task_id"]))
        if dep["status"] != "open":
            raise InvalidOperation("dependency is not open")
        status = op.get("resolution", "resolved")
        if status not in ("resolved", "cancelled"):
            raise InvalidOperation("resolution must be resolved or cancelled", field="resolution")
        if status == "resolved" and (evidence == "inferred" or not op.get("source_ref")):
            raise EvidenceRequired("resolving a dependency needs a non-inferred source_ref")
        self.c.execute("UPDATE dependencies SET status=?, resolved_source_ref=?, resolved_at=? WHERE id=?",
                       (status, op.get("source_ref"), self.now, dep_id))
        cancelled = self.c.execute(
            """UPDATE reminders SET status='cancelled', updated_at=? WHERE workspace_id=? AND status='scheduled'
               AND (dependency_id=? OR (task_id=? AND condition='if_still_waiting' AND NOT EXISTS (
                 SELECT 1 FROM dependencies d WHERE d.workspace_id=? AND d.task_id=? AND d.status='open')))""",
            (self.now, self.ws, dep_id, task["id"], self.ws, task["id"])).rowcount
        return {"task_id": task["id"], "dependency_id": dep_id, "reminders_cancelled": cancelled,
                "event_id": self.event(task["id"], "dependency_resolved", op, evidence,
                                       before={"status": "open"}, after={"dependency_id": dep_id, "status": status})}

    def op_set_reminder(self, op: dict, evidence: str) -> dict:
        task = self.task(op)
        if task["state"] in ("DONE", "CANCELLED"):
            raise InvalidOperation("closed tasks cannot get reminders")
        ws = self.s._workspace(self.c, self.ws)
        tz_name = op.get("timezone") or ws["timezone"]
        due_at = _local_to_utc(op.get("local_at"), tz_name)
        if due_at <= self.now:
            raise InvalidOperation("reminder must be in the future", field="local_at")
        dep_id = self.ref(op.get("dependency_id"))
        if dep_id is not None and self.c.execute(
                "SELECT 1 FROM dependencies WHERE workspace_id=? AND id=? AND task_id=? AND status='open'",
                (self.ws, dep_id, task["id"])).fetchone() is None:
            raise InvalidOperation("dependency_id must be an open dependency of this task")
        kind = op.get("kind", "followup")
        condition = op.get("condition", "if_still_waiting" if dep_id else "if_open")
        every, repeats = op.get("repeat_every_days"), op.get("max_repeats")
        if every is not None and (type(every) is not int or not 1 <= every <= 90
                                  or type(repeats) is not int or not 1 <= repeats <= 20):
            raise InvalidOperation("repeating follow-ups need repeat_every_days 1..90 and max_repeats 1..20")
        recipient = self.member(op.get("recipient_principal_id") or self.owner_id, "recipient_principal_id")
        if self.role_of(recipient) not in ("owner", "editor"):
            # Chasing an outsider is external communication: the owner must ask for it in a real message,
            # and it can only be about something that person asked for or is responsible for.
            self.owner_only("set a reminder that goes to someone outside the team", op)
            concerns = task["requester_id"] == recipient or self.c.execute(
                "SELECT 1 FROM dependencies WHERE workspace_id=? AND task_id=? AND responsible_principal_id=?",
                (self.ws, task["id"], recipient)).fetchone()
            if not concerns:
                raise Forbidden("an outside recipient can only be reminded about their own request or dependency")
        dedupe = op.get("dedupe_key") or f"{self.ws}:{task['id']}:{kind}:{due_at}:{dep_id or ''}:{recipient}"
        existing = self.c.execute("SELECT id,task_id,due_at FROM reminders WHERE dedupe_key=?", (dedupe,)).fetchone()
        if existing is not None:
            if (existing["task_id"], existing["due_at"]) != (task["id"], due_at):
                raise InvalidOperation("dedupe_key already used for another reminder")
            return {"task_id": task["id"], "reminder_id": existing["id"], "duplicate": True}
        reminder_id = self.s._new_id("rem")
        try:
            self.c.execute(
                """INSERT INTO reminders(id,workspace_id,task_id,dependency_id,due_at,timezone,kind,condition,
                     recipient_principal_id,dedupe_key,created_at,updated_at,repeat_every_days,repeats_left,series_id)
                   VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (reminder_id, self.ws, task["id"], dep_id, due_at, tz_name, kind, condition,
                 recipient, dedupe, self.now, self.now, every, (repeats - 1) if every else None,
                 reminder_id if every else None))
        except sqlite3.IntegrityError as exc:
            raise InvalidOperation(f"invalid reminder: {exc}") from exc
        self.bind(op, reminder_id)
        return {"task_id": task["id"], "reminder_id": reminder_id, "due_at": due_at,
                "event_id": self.event(task["id"], "reminder_set", op, evidence, after={
                    "reminder_id": reminder_id, "due_at": due_at, "local_at": op.get("local_at"),
                    "timezone": tz_name, "kind": kind, "condition": condition})}

    def op_cancel_reminder(self, op: dict, evidence: str) -> dict:
        rem_id = self.ref(op.get("reminder_id"))
        rem = self.c.execute("SELECT * FROM reminders WHERE workspace_id=? AND id=?", (self.ws, rem_id)).fetchone()
        if rem is None:
            raise NotFound("reminder not found")
        task = self.task(dict(op, task_id=rem["task_id"]))
        if rem["status"] != "scheduled":
            raise InvalidOperation(f"reminder is {rem['status']}, not scheduled")
        self.c.execute("UPDATE reminders SET status='cancelled', updated_at=? WHERE id=?", (self.now, rem_id))
        return {"task_id": task["id"], "reminder_id": rem_id,
                "event_id": self.event(task["id"], "reminder_cancelled", op, evidence, after={"reminder_id": rem_id})}

    def op_link_inbox(self, op: dict, evidence: str) -> dict:
        task = self.task(op, check_version=False)
        relation = op.get("relation", "context")
        try:
            self.c.execute("INSERT OR IGNORE INTO message_links(workspace_id,inbox_id,task_id,relation) VALUES(?,?,?,?)",
                           (self.ws, op.get("inbox_id"), task["id"], relation))
        except sqlite3.IntegrityError as exc:
            raise InvalidOperation("inbox message not found in this workspace or bad relation") from exc
        return {"task_id": task["id"], "event_id": self.event(
            task["id"], "source_linked", op, evidence, after={"inbox_id": op.get("inbox_id"), "relation": relation})}

    # ------------------------------------------------------- human in the loop
    def op_request_human_action(self, op: dict, evidence: str) -> dict:
        task = self.task(op, check_version=(self.role != "requester"))
        if self.role == "requester":
            assignee = self.owner_id
        else:
            assignee = op.get("assignee_principal_id") or self.owner_id
        kind = op.get("request_kind")
        if kind not in ("input", "decision", "review", "execution", "exception"):
            raise InvalidOperation("unsupported request_kind", field="request_kind")
        urgency = self.urgency(op)
        request_id = self.s._new_id("hum")
        try:
            self.c.execute(
                """INSERT INTO human_action_requests(id,workspace_id,task_id,requested_by_principal_id,
                     assignee_principal_id,request_kind,title,request_json,due_at,urgency,created_at,updated_at)
                   VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""",
                (request_id, self.ws, task["id"], self.actor.principal_id, assignee, kind,
                 _text(op.get("title"), "title", required=True), _dumps(_obj(op.get("request"), "request")),
                 _opt_utc(op.get("due_at"), "due_at"), urgency, self.now, self.now))
        except sqlite3.IntegrityError as exc:
            raise InvalidOperation("assignee must be an active human member") from exc
        self.bind(op, request_id)
        self.announce(task["id"], assignee, urgency, _text(op.get("title"), "title"), request_id)
        return {"task_id": task["id"], "human_action_id": request_id,
                "event_id": self.event(task["id"], "human_action_requested", op, evidence, after={
                    "human_action_id": request_id, "assignee": assignee, "kind": kind, "urgency": urgency})}

    def urgency(self, op: dict) -> str:
        urgency = op.get("urgency", "batch")
        if urgency not in ("batch", "notify", "interrupt"):
            raise InvalidOperation("urgency must be batch, notify or interrupt", field="urgency")
        # An outside requester's messages never interrupt the owner; they wait for the digest.
        return "batch" if self.role == "requester" else urgency

    def announce(self, task_id: str, assignee: str, urgency: str, title: str, request_id: str) -> None:
        if urgency not in ("notify", "interrupt"):
            return
        self.c.execute(
            """INSERT INTO delivery_outbox(id,workspace_id,task_id,recipient_principal_id,channel,payload_json,
                 dedupe_key,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?)""",
            (self.s._new_id("dlv-out"), self.ws, task_id, assignee, "telegram",
             _dumps({"text": ("【急】" if urgency == "interrupt" else "") + "需要你处理：" + title,
                     "human_action_id": request_id}), f"human:{request_id}", self.now, self.now))

    def op_respond_human_action(self, op: dict, evidence: str) -> dict:
        request_id = self.ref(op.get("human_action_id"))
        req = self.c.execute("SELECT * FROM human_action_requests WHERE workspace_id=? AND id=?",
                             (self.ws, request_id)).fetchone()
        if req is None or req["assignee_principal_id"] != self.actor.principal_id:
            raise NotFound("human action request not found")
        if req["status"] not in ("pending", "in_progress"):
            raise InvalidOperation(f"request is already {req['status']}")
        status = op.get("status", "completed")
        if status not in ("completed", "declined"):
            raise InvalidOperation("status must be completed or declined", field="status")
        if evidence not in STRONG_EVIDENCE or not op.get("source_ref"):
            raise EvidenceRequired("a human response needs explicit evidence and the source message ref")
        self.cites_message(op, "answering a human request", sender=self.actor.principal_id)
        self.c.execute(
            """UPDATE human_action_requests SET status=?, response_json=?, response_source_ref=?,
                 responded_at=?, updated_at=? WHERE id=?""",
            (status, _dumps(_obj(op.get("response"), "response")), op["source_ref"], self.now, self.now, request_id))
        # The response re-enters the work loop as a durable wake event in the same transaction.
        wake_id = self.s._new_id("wake")
        self.c.execute(
            """INSERT INTO wake_events(id,workspace_id,task_id,origin,origin_event_id,kind,source_ref,
                 created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?)""",
            (wake_id, self.ws, req["task_id"], "workbench.human_action", request_id,
             "human_action_completed", op["source_ref"], self.now, self.now))
        result = {"human_action_id": request_id, "wake_event_id": wake_id, "status": status}
        if req["task_id"]:
            task = self.task(dict(op, task_id=req["task_id"]), check_version=False)
            result["task_id"] = task["id"]
            result["event_id"] = self.event(task["id"], "human_action_responded", op, evidence,
                                            after={"human_action_id": request_id, "status": status})
        return result

    # --------------------------------------------------------- responsibility
    def op_assign_responsibility(self, op: dict, evidence: str) -> dict:
        self.owner_only("change responsibility or autonomy", op)
        self.strong(op, evidence, "changing responsibility or autonomy")
        task = self.task(op)
        level = op.get("autonomy_level")
        if level not in LEVELS:
            raise InvalidOperation("unsupported autonomy_level", field="autonomy_level", allowed=list(LEVELS))
        executor = self.member(op.get("executor_id"), "executor_id")
        work_class = _text(op.get("work_class"), "work_class", required=True)
        profile = self.c.execute(
            "SELECT * FROM delegation_profiles WHERE workspace_id=? AND work_class=? AND status='active'",
            (self.ws, work_class)).fetchone()
        if profile is None and level != "human_executes":
            raise InvalidOperation("no active owner-approved profile for this work class")

        def narrowed(key: str, profile_key: str) -> list[str]:
            if op.get(key) is not None:
                return _str_list(op.get(key), key)
            return sorted(json.loads(profile[profile_key])) if profile else []

        allowed = narrowed("allowed_actions", "allowed_actions_json")
        reversible = narrowed("reversible_actions", "reversible_actions_json")
        review = sorted(set(narrowed("review_actions", "review_actions_json"))
                        | (set(json.loads(profile["review_actions_json"])) if profile else set()))
        conditions = sorted(set(narrowed("approval_conditions", "mandatory_approval_conditions_json"))
                            | (set(json.loads(profile["mandatory_approval_conditions_json"])) if profile else set()))
        if profile is None and allowed:
            raise InvalidOperation("without a profile the task cannot have delegated actions")
        row = {
            "current_executor_id": executor, "profile_id": profile["id"] if profile else None,
            "work_class": work_class, "autonomy_level": level,
            "effective_allowed_actions_json": _dumps(allowed),
            "effective_reversible_actions_json": _dumps(reversible),
            "effective_review_actions_json": _dumps(review),
            "effective_approval_conditions_json": _dumps(conditions),
            "success_criteria_json": profile["success_criteria_json"] if profile else "[]",
            "evidence_requirements_json": profile["evidence_requirements_json"] if profile else "[]",
            "escalation_policy_json": profile["escalation_policy_json"] if profile else "{}",
            "profile_version": profile["version"] if profile else 1, "status": "active",
        }
        try:
            self.s._envelope(row | {"profile_version": row["profile_version"]}, profile)
        except ValueError as exc:
            raise InvalidOperation(str(exc)) from exc
        old = self.c.execute("SELECT * FROM task_responsibilities WHERE workspace_id=? AND task_id=?",
                             (self.ws, task["id"])).fetchone()
        event_type = self._responsibility_event_type(old, executor, level)
        if old is None:
            self.c.execute(
                f"""INSERT INTO task_responsibilities(workspace_id,task_id,{', '.join(row)},
                      updated_by_principal_id,updated_at) VALUES(?,?,{', '.join('?' * len(row))},?,?)""",
                (self.ws, task["id"], *row.values(), self.actor.principal_id, self.now))
        else:
            self.c.execute(
                f"""UPDATE task_responsibilities SET {', '.join(f'{k}=?' for k in row)}, version=version+1,
                      updated_by_principal_id=?, updated_at=? WHERE workspace_id=? AND task_id=?""",
                (*row.values(), self.actor.principal_id, self.now, self.ws, task["id"]))
        self._responsibility_event(task["id"], event_type, op, old, executor, level, profile)
        return {"task_id": task["id"], "responsibility_event": event_type,
                "event_id": self.event(task["id"], "responsibility_changed", op, evidence, after={
                    "executor_id": executor, "autonomy_level": level, "work_class": work_class,
                    "profile_version": row["profile_version"], "responsibility_event": event_type})}

    def op_set_responsibility_status(self, op: dict, evidence: str) -> dict:
        self.owner_only("pause, revoke or resume delegation", op)
        self.strong(op, evidence, "pausing, revoking or resuming delegation")
        task = self.task(op)
        status = op.get("status")
        if status not in ("active", "paused", "revoked"):
            raise InvalidOperation("status must be active, paused or revoked", field="status")
        old = self.c.execute("SELECT * FROM task_responsibilities WHERE workspace_id=? AND task_id=?",
                             (self.ws, task["id"])).fetchone()
        if old is None:
            raise InvalidOperation("task has no responsibility record")
        if old["status"] == status:
            return {"task_id": task["id"], "changed": False}
        if old["status"] == "revoked":
            raise InvalidOperation("revoked delegation must be re-assigned, not resumed")
        self.c.execute(
            """UPDATE task_responsibilities SET status=?, version=version+1, updated_by_principal_id=?,
                 updated_at=? WHERE workspace_id=? AND task_id=?""",
            (status, self.actor.principal_id, self.now, self.ws, task["id"]))
        event_type = {"paused": "override", "revoked": "revoked", "active": "agent_resumed"}[status]
        profile = self.s._profile(self.c, self.ws, old["profile_id"]) if old["profile_id"] else None
        self._responsibility_event(task["id"], event_type, op, old, old["current_executor_id"],
                                   old["autonomy_level"], profile)
        return {"task_id": task["id"], "responsibility_event": event_type,
                "event_id": self.event(task["id"], "responsibility_changed", op, evidence,
                                       before={"status": old["status"]}, after={"status": status})}

    def _responsibility_event_type(self, old, executor: str, level: str) -> str:
        if old is None:
            return "delegated" if self._is_agent(executor) else "assigned"
        old_rank, new_rank = LEVELS.index(old["autonomy_level"]), LEVELS.index(level)
        if old["current_executor_id"] != executor:
            if self._is_agent(old["current_executor_id"]) and not self._is_agent(executor):
                return "human_takeover"
            return "delegated" if self._is_agent(executor) else "assigned"
        if new_rank > old_rank:
            return "promoted"
        if new_rank < old_rank:
            return "demoted"
        return "assigned"

    def _is_agent(self, principal_id: str) -> bool:
        row = self.c.execute("SELECT kind FROM principals WHERE id=?", (principal_id,)).fetchone()
        return row is not None and row["kind"] == "agent"

    def _responsibility_event(self, task_id, event_type, op, old, executor, level, profile) -> None:
        self.c.execute(
            """INSERT INTO responsibility_events(id,workspace_id,task_id,event_type,from_actor_id,to_actor_id,
                 from_autonomy_level,to_autonomy_level,profile_id,profile_version,actor_id,reason,evidence_json,
                 source_ref,occurred_at,recorded_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (self.s._new_id("resp"), self.ws, task_id, event_type,
             old["current_executor_id"] if old else None, executor,
             old["autonomy_level"] if old else None, level,
             profile["id"] if profile else None, profile["version"] if profile else None,
             self.actor.principal_id, _text(op.get("reason"), "reason", required=True),
             _dumps(_obj(op.get("evidence"), "evidence")), op.get("source_ref"),
             _opt_utc(op.get("occurred_at"), "occurred_at") or self.now, self.now))
