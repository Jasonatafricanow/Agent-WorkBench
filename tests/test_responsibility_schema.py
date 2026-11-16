"""SQL constraints for responsibility transfer and human participation."""
import sqlite3
import tempfile
import unittest
from pathlib import Path

SCHEMA = Path(__file__).resolve().parents[1] / "db" / "schema.sql"
NOW = "2026-11-16T00:00:00Z"


class ResponsibilitySchemaTests(unittest.TestCase):
    def setUp(self):
        self.c = sqlite3.connect(":memory:")
        self.addCleanup(self.c.close)
        self.c.execute("PRAGMA foreign_keys=ON")
        self.c.executescript(SCHEMA.read_text(encoding="utf-8"))
        self.c.execute(
            "INSERT INTO principals(id,display_name,kind,created_at) VALUES('owner','Owner','human',?)",
            (NOW,),
        )
        self.c.execute(
            "INSERT INTO principals(id,display_name,kind,created_at) VALUES('agent','Agent','agent',?)",
            (NOW,),
        )
        self.c.executemany(
            "INSERT INTO workspaces(id,title,owner_id,created_at) VALUES(?,?,?,?)",
            [("a", "A", "owner", NOW), ("b", "B", "owner", NOW)],
        )
        self.c.executemany(
            "INSERT INTO memberships(workspace_id,principal_id,role) VALUES(?,?,?)",
            [("a", "owner", "owner"), ("a", "agent", "editor"),
             ("b", "owner", "owner")],
        )
        self.c.execute(
            "INSERT INTO tasks(id,workspace_id,title,assignee_id,created_at,updated_at) "
            "VALUES('task-a','a','Report','owner',?,?)", (NOW, NOW),
        )
        self.c.execute(
            """INSERT INTO delegation_profiles(
                 id,workspace_id,work_class,version,max_autonomy_level,
                 allowed_actions_json,reversible_actions_json,
                 mandatory_approval_conditions_json,success_criteria_json,
                 evidence_requirements_json,escalation_policy_json,promotion_policy_json,
                 created_by_principal_id,approved_by_principal_id,created_at,updated_at
               ) VALUES('profile-a','a','routine_reporting',1,'agent_bounded',
                 '["read_data","draft_report"]','["read_data","draft_report"]',
                 '["external_commitment"]','["recipient_acceptance"]',
                 '["source_ref","artifact_receipt"]','{}','{}','owner','owner',?,?)""",
            (NOW, NOW),
        )

    def test_task_responsibility_references_same_workspace_profile_and_task(self):
        self.c.execute(
            """INSERT INTO task_responsibilities(
                 workspace_id,task_id,current_executor_id,profile_id,work_class,
                 autonomy_level,effective_allowed_actions_json,
                 effective_reversible_actions_json,effective_approval_conditions_json,
                 updated_by_principal_id,updated_at
               ) VALUES('a','task-a','agent','profile-a','routine_reporting',
                 'agent_bounded','["read_data"]','["read_data"]',
                 '["external_commitment"]','owner',?)""", (NOW,),
        )
        with self.assertRaises(sqlite3.IntegrityError):
            self.c.execute(
                """INSERT INTO task_responsibilities(
                     workspace_id,task_id,current_executor_id,profile_id,work_class,
                     autonomy_level,updated_by_principal_id,updated_at
                   ) VALUES('b','task-a','agent','profile-a','routine_reporting',
                     'agent_bounded','owner',?)""", (NOW,),
            )

    def test_task_responsibility_pins_exact_profile_version(self):
        with self.assertRaises(sqlite3.IntegrityError):
            self.c.execute(
                """INSERT INTO task_responsibilities(
                     workspace_id,task_id,current_executor_id,profile_id,work_class,
                     autonomy_level,profile_version,updated_by_principal_id,updated_at
                   ) VALUES('a','task-a','agent','profile-a','routine_reporting',
                     'agent_bounded',2,'owner',?)""", (NOW,),
            )

    def test_only_one_active_profile_per_work_class(self):
        with self.assertRaises(sqlite3.IntegrityError):
            self.c.execute(
                """INSERT INTO delegation_profiles(
                     id,workspace_id,work_class,version,max_autonomy_level,
                     created_by_principal_id,approved_by_principal_id,created_at,updated_at
                   ) VALUES('profile-a2','a','routine_reporting',2,'agent_prepares',
                     'owner','owner',?,?)""", (NOW, NOW),
            )

    def test_responsibility_events_are_append_only(self):
        self.c.execute(
            """INSERT INTO responsibility_events(
                 id,workspace_id,task_id,event_type,from_actor_id,to_actor_id,
                 from_autonomy_level,to_autonomy_level,actor_id,reason,evidence_json,
                 occurred_at,recorded_at
               ) VALUES('re-1','a','task-a','delegated','owner','agent',
                 'human_executes','agent_prepares','owner','approved delegation','{}',?,?)""",
            (NOW, NOW),
        )
        with self.assertRaises(sqlite3.IntegrityError):
            self.c.execute("UPDATE responsibility_events SET reason='rewritten' WHERE id='re-1'")
        with self.assertRaises(sqlite3.IntegrityError):
            self.c.execute("DELETE FROM responsibility_events WHERE id='re-1'")

    def test_human_action_request_is_workspace_scoped_and_durable(self):
        self.c.execute(
            """INSERT INTO human_action_requests(
                 id,workspace_id,task_id,requested_by_principal_id,assignee_principal_id,
                 request_kind,title,request_json,due_at,urgency,created_at,updated_at
               ) VALUES('human-1','a','task-a','agent','owner','decision',
                 'Confirm report recipient','{}',?,'batch',?,?)""", (NOW, NOW, NOW),
        )
        with self.assertRaises(sqlite3.IntegrityError):
            self.c.execute(
                """INSERT INTO human_action_requests(
                     id,workspace_id,task_id,requested_by_principal_id,assignee_principal_id,
                     request_kind,title,created_at,updated_at
                   ) VALUES('human-2','b','task-a','agent','owner','decision','Wrong scope',?,?)""",
                (NOW, NOW),
            )


if __name__ == "__main__":
    unittest.main()
