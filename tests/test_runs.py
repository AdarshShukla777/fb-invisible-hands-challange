"""Duplicate-execution prevention, claims, cancellation and interrupted runs."""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor

from app.execution.finalize import recover_interrupted
from app.repo import runs as runs_repo

READY = 'Go to /login and verify the page shows "Sign in {}"'


def setup(api, n=1):
    p = api.project("P", "http://app.test")
    return p, api.add_tests(p["id"], *[READY.format(i) for i in range(n)])


def test_same_idempotency_key_returns_the_same_run(api):
    p, tests = setup(api)
    first = api.run(p["id"], key="click-1")
    again = api.run(p["id"], key="click-1", expect=200)
    assert again["replayed"] is True and again["run"]["id"] == first["run"]["id"]
    assert len(api.call("GET", f"/projects/{p['id']}/runs")["items"]) == 1
    r = api.client.post(f"/api/projects/{p['id']}/runs", json={"testIds": [tests[0]["id"]]},
                        headers={"Idempotency-Key": "click-1"})
    assert r.status_code == 422 and r.json()["error"]["code"] == "idempotency_key_reused"


def test_active_test_is_not_queued_twice(api):
    p, tests = setup(api, 2)
    api.run(p["id"], [tests[0]["id"]])
    r = api.client.post(f"/api/projects/{p['id']}/runs", json={"testIds": [tests[0]["id"]]})
    assert r.status_code == 409 and r.json()["error"]["code"] == "already_active"
    both = api.run(p["id"])  # run-all: the busy test is skipped, the other one runs
    assert [s["reason"] for s in both["skipped"]] == ["already_active"]
    assert [t["testCaseId"] for t in both["run"]["testRuns"]] == [tests[1]["id"]]


def test_concurrent_requests_create_one_execution(api):
    p, tests = setup(api)
    def post(_):
        return api.client.post(f"/api/projects/{p['id']}/runs", json={"testIds": [tests[0]["id"]]}).status_code
    with ThreadPoolExecutor(8) as pool:
        codes = sorted(pool.map(post, range(8)))
    assert codes.count(201) == 1 and codes.count(409) == 7
    assert len(api.call("GET", f"/projects/{p['id']}/test-runs")["items"]) == 1


def test_duplicate_ids_in_one_request_are_collapsed(api):
    p, tests = setup(api)
    run = api.run(p["id"], [tests[0]["id"], tests[0]["id"]])["run"]
    assert len(run["testRuns"]) == 1


def test_a_test_run_is_claimed_once(api):
    p, _ = setup(api)
    api.run(p["id"])
    with api.state.db.tx() as conn:
        first = runs_repo.claim_next(conn, "w1")
    with api.state.db.tx() as conn:
        second = runs_repo.claim_next(conn, "w2")
    assert first is not None and second is None


def test_not_ready_tests_are_skipped_with_reason(api):
    p = api.project("P", "http://app.test")
    vague = api.add_tests(p["id"], "Log in with valid credentials")[0]
    r = api.client.post(f"/api/projects/{p['id']}/runs", json={"testIds": [vague["id"]]})
    assert r.status_code == 422 and r.json()["error"]["details"]["skipped"][0]["reason"] == "needs_clarification"


def test_cancel_queued_and_running(api):
    p, tests = setup(api, 2)
    run = api.run(p["id"])["run"]
    with api.state.db.tx() as conn:
        runs_repo.claim_next(conn, "w1")  # first test is now running
    detail = api.call("POST", f"/projects/{p['id']}/runs/{run['id']}/cancel")
    statuses = [t["status"] for t in detail["testRuns"]]
    assert statuses == ["running", "cancelled"] and detail["testRuns"][0]["cancelRequested"] is True
    assert detail["status"] == "running"


def test_interrupted_runs_become_errors_and_queued_runs_survive(api):
    p, tests = setup(api, 2)
    run = api.run(p["id"])["run"]
    with api.state.db.tx() as conn:
        runs_repo.claim_next(conn, "w-dead")
    assert recover_interrupted(api.state.db, None) == 1  # what a restarted server does
    detail = api.run_detail(p["id"], run["id"])
    running, queued = detail["testRuns"]
    assert running["status"] == "error" and running["errorKind"] == "interrupted"
    assert running["errorSource"] == "infrastructure"
    assert queued["status"] == "queued"  # never started, safe to execute later
    assert detail["reports"][0]["summary"].startswith("Error (infrastructure / interrupted)")


def test_events_endpoint_is_incremental(api):
    p, _ = setup(api)
    run = api.run(p["id"])["run"]
    first = api.call("GET", f"/projects/{p['id']}/runs/{run['id']}/events")
    assert first["status"] == "queued" and first["items"][0]["message"].startswith("Queued 1 test")
    again = api.call("GET", f"/projects/{p['id']}/runs/{run['id']}/events", params={"after": first["lastId"]})
    assert again["items"] == [] and again["lastId"] == first["lastId"]
