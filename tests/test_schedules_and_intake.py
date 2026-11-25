"""Tests for /schedules (schedules + watches), inbound attachments, and source retraction."""
import json
import re
import sqlite3
import urllib.error
import urllib.request
from contextlib import closing

from test_framework import NOW, SRC
import test_owner_console

from workdesk import ActorContext, views
from workdesk.approvals import ApprovalService
from workdesk.errors import EvidenceRequired, Forbidden, NotFound
from workdesk.identity import bind_identity, record_edit, record_inbound, record_retract
from workdesk.scheduler import fire_due


class SchedulesWatchesAttachmentsRetractionTests(test_owner_console.OwnerConsoleTests):
    def host_post(self, path, body):
        req = urllib.request.Request(
            self.base + path,
            data=json.dumps(body).encode(),
            method="POST",
            headers={"Authorization": "Bearer " + "t" * 32, "Content-Type": "application/json"},
        )
        try:
            with urllib.request.urlopen(req, timeout=10) as r:
                return r.status, json.loads(r.read())
        except urllib.error.HTTPError as exc:
            return exc.code, json.loads(exc.read() or b"{}")

    def test_schedules_and_watches_web_lifecycle_and_acl(self):
        gao = ActorContext("ws", "gao", "requester", "telegram")
        with self.assertRaises(Forbidden):
            views.schedules(self.db, actor=gao)
        with self.assertRaises(urllib.error.HTTPError) as foreign_err:
            self.get("/schedules?project=foreign-prj", self.owner_auth)
        self.assertEqual(foreign_err.exception.code, 404)

        proj = self.apply(self.owner, "p1", {
            "op": "create_project", "title": "例行运维", "evidence_type": "explicit_user",
        })[0]["project_id"]

        # Create schedule on web with project binding.
        status, loc = self.post_form(self.form(
            nonce="sch-c", back="/schedules", action="create_schedule",
            title="每日巡检", frequency="daily", interval="1",
            start_local_date="2026-11-17", local_time="09:00",
            project_id=proj, next_action="检查队列", priority="1", due_at_occurrence="1", max_occurrences="5",
        ))
        self.assertEqual(status, 303)
        self.assertIn("ok=", loc)
        (sid, active), = self.q("SELECT id, active FROM schedule_rules WHERE title='每日巡检'")
        self.assertEqual(active, 1)

        # Fire the due schedule -> created occurrence inherits project_id.
        fired = fire_due(self.db, now="2026-11-17T02:00:00Z")["schedule_occurrences"]
        self.assertEqual(len(fired), 1)
        tid = fired[0]["task_id"]
        self.assertEqual(self.q("SELECT project_id FROM tasks WHERE id=?", tid), [(proj,)])

        # Toggle schedule off and on via web form.
        _, loc = self.post_form(self.form(
            nonce="sch-off", back="/schedules", action="toggle_schedule", schedule_id=sid, active="0",
        ))
        self.assertIn("ok=", loc)
        self.assertEqual(self.q("SELECT active FROM schedule_rules WHERE id=?", sid), [(0,)])

        # Add watch from /schedules (without explicit version) and another from /task/<id>.
        _, loc = self.post_form(self.form(
            nonce="wat-1", back="/schedules", action="add_watch",
            task_id=tid, source_kind="github_pr", target_ref="org/repo#9", expected_value="merged",
        ))
        self.assertIn("ok=", loc)
        (wid1,), = self.q("SELECT id FROM external_watches WHERE target_ref='org/repo#9'")

        sch_page = self.get("/schedules", self.owner_auth)
        self.assertIn("每日巡检", sch_page)
        self.assertIn("org/repo#9", sch_page)
        self.assertIn("例行运维", sch_page)

        ver = self.q("SELECT version FROM tasks WHERE id=?", tid)[0][0]
        _, loc = self.post_form(self.form(
            nonce="wat-2", back=f"/task/{tid}", action="add_watch", task_id=tid, version=str(ver),
            source_kind="github_ci", target_ref="org/repo@main", expected_value="success",
        ))
        self.assertIn("ok=", loc)
        (wid2,), = self.q("SELECT id FROM external_watches WHERE target_ref='org/repo@main'")

        # Cancel watches from both /schedules and /task/<id>.
        ver = self.q("SELECT version FROM tasks WHERE id=?", tid)[0][0]
        _, loc = self.post_form(self.form(
            nonce="wat-c1", back="/schedules", action="cancel_watch", watch_id=wid1, task_id=tid, version=str(ver),
        ))
        self.assertIn("ok=", loc)
        self.assertEqual(self.q("SELECT status FROM external_watches WHERE id=?", wid1), [("cancelled",)])

        ver = self.q("SELECT version FROM tasks WHERE id=?", tid)[0][0]
        _, loc = self.post_form(self.form(
            nonce="wat-c2", back=f"/task/{tid}", action="cancel_watch", watch_id=wid2, task_id=tid, version=str(ver),
        ))
        self.assertIn("ok=", loc)
        self.assertEqual(self.q("SELECT status FROM external_watches WHERE id=?", wid2), [("cancelled",)])
        kinds = [e["kind"] for e in self.svc.read(actor=self.owner, task_id=tid)["events"]]
        self.assertIn("watch_cancelled", kinds)

        # Viewer page is strictly read-only and POSTs fail closed with 403.
        viewer_sch = self.get("/schedules", self.viewer_auth)
        self.assertIn("每日巡检", viewer_sch)
        self.assertIsNone(re.search(r"method\s*=\s*['\"]?post", viewer_sch, re.I))
        self.assertNotIn("<script", viewer_sch)
        self.assertEqual(self.post_form(
            self.form(nonce="v-sch", back="/schedules", action="toggle_schedule", schedule_id=sid, active="1"),
            auth=self.viewer_auth,
        )[0], 403)

    def test_inbound_attachments_linked_to_task_and_rendered(self):
        bind_identity(self.db, provider="telegram", bot_id="workbot", external_user_id="111", principal_id="gao")
        # Bad attachment fails atomically before storing inbox row.
        bad_status, _ = self.host_post("/v1/intake/inbound", {
            "workspace_id": "ws", "platform": "telegram", "bot_id": "workbot", "update_id": "att-bad",
            "chat_id": "111", "message_id": "90", "external_user_id": "111",
            "raw_source_ref": "tg://workbot/111/90",
            "attachments": [{"kind": "executable", "file_ref": "x"}],
        })
        self.assertEqual(bad_status, 422)
        self.assertEqual(self.q("SELECT COUNT(*) FROM inbox_messages WHERE update_id='att-bad'"), [(0,)])

        # Valid document + photo attachment stored with inbound message.
        status, res = self.host_post("/v1/intake/inbound", {
            "workspace_id": "ws", "platform": "telegram", "bot_id": "workbot", "update_id": "att-1",
            "chat_id": "111", "message_id": "91", "external_user_id": "111",
            "raw_source_ref": "tg://workbot/111/91",
            "attachments": [
                {"kind": "document", "file_name": "spec.pdf", "mime_type": "application/pdf",
                 "file_size": 2048, "file_ref": "tg-doc-file-1", "caption": "补充材料"},
                {"kind": "photo", "file_ref": "tg-photo-file-2"},
            ],
        })
        self.assertEqual(status, 200, res)
        inbox_id = res["result"]["inbox_id"]
        self.assertEqual(res["result"]["attachments"], 2)

        gao = ActorContext("ws", "gao", "requester", "telegram")
        tid = self.apply(gao, "req-att", {
            "op": "create_task", "title": "合同复核", "evidence_type": "explicit_user",
        })[0]["task_id"]
        self.apply(gao, "link-att", {
            "op": "link_inbox", "task_id": tid, "inbox_id": inbox_id, "relation": "request",
            "evidence_type": "system",
        })

        full = self.svc.read(actor=self.owner, task_id=tid)
        self.assertEqual(len(full["sources"]), 1)
        self.assertEqual([a["file_name"] for a in full["sources"][0]["attachments"]], ["spec.pdf", ""])
        req_view = self.svc.read(actor=gao, task_id=tid)
        self.assertNotIn("sources", req_view)

        task_html = self.get(f"/task/{tid}", self.owner_auth)
        self.assertIn("关联消息与附件", task_html)
        self.assertIn("spec.pdf", task_html)
        self.assertIn("tg-photo-file-2", task_html)

    def test_retracted_message_invalidates_evidence_without_deleting_events(self):
        with closing(sqlite3.connect(self.db)) as c, c:
            c.execute("INSERT OR IGNORE INTO principals(id,display_name,kind,created_at) VALUES('li','Li','human',?)", (NOW,))
            c.execute("INSERT OR IGNORE INTO memberships(workspace_id,principal_id,role) VALUES('ws','li','requester')")
        bind_identity(self.db, provider="telegram", bot_id="workbot", external_user_id="111", principal_id="gao")
        bind_identity(self.db, provider="telegram", bot_id="workbot", external_user_id="222", principal_id="owner")
        bind_identity(self.db, provider="telegram", bot_id="workbot", external_user_id="333", principal_id="li")

        in_res = record_inbound(
            self.db, workspace_id="ws", platform="telegram", bot_id="workbot", update_id="r-1",
            chat_id="111", message_id="501", external_user_id="111",
            raw_source_ref="tg://workbot/111/501", clock=self.clock,
        )
        gao_src = "tg://workbot/111/501"
        tid = self.task("ret-t", requester_id="gao")
        self.apply(self.owner, "link-r", {
            "op": "link_inbox", "task_id": tid, "inbox_id": in_res["inbox_id"], "relation": "request",
            "evidence_type": "system",
        })
        # Deliverable submitted so later acceptance check exercises retracted message blocking.
        did = self.apply(self.agent, "dlv-r", {
            "op": "register_deliverable", "task_id": tid, "expected_version": 2,
            "title": "D", "artifact_ref": "file://d", "evidence_type": "explicit_user",
        })[0]["deliverable_id"]
        self.apply(self.agent, "sub-r", {
            "op": "update_deliverable", "deliverable_id": did, "expected_version": 3,
            "status": "SUBMITTED", "evidence_type": "verified_tool", "receipt_ref": self.sent_delivery(tid),
        })

        # Unrelated user cannot retract someone else's message.
        denied = record_retract(
            self.db, workspace_id="ws", platform="telegram", bot_id="workbot", update_id="r-del-bad",
            chat_id="111", message_id="501", external_user_id="333",
            raw_source_ref="tg://workbot/111/501?retract=1", clock=self.clock,
        )
        self.assertFalse(denied["accepted"])

        event_count_before = self.q("SELECT COUNT(*) FROM task_events WHERE task_id=?", tid)[0][0]
        # Host posts deletion notice on /v1/intake/retracted.
        status, ret = self.host_post("/v1/intake/retracted", {
            "workspace_id": "ws", "platform": "telegram", "bot_id": "workbot", "update_id": "r-del-1",
            "chat_id": "111", "message_id": "501", "external_user_id": "111",
            "raw_source_ref": "tg://workbot/111/501?retract=2",
        })
        self.assertEqual(status, 200, ret)
        self.assertTrue(ret["result"]["accepted"])
        self.assertEqual(ret["result"]["linked_tasks"], [tid])

        # Historical events stay intact + one source_retracted event + external_change wake event emitted.
        events = self.svc.read(actor=self.owner, task_id=tid)["events"]
        self.assertEqual(len(events), event_count_before + 1)
        self.assertEqual(events[-1]["kind"], "source_retracted")
        self.assertEqual(self.q("SELECT kind, status FROM wake_events WHERE origin='telegram:workbot:retract'"),
                         [("external_change", "pending")])

        # Duplicate retract is idempotent.
        dup = record_retract(
            self.db, workspace_id="ws", platform="telegram", bot_id="workbot", update_id="r-del-2",
            chat_id="111", message_id="501", raw_source_ref="tg://workbot/111/501?retract=3", clock=self.clock,
        )
        self.assertTrue(dup["duplicate"])

        # Subsequent edit to retracted message is rejected.
        self.assertFalse(record_edit(
            self.db, workspace_id="ws", platform="telegram", bot_id="workbot", update_id="r-edit-after",
            chat_id="111", message_id="501", external_user_id="111", raw_source_ref="tg://workbot/111/501?e",
        )["accepted"])

        # Retracted message cannot serve as strong evidence for closing, deliverable acceptance, or approval.
        ver = self.q("SELECT version FROM tasks WHERE id=?", tid)[0][0]
        with self.assertRaises(EvidenceRequired):
            self.apply(self.agent, "close-retracted", {
                "op": "transition", "task_id": tid, "expected_version": ver, "to": "DONE",
                "evidence_type": "explicit_user", "source_ref": gao_src, "reason": "close with retracted msg",
            })
        with self.assertRaises(EvidenceRequired):
            self.apply(self.agent, "acc-retracted", {
                "op": "update_deliverable", "deliverable_id": did, "expected_version": ver,
                "status": "ACCEPTED", "evidence_type": "explicit_user", "source_ref": gao_src,
            })

        # Owner's own retracted message cannot decide an approval or external approval either.
        own_msg = record_inbound(
            self.db, workspace_id="ws", platform="telegram", bot_id="workbot", update_id="own-1",
            chat_id="222", message_id="601", external_user_id="222",
            raw_source_ref="tg://workbot/222/601", clock=self.clock,
        )
        record_retract(
            self.db, workspace_id="ws", platform="telegram", bot_id="workbot", update_id="own-del",
            chat_id="222", message_id="601", external_user_id="222",
            raw_source_ref="tg://workbot/222/601?retract=1", clock=self.clock,
        )
        with closing(sqlite3.connect(self.db)) as c, c:
            c.execute(
                "INSERT INTO wake_events(id,workspace_id,origin,origin_event_id,kind,source_ref,created_at,updated_at)"
                " VALUES('w-apr','ws','o','ap1','user_message','s',?,?)", (NOW, NOW),
            )
        approvals = ApprovalService(self.db, clock=self.clock)
        apr_id = approvals.request(
            host_actor=self.agent, wake_event_id="w-apr", tool_name="send_external_message",
            tool_input={"x": 1}, summary="外发", host_deferred_ref="s#1", expires_at="2026-11-20T00:00:00Z", task_id=tid,
        )["approval_id"]
        owner_tg = ActorContext("ws", "owner", "owner", "telegram")
        with self.assertRaises(EvidenceRequired):
            approvals.decide(approver=owner_tg, approval_id=apr_id, approve=True, source_ref="tg://workbot/222/601")
        with self.assertRaises(EvidenceRequired):
            approvals.record_external(
                approver=owner_tg, recorded_by="workbot", system="facetide", action="confirm",
                subject_ref="s1", payload_digest="d1", source_ref="tg://workbot/222/601",
                decided_at="2026-11-18T00:00:00Z",
            )

        # Dashboard task detail displays retracted source badge cleanly.
        self.assertIn("已撤回失效", self.get(f"/task/{tid}", self.owner_auth))


for _name in [n for n in dir(test_owner_console.OwnerConsoleTests) if n.startswith("test_")]:
    setattr(SchedulesWatchesAttachmentsRetractionTests, _name, None)
