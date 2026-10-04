"""Bugs found by test runs: recorded once per distinct bug, counted when found again, marked fixed
when the same check passes, reopened as a regression, dismissed by the tester - and remembered by
the agent in later runs of the test."""
from __future__ import annotations

from dataclasses import replace

from fastapi.testclient import TestClient

from app.api.app import create_app
from conftest import Api
from test_agent import ScriptedAgent, run_once
from test_execution import run_one
from test_verification import GOAL, diligent


def bugs(api: Api, pid: str, **params) -> dict:
    return api.call("GET", f"/projects/{pid}/bugs", params=params)


def test_bug_lifecycle_in_agent_mode(settings, site):
    agent = ScriptedAgent(diligent)
    with TestClient(create_app(replace(settings, action_timeout_ms=1500), llm=agent)) as client:
        api = Api(client)
        api.agent = agent
        pid = api.project("Shop", site.base_url + "orders")["id"]
        tid = api.call("POST", f"/projects/{pid}/tests", 201, json={"instructions": [GOAL], "mode": "agent"})["tests"][0]["id"]

        site.variant = "filterbug"
        first, _ = run_once(api, pid, tid)
        assert first["status"] == "failed"
        listed = bugs(api, pid)
        assert listed["counts"] == {"open": 1, "fixed": 0, "dismissed": 0}
        [bug] = listed["items"]
        assert bug["status"] == "open" and bug["source"] == "check" and bug["occurrences"] == 1
        assert bug["title"] == "A filter lists orders of another status"  # the agent's own words
        assert bug["action"] == 'Click button "Cancelled"'
        assert bug["check"] == 'Check that every list item contains "cancelled" and there are 3 item(s)'
        assert bug["expected"] == 'every item contains "cancelled"; 3 item(s)'
        assert "Order #1005 Returned (refund issued)" in bug["actual"] and bug["lastTestRunId"] == first["testRunId"]
        assert bug["testTitle"] and bug["screenshotUrl"]

        second, _ = run_once(api, pid, tid)  # still broken: the same bug, seen twice, known to the agent
        assert second["status"] == "failed"
        assert "Bugs earlier runs found" in agent.prompts[-1] and "A filter lists orders" in agent.prompts[-1]
        [bug] = bugs(api, pid)["items"]
        assert bug["occurrences"] == 2 and bug["lastTestRunId"] == second["testRunId"]

        site.variant = "original"  # the fix ships
        fixed, _ = run_once(api, pid, tid)
        assert fixed["status"] == "passed"
        [bug] = bugs(api, pid, status="fixed")["items"]
        assert bug["fixedTestRunId"] == fixed["testRunId"]

        site.variant = "filterbug"  # and comes back: a regression, caught by the replayed check
        again, _ = run_once(api, pid, tid)
        assert again["status"] == "failed" and again["verdictSource"] != "replay"
        [bug] = bugs(api, pid, status="open")["items"]
        assert bug["regressed"] is True and bug["occurrences"] == 3 and bug["fixedTestRunId"] is None

        # the tester decides it is not a bug: later runs are told so
        out = api.call("PATCH", f"/projects/{pid}/bugs/{bug['id']}",
                       json={"status": "dismissed", "note": "Refunds count as cancellations here"})
        assert out["status"] == "dismissed" and out["note"] == "Refunds count as cancellations here"
        memory = api.call("GET", f"/projects/{pid}/tests/{tid}/memory")
        assert memory["bugs"] == [{"id": bug["id"], "title": bug["title"], "status": "dismissed", "occurrences": 3}]
        run_once(api, pid, tid)
        assert "expected behaviour, not bugs" in agent.prompts[-1] and "Refunds count as cancellations" in agent.prompts[-1]
        assert bugs(api, pid)["counts"]["dismissed"] == 1  # found again, but it stays dismissed
        api.call("PATCH", f"/projects/{pid}/bugs/{bug['id']}", 422, json={"status": "wontfix"})


def test_failed_plan_step_is_a_bug_and_errors_are_not(api, site):
    p = api.project("Demo", site.base_url)
    t = api.add_tests(p["id"], 'Go to /dashboard and verify the page shows "Goodbye"')[0]
    run_one(api, p["id"], t["id"])
    run_one(api, p["id"], t["id"])
    [bug] = bugs(api, p["id"])["items"]
    assert bug["source"] == "step" and bug["occurrences"] == 2 and bug["expected"] == 'text "Goodbye" visible'
    assert bug["testId"] == t["id"] and bug["lastRunId"]
    api.call("GET", f"/projects/{p['id']}/bugs", 422, params={"status": "closed"})
    other = api.project("Other", site.base_url)
    assert bugs(api, other["id"])["items"] == []  # project-scoped
    assert api.client.patch(f"/api/projects/{other['id']}/bugs/{bug['id']}", json={"status": "fixed"}).status_code == 404
