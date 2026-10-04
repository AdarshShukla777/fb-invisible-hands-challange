"""Result checking in agent mode: a pass needs evidence for every change the agent made.

Reproduces a real miss: on a list page whose status filter chips were wrong for two statuses
(both kept every item without the first status), the agent clicked every chip, verified only the
"Showing 8 of 8" text on the All view, and passed. /orders on the demo site has the same kind of
planted bug (variant "filterbug") on a list of plain <div> cards: 4 delivered, 3 cancelled,
1 returned order."""
from __future__ import annotations

from dataclasses import replace

import pytest
from fastapi.testclient import TestClient
from playwright.sync_api import sync_playwright

from app.agent.verification import Verification, diff, live_shapes, segments
from app.api.app import create_app
from app.execution.dom import LIST_ITEMS_JS, LIST_ITEMS_OF_JS, LISTS_JS
from conftest import Api
from test_agent import Prompt, ScriptedAgent, run_once

GOAL = ("Order status filters: each chip (Delivered, Cancelled, Returned) must list only orders with that status "
        "(4 delivered, 3 cancelled, 1 returned); All lists all 8.")
CHIPS = {"Delivered": ("delivered", 4), "Cancelled": ("cancelled", 3), "Returned": ("returned", 1)}


# -- the rules, without a browser ------------------------------------------------------------------

def page(*parts: str) -> str:
    return " | ".join(parts)


def effect(v: Verification, step: int, action: str, before: str, after: str, lists_before=None, lists_after=None,
           key=("button", "x", "", "", "", 0), keys=frozenset(), items=("a",)):
    return v.after_interaction(step=step, description=f"Click {step}", action=action, expect=None, key=key,
                               before_keys=keys, text_before=before, text_after=after,
                               lists_before=lists_before or {}, lists_after=lists_after or {},
                               items=lambda k: list(items))


def test_live_values_are_learned_and_left_out_of_effects():
    t0 = page("Server time", "12:00:01", "Showing 8 of 8 orders")
    t1 = page("Server time", "12:00:04", "Showing 8 of 8 orders")  # changed while the model was thinking
    t2 = page("Server time", "12:00:09", "Showing 3 of 8 orders")
    live = live_shapes(segments(t0), segments(t1))
    assert live == {"#"}
    change = diff(segments(t1), segments(t2), live)
    assert change.changed == [("Showing 8 of 8 orders", "Showing 3 of 8 orders")]
    assert diff(segments(t1), segments(page("Server time", "12:00:12", "Showing 8 of 8 orders")), live).empty


def test_text_that_was_already_visible_does_not_prove_a_change():
    v = Verification()
    v.start("Open /")
    effect(v, 1, "click", page("Saved: no", "Cancel"), page("Saved: yes", "Cancel"))
    why = v.record_check(kind="verify_text", description="Verify Cancel", value="Cancel", count=None, passed=True,
                         detail="visible", list_key=None)
    assert "already visible" in why and v.current.status == "unchecked"
    assert v.record_check(kind="verify_text", description="Verify saved", value="Saved: yes", count=None, passed=True,
                          detail="visible", list_key=None) is None
    assert v.current.status == "checked"


def test_a_list_changed_in_place_needs_check_items():
    v = Verification()
    before, after = {"main>ol": [8, 1]}, {"main>ol": [4, 2]}
    e = effect(v, 1, "click", page("Showing 8 of 8"), page("Showing 4 of 8"), before, after)
    assert e.needs_items and (e.list_before, e.list_after) == (8, 4)
    assert "check_items" in v.record_check(kind="verify_text", description="Verify count", value="Showing 4 of 8",
                                           count=None, passed=True, detail="visible", list_key=None)
    assert "not the list" in v.record_check(kind="check_items", description="c", value="x", count=None, passed=True,
                                            detail="ok", list_key="nav>ul")
    v.record_check(kind="check_items", description="c", value="cancelled", count=None, passed=True, detail="ok",
                   list_key="main>ol")
    assert v.current.status == "checked"
    # same count, other items (a filter swapped them): still a list change
    swapped = effect(v, 2, "click", page("Showing 4 of 8"), page("Showing 4 of 8", "x"), {"main>ol": [4, 2]},
                     {"main>ol": [4, 3]})
    assert swapped.needs_items


def test_changing_the_expected_numbers_is_not_a_correction():
    v = Verification()
    effect(v, 1, "click", page("a"), page("b"))
    v.record_check(kind="verify_text", description="Verify 3", value="Showing 3 of 8 orders", count=None,
                   passed=False, detail="not visible", list_key=None)
    v.record_check(kind="verify_text", description="Verify 4", value="Showing 4 of 8 orders", count=None,
                   passed=True, detail="visible", list_key=None)
    assert v.current.status == "failed"
    assert v.verdict()[:2] == ("failed", "check_failed")
    # a wording fix is a correction
    w = Verification()
    effect(w, 1, "click", page("a"), page("Profile updated"))
    w.record_check(kind="verify_text", description="v", value="Saved successfully", count=None, passed=False,
                   detail="not visible", list_key=None)
    w.record_check(kind="verify_text", description="v", value="Profile updated", count=None, passed=True,
                   detail="visible", list_key=None)
    assert w.current.status == "checked" and w.verdict() is None


def test_moving_on_without_checking_is_held_back_unless_the_action_revealed_the_next_element():
    v = Verification()
    before_keys = frozenset({("button", "Delivered", "", "", "", 0), ("button", "Cancelled", "", "", "", 0)})
    effect(v, 1, "click", page("x"), page("y"), key=("button", "Delivered", "", "", "", 0), keys=before_keys)
    assert "HELD BACK" in v.before_interaction(("button", "Cancelled", "", "", "", 0))
    assert "HELD BACK" in v.before_interaction(("button", "Cancelled", "", "", "", 0))
    assert v.before_interaction(("button", "Cancelled", "", "", "", 0)) is None  # insisted: allowed, stays unchecked
    assert v.current.status == "unchecked"
    effect(v, 2, "click", page("y"), page("z"), key=("tab", "History", "", "", "", 0), keys=before_keys)
    assert v.before_interaction(("button", "New chip", "", "", "", 0)) is None  # revealed by the tab
    assert v.current.status == "covered"
    assert v.verdict()[:2] == ("error", "agent_unverified")  # step 1 was never checked


def test_filter_results_that_contradict_the_filter_are_pointed_out():
    """The two facts that contradict an agent that assumes "returned orders count as cancelled"."""
    v = Verification()
    chip = lambda name: ("button", name, "", "", "", 0)  # noqa: E731
    shown = ["Order #1005 Returned (refund issued) $55", "Order #1009 Returned (refund issued) $20"]
    v.after_interaction(step=1, description='Click button "Cancelled"', action="click", expect=None, key=chip("Cancelled"),
                        before_keys=frozenset(), text_before=page("Showing 8 of 8"), text_after=page("Showing 2 of 8"),
                        lists_before={"ol": [8, 1]}, lists_after={"ol": [2, 2]}, items=lambda k: shown,
                        control="Cancelled")
    assert v.current.hints == ['none of the 2 listed item(s) mention "Cancelled": check what each item\'s own '
                               'status or label says before accepting this result']
    assert 'Note: none of the 2 listed item(s) mention "Cancelled"' in v.feedback()
    same = v.after_interaction(step=2, description='Click button "Returned"', action="click", expect=None,
                               key=chip("Returned"), before_keys=frozenset(), text_before=page("Showing 2 of 8"),
                               text_after=page("Showing 2 of 8"), lists_before={"ol": [2, 2]},
                               lists_after={"ol": [2, 2]}, items=lambda k: shown, control="Returned")
    assert same is None
    feedback = v.feedback()
    assert 'LAST ACTION: "Click button "Returned"" (step 2) made NO visible change.' in feedback
    assert 'the list still shows the same 2 item(s) as after "Click button "Cancelled""' in feedback
    # "All" is not a label items carry
    v.after_interaction(step=3, description='Click button "All"', action="click", expect=None, key=chip("All"),
                        before_keys=frozenset(), text_before=page("Showing 2 of 8"), text_after=page("Showing 8 of 8"),
                        lists_before={"ol": [2, 2]}, lists_after={"ol": [8, 1]}, items=lambda k: shown * 4,
                        control="All")
    assert v.current.hints == []


def test_list_items_are_read_per_list():
    with sync_playwright() as p:
        browser = p.chromium.launch()
        pg = browser.new_page()
        pg.set_content("""<ul id="nav"><li>Home</li><li>About</li></ul>
          <table><thead><tr><th>Name</th><th>Status</th></tr></thead>
            <tbody><tr><td>A</td><td>open</td></tr><tr><td>B</td><td>closed</td></tr>
                   <tr style="display:none"><td>C</td><td>hidden</td></tr></tbody></table>
          <div role="table"><div role="rowgroup"><div role="row"><span role="columnheader">User</span></div></div>
            <div role="rowgroup" id="rows"><div role="row"><span role="cell">Ann</span></div>
            <div role="row"><span role="cell">Bob</span></div></div></div>
          <section class="cards"><div class="card">One <button>Open</button></div><div class="card">Two</div></section>
          <div class="grid"><div class="tile">Ann</div><div class="tile">Bob</div>
            <div class="tile">Cy <i class="icon" aria-label="Archived"></i></div><button class="tile">More</button></div>
          <form class="f"><div class="row">Name <input></div><div class="row">Mail <input></div>
            <div class="row">Code <input></div></form>""")
        lists = pg.evaluate(LISTS_JS)
        # plain look-alike elements count as a list from 3 on; form rows and buttons do not
        assert {k: v[0] for k, v in lists.items()} == {"ul#nav": 2, "table>tbody": 2, "div>div#rows": 2, "div.grid": 3}
        assert pg.evaluate(LIST_ITEMS_JS, "div.grid")["items"] == ["Ann", "Bob", "Cy Archived"]  # icon labels count
        of = lambda sel: pg.locator(sel).first.evaluate(LIST_ITEMS_OF_JS)  # noqa: E731
        assert of("td >> text=open")["items"] == ["A open", "B closed"]  # header and hidden rows skipped
        assert of("table")["items"] == ["A open", "B closed"]
        assert of("#rows [role=cell] >> nth=1")["items"] == ["Ann", "Bob"]
        assert of(".card >> nth=1")["items"] == ["One Open", "Two"]  # unmarked cards: look-alike siblings
        browser.close()


# -- agent runs against the planted bug -------------------------------------------------------------

def lazy(p: Prompt) -> dict:
    """What the agent did in the real run: click every chip, check one text on the All view, pass."""
    for chip in ("Delivered", "Cancelled", "Returned", "All"):
        if not p.done_before(f'Click button "{chip}" -> done'):
            return {"action": "click", "element": p.find(f'button "{chip}"')}
    if not p.done_before("Verify"):
        return {"action": "verify_text", "value": "Showing 8 of 8 orders"}
    return {"action": "done", "outcome": "passed", "summary": "All filter buttons work"}


def diligent(p: Prompt, honest: bool = True) -> dict:
    """Checks every list after every chip; an honest agent reports a failed check as a failure."""
    if honest and "CHECK FAILED" in p.history:
        return {"action": "done", "outcome": "failed", "summary": "A filter lists orders of another status"}
    for chip, (label, n) in CHIPS.items():
        if not p.done_before(f'Click button "{chip}" -> done'):
            return {"action": "click", "element": p.find(f'button "{chip}"'), "expect": f"only {n} {label} order(s)"}
        if not p.done_before(f'contains "{label}"'):
            return {"action": "check_items", "value": label, "count": n}
    if not p.done_before('Click button "All" -> done'):
        return {"action": "click", "element": p.find('button "All"'), "expect": "all 8 orders"}
    if not p.done_before("the list has 8"):
        return {"action": "check_items", "count": 8}
    return {"action": "done", "outcome": "passed", "summary": "Each filter lists only its orders"}


@pytest.fixture
def run_orders(settings, site):
    def run(policy, variant: str, runs: int = 1, then_variant: str | None = None):
        site.variant = variant
        agent = ScriptedAgent(policy)
        with TestClient(create_app(replace(settings, action_timeout_ms=1500), llm=agent)) as client:
            api = Api(client)
            api.agent = agent
            pid = api.project("Shop", site.base_url + "orders")["id"]
            tid = api.call("POST", f"/projects/{pid}/tests", 201,
                           json={"instructions": [GOAL], "mode": "agent"})["tests"][0]["id"]
            reports = []
            for _ in range(runs):
                reports.append(run_once(api, pid, tid))
                site.variant = then_variant or site.variant
            return reports, agent
    return run


def test_clicking_through_without_checking_cannot_pass(run_orders):
    [(report, calls)], agent = run_orders(lazy, "filterbug")
    assert report["status"] == "error" and report["errorKind"] == "agent_unverified", report["summary"]
    assert any(p.count("HELD BACK") for p in agent.prompts)
    shown = next(p for p in agent.prompts if "A list now shows" in p)  # the evidence was put in front of it
    assert "Returned (refund issued)" in shown
    unchecked = [e["action"] for e in report["agent"]["verification"] if e["status"] == "unchecked"]
    assert 'Click button "Delivered"' in unchecked


def test_checking_each_filter_catches_the_planted_bug(run_orders):
    [(report, _)], _ = run_orders(diligent, "filterbug")
    assert report["status"] == "failed" and report["errorSource"] == "application", report["summary"]
    failure = report["failure"]
    assert failure["step"] == 'Check that every list item contains "cancelled" and there are 3 item(s)'
    assert "4 item(s)" in failure["actual"] and "Order #1005 Returned (refund issued)" in failure["actual"]


def test_the_same_checks_pass_on_the_correct_app_and_replay_without_the_model(run_orders):
    [(first, calls), (second, replay_calls)], _ = run_orders(diligent, "original", runs=2)
    assert first["status"] == "passed", first["summary"]
    assert [e["status"] for e in first["agent"]["verification"]] == ["checked"] * 4
    assert "4 action result(s) checked" in first["summary"]
    assert second["status"] == "passed" and second["verdictSource"] == "replay" and replay_calls == 0


def test_a_regression_after_a_passing_run_fails_the_replayed_check(run_orders):
    # the bug ships after the first (passing) run
    [(first, _), (second, _)], _ = run_orders(diligent, "original", runs=2, then_variant="filterbug")
    assert first["status"] == "passed"
    assert second["status"] == "failed", second["summary"]
    deviation = second["agent"]["replay"]["deviations"][0]
    assert deviation["description"].startswith('Check that every list item contains "cancelled"')


def test_a_pass_claimed_over_a_failed_check_is_rejected(run_orders):
    [(report, _)], agent = run_orders(lambda p: diligent(p, honest=False), "filterbug")
    assert report["status"] == "failed" and report["errorKind"] == "check_failed", report["summary"]
    assert "the agent had claimed a pass" in report["summary"]
    assert sum("You cannot finish as passed yet" in p for p in agent.prompts) == 2
