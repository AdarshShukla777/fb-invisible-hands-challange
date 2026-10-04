"""Editing a test or reviewing locators never changes earlier runs."""
from __future__ import annotations

import sqlite3

import pytest

V1 = 'Go to /login and verify the page shows "Sign in"'
V2 = 'Go to /dashboard and verify the page shows "Welcome back"'


def test_edit_creates_revision_and_old_runs_keep_their_revision(api):
    p = api.project("P", "http://app.test")
    t = api.add_tests(p["id"], V1)[0]
    run1 = api.run(p["id"], [t["id"]])["run"]
    api.call("POST", f"/projects/{p['id']}/runs/{run1['id']}/cancel")  # finalize without a browser

    edited = api.call("PUT", f"/projects/{p['id']}/tests/{t['id']}", json={"instruction": V2})
    assert edited["revision"]["number"] == 2 and edited["instruction"] == V2
    assert edited["status"] == "not_run"  # the new revision has no runs yet

    old = api.call("GET", f"/projects/{p['id']}/runs/{run1['id']}")
    tr = old["testRuns"][0]
    assert tr["revisionNumber"] == 1 and tr["instruction"] == V1
    report = old["reports"][0]
    assert report["revision"]["number"] == 1 and report["revision"]["instruction"] == V1
    assert report["steps"][0]["description"] == "Open /login"

    revisions = api.call("GET", f"/projects/{p['id']}/tests/{t['id']}/revisions")["items"]
    assert [r["number"] for r in revisions] == [2, 1]
    assert api.call("GET", f"/projects/{p['id']}/test-runs", params={"revisionId": revisions[1]["id"]})["items"][0]["id"] == tr["id"]

    run2 = api.run(p["id"], [t["id"]])["run"]
    assert run2["testRuns"][0]["revisionNumber"] == 2


def test_archiving_keeps_history(api):
    p = api.project("P", "http://app.test")
    t = api.add_tests(p["id"], V1)[0]
    run = api.run(p["id"])["run"]
    api.call("POST", f"/projects/{p['id']}/runs/{run['id']}/cancel")
    api.call("DELETE", f"/projects/{p['id']}/tests/{t['id']}")
    assert api.call("GET", f"/projects/{p['id']}/tests")["items"] == []
    assert len(api.call("GET", f"/projects/{p['id']}/tests", params={"includeArchived": "true"})["items"]) == 1
    assert api.call("GET", f"/projects/{p['id']}/runs/{run['id']}")["reports"][0]["status"] == "cancelled"
    assert api.client.post(f"/api/projects/{p['id']}/runs", json={}).status_code == 422  # nothing runnable


def test_history_rows_are_immutable(api):
    p = api.project("P", "http://app.test")
    api.add_tests(p["id"], V1)
    run = api.run(p["id"])["run"]
    api.call("POST", f"/projects/{p['id']}/runs/{run['id']}/cancel")
    with pytest.raises(sqlite3.IntegrityError):
        with api.state.db.tx() as conn:
            conn.execute("UPDATE test_case_revisions SET instruction = 'tampered'")
    with pytest.raises(sqlite3.IntegrityError):
        with api.state.db.tx() as conn:
            conn.execute("UPDATE test_runs SET status = 'passed'")
    with pytest.raises(sqlite3.IntegrityError):
        with api.state.db.tx() as conn:
            conn.execute("UPDATE reports SET outcome = 'passed'")
