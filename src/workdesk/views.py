"""Read-only views for the dashboard and the Host: calendar, timeline, project pool.

Every query is scoped to the caller's workspace and checked against their membership role
(`SqliteTaskService._role`). Requesters only see their own requests and only the outward parts of
them (official due, state), never soft dues, reminders, executors or Agent runs. Filters
(`project_id`, `requester_id`) can only narrow what the caller may already see, never widen it.
Nothing here writes.
"""
from __future__ import annotations

import re
from contextlib import closing
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from .contracts import ActorContext
from .errors import InvalidOperation, NotFound
from .storage import connect
from .task_service import READ_ALL_ROLES, REQUESTER_EVENT_SCOPE, SqliteTaskService

_UTC = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$")


def _window(start: str, end: str) -> tuple[str, str]:
    if not (isinstance(start, str) and _UTC.match(start) and isinstance(end, str) and _UTC.match(end)) or start >= end:
        raise InvalidOperation("start/end must be UTC 'YYYY-MM-DDTHH:MM:SSZ' with start < end", field="start")
    return start, end


def _open(database, actor):
    conn = connect(database)
    role = SqliteTaskService._role(conn, actor)
    tz = ZoneInfo(conn.execute("SELECT timezone FROM workspaces WHERE id=?", (actor.workspace_id,)).fetchone()[0])
    return conn, role, tz


def _narrow(conn, actor, project_id, requester_id) -> tuple[str, list]:
    """Optional filters on alias `t`; they only ever add conditions to the caller's own visibility."""
    clause, args = "", []
    if project_id is not None:
        if not isinstance(project_id, str) or not conn.execute(
                "SELECT 1 FROM projects WHERE workspace_id=? AND id=?", (actor.workspace_id, project_id)).fetchone():
            raise NotFound("project not found")
        clause += " AND t.project_id=?"
        args.append(project_id)
    if requester_id is not None:
        if not isinstance(requester_id, str):
            raise InvalidOperation("requester_id must be a principal id", field="requester_id")
        clause += " AND t.requester_id=?"
        args.append(requester_id)
    return clause, args


def _task_filter(conn, actor, role, project_id, requester_id) -> tuple[str, list]:
    """Visibility (requesters: only their own requests) plus the narrowing filters, on alias `t`."""
    clause, args = _narrow(conn, actor, project_id, requester_id)
    if role in READ_ALL_ROLES:
        return clause, args
    return " AND t.requester_id=?" + clause, [actor.principal_id, *args]


def _day(ts: str, tz: ZoneInfo) -> str:
    return datetime.fromisoformat(ts[:-1] + "+00:00").astimezone(tz).date().isoformat()


def calendar(database: str | Path, *, actor: ActorContext, start: str, end: str,
             project_id: str | None = None, requester_id: str | None = None) -> dict:
    """Official dues, soft dues, follow-ups and human requests in [start, end), each its own item."""
    start, end = _window(start, end)
    conn, role, tz = _open(database, actor)
    with closing(conn):
        where, args = _task_filter(conn, actor, role, project_id, requester_id)
        ws, member = actor.workspace_id, role in READ_ALL_ROLES
        items = []
        for r in conn.execute(
                f"""SELECT t.id, t.title, t.state, t.official_due_at, t.official_due_local, t.soft_due_at FROM tasks t
                    WHERE t.workspace_id=? AND t.state NOT IN ('DONE','CANCELLED') {where}
                    AND (t.official_due_at >= ? AND t.official_due_at < ? OR t.soft_due_at >= ? AND t.soft_due_at < ?)""",
                (ws, *args, start, end, start, end)):
            if r["official_due_at"] and start <= r["official_due_at"] < end:
                items.append({"type": "official_due", "at": r["official_due_at"], "local_date": r["official_due_local"]
                              or _day(r["official_due_at"], tz), "task_id": r["id"], "title": r["title"], "state": r["state"]})
            if member and r["soft_due_at"] and start <= r["soft_due_at"] < end:
                items.append({"type": "soft_due", "at": r["soft_due_at"], "local_date": _day(r["soft_due_at"], tz),
                              "task_id": r["id"], "title": r["title"], "state": r["state"]})
        if member:
            for r in conn.execute(
                    f"""SELECT r.id, r.due_at, r.kind, r.recipient_principal_id, t.id AS task_id, t.title FROM reminders r
                        JOIN tasks t ON t.workspace_id=r.workspace_id AND t.id=r.task_id
                        WHERE r.workspace_id=? AND r.status='scheduled' AND r.due_at >= ? AND r.due_at < ? {where}""",
                    (ws, start, end, *args)):
                items.append({"type": "follow_up", "at": r["due_at"], "local_date": _day(r["due_at"], tz),
                              "task_id": r["task_id"], "title": r["title"], "reminder_id": r["id"],
                              "recipient_id": r["recipient_principal_id"]})
        # Human requests: members see all of them; anyone else only the ones addressed to them.
        narrow, narrow_args = _narrow(conn, actor, project_id, requester_id)
        mine, mine_args = ("", []) if member else (" AND h.assignee_principal_id=?", [actor.principal_id])
        for r in conn.execute(
                f"""SELECT h.id, h.title, h.due_at, h.assignee_principal_id, h.task_id FROM human_action_requests h
                    LEFT JOIN tasks t ON t.workspace_id=h.workspace_id AND t.id=h.task_id
                    WHERE h.workspace_id=? AND h.status IN ('pending','in_progress') AND h.due_at >= ? AND h.due_at < ?
                    {mine} {narrow}""", (ws, start, end, *mine_args, *narrow_args)):
            items.append({"type": "human_action", "at": r["due_at"], "local_date": _day(r["due_at"], tz),
                          "task_id": r["task_id"], "title": r["title"], "human_action_id": r["id"],
                          "assignee_id": r["assignee_principal_id"]})
    items.sort(key=lambda i: (i["at"], i["type"]))
    days: dict[str, list] = {}
    for item in items:
        days.setdefault(item["local_date"], []).append(item)
    return {"start": start, "end": end, "items": items, "days": [{"date": d, "items": v} for d, v in sorted(days.items())]}


def timeline(database: str | Path, *, actor: ActorContext, start: str, end: str,
             project_id: str | None = None, requester_id: str | None = None, limit: int = 300) -> dict:
    """Task history (created, changed, delayed, delivered, done...) plus Agent runs, newest first."""
    start, end = _window(start, end)
    if type(limit) is not int or not 1 <= limit <= 1000:
        raise InvalidOperation("limit must be 1-1000", field="limit")
    conn, role, tz = _open(database, actor)
    with closing(conn):
        where, args = _task_filter(conn, actor, role, project_id, requester_id)
        narrow, narrow_args = _narrow(conn, actor, project_id, requester_id)
        member = role in READ_ALL_ROLES
        # A requester sees the shared requester rule (their own events + outward milestones).
        scope, scope_args = ("", []) if member else (REQUESTER_EVENT_SCOPE, [actor.principal_id, actor.principal_id])
        rows = conn.execute(
            f"""SELECT e.id, e.task_id, t.title, e.kind, e.actor_id, e.reason, e.evidence_type, e.source_ref,
                       e.occurred_at FROM task_events e JOIN tasks t ON t.workspace_id=e.workspace_id AND t.id=e.task_id
                WHERE e.workspace_id=? AND e.occurred_at >= ? AND e.occurred_at < ? {scope} {narrow}
                ORDER BY e.occurred_at DESC, e.rowid DESC LIMIT ?""",
            (actor.workspace_id, start, end, *scope_args, *narrow_args, limit)).fetchall()
        items = [{"type": "task_event", "at": r["occurred_at"], "local_date": _day(r["occurred_at"], tz),
                  "event_id": r["id"], "task_id": r["task_id"], "title": r["title"], "kind": r["kind"],
                  "actor_id": r["actor_id"], "reason": r["reason"],
                  **({"evidence_type": r["evidence_type"], "source_ref": r["source_ref"]} if member else {})}
                 for r in rows]
        if member:  # Agent execution record: background Runs that touched a task
            for r in conn.execute(
                    f"""SELECT w.id, w.kind, w.status, w.host_run_ref, w.updated_at, t.id AS task_id, t.title
                        FROM wake_events w JOIN tasks t ON t.workspace_id=w.workspace_id AND t.id=w.task_id
                        WHERE w.workspace_id=? AND w.status IN ('completed','failed','unknown')
                        AND COALESCE(w.host_run_ref,'') NOT IN ('inline','approval-command')
                        AND w.updated_at >= ? AND w.updated_at < ? {where} LIMIT ?""",
                    (actor.workspace_id, start, end, *args, limit)):
                items.append({"type": "agent_run", "at": r["updated_at"], "local_date": _day(r["updated_at"], tz),
                              "wake_event_id": r["id"], "task_id": r["task_id"], "title": r["title"],
                              "kind": r["kind"], "status": r["status"], "run_ref": r["host_run_ref"]})
    items.sort(key=lambda i: i["at"], reverse=True)
    return {"start": start, "end": end, "items": items[:limit]}


def projects(database: str | Path, *, actor: ActorContext, project_id: str | None = None,
             requester_id: str | None = None) -> dict:
    """Project pool: each project with its visible open tasks, executor, progress, dues and blocks."""
    conn, role, tz = _open(database, actor)
    with closing(conn):
        where, args = _task_filter(conn, actor, role, project_id, requester_id)
        member = role in READ_ALL_ROLES
        tasks = conn.execute(
            f"""SELECT t.id, t.title, t.state, t.project_id, t.requester_id, t.assignee_id, t.next_action,
                       t.official_due_at, t.official_due_local, t.soft_due_at, t.updated_at,
                       r.current_executor_id, r.status AS responsibility_status
                FROM tasks t LEFT JOIN task_responsibilities r ON r.workspace_id=t.workspace_id AND r.task_id=t.id
                WHERE t.workspace_id=? AND t.state NOT IN ('CANCELLED') {where}
                ORDER BY COALESCE(t.official_due_at, t.soft_due_at, '9999'), t.updated_at DESC""",
            (actor.workspace_id, *args)).fetchall()
        titles = {r["id"]: dict(r) for r in conn.execute(
            "SELECT id, title, goal, state FROM projects WHERE workspace_id=?", (actor.workspace_id,))}
    pool: dict[str | None, dict] = {}
    for t in tasks:
        key = t["project_id"]
        if key not in pool:
            meta = titles.get(key, {"id": None, "title": "未归入项目", "goal": "", "state": "active"})
            pool[key] = {**meta, "tasks": [], "counts": {}, "blocked": 0, "next_official_due": None}
        p = pool[key]
        task = {"id": t["id"], "title": t["title"], "state": t["state"], "official_due_at": t["official_due_at"],
                "official_due_local": t["official_due_local"]}
        if member:
            active = t["responsibility_status"] == "active"
            task.update(next_action=t["next_action"], soft_due_at=t["soft_due_at"], requester_id=t["requester_id"],
                        executor_id=t["current_executor_id"] if active else t["assignee_id"])
        p["tasks"].append(task)
        p["counts"][t["state"]] = p["counts"].get(t["state"], 0) + 1
        p["blocked"] += t["state"] == "BLOCKED"
        if t["state"] != "DONE" and t["official_due_at"] and (
                p["next_official_due"] is None or t["official_due_at"] < p["next_official_due"]):
            p["next_official_due"] = t["official_due_at"]
    for p in pool.values():
        total = sum(p["counts"].values())
        p["progress"] = {"done": p["counts"].get("DONE", 0), "total": total}
    if member and project_id is None and requester_id is None:  # empty active projects still belong in the pool
        for pid, meta in titles.items():
            if pid not in pool and meta["state"] in ("active", "paused"):
                pool[pid] = {**meta, "tasks": [], "counts": {}, "blocked": 0, "next_official_due": None,
                             "progress": {"done": 0, "total": 0}}
    ordered = sorted(pool.values(), key=lambda p: (p["id"] is None, p["next_official_due"] or "9999", p["title"]))
    return {"projects": ordered}
