"""End-to-end tests for owner web console completion features."""
import re
import sqlite3
import urllib.error
import urllib.parse
from contextlib import closing

from test_framework import NOW
import test_owner_console

from workdesk import ActorContext, DelegationProfileAdmin


class OwnerWebFeatureCompletionTests(test_owner_console.OwnerConsoleTests):
    def test_projects_create_edit_and_viewer_readonly(self):
        status, loc = self.post_form(self.form(
            nonce="prj-c1", back="/projects", action="create_project", title="年终总结", goal="完成复盘与汇报",
        ))
        self.assertEqual(status, 303)
        self.assertIn("ok=", loc)
        (pid, state), = self.q("SELECT id, state FROM projects WHERE title='年终总结'")
        self.assertEqual(state, "active")

        proj_page = self.get("/projects", self.owner_auth)
        self.assertIn("年终总结", proj_page)
        self.assertIn("完成复盘与汇报", proj_page)
        self.assertIn("update_project", proj_page)

        status, loc = self.post_form(self.form(
            nonce="prj-u1", back="/projects", action="update_project", project_id=pid,
            title="年终总结2026", goal="含数据附件", state="paused",
        ))
        self.assertIn("ok=", loc)
        self.assertEqual(self.q("SELECT title, goal, state FROM projects WHERE id=?", pid),
                         [("年终总结2026", "含数据附件", "paused")])

        viewer_proj = self.get("/projects", self.viewer_auth)
        self.assertIn("年终总结2026", viewer_proj)
        self.assertIsNone(re.search(r"method\s*=\s*['\"]?post", viewer_proj, re.I))
        self.assertNotIn("<script", viewer_proj)
        self.assertEqual(self.post_form(
            self.form(nonce="prj-v", action="create_project", title="bad"), auth=self.viewer_auth,
        )[0], 403)

    def test_planned_start_and_adopt_schedule_suggestion(self):
        status, loc = self.post_form(self.form(
            nonce="plan-c", action="create", title="预算初稿", state="READY",
            planned_start="2026-11-18T10:00", soft_due="2026-11-20T17:00",
        ))
        self.assertIn("ok=", loc)
        (tid,), = self.q("SELECT id FROM tasks WHERE title='预算初稿'")
        self.assertIn("计划开始", self.get(f"/task/{tid}", self.owner_auth))
        self.assertIn("计划", self.get("/calendar", self.owner_auth))

        # 3 more tasks on 2026-11-20 triggers overload (>3/day) and generates a suggestion.
        for i in range(3):
            self.task(f"load-{i}", soft_due_at="2026-11-20T09:00:00Z")
        home = self.get("/", self.owner_auth)
        self.assertIn("采纳：改为", home)

        # Adopt the suggestion moving 预算初稿 earlier; official_due stays untouched.
        target_match = re.search(
            r"name=task_id value='" + re.escape(tid) + r"'.*?name=soft_due value='([^']+)'.*?name=target_date value='([^']+)'",
            home,
        )
        self.assertIsNotNone(target_match, home)
        target_local, target_date = target_match.group(1), target_match.group(2)
        self.assertLess(target_date, "2026-11-20")
        status, loc = self.post_form(self.form(
            nonce="adopt-1", action="adopt_soft_due", task_id=tid, version="1",
            soft_due=target_local, target_date=target_date,
        ))
        self.assertIn("ok=", loc)
        (new_soft,), = self.q("SELECT soft_due_at FROM tasks WHERE id=?", tid)
        self.assertTrue(new_soft.startswith(target_date))

    def test_human_action_context_and_atomic_human_apply(self):
        tid = self.task("prop-t", title="年度报税", official_due_at="2026-11-20T15:59:59Z", priority=2)
        with closing(sqlite3.connect(self.db)) as c, c:
            c.execute("UPDATE tasks SET official_due_local='2026-11-20' WHERE id=?", (tid,))
        hid = self.apply(self.agent, "prop-1", {
            "op": "propose_change", "task_id": tid, "fields": {"official_due_local": "2026-11-25", "priority": 0},
            "reason": "对方书面延到 25 号", "evidence_type": "reported_third_party",
        })[0]["human_action_id"]

        home = self.get("/", self.owner_auth)
        self.assertIn("2026-11-20 → 2026-11-25", home)
        self.assertIn("同意并应用修改", home)

        # Stale version must fail without consuming the human action.
        _, loc_stale = self.post_form(self.form(
            nonce="ha-stale", action="human_apply", human_action_id=hid, version="1", delay_kind="external_wait",
        ))
        self.assertIn("err=", loc_stale)
        self.assertEqual(self.q("SELECT status FROM human_action_requests WHERE id=?", hid), [("pending",)])

        # Current version (2 after propose_change) succeeds atomically and bumps to 3.
        status, loc = self.post_form(self.form(
            nonce="ha-1", action="human_apply", human_action_id=hid, version="2", delay_kind="external_wait",
        ))
        self.assertIn("ok=", loc)
        self.assertEqual(self.q("SELECT status FROM human_action_requests WHERE id=?", hid), [("completed",)])
        self.assertEqual(self.q("SELECT official_due_local, priority, version FROM tasks WHERE id=?", tid),
                         [("2026-11-25", 0, 3)])
        self.assertEqual(self.q("SELECT kind, status FROM wake_events WHERE origin_event_id=?", hid),
                         [("human_action_completed", "pending")])

        # Idempotent retry with exact same nonce succeeds cleanly without acting twice.
        status2, loc2 = self.post_form(self.form(
            nonce="ha-1", action="human_apply", human_action_id=hid, version="2", delay_kind="external_wait",
        ))
        self.assertEqual(status2, 303)
        self.assertIn("ok=", loc2)
        self.assertEqual(self.q("SELECT version FROM tasks WHERE id=?", tid), [(3,)])

        # Fresh nonce against already-completed request fails cleanly.
        status3, loc3 = self.post_form(self.form(
            nonce="ha-2", action="human_apply", human_action_id=hid, version="3", delay_kind="external_wait",
        ))
        self.assertIn("err=", loc3)

    def test_unknown_outcomes_reconciliation(self):
        tid = self.task("unk-t")
        with closing(sqlite3.connect(self.db)) as c, c:
            c.execute(
                "INSERT INTO wake_events(id,workspace_id,task_id,origin,origin_event_id,kind,source_ref,status,created_at,updated_at)"
                " VALUES('w-unk','ws',?,'workbench.timer','r-u','timer_fired','s','unknown',?,?)",
                (tid, NOW, NOW),
            )
            c.execute(
                "INSERT INTO delivery_outbox(id,workspace_id,task_id,recipient_principal_id,channel,payload_json,status,dedupe_key,created_at,updated_at)"
                " VALUES('d-unk','ws',?,'owner','telegram','{\"text\":\"hi\"}','unknown','dk-unk',?,?)",
                (tid, NOW, NOW),
            )
        home = self.get("/", self.owner_auth)
        self.assertIn("w-unk", home)
        self.assertIn("d-unk", home)

        # Marking delivery sent without receipt is rejected.
        _, loc_bad = self.post_form(self.form(
            nonce="rec-bad", action="reconcile_delivery", delivery_id="d-unk", status="sent", receipt="",
        ))
        self.assertIn("err=", loc_bad)
        self.assertEqual(self.q("SELECT status FROM delivery_outbox WHERE id='d-unk'"), [("unknown",)])

        # With receipt -> settled sent; wake -> settled failed.
        _, loc_d = self.post_form(self.form(
            nonce="rec-d", action="reconcile_delivery", delivery_id="d-unk", status="sent", receipt="tg-999",
        ))
        self.assertIn("ok=", loc_d)
        self.assertEqual(self.q("SELECT status, provider_message_id FROM delivery_outbox WHERE id='d-unk'"),
                         [("sent", "tg-999")])

        _, loc_w = self.post_form(self.form(
            nonce="rec-w", action="reconcile_wake", event_id="w-unk", status="failed",
        ))
        self.assertIn("ok=", loc_w)
        self.assertEqual(self.q("SELECT status FROM wake_events WHERE id='w-unk'"), [("failed",)])

    def test_task_detail_lifecycle_and_rail_search(self):
        proj = self.apply(self.owner, "p1", {"op": "create_project", "title": "合同跟进", "evidence_type": "explicit_user"})[0]["project_id"]
        DelegationProfileAdmin(self.db, clock=self.clock).publish(
            owner=self.owner, work_class="doc", max_autonomy_level="agent_bounded", allowed_actions=["edit"],
        )
        tid = self.task("dep-t", title="审阅合同条款", project_id=proj)
        self.task("other-t", title="买办公用品")
        dep_id = self.apply(
            self.owner, "dep-add",
            {"op": "add_dependency", "task_id": tid, "expected_version": 1, "kind": "external_response",
             "awaited_outcome": "高老师盖章反馈", "holder_name": "高老师", "evidence_type": "explicit_user"},
            {"op": "transition", "task_id": tid, "to": "WAITING", "evidence_type": "explicit_user"},
            {"op": "assign_responsibility", "task_id": tid, "executor_id": "agent", "work_class": "doc",
             "autonomy_level": "agent_bounded", "evidence_type": "explicit_user", "source_ref": "cli:1", "reason": "delegate"},
            {"op": "add_watch", "task_id": tid, "source_kind": "github_pr", "target_ref": "org/repo#12",
             "expected_value": "merged", "evidence_type": "explicit_user"},
        )[0]["dependency_id"]

        # Rail search + project filter + cross-workspace project 404.
        filtered = self.get("/?q=" + urllib.parse.quote("高老师") + f"&project={proj}", self.owner_auth)
        rail_html = re.search(r"<aside class=rail>.*?</aside>", filtered, re.S).group(0)
        self.assertIn("审阅合同条款", rail_html)
        self.assertNotIn("买办公用品", rail_html)
        with self.assertRaises(urllib.error.HTTPError) as err:
            self.get("/?project=non-existent", self.owner_auth)
        self.assertEqual(err.exception.code, 404)

        task_page = self.get(f"/task/{tid}", self.owner_auth)
        for text in ("等待与依赖", "高老师盖章反馈", "委托状态", "外部监控", "org/repo#12"):
            self.assertIn(text, task_page)

        # Viewer sees details cleanly without POST forms or scripts.
        viewer_task = self.get(f"/task/{tid}", self.viewer_auth)
        self.assertIn("org/repo#12", viewer_task)
        self.assertIsNone(re.search(r"method\s*=\s*['\"]?post", viewer_task, re.I))
        self.assertNotIn("<script", viewer_task)

        # 1. Resolve dependency -> transitions WAITING -> READY atomically.
        ver = self.q("SELECT version FROM tasks WHERE id=?", tid)[0][0]
        _, loc = self.post_form(self.form(
            nonce="res-dep", back=f"/task/{tid}?q=合同", action="resolve_dep", task_id=tid, version=str(ver),
            dependency_id=dep_id, auto_ready="1", reason="高老师已邮件确认",
        ))
        self.assertIn("ok=", loc)
        self.assertIn("q=", loc)
        self.assertEqual(self.q("SELECT state FROM tasks WHERE id=?", tid), [("READY",)])
        self.assertEqual(self.q("SELECT status FROM dependencies WHERE id=?", dep_id), [("resolved",)])

        # 2. Set and cancel reminder.
        ver = self.q("SELECT version FROM tasks WHERE id=?", tid)[0][0]
        _, loc = self.post_form(self.form(
            nonce="rem-1", back=f"/task/{tid}", action="set_reminder", task_id=tid, version=str(ver),
            local_at="2026-11-22T10:00", kind="followup", recipient_id="owner",
            repeat_every_days="3", max_repeats="2",
        ))
        self.assertIn("ok=", loc)
        (rem_id,), = self.q("SELECT id FROM reminders WHERE task_id=? AND status='scheduled'", tid)
        ver = self.q("SELECT version FROM tasks WHERE id=?", tid)[0][0]
        _, loc = self.post_form(self.form(
            nonce="rem-2", back=f"/task/{tid}", action="cancel_reminder", task_id=tid, version=str(ver),
            reminder_id=rem_id,
        ))
        self.assertIn("ok=", loc)
        self.assertEqual(self.q("SELECT status FROM reminders WHERE id=?", rem_id), [("cancelled",)])

        # 3. Record commitment and mark kept.
        ver = self.q("SELECT version FROM tasks WHERE id=?", tid)[0][0]
        _, loc = self.post_form(self.form(
            nonce="com-1", back=f"/task/{tid}", action="record_commitment", task_id=tid, version=str(ver),
            owner_id="owner", direction="owed_to_owner", other_id="gao", promise_text="周五前寄出纸质合同",
            due_local="2026-11-21",
        ))
        self.assertIn("ok=", loc)
        (cid,), = self.q("SELECT id FROM commitments WHERE task_id=?", tid)
        ver = self.q("SELECT version FROM tasks WHERE id=?", tid)[0][0]
        _, loc = self.post_form(self.form(
            nonce="com-2", back=f"/task/{tid}", action="commitment_status", task_id=tid, version=str(ver),
            commitment_id=cid, status="kept",
        ))
        self.assertIn("ok=", loc)
        self.assertEqual(self.q("SELECT status FROM commitments WHERE id=?", cid), [("kept",)])

        # 4. Register deliverable & toggle responsibility status.
        ver = self.q("SELECT version FROM tasks WHERE id=?", tid)[0][0]
        _, loc = self.post_form(self.form(
            nonce="dlv-1", back=f"/task/{tid}", action="register_deliverable", task_id=tid, version=str(ver),
            title="合同终版 PDF", artifact_ref="docs/contract-final.pdf",
        ))
        self.assertIn("ok=", loc)
        self.assertEqual(self.q("SELECT title, status FROM deliverables WHERE task_id=?", tid),
                         [("合同终版 PDF", "DRAFT")])

        ver = self.q("SELECT version FROM tasks WHERE id=?", tid)[0][0]
        _, loc = self.post_form(self.form(
            nonce="rsp-p", back=f"/task/{tid}", action="responsibility_status", task_id=tid, version=str(ver),
            status="paused", reason="暂时接管",
        ))
        self.assertIn("ok=", loc)
        self.assertEqual(self.q("SELECT status FROM task_responsibilities WHERE task_id=?", tid), [("paused",)])

        ver = self.q("SELECT version FROM tasks WHERE id=?", tid)[0][0]
        _, loc = self.post_form(self.form(
            nonce="rsp-a", back=f"/task/{tid}", action="responsibility_status", task_id=tid, version=str(ver),
            status="active", reason="继续交给工作bot",
        ))
        self.assertIn("ok=", loc)
        self.assertEqual(self.q("SELECT status FROM task_responsibilities WHERE task_id=?", tid), [("active",)])

        final_task_page = self.get(f"/task/{tid}", self.owner_auth)
        self.assertIn("周五前寄出纸质合同", final_task_page)
        self.assertIn("合同终版 PDF", final_task_page)


for _name in [n for n in dir(test_owner_console.OwnerConsoleTests) if n.startswith("test_")]:
    setattr(OwnerWebFeatureCompletionTests, _name, None)
