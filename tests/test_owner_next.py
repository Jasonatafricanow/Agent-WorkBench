"""Tests for shared next_for_owner priority ranking and resume-note visibility."""
from test_framework import Base

from workdesk import ActorContext, DelegationProfileAdmin, WorkbenchTools
from workdesk.dashboard import render_dashboard


class OwnerNextRankingTests(Base):
    def test_ranked_next_excludes_now_and_delegated_and_shows_resume_note(self):
        tools = WorkbenchTools(self.agent, self.svc)
        now_task = self.task("now", priority=0)
        later = self.task("later", priority=0)
        today = self.task("today", priority=3, official_due_at="2026-11-17T14:00:00Z")
        overdue = self.task("late", priority=4, soft_due_at="2026-11-16T09:00:00Z")
        next2 = self.task("next2", priority=2)
        next1 = self.task("next1", priority=2)
        self.task("delegated-assignee", assignee_id="agent", priority=0)

        # Active responsibility delegated to agent is also excluded from owner's queue.
        DelegationProfileAdmin(self.db, clock=self.clock).publish(
            owner=self.owner, work_class="w", max_autonomy_level="agent_bounded", allowed_actions=["act"]
        )
        delegated_resp = self.task("delegated-resp", priority=0)
        self.apply(self.owner, "resp", {
            "op": "assign_responsibility", "task_id": delegated_resp, "expected_version": 1,
            "executor_id": "agent", "work_class": "w", "autonomy_level": "agent_bounded",
            "evidence_type": "explicit_user", "source_ref": self.inbox_from("owner"), "reason": "delegate",
        })

        self.apply(self.owner, "focus-now", {"op": "set_focus", "slot": "NOW", "task_id": now_task,
                                             "evidence_type": "explicit_user"})
        self.apply(self.owner, "focus-n2", {"op": "set_focus", "slot": "NEXT2", "task_id": next2,
                                            "evidence_type": "explicit_user"})
        self.apply(self.owner, "focus-n1", {"op": "set_focus", "slot": "NEXT1", "task_id": next1,
                                            "evidence_type": "explicit_user"})

        ranked = [t["id"] for t in tools.list_tasks()["next_for_owner"]]
        self.assertEqual(ranked, [overdue, today, later, next1, next2])

        # Progress note is exposed through get_task and Today pane resume line.
        record = tools.get_task(task_id=later)["task"]
        tools.change_tasks(idempotency_key="agent-progress", operations=[{
            "op": "append_progress", "task_id": later, "expected_version": record["version"],
            "text": "已核对第 13 行", "next_action": "继续第 14 行", "evidence_type": "explicit_user",
        }])
        updated = tools.get_task(task_id=later)["task"]
        self.assertIn("已核对第 13 行", updated["done_summary"])
        self.assertEqual(updated["next_action"], "继续第 14 行")

        html_text = render_dashboard(self.svc, self.owner, self.db, self.clock())
        self.assertIn("接下来（按紧迫度排序）", html_text)
        self.assertIn("上次记下：已核对第 13 行", html_text)

    def test_requester_overview_omits_next_for_owner(self):
        self.task("owner-task", priority=0)
        gao = ActorContext("ws", "gao", "requester", "telegram")
        ov = self.svc.overview(actor=gao, limit=20)
        self.assertEqual(ov["view"], "requester")
        self.assertNotIn("next_for_owner", ov)
