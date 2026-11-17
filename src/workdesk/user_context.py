"""SQL-backed host grant checker for MR-Mem USER scope bindings.

Only trusted admin/identity bootstrap code can create grants. The caller's
principal and workspace must be derived from authenticated transport context.
This class is read-only and never grants permissions implicitly.
"""
from __future__ import annotations

from contextlib import closing
from pathlib import Path
import sqlite3


class SqlMemoryGrantChecker:
    """Implements MR-Mem workbench_host.UserContextAuthorizer protocol."""

    def __init__(self, database: str | Path):
        self._database = str(database)

    def assert_allowed(
        self, *, principal_id: str, workspace_id: str, user_id: str, action: str
    ) -> None:
        if action not in {"memory.read", "memory.write"}:
            raise PermissionError("memory access denied")
        if not all(isinstance(x, str) and x.strip() for x in (
            principal_id, workspace_id, user_id
        )):
            raise PermissionError("memory access denied")
        with closing(sqlite3.connect(self._database, timeout=5)) as conn:
            row = conn.execute(
                """SELECT 1
                   FROM memory_context_grants g
                   JOIN memberships m
                     ON m.workspace_id=g.workspace_id
                    AND m.principal_id=g.actor_principal_id
                   JOIN principals p ON p.id=g.actor_principal_id
                   WHERE g.workspace_id=? AND g.actor_principal_id=?
                     AND g.memory_user_id=? AND g.capability=?
                     AND g.revoked_at IS NULL
                     AND m.active=1 AND p.active=1
                   LIMIT 1""",
                (workspace_id, principal_id, user_id, action),
            ).fetchone()
        if row is None:
            raise PermissionError("memory access denied")
