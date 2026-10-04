"""Result checking for agent runs: a pass needs evidence for every change the agent made.

The runner reports each interaction's visible effect (page text before/after, and which list
changed and how many items it shows) and every check (verify_text, check_items). Rules:

- An action that changed the page must have its result checked before the next interaction,
  unless the next interaction used an element this action revealed (that proves it worked,
  e.g. opening a page or a form) or continued on the same element.
- If an action changed a list in place (filter, search, delete, add), only check_items proves
  it: every listed item, not one text on the page.
- verify_text proves an action's result only with text that was not visible before it.
- A failed check stands unless a later check corrects it; re-checking the same text with other
  numbers ("Showing 3 of 8" failed, "Showing 4 of 8" passed) is not a correction.
- At least one check must pass.

The runner holds the agent back once when it moves on without checking, and pushes back (at
most twice) when it finishes as passed with problems left; then the run is failed (a check
failed: application) or an error (results unproven: agent). Page values that change on their
own (clocks, counters, live readings) are learned while the model thinks and ignored in effects."""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from difflib import SequenceMatcher
from typing import Any, Callable

MAX_SEGMENTS = 3000
MAX_LIST_ITEMS_SHOWN = 10
MAX_PUSHBACKS = 2
MAX_HOLDS = 2  # per unchecked change
# control names that are not a status or label the listed items could carry
_GENERIC = {"all", "any", "everything", "none", "clear", "reset", "show all", "clear all", "clear filters", "apply",
            "search", "filter", "filters", "ok", "save", "submit", "next", "previous", "more", "less"}
_NUMBER = re.compile(r"\d+(?:[.,:]\d+)*")


def segments(page_text: str) -> list[str]:
    return [s.strip() for s in (page_text or "").split(" | ") if s.strip()][:MAX_SEGMENTS]


def shape(s: str) -> str:
    return _NUMBER.sub("#", s.lower())


def norm(s: str) -> str:
    return re.sub(r"\s+", " ", (s or "").replace(" | ", " ")).strip().lower()


def contains(haystack: str, needle: str) -> bool:
    """Like PAGE_HAS_TEXT_JS: whitespace-collapsed, case-insensitive, also ignoring spaces."""
    h, n = norm(haystack), norm(needle)
    return bool(n) and (n in h or n.replace(" ", "") in h.replace(" ", ""))


def live_shapes(before: list[str], after: list[str]) -> set[str]:
    """Shapes of numeric values that changed while nobody touched the page (clocks, live readings)."""
    out: set[str] = set()
    for op, i1, i2, j1, j2 in SequenceMatcher(None, before, after, autojunk=False).get_opcodes():
        if op != "equal":
            out |= {shape(s) for s in before[i1:i2] + after[j1:j2] if "#" in shape(s)}
    return out


@dataclass
class Change:
    changed: list[tuple[str, str]] = field(default_factory=list)
    removed: list[str] = field(default_factory=list)
    added: list[str] = field(default_factory=list)

    @property
    def empty(self) -> bool:
        return not (self.changed or self.removed or self.added)

    def lines(self, limit: int = 1200) -> list[str]:
        out = []
        if self.changed:
            out.append("changed: " + "; ".join(f'"{a}" -> "{b}"' for a, b in self.changed[:12]))
        if self.removed:
            out.append("removed: " + " | ".join(self.removed)[:400])
        if self.added:
            out.append("added: " + " | ".join(self.added)[:400])
        budget, kept = limit, []
        for line in out:
            kept.append(line[:budget])
            budget -= len(kept[-1])
            if budget <= 0:
                break
        return kept


def diff(before: list[str], after: list[str], live: set[str]) -> Change:
    c = Change()
    for op, i1, i2, j1, j2 in SequenceMatcher(None, before, after, autojunk=False).get_opcodes():
        if op == "equal":
            continue
        old, new = before[i1:i2], after[j1:j2]
        paired = min(len(old), len(new)) if op == "replace" else 0
        for a, b in zip(old[:paired], new[:paired]):
            if shape(a) == shape(b):
                if shape(a) not in live:
                    c.changed.append((a, b))
            else:
                c.removed.append(a)
                c.added.append(b)
        c.removed += old[paired:]
        c.added += new[paired:]
    return c


def element_key(el: dict[str, Any] | None) -> tuple | None:
    if not el:
        return None
    return (el.get("role") or el.get("tag"), (el.get("name") or "")[:60], el.get("placeholder") or "",
            el.get("id") or "", el.get("testid") or "", el.get("rowIndex") or 0)


def _contradicts(failed: dict[str, Any], passed: dict[str, Any]) -> bool:
    """A 'correction' that only moves the expected numbers to what the page shows."""
    if failed["kind"] == passed["kind"] == "verify_text":
        return shape(failed["value"] or "") == shape(passed["value"] or "") and norm(failed["value"]) != norm(passed["value"])
    if failed["kind"] == passed["kind"] == "check_items":
        same_list = failed.get("list") == passed.get("list")
        return same_list and failed.get("count") is not None and passed.get("count") is not None \
            and failed["count"] != passed["count"]
    return False


@dataclass
class Effect:
    step: int
    description: str
    action: str
    expect: str | None = None
    element_key: tuple | None = None
    before_keys: frozenset = frozenset()
    before_text: str = ""
    change: list[str] = field(default_factory=list)
    list_key: str | None = None
    list_before: int = 0
    list_after: int = 0
    list_items: list[str] = field(default_factory=list)
    evaluation: str | None = None
    covered_by: str | None = None
    holds: int = 0
    hints: list[str] = field(default_factory=list)  # deterministic observations for the agent
    checks: list[dict[str, Any]] = field(default_factory=list)

    @property
    def needs_items(self) -> bool:
        """The action changed a list in place (it had items before and after): check the items."""
        return bool(self.list_key) and self.list_before >= 1 and self.list_after >= 1

    def standing_failures(self) -> list[dict[str, Any]]:
        out = []
        for i, c in enumerate(self.checks):
            if c["passed"]:
                continue
            fixes = [d for d in self.checks[i + 1:] if d["passed"] and d["counts"]]
            if not any(not _contradicts(c, d) for d in fixes):
                out.append(c)
        return out

    @property
    def status(self) -> str:
        if self.standing_failures():
            return "failed"
        if any(c["passed"] and c["counts"] for c in self.checks):
            return "checked"
        return "covered" if self.covered_by else "unchecked"

    def as_api(self) -> dict[str, Any]:
        return {"step": self.step, "action": self.description, "expected": self.expect, "evaluation": self.evaluation,
                "change": self.change, "list": ({"before": self.list_before, "after": self.list_after}
                                                if self.list_key else None),
                "status": self.status, "coveredBy": self.covered_by, "hints": self.hints,
                "checks": [{"description": c["description"], "passed": c["passed"], "counts": c["counts"],
                            "detail": c["detail"], "expected": c.get("expected"), "actual": c.get("actual")}
                           for c in self.checks]}


class Verification:
    def __init__(self) -> None:
        self.effects: list[Effect] = []
        self.live: set[str] = set()
        self.pushbacks = 0
        self.last_step: int | None = None  # step index of the most recent interaction
        self.last_action: dict[str, Any] | None = None  # the most recent interaction, also without effect

    @property
    def current(self) -> Effect | None:
        return self.effects[-1] if self.effects else None

    def start(self, description: str) -> None:
        """The start page: nothing to check, but checks made there are recorded (and can fail)."""
        self.effects.append(Effect(step=0, description=description, action="navigate", covered_by="start page"))

    def ambient(self, text_at_decision: str, text_before_action: str) -> None:
        self.live |= live_shapes(segments(text_at_decision), segments(text_before_action))

    def evaluate(self, evaluation: str | None) -> None:
        cur = self.current
        if evaluation and cur is not None and cur.evaluation is None and cur.step == self.last_step:
            cur.evaluation = evaluation[:400]

    # -- interactions -------------------------------------------------------------------------

    def before_interaction(self, key: tuple | None) -> str | None:
        """None if the agent may go on; otherwise a note to check first (at most MAX_HOLDS per change)."""
        cur = self.current
        if cur is None or cur.status in ("checked", "covered"):
            return None
        if cur.status == "unchecked" and key is not None and (key == cur.element_key or key not in cur.before_keys):
            cur.covered_by = ("continued on the same element" if key == cur.element_key
                              else "the next step used an element this action revealed")
            return None
        if cur.holds >= MAX_HOLDS:
            return None
        cur.holds += 1
        if cur.status == "failed":
            failed = cur.standing_failures()[-1]
            return (f'HELD BACK: your check "{failed["description"]}" failed ({failed["detail"]}) after '
                    f'"{cur.description}". If the check itself was wrong (wording), correct it now. If the '
                    "application is wrong, finish with outcome failed. Do not move on as if it passed.")
        how = ("check_items on that list (every item must match what you selected; give count when you know it)"
               if cur.needs_items else "verify_text with text that appeared, or check_items for a list")
        return (f'HELD BACK: your last action "{cur.description}" changed the page, but you have not checked its '
                f"result. Compare the result with what you expected, check it now with {how}, and only then "
                "continue. If it is wrong, finish with outcome failed.")

    def after_interaction(self, *, step: int, description: str, action: str, expect: str | None,
                          key: tuple | None, before_keys: frozenset, text_before: str, text_after: str,
                          lists_before: dict[str, list], lists_after: dict[str, list],
                          items: Callable[[str], list[str]], remembered: str | None = None,
                          control: str | None = None) -> Effect | None:
        """Records the action's effect; None when it changed nothing visible. lists_* map a list's key
        to [item count, signature of its item texts] (LISTS_JS); control is what was selected (the
        clicked element's name, the chosen option or the typed search text)."""
        self.last_step = step
        self.last_action = {"step": step, "description": description, "changed": False, "hints": []}
        change = diff(segments(text_before), segments(text_after), self.live)
        none = [0, None]
        moved = [(k, lists_before.get(k, none)[0], lists_after.get(k, none)[0])
                 for k in set(lists_before) | set(lists_after) if lists_before.get(k, none) != lists_after.get(k, none)]
        cur = self.current
        if (not moved and action in ("click", "select") and cur is not None and cur.list_key and cur.list_after
                and cur.element_key != key and lists_before.get(cur.list_key) == lists_after.get(cur.list_key)):
            self.last_action["hints"].append(
                f'the list still shows the same {cur.list_after} item(s) as after "{cur.description}": two different '
                "filters should not list the same items unless every item really matches both")
        if action in ("fill", "check", "uncheck") and not moved:
            return None  # the control's own state is asserted by the executor; nothing else reacted
        if change.empty and not moved:
            return None
        self.last_action["changed"] = True
        effect = Effect(step=step, description=description, action=action, expect=(expect or None) and expect[:300],
                        element_key=key, before_keys=before_keys, before_text=norm(text_before),
                        change=change.lines())
        if moved:  # the list that changed most; same count means its items were swapped
            k, b, a = max(moved, key=lambda m: (abs(m[2] - m[1]), m[2]))
            effect.list_key, effect.list_before, effect.list_after = k, b, a
            listed = items(k) if a else []
            effect.list_items = listed[:MAX_LIST_ITEMS_SHOWN]
            name = (control or "").strip()
            if listed and effect.needs_items and name and len(name) <= 40 and name.lower() not in _GENERIC:
                n = sum(contains(t, name) for t in listed)
                if n < len(listed):
                    some = "none" if n == 0 else f"only {n}"
                    effect.hints.append(f'{some} of the {len(listed)} listed item(s) mention "{name}": check what '
                                        "each item's own status or label says before accepting this result")
        if remembered:  # replay of a gated path: the remembered run already proved this step
            effect.covered_by = f"remembered path ({remembered})"
        self.effects.append(effect)
        return effect

    def remembered(self, step: int, result: str | None) -> None:
        """A remembered step redone by the agent during replay: the remembered path covers its result."""
        cur = self.current
        if cur is not None and cur.step == step and cur.covered_by is None:
            cur.covered_by = f"remembered path ({result or 'no visible change then'})"

    # -- checks -------------------------------------------------------------------------------

    def record_check(self, *, kind: str, description: str, value: str | None, count: int | None, passed: bool,
                     detail: str | None, list_key: str | None, expected: str | None = None,
                     actual: str | None = None) -> str | None:
        """Attaches a check to the latest effect. Returns why a passing check does not prove it, if so."""
        if self.current is None:
            self.start("start page")
        cur = self.current
        why_not = None
        if passed:
            if kind == "verify_text" and cur.needs_items:
                why_not = (f'"{cur.description}" changed a list ({cur.list_before} -> {cur.list_after} items); '
                           "one text does not prove it - use check_items on that list")
            elif kind == "verify_text" and cur.before_text and contains(cur.before_text, value or ""):
                why_not = (f'this text was already visible before "{cur.description}", so it does not prove '
                           "that action's result")
            elif kind == "check_items" and cur.needs_items and list_key != cur.list_key:
                why_not = f'this is not the list that "{cur.description}" changed'
        cur.checks.append({"kind": kind, "description": description, "value": value, "count": count,
                           "list": list_key, "passed": passed, "counts": passed and why_not is None,
                           "detail": detail, "expected": expected, "actual": actual})
        return why_not

    # -- verdict ------------------------------------------------------------------------------

    def problems(self) -> tuple[list[Effect], list[Effect], bool]:
        """(effects with a failed check, effects never checked, whether any check passed)."""
        failed = [e for e in self.effects if e.status == "failed"]
        unchecked = [e for e in self.effects if e.status == "unchecked"]
        any_passed = any(c["passed"] for e in self.effects for c in e.checks)
        return failed, unchecked, any_passed

    def pushback_note(self) -> str | None:
        failed, unchecked, any_passed = self.problems()
        parts = []
        if failed:
            parts.append("These checks FAILED and were not corrected: " + "; ".join(
                f'"{c["description"]}" after "{e.description}" ({c["detail"]})'
                for e in failed for c in e.standing_failures()) + ". A failed check means the application did not "
                "do what was expected: finish with outcome failed, unless the check itself was wrong.")
        if unchecked:
            parts.append("These actions changed the page but their results were never checked: " + "; ".join(
                f'"{e.description}" (step {e.step})' + (" - a list changed: needs check_items" if e.needs_items else "")
                for e in unchecked) + ". Repeat each one and check its result right after it (the page state they "
                "produced is gone).")
        if not any_passed:
            parts.append("No check has passed. Prove the goal with verify_text or check_items.")
        if not parts:
            return None
        return "You cannot finish as passed yet. " + " ".join(parts)

    def verdict(self) -> tuple[str, str, str] | None:
        """After the pushbacks: (status, error kind, message), or None if the pass stands."""
        failed, unchecked, any_passed = self.problems()
        if failed:
            first = failed[0].standing_failures()[0]
            return ("failed", "check_failed",
                    f'Check "{first["description"]}" after "{failed[0].description}" failed: {first["detail"]}'
                    + (f" ({len(failed)} action(s) with failed checks)" if len(failed) > 1 else ""))
        if unchecked:
            return ("error", "agent_unverified", "The agent finished as passed without checking the result of: "
                    + "; ".join(f'"{e.description}"' for e in unchecked))
        if not any_passed:
            return "error", "agent_unverified", "The agent finished as passed without any passing check."
        return None

    # -- prompt -------------------------------------------------------------------------------

    def feedback(self) -> str | None:
        """What the agent sees about its last change: expectation, effect, list items, check status."""
        lines = []
        last = self.last_action
        if last and not last["changed"]:
            lines.append(f'LAST ACTION: "{last["description"]}" (step {last["step"]}) made NO visible change.')
            lines += [f"  Note: {h}" for h in last["hints"]]
        cur = self.current
        if cur is None or cur.covered_by == "start page" and not cur.checks:
            return "\n".join(lines) or None
        if cur.step != self.last_step and cur.status in ("checked", "covered"):
            return "\n".join(lines) or None
        lines.append(f'LAST CHANGE: "{cur.description}" (step {cur.step})')
        if cur.expect:
            lines.append(f"  You expected: {cur.expect}")
        lines += [f"  {line}" for line in cur.change] or ["  (no text change)"]
        if cur.list_key:
            lines.append(f"  A list now shows {cur.list_after} item(s) (before: {cur.list_before})"
                         + (":" if cur.list_items else "."))
            lines += [f"    {i}) {t[:110]}" for i, t in enumerate(cur.list_items, 1)]
        lines += [f"  Note: {h}" for h in cur.hints]
        for c in cur.checks:
            state = "passed" if c["passed"] else "FAILED"
            lines.append(f'  Check "{c["description"]}": {state} - {c["detail"]}'
                         + ("" if c["counts"] or not c["passed"] else " (does not prove this change)"))
        status = cur.status
        if status == "unchecked":
            lines.append("  Result NOT CHECKED yet: does it match your expectation and the goal? Check it now ("
                         + ("check_items on the list" if cur.needs_items else "verify_text / check_items")
                         + ") before any other action. If it is wrong, finish with outcome failed.")
        elif status == "failed":
            lines.append("  A check FAILED: the application did not do what was expected, unless your check was wrong.")
        return "\n".join(lines)

    def details(self) -> list[dict[str, Any]]:
        return [e.as_api() for e in self.effects if not (e.covered_by == "start page" and not e.checks)]
