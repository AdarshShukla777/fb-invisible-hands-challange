"""Planner + validator: concrete instructions become runnable plans; anything ambiguous
becomes a clarification instead of an invented step or assertion."""
from __future__ import annotations

from dataclasses import replace
from typing import Any

import pytest

from app.planning.llm import LLMError
from app.planning.rules import PlanContext
from app.planning.service import PlanningService
from app.planning.splitter import split_instructions

BASE = "http://app.test/"
CTX = PlanContext(base_url=BASE, known_pages=[("http://app.test/login", "Sign in")])


@pytest.fixture
def rules(settings):
    return PlanningService(settings, None)


def codes(outcome) -> set[str]:
    return {i["code"] for i in outcome.issues}


def test_concrete_instruction_becomes_ready_plan(rules):
    o = rules.plan('Go to /login, enter "ada@example.com" into the Email field, type {{secret:PASSWORD}} into '
                   'Password, click the "Log in" button, then verify the page shows "Welcome back" and the URL '
                   'contains /dashboard', CTX)
    assert o.status == "ready", o.issues
    actions = [s["action"] for s in o.plan["steps"]]
    assert actions == ["navigate", "fill", "fill", "click", "assert", "assert"]
    assert [s["assertion"]["type"] for s in o.plan["steps"] if s["action"] == "assert"] == ["text_visible", "url_contains"]
    # secret values are never echoed into descriptions
    assert "PASSWORD" in o.plan["steps"][2]["description"] and "{{" not in o.plan["steps"][2]["description"]


def test_missing_expected_outcome_requires_clarification(rules):
    o = rules.plan('Go to /login and click "Log in"', CTX)
    assert o.status == "needs_clarification"
    assert "missing_expected_outcome" in codes(o)


def test_vague_steps_are_not_guessed(rules):
    o = rules.plan("Log in with valid credentials and check the dashboard loads", CTX)
    assert o.status == "needs_clarification"
    assert {"unrecognized_step", "ambiguous_check"} <= codes(o)


def test_unquoted_expected_text_is_ambiguous(rules):
    o = rules.plan("Go to /login and verify I see a success message", CTX)
    assert o.status == "needs_clarification" and "vague_expectation" in codes(o)


def test_named_page_resolves_only_from_project_history(rules):
    assert rules.plan('Open the login page and verify "Sign in" is visible', CTX).plan["steps"][0]["url"] == "/login"
    o = rules.plan('Open the profile page and verify "Profile" is visible', CTX)
    assert o.status == "needs_clarification" and "unknown_page" in codes(o)


def test_cross_origin_navigation_is_rejected(rules):
    o = rules.plan('Go to https://evil.example.com and verify the page shows "x"', CTX)
    assert o.status == "invalid" and "cross_origin_navigation" in codes(o)


def test_split_instructions_matches_frontend_rules():
    assert split_instructions("- a\n- b\n  continued\n1) c") == ["a", "b continued", "c"]
    assert split_instructions("first test\n\nsecond\ntest") == ["first test", "second test"]
    assert split_instructions("   ") == []


class FakeLLM:
    model = "fake-model"

    def __init__(self, *responses: Any):
        self.responses = list(responses)
        self.calls = 0

    def complete_json(self, *, system, user, schema, timeout_s):
        self.calls += 1
        r = self.responses.pop(0)
        if isinstance(r, Exception):
            raise r
        return r


def _step(action, **kw):
    base = {"action": action, "target": None, "value": None, "url": None, "key": None, "assertion": None}
    return {**base, **kw}


def _target(**kw):
    base = {"description": "x", "role": None, "name": None, "label": None, "placeholder": None, "testid": None,
            "text": None, "css": None, "exact": False}
    return {**base, **kw}


def test_llm_plan_is_validated_and_invented_assertions_rejected(settings):
    invented = {"clarifications": [], "steps": [
        _step("navigate", url="/login"),
        _step("click", target=_target(role="button", name="Log in")),
        _step("assert", assertion={"type": "text_visible", "expected": "Welcome, Ada!", "target": None,
                                   "source": "log in works"}),
    ]}
    llm = FakeLLM(invented)
    svc = PlanningService(replace(settings, planner_mode="llm"), llm)
    o = svc.plan("Go to /login, click Log in and check that log in works", CTX)
    assert o.status == "needs_clarification"
    assert "ungrounded_assertion" in codes(o)
    assert o.planner == "llm:fake-model"


def test_llm_clarifications_are_returned_not_executed(settings):
    llm = FakeLLM({"clarifications": ["Which credentials should be used?"], "steps": []})
    svc = PlanningService(replace(settings, planner_mode="llm"), llm)
    o = svc.plan("Log in with valid credentials", CTX)
    assert o.status == "needs_clarification"
    assert o.issues[0]["message"] == "Which credentials should be used?"


def test_llm_repair_call_is_bounded_by_budget(settings):
    bad = {"clarifications": [], "steps": [_step("navigate", url="https://other.example/"),
                                           _step("assert", assertion={"type": "url_contains", "expected": "/x",
                                                                      "target": None, "source": "x"})]}
    llm = FakeLLM(bad, bad, bad)
    svc = PlanningService(replace(settings, planner_mode="llm", llm_max_calls_per_plan=2), llm)
    o = svc.plan("go to https://other.example/ and check /x", CTX)
    assert llm.calls == 2  # one plan + one repair, never more
    assert o.status == "invalid"


def test_auto_mode_uses_rules_first_and_llm_only_as_fallback(settings):
    llm = FakeLLM(LLMError("timeout", "no response"), LLMError("timeout", "no response"))
    svc = PlanningService(replace(settings, planner_mode="auto"), llm)
    ready = svc.plan('Go to /login and verify the page shows "Sign in"', CTX)
    assert ready.status == "ready" and ready.planner == "rules" and llm.calls == 0
    vague = svc.plan("Log in with valid credentials", CTX)
    assert llm.calls == 2 and vague.status == "needs_clarification"  # one retry, then the rules result
    assert "llm_planner_failed" in codes(vague)


def _plan(*steps):
    return {"steps": list(steps)}


def test_generated_test_data_is_accepted_and_can_be_checked():
    from app.planning.plan import validate_plan

    raw = _plan({"action": "navigate", "url": "/settings"},
                {"action": "fill", "value": "Nova Tester", "value_source": "generated", "target": {"label": "Nickname"}},
                {"action": "click", "target": {"role": "button", "name": "Save"}},
                {"action": "assert", "assertion": {"type": "text_visible", "expected": "Nova Tester"}})
    r = validate_plan(raw, base_url=BASE, instruction="On /settings enter a nickname, save and check it is shown",
                      max_steps=50)
    assert r.status == "ready", r.issues
    assert r.plan.steps[1].description == 'Enter "Nova Tester" (generated test data) into the "Nickname" field'


def test_generated_values_are_refused_for_credentials_and_dropdowns():
    from app.planning.plan import validate_plan

    raw = _plan({"action": "navigate", "url": "/login"},
                {"action": "fill", "value": "hunter2", "value_source": "generated", "target": {"label": "Password"}},
                {"action": "select", "value": "Dark", "value_source": "generated", "target": {"label": "Theme"}},
                {"action": "assert", "assertion": {"type": "url_contains", "expected": "/login"}})
    r = validate_plan(raw, base_url=BASE, instruction="log in on /login and pick a theme", max_steps=50)
    assert [i.code for i in r.issues] == ["credentials_required", "ungrounded_value"]


def test_invented_expectations_are_still_refused():
    from app.planning.plan import validate_plan

    raw = _plan({"action": "navigate", "url": "/"},
                {"action": "assert", "assertion": {"type": "text_visible", "expected": "Dashboard"}})
    r = validate_plan(raw, base_url=BASE, instruction="check the dashboard loads", max_steps=50)
    assert [i.code for i in r.issues] == ["ungrounded_assertion"]


def test_flat_llm_assert_fields_become_a_nested_assertion():
    from app.planning.llm_planner import _prune

    flat = {"action": "assert", "check": "text_visible", "expected": '"Saved"', "source": "shows Saved",
            "target": None, "value": None, "value_source": None, "url": None, "key": None}
    assert _prune(flat) == {"action": "assert", "assertion": {"type": "text_visible", "expected": "Saved",
                                                              "target": None, "source": "shows Saved"}}
