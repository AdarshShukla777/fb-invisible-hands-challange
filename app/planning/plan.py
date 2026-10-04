"""Structured test plans: schema, normalization and validation.

Every planner (rule-based or LLM) produces a raw plan dict; validate_plan() is the
single gate before anything is saved as runnable. It enforces:
  * known actions/assertion types and required fields;
  * navigation stays on the project's origin;
  * at least one explicit assertion (otherwise: clarification, never an invented check);
  * grounding: exact expected texts must be quoted in the instruction (URL fragments may be
    unquoted), typed values and {{secret:NAME}} placeholders must appear in it, and
    navigation targets must come from the instruction or observed project pages.
Step descriptions are generated here so they are consistent and never echo secrets."""
from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, ValidationError

from .. import urls
from ..util import dumps

Action = Literal["navigate", "click", "fill", "select", "check", "uncheck", "press", "hover", "wait_for_text", "assert"]
AssertionType = Literal[
    "text_visible", "text_not_visible", "element_visible", "element_not_visible",
    "element_text_contains", "input_value_equals", "url_contains", "title_contains",
]

TARGET_ACTIONS = {"click", "fill", "select", "check", "uncheck", "hover"}
# assertion type -> (needs expected, needs target)
ASSERTION_RULES: dict[str, tuple[bool, bool]] = {
    "text_visible": (True, False),
    "text_not_visible": (True, False),
    "element_visible": (False, True),
    "element_not_visible": (False, True),
    "element_text_contains": (True, True),
    "input_value_equals": (True, True),
    "url_contains": (True, False),
    "title_contains": (True, False),
}
ROLES = {
    "button", "link", "textbox", "searchbox", "combobox", "listbox", "option", "checkbox", "radio", "switch",
    "tab", "menuitem", "menu", "heading", "img", "dialog", "alert", "status", "navigation", "row", "cell",
    "spinbutton", "slider", "treeitem", "article", "region", "list", "listitem", "form", "banner",
}
KEYS = {
    "Enter", "Tab", "Escape", "Backspace", "Delete", "Space", "ArrowUp", "ArrowDown", "ArrowLeft",
    "ArrowRight", "Home", "End", "PageUp", "PageDown",
}
_KEY_RE = re.compile(r"^(?:(?:Control|Shift|Alt|Meta)\+)*(?:[A-Za-z0-9]|F[1-9]|F1[0-2]|" + "|".join(sorted(KEYS)) + r")$")
SECRET_RE = re.compile(r"^\{\{secret:([A-Z0-9_]{1,64})\}\}$")
_SENSITIVE = re.compile(r"pass(?:word|code|phrase)?|secret|token|\botp\b|\bpin\b|cvv|cvc|card.?number|\bssn\b", re.I)
_HINTS = ("name", "label", "placeholder", "testid", "text", "css")


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Target(_Strict):
    description: str | None = None
    role: str | None = None
    name: str | None = None
    label: str | None = None
    placeholder: str | None = None
    testid: str | None = None
    text: str | None = None
    css: str | None = None
    exact: bool = False

    def hints(self) -> dict[str, str]:
        return {k: getattr(self, k) for k in ("role", *_HINTS) if getattr(self, k)}

    def is_sensitive(self) -> bool:
        return any(_SENSITIVE.search(v) for k, v in self.hints().items() if k != "role") or bool(
            self.description and _SENSITIVE.search(self.description)
        )


class Assertion(_Strict):
    type: AssertionType
    expected: str | None = None
    target: Target | None = None
    source: str | None = None


class Step(_Strict):
    id: str = ""
    action: Action
    description: str = ""
    target: Target | None = None
    value: str | None = None
    # "generated": test data made up by the planner because the instruction gives none
    value_source: Literal["instruction", "generated"] | None = None
    url: str | None = None
    key: str | None = None
    assertion: Assertion | None = None

    @property
    def kind(self) -> str:
        return "assertion" if self.action == "assert" else "action"


class Plan(_Strict):
    steps: list[Step]


class PlanIssue(BaseModel):
    code: str
    message: str
    severity: Literal["clarification", "error"]
    step_id: str | None = None


@dataclass
class ValidationResult:
    plan: Plan | None
    issues: list[PlanIssue] = field(default_factory=list)

    @property
    def status(self) -> str:
        if any(i.severity == "error" for i in self.issues) or self.plan is None and not self.issues:
            return "invalid"
        if self.issues:
            return "needs_clarification"
        return "ready"


def norm_text(value: str) -> str:
    value = value.replace("“", '"').replace("”", '"').replace("‘", "'").replace("’", "'")
    return re.sub(r"\s+", " ", value).strip().lower()


def is_secret(value: str | None) -> bool:
    return bool(value and SECRET_RE.match(value.strip()))


def _clean(obj: Any, keep_empty: frozenset[str] = frozenset({"value", "expected"}), key: str = "") -> Any:
    """Strip strings; turn empty optional strings into None (except fields where "" is meaningful)."""
    if isinstance(obj, dict):
        return {k: _clean(v, keep_empty, k) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_clean(v, keep_empty, key) for v in obj]
    if isinstance(obj, str):
        s = obj.strip()
        return s if s or key in keep_empty else None
    return obj


def describe_target(t: Target) -> str:
    if t.description:
        return t.description
    label = t.name or t.label or t.placeholder or t.text
    if label:
        kind = {"button": "button", "link": "link", "checkbox": "checkbox", "tab": "tab", "heading": "heading",
                "radio": "option", "combobox": "dropdown"}.get(t.role or "", "field" if t.label or t.placeholder else "element")
        return f'the "{label}" {kind}'
    if t.testid:
        return f'the element with test id "{t.testid}"'
    if t.css:
        return f"the element matching {t.css}"
    return f"the {t.role} element" if t.role else "the element"


def describe_step(step: Step) -> str:
    t = step.target
    desc = describe_target(t) if t else ""
    a = step.action
    if a == "navigate":
        return f"Open {step.url}"
    if a == "click":
        return f"Click {desc}"
    if a == "hover":
        return f"Hover over {desc}"
    if a in {"check", "uncheck"}:
        return f"{a.capitalize()} {desc}"
    if a == "fill":
        if is_secret(step.value):
            return f"Enter secret {SECRET_RE.match(step.value.strip()).group(1)} into {desc}"
        if t and t.is_sensitive():
            return f"Enter a value into {desc}"
        if step.value_source == "generated":
            return f'Enter "{step.value}" (generated test data) into {desc}'
        return f'Enter "{step.value}" into {desc}'
    if a == "select":
        return f'Select "{step.value}" in {desc}'
    if a == "press":
        return f"Press {step.key}" + (f" in {desc}" if t else "")
    if a == "wait_for_text":
        return f'Wait for "{step.value}" to appear'
    assertion = step.assertion
    assert assertion is not None
    tdesc = describe_target(assertion.target) if assertion.target else ""
    exp = assertion.expected
    return {
        "text_visible": f'Verify the text "{exp}" is visible',
        "text_not_visible": f'Verify the text "{exp}" is not visible',
        "element_visible": f"Verify {tdesc} is visible",
        "element_not_visible": f"Verify {tdesc} is not visible",
        "element_text_contains": f'Verify {tdesc} contains "{exp}"',
        "input_value_equals": f'Verify {tdesc} has the value "{exp}"',
        "url_contains": f'Verify the URL contains "{exp}"',
        "title_contains": f'Verify the page title contains "{exp}"',
    }[assertion.type]


def action_class(action: str) -> str:
    return {"hover": "click", "uncheck": "check"}.get(action, action)


def target_fingerprint(action: str, target: Target) -> str:
    """Stable identity of "this intended element": same action class + same locator hints."""
    data = {k: norm_text(v) for k, v in target.hints().items()}
    return hashlib.sha1(dumps([action_class(action), data, target.exact]).encode()).hexdigest()[:20]


def _grounded(value: str, instruction_norm: str) -> bool:
    v = norm_text(value)
    return bool(v) and (v in instruction_norm or v.replace(" ", "") in instruction_norm.replace(" ", ""))


_QUOTED = re.compile(r'"([^"]*)"|(?<![\w])\'([^\']*)\'(?![\w])')


def _quoted_in(value: str, instruction_norm: str) -> bool:
    """Exact expected texts must be quoted by the user: "check the dashboard loads" must not
    become a check for the text "Dashboard"."""
    v = norm_text(value)
    return any(norm_text(a or b) == v for a, b in _QUOTED.findall(instruction_norm))


def validate_plan(
    raw: dict[str, Any],
    *,
    base_url: str,
    instruction: str,
    max_steps: int,
    known_urls: set[str] | frozenset[str] = frozenset(),
) -> ValidationResult:
    """known_urls: page identities (urls.page_identity) already observed in this project."""
    try:
        plan = Plan.model_validate(_clean({"steps": raw.get("steps", [])}))
    except ValidationError as exc:
        problems = "; ".join(
            f"{'.'.join(str(p) for p in e['loc'])}: {e['msg']}" for e in exc.errors()[:5]
        )
        return ValidationResult(None, [PlanIssue(code="schema", severity="error", message=f"Plan is malformed: {problems}")])

    issues: list[PlanIssue] = []
    steps = plan.steps
    if not steps:
        return ValidationResult(None, [PlanIssue(
            code="empty_plan", severity="clarification",
            message="No executable steps were found. Describe the actions to perform and the result to verify.",
        )])
    if steps[0].action != "navigate":
        steps.insert(0, Step(action="navigate", url="/"))
    if len(steps) > max_steps:
        issues.append(PlanIssue(code="too_many_steps", severity="error",
                                message=f"Plan has {len(steps)} steps; the limit is {max_steps}. Split the test."))

    instr = norm_text(instruction)
    generated: set[str] = set()  # test data the planner made up, which later checks may refer to
    for i, step in enumerate(steps, start=1):
        step.id = f"s{i}"

        def add(code: str, message: str, severity: Literal["clarification", "error"] = "error") -> None:
            issues.append(PlanIssue(code=code, message=message, severity=severity, step_id=step.id))

        if step.action != "assert" and step.assertion is not None:
            add("unexpected_assertion", "Only 'assert' steps may carry an assertion.")
        if step.action == "navigate":
            if not step.url:
                add("missing_url", "Navigation step has no URL.")
            else:
                absolute = urls.resolve(base_url, step.url)
                if not absolute.lower().startswith(("http://", "https://")) or not urls.same_origin(absolute, base_url):
                    add("cross_origin_navigation", f"'{step.url}' is outside the project's application origin.")
                else:
                    grounded = (
                        step.url == "/" or urls.page_identity(absolute) in known_urls
                        or _grounded(step.url, instr) or _grounded(step.url.strip("/"), instr)
                    )
                    if not grounded:
                        add("ungrounded_url", f"The URL '{step.url}' is neither in the instruction nor a known page. "
                            "State the path to open.", "clarification")
        if step.action in TARGET_ACTIONS or step.action == "press" and step.target:
            if step.target is None:
                add("missing_target", f"'{step.action}' step needs a target element.")
            else:
                _check_target(step.target, add)
        if step.action in {"fill", "select", "wait_for_text"}:
            if step.value is None:
                add("missing_value", f"'{step.action}' step needs a value.")
            elif step.value_source == "generated" and step.value != "":
                # Generated test data is fine for free-text input, but not for credentials (a made-up
                # password cannot log in) or dropdowns (the options are unknown before the run).
                if step.action != "fill":
                    add("ungrounded_value", f'"{step.value}" was guessed for a {step.action} step. Name the option '
                        "to choose; options are only known on the live page.", "clarification")
                elif is_secret(step.value) or (step.target is not None and step.target.is_sensitive()):
                    add("credentials_required", f"{describe_target(step.target) if step.target else 'This field'} "
                        "needs real credentials, which are never generated. Add them to the test, e.g. "
                        "{{secret:PASSWORD}} with UITEST_SECRET_PASSWORD set on the server.", "clarification")
                else:
                    generated.add(norm_text(step.value))
            elif step.value != "" and not _grounded(step.value, instr):  # secret placeholders too
                add("ungrounded_value", f'The value "{step.value}" does not appear in the instruction. '
                    "State the exact value to use (or a {{secret:NAME}} placeholder).", "clarification")
        if step.action == "press":
            if not step.key or not _KEY_RE.match(step.key):
                add("invalid_key", f"'{step.key}' is not a supported key.")
        if step.action == "assert":
            a = step.assertion
            if a is None:
                add("missing_assertion", "'assert' step has no assertion.")
            else:
                needs_expected, needs_target = ASSERTION_RULES[a.type]
                if needs_expected and not a.expected:
                    add("missing_expected", f"'{a.type}' assertion needs an expected value.")
                if needs_target and a.target is None:
                    add("missing_target", f"'{a.type}' assertion needs a target element.")
                if a.target is not None:
                    _check_target(a.target, add)
                if a.expected and needs_expected:
                    if a.type == "url_contains":
                        ok = _grounded(a.expected.strip("/"), instr) or _quoted_in(a.expected, instr)
                    else:
                        # Checking that generated data shows up is derived from the plan's own input,
                        # not an invented expectation.
                        ok = _quoted_in(a.expected, instr) or norm_text(a.expected) in generated
                    if not ok:
                        add("ungrounded_assertion",
                            f'The expected value "{a.expected}" is not quoted in the instruction. '
                            'Put the exact expected text in quotes, e.g. verify the page shows "Welcome".',
                            "clarification")
                if needs_target and a.target is not None:
                    hints = [v for k, v in a.target.hints().items() if k in ("name", "label", "placeholder", "text", "testid")]
                    if hints and not any(_grounded(h, instr) for h in hints):
                        add("ungrounded_assertion", f"The element {describe_target(a.target)} is not mentioned in the "
                            "instruction.", "clarification")
        if step.target is not None and step.action == "assert":
            add("unexpected_target", "Put the element to check inside the assertion, not on the step.")
        if not any(x.step_id == step.id and x.severity == "error" for x in issues):
            step.description = describe_step(step)
        elif not step.description:
            step.description = step.action

    if not any(s.action == "assert" for s in steps):
        issues.append(PlanIssue(
            code="missing_expected_outcome", severity="clarification",
            message="The instruction does not say what result to verify. Add an explicit expectation, "
                    'e.g. \'verify the page shows "Welcome"\' or \'verify the URL contains /dashboard\'.',
        ))
    return ValidationResult(plan, issues)


def _check_target(t: Target, add) -> None:
    if t.role and t.role not in ROLES:
        add("invalid_role", f"'{t.role}' is not a supported ARIA role.")
    if not any(getattr(t, k) for k in _HINTS):
        add("missing_locator", f"{describe_target(t)} needs a name, label, placeholder, test id, text or selector.")
