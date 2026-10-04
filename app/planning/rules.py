"""Deterministic rule-based planner - the fast, free, reproducible baseline.

It understands concrete phrasing such as:
    Go to /login, enter "ada@example.com" into the Email field, type {{secret:PASSWORD}} into Password,
    click the "Log in" button, then verify the page shows "Welcome back" and the URL contains /dashboard.

Anything it cannot map literally becomes a clarification issue (it never guesses).
Exact texts for text assertions must be quoted; element names may be unquoted."""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

from .plan import KEYS, PlanIssue

_Q = "\x00"
Q = rf"{_Q}\d+{_Q}"  # placeholder for a quoted string
_QUOTED = re.compile(r'"([^"]*)"|“([^”]*)”|(?<![\w])\'([^\']*)\'(?![\w])')
_VERB = (
    r"(?:go|navigate|browse|open|visit|load|click|tap|press|hit|type|enter|input|write|put|fill|set|select|choose|"
    r"pick|check|tick|uncheck|untick|hover|wait|verify|assert|ensure|expect|confirm|validate|make sure|"
    r"i should|it should|you should|the page should|the url should)"
)
# Clause starts that state an expectation without a leading "verify" (split after "and").
_ASSERT_SUBJECT = (
    r"(?:(?:the\s+)?(?:current\s+)?(?:url|address|location|page title|title)\b"
    r"|(?:the\s+)?(?:page|screen)\s+(?:shows?|displays?|contains?|includes?|has|does not|doesn't|should)\b"
    r"|(?:i|we|you|the user|user)\s+(?:can\s+|should\s+|do not\s+|don't\s+)?see\b"
    rf"|(?:the\s+)?(?:text\s+|message\s+)?{Q}\s+(?:is|isn't|appears|should|does)\b)"
)
_SPLIT = re.compile(
    rf"(?:[.;!?](?=\s|$)|\n|,?\s+and then\s+|,?\s+then\s+|,\s*(?:and\s+)?(?={_VERB}\b)"
    rf"|\s+and\s+(?={_VERB}\b)|,?\s+and\s+(?={_ASSERT_SUBJECT}))",
    re.IGNORECASE,
)
_LEAD = re.compile(r"^(?:and|then|first|next|finally|also|after that|afterwards|lastly)\b[\s,]*", re.IGNORECASE)
_KEY_ALIASES = {k.lower(): k for k in KEYS} | {"esc": "Escape", "return": "Enter", "space bar": "Space", "spacebar": "Space"}
_KIND_WORDS = [
    ("radio button", "radio"), ("check box", "checkbox"), ("menu item", "menuitem"), ("text box", None),
    ("drop-down", "combobox"), ("dropdown", "combobox"), ("button", "button"), ("link", "link"), ("tab", "tab"),
    ("checkbox", "checkbox"), ("radio", "radio"), ("option", "option"), ("heading", "heading"), ("image", "img"),
    ("select", "combobox"), ("textbox", None), ("textarea", None), ("field", None), ("input", None), ("box", None),
    ("icon", None),
]
_FIELD_WORDS = {"text box", "textbox", "textarea", "field", "input", "box"}


@dataclass
class PlanContext:
    base_url: str
    known_pages: list[tuple[str, str | None]] = field(default_factory=list)  # (page identity url, title)


@dataclass
class PlanDraft:
    raw: dict[str, Any] | None
    issues: list[PlanIssue]
    planner: str
    clarifications: list[str] = field(default_factory=list)


class _Unrecognized(Exception):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code, self.message = code, message


class RulePlanner:
    name = "rules"

    def plan(self, instruction: str, ctx: PlanContext) -> PlanDraft:
        protected, quotes = _protect(instruction)
        steps: list[dict[str, Any]] = []
        issues: list[PlanIssue] = []
        for clause in _split(protected):
            original = _restore(clause, quotes, keep_quotes=True)
            try:
                steps.append(self._parse_clause(clause, quotes, original, ctx))
            except _Unrecognized as exc:
                issues.append(PlanIssue(code=exc.code, severity="clarification", message=exc.message))
        return PlanDraft(raw={"steps": steps}, issues=issues, planner=self.name)

    # -- clause parsing ---------------------------------------------------------------

    def _parse_clause(self, c: str, quotes: list[str], original: str, ctx: PlanContext) -> dict[str, Any]:
        m = re.match(
            r"^(?:(?:verify|assert|ensure|expect|confirm|validate|make sure)\s+(?:that\s+|if\s+|whether\s+)?"
            r"|check\s+(?:that|if|whether)\s+)(?P<rest>.+)$", c, re.I,
        )
        if m or re.search(r"\bshould\b", c, re.I) or re.match(_ASSERT_SUBJECT, c, re.I):
            rest = m.group("rest") if m else c
            return {"action": "assert", "assertion": {**_parse_assertion(rest, quotes, original), "source": original}}

        if m := re.match(r"^(?:go|navigate|browse)\s+(?:back\s+)?to\s+(?P<u>.+)$|^(?:open|visit|load)\s+(?P<u2>.+)$", c, re.I):
            return {"action": "navigate", "url": _resolve_url(m.group("u") or m.group("u2"), quotes, ctx, original)}

        if m := re.match(r"^press\s+(?:the\s+)?(?P<k>[a-z+ ]+?)(?:\s+key)?(?:\s+(?:in|on|into|inside)\s+(?P<t>.+))?$", c, re.I):
            key = _key(m.group("k"))
            if key:
                step: dict[str, Any] = {"action": "press", "key": key}
                if m.group("t"):
                    step["target"] = _target(m.group("t"), quotes, "fill")
                return step

        if m := re.match(
            rf"^(?:type|enter|input|write|put)\s+(?P<v>{Q}|\S+)\s+(?:in|into|in to|on|inside)\s+(?P<t>.+)$", c, re.I
        ) or re.match(rf"^fill\s+(?:in\s+|out\s+)?(?P<t>.+?)\s+with\s+(?P<v>{Q}|\S+)$", c, re.I) or re.match(
            rf"^set\s+(?P<t>.+?)\s+to\s+(?P<v>{Q}|\S+)$", c, re.I
        ):
            return {"action": "fill", "value": _restore(m.group("v"), quotes), "target": _target(m.group("t"), quotes, "fill")}

        if m := re.match(rf"^(?:select|choose|pick)\s+(?P<v>{Q}|\S+)\s+(?:from|in|on|for)\s+(?P<t>.+)$", c, re.I):
            return {"action": "select", "value": _restore(m.group("v"), quotes), "target": _target(m.group("t"), quotes, "select")}

        if m := re.match(r"^(?:uncheck|untick|unmark)\s+(?P<t>.+)$", c, re.I):
            return {"action": "uncheck", "target": _target(m.group("t"), quotes, "check")}
        if m := re.match(r"^(?:check|tick|mark)\s+(?P<t>.+)$", c, re.I):
            # "check X" is a checkbox action only when the element is named explicitly;
            # "check the dashboard loads" is a (vague) expectation, not a tick.
            t = m.group("t")
            if re.match(rf"^(?:the\s+)?{Q}", t, re.I) or re.search(r"\b(?:checkbox|check box|radio|switch|toggle)\b", t, re.I):
                return {"action": "check", "target": _target(t, quotes, "check")}
            raise _Unrecognized(
                "ambiguous_check",
                f'"{original}" could mean ticking a checkbox or verifying something. For a checkbox say '
                'check the "Remember me" checkbox; for a verification say verify the page shows "...".',
            )

        if m := re.match(r"^(?:hover|mouse over|move the mouse over)\s+(?:over\s+|on\s+)?(?P<t>.+)$", c, re.I):
            return {"action": "hover", "target": _target(m.group("t"), quotes, "click")}

        if m := re.match(
            rf"^wait\s+(?:for|until)\s+(?:the\s+)?(?:text\s+|message\s+)?(?P<v>{Q})(?:\s+(?:to\s+)?(?:appear|appears|is visible|shows|is shown|to be visible))?$",
            c, re.I,
        ):
            return {"action": "wait_for_text", "value": _restore(m.group("v"), quotes)}

        if m := re.match(r"^(?:click|tap|press|hit|select|choose)\s+(?:on\s+)?(?P<t>.+)$", c, re.I):
            return {"action": "click", "target": _target(m.group("t"), quotes, "click")}

        raise _Unrecognized(
            "unrecognized_step",
            f'Could not interpret "{original}". Describe it as a concrete action (e.g. click the "Log in" button, '
            'enter "ada@example.com" into the Email field) or expectation (e.g. verify the page shows "Welcome").',
        )


# -- helpers ------------------------------------------------------------------------------

def _protect(text: str) -> tuple[str, list[str]]:
    quotes: list[str] = []

    def repl(m: re.Match) -> str:
        quotes.append(next(g for g in m.groups() if g is not None))
        return f"{_Q}{len(quotes) - 1}{_Q}"

    return _QUOTED.sub(repl, text), quotes


def _restore(s: str, quotes: list[str], keep_quotes: bool = False) -> str:
    return re.sub(
        rf"{_Q}(\d+){_Q}", lambda m: (f'"{quotes[int(m.group(1))]}"' if keep_quotes else quotes[int(m.group(1))]), s
    ).strip()


def _is_quoted(s: str) -> bool:
    return bool(re.fullmatch(Q, s.strip()))


def _split(text: str) -> list[str]:
    clauses = []
    for part in _SPLIT.split(text):
        part = part.strip(" ,")
        while True:
            stripped = _LEAD.sub("", part).strip(" ,")
            if stripped == part:
                break
            part = stripped
        if part:
            clauses.append(part)
    return clauses


def _key(raw: str) -> str | None:
    k = raw.strip().lower()
    if k in _KEY_ALIASES:
        return _KEY_ALIASES[k]
    if re.fullmatch(r"(?:(?:control|ctrl|shift|alt|meta|cmd)\s*\+\s*)+[a-z0-9]", k):
        parts = [p.strip() for p in k.split("+")]
        mods = {"ctrl": "Control", "control": "Control", "shift": "Shift", "alt": "Alt", "meta": "Meta", "cmd": "Meta"}
        return "+".join([mods[p] for p in parts[:-1]] + [parts[-1].upper()])
    return None


def _target(raw: str, quotes: list[str], action_cls: str) -> dict[str, Any]:
    text = raw.strip().rstrip(".")
    text = re.sub(r"^(?:the|a|an|on the|on)\s+", "", text, flags=re.I)
    role, is_field = None, action_cls in {"fill", "select"}
    lowered = text.lower()
    for word, word_role in _KIND_WORDS:
        if lowered.endswith(" " + word) or (lowered == word):
            text = text[: len(text) - len(word)].strip()
            role = word_role
            is_field = is_field or word in _FIELD_WORDS
            break
    name = _restore(text, quotes)
    name = re.sub(r"^(?:the|a|an)\s+", "", name, flags=re.I).strip()
    if not name:
        raise _Unrecognized("unclear_target", f'Could not tell which element "{_restore(raw, quotes, True)}" refers to. '
                            "Name it, e.g. the \"Save\" button.")
    exact = _is_quoted(text)
    if is_field and role is None:
        return {"label": name, "placeholder": name, "name": name, "exact": exact}
    if role in {"checkbox", "radio", "combobox"} or (action_cls == "check" and role is None):
        return {"role": role, "name": name, "label": name, "exact": exact} if role else {"name": name, "label": name, "exact": exact}
    if role:
        return {"role": role, "name": name, "exact": exact}
    return {"name": name, "text": name, "exact": exact}


def _resolve_url(raw: str, quotes: list[str], ctx: PlanContext, original: str) -> str:
    u = _restore(raw, quotes).strip().rstrip(".")
    if re.fullmatch(r"(?:https?://\S+|/\S*)", u, re.I):
        return u
    if re.fullmatch(r"[\w.-]+\.[a-z]{2,}(?:[:/]\S*)?", u, re.I):
        return "http://" + u
    m = re.fullmatch(r"(?:the\s+)?(?P<name>.+?)\s+(?:page|screen|view)", u, re.I)
    if m:
        name = m.group("name").lower()
        if name in {"home", "main", "landing", "start", "index", "front"}:
            return "/"
        token = re.sub(r"[^a-z0-9]", "", name)
        from urllib.parse import urlsplit

        matches = sorted({
            (urlsplit(url).path or "/") + (f"?{urlsplit(url).query}" if urlsplit(url).query else "")
            for url, title in ctx.known_pages
            if "{id}" not in url and token and (
                token in re.sub(r"[^a-z0-9]", "", urlsplit(url).path.lower())
                or (title and token in re.sub(r"[^a-z0-9]", "", title.lower()))
            )
        })
        if len(matches) == 1:
            return matches[0]
        detail = f"several known pages match ({', '.join(matches[:3])})" if matches else "no known page matches it"
        raise _Unrecognized("unknown_page", f'Which URL is "{m.group("name")} page"? {detail.capitalize()}. '
                            "Give the path, e.g. go to /login.")
    raise _Unrecognized("unknown_page", f'Could not tell which URL to open in "{original}". Give a path such as /login.')


def _parse_assertion(rest: str, quotes: list[str], original: str) -> dict[str, Any]:
    r = rest.strip().rstrip(".")
    r = re.sub(r"^(?:the\s+)?(?:it|this)\s+", "", r, flags=re.I)

    def text(group: str) -> str:
        if not _is_quoted(group):
            raise _Unrecognized(
                "vague_expectation",
                f'"{original}" does not quote the exact text to check. Put the expected text in quotes, '
                'e.g. verify the page shows "Welcome back".',
            )
        return _restore(group, quotes)

    patterns: list[tuple[str, str]] = [
        (r"^(?:the\s+)?(?:current\s+)?(?:url|address|location|path)\s+(?:should\s+)?(?:contains?|includes?|has|ends?\s+with|is|be|equals?|matches)\s+(?P<u>\S+)$", "url"),
        (r"^(?:i|we|you|the user|user)?\s*(?:am|are|is|should be|get|gets|will be)?\s*(?:redirected|taken|navigated|sent|brought|forwarded)\s+to\s+(?P<u>\S+)$", "url"),
        (r"^(?:the\s+)?(?:page\s+|document\s+|browser\s+)?title\s+(?:should\s+)?(?:be|is|contains?|includes?|equals?|reads?)\s+(?P<x>.+)$", "title_contains"),
        (r"^(?:i|we|you|the user|user)\s+(?:do not|don't|does not|doesn't|should not|shouldn't|cannot|can't|can not|no longer)\s+see\s+(?:the\s+)?(?:text\s+|message\s+)?(?P<x>.+)$", "text_not_visible"),
        (r"^(?:the\s+)?(?:page|screen|app|application)\s+(?:does not|doesn't|should not|shouldn't|no longer)\s+(?:show|display|contain|include|have)s?\s+(?:the\s+)?(?:text\s+|message\s+)?(?P<x>.+)$", "text_not_visible"),
        (rf"^(?:the\s+)?(?:text\s+|message\s+)?(?P<x>{Q})\s+(?:is not|isn't|is no longer|should not|shouldn't|does not|doesn't|no longer)\s*(?:be\s+)?(?:visible|shown|displayed|present|appears?|exists?)?(?:\s+on the page)?$", "text_not_visible"),
        (r"^(?:the\s+|a\s+|an\s+)?(?P<n>.+?)\s+(?P<k>button|link|checkbox|heading|tab|image|field|input|textbox)\s+(?:is not|isn't|should not be|shouldn't be|is no longer)\s+(?:visible|shown|displayed|present)$", "element_not_visible"),
        (r"^(?:the\s+|a\s+|an\s+)?(?P<n>.+?)\s+(?P<k>button|link|checkbox|heading|tab|image|field|input|textbox)\s+(?:is hidden|is gone|disappears|should be hidden|should disappear)$", "element_not_visible"),
        (r"^(?:the\s+|a\s+|an\s+)?(?P<n>.+?)\s+(?P<k>button|link|checkbox|heading|tab|image|field|input|textbox)\s+(?:is|should be|becomes|appears)\s*(?:visible|shown|displayed|present)?(?:\s+on the page)?$", "element_visible"),
        (r"^(?:the\s+)?(?P<n>.+?)\s+(?:field|input|box|textbox)\s+(?:should\s+)?(?:has|have|contains?|shows?)\s+(?:the\s+)?value\s+(?P<x>.+)$", "input_value_equals"),
        (r"^(?:the\s+)?value\s+of\s+(?:the\s+)?(?P<n>.+?)(?:\s+(?:field|input|box))?\s+(?:is|should be|equals)\s+(?P<x>.+)$", "input_value_equals"),
        (r"^(?:i|we|you|the user|user)\s+(?:can\s+|should\s+|will\s+)?see\s+(?:the\s+)?(?:text\s+|message\s+)?(?P<x>.+)$", "text_visible"),
        (r"^(?:the\s+)?(?:page|screen|app|application)\s+(?:should\s+)?(?:shows?|displays?|contains?|includes?|has|have|renders?)\s+(?:the\s+)?(?:text\s+|message\s+)?(?P<x>.+)$", "text_visible"),
        (rf"^(?:the\s+)?(?:text\s+|message\s+)?(?P<x>{Q})\s+(?:is|should be|appears|should appear|gets|becomes)?\s*(?:visible|shown|displayed|present)?(?:\s+on the page)?$", "text_visible"),
    ]
    for pattern, kind in patterns:
        m = re.match(pattern, r, re.I)
        if not m:
            continue
        if kind == "url":
            return {"type": "url_contains", "expected": _restore(m.group("u"), quotes).rstrip(".,")}
        if kind in {"element_visible", "element_not_visible"}:
            word = m.group("k").lower()
            role = {"button": "button", "link": "link", "checkbox": "checkbox", "heading": "heading", "tab": "tab",
                    "image": "img"}.get(word)
            name = _restore(m.group("n"), quotes)
            target = {"role": role, "name": name} if role else {"label": name, "placeholder": name}
            return {"type": kind, "target": target}
        if kind == "input_value_equals":
            name = _restore(m.group("n"), quotes)
            return {"type": kind, "expected": text(m.group("x")), "target": {"label": name, "placeholder": name, "name": name}}
        return {"type": kind, "expected": text(m.group("x"))}
    raise _Unrecognized(
        "vague_expectation",
        f'"{original}" does not state a checkable result. Quote the exact text that should appear '
        '(verify the page shows "Welcome"), or give the expected URL (verify the URL contains /dashboard).',
    )
