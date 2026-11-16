"""Wake inbox contracts with synthetic SQL and no external Agent Host."""
from __future__ import annotations

import sqlite3
import tempfile
import unittest
from pathlib import Path

from workdesk.runtime_bridge import (
    EventConflict,
    EventTransitionError,
    HostOutcome,
    WakeEvent,
    WakeEventStore,
)

SCHEMA = Path(__file__).resolve().parents[1] / "db" / "schema.sql"
NOW = "2026-11-16T15:00:00Z"
LATER = "2026-11-16T15:05:00Z"


class WakeEventTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / "events.sqlite"
        conn = sqlite3.connect(self.path)
        conn.execute("PRAGMA foreign_keys=ON")
        conn.executescript(SCHEMA.read_text(encoding="utf-8"))
        conn.execute(
            """INSERT INTO principals(id,display_name,kind,created_at)
               VALUES('owner','Owner','human',?)""", (NOW,)
        )
        conn.executemany(
            """INSERT INTO workspaces(id,title,owner_id,created_at)
               VALUES(?,?,'owner',?)""", [("w1", "One", NOW), ("w2", "Two", NOW)]
        )
        conn.commit()
        conn.close()
        self.store = WakeEventStore(self.path)
        self.event = WakeEvent(
            workspace_id="w1", event_id="wake-1",
            origin="telegram", origin_event_id="bot/update/44",
            kind="user_message", source_ref="telegram://msg/10",
            created_at=NOW,
        )

    def test_exact_replay_is_idempotent_and_conflict_fails(self):
        self.assertTrue(self.store.enqueue(self.event))
        self.assertFalse(self.store.enqueue(self.event))
        other = WakeEvent(
            workspace_id="w1", event_id="wake-2",
            origin="telegram", origin_event_id="bot/update/44",
            kind="user_message", source_ref="telegram://msg/changed",
            created_at=NOW,
        )
        with self.assertRaises(EventConflict):
            self.store.enqueue(other)

    def test_claim_is_single_use_until_host_ack(self):
        self.store.enqueue(self.event)
        claimed = self.store.claim(workspace_id="w1", now=NOW, lease_until=LATER)
        self.assertEqual(claimed, self.event)
        self.assertIsNone(self.store.claim(workspace_id="w1", now=NOW, lease_until=LATER))
        self.store.settle(
            workspace_id="w1", event_id=self.event.event_id,
            outcome=HostOutcome("completed", "run-1", "session-1"), now=NOW,
        )
        self.store.settle(
            workspace_id="w1", event_id=self.event.event_id,
            outcome=HostOutcome("completed", "run-1", "session-1"), now=NOW,
        )
        with self.assertRaises(EventTransitionError):
            self.store.settle(
                workspace_id="w1", event_id=self.event.event_id,
                outcome=HostOutcome("failed", "run-2"), now=NOW,
            )

    def test_expired_claim_is_unknown_not_automatically_replayed(self):
        self.store.enqueue(self.event)
        self.store.claim(workspace_id="w1", now=NOW, lease_until=LATER)
        self.assertEqual(self.store.expire_ambiguous(workspace_id="w1", now=LATER), 1)
        self.assertIsNone(self.store.claim(workspace_id="w1", now=LATER,
                                           lease_until="2026-11-16T15:10:00Z"))

    def test_host_defer_needs_session_and_deferred_reference(self):
        with self.assertRaises(ValueError):
            HostOutcome("deferred", "run-1", "session-1")
        self.store.enqueue(self.event)
        self.store.claim(workspace_id="w1", now=NOW, lease_until=LATER)
        self.store.settle(
            workspace_id="w1", event_id="wake-1",
            outcome=HostOutcome("deferred", "run-1", "session-1", "deferred-1"),
            now=NOW,
        )
        with sqlite3.connect(self.path) as conn:
            actual = conn.execute(
                "SELECT status,host_session_ref,deferred_ref FROM wake_events"
            ).fetchone()
        self.assertEqual(actual, ("deferred", "session-1", "deferred-1"))

    def test_cross_workspace_task_link_is_rejected_by_sql(self):
        with sqlite3.connect(self.path) as conn:
            conn.execute("PRAGMA foreign_keys=ON")
            conn.execute(
                """INSERT INTO tasks(id,workspace_id,title,created_at,updated_at)
                   VALUES('task-a','w1','A',?,?)""", (NOW, NOW),
            )
            conn.commit()
            foreign = WakeEvent(
                workspace_id="w2", event_id="wake-2", origin="timer",
                origin_event_id="timer-1", kind="timer_fired",
                source_ref="timer://timer-1", created_at=NOW,
                task_id="task-a",
            )
            with self.assertRaises(sqlite3.IntegrityError):
                self.store.enqueue(foreign)

    def test_invalid_date_or_kind_is_rejected(self):
        with self.assertRaises(ValueError):
            WakeEvent("w1", "wake-3", "timer", "timer-3", "RunNow",
                      "timer://3", NOW)
        with self.assertRaises(ValueError):
            self.store.claim(workspace_id="w1", now=LATER, lease_until=NOW)

    def test_second_workspace_cannot_claim_first_workspace_event(self):
        self.store.enqueue(self.event)
        self.assertIsNone(
            self.store.claim(workspace_id="w2", now=NOW, lease_until=LATER)
        )
        self.assertEqual(
            self.store.claim(workspace_id="w1", now=NOW, lease_until=LATER), self.event
        )


if __name__ == "__main__":
    unittest.main()
