"""Terminal state transitions for test runs, shared by the executor, cancellation and
interrupted-run recovery. A test run is finalized exactly once; the persisted report,
the run's status and the test case's status are updated in the same transaction."""
from __future__ import annotations

import sqlite3
from datetime import timedelta

from ..db import Database
from ..reports import ERROR_SOURCES, build_report
from ..repo import TERMINAL
from ..repo import bugs as bugs_repo
from ..repo import runs as runs_repo
from ..repo import tests as tests_repo
from ..util import dumps, iso, new_id, now_iso, parse_iso, utcnow


def finalize_in(conn: sqlite3.Connection, project_id: str, test_run_id: str, status: str,
                error_kind: str | None = None, error_message: str | None = None, healed_steps: int = 0,
                verdict_source: str | None = None) -> bool:
    tr = conn.execute("SELECT * FROM test_runs WHERE project_id = ? AND id = ?", (project_id, test_run_id)).fetchone()
    if tr is None or tr["status"] in TERMINAL:
        return False
    finished = now_iso()
    duration = None
    if tr["started_at"]:
        duration = int((parse_iso(finished) - parse_iso(tr["started_at"])).total_seconds() * 1000)
    source = ERROR_SOURCES.get(error_kind or "") if status in ("failed", "error") else None
    conn.execute(
        "UPDATE test_runs SET status = ?, error_kind = ?, error_source = ?, error_message = ?, finished_at = ?, "
        "duration_ms = ?, healed_steps = ?, verdict_source = ? WHERE project_id = ? AND id = ?",
        (status, error_kind, source, (error_message or None) and error_message[:2000], finished, duration,
         healed_steps, verdict_source, project_id, test_run_id),
    )
    report = build_report(conn, project_id, test_run_id)
    bugs_repo.record_run(conn, project_id, {**dict(tr), "status": status}, report)
    conn.execute("INSERT INTO reports (id, project_id, test_run_id, outcome, report_json, created_at) VALUES (?, ?, ?, ?, ?, ?)",
                 (new_id(), project_id, test_run_id, status, dumps(report), finished))
    runs_repo.add_event(conn, project_id, tr["run_id"], test_run_id,
                        f"Test finished: {status.upper()} - {report['summary']}",
                        "info" if status == "passed" else "warning" if status == "cancelled" else "error")
    runs_repo.refresh_run_status(conn, project_id, tr["run_id"])
    tests_repo.refresh_test_case_status(conn, project_id, tr["test_case_id"])
    return True


def finalize_test_run(db: Database, project_id: str, test_run_id: str, status: str, error_kind: str | None = None,
                      error_message: str | None = None, healed_steps: int = 0, verdict_source: str | None = None) -> bool:
    with db.tx() as conn:
        return finalize_in(conn, project_id, test_run_id, status, error_kind, error_message, healed_steps,
                           verdict_source)


def recover_interrupted(db: Database, stale_after_s: int | None) -> int:
    """Marks running test runs whose worker is gone as Error/interrupted.

    stale_after_s=None: at startup of the (single) server process every running test run is
    orphaned. Otherwise only runs whose heartbeat is older than the cutoff. Interrupted runs
    are not re-executed automatically: partially executed UI actions may have side effects."""
    cutoff = None if stale_after_s is None else iso(utcnow() - timedelta(seconds=stale_after_s))
    with db.tx() as conn:
        stale = runs_repo.stale_running(conn, cutoff)
        for tr in stale:
            finalize_in(conn, tr["project_id"], tr["id"], "error", "interrupted",
                        "The test runner stopped while this test was running. It was not retried automatically "
                        "because partially executed UI actions may have side effects; start a new run.")
    return len(stale)
