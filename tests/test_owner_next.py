"""Tests for shared next_for_owner ranking, web owner completions, and monthly grid calendar."""
import re
import sqlite3
from contextlib import closing

from test_framework import NOW, Base
import test_owner_console

from workdesk import ActorContext, DelegationProfileAdmin, WorkbenchTools
from workdesk.approvals import ApprovalService
from workdesk.dashboard import render_dashboard
from workdesk.identity import bind_identity


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


class OwnerWebCompletionTests(test_owner_console.OwnerConsoleTests):
    def test_create_and_edit_project_and_official_due_on_web(self):
        proj = self.apply(self.owner, "proj-1", {
            "op": "create_project", "title": "财务对账", "evidence_type": "explicit_user",
        })[0]["project_id"]
        home = self.get("/", self.owner_auth)
        self.assertIn("财务对账", home)

        status, loc = self.post_form(self.form(
            nonce="c-off", action="create", title="季度汇算", state="READY",
            project_id=proj, official_due="2026-11-25",
        ))
        self.assertEqual(status, 303)
        self.assertIn("ok=", loc)
        (tid, pid, off_local, version), = self.q(
            "SELECT id, project_id, official_due_local, version FROM tasks WHERE title='季度汇算'")
        self.assertEqual((pid, off_local), (proj, "2026-11-25"))

        status, loc = self.post_form(self.form(
            nonce="e-off", action="edit", task_id=tid, version=str(version), title="季度汇算",
            has_project="1", project_id="", prev_official_due="2026-11-25", official_due="2026-11-28",
            delay_kind="external_wait", reason="等审计资料",
        ))
        self.assertIn("ok=", loc)
        (pid2, off_local2), = self.q("SELECT project_id, official_due_local FROM tasks WHERE id=?", tid)
        self.assertIsNone(pid2)
        self.assertEqual(off_local2, "2026-11-28")

    def test_today_pane_actions_approvals_human_replies_and_monthly_grid(self):
        bind_identity(self.db, provider="telegram", bot_id="bot", external_user_id="1", principal_id="owner")
        task = self.task("t1", title="核对合同", state="READY", official_due_at="2026-11-20T09:00:00Z")
        with closing(sqlite3.connect(self.db)) as c, c:
            c.execute(
                "INSERT INTO wake_events(id,workspace_id,origin,origin_event_id,kind,source_ref,created_at,updated_at)"
                " VALUES('w1','ws','o','1','user_message','s',?,?)", (NOW, NOW))
        approvals = ApprovalService(self.db, clock=self.clock)
        apr = approvals.request(
            host_actor=self.agent, wake_event_id="w1", tool_name="send_external_message",
            tool_input={"task_id": task, "text": "hi"}, summary="外发给客户",
            host_deferred_ref="sess-1#call-1", expires_at="2026-11-20T00:00:00Z", task_id=task,
        )["approval_id"]
        hum = self.apply(self.agent, "hum-req", {
            "op": "request_human_action", "task_id": task, "expected_version": 1,
            "request_kind": "decision", "title": "确认是否采用旧报价", "urgency": "batch",
            "evidence_type": "system",
        })[0]["human_action_id"]

        owner_home = self.get("/", self.owner_auth)
        self.assertIn("设为 NOW", owner_home)
        self.assertIn("批准", owner_home)
        self.assertIn("确认处理", owner_home)

        viewer_home = self.get("/", self.viewer_auth)
        self.assertIsNone(re.search(r"method\s*=\s*['\"]?post", viewer_home, re.I))

        # Approve on web console and verify Host deferred consumption works.
        status, loc = self.post_form(self.form(nonce="apr-ok", action="approval", approval_id=apr, approve="1"))
        self.assertIn("ok=", loc)
        self.assertEqual(self.q("SELECT status, decision_source_ref FROM approval_requests WHERE id=?", apr),
                         [("approved", "owner-web:apr-ok")])
        self.assertEqual(self.q("SELECT kind, status FROM wake_events WHERE origin_event_id=?", apr),
                         [("approval_decided", "pending")])
        self.assertIsNotNone(approvals.consume(
            workspace_id="ws", tool_name="send_external_message",
            tool_input={"task_id": task, "text": "hi"}, session_ref="sess-1", task_id=task,
        ))

        # Reply to human action on web console: wake_event must be pending (not inline).
        status, loc = self.post_form(self.form(
            nonce="hum-ok", action="human_reply", human_action_id=hum, status="completed", reply="用旧报价",
        ))
        self.assertIn("ok=", loc)
        self.assertEqual(self.q("SELECT status, response_source_ref FROM human_action_requests WHERE id=?", hum),
                         [("completed", "owner-web:hum-ok")])
        self.assertEqual(self.q("SELECT kind, status FROM wake_events WHERE origin_event_id=?", hum),
                         [("human_action_completed", "pending")])

        # Promote from Today pane button.
        status, loc = self.post_form(self.form(nonce="now-ok", action="focus", slot="NOW", task_id=task))
        self.assertIn("ok=", loc)
        self.assertEqual(self.q("SELECT task_id FROM task_focus WHERE slot='NOW'"), [(task,)])

        # Monthly calendar uses 7-column grid.
        month_html = self.get("/calendar?range=month", self.owner_auth)
        self.assertIn("cal-grid", month_html)
        self.assertIn("周一", month_html)
        self.assertIn("核对合同", month_html)


for _name in [n for n in dir(test_owner_console.OwnerConsoleTests) if n.startswith("test_")]:
    setattr(OwnerWebCompletionTests, _name, None)
