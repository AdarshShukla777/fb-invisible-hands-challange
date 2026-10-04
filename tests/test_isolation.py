"""Cross-project isolation at the API and database layers."""
from __future__ import annotations

import sqlite3

import pytest

READY = 'Go to /login and verify the page shows "Sign in"'


@pytest.fixture
def two_projects(api):
    a = api.project("A", "http://a.test")
    b = api.project("B", "http://b.test")
    ta = api.add_tests(a["id"], READY)[0]
    tb = api.add_tests(b["id"], READY)[0]
    run_a = api.run(a["id"])["run"]
    return a, b, ta, tb, run_a


def test_lists_only_show_own_project_data(api, two_projects):
    a, b, ta, tb, run_a = two_projects
    assert [t["id"] for t in api.call("GET", f"/projects/{a['id']}/tests")["items"]] == [ta["id"]]
    assert [t["id"] for t in api.call("GET", f"/projects/{b['id']}/tests")["items"]] == [tb["id"]]
    assert api.call("GET", f"/projects/{b['id']}/runs")["items"] == []
    assert [m["projectId"] for m in api.call("GET", f"/projects/{b['id']}/messages")["items"]] == [b["id"], b["id"]]


@pytest.mark.parametrize("path", [
    "/projects/{b}/tests/{ta}", "/projects/{b}/tests/{ta}/revisions", "/projects/{b}/runs/{run_a}",
    "/projects/{b}/test-runs/{tr_a}", "/projects/{b}/test-runs/{tr_a}/report",
])
def test_foreign_ids_are_not_found(api, two_projects, path):
    a, b, ta, tb, run_a = two_projects
    url = path.format(b=b["id"], ta=ta["id"], run_a=run_a["id"], tr_a=run_a["testRuns"][0]["id"])
    r = api.client.get("/api" + url)
    assert r.status_code == 404, r.text


def test_foreign_ids_cannot_be_used_in_writes_or_filters(api, two_projects):
    a, b, ta, tb, run_a = two_projects
    assert api.client.post(f"/api/projects/{b['id']}/runs", json={"testIds": [ta["id"]]}).status_code == 404
    assert api.client.put(f"/api/projects/{b['id']}/tests/{ta['id']}", json={"instruction": READY}).status_code == 404
    assert api.client.delete(f"/api/projects/{b['id']}/tests/{ta['id']}").status_code == 404
    assert api.client.post(f"/api/projects/{b['id']}/runs/{run_a['id']}/cancel").status_code == 404
    assert api.client.get(f"/api/projects/{b['id']}/test-runs", params={"testId": ta["id"]}).status_code == 404
    assert api.client.get(f"/api/projects/{b['id']}/runs", params={"testId": ta["id"]}).status_code == 404
    # project A is untouched
    assert api.call("GET", f"/projects/{a['id']}/tests/{ta['id']}")["archived"] is False


def test_idempotency_keys_are_scoped_per_project(api, two_projects):
    a, b, ta, tb, run_a = two_projects
    first = api.run(b["id"], key="same-key")
    assert first["run"]["projectId"] == b["id"] and first["replayed"] is False


def test_database_rejects_cross_project_references(api, two_projects):
    a, b, ta, tb, run_a = two_projects
    with pytest.raises(sqlite3.IntegrityError):
        with api.state.db.tx() as conn:  # a run in project B pointing at A's test case
            conn.execute("INSERT INTO runs (id, project_id, status, request_hash, created_at) "
                         "VALUES ('r-x', ?, 'queued', 'h', 't')", (b["id"],))
            conn.execute("INSERT INTO test_runs (id, project_id, run_id, test_case_id, revision_id, position, status, "
                         "queued_at) VALUES ('tr-x', ?, 'r-x', ?, ?, 0, 'queued', 't')",
                         (b["id"], ta["id"], ta["revision"]["id"]))


def test_deleting_a_project_removes_only_its_data(api, two_projects):
    a, b, ta, tb, run_a = two_projects
    api.call("POST", f"/projects/{a['id']}/runs/{run_a['id']}/cancel")
    api.call("DELETE", f"/projects/{a['id']}", 204)
    assert api.client.get(f"/api/projects/{a['id']}").status_code == 404
    assert len(api.call("GET", f"/projects/{b['id']}/tests")["items"]) == 1
