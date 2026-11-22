"""Approvals for one exact Host tool call (Pi `tool_call` hook flow).

The Host blocks a call that needs approval and records it here with a
fingerprint of tool name + input. A decision re-enters the loop as an
`approval_decided` wake event. When the model re-issues the call in the same
session, the Host hook consumes the approval only if the fingerprint matches;
an approval is single-use, expires, and is never a general tool grant.
"""
from __future__ import annotations

import hashlib
import json
import uuid
from pathlib import Path
from typing import Any, Callable

from .contracts import ActorContext
from .errors import EvidenceRequired, Forbidden, InvalidOperation, NotFound
from .storage import utc_now, write_transaction


def fingerprint(tool_name: str, tool_input: Any) -> str:
    body = json.dumps({"tool": tool_name, "input": tool_input}, ensure_ascii=False,
                      sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(body.encode("utf-8")).hexdigest()


class ApprovalService:
    def __init__(self, database: str | Path, *, clock: Callable[[], str] = utc_now):
        self._db, self._clock = str(database), clock

    def request(self, *, host_actor: ActorContext, wake_event_id: str, tool_name: str, tool_input: Any,
                summary: str, host_deferred_ref: str, expires_at: str, task_id: str | None = None,
                approver_id: str | None = None) -> dict:
        now = self._clock()
        if not tool_name or not summary or not host_deferred_ref or not expires_at.endswith("Z"):
            raise InvalidOperation("tool_name, summary, host_deferred_ref and UTC expires_at are required")
        with write_transaction(self._db) as conn:
            ws = conn.execute("SELECT owner_id FROM workspaces WHERE id=?", (host_actor.workspace_id,)).fetchone()
            if ws is None:
                raise NotFound("workspace not found")
            approval_id = f"apr-{uuid.uuid4().hex}"
            conn.execute(
                """INSERT INTO approval_requests(id,workspace_id,wake_event_id,task_id,requested_by,approver_id,
                     request_summary,host_deferred_ref,created_at,tool_name,action_fingerprint,expires_at)
                   VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""",
                (approval_id, host_actor.workspace_id, wake_event_id, task_id, host_actor.principal_id,
                 approver_id or ws["owner_id"], summary, host_deferred_ref, now, tool_name,
                 fingerprint(tool_name, tool_input), expires_at))
            # The approver learns about it in the same transaction, whoever triggered the call.
            conn.execute(
                """INSERT INTO delivery_outbox(id,workspace_id,task_id,recipient_principal_id,channel,payload_json,
                     dedupe_key,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?)""",
                (f"dlv-out-{uuid.uuid4().hex}", host_actor.workspace_id, task_id, approver_id or ws["owner_id"],
                 "telegram", json.dumps({"text": f"待审批 {approval_id}：{summary}\n"
                                                 f"回复 /approve {approval_id} 或 /deny {approval_id}",
                                         "approval_id": approval_id}, ensure_ascii=False),
                 f"approval:{approval_id}", now, now))
            return {"approval_id": approval_id, "approver_id": approver_id or ws["owner_id"]}

    def pending(self, *, workspace_id: str, approver_id: str) -> list[dict]:
        with write_transaction(self._db) as conn:
            return [dict(r) for r in conn.execute(
                """SELECT id,task_id,tool_name,request_summary,created_at,expires_at FROM approval_requests
                   WHERE workspace_id=? AND approver_id=? AND status='pending' ORDER BY created_at""",
                (workspace_id, approver_id))]

    def decide(self, *, approver: ActorContext, approval_id: str, approve: bool, source_ref: str) -> dict:
        now = self._clock()
        if type(approve) is not bool:  # "false", 0 or a missing field must never read as a decision
            raise InvalidOperation("approve must be true or false", field="approve")
        if not source_ref:
            raise EvidenceRequired("a decision needs the approver's own message as source_ref")
        with write_transaction(self._db) as conn:
            if approver.source != "cli" and not conn.execute(
                    """SELECT 1 FROM inbox_messages i WHERE i.workspace_id=? AND i.sender_principal_id=?
                       AND (i.raw_source_ref=? OR EXISTS (SELECT 1 FROM inbox_revisions r WHERE r.inbox_id=i.id
                            AND r.raw_source_ref=?))""",
                    (approver.workspace_id, approver.principal_id, source_ref, source_ref)).fetchone():
                raise EvidenceRequired("source_ref must be a message the approver actually sent")
            row = conn.execute("SELECT * FROM approval_requests WHERE workspace_id=? AND id=?",
                               (approver.workspace_id, approval_id)).fetchone()
            if row is None or row["approver_id"] != approver.principal_id:
                raise NotFound("approval request not found")
            member = conn.execute(
                "SELECT 1 FROM memberships WHERE workspace_id=? AND principal_id=? AND active=1",
                (approver.workspace_id, approver.principal_id)).fetchone()
            if member is None:
                raise Forbidden("approver is no longer an active member")
            if row["status"] != "pending":
                raise InvalidOperation(f"approval is already {row['status']}")
            status = "approved" if approve else "denied"
            if row["expires_at"] and row["expires_at"] <= now:
                status = "expired"
            conn.execute("UPDATE approval_requests SET status=?, decision_source_ref=?, decided_at=? WHERE id=?",
                         (status, source_ref, now, approval_id))
            wake_id = f"wake-{uuid.uuid4().hex}"
            conn.execute(
                """INSERT INTO wake_events(id,workspace_id,task_id,origin,origin_event_id,kind,source_ref,
                     host_session_ref,deferred_ref,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
                (wake_id, approver.workspace_id, row["task_id"], "workbench.approval", approval_id,
                 "approval_decided", source_ref, None, row["host_deferred_ref"], now, now))
            return {"approval_id": approval_id, "status": status, "wake_event_id": wake_id}

    def consume(self, *, workspace_id: str, tool_name: str, tool_input: Any, session_ref: str,
                task_id: str | None = None) -> dict | None:
        """Host hook: let exactly one matching, unexpired approved call through, and only in the
        session (and for the task) that asked for it."""
        now = self._clock()
        fp = fingerprint(tool_name, tool_input)
        if not session_ref:
            return None
        with write_transaction(self._db) as conn:
            row = conn.execute(
                """SELECT id FROM approval_requests WHERE workspace_id=? AND status='approved'
                   AND consumed_at IS NULL AND tool_name=? AND action_fingerprint=?
                   AND substr(host_deferred_ref,1,length(?)+1)=?||'#' AND task_id IS ?
                   AND (expires_at IS NULL OR expires_at>?) ORDER BY decided_at LIMIT 1""",
                (workspace_id, tool_name, fp, session_ref, session_ref, task_id, now)).fetchone()
            if row is None:
                return None
            conn.execute("UPDATE approval_requests SET consumed_at=? WHERE id=?", (now, row["id"]))
            return {"approval_id": row["id"]}

    def record_external(self, *, approver: ActorContext, recorded_by: str, system: str, action: str,
                        subject_ref: str, payload_digest: str, source_ref: str, decided_at: str,
                        issued_at: str | None = None) -> dict:
        """Audit an approval the owner gave inside another system's flow (Facetide review passphrase).
        It must cite a message the approver really sent; the same message is recorded once."""
        now = self._clock()
        if approver.role != "owner":
            raise Forbidden("only the owner's approvals are recorded here")
        for name, value in (("system", system), ("action", action), ("subject_ref", subject_ref),
                            ("payload_digest", payload_digest), ("source_ref", source_ref)):
            if not isinstance(value, str) or not value or len(value) > 300:
                raise InvalidOperation(f"{name} is required", field=name)
        for name, value in (("decided_at", decided_at), ("issued_at", issued_at)):
            if value is not None and not (isinstance(value, str) and value.endswith("Z")):
                raise InvalidOperation(f"{name} must be UTC ISO ending in Z", field=name)
        if not decided_at:
            raise InvalidOperation("decided_at is required", field="decided_at")
        with write_transaction(self._db) as conn:
            if not conn.execute(
                    """SELECT 1 FROM inbox_messages WHERE workspace_id=? AND sender_principal_id=? AND raw_source_ref=?""",
                    (approver.workspace_id, approver.principal_id, source_ref)).fetchone():
                raise EvidenceRequired("source_ref must be a message the approver actually sent")
            row = conn.execute("SELECT id FROM external_approvals WHERE workspace_id=? AND system=? AND source_ref=?",
                               (approver.workspace_id, system, source_ref)).fetchone()
            if row is not None:
                return {"external_approval_id": row["id"], "duplicate": True}
            approval_id = f"xap-{uuid.uuid4().hex}"
            conn.execute(
                """INSERT INTO external_approvals(id,workspace_id,system,action,subject_ref,payload_digest,approver_id,
                     recorded_by,source_ref,issued_at,decided_at,recorded_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""",
                (approval_id, approver.workspace_id, system, action, subject_ref, payload_digest,
                 approver.principal_id, recorded_by, source_ref, issued_at, decided_at, now))
            return {"external_approval_id": approval_id, "duplicate": False}

    def revoke(self, *, owner: ActorContext, approval_id: str) -> None:
        with write_transaction(self._db) as conn:
            role = conn.execute("SELECT role FROM memberships WHERE workspace_id=? AND principal_id=? AND active=1",
                                (owner.workspace_id, owner.principal_id)).fetchone()
            if role is None or role["role"] != "owner":
                raise Forbidden("only the owner may revoke approvals")
            conn.execute("""UPDATE approval_requests SET status='cancelled' WHERE workspace_id=? AND id=?
                            AND status IN ('pending','approved') AND consumed_at IS NULL""",
                         (owner.workspace_id, approval_id))
