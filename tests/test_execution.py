"""End-to-end execution in a real (headless) Chromium against the local demo site."""
from __future__ import annotations

import socket
from dataclasses import replace

import pytest
from fastapi.testclient import TestClient

from app.api.app import create_app
from conftest import Api

LOGIN = ('Go to /login, enter "ada@example.com" into the Email field, type "pw" into the Password field, '
         'click the "Log in" button, then verify the page shows "Welcome back" and the URL contains /dashboard')


def run_one(api: Api, pid: str, test_id: str) -> dict:
    run = api.run(pid, [test_id])["run"]
    api.drain()
    return api.run_detail(pid, run["id"])["reports"][0]


@pytest.fixture
def login(api, site):
    p = api.project("Demo", site.base_url)
    t = api.add_tests(p["id"], LOGIN)[0]
    assert t["planStatus"] == "ready", t["issues"]
    return p["id"], t["id"]


def test_passing_run_records_steps_evidence_and_sanitized_history(api, site, login):
    pid, tid = login
    report = run_one(api, pid, tid)
    assert report["status"] == "passed", report["summary"]
    assert [s["status"] for s in report["steps"]] == ["passed"] * 6
    assert report["assertions"] == {"total": 2, "passed": 2, "failed": 0, "notRun": 0}
    assert report["steps"][3]["locator"]["strategy"] == "role" and report["steps"][3]["locator"]["source"] == "declared"
    assert report["steps"][2]["description"] == 'Enter a value into the "Password" field'  # value not echoed
    assert report["timing"]["durationMs"] > 0 and any(a["kind"] == "screenshot" for a in report["artifacts"])
    shot = report["artifacts"][0]["url"]
    assert api.client.get(shot).headers["content-type"] == "image/png"

    pages = api.call("GET", f"/projects/{pid}/pages")["items"]
    urls = {p["url"] for p in pages}
    assert f"{site.base_url}dashboard?tab=overview" in urls  # session token dropped from identity
    assert "s3cr3t" not in str(api.call("GET", f"/projects/{pid}/test-runs/{report['testRunId']}"))
    login_page = next(p for p in pages if p["url"].endswith("/login"))
    elements = api.call("GET", f"/projects/{pid}/pages/{login_page['id']}")["latestVersion"]["elements"]
    assert any(e.get("testid") == "login-submit" for e in elements)
    assert "ada@example.com" not in str(elements)  # form values are never stored
    assert api.client.get(f"/api/projects/{pid}/test-runs/{report['testRunId']}/report").status_code == 200
    assert api.call("GET", f"/projects/{pid}/tests/{tid}")["status"] == "passed"


def test_assertion_failure_is_failed_with_expected_and_actual(api, site):
    p = api.project("Demo", site.base_url)
    t = api.add_tests(p["id"], 'Go to /dashboard and verify the page shows "Goodbye"')[0]
    report = run_one(api, p["id"], t["id"])
    assert report["status"] == "failed"
    assert report["errorSource"] == "application" and report["errorKind"] == "assertion_failed"
    f = report["failure"]
    assert f["expected"] == 'text "Goodbye" visible' and "not visible" in f["actual"]
    assert f["errorSource"] == "application"


def test_unreachable_app_is_an_infrastructure_error_not_a_failure(api):
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]  # closed port: nothing listens
    p = api.project("Down", f"http://127.0.0.1:{port}")
    t = api.add_tests(p["id"], 'Go to /login and verify the page shows "Sign in"')[0]
    report = run_one(api, p["id"], t["id"])
    assert report["status"] == "error"
    assert report["errorKind"] == "navigation_failed" and report["errorSource"] == "infrastructure"
    assert [s["status"] for s in report["steps"]] == ["error", "skipped"]
    assert "No verdict" in report["summary"]


def test_missing_secret_is_an_error(api, site):
    p = api.project("Demo", site.base_url)
    t = api.add_tests(p["id"], 'Go to /login, type {{secret:NOPE}} into the Password field and verify the page shows "Sign in"')[0]
    report = run_one(api, p["id"], t["id"])
    assert report["status"] == "error" and report["errorKind"] == "missing_secret"


def test_successful_heal_is_provisional_surfaced_and_reviewable(api, site, login):
    pid, tid = login
    assert run_one(api, pid, tid)["status"] == "passed"  # baseline builds page history

    site.variant = "renamed"
    report = run_one(api, pid, tid)
    assert report["status"] == "passed" and report["healedSteps"] == 1
    assert "healing" in report["summary"]
    step = report["steps"][3]
    assert step["healed"] is True and step["locator"]["source"] == "healed"
    [attempt] = report["healing"]
    assert attempt["outcome"] == "healed" and attempt["confidence"] >= 0.75
    assert attempt["evidence"]["history"]["used"] is True and attempt["evidence"]["screenshotUrl"]
    assert attempt["selected"]["element"]["name"] == "Sign in"
    # assertions were not touched
    assert [s["expected"] for s in report["steps"] if s["kind"] == "assertion"] == [
        'text "Welcome back" visible', 'URL containing "/dashboard"']

    [locator] = api.call("GET", f"/projects/{pid}/locators")["items"]
    assert locator["status"] == "provisional"
    assert api.call("GET", f"/projects/{pid}/test-runs", params={"healed": "true"})["items"][0]["id"] == report["testRunId"]

    api.call("POST", f"/projects/{pid}/locators/{locator['id']}/review", json={"decision": "validate"})
    validated_run = run_one(api, pid, tid)
    assert validated_run["status"] == "passed" and validated_run["healedSteps"] == 0
    assert validated_run["steps"][3]["locator"]["source"] == "validated_replacement"
    # the earlier report is an immutable snapshot
    assert api.call("GET", f"/projects/{pid}/test-runs/{report['testRunId']}/report")["healing"][0]["outcome"] == "healed"


def test_ambiguous_heal_stops_and_reports(api, site, login):
    pid, tid = login
    run_one(api, pid, tid)
    site.variant = "ambiguous"
    report = run_one(api, pid, tid)
    assert report["status"] == "error" and report["errorKind"] == "locator_ambiguous"
    assert report["errorSource"] == "agent"
    [attempt] = report["healing"]
    assert attempt["outcome"] == "ambiguous" and attempt["selected"] is None
    assert len([c for c in attempt["candidates"] if c["score"] >= 0.5]) >= 2
    assert api.call("GET", f"/projects/{pid}/locators")["items"] == []


def test_heal_without_confident_candidate_is_rejected(api, site, login):
    pid, tid = login
    run_one(api, pid, tid)
    site.variant = "removed"
    report = run_one(api, pid, tid)
    assert report["status"] == "error" and report["errorKind"] == "locator_not_found"
    [attempt] = report["healing"]
    assert attempt["outcome"] == "no_confident_candidate"
    assert all(c["score"] < 0.75 for c in attempt["candidates"])
    assert report["steps"][4]["status"] == "skipped"


def test_reviewer_rejection_prevents_reuse(api, site, login):
    pid, tid = login
    run_one(api, pid, tid)
    site.variant = "renamed"
    run_one(api, pid, tid)
    [locator] = api.call("GET", f"/projects/{pid}/locators")["items"]
    api.call("POST", f"/projects/{pid}/locators/{locator['id']}/review", json={"decision": "reject", "note": "wrong button"})
    report = run_one(api, pid, tid)
    assert report["status"] == "error" and report["healing"][0]["outcome"] == "no_confident_candidate"
    assert "rejected by a reviewer" in report["healing"][0]["reason"]
    r = api.client.post(f"/api/projects/{pid}/locators/{locator['id']}/review", json={"decision": "validate"})
    assert r.status_code == 409  # rejected replacements cannot silently come back


def test_stale_history_is_ignored_explicitly(settings, site):
    app = create_app(replace(settings, history_stale_days=0), llm=None)
    with TestClient(app) as client:
        api = Api(client)
        p = api.project("Demo", site.base_url)
        t = api.add_tests(p["id"], LOGIN)[0]
        run_one(api, p["id"], t["id"])
        site.variant = "renamed"
        report = run_one(api, p["id"], t["id"])
        history = report["healing"][0]["evidence"]["history"]
        assert history["available"] is True and history["stale"] is True and history["used"] is False
        assert report["status"] == "passed"  # still healed on name + role alone, without history


def test_actions_on_forms(api, site):
    p = api.project("Demo", site.base_url)
    t = api.add_tests(p["id"], 'Go to /settings, check the "Remember me" checkbox, select "Dark" from the Theme dropdown, '
                               'enter "Ada" into the Nickname field, click the "Save" button, then verify the page '
                               'shows "Saved dark for Ada"')[0]
    report = run_one(api, p["id"], t["id"])
    assert report["status"] == "passed", report["summary"]
