"""Schema integrity smoke tests; only synthetic data, no credentials."""
from pathlib import Path
import sqlite3
import unittest


SCHEMA = Path(__file__).resolve().parents[1] / "db" / "schema.sql"


class SchemaTests(unittest.TestCase):
    def setUp(self):
        self.c = sqlite3.connect(":memory:")
        self.c.execute("PRAGMA foreign_keys=ON")
        self.c.executescript(SCHEMA.read_text(encoding="utf-8"))
        self.c.execute(
            "INSERT INTO principals(id,display_name,kind,created_at) "
            "VALUES('owner','Owner','human','2026-11-16T00:00:00Z')"
        )
        self.c.executemany(
            "INSERT INTO workspaces(id,title,owner_id,created_at) VALUES(?,?,?,?)",
            [
                ("a", "A", "owner", "2026-11-16T00:00:00Z"),
                ("b", "B", "owner", "2026-11-16T00:00:00Z"),
            ],
        )

    def tearDown(self):
        self.c.close()

    def test_bootstraps_all_domain_tables(self):
        names = {r[0] for r in self.c.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        )}
        self.assertTrue({
            "principals", "external_identities", "workspaces", "memberships",
            "projects", "tasks", "task_events", "task_focus", "dependencies",
            "reminders", "inbox_messages", "message_links", "command_receipts",
            "memory_outbox", "delivery_outbox", "delegation_profiles",
            "task_responsibilities", "responsibility_events", "human_action_requests",
        } <= names)

    def test_task_cannot_reference_foreign_workspace_project(self):
        now = "2026-11-16T00:00:00Z"
        self.c.execute(
            "INSERT INTO projects(id,workspace_id,title,created_at,updated_at) "
            "VALUES('project-a','a','A',?,?)", (now, now)
        )
        with self.assertRaises(sqlite3.IntegrityError):
            self.c.execute(
                "INSERT INTO tasks(id,workspace_id,project_id,title,created_at,updated_at) "
                "VALUES('task-b','b','project-a','cross-scope',?,?)", (now, now)
            )

    def test_inbox_and_outbox_have_dedupe_constraints(self):
        now = "2026-11-16T00:00:00Z"
        insert = (
            "INSERT INTO inbox_messages(id,workspace_id,platform,bot_id,update_id,"
            "chat_id,message_id,raw_source_ref,received_at,updated_at) "
            "VALUES(?,?,?,?,?,?,?,?,?,?)"
        )
        self.c.execute(insert, ("msg-1","a","telegram","bot","update","chat","m","ref",now,now))
        with self.assertRaises(sqlite3.IntegrityError):
            self.c.execute(insert, ("msg-2","a","telegram","bot","update","chat","m2","ref2",now,now))


if __name__ == "__main__":
    unittest.main()
