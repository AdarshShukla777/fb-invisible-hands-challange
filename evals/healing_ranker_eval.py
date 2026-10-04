"""Healing candidate-ranker comparison: deterministic baseline vs. TypeSafe Jev.

    uv run python evals/healing_ranker_eval.py            # baseline only
    TYPESAFE_API_KEY=... uv run python evals/healing_ranker_eval.py   # baseline + Jev

Why this task: Jev is a "System One" model for typed decisions (Choice/Score/Noul) with
calibrated confidence - the shape of "which of these elements is the one this step
meant, or none of them?". It cannot generate plans (no text generation) and takes no
images, so healing candidate selection is the only place it could fit in v1.

Adoption rule (from the v1 brief): switch only if Jev shows a practical improvement over
the baseline on this set WITHOUT more unsafe selections (picking a wrong element or picking
when the right answer is to stop). Unsafe selections are the metric that matters: a wrong
heal turns a real regression into a false pass.

Status: the Jev client below follows the documented HTTP API (POST /v1/systemone) but has
NOT been executed - no API key was available when this was written. Treat it as untested."""
from __future__ import annotations

import json
import os
import statistics
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.execution.healing import Decision, Reference, Scored, compatible, decide  # noqa: E402
from app.planning.plan import Target  # noqa: E402

THRESHOLDS = {"min_confidence": 0.75, "min_margin": 0.15, "plausible": 0.5}


def el(css: str, **kw: Any) -> dict[str, Any]:
    base = {"tag": "button", "type": "", "role": "button", "name": "", "visible": True, "enabled": True,
            "editable": False, "context": {}, "css": css}
    return {**base, **kw}


def field(css: str, name: str, **kw: Any) -> dict[str, Any]:
    return el(css, tag="input", type=kw.pop("type", "text"), role="textbox", name=name, label=name, editable=True, **kw)


LOGIN_HISTORY = {"tag": "button", "role": "button", "name": "Log in", "testid": "login-submit",
                 "context": {"form": "login-form", "heading": "Sign in"}}
FORM = {"form": "login-form", "heading": "Sign in"}


@dataclass
class Case:
    name: str
    action: str
    target: Target
    history: dict[str, Any] | None
    candidates: list[dict[str, Any]]
    expected: str  # "select:<css>" or "refuse"


CASES = [
    Case("rename via synonym, with history", "click", Target(role="button", name="Log in"), LOGIN_HISTORY,
         [el("#signin", name="Sign in", context=FORM), el("#help", name="Help")], "select:#signin"),
    Case("rename via synonym, no history", "click", Target(role="button", name="Log in"), None,
         [el("#signin", name="Sign in"), el("#help", name="Help")], "select:#signin"),
    Case("duplicate identical buttons", "click", Target(role="button", name="Log in"), LOGIN_HISTORY,
         [el("#a", name="Sign in", context=FORM), el("#b", name="Sign in", context=FORM)], "refuse"),
    Case("history context disambiguates", "click", Target(role="button", name="Log in"), LOGIN_HISTORY,
         [el("#form", name="Sign in", context=FORM), el("#header", name="Sign in", context={"landmark": "Top"})],
         "select:#form"),
    Case("target removed", "click", Target(role="button", name="Log in"), LOGIN_HISTORY,
         [el("#cancel", name="Cancel", context=FORM), el("#help", name="Help")], "refuse"),
    Case("text changed, test id kept", "click", Target(role="button", name="Log in"), LOGIN_HISTORY,
         [el("#go", name="Continue", testid="login-submit", context=FORM), el("#back", name="Back", context=FORM)],
         "select:#go"),
    Case("field relabelled", "fill", Target(label="Email", placeholder="Email", name="Email"), None,
         [field("#email", "Email address", type="email"), field("#pw", "Password", type="password")], "select:#email"),
    Case("field gone - must not fill another field", "fill", Target(label="Email", name="Email"), None,
         [field("#pw", "Password", type="password"), field("#nick", "Nickname")], "refuse"),
    Case("cart synonym", "click", Target(role="button", name="Add to cart"), None,
         [el("#add", name="Add to basket"), el("#wish", name="Add to wishlist")], "select:#add"),
    Case("delete vs remove all", "click", Target(role="button", name="Delete"), None,
         [el("#rm", name="Remove"), el("#rmall", name="Remove all")], "select:#rm"),
    Case("paraphrase: remember me", "check", Target(role="checkbox", name="Remember me"), None,
         [el("#keep", tag="input", type="checkbox", role="checkbox", name="Keep me signed in"),
          el("#terms", tag="input", type="checkbox", role="checkbox", name="I accept the terms")], "select:#keep"),
    Case("paraphrase: place order", "click", Target(role="button", name="Submit order"), None,
         [el("#place", name="Place order"), el("#cancel", name="Cancel order")], "select:#place"),
    Case("paraphrase with decoy: save", "click", Target(role="button", name="Save"), None,
         [el("#s1", name="Save changes"), el("#s2", name="Save as draft")], "refuse"),
    Case("moved link, history picks header", "click", Target(role="link", name="Pricing"),
         {"tag": "a", "role": "link", "name": "Pricing", "context": {"landmark": "Top"}},
         [el("#hp", tag="a", role="link", name="Plans & pricing", context={"landmark": "Top"}),
          el("#fp", tag="a", role="link", name="Plans & pricing", context={"landmark": "contentinfo"})], "select:#hp"),
    Case("only candidate is disabled", "click", Target(role="button", name="Pay now"), None,
         [el("#pay", name="Pay now", enabled=False)], "refuse"),
    Case("search placeholder changed", "fill", Target(placeholder="Search", name="Search"), None,
         [field("#q", "Search products"), field("#news", "Your email", type="email")], "select:#q"),
    Case("button became link", "click", Target(role="button", name="Next"), None,
         [el("#next", tag="a", role="link", name="Next"), el("#prev", tag="a", role="link", name="Previous")],
         "select:#next"),
]


def outcome_label(d: Decision) -> str:
    return f"select:{d.selected.candidate['css']}" if d.selected else "refuse"


# -- optional Jev ranker (untested: no key available) ------------------------------------------

def jev_ranker(api_key: str, model: str = "jev-latest") -> Callable[..., Decision]:
    import httpx

    client = httpx.Client(base_url="https://api.typesafe.ai", timeout=10.0,
                          headers={"Authorization": f"Bearer {api_key}"})

    def rank(ref: Reference, candidates: list[dict[str, Any]], *, min_confidence: float, min_margin: float,
             plausible: float) -> Decision:
        usable = [c for c in candidates if compatible(ref.action, c)[0]]
        if not usable:
            return Decision("no_confident_candidate", None, [], "No compatible candidate.")
        options = {f"c{i}": _describe(c) for i, c in enumerate(usable[:254])}
        options["none"] = "None of these elements is the intended target"
        state = {"action": ref.action, "intended_target": ref.target.model_dump(exclude_none=True),
                 "element_last_time_this_step_passed": ref.history}
        body = {"model": model, "state": state, "questions": {"match": {
            "type": "choice",
            "instructions": "Which current page element is the element the test step intended to act on?",
            "criteria": options,
        }}}
        r = client.post("/v1/systemone", json=body)
        r.raise_for_status()
        answer = r.json()["answers"]["match"]
        probs = sorted(answer["probabilities"].items(), key=lambda kv: -kv[1])
        top, p_top = probs[0]
        p_next = probs[1][1] if len(probs) > 1 else 0.0
        ranked = [Scored(usable[int(k[1:])], p, {}, p) for k, p in probs if k != "none"]
        if top == "none" or answer["confidence"] < min_confidence or p_top < min_confidence:
            return Decision("no_confident_candidate", None, ranked, f"Jev: {top} p={p_top:.2f}")
        if p_top - p_next < min_margin:
            return Decision("ambiguous", None, ranked, f"Jev margin {p_top - p_next:.2f}")
        best = next(s for s in ranked if s.candidate is usable[int(top[1:])])
        return Decision("selected", best, ranked, f"Jev picked {top} p={p_top:.2f}")

    return rank


def _describe(c: dict[str, Any]) -> str:
    parts = [c.get("role") or c.get("tag"), f'"{c.get("name", "")}"']
    for key in ("testid", "id", "placeholder"):
        if c.get(key):
            parts.append(f"{key}={c[key]}")
    ctx = {k: v for k, v in (c.get("context") or {}).items() if v}
    if ctx:
        parts.append(f"context={json.dumps(ctx)}")
    return " ".join(parts)


# -- evaluation ----------------------------------------------------------------------------------

def evaluate(name: str, ranker: Callable[..., Decision]) -> dict[str, Any]:
    correct = unsafe = missed = 0
    latencies = []
    rows = []
    for case in CASES:
        started = time.perf_counter()
        try:
            got = outcome_label(ranker(Reference(case.action, case.target, case.history), case.candidates, **THRESHOLDS))
        except Exception as exc:  # an unavailable model counts as a refusal, and is reported
            got = f"refuse (error: {type(exc).__name__})"
        latencies.append((time.perf_counter() - started) * 1000)
        ok = got == case.expected or (case.expected == "refuse" and got.startswith("refuse"))
        wrong_pick = got.startswith("select") and got != case.expected
        correct += ok
        unsafe += wrong_pick
        missed += case.expected.startswith("select") and got.startswith("refuse")
        rows.append((case.name, case.expected, got, "ok" if ok else "UNSAFE" if wrong_pick else "missed"))
    return {"name": name, "rows": rows, "accuracy": correct / len(CASES), "unsafe": unsafe, "missed": missed,
            "p50_ms": statistics.median(latencies), "p95_ms": sorted(latencies)[int(0.95 * (len(latencies) - 1))]}


def main() -> None:
    results = [evaluate("baseline (rules)", decide)]
    key = os.environ.get("TYPESAFE_API_KEY")
    if key:
        results.append(evaluate("jev", jev_ranker(key)))
    for r in results:
        print(f"\n== {r['name']}: accuracy {r['accuracy']:.0%}, unsafe selections {r['unsafe']}, "
              f"missed heals {r['missed']}, latency p50 {r['p50_ms']:.2f} ms / p95 {r['p95_ms']:.2f} ms")
        for name, expected, got, verdict in r["rows"]:
            print(f"  {verdict:7} {name:40} expected {expected:15} got {got}")
    if not key:
        print("\nJev not evaluated: TYPESAFE_API_KEY is not set (early access: https://console.typesafe.ai/keys).")


if __name__ == "__main__":
    main()
