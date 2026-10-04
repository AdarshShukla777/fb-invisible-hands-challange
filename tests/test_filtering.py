"""Filtering and pagination: validation, determinism, bounds, empty results."""
from __future__ import annotations

import pytest

READY = 'Go to /login and verify the page shows "Sign in {}"'
VAGUE = "Log in with valid credentials"


@pytest.fixture
def project(api):
    p = api.project("P", "http://app.test")
    api.add_tests(p["id"], *[READY.format(i) for i in range(7)], VAGUE)
    return p


def collect(api, path, **params):
    items, cursor, pages = [], None, 0
    while True:
        q = {**params, **({"cursor": cursor} if cursor else {})}
        page = api.call("GET", path, params=q)
        items += page["items"]
        pages += 1
        cursor = page["nextCursor"]
        if not cursor:
            return items, pages


def test_pagination_is_complete_deterministic_and_bounded(api, project):
    path = f"/projects/{project['id']}/tests"
    everything = api.call("GET", path, params={"limit": 100})["items"]
    paged, pages = collect(api, path, limit=3)
    assert [t["id"] for t in paged] == [t["id"] for t in everything] and pages == 3
    assert len({t["id"] for t in paged}) == 8  # no duplicates across pages
    asc, _ = collect(api, path, limit=3, order="asc")
    assert [t["id"] for t in asc] == [t["id"] for t in reversed(everything)]
    assert [t["title"] for t in asc][:2] == [READY.format(0), READY.format(1)]  # insertion order


@pytest.mark.parametrize("params", [
    {"limit": 0}, {"limit": 101}, {"order": "sideways"}, {"cursor": "garbage"}, {"status": "passed,exploded"},
    {"planStatus": "maybe"}, {"sort": "title"}, {"q": "x" * 201},
])
def test_invalid_filters_are_rejected(api, project, params):
    r = api.client.get(f"/api/projects/{project['id']}/tests", params=params)
    assert r.status_code == 422, r.text


def test_cursor_is_bound_to_listing_order_and_project(api, project):
    other = api.project("Q", "http://q.test")
    cursor = api.call("GET", f"/projects/{project['id']}/tests", params={"limit": 2})["nextCursor"]
    assert api.client.get(f"/api/projects/{project['id']}/tests", params={"cursor": cursor, "order": "asc"}).status_code == 422
    assert api.client.get(f"/api/projects/{other['id']}/tests", params={"cursor": cursor}).status_code == 422
    assert api.client.get(f"/api/projects/{project['id']}/runs", params={"cursor": cursor}).status_code == 422


def test_filter_combinations(api, project):
    path = f"/projects/{project['id']}/tests"
    assert len(api.call("GET", path, params={"planStatus": "ready"})["items"]) == 7
    assert [t["instruction"] for t in api.call("GET", path, params={"planStatus": "needs_clarification"})["items"]] == [VAGUE]
    assert len(api.call("GET", path, params={"status": "not_run", "q": "Sign in 3"})["items"]) == 1
    assert api.call("GET", path, params={"q": "100%_match"})["items"] == []  # LIKE wildcards are escaped
    assert api.call("GET", path, params={"status": "passed"}) == {"items": [], "nextCursor": None}


def test_run_history_filters(api, project):
    pid = project["id"]
    tests = api.call("GET", f"/projects/{pid}/tests", params={"planStatus": "ready", "order": "asc"})["items"]
    run = api.run(pid, [tests[0]["id"], tests[1]["id"]])["run"]
    api.call("POST", f"/projects/{pid}/runs/{run['id']}/cancel")
    assert len(api.call("GET", f"/projects/{pid}/runs", params={"testId": tests[0]["id"]})["items"]) == 1
    assert api.call("GET", f"/projects/{pid}/runs", params={"testId": tests[2]["id"]})["items"] == []
    assert len(api.call("GET", f"/projects/{pid}/test-runs", params={"status": "cancelled"})["items"]) == 2
    assert api.call("GET", f"/projects/{pid}/test-runs", params={"status": "passed", "healed": "true"})["items"] == []
    r = api.client.get(f"/api/projects/{pid}/runs", params={"since": "2030-01-01T00:00:00Z", "until": "2020-01-01"})
    assert r.status_code == 422
    assert api.client.get(f"/api/projects/{pid}/runs", params={"since": "yesterday"}).status_code == 422
    assert api.call("GET", f"/projects/{pid}/runs", params={"since": "2999-01-01T00:00:00Z"})["items"] == []
    other_rev = api.call("GET", f"/projects/{pid}/tests/{tests[1]['id']}")["revision"]["id"]
    r = api.client.get(f"/api/projects/{pid}/test-runs", params={"testId": tests[0]["id"], "revisionId": other_rev})
    assert r.status_code == 422  # revision does not belong to that test


def test_empty_project_lists(api):
    p = api.project("Empty", "http://e.test")
    for path in ("tests", "runs", "test-runs", "pages", "locators", "healing-attempts", "messages"):
        assert api.call("GET", f"/projects/{p['id']}/{path}") == {"items": [], "nextCursor": None}
    assert api.client.post(f"/api/projects/{p['id']}/runs", json={}).status_code == 422
