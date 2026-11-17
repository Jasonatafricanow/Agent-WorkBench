"""Regression tests for the second audit (deviations from design, omissions, new bugs)."""
import json
import sqlite3
import unittest
from contextlib import closing

from test_framework import NOW, SRC, Base
from workdesk import ActorContext, DelegationProfileAdmin
from workdesk.errors import EvidenceRequired, Forbidden, InvalidOperation
from workdesk.imports import import_rows
from workdesk.insights import schedule_load
from workdesk.promotion import review_delegations
from workdesk.scheduler import fire_due


class SecondAudit(Base):
    def setUp(self):
        super().setUp()
        self.owner_tg = ActorContext("ws", "owner", "owner", "telegram")
        self.gao = ActorContext("ws", "gao", "requester", "telegram")

    # A1 -------------------------------------------------------------------------------------
    def test_outsider_reminders_need_owner_message_and_get_fixed_text_no_model_run(self):
        mine = self.apply(self.gao, "g", {"op": "create_task", "title": "Review the plan", "evidence_type": "explicit_user"})[0]["task_id"]
        private = self.task(key="private")
        rem = lambda actor, task, key, ref=SRC: self.apply(actor, key, {
            "op": "set_reminder", "task_id": task, "expected_version": 1, "recipient_principal_id": "gao",
            "local_at": "2026-11-20T10:00", "evidence_type": "explicit_user", "source_ref": ref})
        with self.assertRaises(Forbidden):  # the Agent cannot chase an outsider on its own
            rem(self.agent, mine, "a")
        with self.assertRaises(EvidenceRequired):  # the owner's word must be a real message
            rem(self.owner_tg, mine, "b", ref="tg://invented")
        ref = self.inbox_from("owner")
        with self.assertRaises(Forbidden):  # only about the outsider's own request
            rem(self.owner_tg, private, "c", ref=ref)
        rem(self.owner_tg, mine, "d", ref=ref)
        self.clock.now = "2026-11-20T02:30:00Z"
        fire_due(self.db, now=self.clock.now)
        self.assertEqual(self.q("SELECT COUNT(*) FROM wake_events WHERE kind='timer_fired'")[0][0], 0)
        text = json.loads(self.q("SELECT payload_json FROM delivery_outbox WHERE recipient_principal_id='gao'")[0][0])["text"]
        self.assertIn("Review the plan", text)

    def test_requester_cannot_be_made_accountable(self):  # C4
        t = self.task()
        with self.assertRaises(InvalidOperation):
            self.apply(self.owner, "p", {"op": "patch_task", "task_id": t, "expected_version": 1,
                                         "fields": {"assignee_id": "gao"}, "evidence_type": "explicit_user"})

    # A2 -------------------------------------------------------------------------------------
    def test_narrowed_profile_takes_effect_on_existing_delegations(self):
        admin = DelegationProfileAdmin(self.db, clock=self.clock)
        admin.publish(owner=self.owner, work_class="r", max_autonomy_level="agent_bounded", allowed_actions=["draft", "send"])
        t = self.task()
        self.apply(self.owner, "d", {"op": "assign_responsibility", "task_id": t, "expected_version": 1, "executor_id": "agent",
                                     "work_class": "r", "autonomy_level": "agent_bounded", "evidence_type": "explicit_user",
                                     "source_ref": SRC, "reason": "go"})
        route = lambda a: self.svc.evaluate_action(actor=self.agent, task_id=t, action=a)["route"]
        self.assertEqual(route("send"), "agent_execute")
        out = admin.publish(owner=self.owner, work_class="r", max_autonomy_level="agent_prepares", allowed_actions=["draft"])
        self.assertEqual(out["delegations_updated"], [t])
        self.assertEqual(route("send"), "out_of_scope")
        self.assertEqual(route("draft"), "agent_prepare_only")

    # A4 -------------------------------------------------------------------------------------
    def test_model_cannot_answer_the_owners_request_without_the_owners_message(self):
        t = self.task()
        req = self.apply(self.agent, "q", {"op": "request_human_action", "task_id": t, "expected_version": 1,
                                           "request_kind": "decision", "title": "Move the deadline?", "evidence_type": "system"})[0]
        answer = lambda ref, key: self.apply(self.owner_tg, key, {"op": "respond_human_action", "human_action_id": req["human_action_id"],
                                                                  "response": {"ok": True}, "evidence_type": "explicit_user", "source_ref": ref})
        with self.assertRaises(EvidenceRequired):
            answer("tg://made-up", "x")
        answer(self.inbox_from("owner"), "y")

    # A5 / A7 --------------------------------------------------------------------------------
    def test_requester_cannot_interrupt_and_proposals_announce(self):
        mine = self.apply(self.gao, "g", {"op": "create_task", "title": "Need it now", "evidence_type": "explicit_user"})[0]["task_id"]
        self.apply(self.gao, "h", {"op": "request_human_action", "task_id": mine, "request_kind": "decision",
                                   "title": "URGENT!!", "urgency": "interrupt", "evidence_type": "explicit_user"})
        self.assertEqual(self.q("SELECT urgency FROM human_action_requests")[0][0], "batch")
        self.assertEqual(self.q("SELECT COUNT(*) FROM delivery_outbox")[0][0], 0)
        t = self.task()
        with self.assertRaises(InvalidOperation):
            self.apply(self.agent, "p0", {"op": "propose_change", "task_id": t, "fields": {"title": "x"},
                                          "urgency": "loud", "evidence_type": "inferred"})
        self.apply(self.agent, "p", {"op": "propose_change", "task_id": t, "fields": {"title": "x"},
                                     "urgency": "notify", "evidence_type": "inferred"})
        self.assertEqual(self.q("SELECT COUNT(*) FROM delivery_outbox")[0][0], 1)

    # C1 -------------------------------------------------------------------------------------
    def test_repeating_chase_after_downtime_fires_once_not_in_a_burst(self):
        t = self.task()
        self.apply(self.agent, "r", {"op": "set_reminder", "task_id": t, "expected_version": 1, "local_at": "2026-11-18T10:00",
                                     "repeat_every_days": 1, "max_repeats": 10, "evidence_type": "explicit_user"})
        self.clock.now = "2026-11-25T03:00:00Z"  # a week down
        self.assertEqual(len(fire_due(self.db, now=self.clock.now)["reminders_fired"]), 1)
        self.assertEqual(fire_due(self.db, now=self.clock.now)["reminders_fired"], [])
        self.assertEqual(self.q("SELECT due_at, repeats_left FROM reminders WHERE status='scheduled'"),
                         [("2026-11-26T02:00:00Z", 1)])  # 10 total: 1 sent + 7 missed + this + 1 more

    # C2 -------------------------------------------------------------------------------------
    def test_missing_import_row_is_asked_once(self):
        row = lambda k: {"external_key": k, "title": k}
        import_rows(self.db, actor=self.owner, source_name="s", period="1", rows=[row("A"), row("B")], clock=self.clock)
        import_rows(self.db, actor=self.owner, source_name="s", period="2", rows=[row("A")], clock=self.clock)
        import_rows(self.db, actor=self.owner, source_name="s", period="3", rows=[row("A"), row("C")], clock=self.clock)
        self.assertEqual(self.q("SELECT COUNT(*) FROM human_action_requests")[0][0], 1)

    # C3 -------------------------------------------------------------------------------------
    def test_requester_sees_official_due_move_but_not_internal_dates(self):
        mine = self.apply(self.gao, "g", {"op": "create_task", "title": "Plan", "evidence_type": "explicit_user",
                                          "official_due_at": "2026-11-30T09:00:00Z"})[0]["task_id"]
        self.svc.changes_since(actor=self.gao, view="v", mark_seen=True)
        self.apply(self.owner, "s", {"op": "patch_task", "task_id": mine, "expected_version": 1,
                                     "fields": {"soft_due_at": "2026-11-25T09:00:00Z"}, "evidence_type": "explicit_user"})
        self.apply(self.owner, "o", {"op": "set_official_due", "task_id": mine, "expected_version": 2, "local": "2026-12-05",
                                     "delay_kind": "external_wait", "reason": "client asked", "evidence_type": "explicit_user",
                                     "source_ref": SRC})
        events = self.svc.changes_since(actor=self.gao, view="v")["events"]
        self.assertEqual([e["kind"] for e in events], ["delayed"])
        self.assertNotIn("after_json", events[0])

    # B3 -------------------------------------------------------------------------------------
    def test_recurring_occurrence_inherits_owner_approved_delegation(self):
        DelegationProfileAdmin(self.db, clock=self.clock).publish(
            owner=self.owner, work_class="weekly", max_autonomy_level="agent_supervised", allowed_actions=["draft"],
            reversible_actions=["draft"])
        tpl = {"title": "Weekly report", "delegate": {"executor_id": "agent", "work_class": "weekly",
                                                       "autonomy_level": "agent_bounded"}}
        with self.assertRaises(Forbidden):
            self.apply(self.agent, "s0", {"op": "create_schedule", "title": "W", "frequency": "weekly", "local_time": "09:00",
                                          "start_local_date": "2026-11-20", "template": tpl, "evidence_type": "explicit_user"})
        self.apply(self.owner, "s", {"op": "create_schedule", "title": "W", "frequency": "weekly", "local_time": "09:00",
                                     "start_local_date": "2026-11-20", "template": tpl, "evidence_type": "explicit_user"})
        self.clock.now = "2026-11-20T01:30:00Z"
        occ = fire_due(self.db, now=self.clock.now)["schedule_occurrences"][0]
        resp = self.svc.read(actor=self.owner, task_id=occ["task_id"])["responsibility"]
        self.assertEqual((resp["current_executor_id"], resp["autonomy_level"]), ("agent", "agent_supervised"))  # capped

    # B5 / B6 / B7 ---------------------------------------------------------------------------
    def test_recently_done_schedule_suggestions_and_promotion_notice(self):
        t = self.task()
        self.apply(self.owner, "d", {"op": "transition", "task_id": t, "expected_version": 1, "to": "DONE",
                                     "evidence_type": "explicit_user", "source_ref": SRC, "reason": "done"})
        self.assertEqual([x["id"] for x in self.svc.overview(actor=self.owner, limit=10)["recently_done"]], [t])
        for i in range(4):
            self.task(key=f"s{i}", soft_due_at="2026-11-20T09:00:00Z")
        load = schedule_load(str(self.db), actor=self.owner, now=NOW, max_per_day=3)
        self.assertEqual(load["overloaded_days"], ["2026-11-20"])
        self.assertEqual(len(load["suggestions"]), 1)
        self.assertLess(load["suggestions"][0]["to"], "2026-11-20")

        admin = DelegationProfileAdmin(self.db, clock=self.clock)
        policy = {"min_verified_successes": 1, "min_acceptance_ratio": 1.0, "max_rework_ratio": 0.0}
        admin.publish(owner=self.owner, work_class="r", max_autonomy_level="agent_bounded", allowed_actions=["a"],
                      promotion_policy=policy, auto_promotion_enabled=True)
        p = self.task(key="promo")
        self.apply(self.owner, "as", {"op": "assign_responsibility", "task_id": p, "expected_version": 1, "executor_id": "agent",
                                      "work_class": "r", "autonomy_level": "agent_prepares", "evidence_type": "explicit_user",
                                      "source_ref": SRC, "reason": "try"})
        did = self.apply(self.agent, "dl", {"op": "register_deliverable", "task_id": p, "expected_version": 2, "title": "R",
                                            "artifact_ref": "f", "evidence_type": "explicit_user"})[0]["deliverable_id"]
        self.apply(self.agent, "sb", {"op": "update_deliverable", "deliverable_id": did, "expected_version": 3, "status": "SUBMITTED",
                                      "evidence_type": "verified_tool", "receipt_ref": self.sent_delivery(p)})
        self.apply(self.agent, "ac", {"op": "update_deliverable", "deliverable_id": did, "expected_version": 4, "status": "ACCEPTED",
                                      "evidence_type": "explicit_user", "source_ref": self.inbox_from("owner", 7)})
        self.assertEqual(review_delegations(self.db, now=NOW), [{"task_id": p, "promoted_to": "agent_supervised"}])
        self.assertTrue(any("自动升级" in json.loads(r[0]).get("text", "") for r in self.q("SELECT payload_json FROM delivery_outbox")))
        self.assertIn("responsibility_changed", [e["kind"] for e in self.svc.read(actor=self.owner, task_id=p)["events"]])


if __name__ == "__main__":
    unittest.main()
