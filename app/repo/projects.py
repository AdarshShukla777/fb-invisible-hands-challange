"""Projects and chat messages."""
from __future__ import annotations

import sqlite3
from typing import Any

from ..errors import Conflict, NotFound
from ..pagination import PageParams, finish, keyset
from ..util import new_id, now_iso
from . import ACTIVE, in_clause, row, rows


def create_project(conn: sqlite3.Connection, name: str, base_url: str) -> dict[str, Any]:
    ts = now_iso()
    pid = new_id()
    conn.execute("INSERT INTO projects (id, name, base_url, created_at, updated_at) VALUES (?, ?, ?, ?, ?)",
                 (pid, name, base_url, ts, ts))
    return get_project(conn, pid)


def get_project(conn: sqlite3.Connection, project_id: str) -> dict[str, Any]:
    p = row(conn.execute("SELECT * FROM projects WHERE id = ?", (project_id,)).fetchone())
    if p is None:
        raise NotFound("Project not found.", code="project_not_found")
    return p


def list_projects(conn: sqlite3.Connection, page: PageParams) -> tuple[list[dict[str, Any]], str | None]:
    where, params, tail = keyset("created_at", page)
    rs = conn.execute(f"SELECT * FROM projects WHERE 1 = 1{where}{tail}", params).fetchall()
    return finish(rows(rs), page, "created_at", "projects")


def update_project(conn: sqlite3.Connection, project_id: str, name: str | None, base_url: str | None) -> dict[str, Any]:
    get_project(conn, project_id)
    if name is not None:
        conn.execute("UPDATE projects SET name = ?, updated_at = ? WHERE id = ?", (name, now_iso(), project_id))
    if base_url is not None:
        conn.execute("UPDATE projects SET base_url = ?, updated_at = ? WHERE id = ?", (base_url, now_iso(), project_id))
    return get_project(conn, project_id)


def has_active_runs(conn: sqlite3.Connection, project_id: str) -> bool:
    return conn.execute(
        f"SELECT 1 FROM test_runs WHERE project_id = ? AND status IN {in_clause(list(ACTIVE))} LIMIT 1",
        (project_id, *ACTIVE),
    ).fetchone() is not None


def delete_project(conn: sqlite3.Connection, project_id: str) -> None:
    get_project(conn, project_id)
    if has_active_runs(conn, project_id):
        raise Conflict("The project has queued or running tests. Cancel them first.", code="active_runs")
    conn.execute("DELETE FROM projects WHERE id = ?", (project_id,))


def reset_project(conn: sqlite3.Connection, project_id: str) -> None:
    """Explicit user action: removes all tests, runs, history and chat but keeps the project."""
    get_project(conn, project_id)
    if has_active_runs(conn, project_id):
        raise Conflict("The project has queued or running tests. Cancel them first.", code="active_runs")
    for table in ("locators", "runs", "test_cases", "page_observations", "page_versions", "pages", "messages"):
        conn.execute(f"DELETE FROM {table} WHERE project_id = ?", (project_id,))


def add_message(conn: sqlite3.Connection, project_id: str, role: str, text: str) -> dict[str, Any]:
    mid = new_id()
    conn.execute("INSERT INTO messages (id, project_id, role, text, created_at) VALUES (?, ?, ?, ?, ?)",
                 (mid, project_id, role, text, now_iso()))
    return row(conn.execute("SELECT * FROM messages WHERE id = ?", (mid,)).fetchone())


def list_messages(conn: sqlite3.Connection, project_id: str, page: PageParams) -> tuple[list[dict[str, Any]], str | None]:
    where, params, tail = keyset("created_at", page)
    rs = conn.execute(f"SELECT * FROM messages WHERE project_id = ?{where}{tail}", (project_id, *params)).fetchall()
    return finish(rows(rs), page, "created_at", f"messages:{project_id}")
