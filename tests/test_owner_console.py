"""DEV_LOG rule 9 (11-18): the owner manages their own work directly — on the web console with the owner
token, or through their companion agent (Xiyue) with a companion token — while requesters come in through
the work bot. The read-only dashboard password stays read-only."""
import base64
import json
import re
import threading
import urllib.error
import urllib.parse
import urllib.request
from contextlib import closing
from http.server import ThreadingHTTPServer
import sqlite3

from test_framework import Base

from workdesk.http_api import WorkbenchApp, make_handler
from workdesk.identity import bind_identity
from workdesk.owner_ui import csrf_value

OWNER_TOKEN = "o" * 24
COMPANION_TOKEN = "c" * 32


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


class OwnerConsoleTests(Base):
    def setUp(self):
        super().setUp()
        self.app = WorkbenchApp(self.db, host_token="t" * 32, dashboard_token="d" * 20, dashboard_workspace="ws",
                                dashboard_principal="owner", clock=self.clock, owner_token=OWNER_TOKEN,
                                companion_token=COMPANION_TOKEN, companion_bot_id="xiyue")
        server = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(self.app))
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        self.base = f"http://127.0.0.1:{server.server_port}"
        self.owner_auth = "Basic " + base64.b64encode(b"me:" + OWNER_TOKEN.encode()).decode()
        self.viewer_auth = "Basic " + base64.b64encode(b"me:" + b"d" * 20).decode()
        self.opener = urllib.request.build_opener(NoRedirect)

    def get(self, path, auth):
        with urllib.request.urlopen(urllib.request.Request(self.base + path, headers={"Authorization": auth}), timeout=10) as r:
            return r.read().decode("utf-8")

    def post_form(self, fields, auth=None, origin=None):
        data = urllib.parse.urlencode(fields).encode()
        headers = {"Authorization": auth or self.owner_auth, "Content-Type": "application/x-www-form-urlencoded"}
        if origin:
            headers["Origin"] = origin
        try:
            r = self.opener.open(urllib.request.Request(self.base + "/owner/ops", data=data, headers=headers), timeout=10)
            return r.status, r.headers.get("Location")
        except urllib.error.HTTPError as exc:
            return exc.code, exc.headers.get("Location")

    def form(self, **fields):
        return {"csrf": csrf_value(OWNER_TOKEN), "nonce": fields.pop("nonce", "n1"), "back": "/", **fields}

    def test_owner_sees_forms_viewer_does_not(self):
        self.assertIn("/owner/ops", self.get("/", self.owner_auth))
        viewer = self.get("/", self.viewer_auth)
        self.assertIsNone(re.search(r"method\s*=\s*['\"]?post", viewer, re.I))
        self.assertIn("只读视图", viewer)

    def test_owner_creates_and_closes_a_task_without_citing_a_message(self):
        status, location = self.post_form(self.form(action="create", title="写周报", state="READY", priority="1",
                                                    soft_due="2026-11-20", focus="NOW"))
        self.assertEqual(status, 303)
        self.assertIn("ok=", location)
        (task_id, version), = self.q("SELECT id, version FROM tasks WHERE title='写周报'")
        events = self.q("SELECT actor_id, evidence_type, source_ref FROM task_events WHERE task_id=? AND kind='created'", task_id)
        self.assertEqual(events, [("owner", "explicit_user", "owner-web:n1")])
        self.assertEqual(self.q("SELECT slot FROM task_focus WHERE task_id=?", task_id), [("NOW",)])
        # Closing is a strong change: the owner in person needs a reason, not a cited chat message.
        status, location = self.post_form(self.form(nonce="n2", action="move", to="DONE", task_id=task_id,
                                                    version=str(version), reason="交了"))
        self.assertIn("ok=", location, urllib.parse.unquote(location or ""))
        self.assertEqual(self.q("SELECT state FROM tasks WHERE id=?", task_id), [("DONE",)])

    def test_resubmitted_form_does_not_act_twice(self):
        fields = self.form(action="create", title="一次", nonce="same")
        self.post_form(fields)
        self.post_form(fields)
        self.assertEqual(len(self.q("SELECT id FROM tasks WHERE title='一次'")), 1)

    def test_owner_form_refused_without_csrf_cross_origin_or_with_viewer_password(self):
        self.assertEqual(self.post_form({**self.form(action="create", title="x"), "csrf": "bad"})[0], 403)
        self.assertEqual(self.post_form(self.form(action="create", title="x"), origin="http://evil.example")[0], 403)
        self.assertEqual(self.post_form(self.form(action="create", title="x"), auth=self.viewer_auth)[0], 403)
        self.assertEqual(self.q("SELECT id FROM tasks WHERE title='x'"), [])

    def test_refusal_comes_back_as_a_notice(self):
        task = self.task()
        status, location = self.post_form(self.form(action="move", to="DONE", task_id=task, version="1", reason=""))
        self.assertEqual(status, 303)
        self.assertIn("err=", location)

    def companion(self, path, body, token=COMPANION_TOKEN):
        req = urllib.request.Request(self.base + path, data=json.dumps(body).encode(), method="POST",
                                     headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=10) as r:
                return r.status, json.loads(r.read())
        except urllib.error.HTTPError as exc:
            return exc.code, None

    def test_companion_writes_as_the_owner_citing_the_owners_message(self):
        bind_identity(self.db, provider="telegram", bot_id="xiyue", external_user_id="42", principal_id="owner")
        ident = {"provider": "telegram", "bot_id": "xiyue", "external_user_id": "42"}
        status, inbound = self.companion("/v1/companion/message", {
            "workspace_id": "ws", "platform": "telegram", "bot_id": "xiyue", "update_id": "u1", "chat_id": "42",
            "message_id": "7", "external_user_id": "42", "raw_source_ref": "tg://xiyue/42/7"})
        self.assertEqual(status, 200)
        # Answered inline by the companion: nothing left for the work bot, nothing "unknown".
        self.assertEqual(self.q("SELECT status FROM wake_events"), [("completed",)])
        self.assertEqual(self.q("SELECT status FROM inbox_messages"), [("done",)])
        task = self.task()
        status, result = self.companion("/v1/tools/change_tasks", {
            "workspace_id": "ws", "identity": ident, "idempotency_key": "x1",
            "operations": [{"op": "transition", "task_id": task, "expected_version": 1, "to": "DONE",
                            "evidence_type": "explicit_user", "source_ref": "tg://xiyue/42/7", "reason": "我说完成了"}]})
        self.assertEqual(status, 200, result)
        self.assertEqual(self.q("SELECT state FROM tasks WHERE id=?", task), [("DONE",)])
        self.assertIsNone(self.app.host_seen_at)  # the companion is not the work bot coming online

    def test_companion_token_reaches_only_its_routes_and_its_own_bot(self):
        bind_identity(self.db, provider="telegram", bot_id="workbot", external_user_id="42", principal_id="owner")
        other_bot = {"provider": "telegram", "bot_id": "workbot", "external_user_id": "42"}
        self.assertEqual(self.companion("/v1/tools/list_tasks", {"workspace_id": "ws", "identity": other_bot})[0], 403)
        self.assertEqual(self.companion("/v1/wake/claim", {"workspace_id": "ws"})[0], 403)
        self.assertEqual(self.companion("/v1/intake/inbound", {"workspace_id": "ws", "bot_id": "xiyue"})[0], 403)
        self.assertEqual(self.companion("/v1/tools/list_tasks", {"workspace_id": "ws"}, token="z" * 32)[0], 401)
