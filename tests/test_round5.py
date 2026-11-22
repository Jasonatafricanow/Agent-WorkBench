"""Third audit (Codex, 2026-11-17): every counterexample it reproduced must now be refused."""
import sqlite3
import unittest
from contextlib import closing

from test_framework import NOW, Base
from test_task_service import Fixture

from workdesk import DelegationProfileAdmin
from workdesk.approvals import ApprovalService
from workdesk.errors import EvidenceRequired, InvalidOperation
from workdesk.identity import bind_identity, record_inbound
from workdesk.outboxes import DeliveryOutbox


class StrongEvidenceIsReal(Base):
    def inbox(self, principal, ref, at=NOW):
        with closing(sqlite3.connect(self.db)) as c, c:
            c.execute("""INSERT INTO inbox_messages(id,workspace_id,platform,bot_id,update_id,chat_id,message_id,
                           sender_principal_id,raw_source_ref,received_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
                      (f"in-{ref}", "ws", "telegram", "bot", f"u-{ref}", "c", ref, principal, ref, at, at))
        return ref

    def delivery(self, task, key, status="sent"):
        with closing(sqlite3.connect(self.db)) as c, c:
            c.execute("""INSERT INTO delivery_outbox(id,workspace_id,task_id,recipient_principal_id,channel,payload_json,
                           status,provider_message_id,dedupe_key,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
                      (key, "ws", task, "gao", "telegram", "{}", status, "tg-1", key, NOW, NOW))
        return key

    def task(self, key="t", **kw):
        return self.apply(self.owner, key, {"op": "create_task", "title": "Report", "state": "DOING",
                                            "evidence_type": "explicit_user", **kw})[0]["task_id"]

    def done(self, task, key, **ev):
        return self.apply(self.agent, key, {"op": "transition", "task_id": task, "expected_version": 1, "to": "DONE",
                                            "reason": "finished", **ev})

    def test_made_up_source_cannot_close_a_task(self):
        t = self.task()
        with self.assertRaises(EvidenceRequired):  # a tg:// string nobody sent
            self.done(t, "d1", evidence_type="explicit_user", source_ref="tg://bot/1/999")
        with self.assertRaises(EvidenceRequired):  # a real message, but from a requester unrelated to this task
            self.done(t, "d2", evidence_type="explicit_user", source_ref=self.inbox("gao", "g1"))
        other = self.task("t2")
        with self.assertRaises(EvidenceRequired):  # a confirmed delivery of a different task
            self.done(t, "d3", evidence_type="verified_tool", source_ref=self.delivery(other, "dl-other"))
        self.done(t, "d4", evidence_type="explicit_user", source_ref=self.inbox("owner", "o1"))

    def test_reopen_needs_real_evidence_and_keeps_the_completion_history(self):  # A23
        t = self.task()
        self.done(t, "c", evidence_type="explicit_user", source_ref=self.inbox("owner", "o-done"))
        reopen = lambda key, ref: self.apply(self.agent, key, {"op": "transition", "task_id": t, "expected_version": 2,
                                                               "to": "READY", "evidence_type": "explicit_user",
                                                               "source_ref": ref, "reason": "client found an error"})
        with self.assertRaises(EvidenceRequired):
            reopen("r1", "tg://bot/1/made-up")
        reopen("r2", self.inbox("owner", "o-reopen"))
        kinds = [e["kind"] for e in self.svc.read(actor=self.owner, task_id=t)["events"]]
        self.assertEqual(kinds.count("state_changed"), 2)  # the completion stays on record

    def test_verified_tool_close_needs_this_tasks_confirmed_record(self):
        t = self.task()
        with self.assertRaises(EvidenceRequired):
            self.done(t, "v1", evidence_type="verified_tool", source_ref=self.delivery(t, "dl-pending", "pending"))
        self.done(t, "v2", evidence_type="verified_tool", source_ref=self.delivery(t, "dl-sent"))

    def test_existing_official_due_moves_only_on_real_evidence(self):
        t = self.task(official_due_at="2026-11-30T09:00:00Z")
        move = lambda key, ref: self.apply(self.agent, key, {
            "op": "set_official_due", "task_id": t, "expected_version": 1, "local": "2026-11-28",
            "evidence_type": "explicit_user", "source_ref": ref, "reason": "client moved it"})
        with self.assertRaises(EvidenceRequired):
            move("m1", "tg://bot/1/404")
        move("m2", self.inbox("owner", "o2"))

    def test_unrelated_receipt_and_message_cannot_accept_a_deliverable(self):
        t = self.task(requester_id="gao")
        other = self.task("t2")
        did = self.apply(self.agent, "reg", {"op": "register_deliverable", "task_id": t, "expected_version": 1,
                                             "title": "Plan", "artifact_ref": "f", "evidence_type": "explicit_user"})[0]["deliverable_id"]
        submit = lambda key, ref: self.apply(self.agent, key, {"op": "update_deliverable", "deliverable_id": did,
                                                               "expected_version": 2, "status": "SUBMITTED",
                                                               "evidence_type": "verified_tool", "receipt_ref": ref})
        with self.assertRaises(EvidenceRequired):
            submit("s1", self.delivery(other, "dl-other"))
        submit("s2", self.delivery(t, "dl-mine"))
        accept = lambda key, ref: self.apply(self.agent, key, {"op": "update_deliverable", "deliverable_id": did,
                                                               "expected_version": 3, "status": "ACCEPTED",
                                                               "evidence_type": "explicit_user", "source_ref": ref})
        with self.assertRaises(EvidenceRequired):  # a human, but not the person it was delivered to
            accept("a1", self.inbox("owner", "o3"))
        with self.assertRaises(EvidenceRequired):  # the right person, but said before it was submitted
            accept("a2", self.inbox("gao", "g-early", at="2026-11-17T01:00:00Z"))
        accept("a3", self.inbox("gao", "g-ok"))


class ApprovalsDecideOnRealInput(Base):
    def setUp(self):
        super().setUp()
        with closing(sqlite3.connect(self.db)) as c, c:
            c.execute("INSERT INTO wake_events(id,workspace_id,origin,origin_event_id,kind,source_ref,created_at,updated_at)"
                      " VALUES('wk','ws','o','1','user_message','s',?,?)", (NOW, NOW))
        self.svc_a = ApprovalService(self.db, clock=self.clock)
        self.req = self.svc_a.request(host_actor=self.agent, wake_event_id="wk", tool_name="send", tool_input={"x": 1},
                                      summary="s", host_deferred_ref="sess#1", expires_at="2026-11-18T00:00:00Z")
        from workdesk import ActorContext
        self.owner_tg = ActorContext("ws", "owner", "owner", "telegram")

    def test_string_false_is_not_an_approval(self):
        for value in ("false", "true", 0, None):
            with self.assertRaises(InvalidOperation):
                self.svc_a.decide(approver=self.owner, approval_id=self.req["approval_id"], approve=value, source_ref="x")

    def test_decision_must_cite_the_approvers_real_message(self):
        with self.assertRaises(EvidenceRequired):
            self.svc_a.decide(approver=self.owner_tg, approval_id=self.req["approval_id"], approve=True,
                              source_ref="tg://bot/1/404")
        with closing(sqlite3.connect(self.db)) as c, c:
            c.execute("""INSERT INTO inbox_messages(id,workspace_id,platform,bot_id,update_id,chat_id,message_id,
                           sender_principal_id,raw_source_ref,received_at,updated_at) VALUES('i','ws','telegram','b','u','c','m',
                           'owner','tg://bot/c/m',?,?)""", (NOW, NOW))
        res = self.svc_a.decide(approver=self.owner_tg, approval_id=self.req["approval_id"], approve=False,
                                source_ref="tg://bot/c/m")
        self.assertEqual(res["status"], "denied")


class NewRecipientIsDerived(Fixture):
    def test_new_recipient_comes_from_delivery_history_not_the_model(self):
        DelegationProfileAdmin(self.db, clock=lambda: NOW).publish(
            owner=self.owner, work_class="w", max_autonomy_level="agent_bounded", allowed_actions=["send"],
            approval_conditions=["new_recipient"])
        task = self.create(state="READY")["task_id"]
        self.apply(self.owner, "del", {"op": "assign_responsibility", "task_id": task, "expected_version": 1,
                                       "executor_id": "agent", "work_class": "w", "autonomy_level": "agent_bounded",
                                       "evidence_type": "explicit_user", "source_ref": "x", "reason": "go"})
        route = lambda: self.svc.evaluate_action(actor=self.agent, task_id=task, action="send", recipient_id="gao")["route"]
        self.assertEqual(route(), "human_approval_required")  # never delivered to gao before
        with closing(sqlite3.connect(self.db)) as c, c:
            c.execute("""INSERT INTO delivery_outbox(id,workspace_id,recipient_principal_id,channel,payload_json,status,
                           provider_message_id,dedupe_key,created_at,updated_at) VALUES('d','ws','gao','telegram','{}','sent',
                           'tg-1','d',?,?)""", (NOW, NOW))
        self.assertEqual(route(), "agent_execute")


class AnsweredInChatIsNotRunTwice(Fixture):
    def test_answer_inside_a_chat_run_does_not_wake_a_second_run(self):
        from workdesk import ActorContext
        task = self.create(state="READY")["task_id"]
        req = self.apply(self.agent, "ask", {"op": "request_human_action", "task_id": task, "expected_version": 1,
                                             "request_kind": "decision", "title": "ok?", "urgency": "batch",
                                             "evidence_type": "system"})[0]
        owner_tg = ActorContext("ws", "owner", "owner", "telegram")
        out = self.apply(owner_tg, "answer", {"op": "respond_human_action", "human_action_id": req["human_action_id"],
                                              "evidence_type": "explicit_user", "source_ref": "tg://work-bot/chat-1/msg-1"})[0]
        with closing(sqlite3.connect(self.db)) as c:
            self.assertEqual(c.execute("SELECT status, host_run_ref FROM wake_events WHERE id=?",
                                       (out["wake_event_id"],)).fetchone(), ("completed", "inline"))


class RecoveryAndSimulatedReceipts(Base):
    def test_a_message_whose_run_died_is_resumed_not_dropped(self):
        bind_identity(self.db, provider="telegram", bot_id="workbot", external_user_id="111", principal_id="owner")
        args = dict(workspace_id="ws", platform="telegram", bot_id="workbot", chat_id="c", raw_source_ref="tg://w/c/1",
                    update_id="1", message_id="1", external_user_id="111", clock=self.clock)
        first = record_inbound(self.db, **args)
        self.assertTrue(record_inbound(self.db, **args)["duplicate"])  # its Run is still live
        self.clock.now = "2026-11-17T03:00:00Z"  # the lease ran out: the Host died mid-Run
        again = record_inbound(self.db, **args)
        self.assertTrue(again["resumed"])
        self.assertEqual(again["wake_event_id"], first["wake_event_id"])
        with closing(sqlite3.connect(self.db)) as c, c:
            c.execute("UPDATE wake_events SET status='completed' WHERE id=?", (first["wake_event_id"],))
        self.assertTrue(record_inbound(self.db, **args)["duplicate"])  # finished work is never redone

    def test_cli_simulated_delivery_is_not_a_receipt(self):
        out = DeliveryOutbox(self.db, clock=self.clock)
        d = out.enqueue(workspace_id="ws", recipient_id="owner", channel="telegram", payload={"text": "x"}, dedupe_key="k")
        out.claim(workspace_id="ws")
        out.settle(workspace_id="ws", delivery_id=d["delivery_id"], status="simulated")
        self.assertEqual(self.q("SELECT status, provider_message_id FROM delivery_outbox"), [("cancelled", "cli-simulated")])


if __name__ == "__main__":
    unittest.main()
