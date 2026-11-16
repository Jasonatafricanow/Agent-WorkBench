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


def init_database(database: str | Path, schema: Path = SCHEMA) -> None:
    """Create a fresh database from the bootstrap schema (not a migration)."""
    with closing(connect(database)) as conn:
        if str(database) != ":memory:":
            conn.execute("PRAGMA journal_mode=WAL")
        conn.executescript(schema.read_text(encoding="utf-8"))


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
