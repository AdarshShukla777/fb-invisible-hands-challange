"""Test cases and their immutable revisions."""
from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from typing import Any

from ..errors import NotFound
from ..pagination import PageParams, finish, keyset
from ..planning.service import PlanOutcome
from ..util import dumps, new_id, now_iso
from . import ACTIVE, in_clause, like_pattern, row, rows

_SELECT = """
SELECT tc.*, r.revision_number, r.instruction, r.plan_status, r.issues_json, r.planner,
       r.plan_json, r.mode, r.created_at AS revision_created_at
FROM test_cases tc JOIN test_case_revisions r ON r.id = tc.current_revision_id
"""


def _title(instruction: str) -> str:
    text = " ".join(instruction.split())
    return text if len(text) <= 120 else text[:117] + "..."


def _insert_revision(conn: sqlite3.Connection, project_id: str, test_case_id: str, number: int,
                     instruction: str, outcome: PlanOutcome, mode: str) -> str:
    rid = new_id()
    conn.execute(
        """INSERT INTO test_case_revisions (id, project_id, test_case_id, revision_number, instruction, plan_json,
               plan_status, issues_json, planner, mode, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (rid, project_id, test_case_id, number, instruction, dumps(outcome.plan) if outcome.plan else None,
         outcome.status, dumps(outcome.issues), outcome.planner, mode, now_iso()),
    )
    return rid


def create_test_case(conn: sqlite3.Connection, project_id: str, instruction: str, outcome: PlanOutcome,
                     mode: str = "plan") -> dict[str, Any]:
    ts = now_iso()
    tid = new_id()
    conn.execute(
        "INSERT INTO test_cases (id, project_id, title, created_at, updated_at) VALUES (?, ?, ?, ?, ?)",
        (tid, project_id, _title(instruction), ts, ts),
    )
    rid = _insert_revision(conn, project_id, tid, 1, instruction, outcome, mode)
    conn.execute("UPDATE test_cases SET current_revision_id = ? WHERE id = ?", (rid, tid))
    return get_test_case(conn, project_id, tid)


def add_revision(conn: sqlite3.Connection, project_id: str, test_case_id: str, instruction: str,
                 outcome: PlanOutcome, mode: str = "plan") -> dict[str, Any]:
    """Editing never mutates history: it appends a revision and moves the pointer."""
    tc = get_test_case(conn, project_id, test_case_id)
    number = conn.execute(
        "SELECT MAX(revision_number) FROM test_case_revisions WHERE project_id = ? AND test_case_id = ?",
        (project_id, test_case_id),
    ).fetchone()[0] + 1
    rid = _insert_revision(conn, project_id, test_case_id, number, instruction, outcome, mode)
    conn.execute(
        "UPDATE test_cases SET current_revision_id = ?, title = ?, archived_at = NULL, updated_at = ? "
        "WHERE project_id = ? AND id = ?",
        (rid, _title(instruction), now_iso(), project_id, tc["id"]),
    )
    refresh_test_case_status(conn, project_id, test_case_id)
    return get_test_case(conn, project_id, test_case_id)


def get_test_case(conn: sqlite3.Connection, project_id: str, test_case_id: str) -> dict[str, Any]:
    tc = row(conn.execute(f"{_SELECT} WHERE tc.project_id = ? AND tc.id = ?", (project_id, test_case_id)).fetchone())
    if tc is None:
        raise NotFound("Test case not found in this project.", code="test_case_not_found")
    return tc


def get_test_cases(conn: sqlite3.Connection, project_id: str, ids: list[str]) -> dict[str, dict[str, Any]]:
    if not ids:
        return {}
    rs = conn.execute(f"{_SELECT} WHERE tc.project_id = ? AND tc.id IN {in_clause(ids)}", (project_id, *ids)).fetchall()
    return {r["id"]: dict(r) for r in rs}


@dataclass
class TestFilters:
    statuses: list[str] | None = None       # last run status of the current revision (incl. not_run)
    plan_statuses: list[str] | None = None  # ready | needs_clarification | invalid
    q: str | None = None
    include_archived: bool = False
    sort: str = "created_at"                # created_at | updated_at


def list_test_cases(conn: sqlite3.Connection, project_id: str, f: TestFilters, page: PageParams
                    ) -> tuple[list[dict[str, Any]], str | None]:
    clauses, params = ["tc.project_id = ?"], [project_id]  # project scope first, always
    if not f.include_archived:
        clauses.append("tc.archived_at IS NULL")
    if f.statuses:
        clauses.append(f"tc.last_run_status IN {in_clause(f.statuses)}")
        params += f.statuses
    if f.plan_statuses:
        clauses.append(f"r.plan_status IN {in_clause(f.plan_statuses)}")
        params += f.plan_statuses
    if f.q:
        clauses.append("(tc.title LIKE ? ESCAPE '\\' OR r.instruction LIKE ? ESCAPE '\\')")
        pattern = like_pattern(f.q)
        params += [pattern, pattern]
    where, kparams, tail = keyset(f"tc.{f.sort}", page, "tc.id")
    rs = conn.execute(f"{_SELECT} WHERE {' AND '.join(clauses)}{where}{tail}", (*params, *kparams)).fetchall()
    scope = f"tests:{project_id}:{f.sort}"
    return finish(rows(rs), page, f.sort, scope)


def archive_test_case(conn: sqlite3.Connection, project_id: str, test_case_id: str) -> dict[str, Any]:
    get_test_case(conn, project_id, test_case_id)
    conn.execute("UPDATE test_cases SET archived_at = COALESCE(archived_at, ?), updated_at = ? "
                 "WHERE project_id = ? AND id = ?", (now_iso(), now_iso(), project_id, test_case_id))
    return get_test_case(conn, project_id, test_case_id)


def list_revisions(conn: sqlite3.Connection, project_id: str, test_case_id: str, page: PageParams
                   ) -> tuple[list[dict[str, Any]], str | None]:
    get_test_case(conn, project_id, test_case_id)
    where, params, tail = keyset("created_at", page)
    rs = conn.execute(
        f"SELECT * FROM test_case_revisions WHERE project_id = ? AND test_case_id = ?{where}{tail}",
        (project_id, test_case_id, *params),
    ).fetchall()
    return finish(rows(rs), page, "created_at", f"revisions:{project_id}:{test_case_id}")


def get_revision(conn: sqlite3.Connection, project_id: str, revision_id: str) -> dict[str, Any]:
    r = row(conn.execute("SELECT * FROM test_case_revisions WHERE project_id = ? AND id = ?",
                         (project_id, revision_id)).fetchone())
    if r is None:
        raise NotFound("Revision not found in this project.", code="revision_not_found")
    return r


def refresh_test_case_status(conn: sqlite3.Connection, project_id: str, test_case_id: str) -> None:
    """Derived status: an active execution (any revision) wins; otherwise the latest run of
    the current revision; otherwise not_run. Recomputed from source rows, never incremented."""
    tc = conn.execute("SELECT current_revision_id FROM test_cases WHERE project_id = ? AND id = ?",
                      (project_id, test_case_id)).fetchone()
    if tc is None:
        return
    active = conn.execute(
        f"SELECT status, queued_at FROM test_runs WHERE project_id = ? AND test_case_id = ? AND status IN {in_clause(list(ACTIVE))} "
        "ORDER BY queued_at DESC LIMIT 1", (project_id, test_case_id, *ACTIVE),
    ).fetchone()
    latest = active or conn.execute(
        "SELECT status, queued_at FROM test_runs WHERE project_id = ? AND test_case_id = ? AND revision_id = ? "
        "ORDER BY queued_at DESC, id DESC LIMIT 1", (project_id, test_case_id, tc["current_revision_id"]),
    ).fetchone()
    status, at = (latest["status"], latest["queued_at"]) if latest else ("not_run", None)
    conn.execute("UPDATE test_cases SET last_run_status = ?, last_run_at = ? WHERE project_id = ? AND id = ?",
                 (status, at, project_id, test_case_id))
