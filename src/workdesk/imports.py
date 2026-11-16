"""Monthly business-data import (e.g. "official dues for the next three months" exported
from an internal system) reconciled against the task pool.

Each row needs a stable `external_key` (case number) plus `title`, and optionally
`official_due_local` (YYYY-MM-DD or YYYY-MM-DDTHH:MM) and `goal`.

- new key            -> new INBOX task with the official due (verified_tool evidence, import source_ref)
- known key, same    -> nothing
- official due moved -> NOT overwritten; a decision request shows old/new for the owner
- title/goal changed -> recorded as an event only
- key missing now    -> a review request ("gone from the list: closed?") for still-open tasks
- the same file twice (same checksum) -> no-op
Manual fields (soft due, progress, focus, notes) are never touched.
"""
from __future__ import annotations

import csv
import hashlib
import io
import json
import uuid
from pathlib import Path
from typing import Callable

from .contracts import ActorContext
from .errors import Forbidden, InvalidOperation
from .storage import utc_now, write_transaction
from .task_service import _local_to_utc


def _dumps(v) -> str:
    return json.dumps(v, ensure_ascii=False, sort_keys=True)


def rows_from_csv(text: str, mapping: dict[str, str] | None = None) -> list[dict]:
    """Read a CSV export; `mapping` renames source columns (e.g. {"案号": "external_key"})."""
    mapping = mapping or {}
    out = []
    for raw in csv.DictReader(io.StringIO(text.lstrip("﻿"))):
        row = {mapping.get(k, k): (v or "").strip() for k, v in raw.items() if k is not None}
        if row.get("external_key"):
            out.append(row)
    return out


def import_rows(database: str | Path, *, actor: ActorContext, source_name: str, period: str, rows: list[dict],
                clock: Callable[[], str] = utc_now) -> dict:
    if not source_name or not period:
        raise InvalidOperation("source_name and period are required")
    keys = [r.get("external_key") for r in rows]
    if any(not k for k in keys) or len(set(keys)) != len(keys):
        raise InvalidOperation("every row needs a unique external_key")
    checksum = hashlib.sha256(_dumps(sorted(rows, key=lambda r: r["external_key"])).encode()).hexdigest()
    now = clock()
    ws = actor.workspace_id
    summary = {"new": [], "unchanged": 0, "due_changed": [], "details_changed": [], "missing": []}
    with write_transaction(database) as conn:
        role = conn.execute("SELECT role FROM memberships WHERE workspace_id=? AND principal_id=? AND active=1",
                            (ws, actor.principal_id)).fetchone()
        if role is None or role["role"] not in ("owner", "editor"):
            raise Forbidden("only owners and editors import business data")
        prior = conn.execute("SELECT id, summary_json FROM import_batches WHERE workspace_id=? AND source_name=? AND checksum=?",
                             (ws, source_name, checksum)).fetchone()
        if prior:
            return {"batch_id": prior["id"], "duplicate": True, **json.loads(prior["summary_json"])}
        w = conn.execute("SELECT owner_id, timezone FROM workspaces WHERE id=?", (ws,)).fetchone()
        owner, tz = w["owner_id"], w["timezone"]
        batch_id = f"imp-{uuid.uuid4().hex}"
        conn.execute("INSERT INTO import_batches(id,workspace_id,source_name,period,checksum,imported_by,created_at)"
                     " VALUES(?,?,?,?,?,?,?)", (batch_id, ws, source_name, period, checksum, actor.principal_id, now))

        def event(task_id, kind, after, reason, key):
            conn.execute(
                """INSERT INTO task_events(id,workspace_id,task_id,actor_id,kind,after_json,reason,evidence_type,
                     source_ref,occurred_at,recorded_at) VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
                (f"evt-{uuid.uuid4().hex}", ws, task_id, actor.principal_id, kind, _dumps(after), reason,
                 "verified_tool", f"import://{source_name}/{period}/{key}", now, now))

        def ask(task_id, title, request):
            conn.execute(
                """INSERT INTO human_action_requests(id,workspace_id,task_id,requested_by_principal_id,assignee_principal_id,
                     request_kind,title,request_json,urgency,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
                (f"hum-{uuid.uuid4().hex}", ws, task_id, actor.principal_id, owner, "decision", title,
                 _dumps(request), "batch", now, now))

        def due_of(row):
            local = row.get("official_due_local") or None
            if not local:
                return None, None, None
            precision = "date" if len(local) == 10 else "datetime"
            at = _local_to_utc(local + "T23:59:59" if precision == "date" else local, tz)
            return local, at, precision

        for row in rows:
            key = row["external_key"]
            local, due_at, precision = due_of(row)
            known = conn.execute(
                """SELECT k.task_id, k.last_row_json, t.state, t.official_due_at, t.official_due_local, t.title
                   FROM import_keys k JOIN tasks t ON t.workspace_id=k.workspace_id AND t.id=k.task_id
                   WHERE k.workspace_id=? AND k.source_name=? AND k.external_key=?""", (ws, source_name, key)).fetchone()
            if known is None:
                task_id = f"task-{uuid.uuid4().hex}"
                title = f"{key} {row.get('title', '')}".strip()
                conn.execute(
                    """INSERT INTO tasks(id,workspace_id,title,goal,state,assignee_id,official_due_at,official_due_local,
                         official_due_timezone,official_due_precision,created_at,updated_at)
                       VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (task_id, ws, title, row.get("goal", ""), "INBOX", owner, due_at, local,
                     tz if local else None, precision, now, now))
                event(task_id, "created", {"external_key": key, "official_due_local": local, "batch": batch_id},
                      f"imported from {source_name} {period}", key)
                conn.execute("INSERT INTO import_keys VALUES(?,?,?,?,?,?)",
                             (ws, source_name, key, task_id, batch_id, _dumps(row)))
                summary["new"].append(key)
                continue
            task_id, last = known["task_id"], json.loads(known["last_row_json"])
            changed = False
            if local != (last.get("official_due_local") or None) and due_at != known["official_due_at"]:
                ask(task_id, f"官方截止日变了：{known['title']}",
                    {"kind": "import_due_changed", "external_key": key, "current": known["official_due_local"],
                     "imported": local, "batch_id": batch_id, "source_ref": f"import://{source_name}/{period}/{key}"})
                event(task_id, "import_due_changed", {"current": known["official_due_local"], "imported": local},
                      "official due differs in the new import; waiting for the owner", key)
                summary["due_changed"].append({"external_key": key, "current": known["official_due_local"], "imported": local})
                changed = True
            details = {k: row.get(k, "") for k in ("title", "goal") if row.get(k, "") != last.get(k, "")}
            if details:
                event(task_id, "import_details_changed", details, "details changed in the new import", key)
                summary["details_changed"].append(key)
                changed = True
            if changed:
                conn.execute("UPDATE tasks SET version=version+1, updated_at=? WHERE workspace_id=? AND id=?",
                             (now, ws, task_id))
            else:
                summary["unchanged"] += 1
            conn.execute("UPDATE import_keys SET last_batch_id=?, last_row_json=? WHERE workspace_id=? AND source_name=? AND external_key=?",
                         (batch_id, _dumps(row), ws, source_name, key))
        present = set(keys)
        for gone in conn.execute(
                """SELECT k.external_key, k.task_id, t.title FROM import_keys k
                   JOIN tasks t ON t.workspace_id=k.workspace_id AND t.id=k.task_id
                   WHERE k.workspace_id=? AND k.source_name=? AND t.state NOT IN ('DONE','CANCELLED')""",
                (ws, source_name)).fetchall():
            if gone["external_key"] in present:
                continue
            ask(gone["task_id"], f"新表里没有了：{gone['title']}（已结？）",
                {"kind": "import_missing", "external_key": gone["external_key"], "batch_id": batch_id})
            summary["missing"].append(gone["external_key"])
        conn.execute("UPDATE import_batches SET summary_json=? WHERE id=?", (_dumps(summary), batch_id))
    return {"batch_id": batch_id, "duplicate": False, **summary}
