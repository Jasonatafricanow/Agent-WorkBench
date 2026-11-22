"""TaskService contract tests on a real SQLite file (no fakes for the service)."""
import json
import sqlite3
import tempfile
import threading
import unittest
from contextlib import closing
from pathlib import Path

from workdesk import (
    ActorContext, DelegationProfileAdmin, SqliteTaskService, WorkbenchTools, init_database,
)
from workdesk.errors import (
    EvidenceRequired, Forbidden, IdempotencyConflict, InvalidOperation, NotFound, VersionConflict,
)

NOW = "2026-11-17T02:00:00Z"  # 10:00 Asia/Shanghai
SRC = "tg://work-bot/chat-1/msg-1"


class Fixture(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(tmp.cleanup)
        self.db = Path(tmp.name) / "wb.sqlite"
        init_database(self.db)
        with closing(sqlite3.connect(self.db)) as c, c:
            c.executemany("INSERT INTO principals(id,display_name,kind,created_at) VALUES(?,?,?,?)", [
                ("owner", "Owner", "human", NOW), ("gao", "Gao", "human", NOW),
                ("agent", "Work Agent", "agent", NOW), ("agent2", "Other Agent", "agent", NOW)])
            c.execute("INSERT INTO workspaces(id,title,owner_id,timezone,created_at) VALUES('ws','Work','owner','Asia/Shanghai',?)", (NOW,))
            c.executemany("INSERT INTO memberships(workspace_id,principal_id,role) VALUES('ws',?,?)", [
                ("owner", "owner"), ("gao", "requester"), ("agent", "editor"), ("agent2", "editor")])
            # SRC is a message the owner really sent: strong evidence through a Host must resolve to one.
            c.execute("""INSERT INTO inbox_messages(id,workspace_id,platform,bot_id,update_id,chat_id,message_id,
                           sender_principal_id,raw_source_ref,received_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
                      ("in-src", "ws", "telegram", "work-bot", "u-src", "chat-1", "msg-1", "owner", SRC, NOW, NOW))
        self.svc = SqliteTaskService(self.db, clock=lambda: NOW)
        # Trusted CLI source: owner-only ops then need no inbox message (see OwnerSourceTests).
        self.owner = ActorContext("ws", "owner", "owner", "cli")
        self.agent = ActorContext("ws", "agent", "editor", "host")
        self.agent2 = ActorContext("ws", "agent2", "editor", "host")
        self.gao = ActorContext("ws", "gao", "requester", "telegram")

    def apply(self, actor, key, *ops):
        return self.svc.apply(actor=actor, idempotency_key=key, operations=list(ops))

    def create(self, actor=None, key="create", **fields):
        op = {"op": "create_task", "title": "Quarterly report", "evidence_type": "explicit_user",
              "source_ref": SRC, **fields}
        return self.apply(actor or self.owner, key, op)[0]

    def count(self, table):
        with closing(sqlite3.connect(self.db)) as c:
            return c.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]


class TaskLedgerTests(Fixture):
    def test_tool_roundtrip_and_second_agent_sees_same_version(self):
        tools = WorkbenchTools(self.agent, self.svc)
        created = tools.change_tasks(idempotency_key="k1", operations=[
            {"op": "create_task", "title": "Draft plan", "state": "READY", "evidence_type": "explicit_user"}])[0]
        self.assertEqual(created["version"], 1)
        tools.change_tasks(idempotency_key="k2", operations=[
            {"op": "append_progress", "task_id": created["task_id"], "expected_version": 1,
             "text": "outline done", "next_action": "write section 2", "evidence_type": "explicit_user"}])
        seen = WorkbenchTools(self.agent2, self.svc).get_task(task_id=created["task_id"])
        self.assertEqual(seen["task"]["version"], 2)
        self.assertEqual(seen["task"]["next_action"], "write section 2")
        self.assertEqual([e["kind"] for e in seen["events"]], ["created", "progress"])

    def test_replay_returns_first_receipt_and_changed_payload_conflicts(self):
        first = self.create(key="tg:msg-9")
        again = self.create(key="tg:msg-9")
        self.assertEqual(first, again)
        self.assertEqual(self.count("tasks"), 1)
        self.assertEqual(self.count("task_events"), 1)
        with self.assertRaises(IdempotencyConflict):
            self.create(key="tg:msg-9", title="something else")
        with self.assertRaises(IdempotencyConflict):  # same key, different trusted actor
            self.create(actor=self.agent, key="tg:msg-9")

    def test_concurrent_writers_from_same_version_one_wins(self):
        task = self.create()["task_id"]
        outcomes = []

        def write(actor, text):
            try:
                self.apply(actor, f"k-{text}", {"op": "append_progress", "task_id": task,
                           "expected_version": 1, "text": text, "evidence_type": "explicit_user"})
                outcomes.append("ok")
            except VersionConflict as exc:
                outcomes.append(exc.code)

        threads = [threading.Thread(target=write, args=(a, t)) for a, t in
                   ((self.agent, "a"), (self.agent2, "b"))]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(sorted(outcomes), ["VERSION_CONFLICT", "ok"])
        self.assertEqual(self.svc.read(actor=self.owner, task_id=task)["task"]["version"], 2)

    def test_failed_batch_leaves_nothing_behind(self):
        with self.assertRaises(InvalidOperation) as ctx:
            self.apply(self.owner, "bad-batch",
                       {"op": "create_task", "ref": "t", "title": "A", "evidence_type": "explicit_user"},
                       {"op": "set_reminder", "task_id": "$t", "local_at": "2026-11-20T10:00",
                        "evidence_type": "explicit_user"},
                       {"op": "transition", "task_id": "$t", "to": "NOT_A_STATE", "evidence_type": "explicit_user"})
        self.assertEqual(ctx.exception.details["op_index"], 2)
        for table in ("tasks", "task_events", "reminders", "command_receipts"):
            self.assertEqual(self.count(table), 0, table)

    def test_state_survives_restart(self):
        created = self.create(key="restart")
        fresh = SqliteTaskService(self.db, clock=lambda: NOW)
        self.assertEqual(fresh.read(actor=self.owner, task_id=created["task_id"])["task"]["title"], "Quarterly report")
        self.assertEqual(fresh.apply(actor=self.owner, idempotency_key="restart", operations=[
            {"op": "create_task", "title": "Quarterly report", "evidence_type": "explicit_user",
             "source_ref": SRC}]), [created])


class EvidenceTests(Fixture):
    def test_done_needs_strong_evidence_source_and_reason(self):
        task = self.create(state="DOING")["task_id"]
        with self.assertRaises(EvidenceRequired):
            self.apply(self.agent, "d1", {"op": "transition", "task_id": task, "expected_version": 1,
                                          "to": "DONE", "evidence_type": "inferred", "reason": "looks done"})
        with self.assertRaises(EvidenceRequired):
            self.apply(self.agent, "d2", {"op": "transition", "task_id": task, "expected_version": 1,
                                          "to": "DONE", "evidence_type": "explicit_user", "reason": "done"})
        done = self.apply(self.agent, "d3", {"op": "transition", "task_id": task, "expected_version": 1,
                                             "to": "DONE", "evidence_type": "explicit_user",
                                             "source_ref": SRC, "reason": "user confirmed delivery accepted"})
        self.assertEqual(done[0]["version"], 2)

    def test_inbox_item_can_be_closed_directly_with_the_owners_word(self):
        # 11-20: "that one is done" about an inbox item; Xiyue was refused INBOX -> DONE.
        task = self.create(state="INBOX")["task_id"]
        with self.assertRaises(EvidenceRequired):
            self.apply(self.agent, "i1", {"op": "transition", "task_id": task, "expected_version": 1,
                                          "to": "DONE", "evidence_type": "inferred", "reason": "looks done"})
        done = self.apply(self.agent, "i2", {"op": "transition", "task_id": task, "expected_version": 1,
                                             "to": "DONE", "evidence_type": "explicit_user",
                                             "source_ref": SRC, "reason": "owner said it is done"})
        self.assertEqual(done[0]["version"], 2)

    def test_official_due_change_and_delay_are_justified(self):
        task = self.create(official_due_at="2026-11-30T09:00:00Z", soft_due_at="2026-11-25T09:00:00Z")["task_id"]
        with self.assertRaises(EvidenceRequired):
            self.apply(self.agent, "o1", {"op": "patch_task", "task_id": task, "expected_version": 1,
                                          "fields": {"official_due_at": "2026-12-05T09:00:00Z"},
                                          "evidence_type": "inferred", "reason": "probably later",
                                          "delay_kind": "other"})
        with self.assertRaises(InvalidOperation):  # later soft due without a delay kind
            self.apply(self.agent, "o2", {"op": "patch_task", "task_id": task, "expected_version": 1,
                                          "fields": {"soft_due_at": "2026-11-27T09:00:00Z"},
                                          "evidence_type": "explicit_user"})
        self.apply(self.agent, "o3", {"op": "patch_task", "task_id": task, "expected_version": 1,
                                      "fields": {"soft_due_at": "2026-11-27T09:00:00Z"},
                                      "evidence_type": "explicit_user", "source_ref": SRC,
                                      "delay_kind": "external_wait", "reason": "waiting for client data"})
        events = self.svc.read(actor=self.owner, task_id=task)["events"]
        delayed = [e for e in events if e["kind"] == "delayed"][0]
        self.assertEqual(json.loads(delayed["before_json"]), {"soft_due_at": "2026-11-25T09:00:00Z"})
        self.assertEqual(json.loads(delayed["after_json"])["delay_kind"], "external_wait")


class RequesterIsolationTests(Fixture):
    def test_requester_sees_only_own_requests_and_cannot_probe(self):
        private = self.create(key="private")["task_id"]
        mine = self.create(actor=self.gao, key="gao-1", title="Review the plan by Friday",
                           official_due_at="2026-11-21T10:00:00Z")
        with self.assertRaises(NotFound) as hidden:
            self.svc.read(actor=self.gao, task_id=private)
        with self.assertRaises(NotFound) as missing:
            self.svc.read(actor=self.gao, task_id="task-does-not-exist")
        self.assertEqual(str(hidden.exception), str(missing.exception))
        view = self.svc.overview(actor=self.gao, limit=50)
        self.assertEqual([t["id"] for t in view["tasks"]], [mine["task_id"]])
        self.assertNotIn("soft_due_at", view["tasks"][0])
        owner_view = self.svc.read(actor=self.owner, task_id=mine["task_id"])["task"]
        self.assertEqual((owner_view["state"], owner_view["requester_id"], owner_view["assignee_id"]),
                         ("INBOX", "gao", "owner"))

    def test_requester_cannot_plan_transition_or_write_private_tasks(self):
        private = self.create(key="private")["task_id"]
        with self.assertRaises(Forbidden):
            self.create(actor=self.gao, key="g1", soft_due_at="2026-11-20T00:00:00Z")
        with self.assertRaises(NotFound):
            self.apply(self.gao, "g2", {"op": "append_progress", "task_id": private, "text": "hi",
                                        "evidence_type": "explicit_user"})
        mine = self.create(actor=self.gao, key="g3")["task_id"]
        with self.assertRaises(Forbidden):
            self.apply(self.gao, "g4", {"op": "transition", "task_id": mine, "expected_version": 1,
                                        "to": "CANCELLED", "evidence_type": "explicit_user", "source_ref": SRC,
                                        "reason": "never mind"})

    def test_forged_role_fails_closed(self):
        with self.assertRaises(Forbidden):
            self.svc.overview(actor=ActorContext("ws", "gao", "owner", "telegram"), limit=10)


class WaitingAndFocusTests(Fixture):
    def test_waiting_followup_is_cancelled_when_reply_arrives_first(self):
        task = self.create(state="DOING")["task_id"]
        with self.assertRaises(InvalidOperation):  # WAITING without a dependency
            self.apply(self.agent, "w0", {"op": "transition", "task_id": task, "expected_version": 1,
                                          "to": "WAITING", "evidence_type": "explicit_user"})
        res = self.apply(self.agent, "w1",
                         {"op": "add_dependency", "ref": "d", "task_id": task, "expected_version": 1,
                          "kind": "external_response", "awaited_outcome": "Gao's comments on the draft",
                          "responsible_principal_id": "gao", "evidence_type": "explicit_user"},
                         {"op": "transition", "task_id": task, "expected_version": 1, "to": "WAITING",
                          "evidence_type": "explicit_user", "reason": "draft sent"},
                         {"op": "set_reminder", "task_id": task, "expected_version": 1,
                          "dependency_id": "$d", "local_at": "2026-11-20T10:00",
                          "evidence_type": "explicit_user"})
        self.assertEqual(res[2]["due_at"], "2026-11-20T02:00:00Z")  # 10:00 Shanghai, not UTC midnight
        self.assertEqual({r["version"] for r in res}, {2})
        out = self.apply(self.agent, "w2",
                         {"op": "resolve_dependency", "dependency_id": res[0]["dependency_id"],
                          "expected_version": 2, "evidence_type": "explicit_user",
                          "source_ref": "tg://work-bot/chat-gao/msg-7"},
                         {"op": "transition", "task_id": task, "expected_version": 2, "to": "READY",
                          "evidence_type": "explicit_user", "reason": "comments received"})
        self.assertEqual(out[0]["reminders_cancelled"], 1)
        full = self.svc.read(actor=self.owner, task_id=task)
        self.assertEqual(full["task"]["state"], "READY")
        self.assertEqual(full["reminders"], [])

    def test_focus_is_owner_only_and_closed_tasks_leave_it(self):
        task = self.create(state="DOING")["task_id"]
        with self.assertRaises(Forbidden):
            self.apply(self.agent, "f0", {"op": "set_focus", "slot": "NOW", "task_id": task,
                                          "evidence_type": "explicit_user"})
        self.apply(self.owner, "f1", {"op": "set_focus", "slot": "NOW", "task_id": task,
                                      "evidence_type": "explicit_user"})
        self.assertEqual(self.svc.overview(actor=self.owner, limit=10)["focus"]["NOW"]["id"], task)
        self.apply(self.owner, "f2", {"op": "transition", "task_id": task, "expected_version": 2, "to": "DONE",
                                      "evidence_type": "explicit_user", "source_ref": SRC, "reason": "shipped"})
        self.assertIsNone(self.svc.overview(actor=self.owner, limit=10)["focus"]["NOW"])


class DelegationTests(Fixture):
    def setUp(self):
        super().setUp()
        self.admin = DelegationProfileAdmin(self.db, clock=lambda: NOW)
        self.admin.publish(owner=self.owner, work_class="weekly_report", max_autonomy_level="agent_bounded",
                           allowed_actions=["collect_data", "draft_report", "send_report"],
                           reversible_actions=["collect_data", "draft_report"],
                           review_actions=["send_report"], approval_conditions=["new_recipient"])
        self.task = self.create(state="READY")["task_id"]

    def delegate(self, actor=None, key="del", **extra):
        op = {"op": "assign_responsibility", "task_id": self.task, "expected_version": 1,
              "executor_id": "agent", "work_class": "weekly_report", "autonomy_level": "agent_bounded",
              "evidence_type": "explicit_user", "source_ref": SRC, "reason": "owner delegated weekly report",
              **extra}
        return self.apply(actor or self.owner, key, op)

    def test_only_human_owner_publishes_profiles(self):
        with self.assertRaises(Forbidden):
            self.admin.publish(owner=self.agent, work_class="weekly_report",
                               max_autonomy_level="agent_exception_only", allowed_actions=["send_report"])
        self.assertFalse(hasattr(WorkbenchTools, "publish"))

    def test_delegation_narrows_profile_and_routes_actions(self):
        with self.assertRaises(Forbidden):
            self.delegate(actor=self.agent, key="self-grant")
        with self.assertRaises(InvalidOperation):
            self.delegate(key="too-high", autonomy_level="agent_exception_only")
        with self.assertRaises(InvalidOperation):
            self.delegate(key="too-wide", allowed_actions=["collect_data", "send_report", "pay_invoice"])
        self.assertEqual(self.delegate()[0]["responsibility_event"], "delegated")
        route = lambda action, *cond: self.svc.evaluate_action(
            actor=self.agent, task_id=self.task, action=action, triggered_conditions=cond)["route"]
        self.assertEqual(route("collect_data"), "agent_execute")
        self.assertEqual(route("send_report"), "agent_execute_with_review")
        self.assertEqual(route("send_report", "new_recipient"), "human_approval_required")
        self.assertEqual(route("pay_invoice"), "out_of_scope")
        self.apply(self.owner, "revoke", {"op": "set_responsibility_status", "task_id": self.task,
                                          "expected_version": 2, "status": "revoked",
                                          "evidence_type": "explicit_user", "source_ref": SRC,
                                          "reason": "owner takes this back"})
        self.assertEqual(route("collect_data"), "out_of_scope")
        with closing(sqlite3.connect(self.db)) as c:
            kinds = [r[0] for r in c.execute("SELECT event_type FROM responsibility_events ORDER BY recorded_at, rowid")]
        self.assertEqual(kinds, ["delegated", "revoked"])

    def test_human_action_response_reenters_the_loop(self):
        req = self.apply(self.agent, "ask", {"op": "request_human_action", "task_id": self.task,
                                             "expected_version": 1, "request_kind": "decision",
                                             "title": "Which data source for the report?",
                                             "urgency": "batch", "evidence_type": "system"})[0]
        inbox = self.svc.overview(actor=self.owner, limit=10)["my_human_actions"]
        self.assertEqual([r["id"] for r in inbox], [req["human_action_id"]])
        with self.assertRaises(NotFound):  # only the assignee answers
            self.apply(self.agent, "fake-answer", {"op": "respond_human_action",
                       "human_action_id": req["human_action_id"], "evidence_type": "explicit_user",
                       "source_ref": SRC})
        out = self.apply(self.owner, "answer", {"op": "respond_human_action",
                         "human_action_id": req["human_action_id"], "response": {"choice": "ERP export"},
                         "evidence_type": "explicit_user", "source_ref": SRC})[0]
        with closing(sqlite3.connect(self.db)) as c:
            wake = c.execute("SELECT kind,status,task_id FROM wake_events WHERE id=?",
                             (out["wake_event_id"],)).fetchone()
        self.assertEqual(wake, ("human_action_completed", "pending", self.task))
        self.assertEqual(self.svc.overview(actor=self.owner, limit=10)["my_human_actions"], [])



class OwnerSourceTests(Fixture):
    def test_owner_only_change_via_telegram_must_cite_a_real_owner_message(self):
        owner_tg = ActorContext("ws", "owner", "owner", "telegram")
        task = self.create(state="DOING")["task_id"]
        focus = {"op": "set_focus", "slot": "NOW", "task_id": task, "evidence_type": "explicit_user",
                 "source_ref": "tg://made-up"}
        with self.assertRaises(EvidenceRequired):
            self.apply(owner_tg, "f-fake", focus)
        with closing(sqlite3.connect(self.db)) as c, c:
            c.execute("""INSERT INTO inbox_messages(id,workspace_id,platform,bot_id,update_id,chat_id,message_id,
                           sender_principal_id,raw_source_ref,received_at,updated_at)
                         VALUES('in1','ws','telegram','bot','u1','c','m1','owner','tg://bot/c/m1',?,?)""", (NOW, NOW))
        self.apply(owner_tg, "f-real", dict(focus, source_ref="tg://bot/c/m1"))

    def test_only_the_executor_gets_a_delegated_route(self):
        DelegationProfileAdmin(self.db, clock=lambda: NOW).publish(
            owner=self.owner, work_class="w", max_autonomy_level="agent_bounded", allowed_actions=["act"])
        task = self.create(state="READY")["task_id"]
        self.apply(self.owner, "d", {"op": "assign_responsibility", "task_id": task, "expected_version": 1,
                                     "executor_id": "agent", "work_class": "w", "autonomy_level": "agent_bounded",
                                     "evidence_type": "explicit_user", "source_ref": SRC, "reason": "go"})
        route = lambda actor: self.svc.evaluate_action(actor=actor, task_id=task, action="act")["route"]
        self.assertEqual(route(self.agent), "agent_execute")
        self.assertEqual(route(self.agent2), "out_of_scope")

    def test_requester_whats_new_hides_internal_work(self):
        mine = self.create(actor=self.gao, key="g")["task_id"]
        self.svc.changes_since(actor=self.gao, view="tg", mark_seen=True)
        self.apply(self.owner, "p", {"op": "patch_task", "task_id": mine, "expected_version": 1,
                                     "fields": {"priority": 0}, "evidence_type": "explicit_user"})
        self.apply(self.owner, "s", {"op": "transition", "task_id": mine, "expected_version": 2, "to": "READY",
                                     "evidence_type": "explicit_user", "reason": "accepted"})
        events = self.svc.changes_since(actor=self.gao, view="tg")["events"]
        self.assertEqual([e["kind"] for e in events], ["state_changed"])
        self.assertIsNone(events[0]["actor_id"])


if __name__ == "__main__":
    unittest.main()
