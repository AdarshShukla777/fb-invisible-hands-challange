"""Locator specs (JSON-serializable) and resolution against a live page.

A target's hints produce an ordered list of *declared* strategies, most stable first:
test id -> role + accessible name -> label -> name with action-compatible roles ->
placeholder -> visible text -> css. Resolution succeeds only when a strategy matches
exactly one visible element. Anything else is a locator failure, handed to healing."""
from __future__ import annotations

import re
import time
from dataclasses import dataclass, field
from typing import Any

from playwright.sync_api import Error as PlaywrightError
from playwright.sync_api import Locator, Page

from ..planning.plan import Target, action_class

ROLE_FALLBACKS: dict[str, list[str]] = {
    "click": ["button", "link", "menuitem", "tab", "checkbox", "radio", "switch", "option"],
    "fill": ["textbox", "searchbox", "combobox", "spinbutton"],
    "select": ["combobox", "listbox"],
    "check": ["checkbox", "radio", "switch"],
    "press": ["textbox", "searchbox", "button", "link"],
    "assert": [],
}


def declared_specs(action: str, target: Target) -> list[dict[str, Any]]:
    cls = action_class(action)
    t = target
    specs: list[dict[str, Any]] = []
    if t.testid:
        specs.append({"strategy": "testid", "value": t.testid})
    if t.role and t.name:
        specs.append({"strategy": "role", "role": t.role, "name": t.name, "exact": t.exact})
    if t.label:
        specs.append({"strategy": "label", "value": t.label, "exact": t.exact})
    if t.name and not t.role:
        for role in ROLE_FALLBACKS.get(cls, []):
            specs.append({"strategy": "role", "role": role, "name": t.name, "exact": t.exact})
        if cls == "assert":
            specs.append({"strategy": "text", "value": t.name, "exact": t.exact})
    if t.placeholder:
        specs.append({"strategy": "placeholder", "value": t.placeholder, "exact": t.exact})
    if t.text:
        specs.append({"strategy": "text", "value": t.text, "exact": t.exact})
    if t.css:
        specs.append({"strategy": "css", "value": t.css})
    if t.role and not specs:
        specs.append({"strategy": "role", "role": t.role})
    unique: list[dict[str, Any]] = []
    for s in specs:
        if s not in unique:
            unique.append(s)
    return unique


def _full(value: str) -> re.Pattern[str]:
    return re.compile(r"^\s*" + re.escape(value.strip()) + r"\s*$", re.IGNORECASE)


ROW_SELECTOR = "[role=row]:has([role=cell], [role=gridcell]), tr:has(td)"
CONTROL_SELECTOR = ":is(button, a, [role=button], [role=link], [role=menuitem], [role=tab])"


def _css_str(value: str) -> str:
    return value.replace("\\", "\\\\").replace('"', '\\"')


def _root(page: Page, spec: dict[str, Any]) -> Page | Locator:
    """Optional table-row scope: rowText (row containing that text) or rowIndex (n-th data row)."""
    if spec.get("rowText"):
        return page.locator(ROW_SELECTOR).filter(has_text=spec["rowText"])
    if spec.get("rowIndex"):
        return page.locator(ROW_SELECTOR).nth(int(spec["rowIndex"]) - 1)
    return page


def variants(page: Page, spec: dict[str, Any]) -> list[tuple[str, Locator]]:
    """Locators for a spec. Non-exact text strategies try a whole-string (case-insensitive)
    match first, then a substring match, so "Log in" prefers "Log in" over "Log in with SSO"."""
    s = spec["strategy"]
    exact = spec.get("exact", False)
    root = _root(page, spec)
    if s == "testid":
        return [("exact", root.get_by_test_id(spec["value"]))]
    if s == "attr":
        return [("exact", root.locator(f'[{spec["attr"]}="{_css_str(spec["value"])}"]'))]
    if s == "css":
        return [("exact", root.locator(spec["value"]))]
    if s == "icon":  # icon-only control, e.g. <button><i class="bi-pencil-fill"></i></button>
        return [("exact", root.locator(f'{CONTROL_SELECTOR}:has([class~="{_css_str(spec["value"])}"])'))]
    if s == "role":
        if not spec.get("name"):
            return [("exact", root.get_by_role(spec["role"]))]
        if exact:
            return [("exact", root.get_by_role(spec["role"], name=spec["name"], exact=True))]
        return [("full", root.get_by_role(spec["role"], name=_full(spec["name"]))),
                ("substring", root.get_by_role(spec["role"], name=spec["name"]))]
    getter = {"label": root.get_by_label, "placeholder": root.get_by_placeholder, "text": root.get_by_text}[s]
    if exact:
        return [("exact", getter(spec["value"], exact=True))]
    return [("full", getter(_full(spec["value"]))), ("substring", getter(spec["value"]))]


def describe_spec(spec: dict[str, Any]) -> str:
    s = spec["strategy"]
    if s == "role":
        out = f'role={spec["role"]}' + (f' name="{spec["name"]}"' if spec.get("name") else "")
    elif s == "attr":
        out = f'[{spec["attr"]}="{spec["value"]}"]'
    else:
        out = f'{s}="{spec["value"]}"'
    if spec.get("rowText"):
        out += f' in row "{spec["rowText"][:40]}"'
    elif spec.get("rowIndex"):
        out += f' in row {spec["rowIndex"]}'
    return out


@dataclass
class Resolution:
    locator: Locator | None
    spec: dict[str, Any] | None
    attempts: list[dict[str, Any]] = field(default_factory=list)
    failure: str | None = None  # not_found | ambiguous


def count_visible(page: Page, spec: dict[str, Any]) -> list[tuple[str, Locator, int]]:
    out = []
    for variant, loc in variants(page, spec):
        visible = loc.filter(visible=True)
        try:
            n = visible.count()
        except PlaywrightError:
            n = -1  # invalid selector
        out.append((variant, visible, n))
    return out


def resolve(page: Page, specs: list[dict[str, Any]], timeout_ms: int, poll_ms: int = 150) -> Resolution:
    """Polls until one declared spec matches exactly one visible element, or the timeout passes."""
    deadline = time.monotonic() + timeout_ms / 1000
    attempts: list[dict[str, Any]] = []
    while True:
        attempts = []
        for spec in specs:
            for variant, loc, n in count_visible(page, spec):
                attempts.append({"locator": describe_spec(spec), "match": variant, "visibleMatches": n})
                if n == 1:
                    return Resolution(loc, {**spec, "match": variant}, attempts)
        if time.monotonic() >= deadline:
            break
        page.wait_for_timeout(poll_ms)
    failure = "ambiguous" if any(a["visibleMatches"] > 1 for a in attempts) else "not_found"
    return Resolution(None, None, attempts, failure)


def replacement_specs(candidate: dict[str, Any]) -> list[dict[str, Any]]:
    """Stable locator options for an element, most stable first. Elements inside a table row
    also get row-scoped variants (by row text, then by row number) for when the unscoped
    locator is not unique, e.g. the same "edit" icon in every row."""
    specs: list[dict[str, Any]] = []
    if candidate.get("testid"):
        attr = candidate.get("testidAttr") or "data-testid"
        specs.append({"strategy": "testid", "value": candidate["testid"]} if attr == "data-testid"
                     else {"strategy": "attr", "attr": attr, "value": candidate["testid"]})
    if candidate.get("role") and candidate.get("name"):
        specs.append({"strategy": "role", "role": candidate["role"], "name": candidate["name"], "exact": True})
    if candidate.get("label"):
        specs.append({"strategy": "label", "value": candidate["label"], "exact": True})
    if candidate.get("placeholder"):
        specs.append({"strategy": "placeholder", "value": candidate["placeholder"], "exact": True})
    if candidate.get("id"):
        specs.append({"strategy": "attr", "attr": "id", "value": candidate["id"]})
    if candidate.get("text") and len(candidate["text"]) <= 60:
        specs.append({"strategy": "text", "value": candidate["text"], "exact": True})
    if candidate.get("icon") and not candidate.get("name"):
        specs.append({"strategy": "icon", "value": candidate["icon"]})
    if candidate.get("row"):
        scoped = [s for s in specs if s["strategy"] != "attr"]
        specs += [{**s, "rowText": candidate["row"]} for s in scoped]
        if candidate.get("rowIndex"):
            specs += [{**s, "rowIndex": candidate["rowIndex"]} for s in scoped]
    if candidate.get("css"):
        specs.append({"strategy": "css", "value": candidate["css"]})
    return specs


def locate_candidate(page: Page, candidate: dict[str, Any]) -> tuple[dict[str, Any], Locator] | None:
    """The most stable locator that matches exactly this element (checked against the node the
    inventory captured, via its idx), or None."""
    from .dom import SAME_ELEMENT_JS

    for spec in replacement_specs(candidate):
        for variant, loc, n in count_visible(page, spec):
            try:
                if n == 1 and loc.evaluate(SAME_ELEMENT_JS, candidate["idx"]):
                    return {**spec, "match": variant}, loc
            except PlaywrightError:
                continue
    return None
