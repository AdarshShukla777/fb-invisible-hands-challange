"""SQLite access: one short-lived connection per unit of work, explicit transactions,
and a minimal forward-only migration runner (numbered .sql files in migrations/)."""
from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

from .util import now_iso

MIGRATIONS_DIR = Path(__file__).parent / "migrations"


class Database:
    def __init__(self, path: Path):
        self.path = Path(path)

    def connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path, timeout=30, isolation_level=None, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute("PRAGMA busy_timeout = 30000")
        conn.execute("PRAGMA journal_mode = WAL")
        conn.execute("PRAGMA synchronous = NORMAL")
        return conn

    @contextmanager
    def read(self) -> Iterator[sqlite3.Connection]:
        conn = self.connect()
        try:
            yield conn
        finally:
            conn.close()

    @contextmanager
    def tx(self) -> Iterator[sqlite3.Connection]:
        """Write transaction. BEGIN IMMEDIATE takes the write lock up front, so
        check-then-insert sequences (idempotency, duplicate-run checks) are serialized."""
        conn = self.connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            try:
                yield conn
            except BaseException:
                conn.execute("ROLLBACK")
                raise
            conn.execute("COMMIT")
        finally:
            conn.close()


def _migration_files() -> list[Path]:
    return sorted(MIGRATIONS_DIR.glob("*.sql"))


def applied_migrations(db: Database) -> list[str]:
    with db.read() as conn:
        conn.execute(
            "CREATE TABLE IF NOT EXISTS schema_migrations (version TEXT PRIMARY KEY, applied_at TEXT NOT NULL)"
        )
        return [r[0] for r in conn.execute("SELECT version FROM schema_migrations ORDER BY version")]


def pending_migrations(db: Database) -> list[str]:
    done = set(applied_migrations(db))
    return [f.stem for f in _migration_files() if f.stem not in done]


def migrate(db: Database) -> list[str]:
    """Apply pending migrations in order; each file runs in its own transaction."""
    db.path.parent.mkdir(parents=True, exist_ok=True)
    applied: list[str] = []
    done = set(applied_migrations(db))
    for file in _migration_files():
        if file.stem in done:
            continue
        conn = db.connect()
        try:
            script = (
                "BEGIN IMMEDIATE;\n"
                + file.read_text(encoding="utf-8")
                + f"\nINSERT INTO schema_migrations (version, applied_at) VALUES ('{file.stem}', '{now_iso()}');\nCOMMIT;"
            )
            try:
                conn.executescript(script)
            except BaseException:
                if conn.in_transaction:
                    conn.execute("ROLLBACK")
                raise
        finally:
            conn.close()
        applied.append(file.stem)
    return applied
