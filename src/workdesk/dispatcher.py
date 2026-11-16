"""Wake-event dispatch to the Agent Host, one active Run per workspace lane.

The inbox (wake_events) is the record of pending work, not the worker. A Run
whose acknowledgement is lost becomes `unknown` and is reconciled by asking the
Host about that event id; it is never blindly re-submitted.
"""
from __future__ import annotations

from datetime import datetime, timedelta
from pathlib import Path
from typing import Callable, Protocol

from .runtime_bridge import HostOutcome, WakeEvent
from .storage import utc_now, write_transaction


class HostPort(Protocol):
    def submit_event(self, event: WakeEvent) -> HostOutcome: ...
    def lookup_event(self, event_id: str) -> HostOutcome | None: ...


def _plus(now: str, seconds: int) -> str:
    return (datetime.fromisoformat(now[:-1]) + timedelta(seconds=seconds)).strftime("%Y-%m-%dT%H:%M:%SZ")


class Dispatcher:
    def __init__(self, database: str | Path, host: HostPort, *, clock: Callable[[], str] = utc_now):
        self._db, self._host, self._clock = str(database), host, clock

    def claim(self, workspace_id: str, *, lease_seconds: int = 600) -> WakeEvent | None:
        """Lease the oldest pending event unless this workspace already has a live Run."""
        now = self._clock()
        with write_transaction(self._db) as conn:
            conn.execute("""UPDATE wake_events SET status='unknown', lease_until=NULL, updated_at=?
                            WHERE workspace_id=? AND status='leased' AND lease_until<=?""",
                         (now, workspace_id, now))
            # One live background Run per workspace. Chat turns answered inline run in their own
            # per-chat lane on the Host and do not hold this lane.
            if conn.execute("""SELECT 1 FROM wake_events WHERE workspace_id=? AND status='leased'
                               AND COALESCE(host_run_ref,'')!='inline'""",
                            (workspace_id,)).fetchone():
                return None
            for row in conn.execute(
                    """SELECT workspace_id,id,origin,origin_event_id,kind,source_ref,created_at,task_id
                       FROM wake_events WHERE workspace_id=? AND status='pending'
                       ORDER BY created_at,id LIMIT 20""", (workspace_id,)).fetchall():
                try:
                    event = WakeEvent(*tuple(row))
                except ValueError:  # a malformed row must not block the whole inbox
                    conn.execute("UPDATE wake_events SET status='failed', updated_at=? WHERE id=?", (now, row[1]))
                    continue
                conn.execute("""UPDATE wake_events SET status='leased', attempts=attempts+1, lease_until=?, updated_at=?
                                WHERE id=? AND status='pending'""", (_plus(now, lease_seconds), now, row[1]))
                return event
            return None

    def settle(self, workspace_id: str, event_id: str, outcome: HostOutcome) -> None:
        now = self._clock()
        with write_transaction(self._db) as conn:
            conn.execute(
                """UPDATE wake_events SET status=?, host_run_ref=?, host_session_ref=?, deferred_ref=?,
                     lease_until=NULL, updated_at=? WHERE workspace_id=? AND id=? AND status IN ('leased','unknown')""",
                (outcome.status, outcome.run_ref, outcome.session_ref, outcome.deferred_ref, now,
                 workspace_id, event_id))

    def dispatch_once(self, workspace_id: str, *, lease_seconds: int = 600) -> dict:
        event = self.claim(workspace_id, lease_seconds=lease_seconds)
        if event is None:
            return {"dispatched": False}
        try:
            outcome = self._host.submit_event(event)
        except Exception as exc:  # noqa: BLE001 - lease expiry turns this into `unknown`
            return {"dispatched": True, "event_id": event.event_id, "acknowledged": False, "error": str(exc)[:200]}
        self.settle(workspace_id, event.event_id, outcome)
        return {"dispatched": True, "event_id": event.event_id, "acknowledged": True, "status": outcome.status}

    def unknown(self, workspace_id: str) -> list[dict]:
        """Events whose outcome nobody confirmed; shown to the owner and re-settled from the Host ledger."""
        with write_transaction(self._db) as conn:
            return [dict(r) for r in conn.execute(
                """SELECT id,kind,task_id,origin,source_ref,attempts,updated_at FROM wake_events
                   WHERE workspace_id=? AND status='unknown' ORDER BY updated_at""", (workspace_id,))]

    def reconcile_unknown(self, workspace_id: str) -> dict:
        """Ask the Host what happened to each unknown event; unresolved ones stay unknown for a human."""
        with write_transaction(self._db) as conn:
            ids = [r[0] for r in conn.execute(
                "SELECT id FROM wake_events WHERE workspace_id=? AND status='unknown' ORDER BY created_at",
                (workspace_id,))]
        settled, unresolved = [], []
        for event_id in ids:
            outcome = self._host.lookup_event(event_id)
            if outcome is None:
                unresolved.append(event_id)
            else:
                self.settle(workspace_id, event_id, outcome)
                settled.append(event_id)
        return {"settled": settled, "unresolved": unresolved}
