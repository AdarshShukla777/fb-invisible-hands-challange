"""Bugs found by test runs: the tester's list, and their decisions (not a bug, fixed, reopen)."""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Body, Depends, Query

from ..pagination import parse_page
from ..repo import bugs as bugs_repo
from ..repo import projects as projects_repo
from . import schemas as s
from .state import AppState, get_state

router = APIRouter()


def bug(b: dict[str, Any]) -> dict[str, Any]:
    return {"id": b["id"], "testId": b["test_case_id"], "testTitle": b.get("test_title"), "title": b["title"],
            "source": b["source"], "action": b["action"], "check": b["check_text"], "expected": b["expected"],
            "actual": b["actual"], "pageUrl": b["page_url"], "screenshotUrl": b["screenshot_url"],
            "status": b["status"], "regressed": bool(b["regressed"]), "occurrences": b["occurrences"],
            "firstTestRunId": b["first_test_run_id"], "lastTestRunId": b["last_test_run_id"],
            "lastRunId": b["last_run_id"], "fixedTestRunId": b["fixed_test_run_id"], "note": b["note"],
            "firstSeenAt": b["first_seen_at"], "lastSeenAt": b["last_seen_at"], "updatedAt": b["updated_at"]}


@router.get("/projects/{project_id}/bugs")
def list_bugs(project_id: str, status: str | None = None, test_id: str | None = Query(default=None, alias="testId"),
              limit: int | None = None, cursor: str | None = None, order: str | None = None,
              st: AppState = Depends(get_state)) -> dict[str, Any]:
    """Bugs of the project, most recently seen first; status filter: open, fixed, dismissed (comma-separated)."""
    statuses = s.csv_param(status, bugs_repo.STATUSES, "status")
    with st.db.read() as conn:
        projects_repo.get_project(conn, project_id)
        page = parse_page(limit, cursor, order, f"bugs:{project_id}")
        items, nxt = bugs_repo.list_bugs(conn, project_id, statuses, test_id, page)
        counts = bugs_repo.counts(conn, project_id)
    return {**s.listing([bug(b) for b in items], nxt), "counts": counts}


@router.patch("/projects/{project_id}/bugs/{bug_id}")
def update_bug(project_id: str, bug_id: str, body: dict[str, Any] = Body(...),
               st: AppState = Depends(get_state)) -> dict[str, Any]:
    """The tester's decision: {"status": "dismissed" | "open" | "fixed", "note": "..."}. A dismissed bug
    is shown to later agent runs as expected behaviour."""
    with st.db.tx() as conn:
        projects_repo.get_project(conn, project_id)
        return bug(bugs_repo.update_bug(conn, project_id, bug_id, body.get("status"), body.get("note")))
