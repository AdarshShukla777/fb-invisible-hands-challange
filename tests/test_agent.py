"""Agent mode (browser-use style) with per-test memory, against the demo HRM app.

The model is replaced by ScriptedAgent, which reads the rendered prompt (page, elements,
steps so far, notes) and answers like an agent would. These tests cover the runner's
mechanics: replay, assist, deviations, memory, redaction and guardrails."""
from __future__ import annotations

import re
from dataclasses import replace
from typing import Any

import pytest
from fastapi.testclient import TestClient

from app.api.app import create_app
from conftest import Api

GOAL = "login and click on admin tab and edit name of first user on the admin section"


class ScriptedAgent:
    model = "scripted-agent"

    def __init__(self, policy=None):
        self.calls, self.prompts = 0, []
        self.policy = policy or hrm_policy

    def complete_json(self, *, system: str, user: str, schema: dict, timeout_s: float) -> dict:
        self.calls += 1
        self.prompts.append(user)
        return {"evaluation": None, "thought": "", "element": None, "value": None, "count": None, "expect": None,
                "value_source": None, "outcome": None, "summary": None, "missing": [], "notes": [],
                **self.policy(Prompt(user))}


class Prompt:
    def __init__(self, text: str):
        self.text = text
        self.path = re.search(r"^URL: (.*)$", text, re.M).group(1)
        self.goal = re.search(r"^GOAL: (.*)$", text, re.M).group(1)
        self.history = (text.split("STEPS SO FAR IN THIS RUN:")[1].split("CURRENT PAGE:")[0].split("\nNOTE:")[0]
                        .split("\nLAST CHANGE:")[0])
        note = re.search(r"^NOTE: (.*)$", text, re.M)
        self.note = note.group(1) if note else ""
        self.last_change = text.split("\nLAST CHANGE:")[1].split("CURRENT PAGE:")[0] if "\nLAST CHANGE:" in text else ""
        self.elements = re.findall(r"^\s+\[(\d+)\] (.*)$", text, re.M)

    def find(self, *needles: str) -> int:
        for idx, line in self.elements:
            if all(n in line for n in needles):
                return int(idx)
        raise AssertionError(f"no element with {needles} in {self.elements}")

    def done_before(self, text: str) -> bool:
        return text in self.history


def hrm_policy(p: Prompt) -> dict[str, Any]:
    if p.path.endswith("/hrm/login"):
        if not p.done_before('"Username"'):
            return {"action": "fill", "element": p.find('"Username"'), "value": "Admin", "value_source": "page"}
        if not p.done_before('"Password"'):
            secret = re.search(r"\{\{secret:\w+\}\}", p.goal)
            return {"action": "fill", "element": p.find("(password)"),
                    "value": secret.group(0) if secret else "admin123",
                    "value_source": "instruction" if secret else "page"}
        return {"action": "click", "element": p.find('button "Login"')}
    if p.path.endswith("/hrm/dashboard"):
        link = next(i for i, line in p.elements if line.startswith('link "Admin'))
        return {"action": "click", "element": int(link)}
    if p.path.endswith("/hrm/admin/edit"):
        if not p.done_before('"Employee Name"'):
            return {"action": "fill", "element": p.find('"Employee Name"'), "value": "Jane Tester",
                    "value_source": "agent_chosen"}
        return {"action": "click", "element": p.find('button "Save"')}
    if p.path.endswith("/hrm/admin"):
        if not p.done_before('"Employee Name"'):
            return {"action": "click", "element": p.find("icon=bi-pencil", "row 1:")}
        if not p.done_before("Verify"):
            return {"action": "verify_text", "value": "Jane Tester"}
        return {"action": "done", "outcome": "passed", "summary": "Edited the first user's name",
                "notes": ["Credentials are printed on the login page"]}
    return {"action": "done", "outcome": "blocked", "summary": f"Unexpected page {p.path}"}


@pytest.fixture
def agent_api(settings):
    agent = ScriptedAgent()
    app = create_app(replace(settings, action_timeout_ms=1500), llm=agent)
    with TestClient(app) as client:
        api = Api(client)
        api.agent = agent
        yield api


def agent_test(api: Api, site, goal: str = GOAL) -> tuple[str, str]:
    p = api.project("HRM", site.base_url + "hrm/login")
    t = api.call("POST", f"/projects/{p['id']}/tests", 201, json={"instructions": [goal], "mode": "agent"})["tests"][0]
    assert t["mode"] == "agent" and t["planStatus"] == "ready", t
    return p["id"], t["id"]


def run_once(api: Api, pid: str, tid: str) -> tuple[dict, int]:
    before = api.agent.calls
    run = api.run(pid, [tid])["run"]
    api.drain()
    return api.run_detail(pid, run["id"])["reports"][0], api.agent.calls - before


def test_first_run_explores_second_run_replays(agent_api, site):
    pid, tid = agent_test(agent_api, site)
    first, calls = run_once(agent_api, pid, tid)
    assert first["status"] == "passed" and first["verdictSource"] == "agent", first["summary"]
    assert {s["origin"] for s in first["steps"]} == {"start", "agent"} and calls == 9
    assert first["agent"]["verified"] is True
    assert first["agent"]["agentChosenValues"] == [{"field": 'textbox "Employee Name"', "value": "Jane Tester"}]

    memory = agent_api.call("GET", f"/projects/{pid}/tests/{tid}/memory")
    assert [s["description"] for s in memory["steps"]][:3] == [
        'Enter "Admin" into textbox "Username"', 'Enter a value into textbox "Password"', 'Click button "Login"']
    password_step = memory["steps"][1]
    assert password_step["sensitive"] is True and password_step["value"] is None
    assert memory["notes"] == ["Credentials are printed on the login page"]

    second, calls = run_once(agent_api, pid, tid)
    assert second["status"] == "passed" and second["verdictSource"] == "replay", second["summary"]
    assert calls == 1  # only the password, which memory deliberately does not store
    assert [s["origin"] for s in second["steps"]].count("agent_assist") == 1
    assert second["agent"]["replay"]["deviations"] == []


def test_ui_change_is_reported_then_learned(agent_api, site):
    pid, tid = agent_test(agent_api, site)
    run_once(agent_api, pid, tid)
    site.variant = "renamed"  # nav "Administration", edit icon bi-pencil-square
    changed, calls = run_once(agent_api, pid, tid)
    assert changed["status"] == "passed", (changed["summary"], changed["agent"]["replay"], [(x["description"], x["status"], x.get("origin"), x.get("errorMessage")) for x in changed["steps"]])
    deviations = changed["agent"]["replay"]["deviations"]
    assert [(d["description"], d["resolvedBy"]) for d in deviations] == [
        ('Click link "Admin"', 'Click link "Administration"'),
        ("Click button (icon bi-pencil-fill) in row 1", "Click button (icon bi-pencil-square) in row 1"),
    ]
    assert "Differed from the last passing run at 2 step(s)" in changed["summary"]
    learned, calls = run_once(agent_api, pid, tid)
    assert learned["agent"]["replay"]["deviations"] == [] and calls == 1


def test_secret_placeholder_makes_replay_model_free(agent_api, site, monkeypatch):
    monkeypatch.setenv("UITEST_SECRET_HRM_PASSWORD", "admin123")
    pid, tid = agent_test(agent_api, site, GOAL + " using password {{secret:HRM_PASSWORD}}")
    assert run_once(agent_api, pid, tid)[0]["status"] == "passed"
    replayed, calls = run_once(agent_api, pid, tid)
    assert replayed["verdictSource"] == "replay" and calls == 0


def test_credentials_never_reach_storage(agent_api, site):
    pid, tid = agent_test(agent_api, site)
    run_once(agent_api, pid, tid)
    run_once(agent_api, pid, tid)
    with agent_api.state.db.read() as conn:
        for (table,) in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'").fetchall():
            for row in conn.execute(f"SELECT * FROM {table}").fetchall():
                assert "admin123" not in str(tuple(row)), f"password stored in {table}"


def test_blocked_run_records_what_was_missing(settings, site):
    agent = ScriptedAgent(lambda p: {"action": "done", "outcome": "blocked", "summary": "No credentials given",
                                     "missing": ["Login credentials for the admin account"]})
    with TestClient(create_app(settings, llm=agent)) as client:
        api = Api(client)
        api.agent = agent
        pid, tid = agent_test(api, site)
        report, _ = run_once(api, pid, tid)
        assert report["status"] == "error" and report["errorKind"] == "agent_blocked"
        assert report["errorSource"] == "agent" and "Missing: Login credentials" in report["summary"]
        assert api.call("GET", f"/projects/{pid}/tests/{tid}/memory")["missing"] == ["Login credentials for the admin account"]
        run_once(api, pid, tid)
        assert "Information that was missing before:" in agent.prompts[-1]
        assert "Most recent problem: blocked: No credentials given" in agent.prompts[-1]


def test_repeating_the_same_action_is_stopped(settings, site):
    agent = ScriptedAgent(lambda p: {"action": "click", "element": p.find('button "Login"')})
    with TestClient(create_app(settings, llm=agent)) as client:
        api = Api(client)
        api.agent = agent
        pid, tid = agent_test(api, site)
        report, calls = run_once(api, pid, tid)
        assert report["status"] == "error" and report["errorKind"] == "agent_stuck" and calls == 3


def test_step_limit(settings, site):
    agent = ScriptedAgent(lambda p: {"action": "scroll"})
    with TestClient(create_app(replace(settings, agent_max_steps=4), llm=agent)) as client:
        api = Api(client)
        api.agent = agent
        pid, tid = agent_test(api, site)
        report, calls = run_once(api, pid, tid)
        assert report["errorKind"] == "agent_step_limit" and calls == 4


def test_a_pass_without_any_check_is_not_accepted(settings, site):
    agent = ScriptedAgent(lambda p: {"action": "done", "outcome": "passed", "summary": "Looks fine"})
    with TestClient(create_app(settings, llm=agent)) as client:
        api = Api(client)
        api.agent = agent
        pid, tid = agent_test(api, site)
        report, calls = run_once(api, pid, tid)
        assert calls == 3 and "No check has passed" in agent.prompts[1]  # pushed back twice, then refused
        assert report["status"] == "error" and report["errorKind"] == "agent_unverified"
        assert report["errorSource"] == "agent" and "No verdict on application behavior" in report["summary"]


def test_agent_mode_needs_a_model(api, site):
    p = api.project("HRM", site.base_url + "hrm/login")
    t = api.call("POST", f"/projects/{p['id']}/tests", 201, json={"instructions": [GOAL], "mode": "agent"})["tests"][0]
    assert t["planStatus"] == "invalid" and t["issues"][0]["code"] == "agent_unavailable"


def test_memory_is_project_scoped(agent_api, site):
    pid, tid = agent_test(agent_api, site)
    other = agent_api.project("Other", site.base_url)
    assert agent_api.client.get(f"/api/projects/{other['id']}/tests/{tid}/memory").status_code == 404


def test_strict_test_needing_clarification_can_be_switched_to_agent_mode(agent_api, site):
    p = agent_api.project("HRM", site.base_url + "hrm/login")
    t = agent_api.add_tests(p["id"], GOAL)[0]  # strict mode: the rule planner cannot parse it
    assert t["mode"] == "plan" and t["planStatus"] == "needs_clarification"
    r = agent_api.client.post(f"/api/projects/{p['id']}/runs", json={})
    assert r.status_code == 422 and "switch it to agent mode" in r.json()["error"]["message"]
    switched = agent_api.call("PUT", f"/projects/{p['id']}/tests/{t['id']}", json={"instruction": GOAL, "mode": "agent"})
    assert switched["mode"] == "agent" and switched["planStatus"] == "ready" and switched["revision"]["number"] == 2
    report, _ = run_once(agent_api, p["id"], t["id"])
    assert report["status"] == "passed" and report["revision"]["mode"] == "agent"


def test_agent_run_with_visual_overlay(settings, site):
    """Pointer, target box and element boxes are drawn but never change what the agent sees or does."""
    agent = ScriptedAgent()
    with TestClient(create_app(replace(settings, highlight="on", slow_mo_ms=0), llm=agent)) as client:
        api = Api(client)
        api.agent = agent
        pid, tid = agent_test(api, site)
        report, calls = run_once(api, pid, tid)
        assert report["status"] == "passed" and calls == 9, report["summary"]
        assert not any("Agent:" in p or "Verified:" in p for p in agent.prompts)  # overlay text never leaks


def test_settling_ignores_third_party_and_background_traffic():
    from app.execution.executor import NetworkQuiet

    class R:  # stand-in for a Playwright request (hashable, like the real one)
        def __init__(self, resource_type: str, url: str):
            self.resource_type, self.url = resource_type, url

    q = NetworkQuiet.__new__(NetworkQuiet)
    q.pending, q.last_activity, q.origin = set(), 0.0, "http://127.0.0.1:4010"
    for r in (R(resource_type="xhr", url="https://mt0.google.com/vt/x=1"),        # map tiles
              R(resource_type="script", url="http://127.0.0.1:4010/cesium/w.js"),  # web workers
              R(resource_type="image", url="http://127.0.0.1:4010/a.png")):
        q._started(r)
    assert q.inflight == 0
    api_call = R(resource_type="fetch", url="http://127.0.0.1:4010/api/health")
    q._started(api_call)
    assert q.inflight == 1
    q._ended(api_call)
    assert q.inflight == 0


def test_text_check_spans_adjacent_elements():
    from playwright.sync_api import sync_playwright

    from app.execution.executor import text_visible

    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page()
        page.set_content("<div><b>73</b><span>warning</span><p>Overall health</p><p hidden>Secret text</p></div>")
        assert text_visible(page, "73 warning Overall health") and text_visible(page, "overall HEALTH")
        assert not text_visible(page, "Secret text") and not text_visible(page, "74 warning")
        browser.close()
