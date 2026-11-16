"""Durable WorkBench wake-event ledger and minimal Agent Host handoff contract.

Business events are WorkBench's authority. Agent Host owns Sessions/Runs and
resumable tool calls. No LLM, network, credentials or MR-Mem dependency here.
Callers MUST authenticate the event origin and authorize its workspace before
using this internal storage component.
"""
from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Protocol


class EventConflict(RuntimeError):
    """An event id was replayed with different immutable source metadata."""


class EventTransitionError(RuntimeError):
    """The event is not in a state that permits this transition."""


def _required(value: str, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field} is required")
    return value


def _utc(value: str) -> str:
    _required(value, "UTC time")
    if not value.endswith("Z"):
        raise ValueError("timestamps must be canonical UTC with Z suffix")
    try:
        datetime.fromisoformat(value[:-1] + "+00:00")
    except ValueError as exc:
        raise ValueError("invalid UTC timestamp") from exc
    return value


KINDS = frozenset({
    "user_message", "timer_fired", "external_change", "approval_decided",
    "agent_handoff",
})
OUTCOMES = frozenset({"completed", "deferred", "failed"})


@dataclass(frozen=True, slots=True)
class WakeEvent:
    workspace_id: str
    event_id: str
    origin: str
    origin_event_id: str
    kind: str
    source_ref: str
    created_at: str
    task_id: str | None = None

    def __post_init__(self) -> None:
        for key in ("workspace_id", "event_id", "origin", "origin_event_id",
                    "source_ref"):
            _required(getattr(self, key), key)
        if self.task_id is not None:
            _required(self.task_id, "task_id")
        if self.kind not in KINDS:
            raise ValueError("unsupported wake event kind")
        _utc(self.created_at)


@dataclass(frozen=True, slots=True)
class HostOutcome:
    """External Host acknowledgement; never a task-completion proof by itself."""

    status: str
    run_ref: str
    session_ref: str | None = None
    deferred_ref: str | None = None

    def __post_init__(self) -> None:
        if self.status not in OUTCOMES:
            raise ValueError("unsupported run outcome")
        _required(self.run_ref, "run_ref")
        if self.session_ref is not None:
            _required(self.session_ref, "session_ref")
        if self.deferred_ref is not None:
            _required(self.deferred_ref, "deferred_ref")
        if self.status == "deferred" and not (self.session_ref and self.deferred_ref):
            raise ValueError("deferred outcome requires session and deferred references")
        if self.status != "deferred" and self.deferred_ref is not None:
            raise ValueError("deferred_ref is only legal for deferred outcomes")


class AgentHostRunPort(Protocol):
    """Adapter supplied by Hermes or another Agent Host.

    The Host MUST accept a stable event ID idempotently, preserve its own
    Run/Session state and recheck tool permissions when resuming.
    """

    def submit_event(self, event: WakeEvent) -> HostOutcome: ...


class WakeEventStore:
    """SQL inbox state machine; no direct calls to Agent Host or third parties.

    An expired lease becomes UNKNOWN rather than automatically starting a
    second Host Run. An operator/Host reconciler must establish the outcome.
    """

    def __init__(self, database: str | Path):
        self._database = str(database)

    @contextmanager
    def _transaction(self):
        conn = sqlite3.connect(self._database, timeout=5, isolation_level=None)
        try:
            conn.execute("PRAGMA foreign_keys=ON")
            conn.execute("BEGIN IMMEDIATE")
            yield conn
            conn.execute("COMMIT")
        except BaseException:
            if conn.in_transaction:
                conn.execute("ROLLBACK")
            raise
        finally:
            conn.close()

    def enqueue(self, event: WakeEvent) -> bool:
        """Durably insert a new event; exact replays return False."""
        with self._transaction() as conn:
            row = conn.execute(
                """SELECT workspace_id, origin, origin_event_id, kind, source_ref, task_id
                   FROM wake_events WHERE workspace_id=? AND origin=? AND origin_event_id=?""",
                (event.workspace_id, event.origin, event.origin_event_id),
            ).fetchone()
            expected = (
                event.workspace_id, event.origin, event.origin_event_id,
                event.kind, event.source_ref, event.task_id,
            )
            if row is not None:
                if tuple(row) != expected:
                    raise EventConflict("source event reused with conflicting metadata")
                return False
            conn.execute(
                """INSERT INTO wake_events(
                     id, workspace_id, task_id, origin, origin_event_id, kind,
                     source_ref, created_at, updated_at
                   ) VALUES(?,?,?,?,?,?,?,?,?)""",
                (
                    event.event_id, event.workspace_id, event.task_id, event.origin,
                    event.origin_event_id, event.kind, event.source_ref,
                    event.created_at, event.created_at,
                ),
            )
            return True

    def claim(self, *, workspace_id: str, now: str, lease_until: str) -> WakeEvent | None:
        """Single-writer transactional claim of a never-dispatched pending event."""
        _required(workspace_id, "workspace_id")
        _utc(now)
        _utc(lease_until)
        if datetime.fromisoformat(lease_until[:-1]) <= datetime.fromisoformat(now[:-1]):
            raise ValueError("lease must expire after now")
        with self._transaction() as conn:
            row = conn.execute(
                """SELECT workspace_id,id,origin,origin_event_id,kind,source_ref,
                          created_at,task_id
                   FROM wake_events
                   WHERE workspace_id=? AND status='pending'
                   ORDER BY created_at,id LIMIT 1""",
                (workspace_id,),
            ).fetchone()
            if row is None:
                return None
            conn.execute(
                """UPDATE wake_events SET status='leased', attempts=attempts+1,
                   lease_until=?,updated_at=? WHERE id=? AND status='pending'""",
                (lease_until, now, row[1]),
            )
            return WakeEvent(*row)

    def settle(
        self, *, workspace_id: str, event_id: str, outcome: HostOutcome, now: str
    ) -> None:
        """Store Host acknowledgement; does not mutate task DONE or invoke tools."""
        _required(workspace_id, "workspace_id")
        _required(event_id, "event_id")
        _utc(now)
        with self._transaction() as conn:
            row = conn.execute(
                """SELECT status,host_run_ref,host_session_ref,deferred_ref
                   FROM wake_events WHERE workspace_id=? AND id=?""",
                (workspace_id, event_id),
            ).fetchone()
            if row is None:
                raise EventTransitionError("unknown event")
            intended = (
                outcome.status, outcome.run_ref, outcome.session_ref, outcome.deferred_ref
            )
            if tuple(row) == intended:
                return
            if row[0] != "leased":
                raise EventTransitionError("event is not currently leased")
            conn.execute(
                """UPDATE wake_events SET status=?, host_run_ref=?,host_session_ref=?,
                   deferred_ref=?,lease_until=NULL,updated_at=?
                   WHERE workspace_id=? AND id=?""",
                (*intended, now, workspace_id, event_id),
            )

    def expire_ambiguous(self, *, workspace_id: str, now: str) -> int:
        """Mark expired leases unknown; never blindly reissue a tool/run."""
        _required(workspace_id, "workspace_id")
        _utc(now)
        with self._transaction() as conn:
            result = conn.execute(
                """UPDATE wake_events SET status='unknown',lease_until=NULL,
                   updated_at=? WHERE workspace_id=? AND status='leased'
                   AND lease_until<=?""",
                (now, workspace_id, now),
            )
            return result.rowcount
