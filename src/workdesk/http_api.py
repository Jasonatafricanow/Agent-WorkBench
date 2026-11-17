"""Local HTTP service for the Agent Host and a read-only dashboard.

- `/v1/*` (POST, JSON): only the Host may call it, with `Authorization: Bearer
  $WORKBENCH_HOST_TOKEN`. The acting person/agent is passed as a verified
  transport identity (`provider`, `bot_id`, `external_user_id`) and resolved
  here; the Host cannot name a principal or role directly.
- `/` dashboard (GET): read-only owner view behind HTTP Basic auth with
  `$WORKBENCH_DASHBOARD_TOKEN`; it can never write.

Bind to localhost / a private network (Tailscale); this is not an Internet API.
"""
from __future__ import annotations

import base64
import hmac
import html
import json
import sqlite3
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Callable

from .approvals import ApprovalService
from .contracts import ActorContext
from .dispatcher import Dispatcher
from .errors import Forbidden, InvalidOperation, WorkbenchError
from .identity import SqlIdentityProvider, finish_inbound, record_edit, record_inbound
from .insights import schedule_load, stagnation
from . import views
from .outboxes import DeliveryOutbox, MemoryPointerQueue
from .runtime_bridge import HostOutcome
from .scheduler import fire_due, observe_watch
from .storage import utc_now
from .task_service import SqliteTaskService


class WorkbenchApp:
    def __init__(self, database: str | Path, *, host_token: str, dashboard_token: str | None = None,
                 dashboard_workspace: str | None = None, dashboard_principal: str | None = None,
                 clock: Callable[[], str] = utc_now, life_adapter=None, status_token: str | None = None,
                 owner_token: str | None = None, companion_token: str | None = None, companion_bot_id: str = "xiyue"):
        if not host_token or len(host_token) < 16:
            raise ValueError("WORKBENCH_HOST_TOKEN must be set (16+ chars)")
        self.db = str(database)
        self.host_token, self.dashboard_token = host_token, dashboard_token
        self.dashboard_actor = (ActorContext(dashboard_workspace, dashboard_principal, "owner", "dashboard")
                                if dashboard_token and dashboard_workspace and dashboard_principal else None)
        self.clock = clock
        self.life = life_adapter  # LifeStateAdapter for the owner's Life view only; never on the Host API
        # Read-only token for workbench.status.v1 (desktop indicator); too short counts as not configured.
        self.status_token = status_token if status_token and len(status_token) >= 24 else None
        self.host_seen_at: float | None = None  # wall time of the Host's last authenticated call
        # The owner in person (DEV_LOG rule 9): the owner token signs in to the dashboard with write forms.
        # Same workspace and principal as the dashboard, actor source "owner" (direct, no chat in between).
        self.owner_token = owner_token if owner_token and len(owner_token) >= 16 else None
        self.owner_actor = (ActorContext(dashboard_workspace, dashboard_principal, "owner", "owner")
                            if self.owner_token and dashboard_workspace and dashboard_principal else None)
        # A companion agent of the owner (Xiyue): task tools and inbound recording only, under its own bot id,
        # never the Host's wake/approval/delivery routes, and it does not count as the work bot being online.
        self.companion_token = companion_token if companion_token and len(companion_token) >= 24 else None
        self.companion_bot_id = companion_bot_id
        self.tasks = SqliteTaskService(self.db, clock=clock)
        self.identities = SqlIdentityProvider(self.db)
        self.approvals = ApprovalService(self.db, clock=clock)
        self.delivery = DeliveryOutbox(self.db, clock=clock)
        self.memory = MemoryPointerQueue(self.db, clock=clock)
        self.dispatcher = Dispatcher(self.db, host=None, clock=clock)  # Host pulls; no push port here

    # ----------------------------------------------------------------- routing
    def actor(self, body: dict) -> ActorContext:
        ident = body.get("identity") or {}
        return self.identities.resolve_authenticated(
            provider=str(ident.get("provider", "")), connection_id=str(ident.get("bot_id", "")),
            external_user_id=str(ident.get("external_user_id", "")), workspace_id=str(body.get("workspace_id", "")))

    def handle(self, path: str, body: dict) -> Any:
        now = self.clock()
        ws = body.get("workspace_id")
        routes: dict[str, Callable[[], Any]] = {
            "/v1/intake/inbound": lambda: record_inbound(self.db, clock=self.clock, **{k: body[k] for k in (
                "workspace_id", "platform", "bot_id", "update_id", "chat_id", "message_id",
                "external_user_id", "raw_source_ref")}, handled_inline=bool(body.get("handled_inline", True))),
            "/v1/intake/edited": lambda: record_edit(self.db, clock=self.clock, **{k: body[k] for k in (
                "workspace_id", "platform", "bot_id", "update_id", "chat_id", "message_id",
                "external_user_id", "raw_source_ref")}),
            "/v1/intake/finish":lambda: finish_inbound(self.db, workspace_id=ws, inbox_id=body["inbox_id"],
                                                        ok=bool(body.get("ok")), clock=self.clock),
            "/v1/tools/list_tasks": lambda: self.tasks.overview(actor=self.actor(body), limit=int(body.get("limit", 50))),
            "/v1/tools/get_task": lambda: self.tasks.read(actor=self.actor(body), task_id=body.get("task_id")),
            "/v1/tools/change_tasks": lambda: self.tasks.apply(actor=self.actor(body),
                                                               idempotency_key=body.get("idempotency_key"),
                                                               operations=body.get("operations")),
            "/v1/tools/whats_new": lambda: self.tasks.changes_since(actor=self.actor(body),
                                                                    view=body.get("view", "agent"),
                                                                    mark_seen=bool(body.get("mark_seen", True))),
            "/v1/evaluate_action": lambda: self.tasks.evaluate_action(
                actor=self.actor(body), task_id=body.get("task_id"), action=body.get("action"),
                triggered_conditions=body.get("triggered_conditions") or [], recipient_id=body.get("recipient_id")),
            "/v1/insights/stagnation": lambda: stagnation(self.db, actor=self.actor(body), now=now),
            "/v1/insights/schedule_load": lambda: schedule_load(self.db, actor=self.actor(body), now=now),
            "/v1/views/calendar": lambda: views.calendar(
                self.db, actor=self.actor(body), start=body["start"], end=body["end"],
                project_id=body.get("project_id"), requester_id=body.get("requester_id")),
            "/v1/views/timeline": lambda: views.timeline(
                self.db, actor=self.actor(body), start=body["start"], end=body["end"],
                project_id=body.get("project_id"), requester_id=body.get("requester_id"),
                limit=body.get("limit", 300)),
            "/v1/views/projects": lambda: views.projects(
                self.db, actor=self.actor(body), project_id=body.get("project_id"),
                requester_id=body.get("requester_id")),
            "/v1/wake/claim": lambda: self._claim(ws, int(body.get("lease_seconds", 600))),
            "/v1/wake/settle": lambda: self.dispatcher.settle(ws, body["event_id"], HostOutcome(
                body["status"], body["run_ref"], body.get("session_ref"), body.get("deferred_ref"))),
            "/v1/approvals/request": lambda: self.approvals.request(
                host_actor=self.actor(body), wake_event_id=body["wake_event_id"], tool_name=body["tool_name"],
                tool_input=body.get("tool_input"), summary=body["summary"],
                host_deferred_ref=body["host_deferred_ref"], expires_at=body["expires_at"],
                task_id=body.get("task_id"), approver_id=None),
            "/v1/approvals/decide": lambda: self.approvals.decide(
                approver=self.actor(body), approval_id=body["approval_id"], approve=body.get("approve"),
                source_ref=body.get("source_ref", "")),
            "/v1/approvals/consume": lambda: self.approvals.consume(
                workspace_id=ws, tool_name=body["tool_name"], tool_input=body.get("tool_input"),
                session_ref=body.get("session_ref", ""), task_id=body.get("task_id")),
            "/v1/wake/unknown": lambda: self.dispatcher.unknown(ws),
            "/v1/delivery/enqueue": lambda: self.delivery.enqueue(
                workspace_id=ws, recipient_id=body["recipient_id"], channel=body.get("channel", "telegram"),
                payload=body.get("payload") or {}, dedupe_key=body["dedupe_key"], task_id=body.get("task_id"),
                reminder_id=body.get("reminder_id")),
            "/v1/delivery/claim": lambda: self.delivery.claim(workspace_id=ws, limit=int(body.get("limit", 10))),
            "/v1/delivery/settle": lambda: self.delivery.settle(
                workspace_id=ws, delivery_id=body["delivery_id"], status=body["status"],
                provider_message_id=body.get("provider_message_id")),
            "/v1/delivery/report": lambda: self.delivery.report(workspace_id=ws),
            "/v1/memory/authorize": lambda: self._memory_authorize(body),
            "/v1/memory/enqueue": lambda: self.memory.enqueue(
                workspace_id=ws, task_event_id=body["task_event_id"], scope=body.get("scope", "workspace"),
                source_ref=body["source_ref"]),
            "/v1/memory/claim": lambda: self.memory.claim(workspace_id=ws),
            "/v1/memory/settle": lambda: self.memory.settle(workspace_id=ws, pointer_id=body["pointer_id"],
                                                            status=body["status"]),
            "/v1/timers/fire": lambda: fire_due(self.db, now=now),
            "/v1/notices/sweep": lambda: __import__("workdesk.notices", fromlist=["sweep"]).sweep(self.db, now=now),
            "/v1/imports/rows": lambda: __import__("workdesk.imports", fromlist=["import_rows"]).import_rows(
                self.db, actor=self.actor(body), source_name=body["source_name"], period=body["period"],
                rows=body["rows"], clock=self.clock),
            "/v1/watches/observe": lambda: observe_watch(
                self.db, workspace_id=ws, watch_id=body["watch_id"], observed_value=str(body["observed_value"]),
                source_ref=body["source_ref"], now=now),
        }
        if path not in routes:
            raise LookupError(path)
        try:
            return routes[path]()
        except KeyError as exc:
            raise InvalidOperation(f"missing field {exc}") from exc

    def _claim(self, ws: str, lease_seconds: int) -> dict | None:
        event = _event_dict(self.dispatcher.claim(ws, lease_seconds=lease_seconds))
        if event is not None:  # an approval decision points back at the blocked Host session
            from .storage import connect
            from contextlib import closing
            with closing(connect(self.db)) as conn:
                row = conn.execute("SELECT deferred_ref FROM wake_events WHERE workspace_id=? AND id=?",
                                   (ws, event["event_id"])).fetchone()
            event["deferred_ref"] = row["deferred_ref"] if row else None
            with closing(connect(self.db)) as conn:
                # Who a fired reminder is for (team members only; outsiders get fixed-text follow-ups,
                # never a model Run's reply) and which Agent currently executes the task.
                rem = conn.execute(
                    """SELECT r.recipient_principal_id FROM reminders r JOIN memberships m
                         ON m.workspace_id=r.workspace_id AND m.principal_id=r.recipient_principal_id
                       WHERE r.workspace_id=? AND r.id=? AND m.role IN ('owner','editor')""",
                    (ws, event["origin_event_id"])).fetchone() if event["origin"] == "workbench.timer" else None
                resp = conn.execute(
                    "SELECT current_executor_id FROM task_responsibilities WHERE workspace_id=? AND task_id=? AND status='active'",
                    (ws, event["task_id"])).fetchone() if event["task_id"] else None
            event["recipient_id"] = rem["recipient_principal_id"] if rem else None
            event["executor_id"] = resp["current_executor_id"] if resp else None
        return event

    def _memory_authorize(self, body: dict) -> dict:
        """Work memory is a USER-scope like any other: only principals with an explicit grant
        may search or write it. A requester has none unless the owner grants one."""
        from .user_context import SqlMemoryGrantChecker
        actor = self.actor(body)
        action = body.get("action")
        try:
            SqlMemoryGrantChecker(self.db).assert_allowed(
                principal_id=actor.principal_id, workspace_id=actor.workspace_id,
                user_id=f"workbench:{actor.workspace_id}", action=action)
        except PermissionError:
            raise Forbidden("memory access denied")
        return {"allowed": True, "memory_user_id": f"workbench:{actor.workspace_id}"}

    def check_host(self, header: str | None) -> bool:
        expected = f"Bearer {self.host_token}"
        ok = bool(header) and hmac.compare_digest(header, expected)
        if ok:
            self.host_seen_at = time.time()  # Host liveness for the status indicator
        return ok

    def check_status(self, header: str | None) -> bool:
        return bool(self.status_token and header) and hmac.compare_digest(header, f"Bearer {self.status_token}")

    def status(self) -> dict:
        from .status import build_status
        return build_status(self.tasks, self.dashboard_actor, generated_at=self.clock(), host_seen_at=self.host_seen_at)

    def check_dashboard(self, header: str | None) -> str | None:
        """'owner' (owner token: write forms), 'viewer' (dashboard password: read-only) or None."""
        if not self.dashboard_actor or not header or not header.startswith("Basic "):
            return None
        try:
            _, _, password = base64.b64decode(header[6:]).decode("utf-8").partition(":")
        except Exception:  # noqa: BLE001
            return None
        if self.owner_actor and hmac.compare_digest(password, self.owner_token):
            return "owner"
        return "viewer" if hmac.compare_digest(password, self.dashboard_token) else None

    COMPANION_ROUTES = frozenset({"/v1/tools/list_tasks", "/v1/tools/get_task", "/v1/tools/change_tasks",
                                  "/v1/tools/whats_new", "/v1/intake/inbound", "/v1/intake/finish"})

    def check_companion(self, header: str | None, path: str, body: dict) -> bool:
        """The companion token only reaches its routes, and only as its own bot (identity or inbound bot_id)."""
        if not self.companion_token or not header or path not in self.COMPANION_ROUTES:
            return False
        if not hmac.compare_digest(header, f"Bearer {self.companion_token}"):
            return False
        bot = (body.get("identity") or {}).get("bot_id") if path.startswith("/v1/tools/") else body.get("bot_id")
        return path == "/v1/intake/finish" or bot == self.companion_bot_id

    def owner_form(self, form: dict) -> str:
        """Run one owner form; returns the success message (raises WorkbenchError on refusal)."""
        from contextlib import closing
        from zoneinfo import ZoneInfo
        from .owner_ui import operations
        from .storage import connect
        with closing(connect(self.db)) as conn:
            row = conn.execute("SELECT timezone FROM workspaces WHERE id=?", (self.owner_actor.workspace_id,)).fetchone()
        ops, message = operations(form, ZoneInfo(row["timezone"] if row else "Asia/Shanghai"))
        self.tasks.apply(actor=self.owner_actor, idempotency_key=f"owner-web:{form.get('nonce', '')}", operations=ops)
        return message


def _event_dict(event):
    if event is None:
        return None
    return {k: getattr(event, k) for k in ("workspace_id", "event_id", "origin", "origin_event_id", "kind",
                                           "source_ref", "created_at", "task_id")}


def make_handler(app: WorkbenchApp, *, host_api: bool = True):
    """host_api=False: the LAN listener — dashboard pages and /status/v1 only, no POST at all, so the
    Host API (the only write path) never leaves loopback."""
    from urllib.parse import parse_qs
    from urllib.parse import quote, urlparse
    from .dashboard import (render_calendar, render_dashboard, render_life, render_projects, render_task,
                            render_timeline)
    from .owner_ui import csrf_value, new_task_form, notice, task_panel
    pages = {"/calendar", "/timeline", "/projects", "/life"}

    class Handler(BaseHTTPRequestHandler):
        server_version = "WorkBench/1"

        def log_message(self, fmt, *args):  # no request bodies or tokens in logs
            pass

        def _send(self, status: int, payload: Any, content_type: str = "application/json") -> None:
            data = payload if isinstance(payload, bytes) else json.dumps(payload, ensure_ascii=False).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", content_type + "; charset=utf-8")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def do_GET(self):
            if self.path.split("?")[0] == "/healthz":
                return self._send(200, {"ok": True})
            if self.path.split("?")[0] == "/status/v1":
                return _status_reply(self, app)
            route, _, query = self.path.partition("?")
            if route not in ("/", "/dashboard", *pages) and not route.startswith("/task/"):
                return self._send(404, {"code": "NOT_FOUND"})
            who = app.check_dashboard(self.headers.get("Authorization"))
            if not who:
                self.send_response(401)
                self.send_header("WWW-Authenticate", 'Basic realm="WorkBench"')
                self.end_headers()
                return
            # Query values only narrow a view; `views` checks them against the dashboard owner's scope.
            q = {k: v[0] for k, v in parse_qs(query).items() if v and v[0]}
            actor, now = app.dashboard_actor, app.clock()
            narrow = {"project": q.get("project"), "requester": q.get("requester")}
            owner = who == "owner"
            csrf = csrf_value(app.owner_token) if owner else ""
            try:
                if route.startswith("/task/"):
                    page = render_task(app.tasks, actor, app.db, route[len("/task/"):], now,
                                       owner_panel=(lambda t, slot, tz: task_panel(csrf, t, slot, tz)) if owner else None,
                                       notice=notice(q) if owner else "")
                elif route == "/calendar":
                    page = render_calendar(app.tasks, actor, app.db, now, range_kind=q.get("range", "week"), **narrow)
                elif route == "/timeline":
                    page = render_timeline(app.tasks, actor, app.db, now, range_kind=q.get("range", "week"), **narrow)
                elif route == "/projects":
                    page = render_projects(app.tasks, actor, app.db, now, **narrow)
                elif route == "/life":
                    page = render_life(actor, app.db, now, app.life)
                else:
                    page = render_dashboard(app.tasks, actor, app.db, now,
                                            owner_html=(notice(q) + new_task_form(csrf)) if owner else "")
            except WorkbenchError as exc:
                return self._send(404 if exc.code in ("NOT_FOUND", "FORBIDDEN") else 400, {"code": exc.code})
            self._send(200, page.encode("utf-8"), "text/html")

        def _owner_post(self):
            """POST /owner/ops: one owner form, then back to the page it came from (303) with a notice."""
            if app.check_dashboard(self.headers.get("Authorization")) != "owner":
                return self._send(401 if not app.owner_actor else 403, {"code": "UNAUTHORIZED"})
            origin = self.headers.get("Origin")
            if origin and origin != "null" and urlparse(origin).netloc != self.headers.get("Host"):
                return self._send(403, {"code": "CROSS_ORIGIN"})
            length = int(self.headers.get("Content-Length") or 0)
            if length > 20_000:
                return self._send(413, {"code": "TOO_LARGE"})
            form = {k: v[0] for k, v in parse_qs(self.rfile.read(length).decode("utf-8")).items() if v}
            if not hmac.compare_digest(form.get("csrf", ""), csrf_value(app.owner_token)):
                return self._send(403, {"code": "CSRF"})
            back = form.get("back", "/")
            if not back.startswith("/") or back.startswith("//"):
                back = "/"
            try:
                result = "ok=" + quote(app.owner_form(form))
            except WorkbenchError as exc:
                result = "err=" + quote(str(exc)[:300])
            except (ValueError, TypeError, KeyError) as exc:
                result = "err=" + quote(str(exc)[:200])
            self.send_response(303)
            self.send_header("Location", f"{back.split('?')[0]}?{result}")
            self.send_header("Content-Length", "0")
            self.end_headers()

        def do_POST(self):
            if self.path.split("?")[0] == "/owner/ops":
                return self._owner_post()
            if not host_api:
                return self._send(405, {"code": "METHOD_NOT_ALLOWED"})
            auth = self.headers.get("Authorization")
            host = app.check_host(auth)
            # Nothing is read from a caller holding neither token; a companion is checked against the body below.
            if not host and not (app.companion_token and auth and hmac.compare_digest(auth, f"Bearer {app.companion_token}")):
                return self._send(401, {"code": "UNAUTHORIZED"})
            try:
                length = int(self.headers.get("Content-Length") or 0)
                if length > 1_000_000:
                    return self._send(413, {"code": "TOO_LARGE"})
                body = json.loads(self.rfile.read(length) or b"{}")
                if not isinstance(body, dict):
                    raise InvalidOperation("body must be a JSON object")
                if not host and not app.check_companion(auth, self.path, body):
                    return self._send(403, {"code": "FORBIDDEN"})
                return self._send(200, {"ok": True, "result": app.handle(self.path, body)})
            except LookupError:
                return self._send(404, {"code": "NOT_FOUND"})
            except WorkbenchError as exc:
                status = {"NOT_FOUND": 404, "FORBIDDEN": 403, "VERSION_CONFLICT": 409,
                          "IDEMPOTENCY_CONFLICT": 409}.get(exc.code, 422)
                return self._send(status, {"ok": False, "error": exc.to_dict()})
            except (ValueError, TypeError) as exc:
                return self._send(422, {"ok": False, "error": {"code": "INVALID", "message": str(exc)[:300]}})
            except sqlite3.OperationalError as exc:
                return self._send(503, {"ok": False, "error": {"code": "BUSY", "message": str(exc)[:200]}})
            except sqlite3.Error as exc:
                return self._send(409, {"ok": False, "error": {"code": "CONSTRAINT", "message": str(exc)[:200]}})

    return Handler


def _status_reply(handler, app: "WorkbenchApp") -> None:
    if not app.status_token or not app.dashboard_actor:
        return handler._send(404, {"code": "NOT_FOUND"})
    if not app.check_status(handler.headers.get("Authorization")):
        return handler._send(401, {"code": "UNAUTHORIZED"})
    try:
        return handler._send(200, app.status())
    except WorkbenchError as exc:
        return handler._send(503, {"code": exc.code})


def make_status_handler(app: WorkbenchApp):
    """A listener that serves only GET /status/v1 — what may be exposed to the LAN for the indicator."""
    class StatusHandler(BaseHTTPRequestHandler):
        server_version = "WorkBench-status/1"

        def log_message(self, fmt, *args):
            pass

        def _send(self, status: int, payload: Any, content_type: str = "application/json") -> None:
            data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", content_type + "; charset=utf-8")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def do_GET(self):
            if self.path.split("?")[0] != "/status/v1":
                return self._send(404, {"code": "NOT_FOUND"})
            return _status_reply(self, app)

        def do_POST(self):
            return self._send(405, {"code": "METHOD_NOT_ALLOWED"})
    return StatusHandler


def serve(app: WorkbenchApp, *, host: str = "127.0.0.1", port: int = 8790, timer_seconds: int = 30) -> None:
    stop = threading.Event()

    import os
    from .notices import sweep
    from .promotion import review_delegations
    from .storage import backup
    from .watchers import poll_watches
    backup_dir = os.environ.get("WORKBENCH_BACKUP_DIR")

    def timers():
        ticks = 0
        while not stop.wait(timer_seconds):
            ticks += 1
            jobs = [("timers", lambda: fire_due(app.db, now=app.clock())),
                    ("notices", lambda: sweep(app.db, now=app.clock()))]
            if ticks % max(1, 300 // timer_seconds) == 0:       # every ~5 minutes
                jobs.append(("watches", lambda: poll_watches(app.db, now=app.clock())))
            if ticks % max(1, 86400 // timer_seconds) == 1:     # once a day (and at start)
                jobs.append(("delegation review", lambda: review_delegations(app.db, now=app.clock())))
                if backup_dir:
                    jobs.append(("backup", lambda: backup(app.db, backup_dir)))
            for name, job in jobs:
                try:
                    job()
                except Exception as exc:  # noqa: BLE001 - keep the loop alive; surface in logs
                    print(f"{name} error: {exc}", flush=True)

    threading.Thread(target=timers, daemon=True).start()
    # Optional second listener with only GET /status/v1, e.g. WORKBENCH_STATUS_BIND=0.0.0.0:8791, so a
    # desktop indicator on another machine can read it while everything else stays on loopback.
    # LAN listener for other devices (phone, other PCs): read-only dashboard + /status/v1, no Host API.
    lan_bind = os.environ.get("WORKBENCH_LAN_BIND")
    if lan_bind:
        l_host, _, l_port = lan_bind.rpartition(":")
        lan_server = ThreadingHTTPServer((l_host or "0.0.0.0", int(l_port)), make_handler(app, host_api=False))
        threading.Thread(target=lan_server.serve_forever, daemon=True).start()
        print(f"WorkBench LAN (read-only dashboard + status) listening on {l_host}:{l_port}", flush=True)
    status_bind = os.environ.get("WORKBENCH_STATUS_BIND")
    if status_bind and app.status_token:
        s_host, _, s_port = status_bind.rpartition(":")
        status_server = ThreadingHTTPServer((s_host or "127.0.0.1", int(s_port)), make_status_handler(app))
        threading.Thread(target=status_server.serve_forever, daemon=True).start()
        print(f"WorkBench status listening on {s_host}:{s_port}", flush=True)
    server = ThreadingHTTPServer((host, port), make_handler(app))
    print(f"WorkBench listening on {host}:{port}", flush=True)
    try:
        server.serve_forever()
    finally:
        stop.set()
        server.server_close()
