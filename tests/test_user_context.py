"""Fail-closed SQL grants: no implicit owner access or cross-user leakage."""
from pathlib import Path
import sqlite3
import tempfile
import unittest

from workdesk.user_context import SqlMemoryGrantChecker


SCHEMA = Path(__file__).resolve().parents[1] / "db" / "schema.sql"
NOW = "2026-11-16T00:00:00Z"


class GrantTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / "workbench.sqlite"
        self.c = sqlite3.connect(self.path)
        self.addCleanup(self.c.close)
        self.c.execute("PRAGMA foreign_keys=ON")
        self.c.executescript(SCHEMA.read_text(encoding="utf-8"))
        self.c.executemany(
            "INSERT INTO principals(id,display_name,kind,created_at) VALUES(?,?,?,?)",
            [("owner","Owner","human",NOW),("requester","External","human",NOW)]
        )
        self.c.execute(
            "INSERT INTO workspaces(id,title,owner_id,created_at) VALUES(?,?,?,?)",
            ("personal","Personal","owner",NOW)
        )
        self.c.executemany(
            "INSERT INTO memberships(workspace_id,principal_id,role) VALUES(?,?,?)",
            [("personal","owner","owner"),("personal","requester","requester")]
        )
        self.c.commit()
        self.checker = SqlMemoryGrantChecker(self.path)

    def allow(self, principal, target="owner", action="memory.read"):
        self.c.execute(
            "INSERT INTO memory_context_grants("
            "workspace_id,actor_principal_id,memory_user_id,capability,"
            "granted_by_principal_id,grant_source_ref,created_at) "
            "VALUES(?,?,?,?,?,?,?)",
            ("personal",principal,target,action,"owner","admin://grant/1",NOW),
        )
        self.c.commit()

    def test_no_implicit_right_even_for_workspace_owner(self):
        with self.assertRaises(PermissionError):
            self.checker.assert_allowed(
                principal_id="owner",workspace_id="personal",
                user_id="owner",action="memory.read"
            )

    def test_requester_cannot_read_owner_scope(self):
        self.allow("owner")
        with self.assertRaises(PermissionError):
            self.checker.assert_allowed(
                principal_id="requester",workspace_id="personal",
                user_id="owner",action="memory.read"
            )

    def test_exact_grant_and_revocation(self):
        self.allow("owner")
        self.checker.assert_allowed(
            principal_id="owner",workspace_id="personal",
            user_id="owner",action="memory.read"
        )
        with self.assertRaises(PermissionError):
            self.checker.assert_allowed(
                principal_id="owner",workspace_id="personal",
                user_id="owner",action="memory.write"
            )
        self.c.execute(
            "UPDATE memory_context_grants SET revoked_at=? WHERE actor_principal_id='owner'",
            (NOW,)
        )
        self.c.commit()
        with self.assertRaises(PermissionError):
            self.checker.assert_allowed(
                principal_id="owner",workspace_id="personal",
                user_id="owner",action="memory.read"
            )

    def test_inactive_membership_blocks_previous_grant(self):
        self.allow("owner")
        self.c.execute(
            "UPDATE memberships SET active=0 WHERE principal_id='owner'"
        )
        self.c.commit()
        with self.assertRaises(PermissionError):
            self.checker.assert_allowed(
                principal_id="owner",workspace_id="personal",
                user_id="owner",action="memory.read"
            )


if __name__ == "__main__":
    unittest.main()
