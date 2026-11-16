"""Read-only projections: stagnation review and schedule load.

Both report facts from task events and due dates with their sources; they
suggest, they never reprioritise or move dates on their own.
"""
from __future__ import annotations

import json
from collections import defaultdict
from contextlib import closing
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from .contracts import ActorContext
from .errors import Forbidden
from .storage import connect


def _check(conn, actor: ActorContext) -> None:
    row = conn.execute(
        "SELECT role FROM memberships WHERE workspace_id=? AND principal_id=? AND active=1",
        (actor.workspace_id, actor.principal_id)).fetchone()
    if row is None or row["role"] != actor.role or row["role"] == "requester":
        raise Forbidden("insights are for workspace members, not requesters")


def stagnation(database: str | Path, *, actor: ActorContext, now: str, min_delays: int = 2,
               waiting_days: int = 5) -> list[dict]:
    """Tasks postponed repeatedly or waiting too long, with each delay's stated reason."""
    cutoff = (datetime.fromisoformat(now[:-1]) - timedelta(days=waiting_days)).strftime("%Y-%m-%dT%H:%M:%SZ")
    out = []
    with closing(connect(database)) as conn:
        _check(conn, actor)
        for task in conn.execute(
                """SELECT id,title,state,updated_at FROM tasks WHERE workspace_id=?
                   AND state NOT IN ('DONE','CANCELLED')""", (actor.workspace_id,)):
            delays = [{"at": e["occurred_at"], "reason": e["reason"],
                       "delay_kind": json.loads(e["after_json"]).get("delay_kind"), "source_ref": e["source_ref"]}
                      for e in conn.execute(
                          """SELECT occurred_at,reason,after_json,source_ref FROM task_events
                             WHERE workspace_id=? AND task_id=? AND kind='delayed' ORDER BY occurred_at""",
                          (actor.workspace_id, task["id"]))]
            waiting_since = None
            if task["state"] in ("WAITING", "BLOCKED"):
                row = conn.execute(
                    """SELECT occurred_at FROM task_events WHERE workspace_id=? AND task_id=?
                       AND kind='state_changed' ORDER BY occurred_at DESC LIMIT 1""",
                    (actor.workspace_id, task["id"])).fetchone()
                waiting_since = row["occurred_at"] if row else task["updated_at"]
            long_wait = waiting_since is not None and waiting_since <= cutoff
            if len(delays) >= min_delays or long_wait:
                deps = [dict(d) for d in conn.execute(
                    """SELECT awaited_outcome,responsible_principal_id,created_at FROM dependencies
                       WHERE workspace_id=? AND task_id=? AND status='open'""", (actor.workspace_id, task["id"]))]
                out.append({"task_id": task["id"], "title": task["title"], "state": task["state"],
                            "delays": delays, "waiting_since": waiting_since, "waiting_on": deps})
    return out


def schedule_load(database: str | Path, *, actor: ActorContext, now: str, days: int = 14,
                  max_per_day: int = 3) -> dict:
    """Open deadlines and human requests per local day; days above the limit are flagged."""
    with closing(connect(database)) as conn:
        _check(conn, actor)
        tz = ZoneInfo(conn.execute("SELECT timezone FROM workspaces WHERE id=?",
                                   (actor.workspace_id,)).fetchone()["timezone"])
        end = (datetime.fromisoformat(now[:-1]) + timedelta(days=days)).strftime("%Y-%m-%dT%H:%M:%SZ")
        per_day: dict[str, list] = defaultdict(list)
        rows = conn.execute(
            """SELECT id,title,COALESCE(soft_due_at,official_due_at) AS due, official_due_at IS NOT NULL AS official
               FROM tasks WHERE workspace_id=? AND state NOT IN ('DONE','CANCELLED')
               AND COALESCE(soft_due_at,official_due_at) BETWEEN ? AND ?""", (actor.workspace_id, now, end))
        for r in rows:
            day = datetime.fromisoformat(r["due"][:-1] + "+00:00").astimezone(tz).date().isoformat()
            per_day[day].append({"kind": "task", "id": r["id"], "title": r["title"], "official": bool(r["official"])})
        for r in conn.execute(
                """SELECT id,title,due_at FROM human_action_requests WHERE workspace_id=?
                   AND status IN ('pending','in_progress') AND due_at BETWEEN ? AND ?""",
                (actor.workspace_id, now, end)):
            day = datetime.fromisoformat(r["due_at"][:-1] + "+00:00").astimezone(tz).date().isoformat()
            per_day[day].append({"kind": "human_action", "id": r["id"], "title": r["title"]})
    days_out = [{"date": d, "items": items, "overloaded": len(items) > max_per_day}
                for d, items in sorted(per_day.items())]
    return {"days": days_out, "overloaded_days": [d["date"] for d in days_out if d["overloaded"]]}
