"""SQLite connection discipline shared by WorkBench services.

One writer transaction at a time (`BEGIN IMMEDIATE`), foreign keys always on,
WAL for file databases. The database must live on a local filesystem, never on
SMB/NFS shared by several writers.
"""
from __future__ import annotations

import sqlite3
from contextlib import closing, contextmanager
from datetime import datetime, timezone
from pathlib import Path

SCHEMA = Path(__file__).resolve().parents[2] / "db" / "schema.sql"


def utc_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def connect(database: str | Path) -> sqlite3.Connection:
    conn = sqlite3.connect(str(database), timeout=10, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA busy_timeout=10000")
    return conn


MIGRATIONS = SCHEMA.parent / "migrations"


def _migration_files() -> list[tuple[int, Path]]:
    files = []
    for path in sorted(MIGRATIONS.glob("[0-9][0-9][0-9][0-9]_*.sql")):
        files.append((int(path.name[:4]), path))
    return files


def schema_version(database: str | Path) -> int:
    with closing(connect(database)) as conn:
        if not conn.execute("SELECT 1 FROM sqlite_master WHERE name='schema_migrations'").fetchone():
            return 1 if conn.execute("SELECT 1 FROM sqlite_master WHERE name='tasks'").fetchone() else 0
        return conn.execute("SELECT COALESCE(MAX(version),0) FROM schema_migrations").fetchone()[0]


def migrate(database: str | Path, *, backup_to: str | Path | None = None) -> list[int]:
    """Bring a database to the latest version; each migration is one transaction.

    Version 1 is the bootstrap `schema.sql`. A file database is backed up with
    SQLite's online backup API before any pending migration runs.
    """
    applied: list[int] = []
    current = schema_version(database)
    pending = [(v, p) for v, p in _migration_files() if v > max(current, 1)]
    if current >= 1 and pending and backup_to is not None:
        with closing(connect(database)) as src, closing(sqlite3.connect(str(backup_to))) as dst:
            src.backup(dst)
    with closing(connect(database)) as conn:
        if current == 0:
            if str(database) != ":memory:":
                conn.execute("PRAGMA journal_mode=WAL")
            conn.executescript(SCHEMA.read_text(encoding="utf-8"))
        conn.execute("""CREATE TABLE IF NOT EXISTS schema_migrations(
                          version INTEGER PRIMARY KEY, name TEXT NOT NULL, applied_at TEXT NOT NULL)""")
        conn.execute("INSERT OR IGNORE INTO schema_migrations VALUES(1,'schema.sql',?)", (utc_now(),))
        if current == 0:
            applied.append(1)
        for version, path in pending:
            conn.execute("BEGIN IMMEDIATE")
            try:
                for statement in _statements(path.read_text(encoding="utf-8")):
                    conn.execute(statement)
                conn.execute("INSERT INTO schema_migrations VALUES(?,?,?)", (version, path.name, utc_now()))
                conn.execute("COMMIT")
            except BaseException:
                conn.execute("ROLLBACK")
                raise
            applied.append(version)
        problems = conn.execute("PRAGMA foreign_key_check").fetchall()
        if problems:
            raise RuntimeError(f"foreign key check failed after migration: {problems[:5]}")
        if conn.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
            raise RuntimeError("integrity_check failed after migration")
    return applied


def _statements(script: str) -> list[str]:
    """Split a migration file into statements (no triggers with inner ';' here)."""
    lines = [line for line in script.splitlines() if not line.strip().startswith("--")]
    return [s.strip() for s in "\n".join(lines).split(";") if s.strip()]


def init_database(database: str | Path, schema: Path = SCHEMA) -> None:
    """Create a fresh database at the latest schema version."""
    migrate(database)


@contextmanager
def write_transaction(database: str | Path):
    conn = connect(database)
    try:
        conn.execute("BEGIN IMMEDIATE")
        yield conn
        conn.execute("COMMIT")
    except BaseException:
        if conn.in_transaction:
            conn.execute("ROLLBACK")
        raise
    finally:
        conn.close()


@contextmanager
def read_snapshot(database: str | Path):
    conn = connect(database)
    try:
        conn.execute("BEGIN")
        yield conn
        conn.execute("COMMIT")
    finally:
        if conn.in_transaction:
            conn.execute("ROLLBACK")
        conn.close()
