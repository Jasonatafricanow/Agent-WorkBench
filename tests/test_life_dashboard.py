"""WB-LIFE-01 P1/P3: five read-only dashboard views; Facetide via LifeStateAdapter, owner only."""
import base64
import json
import re
import socket
import threading
import time
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from test_framework import NOW, Base

from workdesk import ActorContext
from workdesk.dashboard import render_life
from workdesk.errors import Forbidden
from workdesk.http_api import WorkbenchApp, make_handler
from workdesk.life import LifeStateAdapter

SUMMARY = {
    "contract": "facetide.summary.v1", "generated_at": NOW, "score_scale": {"min": 0, "max": 10, "step": 0.5},
    "domains": [{"domain_id": d, "name": n} for d, n in (("career", "Work & career"), ("health", "Body & health"),
                                                        ("social", "Friends & social"))],
    "latest": {"review_id": "r2", "period_start": "2026-11-01", "period_end": "2026-11-15", "confirmed_at": "2026-11-16T02:00:00Z",
               "domain_scores": {"career": 7.5, "health": 5, "social": None}, "personal_growth": None,
               "self_realization": "building the workbench"},
    "previous": {"review_id": "r1", "period_start": "2026-10-01", "period_end": "2026-10-31", "confirmed_at": "2026-11-01T02:00:00Z",
                 "domain_scores": {"career": 6, "health": 6, "social": None}, "personal_growth": None, "self_realization": None},
    "goals": [{"goal_id": "g1", "title": "Sleep before 1am", "enough_condition": "5 nights a week", "status": "building",
               "domain_ids": ["health"], "updated_at": NOW}],
}
FACETIDE_TOKEN = "facetide-service-token-0123456789"


class FakeFacetide:
    """A stand-in for Facetide's /api/summary/v1 that can be switched off."""
    def __init__(self, test):
        outer = self
        self.calls, self.body = 0, SUMMARY

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def do_GET(self):
                outer.calls += 1
                if self.headers.get("Authorization") != f"Bearer {FACETIDE_TOKEN}":
                    self.send_response(401); self.end_headers(); return
                data = json.dumps(outer.body).encode()
                self.send_response(200); self.send_header("Content-Length", str(len(data))); self.end_headers()
                self.wfile.write(data)
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), H)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        test.addCleanup(self.stop)
        self.url = f"http://127.0.0.1:{self.server.server_port}/api/summary/v1"

    def stop(self):
        try:
            self.server.shutdown(); self.server.server_close()
        except OSError:
            pass


def closed_port_url():
    s = socket.socket(); s.bind(("127.0.0.1", 0)); port = s.getsockname()[1]; s.close()
    return f"http://127.0.0.1:{port}/api/summary/v1"


class AdapterTests(Base):
    def adapter(self, url, token=FACETIDE_TOKEN, **kw):
        return LifeStateAdapter(url=url, token=token, cache_file=self.dir / "cache.json", min_interval=0, **kw)

    def test_fresh_then_stale_from_cache_when_facetide_goes_down(self):
        fake = FakeFacetide(self)
        adapter = self.adapter(fake.url)
        first = adapter.summary()
        self.assertEqual(first["status"], "fresh")
        fetched = first["fetched_at"]
        fake.stop()
        adapter.url = closed_port_url()
        second = adapter.summary()
        self.assertEqual(second["status"], "stale")
        self.assertEqual(second["fetched_at"], fetched)          # last successful update is kept
        self.assertEqual(second["summary"]["latest"]["review_id"], "r2")

    def test_unavailable_without_cache_and_not_configured(self):
        self.assertEqual(self.adapter(closed_port_url()).summary()["status"], "unavailable")
        self.assertEqual(LifeStateAdapter(url=None, token=None, cache_file=self.dir / "c.json").summary()["status"],
                         "not_configured")

    def test_wrong_token_or_contract_is_not_accepted(self):
        fake = FakeFacetide(self)
        self.assertEqual(self.adapter(fake.url, token="nope").summary()["status"], "unavailable")
        fake.body = {**SUMMARY, "contract": "something-else"}
        self.assertEqual(self.adapter(fake.url).summary()["status"], "unavailable")

    def test_repeated_page_loads_do_not_hammer_facetide(self):
        fake = FakeFacetide(self)
        adapter = LifeStateAdapter(url=fake.url, token=FACETIDE_TOKEN, cache_file=self.dir / "c.json", min_interval=60)
        for _ in range(5):
            adapter.summary()
        self.assertEqual(fake.calls, 1)

    def test_slow_facetide_times_out(self):
        listener = socket.socket(); listener.bind(("127.0.0.1", 0)); listener.listen(1)  # accepts, never answers
        self.addCleanup(listener.close)
        adapter = self.adapter(f"http://127.0.0.1:{listener.getsockname()[1]}/api/summary/v1", timeout=0.5)
        started = time.monotonic()
        self.assertEqual(adapter.summary()["status"], "unavailable")
        self.assertLess(time.monotonic() - started, 3)


class LifeViewTests(Base):
    def test_life_view_is_owner_only(self):
        adapter = LifeStateAdapter(url=None, token=None, cache_file=self.dir / "c.json")
        for actor in (ActorContext("ws", "gao", "requester", "dashboard"), ActorContext("ws", "agent", "editor", "dashboard")):
            with self.assertRaises(Forbidden):
                render_life(actor, str(self.db), NOW, adapter)

    def test_life_view_shows_confirmed_scores_nulls_and_previous_period(self):
        fake = FakeFacetide(self)
        adapter = LifeStateAdapter(url=fake.url, token=FACETIDE_TOKEN, cache_file=self.dir / "c.json", min_interval=0)
        page = render_life(self.owner, str(self.db), NOW, adapter)
        self.assertIn("Work &amp; career", page)
        self.assertIn(">7.5<", page)
        self.assertIn(">+1.5<", page)          # vs previous period
        self.assertIn(">—<", page)             # social not scored: shown as missing, never inferred
        self.assertIn("building the workbench", page)
        self.assertIn("Sleep before 1am", page)


class DashboardHttpTests(Base):
    def serve(self, adapter):
        app = WorkbenchApp(self.db, host_token="t" * 32, dashboard_token="d" * 20, dashboard_workspace="ws",
                           dashboard_principal="owner", clock=self.clock, life_adapter=adapter)
        server = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(app))
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        self.base = f"http://127.0.0.1:{server.server_port}"
        self.auth = {"Authorization": "Basic " + base64.b64encode(b"owner:" + b"d" * 20).decode()}

    def get(self, path):
        with urllib.request.urlopen(urllib.request.Request(self.base + path, headers=self.auth), timeout=10) as r:
            return r.status, r.read().decode("utf-8")

    def test_facetide_down_leaves_the_other_four_views_working(self):
        self.task(official_due_at="2026-11-20T09:00:00Z", soft_due_at="2026-11-19T09:00:00Z")
        self.serve(LifeStateAdapter(url=closed_port_url(), token=FACETIDE_TOKEN, cache_file=self.dir / "c.json"))
        for path in ("/", "/calendar", "/calendar?range=month", "/timeline?range=today", "/timeline?range=month",
                     "/projects"):
            status, page = self.get(path)
            self.assertEqual(status, 200, path)
            self.assertIn("<nav>", page)
        status, page = self.get("/life")
        self.assertEqual(status, 200)
        self.assertIn("Facetide 暂时连不上", page)
        status, cal = self.get("/calendar")
        self.assertIn("官方 DDL", cal)
        self.assertIn("Soft Due", cal)

    def test_dashboard_has_no_write_entry(self):
        self.serve(None)
        for path in ("/", "/calendar", "/timeline", "/projects", "/life"):
            page = self.get(path)[1]
            self.assertIsNone(re.search(r"method\s*=\s*['\"]?post", page, re.I), path)
            self.assertNotIn("<script", page)
        # The dashboard password is not a Host credential: it cannot call any write API.
        req = urllib.request.Request(self.base + "/v1/tools/change_tasks", data=b"{}", method="POST",
                                     headers={"Authorization": "Bearer " + "d" * 20, "Content-Type": "application/json"})
        with self.assertRaises(urllib.error.HTTPError) as denied:
            urllib.request.urlopen(req, timeout=5)
        self.assertEqual(denied.exception.code, 401)
        req = urllib.request.Request(self.base + "/", data=b"x", method="POST", headers=self.auth)
        with self.assertRaises(urllib.error.HTTPError) as no_post:
            urllib.request.urlopen(req, timeout=5)
        self.assertIn(no_post.exception.code, (401, 404, 405))

    def test_url_filters_cannot_widen_or_cross_workspaces(self):
        self.serve(None)
        with self.assertRaises(urllib.error.HTTPError) as other:
            self.get("/projects?project=p-from-another-workspace")
        self.assertEqual(other.exception.code, 404)
