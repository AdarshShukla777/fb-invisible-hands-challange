"""Healing attempts (immutable evidence) and replacement locators (reviewable lifecycle)."""
from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from typing import Any

from ..errors import Conflict, Invalid, NotFound
from ..pagination import PageParams, finish, keyset
from ..util import dumps, loads, new_id, now_iso
from . import in_clause, row, rows

LOCATOR_STATUSES = ("provisional", "validated", "rejected", "superseded")
HEALING_OUTCOMES = ("healed", "ambiguous", "no_confident_candidate", "retry_failed", "budget_exhausted")
# review decision -> (allowed current statuses, new status)
_TRANSITIONS = {"validate": (("provisional",), "validated"), "reject": (("provisional", "validated"), "rejected")}


def insert_attempt(conn: sqlite3.Connection, project_id: str, test_run_id: str, *, step_index: int, step_key: str,
                   fingerprint: str, original: dict[str, Any], failure_reason: str, candidates: list[dict[str, Any]],
                   selected: dict[str, Any] | None, confidence: float | None, reason: str, outcome: str,
                   evidence: dict[str, Any]) -> str:
    aid = new_id()
    conn.execute(
        "INSERT INTO healing_attempts (id, project_id, test_run_id, step_index, step_key, target_fingerprint, "
        "original_locator_json, failure_reason, candidates_json, selected_json, confidence, selection_reason, outcome, "
        "evidence_json, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (aid, project_id, test_run_id, step_index, step_key, fingerprint, dumps(original), failure_reason,
         dumps(candidates), dumps(selected) if selected else None, confidence, reason, outcome, dumps(evidence), now_iso()),
    )
    return aid


def record_provisional(conn: sqlite3.Connection, project_id: str, *, test_case_id: str, fingerprint: str, step_key: str,
                       original: dict[str, Any], locator: dict[str, Any], confidence: float, attempt_id: str) -> str:
    """A successful heal becomes a *provisional* replacement. It is not used ahead of the
    original locator until a reviewer validates it."""
    same = conn.execute(
        "SELECT id FROM locators WHERE project_id = ? AND test_case_id = ? AND target_fingerprint = ? "
        "AND status = 'provisional' AND locator_json = ?",
        (project_id, test_case_id, fingerprint, dumps(locator)),
    ).fetchone()
    if same:
        return same[0]
    conn.execute(
        "UPDATE locators SET status = 'superseded' WHERE project_id = ? AND test_case_id = ? AND target_fingerprint = ? "
        "AND status = 'provisional'", (project_id, test_case_id, fingerprint),
    )
    lid = new_id()
    conn.execute(
        "INSERT INTO locators (id, project_id, test_case_id, target_fingerprint, step_key, original_json, locator_json, "
        "status, confidence, healing_attempt_id, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, 'provisional', ?, ?, ?)",
        (lid, project_id, test_case_id, fingerprint, step_key, dumps(original), dumps(locator), confidence, attempt_id,
         now_iso()),
    )
    return lid


def validated_locator(conn: sqlite3.Connection, project_id: str, test_case_id: str, fingerprint: str
                      ) -> dict[str, Any] | None:
    r = row(conn.execute(
        "SELECT * FROM locators WHERE project_id = ? AND test_case_id = ? AND target_fingerprint = ? AND status = 'validated'",
        (project_id, test_case_id, fingerprint),
    ).fetchone())
    if r:
        r["locator"] = loads(r["locator_json"])
    return r


def rejected_specs(conn: sqlite3.Connection, project_id: str, test_case_id: str, fingerprint: str) -> list[dict[str, Any]]:
    return [loads(r[0]) for r in conn.execute(
        "SELECT locator_json FROM locators WHERE project_id = ? AND test_case_id = ? AND target_fingerprint = ? "
        "AND status = 'rejected'", (project_id, test_case_id, fingerprint),
    )]


def get_locator(conn: sqlite3.Connection, project_id: str, locator_id: str) -> dict[str, Any]:
    r = row(conn.execute("SELECT * FROM locators WHERE project_id = ? AND id = ?", (project_id, locator_id)).fetchone())
    if r is None:
        raise NotFound("Locator not found in this project.", code="locator_not_found")
    return r


def review_locator(conn: sqlite3.Connection, project_id: str, locator_id: str, decision: str, note: str | None
                   ) -> dict[str, Any]:
    if decision not in _TRANSITIONS:
        raise Invalid("decision must be 'validate' or 'reject'", code="invalid_decision")
    loc = get_locator(conn, project_id, locator_id)
    allowed, new_status = _TRANSITIONS[decision]
    if loc["status"] not in allowed:
        raise Conflict(f"A {loc['status']} locator cannot be {decision}d.", code="invalid_transition")
    if new_status == "validated":
        conn.execute(
            "UPDATE locators SET status = 'superseded' WHERE project_id = ? AND test_case_id = ? AND target_fingerprint = ? "
            "AND status = 'validated'", (project_id, loc["test_case_id"], loc["target_fingerprint"]),
        )
    conn.execute("UPDATE locators SET status = ?, reviewed_at = ?, review_note = ? WHERE project_id = ? AND id = ?",
                 (new_status, now_iso(), note, project_id, locator_id))
    return get_locator(conn, project_id, locator_id)


@dataclass
class LocatorFilters:
    test_case_id: str | None = None
    statuses: list[str] | None = None


def list_locators(conn: sqlite3.Connection, project_id: str, f: LocatorFilters, page: PageParams
                  ) -> tuple[list[dict[str, Any]], str | None]:
    clauses, params = ["project_id = ?"], [project_id]
    if f.test_case_id:
        clauses.append("test_case_id = ?")
        params.append(f.test_case_id)
    if f.statuses:
        clauses.append(f"status IN {in_clause(f.statuses)}")
        params += f.statuses
    where, kparams, tail = keyset("created_at", page)
    rs = conn.execute(f"SELECT * FROM locators WHERE {' AND '.join(clauses)}{where}{tail}", (*params, *kparams)).fetchall()
    return finish(rows(rs), page, "created_at", f"locators:{project_id}")


@dataclass
class AttemptFilters:
    test_run_id: str | None = None
    outcomes: list[str] | None = None


def list_attempts(conn: sqlite3.Connection, project_id: str, f: AttemptFilters, page: PageParams
                  ) -> tuple[list[dict[str, Any]], str | None]:
    clauses, params = ["project_id = ?"], [project_id]
    if f.test_run_id:
        clauses.append("test_run_id = ?")
        params.append(f.test_run_id)
    if f.outcomes:
        clauses.append(f"outcome IN {in_clause(f.outcomes)}")
        params += f.outcomes
    where, kparams, tail = keyset("created_at", page)
    rs = conn.execute(f"SELECT * FROM healing_attempts WHERE {' AND '.join(clauses)}{where}{tail}",
                      (*params, *kparams)).fetchall()
    return finish(rows(rs), page, "created_at", f"healing:{project_id}")


def attempts_for_test_run(conn: sqlite3.Connection, project_id: str, test_run_id: str) -> list[dict[str, Any]]:
    return rows(conn.execute(
        "SELECT * FROM healing_attempts WHERE project_id = ? AND test_run_id = ? ORDER BY step_index, created_at",
        (project_id, test_run_id),
    ).fetchall())
