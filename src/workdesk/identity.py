"""Identity resolution and inbound message intake.

Only verified transport metadata (bot id + platform user id) maps to a
principal; a name typed in a message never does. Every inbound update is
stored once (deduped by platform update id) and becomes one wake event.
"""
from __future__ import annotations

import uuid
from pathlib import Path
from typing import Callable

from .contracts import ActorContext
from .dispatcher import _plus
from .errors import Forbidden, InvalidOperation
from .storage import connect, utc_now, write_transaction
from contextlib import closing


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
                   clock: Callable[[], str] = utc_now) -> dict:
    """Persist an inbound message before any model sees it.

    With `handled_inline` the Host answers the message right away, so its wake
    event is created already leased to that Run (no second dispatch).
    """
    now = clock()
    if not all(str(v).strip() for v in (platform, bot_id, update_id, chat_id, message_id, raw_source_ref)):
        raise InvalidOperation("inbound message metadata is incomplete")
    with write_transaction(database) as conn:
        dup = conn.execute(
            "SELECT id, status, sender_principal_id FROM inbox_messages WHERE platform=? AND bot_id=? AND update_id=?",
            (platform, bot_id, str(update_id))).fetchone()
        if dup is not None:
            wake = conn.execute("SELECT id,status FROM wake_events WHERE workspace_id=? AND origin=? AND origin_event_id=?",
                                (workspace_id, f"{platform}:{bot_id}", str(update_id))).fetchone()
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
        wake_id = f"wake-{uuid.uuid4().hex}"
        conn.execute(
            """INSERT INTO wake_events(id,workspace_id,origin,origin_event_id,kind,source_ref,status,attempts,
                 lease_until,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
            (wake_id, workspace_id, f"{platform}:{bot_id}", str(update_id), "user_message", raw_source_ref,
             "leased" if handled_inline else "pending", 1 if handled_inline else 0,
             _plus(now, lease_seconds) if handled_inline else None, now, now))
        return {"inbox_id": inbox_id, "accepted": True, "wake_event_id": wake_id,
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
            """SELECT i.id, i.sender_principal_id, i.revision, m.role FROM inbox_messages i
               LEFT JOIN memberships m ON m.workspace_id=i.workspace_id AND m.principal_id=i.sender_principal_id AND m.active=1
               WHERE i.workspace_id=? AND i.platform=? AND i.bot_id=? AND i.chat_id=? AND i.message_id=?""",
            (workspace_id, platform, bot_id, str(chat_id), str(message_id))).fetchone()
        if original is None:
            return {"accepted": False, "reason": "original message not recorded"}
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
                 lease_until,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
            (wake_id, workspace_id, f"{platform}:{bot_id}:edit", str(update_id), "user_message", raw_source_ref,
             "leased", 1, _plus(now, lease_seconds), now, now))
        return {"accepted": True, "inbox_id": original["id"], "revision": revision, "linked_tasks": tasks,
                "wake_event_id": wake_id,
                "actor": {"workspace_id": workspace_id, "principal_id": original["sender_principal_id"],
                          "role": original["role"], "source": platform}}


def finish_inbound(database: str | Path, *, workspace_id: str, inbox_id: str, ok: bool,
                   clock: Callable[[], str] = utc_now) -> None:
    with write_transaction(database) as conn:
        conn.execute("UPDATE inbox_messages SET status=?, updated_at=? WHERE workspace_id=? AND id=?",
                     ("done" if ok else "failed", clock(), workspace_id, inbox_id))
