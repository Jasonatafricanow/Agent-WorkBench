"""workbench.status.v1 for the LiteMonitor indicator: own read-only token, status-only listener, Host liveness."""
import json
import re
import threading
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path

from test_framework import NOW, Base

from workdesk.http_api import WorkbenchApp, make_handler, make_status_handler
from workdesk.status import build_status

STATUS = "s" * 32
HOST = "t" * 32
PLUGIN = Path(__file__).resolve().parents[1] / "host" / "litemonitor" / "WorkBench.json"


class StatusFixture(Base):
    def start(self, status_token=STATUS, status_only=False):
        self.app = WorkbenchApp(self.db, host_token=HOST, dashboard_token="d" * 20, dashboard_workspace="ws",
                                dashboard_principal="owner", clock=self.clock, status_token=status_token)
        handler = make_status_handler(self.app) if status_only else make_handler(self.app)
        server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        self.base = f"http://127.0.0.1:{server.server_port}"

    def call(self, path, token=STATUS, method="GET", data=None):
        req = urllib.request.Request(self.base + path, method=method, data=data,
                                     headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=5) as r:
                return r.status, json.loads(r.read())
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read() or b"{}")


class StatusTests(StatusFixture):
    def test_disabled_without_token_and_refuses_other_credentials(self):
        self.start(status_token=None)
        self.assertEqual(self.call("/status/v1")[0], 404)
        self.start()
        self.assertEqual(self.call("/status/v1", token="wrong")[0], 401)
        self.assertEqual(self.call("/status/v1", token=HOST)[0], 401)       # the Host token is not a status token
        self.assertEqual(self.call("/status/v1", token="d" * 20)[0], 401)   # nor is the dashboard password

    def test_status_token_cannot_write(self):
        self.start()
        body = json.dumps({"workspace_id": "ws", "idempotency_key": "x", "operations": []}).encode()
        self.assertEqual(self.call("/v1/tools/change_tasks", method="POST", data=body)[0], 401)

    def test_counts_line_and_host_liveness(self):
        t = self.task(title="季度报告初稿交给老板（修改版）", official_due_at="2026-11-16T09:00:00Z")
        self.apply(self.owner, "f", {"op": "set_focus", "slot": "NOW", "task_id": t, "evidence_type": "explicit_user"})
        self.start()
        status, body = self.call("/status/v1")
        self.assertEqual(status, 200)
        self.assertEqual(body["contract"], "workbench.status.v1")
        self.assertEqual((body["overdue"], body["host_state"], body["level"]), (1, 2, 2))  # Host never polled yet
        self.assertIn("逾期1", body["line"])
        self.assertIn("Host离线", body["line"])
        # Any authenticated Host call counts as a heartbeat.
        self.call("/v1/wake/claim", token=HOST, method="POST", data=json.dumps({"workspace_id": "ws"}).encode())
        body = self.call("/status/v1")[1]
        self.assertEqual(body["host_state"], 0)
        self.assertEqual(body["level"], 1)  # still overdue: attention, not alarm

    def test_host_state_thresholds(self):
        seen = 1_000_000.0
        state = lambda age: build_status(self.svc, self.owner, generated_at=NOW, host_seen_at=seen, now=seen + age)
        self.assertEqual([state(a)["host_state"] for a in (30, 300, 900)], [0, 1, 2])

    def test_status_listener_serves_nothing_else(self):
        self.start(status_only=True)
        self.assertEqual(self.call("/status/v1")[0], 200)
        for path in ("/", "/life", "/calendar", "/healthz"):
            self.assertEqual(self.call(path)[0], 404, path)
        body = json.dumps({"workspace_id": "ws"}).encode()
        self.assertEqual(self.call("/v1/wake/claim", token=HOST, method="POST", data=body)[0], 405)


class LiteMonitorPluginContract(StatusFixture):
    """Runs WorkBench.json the way LiteMonitor's chain executor does: resolve {{}} in url/headers, GET,
    extract dot paths, apply `map`, render outputs. An emulation of the engine, not LiteMonitor itself."""

    def render(self, template, ctx):
        return re.sub(r"\{\{(\w+)\}\}", lambda m: str(ctx.get(m.group(1), "")), template)

    def test_plugin_renders_line_and_colors(self):
        plugin = json.loads(PLUGIN.read_text(encoding="utf-8"))
        self.assertEqual(plugin["execution"]["type"], "chain")
        self.assertEqual({i["key"]: i["type"] for i in plugin["inputs"]}, {"base_url": "text", "status_token": "password"})
        self.start()
        ctx = {"base_url": self.base, "status_token": STATUS}
        for step in plugin["execution"]["steps"]:
            self.assertEqual(step["method"], "GET")  # the indicator never writes
            headers = {k: self.render(v, ctx) for k, v in step["headers"].items()}
            req = urllib.request.Request(self.render(step["url"], ctx), headers=headers)
            with urllib.request.urlopen(req, timeout=5) as r:
                data = json.loads(r.read())
            for var, path in step["extract"].items():
                ctx[var] = str(data[path])
            for proc in step.get("process", []):
                self.assertEqual(proc["function"], "map")
                ctx[proc["var"]] = proc["map"][ctx[proc["source"]]]
        out = {o["key"]: (self.render(o["format_val"], ctx), self.render(o["color"], ctx)) for o in plugin["outputs"]}
        self.assertEqual(out["host"], ("离线", "2"))
        self.assertTrue(out["summary"][0].startswith("无焦点"))
        self.assertIn(out["summary"][1], ("0", "1", "2"))
