"""Page history, replacement locators and healing attempts."""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, Query

from ..pagination import parse_page
from ..repo import healing as healing_repo
from ..repo import history as history_repo
from ..repo import projects as projects_repo
from ..repo import tests as tests_repo
from . import schemas as s
from .state import AppState, get_state

router = APIRouter()


@router.get("/projects/{project_id}/pages")
def list_pages(project_id: str, q: str | None = None, freshness: str = "all", limit: int | None = None,
               cursor: str | None = None, order: str | None = None, st: AppState = Depends(get_state)) -> dict[str, Any]:
    stale_before = st.stale_before()
    with st.db.read() as conn:
        projects_repo.get_project(conn, project_id)
        page = parse_page(limit, cursor, order, f"pages:{project_id}")
        items, nxt = history_repo.list_pages(
            conn, project_id, history_repo.PageFilters(q=q or None, freshness=freshness, stale_before=stale_before), page)
    return s.listing([s.page(p, stale_before) for p in items], nxt)


@router.get("/projects/{project_id}/pages/{page_id}")
def get_page(project_id: str, page_id: str, st: AppState = Depends(get_state)) -> dict[str, Any]:
    with st.db.read() as conn:
        projects_repo.get_project(conn, project_id)
        return s.page(history_repo.get_page(conn, project_id, page_id), st.stale_before())


@router.get("/projects/{project_id}/pages/{page_id}/versions")
def list_page_versions(project_id: str, page_id: str, limit: int | None = None, cursor: str | None = None,
                       order: str | None = None, st: AppState = Depends(get_state)) -> dict[str, Any]:
    with st.db.read() as conn:
        projects_repo.get_project(conn, project_id)
        page = parse_page(limit, cursor, order, f"page-versions:{project_id}:{page_id}")
        items, nxt = history_repo.list_versions(conn, project_id, page_id, page)
    return s.listing([s.page_version(v) for v in items], nxt)


@router.get("/projects/{project_id}/locators")
def list_locators(project_id: str, test_id: str | None = Query(default=None, alias="testId"), status: str | None = None,
                  limit: int | None = None, cursor: str | None = None, order: str | None = None,
                  st: AppState = Depends(get_state)) -> dict[str, Any]:
    filters = healing_repo.LocatorFilters(test_case_id=test_id,
                                          statuses=s.csv_param(status, healing_repo.LOCATOR_STATUSES, "status"))
    with st.db.read() as conn:
        projects_repo.get_project(conn, project_id)
        if test_id:
            tests_repo.get_test_case(conn, project_id, test_id)
        page = parse_page(limit, cursor, order, f"locators:{project_id}")
        items, nxt = healing_repo.list_locators(conn, project_id, filters, page)
    return s.listing([s.locator(entry) for entry in items], nxt)


@router.post("/projects/{project_id}/locators/{locator_id}/review")
def review_locator(project_id: str, locator_id: str, body: s.ReviewIn, st: AppState = Depends(get_state)) -> dict[str, Any]:
    """validate: the replacement is used ahead of the original locator in future runs.
    reject: it is never applied again for this target (past reports are unchanged)."""
    with st.db.tx() as conn:
        projects_repo.get_project(conn, project_id)
        return s.locator(healing_repo.review_locator(conn, project_id, locator_id, body.decision, body.note))


@router.get("/projects/{project_id}/healing-attempts")
def list_healing_attempts(project_id: str, test_run_id: str | None = Query(default=None, alias="testRunId"),
                          outcome: str | None = None, limit: int | None = None, cursor: str | None = None,
                          order: str | None = None, st: AppState = Depends(get_state)) -> dict[str, Any]:
    filters = healing_repo.AttemptFilters(test_run_id=test_run_id,
                                          outcomes=s.csv_param(outcome, healing_repo.HEALING_OUTCOMES, "outcome"))
    with st.db.read() as conn:
        projects_repo.get_project(conn, project_id)
        page = parse_page(limit, cursor, order, f"healing:{project_id}")
        items, nxt = healing_repo.list_attempts(conn, project_id, filters, page)
    return s.listing([s.healing_attempt(a) for a in items], nxt)
