"""Projects, chat messages and test definitions."""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, Query, Response

from .. import urls
from ..agent import memory as agent_memory
from ..errors import Invalid, NotFound
from ..pagination import parse_page
from ..planning.rules import PlanContext
from ..planning.service import PlanOutcome
from ..planning.splitter import split_instructions
from ..repo import RUN_STATUSES
from ..repo import history as history_repo
from ..repo import projects as projects_repo
from ..repo import tests as tests_repo
from . import schemas as s
from .state import AppState, get_state

router = APIRouter()
MAX_INSTRUCTIONS = 20
MAX_INSTRUCTION_CHARS = 4000


# -- projects -------------------------------------------------------------------------------

@router.get("/projects")
def list_projects(limit: int | None = None, cursor: str | None = None, order: str | None = None,
                  st: AppState = Depends(get_state)) -> dict[str, Any]:
    page = parse_page(limit, cursor, order, "projects")
    with st.db.read() as conn:
        items, nxt = projects_repo.list_projects(conn, page)
    return s.listing([s.project(p) for p in items], nxt)


def _base_url(raw: str) -> str:
    url = urls.normalize_base_url(raw)
    if not url:
        raise Invalid("Enter a valid http(s) URL, e.g. http://localhost:3000", code="invalid_url")
    return url


@router.post("/projects", status_code=201)
def create_project(body: s.ProjectIn, st: AppState = Depends(get_state)) -> dict[str, Any]:
    with st.db.tx() as conn:
        return s.project(projects_repo.create_project(conn, body.name.strip(), _base_url(body.url)))


@router.get("/projects/{project_id}")
def get_project(project_id: str, st: AppState = Depends(get_state)) -> dict[str, Any]:
    with st.db.read() as conn:
        return s.project(projects_repo.get_project(conn, project_id))


@router.patch("/projects/{project_id}")
def update_project(project_id: str, body: s.ProjectPatch, st: AppState = Depends(get_state)) -> dict[str, Any]:
    with st.db.tx() as conn:
        return s.project(projects_repo.update_project(
            conn, project_id, body.name.strip() if body.name else None, _base_url(body.url) if body.url else None))


@router.delete("/projects/{project_id}", status_code=204)
def delete_project(project_id: str, st: AppState = Depends(get_state)) -> Response:
    with st.db.tx() as conn:
        projects_repo.delete_project(conn, project_id)
    st.remove_artifacts(project_id)
    return Response(status_code=204)


@router.post("/projects/{project_id}/reset", status_code=204)
def reset_project(project_id: str, st: AppState = Depends(get_state)) -> Response:
    with st.db.tx() as conn:
        projects_repo.reset_project(conn, project_id)
    st.remove_artifacts(project_id)
    return Response(status_code=204)


@router.get("/projects/{project_id}/messages")
def list_messages(project_id: str, limit: int | None = None, cursor: str | None = None, order: str | None = None,
                  st: AppState = Depends(get_state)) -> dict[str, Any]:
    with st.db.read() as conn:
        projects_repo.get_project(conn, project_id)
        page = parse_page(limit, cursor, order, f"messages:{project_id}")
        items, nxt = projects_repo.list_messages(conn, project_id, page)
    return s.listing([s.message(m) for m in items], nxt)


# -- tests ----------------------------------------------------------------------------------

def _plan_context(st: AppState, project_id: str) -> PlanContext:
    with st.db.read() as conn:
        project = projects_repo.get_project(conn, project_id)
        known = history_repo.known_pages(conn, project_id)
    return PlanContext(base_url=project["base_url"], known_pages=known)


def _outcome_note(title: str, outcome: PlanOutcome) -> str:
    if outcome.status == "ready":
        return ""
    reasons = "; ".join(i["message"] for i in outcome.issues[:3])
    label = "needs clarification" if outcome.status == "needs_clarification" else "is invalid"
    return f'\n- "{title}" {label}: {reasons}'


@router.get("/projects/{project_id}/tests")
def list_tests(
    project_id: str,
    status: str | None = None,
    plan_status: str | None = Query(default=None, alias="planStatus"),
    q: str | None = None,
    include_archived: bool = Query(default=False, alias="includeArchived"),
    sort: str = "created_at",
    order: str | None = None,
    limit: int | None = None,
    cursor: str | None = None,
    st: AppState = Depends(get_state),
) -> dict[str, Any]:
    if sort not in {"created_at", "updated_at"}:
        raise Invalid("sort must be created_at or updated_at", code="invalid_sort")
    filters = tests_repo.TestFilters(
        statuses=s.csv_param(status, ("not_run", *RUN_STATUSES), "status"),
        plan_statuses=s.csv_param(plan_status, ("ready", "needs_clarification", "invalid"), "planStatus"),
        q=q or None, include_archived=include_archived, sort=sort,
    )
    with st.db.read() as conn:
        projects_repo.get_project(conn, project_id)
        page = parse_page(limit, cursor, order, f"tests:{project_id}:{sort}")
        items, nxt = tests_repo.list_test_cases(conn, project_id, filters, page)
    return s.listing([s.test_case(t) for t in items], nxt)


@router.post("/projects/{project_id}/tests", status_code=201)
def create_tests(project_id: str, body: s.TestsIn, st: AppState = Depends(get_state)) -> dict[str, Any]:
    if (body.text is None) == (body.instructions is None):
        raise Invalid("Send either 'text' (split into tests) or 'instructions' (one test each).", code="invalid_body")
    instructions = split_instructions(body.text) if body.text is not None else [
        " ".join(i.split()) for i in body.instructions or [] if i.strip()]
    if not instructions:
        raise Invalid("No test instructions found.", code="empty_instructions")
    if len(instructions) > MAX_INSTRUCTIONS:
        raise Invalid(f"At most {MAX_INSTRUCTIONS} tests per request.", code="too_many_tests")
    if any(len(i) > MAX_INSTRUCTION_CHARS for i in instructions):
        raise Invalid(f"Each instruction must be at most {MAX_INSTRUCTION_CHARS} characters.", code="instruction_too_long")

    if body.mode == "agent":
        with st.db.read() as conn:
            projects_repo.get_project(conn, project_id)
        outcomes = [st.planning.agent_outcome(i) for i in instructions]
    else:
        ctx = _plan_context(st, project_id)
        outcomes = st.planning.plan_many(instructions, ctx)  # outside the write transaction: may call a model

    with st.db.tx() as conn:
        projects_repo.get_project(conn, project_id)
        user_msg = projects_repo.add_message(conn, project_id, "user", body.text if body.text is not None
                                             else "\n".join(f"- {i}" for i in instructions))
        created = [tests_repo.create_test_case(conn, project_id, text, o, body.mode)
                   for text, o in zip(instructions, outcomes)]
        ready = sum(o.status == "ready" for o in outcomes)
        kind = "agent test" if body.mode == "agent" else "test"
        summary = f"Added {len(created)} {kind}{'s' if len(created) != 1 else ''}; {ready} ready to run."
        summary += "".join(_outcome_note(t["title"], o) for t, o in zip(created, outcomes))
        system_msg = projects_repo.add_message(conn, project_id, "system", summary)
    return {"tests": [s.test_case(t, with_plan=True) for t in created],
            "messages": [s.message(user_msg), s.message(system_msg)]}


@router.get("/projects/{project_id}/tests/{test_id}")
def get_test(project_id: str, test_id: str, st: AppState = Depends(get_state)) -> dict[str, Any]:
    with st.db.read() as conn:
        projects_repo.get_project(conn, project_id)
        return s.test_case(tests_repo.get_test_case(conn, project_id, test_id), with_plan=True)


@router.put("/projects/{project_id}/tests/{test_id}")
def update_test(project_id: str, test_id: str, body: s.TestUpdateIn, st: AppState = Depends(get_state)) -> dict[str, Any]:
    """Editing creates a new immutable revision; earlier runs keep pointing at theirs."""
    instruction = " ".join(body.instruction.split())
    with st.db.read() as conn:
        current = tests_repo.get_test_case(conn, project_id, test_id)
    mode = body.mode or current["mode"]
    outcome = (st.planning.agent_outcome(instruction) if mode == "agent"
               else st.planning.plan(instruction, _plan_context(st, project_id)))
    with st.db.tx() as conn:
        tc = tests_repo.add_revision(conn, project_id, test_id, instruction, outcome, mode)
        note = _outcome_note(tc["title"], outcome)
        projects_repo.add_message(conn, project_id, "system",
                                  f'Updated "{tc["title"]}" (revision {tc["revision_number"]}).{note}')
    return s.test_case(tc, with_plan=True)


@router.delete("/projects/{project_id}/tests/{test_id}")
def archive_test(project_id: str, test_id: str, st: AppState = Depends(get_state)) -> dict[str, Any]:
    """Archives (hides) the test. Its revisions and run history are preserved."""
    with st.db.tx() as conn:
        return s.test_case(tests_repo.archive_test_case(conn, project_id, test_id))


@router.get("/projects/{project_id}/tests/{test_id}/memory")
def get_test_memory(project_id: str, test_id: str, st: AppState = Depends(get_state)) -> dict[str, Any]:
    """What the next agent run of this test will start from: the path that worked last time,
    agent-chosen values, notes and information that was missing."""
    with st.db.read() as conn:
        projects_repo.get_project(conn, project_id)
        tests_repo.get_test_case(conn, project_id, test_id)
        return agent_memory.load(conn, project_id, test_id).as_api()


@router.get("/projects/{project_id}/tests/{test_id}/revisions")
def list_revisions(project_id: str, test_id: str, limit: int | None = None, cursor: str | None = None,
                   order: str | None = None, st: AppState = Depends(get_state)) -> dict[str, Any]:
    with st.db.read() as conn:
        projects_repo.get_project(conn, project_id)
        page = parse_page(limit, cursor, order, f"revisions:{project_id}:{test_id}")
        items, nxt = tests_repo.list_revisions(conn, project_id, test_id, page)
    return s.listing([s.revision(r) for r in items], nxt)


@router.get("/projects/{project_id}/tests/{test_id}/revisions/{revision_id}")
def get_revision(project_id: str, test_id: str, revision_id: str, st: AppState = Depends(get_state)) -> dict[str, Any]:
    with st.db.read() as conn:
        projects_repo.get_project(conn, project_id)
        rev = tests_repo.get_revision(conn, project_id, revision_id)
    if rev["test_case_id"] != test_id:
        raise NotFound("Revision not found for this test.", code="revision_not_found")
    return s.revision(rev)
