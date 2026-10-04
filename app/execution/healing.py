"""Conservative locator healing: candidate filtering, scoring and the decision rule.

Pure functions (no browser) so every rule is unit-testable. The executor supplies the
live candidates and the historical reference, then acts on the Decision.

Rules:
  1. Hard filters: a candidate must be visible and compatible with the intended action
     (e.g. editable for fill, a checkbox/radio/switch for check, enabled for click).
  2. Score = weighted similarity to the target's declared hints and to the element that
     last matched this target on the same page (same project only; ignored if stale).
  3. A candidate without identity evidence (name/text, test id or id similarity) is never
     plausible - matching only on role or position is not enough. Conversely, the exact test
     id of the element that last passed this step (on a compatible element) is strong
     evidence on its own (score floor 0.85), because test ids are deliberate and stable.
  4. Select only if best >= min_confidence AND no other plausible candidate is within
     min_margin of it. Otherwise report "ambiguous" or "no_confident_candidate"."""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from difflib import SequenceMatcher
from typing import Any

from ..planning.plan import Target, action_class

WEIGHTS = {"name": 0.40, "role": 0.15, "testid": 0.15, "context": 0.15, "attributes": 0.10, "tag": 0.05}
CLICK_ROLES = {"button", "link", "menuitem", "tab", "checkbox", "radio", "switch", "option", "treeitem", "combobox"}
_SYNONYMS = [
    ("log in", "login", "sign in", "signin", "log on", "logon"),
    ("log out", "logout", "sign out", "signout", "log off"),
    ("sign up", "signup", "register", "create account", "create an account"),
    ("submit", "send"),
    ("delete", "remove"),
    ("close", "dismiss"),
    ("next", "continue", "proceed"),
    ("cart", "basket", "bag"),
    ("search", "find"),
    ("email", "e mail", "email address"),
    ("username", "user name"),
]
_CANON: list[tuple[re.Pattern[str], str]] = []
for group in _SYNONYMS:
    canonical = group[0].replace(" ", "_")
    for phrase in sorted(group, key=len, reverse=True):
        _CANON.append((re.compile(r"\b" + re.escape(phrase) + r"\b"), canonical))


def canon(text: str) -> str:
    t = re.sub(r"[^a-z0-9]+", " ", (text or "").lower()).strip()
    for pattern, replacement in _CANON:
        t = pattern.sub(replacement, t)
    return re.sub(r"\s+", " ", t).strip()


def text_sim(a: str, b: str) -> float:
    a, b = canon(a), canon(b)
    if not a or not b:
        return 0.0
    if a == b:
        return 1.0
    ta, tb = set(a.split()), set(b.split())
    jaccard = len(ta & tb) / len(ta | tb)
    return max(jaccard, 0.9 * SequenceMatcher(None, a, b).ratio())


def token_sim(a: str, b: str) -> float:
    """For identifiers: split camelCase / kebab / snake into words, then compare."""
    def words(s: str) -> str:
        return " ".join(re.sub(r"([a-z0-9])([A-Z])", r"\1 \2", s or "").replace("-", " ").replace("_", " ").split())
    return text_sim(words(a), words(b))


def compatible(action: str, c: dict[str, Any]) -> tuple[bool, str]:
    if not c.get("visible"):
        return False, "not visible"
    cls = action_class(action)
    role, tag = c.get("role") or "", c.get("tag") or ""
    clickable = role in CLICK_ROLES or tag in {"button", "a", "summary"} or bool(c.get("clickable"))
    if cls in {"click", "hover"}:
        if not clickable:
            return False, "not clickable"
        if cls == "click" and not c.get("enabled", True):
            return False, "disabled"
    elif cls == "fill":
        if not c.get("editable"):
            return False, "not editable"
    elif cls == "select":
        if tag != "select" and role not in {"combobox", "listbox"}:
            return False, "not a select/combobox"
        if not c.get("enabled", True):
            return False, "disabled"
    elif cls == "check":
        if role not in {"checkbox", "radio", "switch"}:
            return False, "not a checkbox/radio/switch"
        if not c.get("enabled", True):
            return False, "disabled"
    elif cls == "press":
        if not (c.get("editable") or clickable):
            return False, "not focusable"
    return True, ""


@dataclass
class Reference:
    action: str
    target: Target
    history: dict[str, Any] | None = None  # element snapshot from the last clean success


@dataclass
class Scored:
    candidate: dict[str, Any]
    score: float
    signals: dict[str, float]
    identity: float

    def summary(self) -> dict[str, Any]:
        c = self.candidate
        return {
            "score": round(self.score, 3),
            "signals": {k: round(v, 3) for k, v in self.signals.items()},
            "element": {k: c.get(k) for k in ("tag", "type", "role", "name", "label", "placeholder", "testid", "id", "css")},
            "context": c.get("context"),
        }


def score(ref: Reference, c: dict[str, Any]) -> Scored:
    t, h = ref.target, ref.history or {}
    signals: dict[str, float] = {}
    ref_names = [v for v in (t.name, t.label, t.text, t.placeholder, h.get("name"), h.get("label")) if v]
    if ref_names:
        cand_names = [v for v in (c.get("name"), c.get("label"), c.get("text"), c.get("placeholder")) if v]
        signals["name"] = max((text_sim(r, n) for r in ref_names for n in cand_names), default=0.0)
    ref_role = t.role or h.get("role")
    if ref_role:
        cls_roles = {"click": CLICK_ROLES, "fill": {"textbox", "searchbox", "combobox", "spinbutton"},
                     "select": {"combobox", "listbox"}, "check": {"checkbox", "radio", "switch"}}.get(action_class(ref.action), set())
        signals["role"] = 1.0 if c.get("role") == ref_role else 0.5 if c.get("role") in cls_roles else 0.0
    ref_testid = t.testid or h.get("testid")
    if ref_testid:
        signals["testid"] = token_sim(ref_testid, c.get("testid") or "") if c.get("testid") else 0.0
    attr_pairs = [(h.get("id"), c.get("id")), (h.get("nameAttr"), c.get("nameAttr"))]
    if any(a for a, _ in attr_pairs):
        signals["attributes"] = max((token_sim(a, b) if a and b else 0.0) for a, b in attr_pairs)
    hctx = {k: v for k, v in (h.get("context") or {}).items() if v}
    if hctx:
        cctx = c.get("context") or {}
        signals["context"] = sum(1.0 for k, v in hctx.items() if text_sim(v, cctx.get(k) or "") >= 0.8) / len(hctx)
    if h.get("tag"):
        signals["tag"] = 1.0 if (c.get("tag"), c.get("type")) == (h.get("tag"), h.get("type")) else 0.5 if c.get("tag") == h.get("tag") else 0.0
    total_w = sum(WEIGHTS[k] for k in signals)
    value = sum(WEIGHTS[k] * v for k, v in signals.items()) / total_w if total_w else 0.0
    identity = max(
        signals.get("name", 0.0),
        signals.get("testid", 0.0) if signals.get("testid", 0.0) >= 0.8 else 0.0,
        signals.get("attributes", 0.0) if signals.get("attributes", 0.0) >= 0.9 else 0.0,
    )
    if identity < 0.5:
        value = min(value, 0.45)  # no identity evidence -> never plausible
    anchor = h.get("testid") or t.testid
    if anchor and c.get("testid") == anchor and signals.get("role", 1.0) > 0:
        # The exact developer-assigned test id of the element that last passed this step, on a
        # compatible element: strong identity even if the visible text changed.
        value = max(value, 0.85)
        signals["testidAnchor"] = 1.0
    return Scored(c, value, signals, identity)


@dataclass
class Decision:
    outcome: str  # selected | ambiguous | no_confident_candidate
    selected: Scored | None
    ranked: list[Scored]
    reason: str
    excluded: dict[str, int] = field(default_factory=dict)

    def top(self, n: int = 5) -> list[dict[str, Any]]:
        return [s.summary() for s in self.ranked[:n]]


def decide(
    ref: Reference, candidates: list[dict[str, Any]], *, min_confidence: float, min_margin: float, plausible: float,
) -> Decision:
    excluded: dict[str, int] = {}
    scored: list[Scored] = []
    for c in candidates:
        ok, why = compatible(ref.action, c)
        if not ok:
            excluded[why] = excluded.get(why, 0) + 1
            continue
        scored.append(score(ref, c))
    scored.sort(key=lambda s: (-s.score, s.candidate.get("css") or "", s.candidate.get("idx", 0)))
    if not scored:
        return Decision("no_confident_candidate", None, [], "No visible element is compatible with the action.", excluded)
    best = scored[0]
    rivals = [s for s in scored[1:] if s.score >= plausible]
    if best.score < min_confidence:
        return Decision("no_confident_candidate", None, scored,
                        f"Best candidate scored {best.score:.2f}, below the {min_confidence:.2f} threshold.", excluded)
    if rivals and best.score - rivals[0].score < min_margin:
        return Decision("ambiguous", None, scored,
                        f"{len(rivals) + 1} candidates are plausible; the best ({best.score:.2f}) leads the next "
                        f"({rivals[0].score:.2f}) by less than {min_margin:.2f}.", excluded)
    return Decision("selected", best, scored, _explain(ref, best, rivals), excluded)


def _explain(ref: Reference, best: Scored, rivals: list[Scored]) -> str:
    c, s = best.candidate, best.signals
    parts = [f'Selected {c.get("role") or c.get("tag")} "{c.get("name") or c.get("text") or c.get("css")}" '
             f"(score {best.score:.2f}"]
    parts[0] += f", next plausible {rivals[0].score:.2f})" if rivals else ", no other plausible candidate)"
    if s.get("name", 0) >= 0.99:
        parts.append("accessible name matches the target (including known synonyms)")
    elif s.get("name", 0) > 0:
        parts.append(f"name similarity {s['name']:.2f}")
    if s.get("role") == 1.0:
        parts.append("same role")
    if s.get("testid", 0) >= 0.8:
        parts.append("test id matches history")
    if s.get("context") == 1.0:
        parts.append("same form/section as the last successful run")
    if ref.history is None:
        parts.append("no usable page history for this target")
    return "; ".join(parts) + "."
