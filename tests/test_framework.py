"""Framework contract tests: migration, timers, dispatch, approvals, outboxes, intake, HTTP."""
import base64
import json
import sqlite3
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from contextlib import closing
from http.server import ThreadingHTTPServer
from pathlib import Path

from workdesk import ActorContext, SqliteTaskService, init_database
from workdesk.approvals import ApprovalService
from workdesk.dispatcher import Dispatcher
from workdesk.errors import EvidenceRequired, InvalidOperation
from workdesk.http_api import WorkbenchApp, make_handler
from workdesk.identity import bind_identity, record_inbound
from workdesk.outboxes import DeliveryOutbox
from workdesk.runtime_bridge import HostOutcome
from workdesk.scheduler import advance, fire_due, observe_watch
from workdesk.storage import SCHEMA, migrate, schema_version

NOW = "2026-11-17T02:00:00Z"
SRC = "tg://bot/1/1"


class Clock:
    def __init__(self, now=NOW):
        self.now = now

    def __call__(self):
        return self.now


class Base(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(tmp.cleanup)
        self.dir = Path(tmp.name)
        self.db = self.dir / "wb.sqlite"
        init_database(self.db)
        with closing(sqlite3.connect(self.db)) as c, c:
            c.executemany("INSERT INTO principals(id,display_name,kind,created_at) VALUES(?,?,?,?)",
                          [("owner", "Owner", "human", NOW), ("gao", "Gao", "human", NOW), ("agent", "Agent", "agent", NOW)])
            c.execute("INSERT INTO workspaces(id,title,owner_id,timezone,created_at) VALUES('ws','W','owner','Asia/Shanghai',?)", (NOW,))
            c.executemany("INSERT INTO memberships(workspace_id,principal_id,role) VALUES('ws',?,?)",
                          [("owner", "owner"), ("gao", "requester"), ("agent", "editor")])
        self.clock = Clock()
        self.svc = SqliteTaskService(self.db, clock=self.clock)
        self.owner = ActorContext("ws", "owner", "owner", "cli")
        self.agent = ActorContext("ws", "agent", "editor", "host")

    def apply(self, actor, key, *ops):
        return self.svc.apply(actor=actor, idempotency_key=key, operations=list(ops))

    def sent_delivery(self, task, key="d1"):
        """A delivery of `task` the carrier confirmed (what a real SUBMITTED receipt points at)."""
        with closing(sqlite3.connect(self.db)) as c, c:
            c.execute("""INSERT INTO delivery_outbox(id,workspace_id,task_id,recipient_principal_id,channel,payload_json,status,
                           provider_message_id,dedupe_key,created_at,updated_at)
                         VALUES(?,?,?,?,?,?,?,?,?,?,?)""", (key, "ws", task, "gao", "telegram", "{}", "sent", "tg-88", key, NOW, NOW))
        return key

    def inbox_from(self, principal, n=1):
        """A message `principal` really sent (what an acceptance source_ref points at)."""
        ref = f"tg://bot/{principal}/{n}"
        with closing(sqlite3.connect(self.db)) as c, c:
            c.execute("""INSERT INTO inbox_messages(id,workspace_id,platform,bot_id,update_id,chat_id,message_id,
                           sender_principal_id,raw_source_ref,received_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
                      (f"in-{principal}-{n}", "ws", "telegram", "bot", f"u-{principal}-{n}", principal, str(n), principal,
                       ref, NOW, NOW))
        return ref

    def task(self, key="t", **kw):
        return self.apply(self.owner, key, {"op": "create_task", "title": "Report", "state": "DOING",
                                            "evidence_type": "explicit_user", **kw})[0]["task_id"]

    def q(self, sql, *args):
        with closing(sqlite3.connect(self.db)) as c:
            return c.execute(sql, args).fetchall()


class MigrationTests(unittest.TestCase):
    def test_bootstrap_database_upgrades_with_data_and_backup(self):
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as d:
            db, backup = Path(d) / "old.sqlite", Path(d) / "backup.sqlite"
            with closing(sqlite3.connect(db)) as c, c:
                c.executescript(SCHEMA.read_text(encoding="utf-8"))
                c.execute("INSERT INTO principals VALUES('o','O','human',1,?)", (NOW,))
                c.execute("INSERT INTO workspaces VALUES('w','W','o','UTC',?)", (NOW,))
                c.execute("INSERT INTO tasks(id,workspace_id,title,created_at,updated_at) VALUES('t','w','Old',?,?)", (NOW, NOW))
            self.assertEqual(schema_version(db), 1)
            self.assertEqual(migrate(db, backup_to=backup), [2, 3, 4])
            self.assertEqual(migrate(db), [])
            self.assertEqual(schema_version(db), 4)
            with closing(sqlite3.connect(db)) as c:
                self.assertEqual(c.execute("SELECT title, official_due_precision FROM tasks").fetchone(), ("Old", None))
            with closing(sqlite3.connect(backup)) as c:
                self.assertEqual(c.execute("SELECT COUNT(*) FROM tasks").fetchone()[0], 1)


class BusinessObjectTests(Base):
    def test_date_only_official_due_keeps_the_date(self):
        t = self.task()
        out = self.apply(self.owner, "due", {"op": "set_official_due", "task_id": t, "expected_version": 1,
                                             "local": "2026-11-30", "evidence_type": "explicit_user"})[0]
        self.assertEqual(out["official_due_at"], "2026-11-30T15:59:59Z")  # end of day Shanghai
        row = self.svc.read(actor=self.owner, task_id=t)["task"]
        self.assertEqual((row["official_due_local"], row["official_due_precision"]), ("2026-11-30", "date"))

    def test_commitments_and_deliverable_evidence_steps(self):
        t = self.task()
        res = self.apply(self.owner, "c",
                         {"op": "record_commitment", "task_id": t, "expected_version": 1, "promisor_id": "owner",
                          "promisee_id": "gao", "promise_text": "draft by Wednesday", "due_local": "2026-11-19",
                          "evidence_type": "explicit_user", "source_ref": SRC},
                         {"op": "register_deliverable", "ref": "d", "task_id": t, "expected_version": 1,
                          "title": "Plan draft", "artifact_ref": "file://plan-v1.docx", "evidence_type": "explicit_user"})
        self.assertEqual(res[0]["direction"], "owner_owes")
        did = res[1]["deliverable_id"]
        with self.assertRaises(EvidenceRequired):  # the model saying "sent" is not a receipt
            self.apply(self.agent, "s0", {"op": "update_deliverable", "deliverable_id": did, "expected_version": 2,
                                          "status": "SUBMITTED", "evidence_type": "explicit_user"})
        with self.assertRaises(EvidenceRequired):  # a receipt string that no carrier confirmed
            self.apply(self.agent, "s0b", {"op": "update_deliverable", "deliverable_id": did, "expected_version": 2,
                                           "status": "SUBMITTED", "evidence_type": "verified_tool", "receipt_ref": "made-up"})
        self.apply(self.agent, "s1", {"op": "update_deliverable", "deliverable_id": did, "expected_version": 2,
                                      "status": "SUBMITTED", "evidence_type": "verified_tool", "receipt_ref": self.sent_delivery(t)})
        with self.assertRaises(InvalidOperation):  # cannot skip back to DRAFT
            self.apply(self.agent, "s2", {"op": "update_deliverable", "deliverable_id": did, "expected_version": 3,
                                          "status": "DRAFT", "evidence_type": "explicit_user", "source_ref": SRC})
        with self.assertRaises(EvidenceRequired):  # the Agent cannot accept its own work
            self.apply(self.agent, "s3a", {"op": "update_deliverable", "deliverable_id": did, "expected_version": 3,
                                           "status": "ACCEPTED", "evidence_type": "explicit_user", "source_ref": "tg://gao/9"})
        self.apply(self.agent, "s3", {"op": "update_deliverable", "deliverable_id": did, "expected_version": 3,
                                      "status": "ACCEPTED", "evidence_type": "explicit_user", "source_ref": self.inbox_from("gao")})
        view = self.svc.read(actor=self.owner, task_id=t)
        self.assertEqual(view["deliverables"][0]["status"], "ACCEPTED")
        self.assertEqual(view["task"]["state"], "DOING")  # accepted deliverable does not auto-complete the task

    def test_checkpoint_and_changes_since(self):
        t = self.task()
        first = self.svc.changes_since(actor=self.owner, view="tg", mark_seen=True)
        self.assertFalse(first["unchanged"])
        self.assertTrue(self.svc.changes_since(actor=self.owner, view="tg")["unchanged"])
        self.apply(self.agent, "chk", {"op": "save_checkpoint", "task_id": t, "next_action": "run integration tests",
                                       "completed_steps": ["schema fixed"], "unverified": ["pushed?"],
                                       "repo_ref": "Agent-WorkBench@abc", "evidence_type": "verified_tool"})
        news = self.svc.changes_since(actor=self.owner, view="tg")
        self.assertEqual([e["kind"] for e in news["events"]], ["checkpoint_saved"])
        latest = self.svc.read(actor=self.owner, task_id=t)["latest_checkpoint"]
        self.assertEqual(json.loads(latest["unverified_json"]), ["pushed?"])


class TimerTests(Base):
    def test_reminder_fires_once_and_stale_one_is_cancelled(self):
        t = self.task()
        r = self.apply(self.owner, "r", {"op": "set_reminder", "task_id": t, "expected_version": 1,
                                         "local_at": "2026-11-17T11:00", "evidence_type": "explicit_user"},
                       {"op": "set_reminder", "task_id": t, "expected_version": 1, "kind": "checkin",
                        "local_at": "2026-11-17T12:00", "evidence_type": "explicit_user"})
        self.clock.now = "2026-11-17T03:30:00Z"
        out = fire_due(self.db, now=self.clock.now)
        self.assertEqual([x["reminder_id"] for x in out["reminders_fired"]], [r[0]["reminder_id"]])
        self.assertEqual(fire_due(self.db, now=self.clock.now)["reminders_fired"], [])
        self.apply(self.owner, "done", {"op": "transition", "task_id": t, "expected_version": 2, "to": "DONE",
                                        "evidence_type": "explicit_user", "source_ref": SRC, "reason": "done"})
        self.clock.now = "2026-11-17T05:00:00Z"
        self.assertEqual(fire_due(self.db, now=self.clock.now)["reminders_fired"], [])
        self.assertEqual(self.q("SELECT COUNT(*) FROM wake_events WHERE kind='timer_fired'")[0][0], 1)

    def test_monthly_schedule_creates_one_task_per_occurrence(self):
        self.assertEqual(advance("2026-01-31T18:00", "monthly", 1), "2026-02-28T18:00")
        self.apply(self.owner, "sch", {"op": "create_schedule", "title": "Pull due list, email boss",
                                       "frequency": "monthly", "local_time": "18:00",
                                       "start_local_date": "2026-11-30", "evidence_type": "explicit_user"})
        self.clock.now = "2026-11-30T10:05:00Z"
        first = fire_due(self.db, now=self.clock.now)["schedule_occurrences"]
        again = fire_due(self.db, now=self.clock.now)["schedule_occurrences"]
        self.assertEqual((len(first), again), (1, []))
        self.assertEqual(self.q("SELECT next_fire_local FROM schedule_rules")[0][0], "2026-12-30T18:00")

    def test_external_watch_resolves_dependency_and_cancels_followup(self):
        t = self.task()
        res = self.apply(self.agent, "w",
                         {"op": "add_dependency", "ref": "d", "task_id": t, "expected_version": 1,
                          "kind": "external_response", "awaited_outcome": "CI green", "evidence_type": "explicit_user"},
                         {"op": "set_reminder", "task_id": t, "expected_version": 1, "dependency_id": "$d",
                          "local_at": "2026-11-20T10:00", "evidence_type": "explicit_user"},
                         {"op": "add_watch", "ref": "w", "task_id": t, "expected_version": 1, "dependency_id": "$d",
                          "source_kind": "github_ci", "target_ref": "repo#1", "expected_value": "success",
                          "evidence_type": "explicit_user"})
        wid = res[2]["watch_id"]
        self.assertFalse(observe_watch(self.db, workspace_id="ws", watch_id=wid, observed_value="failure",
                                       source_ref="gh://run/1", now=NOW)["matched"])
        out = observe_watch(self.db, workspace_id="ws", watch_id=wid, observed_value="success",
                            source_ref="gh://run/2", now=NOW)
        self.assertEqual(out["dependency_resolved"], res[0]["dependency_id"])
        self.assertEqual(self.q("SELECT status FROM reminders")[0][0], "cancelled")


class FakeHost:
    def __init__(self):
        self.submitted, self.known = [], {}

    def submit_event(self, event):
        self.submitted.append(event.event_id)
        if event.kind == "timer_fired":
            raise TimeoutError("ack lost")
        return HostOutcome("completed", f"run-{event.event_id}", "s1")

    def lookup_event(self, event_id):
        return self.known.get(event_id)


class DispatchApprovalOutboxTests(Base):
    def test_one_live_run_per_workspace_and_unknown_reconcile(self):
        t = self.task()
        self.apply(self.owner, "r", {"op": "set_reminder", "task_id": t, "expected_version": 1,
                                     "local_at": "2026-11-17T10:30", "evidence_type": "explicit_user"})
        self.clock.now = "2026-11-17T02:31:00Z"
        fire_due(self.db, now=self.clock.now)
        host = FakeHost()
        d = Dispatcher(self.db, host, clock=self.clock)
        first = d.dispatch_once("ws", lease_seconds=60)
        self.assertFalse(first["acknowledged"])
        self.assertFalse(d.dispatch_once("ws")["dispatched"])  # lane busy until the lease resolves
        self.clock.now = "2026-11-17T02:33:00Z"
        self.assertFalse(d.dispatch_once("ws")["dispatched"])  # expired lease -> unknown, never re-submitted
        self.assertEqual(host.submitted, [first["event_id"]])
        host.known[first["event_id"]] = HostOutcome("completed", "run-x", "s1")
        self.assertEqual(d.reconcile_unknown("ws")["settled"], [first["event_id"]])

    def test_approval_binds_exact_call_once(self):
        with closing(sqlite3.connect(self.db)) as c, c:
            c.execute("INSERT INTO wake_events(id,workspace_id,origin,origin_event_id,kind,source_ref,created_at,updated_at)"
                      " VALUES('wk','ws','o','1','user_message','s',?,?)", (NOW, NOW))
        svc = ApprovalService(self.db, clock=self.clock)
        call = {"to": "gao", "text": "final plan attached"}
        req = svc.request(host_actor=self.agent, wake_event_id="wk", tool_name="send_message", tool_input=call,
                          summary="send final plan to Gao", host_deferred_ref="session-1#call-3",
                          expires_at="2026-11-18T00:00:00Z")
        use = lambda inp, session="session-1": svc.consume(workspace_id="ws", tool_name="send_message",
                                                          tool_input=inp, session_ref=session)
        self.assertIsNone(use(call))
        self.assertEqual(self.q("SELECT recipient_principal_id FROM delivery_outbox WHERE dedupe_key=?",
                                f"approval:{req['approval_id']}"), [("owner",)])  # approver is told
        decided = svc.decide(approver=self.owner, approval_id=req["approval_id"], approve=True, source_ref=SRC)
        self.assertEqual(decided["status"], "approved")
        self.assertIsNone(use({"to": "someone-else", "text": "final plan attached"}))
        self.assertIsNone(use(call, session="session-2"))  # another session cannot use it
        self.assertIsNotNone(use(call))
        self.assertIsNone(use(call))

    def test_delivery_cancels_stale_followups_and_respects_quiet_hours(self):
        t = self.task()
        out = DeliveryOutbox(self.db, clock=self.clock)
        with self.assertRaises(InvalidOperation):
            out.settle(workspace_id="ws", delivery_id="x", status="sent")
        a = out.enqueue(workspace_id="ws", recipient_id="owner", channel="telegram", payload={"text": "ping"},
                        dedupe_key="k1", task_id=t)
        self.assertTrue(out.enqueue(workspace_id="ws", recipient_id="owner", channel="telegram",
                                    payload={"text": "ping"}, dedupe_key="k1", task_id=t)["duplicate"])
        with closing(sqlite3.connect(self.db)) as c, c:
            c.execute("INSERT INTO user_profiles(workspace_id,principal_id,work_preferences_json,updated_at) "
                      "VALUES('ws','owner',?,?)", (json.dumps({"quiet_hours": {"start": "23:00", "end": "08:00"}}), NOW))
        self.clock.now = "2026-11-17T16:00:00Z"  # 00:00 Shanghai -> quiet
        self.assertEqual(out.claim(workspace_id="ws"), [])
        self.clock.now = "2026-11-18T00:30:00Z"  # 08:30 Shanghai
        self.assertEqual([x["delivery_id"] for x in out.claim(workspace_id="ws")], [a["delivery_id"]])
        out.settle(workspace_id="ws", delivery_id=a["delivery_id"], status="unknown")
        self.assertEqual(len(out.report(workspace_id="ws")["needs_human_check"]), 1)


class IntakeAndHttpTests(Base):
    def test_inbound_dedupe_and_unknown_sender(self):
        bind_identity(self.db, provider="telegram", bot_id="workbot", external_user_id="111", principal_id="gao")
        args = dict(workspace_id="ws", platform="telegram", bot_id="workbot", chat_id="c", raw_source_ref=SRC)
        first = record_inbound(self.db, update_id="1", message_id="m1", external_user_id="111", **args)
        self.assertEqual(first["actor"]["role"], "requester")
        self.assertTrue(record_inbound(self.db, update_id="1", message_id="m1", external_user_id="111", **args)["duplicate"])
        self.assertFalse(record_inbound(self.db, update_id="2", message_id="m2", external_user_id="999", **args)["accepted"])
        self.assertEqual(self.q("SELECT COUNT(*) FROM wake_events WHERE kind='user_message'")[0][0], 1)

    def test_http_requires_host_token_and_resolves_identity(self):
        bind_identity(self.db, provider="telegram", bot_id="workbot", external_user_id="111", principal_id="gao")
        app = WorkbenchApp(self.db, host_token="t" * 32, dashboard_token="d" * 20, dashboard_workspace="ws",
                           dashboard_principal="owner", clock=self.clock)
        server = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(app))
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        base = f"http://127.0.0.1:{server.server_port}"

        def post(path, body, token="t" * 32):
            req = urllib.request.Request(base + path, data=json.dumps(body).encode(), method="POST",
                                         headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"})
            try:
                with urllib.request.urlopen(req) as r:
                    return r.status, json.loads(r.read())
            except urllib.error.HTTPError as e:
                return e.code, json.loads(e.read() or b"{}")

        ident = {"workspace_id": "ws", "identity": {"provider": "telegram", "bot_id": "workbot", "external_user_id": "111"}}
        self.assertEqual(post("/v1/tools/list_tasks", ident, token="wrong")[0], 401)
        status, body = post("/v1/tools/change_tasks", ident | {"idempotency_key": "tg:5", "operations": [
            {"op": "create_task", "title": "Please review the plan", "evidence_type": "explicit_user"}]})
        self.assertEqual(status, 200)
        status, body = post("/v1/tools/change_tasks", ident | {"idempotency_key": "tg:6", "operations": [
            {"op": "set_focus", "slot": "NOW", "task_id": body["result"][0]["task_id"], "evidence_type": "explicit_user"}]})
        self.assertEqual((status, body["error"]["code"]), (403, "FORBIDDEN"))
        with self.assertRaises(urllib.error.HTTPError) as no_auth:
            urllib.request.urlopen(base + "/")
        self.assertEqual(no_auth.exception.code, 401)
        auth = base64.b64encode(b"owner:" + b"d" * 20).decode()
        with urllib.request.urlopen(urllib.request.Request(base + "/", headers={"Authorization": f"Basic {auth}"})) as r:
            page = r.read().decode("utf-8")
        self.assertIn("Please review the plan", page)


class GapFillTests(Base):
    def test_promotion_review_then_auto_promotion_from_recorded_outcomes(self):
        from workdesk import DelegationProfileAdmin
        from workdesk.promotion import review_delegations
        admin = DelegationProfileAdmin(self.db, clock=self.clock)
        policy = {"min_verified_successes": 1, "min_acceptance_ratio": 1.0, "max_rework_ratio": 0.0}
        admin.publish(owner=self.owner, work_class="report", max_autonomy_level="agent_bounded",
                      allowed_actions=["draft", "send"], reversible_actions=["draft"], promotion_policy=policy)
        t = self.task()
        self.apply(self.owner, "del", {"op": "assign_responsibility", "task_id": t, "expected_version": 1,
                                       "executor_id": "agent", "work_class": "report", "autonomy_level": "agent_prepares",
                                       "evidence_type": "explicit_user", "source_ref": SRC, "reason": "try it"})
        self.assertEqual(review_delegations(self.db, now=NOW), [])  # no outcomes yet
        did = self.apply(self.agent, "dl", {"op": "register_deliverable", "task_id": t, "expected_version": 2,
                                            "title": "R", "artifact_ref": "f", "evidence_type": "explicit_user"})[0]["deliverable_id"]
        self.apply(self.agent, "sub", {"op": "update_deliverable", "deliverable_id": did, "expected_version": 3,
                                       "status": "SUBMITTED", "evidence_type": "verified_tool", "receipt_ref": self.sent_delivery(t)})
        self.apply(self.agent, "acc", {"op": "update_deliverable", "deliverable_id": did, "expected_version": 4,
                                       "status": "ACCEPTED", "evidence_type": "explicit_user", "source_ref": self.inbox_from("owner", 7)})
        out = review_delegations(self.db, now=NOW)
        self.assertEqual(out, [{"task_id": t, "review_requested": "agent_supervised"}])
        self.assertEqual(review_delegations(self.db, now=NOW), [])  # one pending review, not one per run
        self.assertEqual(self.q("SELECT autonomy_level FROM task_responsibilities")[0][0], "agent_prepares")
        admin.publish(owner=self.owner, work_class="report", max_autonomy_level="agent_bounded",
                      allowed_actions=["draft", "send"], reversible_actions=["draft"], promotion_policy=policy,
                      auto_promotion_enabled=True)
        self.apply(self.owner, "del2", {"op": "assign_responsibility", "task_id": t, "expected_version": 5,
                                        "executor_id": "agent", "work_class": "report", "autonomy_level": "agent_prepares",
                                        "evidence_type": "explicit_user", "source_ref": SRC, "reason": "new profile"})
        self.assertEqual(review_delegations(self.db, now=NOW), [{"task_id": t, "promoted_to": "agent_supervised"}])

    def test_github_watch_adapter_maps_ci_and_pr_states(self):
        from workdesk.watchers import ci_state, poll_watches
        self.assertEqual(ci_state([]), "pending")
        self.assertEqual(ci_state([{"status": "completed", "conclusion": "success"},
                                   {"status": "completed", "conclusion": "failure"}]), "failure")
        t = self.task()
        self.apply(self.agent, "w", {"op": "add_watch", "task_id": t, "expected_version": 1, "source_kind": "github_pr",
                                     "target_ref": "acme/repo#7", "expected_value": "merged", "evidence_type": "explicit_user"})
        fake = lambda path: {"merged": True, "state": "closed"} if path.endswith("/pulls/7") else {}
        out = poll_watches(self.db, now=NOW, fetch=fake)
        self.assertTrue(out[0]["matched"])
        self.assertEqual(self.q("SELECT COUNT(*) FROM wake_events WHERE kind='external_change'")[0][0], 1)

    def test_edited_message_is_a_revision_linked_to_its_tasks(self):
        from workdesk.identity import record_edit
        bind_identity(self.db, provider="telegram", bot_id="workbot", external_user_id="111", principal_id="gao")
        bind_identity(self.db, provider="telegram", bot_id="workbot", external_user_id="222", principal_id="owner")
        args = dict(workspace_id="ws", platform="telegram", bot_id="workbot", chat_id="c", message_id="m1")
        first = record_inbound(self.db, update_id="1", external_user_id="111", raw_source_ref=SRC, **args)
        gao = ActorContext("ws", "gao", "requester", "telegram")
        t = self.apply(gao, "g", {"op": "create_task", "title": "Review by Friday", "evidence_type": "explicit_user"})[0]["task_id"]
        self.apply(gao, "l", {"op": "link_inbox", "task_id": t, "inbox_id": first["inbox_id"], "relation": "request",
                              "evidence_type": "system"})
        self.assertFalse(record_edit(self.db, update_id="2", external_user_id="222", raw_source_ref=SRC + "?e", **args)["accepted"])
        edit = record_edit(self.db, update_id="3", external_user_id="111", raw_source_ref=SRC + "?e", **args)
        self.assertEqual((edit["revision"], edit["linked_tasks"]), (2, [t]))
        self.assertTrue(record_edit(self.db, update_id="3", external_user_id="111", raw_source_ref=SRC + "?e", **args)["duplicate"])
        kinds = [e["kind"] for e in self.svc.read(actor=self.owner, task_id=t)["events"]]
        self.assertIn("source_edited", kinds)

    def test_dashboard_task_page_shows_history(self):
        from workdesk.dashboard import render_task
        t = self.task()
        self.apply(self.owner, "p", {"op": "append_progress", "task_id": t, "expected_version": 1, "text": "outline done",
                                     "evidence_type": "explicit_user", "reason": "outline done"})
        page = render_task(self.svc, self.owner, str(self.db), t, NOW)
        self.assertIn("进展", page)
        self.assertIn("outline done", page)



class AuditRegressionTests(Base):
    def test_completion_report_is_delivered_but_stale_followup_is_not(self):
        t = self.task()
        rem = self.apply(self.owner, "r", {"op": "set_reminder", "task_id": t, "expected_version": 1,
                                           "local_at": "2026-11-20T10:00", "evidence_type": "explicit_user"})[0]
        self.apply(self.owner, "d", {"op": "transition", "task_id": t, "expected_version": 2, "to": "DONE",
                                     "evidence_type": "explicit_user", "source_ref": SRC, "reason": "done"})
        out = DeliveryOutbox(self.db, clock=self.clock)
        report = out.enqueue(workspace_id="ws", recipient_id="owner", channel="telegram",
                             payload={"text": "X is done"}, dedupe_key="k-report", task_id=t)
        out.enqueue(workspace_id="ws", recipient_id="owner", channel="telegram", payload={"text": "chase"},
                    dedupe_key="k-chase", task_id=t, reminder_id=rem["reminder_id"])
        self.assertEqual([x["delivery_id"] for x in out.claim(workspace_id="ws")], [report["delivery_id"]])

    def test_month_end_schedule_does_not_drift_and_catch_up_is_bounded(self):
        self.assertEqual(advance("2026-02-28T18:00", "monthly", 1, 31), "2026-03-31T18:00")
        self.apply(self.owner, "s", {"op": "create_schedule", "title": "Month end", "frequency": "monthly",
                                     "local_time": "18:00", "start_local_date": "2026-01-31",
                                     "evidence_type": "explicit_user"})
        self.clock.now = "2026-11-17T02:00:00Z"  # ten months of missed occurrences
        out = fire_due(self.db, now=self.clock.now)["schedule_occurrences"]
        self.assertEqual(len(out), 1)
        self.assertEqual(out[0]["occurrence_local"], "2026-10-31T18:00")
        self.assertEqual(self.q("SELECT next_fire_local FROM schedule_rules")[0][0], "2026-11-30T18:00")

    def test_bad_wake_row_does_not_block_the_inbox(self):
        t = self.task()
        with closing(sqlite3.connect(self.db)) as c, c:
            c.execute("INSERT INTO wake_events(id,workspace_id,origin,origin_event_id,kind,source_ref,created_at,updated_at)"
                      " VALUES('bad','ws','x','1','user_message','',?,?)", ("2026-11-17T00:00:00Z", NOW))
            c.execute("INSERT INTO wake_events(id,workspace_id,task_id,origin,origin_event_id,kind,source_ref,created_at,updated_at)"
                      " VALUES('good','ws',?,'x','2','timer_fired','s',?,?)", (t, NOW, NOW))
        with self.assertRaises(InvalidOperation):
            observe_watch(self.db, workspace_id="ws", watch_id="w", observed_value="v", source_ref="", now=NOW)
        event = Dispatcher(self.db, FakeHost(), clock=self.clock).claim("ws")
        self.assertEqual(event.event_id, "good")
        self.assertEqual(self.q("SELECT status FROM wake_events WHERE id='bad'")[0][0], "failed")

    def test_inline_chat_turn_does_not_hold_the_background_lane(self):
        bind_identity(self.db, provider="telegram", bot_id="workbot", external_user_id="111", principal_id="gao")
        record_inbound(self.db, workspace_id="ws", platform="telegram", bot_id="workbot", update_id="1",
                       chat_id="c", message_id="m", external_user_id="111", raw_source_ref=SRC)
        t = self.task()
        with closing(sqlite3.connect(self.db)) as c, c:
            c.execute("INSERT INTO wake_events(id,workspace_id,task_id,origin,origin_event_id,kind,source_ref,created_at,updated_at)"
                      " VALUES('timer','ws',?,'x','2','timer_fired','s',?,?)", (t, NOW, NOW))
        self.assertEqual(Dispatcher(self.db, FakeHost(), clock=self.clock).claim("ws").event_id, "timer")


if __name__ == "__main__":
    unittest.main()
