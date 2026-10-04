"""References to evidence files (screenshots, traces, videos) stored under the artifacts dir."""
from __future__ import annotations

import sqlite3
from typing import Any

from ..errors import NotFound
from ..util import new_id, now_iso
from . import row, rows


def add_artifact(conn: sqlite3.Connection, project_id: str, test_run_id: str, *, kind: str, rel_path: str,
                 content_type: str, size_bytes: int | None, step_index: int | None = None, label: str | None = None) -> str:
    aid = new_id()
    conn.execute(
        "INSERT INTO artifacts (id, project_id, test_run_id, step_index, kind, label, rel_path, content_type, size_bytes, "
        "created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (aid, project_id, test_run_id, step_index, kind, label, rel_path, content_type, size_bytes, now_iso()),
    )
    return aid


def get_artifact(conn: sqlite3.Connection, project_id: str, artifact_id: str) -> dict[str, Any]:
    a = row(conn.execute("SELECT * FROM artifacts WHERE project_id = ? AND id = ?", (project_id, artifact_id)).fetchone())
    if a is None:
        raise NotFound("Artifact not found in this project.", code="artifact_not_found")
    return a


def for_test_run(conn: sqlite3.Connection, project_id: str, test_run_id: str) -> list[dict[str, Any]]:
    return rows(conn.execute("SELECT * FROM artifacts WHERE project_id = ? AND test_run_id = ? ORDER BY created_at, id",
                             (project_id, test_run_id)).fetchall())
