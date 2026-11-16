"""Negative SQL invariants: event/approval/delivery links cannot cross workspaces."""
import sqlite3
import tempfile
import unittest
from pathlib import Path

SCHEMA = Path(__file__).resolve().parents[1] / "db" / "schema.sql"
NOW = "2026-11-16T15:00:00Z"


class CrossWorkspaceTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / "workbench.sqlite"
        self.c = sqlite3.connect(self.path)
        self.addCleanup(self.c.close)
        self.c.execute("PRAGMA foreign_keys=ON")
        self.c.executescript(SCHEMA.read_text(encoding="utf-8"))
        self.c.execute(
            """INSERT INTO principals(id,display_name,kind,created_at)
               VALUES('owner','Owner','human',?)""", (NOW,)
        )
        self.c.executemany(
            """INSERT INTO workspaces(id,title,owner_id,created_at)
               VALUES(?,?,'owner',?)""",
            [("w1", "One", NOW), ("w2", "Two", NOW)],
        )
        self.c.executemany(
            """INSERT INTO tasks(id,workspace_id,title,created_at,updated_at)
               VALUES(?,?,?, ?,?)""",
            [("t1", "w1", "One", NOW, NOW), ("t2", "w2", "Two", NOW, NOW)],
        )
        self.c.executemany(
            """INSERT INTO inbox_messages
               (id,workspace_id,platform,bot_id,update_id,chat_id,
                message_id,raw_source_ref,received_at,updated_at)
               VALUES(?,?,'telegram','bot',?,'chat',?, 'ref',?,?)""",
            [("m1", "w1", "1", "1", NOW, NOW),
             ("m2", "w2", "2", "2", NOW, NOW)],
        )
        self.c.execute(
            """INSERT INTO task_events
               (id,workspace_id,task_id,actor_id,kind,evidence_type,occurred_at,recorded_at)
               VALUES('ev1','w1','t1','owner','updated','explicit_user',?,?)""",
            (NOW, NOW),
        )
        self.c.execute(
            """INSERT INTO dependencies
               (id,workspace_id,task_id,kind,awaited_outcome,created_at)
               VALUES('d1','w1','t1','external_response','reply',?)""",
            (NOW,),
        )
        self.c.execute(
            """INSERT INTO reminders
               (id,workspace_id,task_id,due_at,timezone,kind,recipient_principal_id,
                dedupe_key,created_at,updated_at)
               VALUES('r1','w1','t1',?,'UTC','followup','owner','dedupe-r1',?,?)""",
            (NOW, NOW, NOW),
        )
        self.c.commit()

    def test_valid_links_are_permitted(self):
        self.c.execute(
            """INSERT INTO message_links(workspace_id,inbox_id,task_id,relation)
               VALUES('w1','m1','t1','request')"""
        )
        self.c.execute(
            """INSERT INTO memory_outbox(id,task_event_id,workspace_id,scope,
                                        source_ref,created_at,updated_at)
               VALUES('mem1','ev1','w1','user:owner','ref',?,?)""", (NOW, NOW),
        )
        self.c.execute(
            """INSERT INTO delivery_outbox(id,workspace_id,task_id,reminder_id,
                  recipient_principal_id,channel,payload_json,dedupe_key,created_at,updated_at)
               VALUES('del1','w1','t1','r1','owner','telegram','{}','del-dedupe',?,?)""",
            (NOW, NOW),
        )

    def test_message_link_cannot_mix_workspaces(self):
        with self.assertRaises(sqlite3.IntegrityError):
            self.c.execute(
                """INSERT INTO message_links(workspace_id,inbox_id,task_id,relation)
                   VALUES('w1','m1','t2','request')"""
            )

    def test_message_link_cannot_steal_foreign_message(self):
        with self.assertRaises(sqlite3.IntegrityError):
            self.c.execute(
                """INSERT INTO message_links(workspace_id,inbox_id,task_id,relation)
                   VALUES('w1','m2','t1','request')"""
            )

    def test_reminder_cannot_point_to_foreign_dependency(self):
        with self.assertRaises(sqlite3.IntegrityError):
            self.c.execute(
                """INSERT INTO reminders(
                    id,workspace_id,task_id,dependency_id,due_at,timezone,kind,
                    recipient_principal_id,dedupe_key,created_at,updated_at)
                   VALUES('r2','w2','t2','d1',?,'UTC','followup','owner','dedupe-r2',?,?)""",
                (NOW, NOW, NOW),
            )

    def test_memory_outbox_cannot_link_foreign_event(self):
        with self.assertRaises(sqlite3.IntegrityError):
            self.c.execute(
                """INSERT INTO memory_outbox(id,task_event_id,workspace_id,scope,
                                            source_ref,created_at,updated_at)
                   VALUES('mem2','ev1','w2','user:owner','ref',?,?)""", (NOW, NOW),
            )

    def test_delivery_outbox_cannot_link_foreign_task_or_reminder(self):
        with self.assertRaises(sqlite3.IntegrityError):
            self.c.execute(
                """INSERT INTO delivery_outbox(id,workspace_id,task_id,
                       recipient_principal_id,channel,payload_json,dedupe_key,
                       created_at,updated_at)
                   VALUES('del2','w1','t2','owner','telegram','{}','dedupe-del2',?,?)""",
                (NOW, NOW),
            )
        with self.assertRaises(sqlite3.IntegrityError):
            self.c.execute(
                """INSERT INTO delivery_outbox(id,workspace_id,reminder_id,
                       recipient_principal_id,channel,payload_json,dedupe_key,
                       created_at,updated_at)
                   VALUES('del3','w2','r1','owner','telegram','{}','dedupe-del3',?,?)""",
                (NOW, NOW),
            )


if __name__ == "__main__":
    unittest.main()
