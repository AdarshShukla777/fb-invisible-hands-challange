"""Unit tests for the healing decision rule (no browser)."""
from __future__ import annotations

from app.execution.healing import Reference, compatible, decide, text_sim
from app.planning.plan import Target

KW = {"min_confidence": 0.75, "min_margin": 0.15, "plausible": 0.5}


def el(**kw):
    base = {"tag": "button", "type": "", "role": "button", "name": "", "visible": True, "enabled": True,
            "editable": False, "context": {}, "css": kw.get("name", "x"), "idx": 0}
    return {**base, **kw}


LOGIN = Target(role="button", name="Log in")
HISTORY = {"tag": "button", "role": "button", "name": "Log in", "testid": "login-submit",
           "context": {"form": "login-form", "heading": "Sign in"}}


def test_synonyms_and_similarity():
    assert text_sim("Log in", "Sign in") == 1.0
    assert text_sim("Add to cart", "Add to basket") == 1.0
    assert text_sim("Log in", "Delete account") < 0.5


def test_confident_unambiguous_replacement_is_selected():
    cands = [el(name="Sign in", context={"form": "login-form", "heading": "Sign in"}, css="#a"),
             el(name="Help", css="#b")]
    d = decide(Reference("click", LOGIN, HISTORY), cands, **KW)
    assert d.outcome == "selected" and d.selected.candidate["name"] == "Sign in"
    assert d.selected.score >= 0.75


def test_two_equally_plausible_candidates_are_ambiguous():
    same_ctx = {"form": "login-form", "heading": "Sign in"}
    cands = [el(name="Sign in", context=same_ctx, css="#a"), el(name="Sign in", context=same_ctx, css="#b")]
    d = decide(Reference("click", LOGIN, HISTORY), cands, **KW)
    assert d.outcome == "ambiguous" and d.selected is None


def test_history_context_disambiguates():
    cands = [el(name="Sign in", context={"form": "login-form", "heading": "Sign in"}, css="#form"),
             el(name="Sign in", context={"landmark": "Top"}, css="#header")]
    d = decide(Reference("click", LOGIN, HISTORY), cands, **KW)
    assert d.outcome == "selected" and d.selected.candidate["css"] == "#form"


def test_role_only_match_is_never_plausible():
    d = decide(Reference("click", LOGIN, None), [el(name="Cancel"), el(name="Help", css="#h")], **KW)
    assert d.outcome == "no_confident_candidate"
    assert all(s.score < 0.5 for s in d.ranked)


def test_incompatible_candidates_are_excluded():
    field = el(tag="input", type="text", role="textbox", name="Log in", editable=True)
    hidden = el(name="Log in", visible=False)
    disabled = el(name="Log in", enabled=False)
    d = decide(Reference("click", LOGIN, None), [field, hidden, disabled], **KW)
    assert d.outcome == "no_confident_candidate"
    assert d.excluded == {"not clickable": 1, "not visible": 1, "disabled": 1}
    ok, why = compatible("fill", el(tag="input", role="textbox", editable=False))
    assert not ok and why == "not editable"


def test_fill_target_heals_to_relabelled_field():
    target = Target(label="Email", placeholder="Email", name="Email")
    cands = [el(tag="input", type="email", role="textbox", name="Email address", label="Email address",
                editable=True, css="#e"),
             el(tag="input", type="password", role="textbox", name="Password", label="Password", editable=True, css="#p")]
    d = decide(Reference("fill", target, None), cands, **KW)
    assert d.outcome == "selected" and d.selected.candidate["css"] == "#e"


def test_exact_history_test_id_anchors_a_relabelled_element():
    cands = [el(name="Continue", testid="login-submit", context={"form": "login-form"}, css="#go"),
             el(name="Back", context={"form": "login-form"}, css="#back")]
    d = decide(Reference("click", LOGIN, HISTORY), cands, **KW)
    assert d.outcome == "selected" and d.selected.candidate["css"] == "#go"
    # the same test id without history (nothing to anchor to) is not enough
    d2 = decide(Reference("click", LOGIN, None), cands, **KW)
    assert d2.outcome == "no_confident_candidate"
