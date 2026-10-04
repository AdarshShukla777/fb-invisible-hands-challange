"""Runs (user requests), test runs (one execution of one revision), steps and events."""
from __future__ import annotations

import hashlib
import sqlite3
from dataclasses import dataclass
from typing import Any

from ..errors import Conflict, Invalid, NotFound
from ..pagination import PageParams, finish, keyset
from ..util import dumps, loads, new_id, now_iso
from . import ACTIVE, TERMINAL, in_clause, row, rows
from . import tests as tests_repo

MAX_TESTS_PER_RUN = 100

_TEST_RUN_SELECT = """
SELECT tr.*, r.revision_number, r.instruction, tc.title
FROM test_runs tr
JOIN test_case_revisions r ON r.id = tr.revision_id
JOIN test_cases tc ON tc.id = tr.test_case_id
"""


def request_hash(test_case_ids: list[str] | None) -> str:
    return hashlib.sha1(dumps({"testIds": test_case_ids}).encode()).hexdigest()


def create_run(conn: sqlite3.Connection, project_id: str, test_case_ids: list[str] | None,
               idempotency_key: str | None) -> tuple[dict[str, Any], list[dict[str, Any]], bool]:
    """Returns (run, skipped, replayed). Must run inside db.tx() (BEGIN IMMEDIATE), which
    serializes the duplicate checks below against concurrent requests."""
    if test_case_ids is not None:
        test_case_ids = list(dict.fromkeys(test_case_ids))  # dedupe, keep order
        if not test_case_ids:
            raise Invalid("testIds must not be empty; omit it to run all tests.", code="empty_test_ids")
        if len(test_case_ids) > MAX_TESTS_PER_RUN:
            raise Invalid(f"At most {MAX_TESTS_PER_RUN} tests per run.", code="too_many_tests")
    req_hash = request_hash(test_case_ids)

    if idempotency_key:
        existing = row(conn.execute("SELECT * FROM runs WHERE project_id = ? AND idempotency_key = ?",
                                    (project_id, idempotency_key)).fetchone())
        if existing:
            if existing["request_hash"] != req_hash:
                raise Invalid("This Idempotency-Key was already used for a different request.", code="idempotency_key_reused")
            return get_run(conn, project_id, existing["id"]), loads(existing["skipped_json"], []), True

    if test_case_ids is not None:
        found = tests_repo.get_test_cases(conn, project_id, test_case_ids)
        missing = [i for i in test_case_ids if i not in found]
        if missing:
            raise NotFound("Test case(s) not found in this project: " + ", ".join(missing), code="test_case_not_found")
        candidates = [found[i] for i in test_case_ids]
    else:
        candidates = rows(conn.execute(
            f"{tests_repo._SELECT} WHERE tc.project_id = ? AND tc.archived_at IS NULL ORDER BY tc.created_at, tc.id",
            (project_id,),
        ).fetchall())
        if not candidates:
            raise Invalid("This project has no tests yet. Add a test first.", code="no_tests")

    runnable, skipped = [], []
    for tc in candidates:
        active = conn.execute(
            f"SELECT id, run_id FROM test_runs WHERE project_id = ? AND test_case_id = ? AND status IN {in_clause(list(ACTIVE))}",
            (project_id, tc["id"], *ACTIVE),
        ).fetchone()
        if tc["archived_at"]:
            skipped.append({"testId": tc["id"], "reason": "archived", "detail": "The test is archived."})
        elif tc["plan_status"] != "ready":
            issues = loads(tc["issues_json"], [])
            skipped.append({"testId": tc["id"], "reason": tc["plan_status"],
                            "detail": issues[0]["message"] if issues else "The test plan is not runnable."})
        elif active:
            skipped.append({"testId": tc["id"], "reason": "already_active", "activeTestRunId": active["id"],
                            "activeRunId": active["run_id"], "detail": "This test is already queued or running."})
        else:
            runnable.append(tc)
    if not runnable:
        details = {"skipped": skipped}
        if skipped and all(s["reason"] == "already_active" for s in skipped):
            raise Conflict("The requested tests are already queued or running.", code="already_active", details=details)
        hint = ""
        if any(s["reason"] in ("needs_clarification", "invalid") for s in skipped):
            hint = (" Edit the test to clarify it, or switch it to agent mode so the agent works it out on the "
                    "live app.")
        raise Invalid("None of the requested tests can run. " + skipped[0]["detail"] + hint,
                      code="no_runnable_tests", details=details)

    ts = now_iso()
    run_id = new_id()
    conn.execute(
        "INSERT INTO runs (id, project_id, status, idempotency_key, request_hash, skipped_json, created_at) "
        "VALUES (?, ?, 'queued', ?, ?, ?, ?)",
        (run_id, project_id, idempotency_key, req_hash, dumps(skipped), ts),
    )
    for position, tc in enumerate(runnable):
        try:
            conn.execute(
                "INSERT INTO test_runs (id, project_id, run_id, test_case_id, revision_id, position, status, queued_at) "
                "VALUES (?, ?, ?, ?, ?, ?, 'queued', ?)",
                (new_id(), project_id, run_id, tc["id"], tc["current_revision_id"], position, ts),
            )
        except sqlite3.IntegrityError as exc:  # defensive: the partial unique index is the last line
            raise Conflict("A test in this request is already queued or running.", code="already_active") from exc
        tests_repo.refresh_test_case_status(conn, project_id, tc["id"])
    add_event(conn, project_id, run_id, None, f"Queued {len(runnable)} test(s)"
              + (f"; skipped {len(skipped)}" if skipped else "") + ".")
    return get_run(conn, project_id, run_id), skipped, False


def get_run(conn: sqlite3.Connection, project_id: str, run_id: str) -> dict[str, Any]:
    r = row(conn.execute("SELECT * FROM runs WHERE project_id = ? AND id = ?", (project_id, run_id)).fetchone())
    if r is None:
        raise NotFound("Run not found in this project.", code="run_not_found")
    r["test_runs"] = _test_runs_for(conn, project_id, [run_id]).get(run_id, [])
    return r


def _test_runs_for(conn: sqlite3.Connection, project_id: str, run_ids: list[str]) -> dict[str, list[dict[str, Any]]]:
    if not run_ids:
        return {}
    rs = conn.execute(
        f"{_TEST_RUN_SELECT} WHERE tr.project_id = ? AND tr.run_id IN {in_clause(run_ids)} ORDER BY tr.run_id, tr.position",
        (project_id, *run_ids),
    ).fetchall()
    out: dict[str, list[dict[str, Any]]] = {}
    for r in rs:
        out.setdefault(r["run_id"], []).append(dict(r))
    return out


@dataclass
class RunFilters:
    statuses: list[str] | None = None
    test_case_id: str | None = None
    since: str | None = None
    until: str | None = None


def list_runs(conn: sqlite3.Connection, project_id: str, f: RunFilters, page: PageParams
              ) -> tuple[list[dict[str, Any]], str | None]:
    clauses, params = ["runs.project_id = ?"], [project_id]
    if f.statuses:
        clauses.append(f"runs.status IN {in_clause(f.statuses)}")
        params += f.statuses
    if f.test_case_id:
        tests_repo.get_test_case(conn, project_id, f.test_case_id)
        clauses.append("EXISTS (SELECT 1 FROM test_runs t WHERE t.project_id = runs.project_id "
                       "AND t.run_id = runs.id AND t.test_case_id = ?)")
        params.append(f.test_case_id)
    if f.since:
        clauses.append("runs.created_at >= ?")
        params.append(f.since)
    if f.until:
        clauses.append("runs.created_at < ?")
        params.append(f.until)
    where, kparams, tail = keyset("runs.created_at", page, "runs.id")
    rs = rows(conn.execute(f"SELECT runs.* FROM runs WHERE {' AND '.join(clauses)}{where}{tail}",
                           (*params, *kparams)).fetchall())
    items, cursor = finish(rs, page, "created_at", f"runs:{project_id}")
    grouped = _test_runs_for(conn, project_id, [r["id"] for r in items])
    for r in items:
        r["test_runs"] = grouped.get(r["id"], [])
    return items, cursor


@dataclass
class TestRunFilters:
    test_case_id: str | None = None
    revision_id: str | None = None
    statuses: list[str] | None = None
    error_kinds: list[str] | None = None
    error_sources: list[str] | None = None
    healed: bool | None = None
    since: str | None = None
    until: str | None = None


def list_test_runs(conn: sqlite3.Connection, project_id: str, f: TestRunFilters, page: PageParams
                   ) -> tuple[list[dict[str, Any]], str | None]:
    clauses, params = ["tr.project_id = ?"], [project_id]
    if f.test_case_id:
        tests_repo.get_test_case(conn, project_id, f.test_case_id)
        clauses.append("tr.test_case_id = ?")
        params.append(f.test_case_id)
    if f.revision_id:
        rev = tests_repo.get_revision(conn, project_id, f.revision_id)
        if f.test_case_id and rev["test_case_id"] != f.test_case_id:
            raise Invalid("revisionId does not belong to testId.", code="invalid_filter")
        clauses.append("tr.revision_id = ?")
        params.append(f.revision_id)
    for column, values in (("tr.status", f.statuses), ("tr.error_kind", f.error_kinds), ("tr.error_source", f.error_sources)):
        if values:
            clauses.append(f"{column} IN {in_clause(values)}")
            params += values
    if f.healed is not None:
        clauses.append("tr.healed_steps > 0" if f.healed else "tr.healed_steps = 0")
    if f.since:
        clauses.append("tr.queued_at >= ?")
        params.append(f.since)
    if f.until:
        clauses.append("tr.queued_at < ?")
        params.append(f.until)
    where, kparams, tail = keyset("tr.queued_at", page, "tr.id")
    rs = conn.execute(f"{_TEST_RUN_SELECT} WHERE {' AND '.join(clauses)}{where}{tail}", (*params, *kparams)).fetchall()
    return finish(rows(rs), page, "queued_at", f"test-runs:{project_id}")


def get_test_run(conn: sqlite3.Connection, project_id: str, test_run_id: str) -> dict[str, Any]:
    r = row(conn.execute(f"{_TEST_RUN_SELECT} WHERE tr.project_id = ? AND tr.id = ?", (project_id, test_run_id)).fetchone())
    if r is None:
        raise NotFound("Test run not found in this project.", code="test_run_not_found")
    return r


# -- worker side -----------------------------------------------------------------------------

def claim_next(conn: sqlite3.Connection, worker_id: str) -> dict[str, Any] | None:
    """Atomically moves the oldest queued test run to running. The queue is global (the
    worker pool is shared infrastructure); everything after the claim is project-scoped."""
    candidate = conn.execute(
        "SELECT id, project_id FROM test_runs WHERE status = 'queued' AND cancel_requested = 0 "
        "ORDER BY queued_at, position, id LIMIT 1"
    ).fetchone()
    if candidate is None:
        return None
    ts = now_iso()
    cur = conn.execute(
        "UPDATE test_runs SET status = 'running', worker_id = ?, started_at = ?, heartbeat_at = ? "
        "WHERE id = ? AND status = 'queued'",
        (worker_id, ts, ts, candidate["id"]),
    )
    if cur.rowcount != 1:
        return None
    tr = get_test_run(conn, candidate["project_id"], candidate["id"])
    refresh_run_status(conn, tr["project_id"], tr["run_id"])
    tests_repo.refresh_test_case_status(conn, tr["project_id"], tr["test_case_id"])
    return tr


def heartbeat(conn: sqlite3.Connection, project_id: str, test_run_id: str) -> bool:
    """Updates the heartbeat and returns whether cancellation was requested."""
    r = conn.execute(
        "UPDATE test_runs SET heartbeat_at = ? WHERE project_id = ? AND id = ? AND status = 'running' "
        "RETURNING cancel_requested", (now_iso(), project_id, test_run_id),
    ).fetchone()
    return bool(r and r[0])


def stale_running(conn: sqlite3.Connection, heartbeat_before: str | None) -> list[dict[str, Any]]:
    """Running test runs whose worker stopped reporting (all running ones if no cutoff given)."""
    if heartbeat_before is None:
        return rows(conn.execute("SELECT id, project_id FROM test_runs WHERE status = 'running'").fetchall())
    return rows(conn.execute(
        "SELECT id, project_id FROM test_runs WHERE status = 'running' AND (heartbeat_at IS NULL OR heartbeat_at < ?)",
        (heartbeat_before,),
    ).fetchall())


def request_cancel(conn: sqlite3.Connection, project_id: str, run_id: str) -> tuple[list[str], list[str]]:
    """Returns (queued test runs to finalize as cancelled, running test runs flagged)."""
    get_run(conn, project_id, run_id)
    queued = [r[0] for r in conn.execute(
        "SELECT id FROM test_runs WHERE project_id = ? AND run_id = ? AND status = 'queued'", (project_id, run_id))]
    running = [r[0] for r in conn.execute(
        "UPDATE test_runs SET cancel_requested = 1 WHERE project_id = ? AND run_id = ? AND status = 'running' "
        "RETURNING id", (project_id, run_id))]
    return queued, running


def refresh_run_status(conn: sqlite3.Connection, project_id: str, run_id: str) -> None:
    """Derived from the run's test runs: active if any is active; otherwise the most
    actionable outcome wins (failed > error > cancelled > passed)."""
    statuses = [r[0] for r in conn.execute("SELECT status FROM test_runs WHERE project_id = ? AND run_id = ?",
                                           (project_id, run_id))]
    if not statuses:
        return
    finished_at = None
    if "running" in statuses or ("queued" in statuses and any(s in TERMINAL for s in statuses)):
        status = "running"
    elif "queued" in statuses:
        status = "queued"
    else:
        status = next(s for s in ("failed", "error", "cancelled", "passed") if s in statuses)
        finished_at = conn.execute("SELECT MAX(finished_at) FROM test_runs WHERE project_id = ? AND run_id = ?",
                                   (project_id, run_id)).fetchone()[0]
    conn.execute("UPDATE runs SET status = ?, finished_at = ? WHERE project_id = ? AND id = ?",
                 (status, finished_at, project_id, run_id))


def add_event(conn: sqlite3.Connection, project_id: str, run_id: str, test_run_id: str | None, message: str,
              level: str = "info") -> None:
    conn.execute("INSERT INTO run_events (project_id, run_id, test_run_id, at, level, message) VALUES (?, ?, ?, ?, ?, ?)",
                 (project_id, run_id, test_run_id, now_iso(), level, message[:1000]))


def list_events(conn: sqlite3.Connection, project_id: str, run_id: str, after_id: int = 0, limit: int = 500
                ) -> list[dict[str, Any]]:
    return rows(conn.execute(
        "SELECT * FROM run_events WHERE project_id = ? AND run_id = ? AND id > ? ORDER BY id LIMIT ?",
        (project_id, run_id, after_id, limit),
    ).fetchall())


def insert_step_result(conn: sqlite3.Connection, project_id: str, test_run_id: str, **f: Any) -> None:
    conn.execute(
        """INSERT INTO step_results (id, project_id, test_run_id, step_index, step_key, action, kind, description, status,
               target_fingerprint, locator_json, healed, expected, actual, detail, error_kind, error_message, page_id,
               page_url, element_json, started_at, finished_at, duration_ms, origin, layout_key, layout_json)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (new_id(), project_id, test_run_id, f["step_index"], f["step_key"], f["action"], f["kind"], f["description"],
         f["status"], f.get("target_fingerprint"), dumps(f["locator"]) if f.get("locator") else None,
         1 if f.get("healed") else 0, f.get("expected"), f.get("actual"), f.get("detail"), f.get("error_kind"),
         f.get("error_message"), f.get("page_id"), f.get("page_url"),
         dumps(f["element"]) if f.get("element") else None, f.get("started_at"), f.get("finished_at"),
         f.get("duration_ms"), f.get("origin"), f.get("layout_key"),
         dumps(f["layout"]) if f.get("layout") else None),
    )


def step_results(conn: sqlite3.Connection, project_id: str, test_run_id: str) -> list[dict[str, Any]]:
    return rows(conn.execute("SELECT * FROM step_results WHERE project_id = ? AND test_run_id = ? ORDER BY step_index",
                             (project_id, test_run_id)).fetchall())
