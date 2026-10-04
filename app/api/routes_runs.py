"""Runs, test runs, reports and artifacts."""
from __future__ import annotations

import re
from typing import Any

from fastapi import APIRouter, Depends, Header, Query, Response
from fastapi.responses import FileResponse, JSONResponse

from ..errors import Conflict, Invalid, NotFound
from ..execution.finalize import finalize_in
from ..pagination import parse_page
from ..reports import ERROR_SOURCES, build_report
from ..repo import RUN_STATUSES
from ..repo import artifacts as artifacts_repo
from ..repo import projects as projects_repo
from ..repo import runs as runs_repo
from ..util import loads
from . import schemas as s
from .state import AppState, get_state

router = APIRouter()
_IDEMPOTENCY_KEY = re.compile(r"^[A-Za-z0-9._:\-]{1,200}$")


def _report(conn, project_id: str, test_run_id: str) -> dict[str, Any]:
    """Persisted report when the test run is finished; otherwise live progress."""
    r = conn.execute("SELECT report_json FROM reports WHERE project_id = ? AND test_run_id = ?",
                     (project_id, test_run_id)).fetchone()
    return loads(r[0]) if r else build_report(conn, project_id, test_run_id)


def _run_detail(conn, project_id: str, run_id: str) -> dict[str, Any]:
    run = runs_repo.get_run(conn, project_id, run_id)
    out = s.run(run)
    out["reports"] = [_report(conn, project_id, tr["id"]) for tr in run["test_runs"]]
    out["events"] = [s.event(e) for e in runs_repo.list_events(conn, project_id, run_id)]
    return out


@router.post("/projects/{project_id}/runs", status_code=201)
def create_run(project_id: str, body: s.RunIn | None = None,
               idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
               st: AppState = Depends(get_state)) -> Any:
    """Queues one execution per requested test (all tests when testIds is omitted).

    Duplicate protection: the same Idempotency-Key returns the original run (200), and a
    test that is already queued/running is skipped (409 if nothing else can run)."""
    if idempotency_key is not None and not _IDEMPOTENCY_KEY.match(idempotency_key):
        raise Invalid("Idempotency-Key must be 1-200 characters of letters, digits and . _ : -", code="invalid_idempotency_key")
    with st.db.tx() as conn:
        projects_repo.get_project(conn, project_id)
        run, skipped, replayed = runs_repo.create_run(conn, project_id, body.test_ids if body else None, idempotency_key)
        if not replayed:
            n = len(run["test_runs"])
            projects_repo.add_message(conn, project_id, "system", f"Run started for {n} test{'s' if n != 1 else ''}."
                                      + (f" Skipped {len(skipped)} (see run details)." if skipped else ""))
    st.notify_workers()
    payload = {"run": s.run(run), "skipped": skipped, "replayed": replayed}
    return JSONResponse(payload, status_code=200 if replayed else 201)


@router.get("/projects/{project_id}/runs")
def list_runs(project_id: str, status: str | None = None, test_id: str | None = Query(default=None, alias="testId"),
              since: str | None = None, until: str | None = None, limit: int | None = None, cursor: str | None = None,
              order: str | None = None, st: AppState = Depends(get_state)) -> dict[str, Any]:
    since_ts, until_ts = s.time_range(since, until)
    filters = runs_repo.RunFilters(statuses=s.csv_param(status, RUN_STATUSES, "status"), test_case_id=test_id,
                                   since=since_ts, until=until_ts)
    with st.db.read() as conn:
        projects_repo.get_project(conn, project_id)
        page = parse_page(limit, cursor, order, f"runs:{project_id}")
        items, nxt = runs_repo.list_runs(conn, project_id, filters, page)
    return s.listing([s.run(r) for r in items], nxt)


@router.get("/projects/{project_id}/runs/{run_id}")
def get_run(project_id: str, run_id: str, st: AppState = Depends(get_state)) -> dict[str, Any]:
    with st.db.read() as conn:
        projects_repo.get_project(conn, project_id)
        return _run_detail(conn, project_id, run_id)


@router.get("/projects/{project_id}/runs/{run_id}/events")
def run_events(project_id: str, run_id: str, after: int = Query(default=0, ge=0),
               limit: int = Query(default=200, ge=1, le=500), st: AppState = Depends(get_state)) -> dict[str, Any]:
    """Incremental progress log: poll with ?after=<last id seen>."""
    with st.db.read() as conn:
        projects_repo.get_project(conn, project_id)
        run = runs_repo.get_run(conn, project_id, run_id)
        events = [s.event(e) for e in runs_repo.list_events(conn, project_id, run_id, after, limit)]
    return {"status": run["status"], "items": events, "lastId": events[-1]["id"] if events else after}


@router.post("/projects/{project_id}/runs/{run_id}/cancel")
def cancel_run(project_id: str, run_id: str, st: AppState = Depends(get_state)) -> dict[str, Any]:
    """Queued tests are cancelled immediately; running tests stop at the next step boundary."""
    with st.db.tx() as conn:
        projects_repo.get_project(conn, project_id)
        queued, running = runs_repo.request_cancel(conn, project_id, run_id)
        for test_run_id in queued:
            finalize_in(conn, project_id, test_run_id, "cancelled", "cancelled", "Cancelled before it started.")
        if running:
            runs_repo.add_event(conn, project_id, run_id, None,
                                f"Cancellation requested for {len(running)} running test(s).", "warning")
        return _run_detail(conn, project_id, run_id)


@router.get("/projects/{project_id}/test-runs")
def list_test_runs(
    project_id: str,
    test_id: str | None = Query(default=None, alias="testId"),
    revision_id: str | None = Query(default=None, alias="revisionId"),
    status: str | None = None,
    error_kind: str | None = Query(default=None, alias="errorKind"),
    error_source: str | None = Query(default=None, alias="errorSource"),
    healed: bool | None = None,
    since: str | None = None, until: str | None = None,
    limit: int | None = None, cursor: str | None = None, order: str | None = None,
    st: AppState = Depends(get_state),
) -> dict[str, Any]:
    since_ts, until_ts = s.time_range(since, until)
    filters = runs_repo.TestRunFilters(
        test_case_id=test_id, revision_id=revision_id, statuses=s.csv_param(status, RUN_STATUSES, "status"),
        error_kinds=s.csv_param(error_kind, (*ERROR_SOURCES, "cancelled"), "errorKind"),
        error_sources=s.csv_param(error_source, ("application", "agent", "browser", "infrastructure"), "errorSource"),
        healed=healed, since=since_ts, until=until_ts,
    )
    with st.db.read() as conn:
        projects_repo.get_project(conn, project_id)
        page = parse_page(limit, cursor, order, f"test-runs:{project_id}")
        items, nxt = runs_repo.list_test_runs(conn, project_id, filters, page)
    return s.listing([s.test_run(t) for t in items], nxt)


@router.get("/projects/{project_id}/test-runs/{test_run_id}")
def get_test_run(project_id: str, test_run_id: str, st: AppState = Depends(get_state)) -> dict[str, Any]:
    with st.db.read() as conn:
        projects_repo.get_project(conn, project_id)
        tr = runs_repo.get_test_run(conn, project_id, test_run_id)
        return {**s.test_run(tr), "report": _report(conn, project_id, test_run_id)}


@router.get("/projects/{project_id}/test-runs/{test_run_id}/report")
def get_report(project_id: str, test_run_id: str, st: AppState = Depends(get_state)) -> dict[str, Any]:
    with st.db.read() as conn:
        projects_repo.get_project(conn, project_id)
        tr = runs_repo.get_test_run(conn, project_id, test_run_id)
        r = conn.execute("SELECT report_json FROM reports WHERE project_id = ? AND test_run_id = ?",
                         (project_id, test_run_id)).fetchone()
    if r is None:
        raise Conflict(f"The test run is still {tr['status']}; its report is written when it finishes. "
                       "GET the test run for live progress.", code="report_not_ready")
    return loads(r[0])


@router.get("/projects/{project_id}/artifacts/{artifact_id}")
def get_artifact(project_id: str, artifact_id: str, st: AppState = Depends(get_state)) -> Response:
    with st.db.read() as conn:
        projects_repo.get_project(conn, project_id)
        a = artifacts_repo.get_artifact(conn, project_id, artifact_id)
    root = st.settings.artifacts_dir.resolve()
    path = (root / a["rel_path"]).resolve()
    if root not in path.parents or not path.is_file():
        raise NotFound("Artifact file is missing.", code="artifact_missing")
    return FileResponse(path, media_type=a["content_type"], filename=path.name,
                        content_disposition_type="inline" if a["kind"] != "trace" else "attachment")
