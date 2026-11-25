"""Identity resolution and inbound message intake.

Only verified transport metadata (bot id + platform user id) maps to a
principal; a name typed in a message never does. Every inbound update is
stored once (deduped by platform update id) and becomes one wake event.
"""
from __future__ import annotations

import json
import uuid
from contextlib import closing
from pathlib import Path
from typing import Callable

from .contracts import ActorContext
from .dispatcher import _plus
from .errors import Forbidden, InvalidOperation
from .scheduler import SYSTEM_PRINCIPAL
from .storage import connect, utc_now, write_transaction

ATTACHMENT_KINDS = frozenset({"document", "photo", "audio", "video", "voice", "other"})


def _normalize_attachments(attachments) -> list[dict]:
    if attachments is None:
        return []
    if not isinstance(attachments, list) or len(attachments) > 20:
        raise InvalidOperation("attachments must be a list of at most 20 items", field="attachments")
    out = []
    for item in attachments:
        if not isinstance(item, dict):
            raise InvalidOperation("each attachment must be an object", field="attachments")
        kind = str(item.get("kind") or "other")
        if kind not in ATTACHMENT_KINDS:
            raise InvalidOperation("unsupported attachment kind", field="attachments", allowed=sorted(ATTACHMENT_KINDS))
        file_ref = str(item.get("file_ref") or "").strip()
        if not file_ref or len(file_ref) > 500:
            raise InvalidOperation("attachment file_ref is required", field="attachments")
        size = item.get("file_size")
        if size is not None and (type(size) is not int or size < 0):
            raise InvalidOperation("attachment file_size must be a non-negative integer", field="attachments")
        out.append({
            "kind": kind,
            "file_name": str(item.get("file_name") or "").strip()[:240],
            "mime_type": str(item.get("mime_type") or "").strip()[:120],
            "file_size": size,
            "file_ref": file_ref,
            "caption": str(item.get("caption") or "").strip()[:500],
        })
    return out


class SqlIdentityProvider:
    def __init__(self, database: str | Path):
        self._db = str(database)

    def resolve_authenticated(self, *, provider: str, connection_id: str, external_user_id: str,
                              workspace_id: str) -> ActorContext:
        with closing(connect(self._db)) as conn:
            row = conn.execute(
                """SELECT m.principal_id, m.role FROM external_identities x
                   JOIN memberships m ON m.principal_id=x.principal_id AND m.workspace_id=?
                   JOIN principals p ON p.id=x.principal_id
                   WHERE x.provider=? AND x.bot_id=? AND x.external_user_id=? AND m.active=1 AND p.active=1""",
                (workspace_id, provider, connection_id, str(external_user_id))).fetchone()
        if row is None:
            raise Forbidden("unknown or inactive identity")
        return ActorContext(workspace_id, row["principal_id"], row["role"], provider)


def bind_identity(database: str | Path, *, provider: str, bot_id: str, external_user_id: str,
                  principal_id: str) -> None:
    """Trusted bootstrap only (CLI); never reachable from an Agent tool."""
    with write_transaction(database) as conn:
        conn.execute("INSERT OR REPLACE INTO external_identities VALUES(?,?,?,?)",
                     (provider, bot_id, str(external_user_id), principal_id))


def record_inbound(database: str | Path, *, workspace_id: str, platform: str, bot_id: str, update_id: str,
                   chat_id: str, message_id: str, external_user_id: str, raw_source_ref: str,
                   handled_inline: bool = True, lease_seconds: int = 600,
                   attachments: list[dict] | None = None, clock: Callable[[], str] = utc_now) -> dict:
    """Persist an inbound message before any model sees it.

    With `handled_inline` the Host answers the message right away, so its wake
    event is created already leased to that Run (no second dispatch).
    """
    now = clock()
    if not all(str(v).strip() for v in (platform, bot_id, update_id, chat_id, message_id, raw_source_ref)):
        raise InvalidOperation("inbound message metadata is incomplete")
    items = _normalize_attachments(attachments)
    with write_transaction(database) as conn:
        dup = conn.execute(
            "SELECT id, status, sender_principal_id FROM inbox_messages WHERE platform=? AND bot_id=? AND update_id=?",
            (platform, bot_id, str(update_id))).fetchone()
        if dup is not None:
            wake = conn.execute(
                "SELECT id,status,lease_until FROM wake_events WHERE workspace_id=? AND origin=? AND origin_event_id=?",
                (workspace_id, f"{platform}:{bot_id}", str(update_id))).fetchone()
            # A message whose Run died (failed, unknown, or a lease that ran out) is resumed on the same
            # wake event when the Host re-submits it; a live or finished one stays a plain duplicate.
            dead = wake is not None and (wake["status"] in ("failed", "unknown") or (
                wake["status"] == "leased" and (wake["lease_until"] or "") <= now))
            role = dup["sender_principal_id"] and conn.execute(
                "SELECT role FROM memberships WHERE workspace_id=? AND principal_id=? AND active=1",
                (workspace_id, dup["sender_principal_id"])).fetchone()
            if handled_inline and dup["status"] in ("processing", "failed") and dead and role:
                conn.execute("""UPDATE wake_events SET status='leased', attempts=attempts+1, lease_until=?,
                                  host_run_ref='inline', updated_at=? WHERE id=?""",
                             (_plus(now, lease_seconds), now, wake["id"]))
                conn.execute("UPDATE inbox_messages SET status='processing', updated_at=? WHERE id=?", (now, dup["id"]))
                return {"inbox_id": dup["id"], "accepted": True, "resumed": True, "wake_event_id": wake["id"],
                        "actor": {"workspace_id": workspace_id, "principal_id": dup["sender_principal_id"],
                                  "role": role["role"], "source": platform}}
            return {"inbox_id": dup["id"], "duplicate": True, "status": dup["status"],
                    "wake_event_id": wake["id"] if wake else None}
        sender = conn.execute(
            """SELECT m.principal_id, m.role FROM external_identities x
               JOIN memberships m ON m.principal_id=x.principal_id AND m.workspace_id=?
               JOIN principals p ON p.id=x.principal_id
               WHERE x.provider=? AND x.bot_id=? AND x.external_user_id=? AND m.active=1 AND p.active=1""",
            (workspace_id, platform, bot_id, str(external_user_id))).fetchone()
        inbox_id = f"in-{uuid.uuid4().hex}"
        conn.execute(
            """INSERT INTO inbox_messages(id,workspace_id,platform,bot_id,update_id,chat_id,message_id,
                 sender_principal_id,raw_source_ref,status,received_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""",
            (inbox_id, workspace_id, platform, bot_id, str(update_id), str(chat_id), str(message_id),
             sender["principal_id"] if sender else None, raw_source_ref,
             "processing" if sender else "rejected", now, now))
        if sender is None:
            return {"inbox_id": inbox_id, "accepted": False}
        for att in items:
            conn.execute(
                """INSERT INTO inbox_attachments(id,workspace_id,inbox_id,kind,file_name,mime_type,file_size,
                     file_ref,caption,created_at) VALUES(?,?,?,?,?,?,?,?,?,?)""",
                (f"att-{uuid.uuid4().hex}", workspace_id, inbox_id, att["kind"], att["file_name"],
                 att["mime_type"], att["file_size"], att["file_ref"], att["caption"], now))
        wake_id = f"wake-{uuid.uuid4().hex}"
        conn.execute(
            """INSERT INTO wake_events(id,workspace_id,origin,origin_event_id,kind,source_ref,status,attempts,
                 lease_until,host_run_ref,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""",
            (wake_id, workspace_id, f"{platform}:{bot_id}", str(update_id), "user_message", raw_source_ref,
             "leased" if handled_inline else "pending", 1 if handled_inline else 0,
             _plus(now, lease_seconds) if handled_inline else None, "inline" if handled_inline else None, now, now))
        return {"inbox_id": inbox_id, "accepted": True, "wake_event_id": wake_id, "attachments": len(items),
                "actor": {"workspace_id": workspace_id, "principal_id": sender["principal_id"],
                          "role": sender["role"], "source": platform}}


def record_edit(database: str | Path, *, workspace_id: str, platform: str, bot_id: str, update_id: str,
                chat_id: str, message_id: str, external_user_id: str, raw_source_ref: str,
                lease_seconds: int = 600, clock: Callable[[], str] = utc_now) -> dict:
    """An edited message becomes a new revision of the original inbox row; tasks that cite
    the message get a `source_edited` event, and the edit re-enters the loop as a wake event."""
    now = clock()
    with write_transaction(database) as conn:
        original = conn.execute(
            """SELECT i.id, i.sender_principal_id, i.revision, i.retracted_at, m.role FROM inbox_messages i
               LEFT JOIN memberships m ON m.workspace_id=i.workspace_id AND m.principal_id=i.sender_principal_id AND m.active=1
               WHERE i.workspace_id=? AND i.platform=? AND i.bot_id=? AND i.chat_id=? AND i.message_id=?""",
            (workspace_id, platform, bot_id, str(chat_id), str(message_id))).fetchone()
        if original is None:
            return {"accepted": False, "reason": "original message not recorded"}
        if original["retracted_at"]:
            return {"accepted": False, "reason": "original message was retracted"}
        sender = conn.execute(
            "SELECT principal_id FROM external_identities WHERE provider=? AND bot_id=? AND external_user_id=?",
            (platform, bot_id, str(external_user_id))).fetchone()
        if sender is None or sender["principal_id"] != original["sender_principal_id"] or original["role"] is None:
            return {"accepted": False, "reason": "editor is not the original sender"}
        if conn.execute("SELECT 1 FROM inbox_revisions WHERE inbox_id=? AND update_id=?",
                        (original["id"], str(update_id))).fetchone():
            return {"accepted": True, "duplicate": True, "inbox_id": original["id"]}
        revision = original["revision"] + 1
        conn.execute("UPDATE inbox_messages SET revision=?, edited_at=?, raw_source_ref=?, updated_at=? WHERE id=?",
                     (revision, now, raw_source_ref, now, original["id"]))
        conn.execute("INSERT INTO inbox_revisions VALUES(?,?,?,?,?)",
                     (original["id"], revision, str(update_id), raw_source_ref, now))
        tasks = [r[0] for r in conn.execute(
            "SELECT DISTINCT task_id FROM message_links WHERE workspace_id=? AND inbox_id=?",
            (workspace_id, original["id"]))]
        for task_id in tasks:
            conn.execute(
                """INSERT INTO task_events(id,workspace_id,task_id,actor_id,kind,after_json,reason,evidence_type,
                     source_ref,occurred_at,recorded_at) VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
                (f"evt-{uuid.uuid4().hex}", workspace_id, task_id, original["sender_principal_id"], "source_edited",
                 f'{{"inbox_id":"{original["id"]}","revision":{revision}}}', "source message was edited",
                 "explicit_user", raw_source_ref, now, now))
            conn.execute("UPDATE tasks SET version=version+1, updated_at=? WHERE workspace_id=? AND id=?",
                         (now, workspace_id, task_id))
        wake_id = f"wake-{uuid.uuid4().hex}"
        conn.execute(
            """INSERT INTO wake_events(id,workspace_id,origin,origin_event_id,kind,source_ref,status,attempts,
                 lease_until,host_run_ref,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""",
            (wake_id, workspace_id, f"{platform}:{bot_id}:edit", str(update_id), "user_message", raw_source_ref,
             "leased", 1, _plus(now, lease_seconds), "inline", now, now))
        return {"accepted": True, "inbox_id": original["id"], "revision": revision, "linked_tasks": tasks,
                "wake_event_id": wake_id,
                "actor": {"workspace_id": workspace_id, "principal_id": original["sender_principal_id"],
                          "role": original["role"], "source": platform}}


def record_retract(database: str | Path, *, workspace_id: str, platform: str, bot_id: str, update_id: str,
                   chat_id: str, message_id: str, raw_source_ref: str, external_user_id: str | None = None,
                   clock: Callable[[], str] = utc_now) -> dict:
    """Mark a deleted/retracted inbound message as no longer active evidence without deleting task events.

    Every task linked to the message receives an append-only `source_retracted` audit event and one wake
    event when affected tasks exist.
    """
    now = clock()
    if not all(str(v).strip() for v in (platform, bot_id, update_id, chat_id, message_id, raw_source_ref)):
        raise InvalidOperation("retract metadata is incomplete")
    with write_transaction(database) as conn:
        conn.execute("INSERT OR IGNORE INTO principals(id,display_name,kind,created_at) VALUES(?,?,?,?)",
                     (SYSTEM_PRINCIPAL, "WorkBench system", "service", now))
        original = conn.execute(
            """SELECT id, sender_principal_id, raw_source_ref, retracted_at FROM inbox_messages
               WHERE workspace_id=? AND platform=? AND bot_id=? AND chat_id=? AND message_id=?""",
            (workspace_id, platform, bot_id, str(chat_id), str(message_id))).fetchone()
        if original is None:
            return {"accepted": False, "reason": "original message not recorded"}
        if external_user_id:
            sender = conn.execute(
                """SELECT m.principal_id, m.role FROM external_identities x
                   JOIN memberships m ON m.workspace_id=? AND m.principal_id=x.principal_id AND m.active=1
                   WHERE x.provider=? AND x.bot_id=? AND x.external_user_id=?""",
                (workspace_id, platform, bot_id, str(external_user_id))).fetchone()
            if sender is None or (sender["principal_id"] != original["sender_principal_id"] and sender["role"] != "owner"):
                return {"accepted": False, "reason": "actor cannot retract this message"}
        tasks = [r[0] for r in conn.execute(
            "SELECT DISTINCT task_id FROM message_links WHERE workspace_id=? AND inbox_id=?",
            (workspace_id, original["id"]))]
        if original["retracted_at"] is not None:
            return {"accepted": True, "duplicate": True, "inbox_id": original["id"],
                    "linked_tasks": tasks, "retracted_at": original["retracted_at"]}
        conn.execute(
            "UPDATE inbox_messages SET retracted_at=?, retracted_source_ref=?, updated_at=? WHERE id=?",
            (now, raw_source_ref, now, original["id"]))
        actor_id = original["sender_principal_id"] or SYSTEM_PRINCIPAL
        payload = json.dumps({"inbox_id": original["id"], "retracted_source_ref": original["raw_source_ref"]},
                             ensure_ascii=False, sort_keys=True)
        for task_id in tasks:
            conn.execute(
                """INSERT INTO task_events(id,workspace_id,task_id,actor_id,kind,after_json,reason,evidence_type,
                     source_ref,occurred_at,recorded_at) VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
                (f"evt-{uuid.uuid4().hex}", workspace_id, task_id, actor_id, "source_retracted", payload,
                 "source message was retracted or deleted", "system", raw_source_ref, now, now))
            conn.execute("UPDATE tasks SET version=version+1, updated_at=? WHERE workspace_id=? AND id=?",
                         (now, workspace_id, task_id))
        wake_id = None
        if tasks:
            wake_id = f"wake-{uuid.uuid4().hex}"
            conn.execute(
                """INSERT INTO wake_events(id,workspace_id,task_id,origin,origin_event_id,kind,source_ref,status,
                     created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?)""",
                (wake_id, workspace_id, tasks[0], f"{platform}:{bot_id}:retract", str(update_id),
                 "external_change", raw_source_ref, "pending", now, now))
        return {"accepted": True, "inbox_id": original["id"], "linked_tasks": tasks,
                "retracted_at": now, "wake_event_id": wake_id}


def finish_inbound(database: str | Path, *, workspace_id: str, inbox_id: str, ok: bool,
                   clock: Callable[[], str] = utc_now) -> None:
    with write_transaction(database) as conn:
        conn.execute("UPDATE inbox_messages SET status=?, updated_at=? WHERE workspace_id=? AND id=?",
                     ("done" if ok else "failed", clock(), workspace_id, inbox_id))
