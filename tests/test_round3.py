"""Design-gap features: memory grants, repeating follow-ups, notices/digest, soft-due rule,
proposals, handoff, projects, monthly import, backup/verify, denial audit."""
import json
import sqlite3
import time
import unittest
from contextlib import closing

from test_framework import NOW, SRC, Base
from workdesk import ActorContext, DelegationProfileAdmin
from workdesk.errors import Forbidden, InvalidOperation
from workdesk.http_api import WorkbenchApp
from workdesk.identity import bind_identity
from workdesk.imports import import_rows, rows_from_csv
from workdesk.notices import sweep
from workdesk.scheduler import fire_due
from workdesk.storage import backup, verify


class Round3(Base):
    def prefs(self, **prefs):
        with closing(sqlite3.connect(self.db)) as c, c:
            c.execute("INSERT OR REPLACE INTO user_profiles(workspace_id,principal_id,work_preferences_json,updated_at)"
                      " VALUES('ws','owner',?,?)", (json.dumps(prefs), NOW))

    def test_work_memory_needs_an_explicit_grant(self):
        bind_identity(self.db, provider="telegram", bot_id="b", external_user_id="1", principal_id="gao")
        bind_identity(self.db, provider="telegram", bot_id="b", external_user_id="2", principal_id="owner")
        with closing(sqlite3.connect(self.db)) as c, c:
            c.execute("""INSERT INTO memory_context_grants(workspace_id,actor_principal_id,memory_user_id,capability,
                           granted_by_principal_id,grant_source_ref,created_at) VALUES('ws','owner','workbench:ws',
                           'memory.read','owner','cli',?)""", (NOW,))
        app = WorkbenchApp(self.db, host_token="t" * 32, clock=self.clock)
        ask = lambda ext: app.handle("/v1/memory/authorize", {"workspace_id": "ws", "action": "memory.read",
                                                              "identity": {"provider": "telegram", "bot_id": "b", "external_user_id": ext}})
        self.assertTrue(ask("2")["allowed"])
        with self.assertRaises(Forbidden):
            ask("1")

    def test_chase_every_n_days_until_answered(self):
        t = self.task()
        res = self.apply(self.agent, "w",
                         {"op": "add_dependency", "ref": "d", "task_id": t, "expected_version": 1, "kind": "external_response",
                          "awaited_outcome": "comments", "responsible_principal_id": "gao", "evidence_type": "explicit_user"},
                         {"op": "transition", "task_id": t, "expected_version": 1, "to": "WAITING", "evidence_type": "explicit_user"},
                         {"op": "set_reminder", "task_id": t, "expected_version": 1, "dependency_id": "$d",
                          "local_at": "2026-11-18T10:00", "repeat_every_days": 7, "max_repeats": 3,
                          "evidence_type": "explicit_user"})
        self.clock.now = "2026-11-18T02:30:00Z"
        self.assertEqual(len(fire_due(self.db, now=self.clock.now)["reminders_fired"]), 1)
        self.assertEqual(self.q("SELECT due_at, repeats_left FROM reminders WHERE status='scheduled'"),
                         [("2026-11-25T02:00:00Z", 1)])
        self.apply(self.agent, "r", {"op": "resolve_dependency", "dependency_id": res[0]["dependency_id"],
                                     "expected_version": 2, "evidence_type": "explicit_user", "source_ref": SRC})
        self.assertEqual(self.q("SELECT COUNT(*) FROM reminders WHERE status='scheduled'")[0][0], 0)

    def test_urgent_requests_notify_now_routine_ones_wait_for_digest(self):
        t = self.task()
        self.apply(self.agent, "a", {"op": "request_human_action", "task_id": t, "expected_version": 1, "request_kind": "decision",
                                     "title": "Which template?", "urgency": "batch", "evidence_type": "system"})
        self.apply(self.agent, "b", {"op": "request_human_action", "task_id": t, "expected_version": 2, "request_kind": "decision",
                                     "title": "Client waiting on the phone", "urgency": "interrupt", "evidence_type": "system"})
        texts = [json.loads(r[0])["text"] for r in self.q("SELECT payload_json FROM delivery_outbox")]
        self.assertEqual(texts, ["【急】需要你处理：Client waiting on the phone"])
        self.prefs(digest_time="09:00")
        self.clock.now = "2026-11-17T00:30:00Z"  # 08:30 local: too early
        sweep(self.db, now=self.clock.now)
        self.clock.now = "2026-11-17T01:05:00Z"  # 09:05 local
        self.assertIn("digest", sweep(self.db, now=self.clock.now)["sent"])
        self.assertNotIn("digest", sweep(self.db, now=self.clock.now)["sent"])  # once a day
        digest = [json.loads(r[0])["text"] for r in self.q("SELECT payload_json FROM delivery_outbox") if "日报" in r[0]]
        self.assertIn("Which template?", digest[0])

    def test_due_notices_are_sent_once_and_expired_approvals_close(self):
        t = self.task()
        self.apply(self.owner, "d", {"op": "set_official_due", "task_id": t, "expected_version": 1, "local": "2026-11-17",
                                     "evidence_type": "explicit_user"},
                   {"op": "record_commitment", "task_id": t, "expected_version": 1, "promisor_id": "gao",
                    "promisee_id": "owner", "promise_text": "send the data", "due_local": "2026-11-16",
                    "evidence_type": "explicit_user", "source_ref": SRC})
        with closing(sqlite3.connect(self.db)) as c, c:
            c.execute("INSERT INTO wake_events(id,workspace_id,origin,origin_event_id,kind,source_ref,created_at,updated_at)"
                      " VALUES('wk','ws','o','1','user_message','s',?,?)", (NOW, NOW))
            c.execute("""INSERT INTO approval_requests(id,workspace_id,wake_event_id,requested_by,approver_id,request_summary,
                           host_deferred_ref,created_at,expires_at) VALUES('apr','ws','wk','agent','owner','x','s#1',?,?)""",
                      (NOW, "2026-11-17T01:00:00Z"))
        first = sweep(self.db, now=NOW)["sent"]
        self.assertEqual(sorted(set(first)), ["approval_expired", "commitment_overdue", "due_today"])
        self.assertEqual(sweep(self.db, now=NOW)["sent"], [])
        self.assertEqual(self.q("SELECT status FROM approval_requests")[0][0], "expired")

    def test_soft_due_rule_suggests_without_overwriting(self):
        self.prefs(soft_due_rule={"days_before": 3, "skip_weekends": True, "at": "18:00"})
        t = self.task()
        out = self.apply(self.owner, "d", {"op": "set_official_due", "task_id": t, "expected_version": 1,
                                           "local": "2026-11-23", "evidence_type": "explicit_user"})[0]  # a Monday
        self.assertEqual(out["suggested_soft_due"]["local"], "2026-11-18T18:00")  # Wed: skips Sat/Sun
        self.assertIsNone(self.svc.read(actor=self.owner, task_id=t)["task"]["soft_due_at"])

    def test_unconfirmed_official_due_becomes_a_decision_not_an_overwrite(self):
        t = self.task(official_due_at="2026-11-30T09:00:00Z")
        res = self.apply(self.agent, "p", {"op": "propose_change", "task_id": t, "fields": {"official_due_local": "2026-12-05"},
                                           "reason": "Gao mentioned it may move", "evidence_type": "reported_third_party"})[0]
        self.assertTrue(res["human_action_id"])
        self.assertEqual(self.svc.read(actor=self.owner, task_id=t)["task"]["official_due_at"], "2026-11-30T09:00:00Z")

    def test_agent_handoff_keeps_level_and_leaves_a_checkpoint(self):
        with closing(sqlite3.connect(self.db)) as c, c:
            c.execute("INSERT INTO principals(id,display_name,kind,created_at) VALUES('agent2','B','agent',?)", (NOW,))
            c.execute("INSERT INTO memberships(workspace_id,principal_id,role) VALUES('ws','agent2','editor')")
        DelegationProfileAdmin(self.db, clock=self.clock).publish(owner=self.owner, work_class="w",
                                                                  max_autonomy_level="agent_bounded", allowed_actions=["a"])
        t = self.task()
        self.apply(self.owner, "d", {"op": "assign_responsibility", "task_id": t, "expected_version": 1, "executor_id": "agent",
                                     "work_class": "w", "autonomy_level": "agent_supervised", "evidence_type": "explicit_user",
                                     "source_ref": SRC, "reason": "go"})
        agent2 = ActorContext("ws", "agent2", "editor", "host")
        with self.assertRaises(Forbidden):
            self.apply(agent2, "x", {"op": "handoff", "task_id": t, "expected_version": 2, "to_executor_id": "agent2",
                                     "next_action": "n", "evidence_type": "system"})
        with self.assertRaises(InvalidOperation):
            self.apply(self.agent, "h0", {"op": "handoff", "task_id": t, "expected_version": 2, "to_executor_id": "owner",
                                          "next_action": "n", "evidence_type": "system"})
        out = self.apply(self.agent, "h", {"op": "handoff", "task_id": t, "expected_version": 2, "to_executor_id": "agent2",
                                           "next_action": "run integration tests", "unverified": ["pushed?"],
                                           "evidence_type": "verified_tool"})[0]
        view = self.svc.read(actor=self.owner, task_id=t)
        self.assertEqual((view["responsibility"]["current_executor_id"], view["responsibility"]["autonomy_level"]),
                         ("agent2", "agent_supervised"))
        self.assertEqual(view["latest_checkpoint"]["next_action"], "run integration tests")
        self.assertEqual(self.q("SELECT kind FROM wake_events WHERE id=?", out["wake_event_id"])[0][0], "agent_handoff")

    def test_projects(self):
        p = self.apply(self.owner, "p", {"op": "create_project", "title": "Client X", "evidence_type": "explicit_user"})[0]
        self.task(key="t2", project_id=p["project_id"])
        with self.assertRaises(InvalidOperation):
            self.task(key="t3", project_id="nope")
        # A project made in the same batch is referenced by its batch ref.
        out = self.apply(self.owner, "p2", {"op": "create_project", "ref": "np", "title": "Y", "evidence_type": "system"},
                         {"op": "create_task", "title": "in Y", "project_id": "$np", "evidence_type": "system"})
        self.assertEqual(self.q("SELECT project_id FROM tasks WHERE id=?", out[1]["task_id"])[0][0], out[0]["project_id"])

    def test_monthly_import_reconciles_without_touching_manual_fields(self):
        csv_text = "案号,名称,官方期限\nA-1,Alpha,2026-12-10\nA-2,Beta,2026-12-20\n"
        mapping = {"案号": "external_key", "名称": "title", "官方期限": "official_due_local"}
        first = import_rows(self.db, actor=self.owner, source_name="sys", period="2026-11",
                            rows=rows_from_csv(csv_text, mapping), clock=self.clock)
        self.assertEqual(first["new"], ["A-1", "A-2"])
        self.assertTrue(import_rows(self.db, actor=self.owner, source_name="sys", period="2026-11",
                                    rows=rows_from_csv(csv_text, mapping), clock=self.clock)["duplicate"])
        task_a1 = self.q("SELECT task_id FROM import_keys WHERE external_key='A-1'")[0][0]
        self.apply(self.owner, "soft", {"op": "patch_task", "task_id": task_a1, "expected_version": 1,
                                        "fields": {"soft_due_at": "2026-12-05T10:00:00Z"}, "evidence_type": "explicit_user"})
        second = import_rows(self.db, actor=self.owner, source_name="sys", period="2026-12",
                             rows=rows_from_csv("案号,名称,官方期限\nA-1,Alpha,2026-12-15\nA-3,Gamma,2027-01-05\n", mapping),
                             clock=self.clock)
        self.assertEqual((second["new"], second["missing"]), (["A-3"], ["A-2"]))
        self.assertEqual(second["due_changed"][0]["imported"], "2026-12-15")
        task = self.svc.read(actor=self.owner, task_id=task_a1)["task"]
        self.assertEqual((task["official_due_local"], task["soft_due_at"]), ("2026-12-10", "2026-12-05T10:00:00Z"))
        kinds = [json.loads(r[0])["kind"] for r in self.q("SELECT request_json FROM human_action_requests")]
        self.assertEqual(sorted(kinds), ["import_due_changed", "import_missing"])

    def test_backup_restores_tasks_reminders_and_sent_state(self):
        t = self.task()
        self.apply(self.owner, "r", {"op": "set_reminder", "task_id": t, "expected_version": 1,
                                     "local_at": "2026-11-20T10:00", "evidence_type": "explicit_user"})
        copy = backup(self.db, self.dir / "backups")
        report = verify(copy)
        self.assertEqual((report["integrity"], report["foreign_key_problems"], report["future_reminders"]), ("ok", 0, 1))
        self.assertEqual(report["counts"]["tasks"], 1)

    def test_refused_access_is_recorded_without_content(self):
        private = self.task()
        gao = ActorContext("ws", "gao", "requester", "telegram")
        with self.assertRaises(Exception):
            self.svc.read(actor=gao, task_id=private)
        time.sleep(0.3)
        self.assertEqual(self.q("SELECT principal_id, action, code FROM access_denials"), [("gao", "read", "NOT_FOUND")])


if __name__ == "__main__":
    unittest.main()
