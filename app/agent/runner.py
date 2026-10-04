"""Agent-mode test run (browser-use style), with memory across runs of the same test.

1. Replay: if an earlier run passed, its remembered steps are replayed with their stored
   locators - fast and without the model. A remembered step whose value was not stored
   (e.g. a password) is handed to the agent for that single step ("agent assist").
   If every remembered check passes, the run passes with verdict_source=replay.
2. Agent loop: when there is no memory, the replay deviates (UI changed) or the remembered
   path has no checks, the model sees the live page and picks one action at a time until it
   declares the outcome (verdict_source=agent).
3. Memory: the run is stored (path that worked, agent-chosen values, notes, missing
   information, dead ends, replay deviations), redacted against sensitive values.

Result checking (see verification.py): the agent states what it expects before each action and
judges the result after it; the runner shows it what the action changed (texts, list items) and
does not accept a pass while any change is unchecked or a check failed.

Guardrails: step limit, repeated-action and consecutive-failure detection, per-decision
timeout with one retry, same-origin navigation, cancellation and the test time limit."""
from __future__ import annotations

import time
from collections import deque
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from playwright.sync_api import Error as PlaywrightError
from playwright.sync_api import Page

from .. import urls
from ..execution.dom import (INVENTORY_JS, LIST_ITEMS_JS, LIST_ITEMS_OF_JS, LISTS_JS, OVERLAY_BANNER_JS,
                             OVERLAY_BOXES_JS, OVERLAY_CLEAR_JS, PAGE_TEXT_JS)
from ..execution.executor import (NetworkQuiet, StepError, StepOutcome, _poll, classify, resolve_secret,
                                  stable_evaluate, text_visible)
from ..execution import layout as layout_mod
from ..execution.locators import locate_candidate, replacement_specs, resolve
from ..planning.llm import JsonLLM, LLMError, schema_for
from ..planning.plan import Step, is_secret
from ..repo import history as history_repo
from ..repo import runs as runs_repo
from ..util import now_iso
from . import memory as memory_repo
from .memory import Memory, redact
from .prompt import (ACTIONS, CHECK_ACTIONS, DECISION_SCHEMA, ELEMENT_ACTIONS, MAX_ELEMENTS_SHOWN, SYSTEM_PROMPT,
                     describe_action, element_label, is_sensitive, visible_elements, user_message)
from .verification import MAX_PUSHBACKS, Verification, contains, element_key

if TYPE_CHECKING:
    from ..execution.executor import RunContext, TestRunExecutor

_TRANSIENT = {"timeout", "degenerate", "truncated", "invalid_output", "connection", "rate_limited"}
_MAX_CONSECUTIVE_FAILURES = 4
_MAX_REPEATS = 3
_VALUE_ACTIONS = {"fill", "select", "press", "navigate", "verify_text"}
_INTERACTIONS = {"click", "fill", "select", "check", "uncheck", "press", "navigate"}  # may trigger requests


class AgentStop(Exception):
    def __init__(self, status: str, kind: str | None, message: str | None):
        super().__init__(message)
        self.status, self.kind, self.message = status, kind, message


@dataclass
class AgentState:
    step_index: int = 0
    agent_steps: int = 0
    llm_calls: int = 0
    history: list[str] = field(default_factory=list)
    trajectory: list[dict[str, Any]] = field(default_factory=list)
    agent_values: list[dict[str, Any]] = field(default_factory=list)
    dead_ends: list[dict[str, Any]] = field(default_factory=list)
    reasoning: list[dict[str, Any]] = field(default_factory=list)
    secrets: set[str] = field(default_factory=set)
    verified: bool = False
    checks: Verification = field(default_factory=Verification)
    note: str | None = None  # for the next decision (e.g. held back: check the last result first)
    boxes: dict[int, dict[str, Any]] = field(default_factory=dict)  # step index -> element box (layout)
    failures: int = 0
    recent: deque = field(default_factory=lambda: deque(maxlen=6))
    replay: dict[str, Any] = field(default_factory=dict)
    outcome: str | None = None
    summary: str | None = None
    notes: list[str] = field(default_factory=list)
    missing: list[str] = field(default_factory=list)


def _compact(el: dict[str, Any] | None) -> dict[str, Any] | None:
    if not el:
        return None
    keep = ("tag", "type", "role", "name", "label", "placeholder", "testid", "testidAttr", "id", "icon", "row",
            "rowIndex", "text", "css", "context")
    return {k: el[k] for k in keep if el.get(k) not in (None, "", 0)}


class AgentRunner:
    def __init__(self, executor: "TestRunExecutor", llm: JsonLLM):
        self.ex, self.llm = executor, llm
        self.db, self.settings = executor.db, executor.settings

    def run(self, page: Page, rc: "RunContext") -> tuple[str, str | None, str | None, str]:
        with self.db.read() as conn:
            memory = memory_repo.load(conn, rc.project_id, rc.test_case_id)
        st = AgentState(agent_values=list(memory.agent_values))
        status, kind, message, verdict_source = "error", "internal", None, "agent"
        try:
            note = None
            result = None
            self.quiet = NetworkQuiet(page, rc.base_url)
            self._open_start_page(page, rc, st)
            if self.settings.agent_replay and memory.path:
                result, note = self._replay(page, rc, st, memory)
            if result == "verified":
                status, kind, message, verdict_source = "passed", None, None, "replay"
                st.outcome, st.summary = "passed", "Replayed the remembered steps; every remembered check passed."
                st.notes = list(memory.notes)
            else:
                status, kind, message = self._agent_loop(page, rc, st, memory, note)
                if not st.notes:
                    st.notes = list(memory.notes)
        except AgentStop as stop:
            status, kind, message = stop.status, stop.kind, stop.message
        except LLMError as exc:
            status, kind, message = "error", "agent_llm_error", f"The agent's model failed: {exc}"
        except StepError as exc:
            status, kind, message = "error", exc.kind, exc.message
        except PlaywrightError as exc:
            status, kind, message = "error", classify(exc), str(exc).splitlines()[0][:500]
        message = redact(message, st.secrets)
        if status in ("error", "failed"):  # evidence of how the page looked when the run ended
            self.ex._screenshot(page, rc, max(st.step_index - 1, 0), "end")
        self._remember(rc, st, status, message)
        return status, kind, message, verdict_source

    def _open_start_page(self, page: Page, rc: "RunContext", st: AgentState) -> None:
        """Every agent run starts on the project URL (not part of the remembered path)."""
        started, t0 = now_iso(), time.monotonic()
        outcome = self.ex._navigate(page, rc, Step(action="navigate", url=rc.base_url))
        self._record(page, rc, st, action="navigate", description=f"Open {urls.sanitize_url(rc.base_url)}",
                     origin="start", outcome=outcome, spec=None, element=None, started=started, t0=t0,
                     url_before=rc.base_url)
        if outcome.status != "passed":
            raise AgentStop("error", outcome.error_kind or "navigation_failed", outcome.error_message)
        st.checks.start(f"Open {urls.sanitize_url(rc.base_url)}")

    # -- replay -----------------------------------------------------------------------------

    def _replay(self, page: Page, rc: "RunContext", st: AgentState, memory: Memory) -> tuple[str, str | None]:
        path = memory.path
        st.replay = {"sourceTestRunId": memory.source_test_run_id, "total": len(path), "replayed": 0, "assisted": 0,
                     "deviations": []}
        self.ex._event(rc, f"Replaying {len(path)} remembered step(s) from the last passing run.")
        for i, rec in enumerate(path):
            self._checkpoint(rc)
            self.ex._observe(page, rc, st.step_index)
            action = rec["action"]
            url_before, started, t0 = page.url, now_iso(), time.monotonic()
            element, spec, loc = None, None, None
            assist, changed = None, False
            if action in ("fill", "select") and (rec.get("sensitive") or rec.get("value") is None):
                assist = f'its value is not stored (it came from {rec.get("valueSource") or "an unknown source"})'
            elif rec.get("locators") and (action in ELEMENT_ACTIONS or action == "press"):
                # Only identity-based locators: a position-only css path could silently hit a different element.
                identity = [s for s in rec["locators"] if s.get("strategy") != "css"]
                res = resolve(page, identity, min(self.settings.action_timeout_ms, 4000)) if identity else None
                if res is None:
                    assist = "the remembered element has no stable identity"
                elif res.locator is None:
                    assist, changed = f"the remembered element was not found on the page ({res.failure})", True
                else:
                    loc, spec = res.locator, {**(res.spec or {}), "source": "replay"}
                    element = self.ex._describe(loc)
            if assist:
                # One targeted agent step for this remembered step, then replay continues.
                note = (f'REPLAY ASSIST: the next remembered step is "{rec["description"]}", but {assist}. '
                        "Perform this step on the current page now; the page may have changed.")
                decision, inventory, _ = self._decide(page, rc, st, memory, note)
                before = len(st.trajectory)
                if decision["action"] == "done":
                    return self._deviate(rc, st, i, rec, f"{assist}, and the agent did not perform it", path)
                if decision["action"] != action:
                    # The agent went another way: keep its action, but stop replaying a path that no longer lines up.
                    self._act(page, rc, st, decision, inventory, "agent")
                    return self._deviate(rc, st, i, rec, f"{assist}; the agent chose a different action "
                                                         f"({decision['action']}) than remembered ({action})", path)
                if not self._act(page, rc, st, decision, inventory, "agent_assist"):
                    return self._deviate(rc, st, i, rec, f"{assist}, and the agent could not perform it", path)
                st.checks.remembered(st.step_index - 1, rec.get("result"))
                st.replay["assisted"] += 1
                if changed:
                    instead = st.trajectory[before]["description"] if len(st.trajectory) > before else "another action"
                    st.replay["deviations"].append({"step": i + 1, "description": rec["description"],
                                                    "reason": redact(assist, st.secrets), "resolvedBy": instead})
                    self.ex._event(rc, f'Differs from memory at step {i + 1}: "{rec["description"]}" - {assist}. '
                                       f'The agent did "{instead}" instead.', "warning")
                continue
            value = self._value(st, rec.get("value")) if action == "fill" else rec.get("value")
            self.ex.show(page, OVERLAY_BANNER_JS, [f"Replay: {rec['description']}", "rgba(23,138,77,.92)"])
            before = self._snapshot(page) if action in _INTERACTIONS else None
            outcome = self._execute(page, rc, action, loc, value, rec["description"],
                                    sensitive=bool(rec.get("sensitive")) or is_secret(rec.get("value")),
                                    count=rec.get("count"), list_key=rec.get("listKey"))
            if action in _INTERACTIONS:
                self.quiet.wait()
            if action == "check_items":
                spec = outcome.locator
            self._record(page, rc, st, action=action, description=rec["description"], origin="replay", outcome=outcome,
                         spec=spec, element=element, started=started, t0=t0, url_before=url_before)
            if before is not None and outcome.status == "passed":
                self._track(page, st, before, action, rec["description"], None, element, frozenset(),
                            remembered=rec.get("result") or "no visible change then")
            if action in CHECK_ACTIONS and outcome.status in ("passed", "failed"):
                self._track_check(st, action, rec["description"], rec.get("value"), rec.get("count"), outcome)
            if outcome.status != "passed":
                reason = outcome.error_message or f"expected {outcome.expected}, got {outcome.actual}"
                label = "CHECK FAILED" if action in CHECK_ACTIONS and outcome.status == "failed" else "FAILED"
                st.history.append(f"{len(st.history) + 1}. {rec['description']} -> {label}: "
                                  f"{redact(reason, st.secrets)} (replayed from memory)")
                return self._deviate(rc, st, i, rec, reason, path)
            st.replay["replayed"] += 1
            st.history.append(f"{len(st.history) + 1}. {rec['description']} -> ok (replayed from memory)")
            fresh = _compact(element) or rec.get("element")
            st.trajectory.append({**rec, "step": st.step_index - 1, "page": urls.page_identity(url_before),
                                  "element": fresh,
                                  "locators": replacement_specs(element) if element else rec.get("locators", [])})
            if action in CHECK_ACTIONS:
                st.verified = True
        if any(s["action"] in CHECK_ACTIONS for s in path):
            return "verified", None
        return "completed", ("All remembered steps were replayed, but the last passing run recorded no check. "
                             "Check that the goal is achieved (verify_text / check_items), then finish.")

    def _deviate(self, rc: "RunContext", st: AgentState, i: int, rec: dict[str, Any], reason: str,
                 path: list[dict[str, Any]]) -> tuple[str, str]:
        reason = redact(reason, st.secrets)
        st.replay["deviations"].append({"step": i + 1, "description": rec["description"], "reason": reason,
                                        "resolvedBy": "handed over to the agent"})
        self.ex._event(rc, f"Replay differs from memory at remembered step {i + 1} ({rec['description']}): {reason}. "
                           "Handing over to the agent.", "warning")
        remaining = "; ".join(s["description"] for s in path[i + 1:i + 6])
        what = ("A remembered CHECK failed: this can be a regression. Judge from the page whether the application "
                "is wrong (then finish with outcome failed) or only its wording or data changed. "
                if rec["action"] in CHECK_ACTIONS else "The application may have changed; ")
        note = (f'Replaying the remembered steps stopped at step {i + 1} ("{rec["description"]}"): {reason}. '
                + what + "Continue towards the goal yourself."
                + (f" Remaining remembered steps: {remaining}." if remaining else ""))
        return "deviated", note

    # -- agent loop --------------------------------------------------------------------------

    def _agent_loop(self, page: Page, rc: "RunContext", st: AgentState, memory: Memory,
                    note: str | None) -> tuple[str, str | None, str | None]:
        while True:
            if st.agent_steps >= self.settings.agent_max_steps:
                st.outcome = "blocked"
                st.summary = f"No verdict after {self.settings.agent_max_steps} agent steps."
                return "error", "agent_step_limit", st.summary
            self._checkpoint(rc)
            self.ex._observe(page, rc, st.step_index)
            decision, inventory, text = self._decide(page, rc, st, memory, note or st.note)
            note, st.note = None, None
            st.agent_steps += 1
            st.checks.evaluate(decision.get("evaluation"))
            if decision["action"] != "done":
                self._act(page, rc, st, decision, inventory, "agent", text)
                continue
            if decision.get("outcome") == "passed" and st.checks.pushbacks < MAX_PUSHBACKS:
                note = st.checks.pushback_note()
                if note:  # not proven yet: say exactly what is missing and let the agent fix it
                    st.checks.pushbacks += 1
                    st.reasoning.append({"step": st.step_index, "origin": "agent", "action": "done (not accepted)",
                                         "thought": decision.get("thought"), "evaluation": decision.get("evaluation")})
                    self.ex._event(rc, "Pass not accepted yet: " + note, "warning")
                    continue
            st.outcome = decision.get("outcome") or "blocked"
            st.summary = decision.get("summary") or None
            st.notes = [n for n in decision.get("notes") or [] if n]
            st.missing = [m for m in decision.get("missing") or [] if m]
            st.reasoning.append({"step": st.step_index, "origin": "agent", "action": "done",
                                 "thought": decision.get("thought"), "evaluation": decision.get("evaluation")})
            if st.outcome == "passed" and (verdict := st.checks.verdict()):
                status, kind, message = verdict
                st.outcome = "failed" if status == "failed" else "blocked"
                st.summary = f"{message} (the agent claimed: {st.summary or 'passed'})"
                self.ex._event(rc, f"Agent's pass rejected: {redact(message, st.secrets)}", "warning")
                return status, kind, message
            self.ex._event(rc, f"Agent finished: {st.outcome}. {redact(st.summary or '', st.secrets)}")
            if st.outcome == "passed":
                return "passed", None, None
            if st.outcome == "failed":
                return "failed", "agent_reported_failure", st.summary or "The agent judged that the application did not behave as the goal requires."
            detail = (" Missing: " + "; ".join(st.missing)) if st.missing else ""
            return "error", "agent_blocked", (st.summary or "The agent could not continue.") + detail

    def _decide(self, page: Page, rc: "RunContext", st: AgentState, memory: Memory,
                note: str | None) -> tuple[dict[str, Any], dict[str, Any], str]:
        inventory = stable_evaluate(page, INVENTORY_JS, history_repo.MAX_ELEMENTS)
        text = self._text(page)
        message = user_message(rc.goal, memory.prompt_block(), st.history, page.url, inventory["title"], text,
                               inventory["elements"], st.agent_steps + 1, self.settings.agent_max_steps, note,
                               redact(st.checks.feedback(), st.secrets))
        schema = schema_for(self.llm, DECISION_SCHEMA)
        self.ex.show(page, OVERLAY_BOXES_JS,
                     [e["idx"] for e in visible_elements(inventory["elements"])[:MAX_ELEMENTS_SHOWN]])
        self.ex.show(page, OVERLAY_BANNER_JS, ["Agent is deciding the next step...", None])
        for attempt in (1, 2):
            try:
                st.llm_calls += 1
                decision = self.llm.complete_json(system=SYSTEM_PROMPT, user=message, schema=schema,
                                                  timeout_s=self.settings.agent_decision_timeout_s)
                if decision.get("action") not in ACTIONS:
                    raise LLMError("invalid_output", f"unknown action {decision.get('action')!r}")
                self.ex.show(page, OVERLAY_CLEAR_JS)
                return decision, inventory, text
            except LLMError as exc:
                if attempt == 2 or exc.kind not in _TRANSIENT:
                    raise
        raise AssertionError("unreachable")

    def _act(self, page: Page, rc: "RunContext", st: AgentState, decision: dict[str, Any], inventory: dict[str, Any],
             origin: str, text_at_decision: str | None = None) -> bool:
        action, value, source = decision["action"], decision.get("value"), decision.get("value_source")
        thought, count = decision.get("thought"), decision.get("count")
        el, loc, spec = None, None, None
        if action in ELEMENT_ACTIONS or (action in ("press", "check_items") and decision.get("element") is not None):
            idx = decision.get("element")
            el = next((e for e in inventory["elements"] if e.get("idx") == idx and e.get("visible")), None)
            if el is None:
                return self._invalid(rc, st, f"element [{idx}] is not in the current list of visible elements", thought)
            found = locate_candidate(page, el)
            if found:
                spec, loc = found
            else:  # no stable unique locator: act on the exact node, remember its css path
                loc = page.evaluate_handle("i => window.__uitestCands[i]", idx).as_element()
                spec = {"strategy": "css", "value": el.get("css", "")}
            spec = {**spec, "source": origin}
        if action in _VALUE_ACTIONS and not value:
            return self._invalid(rc, st, f"'{action}' needs a value", thought)
        if action == "check_items" and not value and count is None:
            return self._invalid(rc, st, "'check_items' needs a value (text every item must contain) or a count", thought)
        # A {{secret:NAME}} placeholder is safe to remember; the real value is substituted at run time.
        sensitive = action == "fill" and is_sensitive(el) and not is_secret(value)
        if sensitive and value:
            st.secrets.add(value)
        description = describe_action(action, el, value, sensitive, count)
        n = len(st.history) + 1
        if action in _INTERACTIONS and origin == "agent":
            held = st.checks.before_interaction(element_key(el))
            if held:  # once per change: the state to check is lost as soon as the agent acts again
                st.note = held
                st.history.append(f"{n}. {description} -> NOT DONE: check the result of the previous action first")
                st.reasoning.append({"step": st.step_index, "origin": origin, "action": f"held back: {description}",
                                     "thought": thought, "evaluation": decision.get("evaluation")})
                self.ex._event(rc, f'Agent held back from "{description}": the previous result is not checked yet.',
                               "warning")
                return False
        who = "Agent" if origin == "agent" else "Agent (assist)"
        self.ex._event(rc, f"{who}: {description}")
        self.ex.show(page, OVERLAY_BANNER_JS, [f"{who}: {description}", None])
        url_before, started, t0 = page.url, now_iso(), time.monotonic()
        before = None
        if action in _INTERACTIONS:
            before = self._snapshot(page)
            if text_at_decision is not None:
                st.checks.ambient(text_at_decision, before[0])
        try:
            typed = self._value(st, value) if action == "fill" else value
            outcome = self._execute(page, rc, action, loc, typed, element_label(el) if el else action,
                                    sensitive=sensitive or is_secret(value), count=count,
                                    list_key=None if loc is not None else self._changed_list(st))
            if action in _INTERACTIONS:
                self.quiet.wait()
        except StepError as exc:
            outcome = StepOutcome("error", error_kind=exc.kind, error_message=exc.message)
        if action == "check_items" and outcome.locator:
            spec = {**outcome.locator, "source": origin}
        self._record(page, rc, st, action=action, description=description, origin=origin, outcome=outcome, spec=spec,
                     element=el, started=started, t0=t0, url_before=url_before)
        st.reasoning.append({"step": st.step_index - 1, "origin": origin, "action": description, "thought": thought,
                             "expect": decision.get("expect"), "evaluation": decision.get("evaluation")})
        ok = outcome.status == "passed"
        why_not = None
        if action in CHECK_ACTIONS and outcome.status in ("passed", "failed"):
            why_not = self._track_check(st, action, description, value, count, outcome)
        if ok:
            st.failures = 0
            line = f"{n}. {description} -> " + ("check passed" if action in CHECK_ACTIONS else "done")
            if before is not None:
                effect = self._track(page, st, before, action, description, decision.get("expect"), el,
                                     frozenset(element_key(e) for e in inventory["elements"] if e.get("visible")),
                                     value=None if sensitive else value)
                line += "; the page changed (see LAST CHANGE)" if effect else "; no visible change"
            if action == "check_items" and outcome.actual:
                line += f" ({outcome.actual})"
            if why_not:
                line += f", but {why_not}"
            st.history.append(line)
            record = {
                "action": action, "description": description, "page": urls.page_identity(url_before),
                "element": _compact(el), "locators": replacement_specs(el) if el else [],
                "value": None if sensitive else value, "valueSource": source, "sensitive": sensitive,
                "step": st.step_index - 1,
            }
            if action == "check_items":
                record.update(count=count, listKey=(outcome.locator or {}).get("value"))
            st.trajectory.append(record)
            if action in CHECK_ACTIONS:
                st.verified = True
            if source == "agent_chosen" and value and not sensitive:
                label = element_label(el) if el else action
                st.agent_values = [v for v in st.agent_values if v["field"] != label] + [{"field": label, "value": value}]
        else:
            error = outcome.error_message or f"expected {outcome.expected}, got {outcome.actual}"
            label = "CHECK FAILED" if action in CHECK_ACTIONS and outcome.status == "failed" else "FAILED"
            st.history.append(f"{n}. {description} -> {label}: {redact(error, st.secrets)}")
            st.dead_ends = (st.dead_ends + [{"description": description, "error": error}])[-10:]
            st.failures += 1
        self._check_progress(st, action, description, page.url)
        return ok

    def _invalid(self, rc: "RunContext", st: AgentState, problem: str, thought: str | None) -> bool:
        st.history.append(f"{len(st.history) + 1}. INVALID CHOICE: {problem}")
        st.reasoning.append({"step": st.step_index, "origin": "agent", "action": "invalid", "thought": thought})
        st.failures += 1
        self._check_progress(st, "invalid", problem, "")
        return False

    def _check_progress(self, st: AgentState, action: str, description: str, url: str) -> None:
        signature = f"{action}|{description}|{urls.page_identity(url) if url.startswith('http') else url}"
        st.recent.append(signature)
        if list(st.recent).count(signature) >= _MAX_REPEATS and action not in ("wait", "scroll"):
            st.outcome, st.summary = "blocked", f"Repeated the same action without progress: {description}"
            raise AgentStop("error", "agent_stuck", st.summary)
        if st.failures >= _MAX_CONSECUTIVE_FAILURES:
            st.outcome, st.summary = "blocked", f"{st.failures} consecutive actions failed."
            raise AgentStop("error", "agent_stuck", st.summary)

    # -- shared --------------------------------------------------------------------------------

    @staticmethod
    def _value(st: AgentState, value: str | None) -> str | None:
        """Substitutes {{secret:NAME}} placeholders (from UITEST_SECRET_NAME) and redacts the result."""
        if not is_secret(value):
            return value
        real = resolve_secret(value)
        if real:
            st.secrets.add(real)
        return real

    def _execute(self, page: Page, rc: "RunContext", action: str, loc: Any, value: str | None,
                 what: str = "the element", sensitive: bool = False, count: int | None = None,
                 list_key: str | None = None) -> StepOutcome:
        if action in ELEMENT_ACTIONS or (action == "press" and loc is not None):
            step = Step(action=action, key=value if action == "press" else None, value=value)
            return self.ex._perform(page, step, loc, value, what, sensitive=sensitive)
        if action == "press":
            page.keyboard.press(value or "Enter")
            self.ex._settle(page)
            return StepOutcome("passed")
        if action == "navigate":
            return self.ex._navigate(page, rc, Step(action="navigate", url=value))
        if action == "scroll":
            page.mouse.wheel(0, 700)
            page.wait_for_timeout(400)
            return StepOutcome("passed")
        if action == "wait":
            page.wait_for_timeout(1500)
            return StepOutcome("passed")
        if action == "check_items":
            return self._check_items(page, loc, value, count, list_key)
        # verify_text: a deterministic check of what the agent claims (also across adjacent elements)
        ok = _poll(page, lambda: text_visible(page, value or ""), self.settings.action_timeout_ms)
        self.ex.show(page, OVERLAY_BANNER_JS, [f"Verified: \"{value}\"" if ok else f"Not visible: \"{value}\"",
                                               "rgba(23,138,77,.92)" if ok else "rgba(209,56,43,.92)"])
        return StepOutcome("passed" if ok else "failed", expected=f'text "{value}" visible',
                           actual="visible" if ok else "not visible on the page")

    def _check_items(self, page: Page, loc: Any, value: str | None, count: int | None,
                     list_key: str | None) -> StepOutcome:
        """Every visible item of one list contains value (and there are count items), polled until the
        action timeout: a deterministic check of the whole list, not of one text on the page."""
        expected = "; ".join(x for x in (f'every item contains "{value}"' if value else "",
                                         f"{count} item(s)" if count is not None else "") if x)
        if loc is not None:
            list_key = loc.evaluate(LIST_ITEMS_OF_JS)["key"]
        if not list_key:
            return StepOutcome("failed", expected=expected, actual="no list to check: the last action changed no "
                               "list; give an element of the list")
        state: dict[str, Any] = {}

        def holds() -> bool:
            state.update(stable_evaluate(page, LIST_ITEMS_JS, list_key))
            state["bad"] = [t for t in state["items"] if value and not contains(t, value)]
            return state["found"] and not state["bad"] and (count is None or len(state["items"]) == count)

        ok = _poll(page, holds, self.settings.action_timeout_ms)
        spec = {"strategy": "list", "value": list_key}
        if not state.get("found"):
            return StepOutcome("failed", expected=expected, actual="the list is no longer on the page", locator=spec)
        items, bad = state["items"], state.get("bad", [])
        actual = f"{len(items)} item(s)"
        if value and not bad:
            actual += f', all contain "{value}"' if items else " (the list is empty)"
        if bad:
            actual += f'; {len(bad)} without "{value}": ' + "; ".join(f'"{t[:90]}"' for t in bad[:4])
        self.ex.show(page, OVERLAY_BANNER_JS, [f"Checked {len(items)} item(s)" if ok else f"Check failed: {actual}",
                                               "rgba(23,138,77,.92)" if ok else "rgba(209,56,43,.92)"])
        return StepOutcome("passed" if ok else "failed", expected=expected, actual=actual[:500], locator=spec)

    # -- result tracking (see verification.py) ---------------------------------------------------

    @staticmethod
    def _text(page: Page) -> str:
        try:
            return stable_evaluate(page, PAGE_TEXT_JS) or ""
        except PlaywrightError:
            return ""

    def _snapshot(self, page: Page) -> tuple[str, dict[str, list]]:
        try:
            lists = stable_evaluate(page, LISTS_JS) or {}
        except PlaywrightError:
            lists = {}
        return self._text(page), lists

    def _track(self, page: Page, st: AgentState, before: tuple[str, dict[str, list]], action: str, description: str,
               expect: str | None, el: dict[str, Any] | None, before_keys: frozenset,
               remembered: str | None = None, value: str | None = None) -> Any:
        text_after, lists_after = self._snapshot(page)
        # what was selected: the chosen option or typed search text, else the clicked control's name
        control = value if action in ("select", "fill") and not is_secret(value) else (el or {}).get("name")

        def items(key: str) -> list[str]:
            try:
                return stable_evaluate(page, LIST_ITEMS_JS, key)["items"]
            except PlaywrightError:
                return []

        return st.checks.after_interaction(
            step=st.step_index - 1, description=description, action=action, expect=expect, key=element_key(el),
            before_keys=before_keys, text_before=before[0], text_after=text_after, lists_before=before[1],
            lists_after=lists_after, items=items, remembered=remembered, control=control)

    @staticmethod
    def _track_check(st: AgentState, action: str, description: str, value: str | None, count: int | None,
                     outcome: StepOutcome) -> str | None:
        detail = outcome.actual if outcome.status == "passed" else f"expected {outcome.expected}, got {outcome.actual}"
        return st.checks.record_check(kind=action, description=description, value=value, count=count,
                                      passed=outcome.status == "passed", detail=redact(detail, st.secrets),
                                      list_key=(outcome.locator or {}).get("value"),
                                      expected=redact(outcome.expected, st.secrets), actual=redact(outcome.actual, st.secrets))

    @staticmethod
    def _changed_list(st: AgentState) -> str | None:
        cur = st.checks.current
        return cur.list_key if cur else None

    def _record(self, page: Page, rc: "RunContext", st: AgentState, *, action: str, description: str, origin: str,
                outcome: StepOutcome, spec: dict[str, Any] | None, element: dict[str, Any] | None, started: str,
                t0: float, url_before: str) -> None:
        if outcome.status != "passed":
            self.ex._screenshot(page, rc, st.step_index, "failure")
        with self.db.tx() as conn:
            key, layout = layout_mod.track(conn, rc.project_id, rc.test_case_id, rc.test_run_id, action,
                                           layout_mod.identity_of(element), outcome.geometry,
                                           self.settings.layout_tolerance_px)
            runs_repo.insert_step_result(
                conn, rc.project_id, rc.test_run_id, step_index=st.step_index, step_key=f"s{st.step_index + 1}",
                action="assert" if action in CHECK_ACTIONS else action,
                kind="assertion" if action in CHECK_ACTIONS else "action", description=description,
                status=outcome.status, locator=spec, expected=outcome.expected, actual=outcome.actual,
                detail=redact(outcome.detail, st.secrets), error_kind=outcome.error_kind,
                error_message=redact(outcome.error_message, st.secrets), page_id=rc.page_id,
                page_url=urls.sanitize_url(url_before) if url_before.startswith("http") else None,
                element=_compact(element), started_at=started, finished_at=now_iso(),
                duration_ms=int((time.monotonic() - t0) * 1000), origin=origin, layout_key=key, layout=layout,
            )
        self.ex.layout_event(rc, description, layout)
        if layout:
            st.boxes[st.step_index] = layout["box"]
        st.step_index += 1

    def _checkpoint(self, rc: "RunContext") -> None:
        with self.db.tx() as conn:
            cancel = runs_repo.heartbeat(conn, rc.project_id, rc.test_run_id)
        if cancel:
            raise AgentStop("cancelled", "cancelled", "Cancelled by user.")
        if time.monotonic() > rc.deadline:
            raise AgentStop("error", "timeout", f"The test exceeded its {self.settings.test_timeout_s}s time limit.")

    def _remember(self, rc: "RunContext", st: AgentState, status: str, message: str | None) -> None:
        outcome = {"passed": "passed", "failed": "failed", "cancelled": "cancelled"}.get(status)
        if outcome is None:
            outcome = "blocked" if st.outcome == "blocked" else "error"
        by_step = {e.step: e.status for e in st.checks.effects}
        for rec in st.trajectory:
            if rec.get("step") in by_step:
                rec["result"] = by_step[rec["step"]]
            if rec.get("step") in st.boxes:
                rec["box"] = st.boxes[rec["step"]]
        details = {"agentValues": st.agent_values, "deadEnds": st.dead_ends, "replay": st.replay,
                   "reasoning": st.reasoning[-60:], "llmCalls": st.llm_calls, "agentSteps": st.agent_steps,
                   "verification": st.checks.details()[-40:], "pushbacks": st.checks.pushbacks,
                   "gate": 1}  # recorded with result checking: its path may be replayed
        with self.db.tx() as conn:
            memory_repo.record(conn, rc.project_id, test_case_id=rc.test_case_id, revision_id=rc.revision_id,
                               test_run_id=rc.test_run_id, outcome=outcome, summary=st.summary or message,
                               verified=st.verified, trajectory=st.trajectory, notes=st.notes, missing=st.missing,
                               details=details, secrets=st.secrets)
