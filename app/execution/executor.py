"""Coordinator for one test run (the "primary agent").

Executes a validated plan step by step in its own isolated browser context (actions in a
context are strictly sequential), records every step, captures page history and evidence,
and invokes conservative healing when a declared locator fails. Assertions are never
healed or altered. The outcome distinguishes:
  failed -> an explicit assertion did not hold (application behavior);
  error  -> no verdict: locator/agent, browser or infrastructure problem (see error_kind)."""
from __future__ import annotations

import logging
import os
import re
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from playwright.sync_api import Browser, BrowserContext, Locator, Page
from playwright.sync_api import Error as PlaywrightError
from playwright.sync_api import TimeoutError as PlaywrightTimeoutError

from .. import urls
from ..config import Settings
from ..db import Database
from ..planning.llm import JsonLLM
from ..planning.plan import SECRET_RE, Plan, Step, describe_target, is_secret, norm_text, target_fingerprint
from ..repo import artifacts as artifacts_repo
from ..repo import healing as healing_repo
from ..repo import history as history_repo
from ..repo import projects as projects_repo
from ..repo import runs as runs_repo
from ..repo import tests as tests_repo
from ..util import loads, now_iso, parse_iso
from .dom import DESCRIBE_JS, INVENTORY_JS, OVERLAY_BANNER_JS, OVERLAY_CLEAR_JS, OVERLAY_POINT_JS, PAGE_HAS_TEXT_JS
from .finalize import finalize_test_run
from .healing import Decision, Reference, decide
from . import layout as layout_mod
from .locators import count_visible, declared_specs, describe_spec, locate_candidate, resolve

log = logging.getLogger(__name__)

_CRASH_MARKERS = ("has been closed", "browser has disconnected", "Target closed", "Target crashed", "Page crashed")


class StepError(Exception):
    def __init__(self, kind: str, message: str):
        super().__init__(message)
        self.kind, self.message = kind, message


@dataclass
class StepOutcome:
    status: str  # passed | failed | error
    expected: str | None = None
    actual: str | None = None
    detail: str | None = None
    error_kind: str | None = None
    error_message: str | None = None
    locator: dict[str, Any] | None = None
    healed: bool = False
    element: dict[str, Any] | None = None
    fingerprint: str | None = None
    geometry: dict[str, Any] | None = None  # where the acted-on element was (layout tracking)


@dataclass
class RunContext:
    project_id: str
    run_id: str
    test_run_id: str
    test_case_id: str
    base_url: str
    plan: Plan | None
    run_dir: Path
    deadline: float
    mode: str = "plan"
    goal: str = ""
    revision_id: str = ""
    healing_used: int = 0
    healed_steps: int = 0
    page_key: str | None = None
    page_id: str | None = None


def classify(exc: BaseException) -> str:
    msg = str(exc)
    if any(m in msg for m in _CRASH_MARKERS):
        return "browser_crash"
    if "net::ERR_" in msg or "NS_ERROR_" in msg:
        return "navigation_failed"
    return "action_failed"


_NAVIGATION_RACE = ("Execution context was destroyed", "Cannot find context with specified id", "frame was detached",
                    "because of a navigation")


def stable_evaluate(page: Page, expression: str, arg: Any = None, attempts: int = 4) -> Any:
    """page.evaluate that survives a navigation starting or finishing mid-call (common right after
    a click that submits a form in the background): wait for the new document, then try again."""
    for attempt in range(attempts):
        try:
            return page.evaluate(expression) if arg is None else page.evaluate(expression, arg)
        except PlaywrightError as exc:
            if attempt == attempts - 1 or not any(m in str(exc) for m in _NAVIGATION_RACE):
                raise
            try:
                page.wait_for_load_state("domcontentloaded", timeout=10000)
            except PlaywrightError:
                pass
            page.wait_for_timeout(250)
    raise AssertionError("unreachable")


class NetworkQuiet:
    """Tracks the page's in-flight requests so a caller can wait until the app has settled after
    an action - e.g. a login that validates in the background and only then navigates. Waiting
    for the load state is not enough there: the old document already counts as loaded."""

    # Only the app's own page loads and API calls decide when the page has settled. Third-party
    # traffic (map tiles, analytics, CDNs), scripts, images and streams can run forever on busy
    # apps (e.g. a 3D globe keeps ~40 requests/s in flight) and must not block every step.
    _TRACKED = {"document", "xhr", "fetch"}

    def __init__(self, page: Page, origin: str | None = None):
        self.page, self.pending, self.last_activity = page, set(), time.monotonic()
        self.origin = urls.origin(origin) if origin else None
        page.on("request", self._started)
        page.on("requestfinished", self._ended)
        page.on("requestfailed", self._ended)
        page.on("framenavigated", self._navigated)

    @property
    def inflight(self) -> int:
        return len(self.pending)

    def _touch(self, *_: Any) -> None:
        self.last_activity = time.monotonic()

    def _tracked(self, request: Any) -> bool:
        if request.resource_type not in self._TRACKED:
            return False
        return self.origin is None or urls.origin(request.url) == self.origin

    def _started(self, request: Any) -> None:
        if self._tracked(request):
            self.pending.add(request)
            self._touch()

    def _ended(self, request: Any) -> None:
        if request in self.pending:
            self.pending.discard(request)
            self._touch()

    def _navigated(self, frame: Any) -> None:
        # Requests of the replaced document never report finishing once it is gone.
        if frame == self.page.main_frame:
            self.pending.clear()
        self._touch()

    def wait(self, quiet_ms: int = 500, timeout_ms: int = 5000) -> None:
        """Returns once nothing is in flight and nothing happened for quiet_ms (at most timeout_ms,
        so apps that poll constantly do not stall the run)."""
        deadline = time.monotonic() + timeout_ms / 1000
        self._touch()  # a request triggered by the action may not have been announced yet
        while time.monotonic() < deadline:
            if self.inflight == 0 and time.monotonic() - self.last_activity >= quiet_ms / 1000:
                break
            try:
                self.page.wait_for_timeout(100)
            except PlaywrightError:
                return
        try:
            self.page.wait_for_load_state("domcontentloaded", timeout=5000)
        except PlaywrightError:
            pass


def text_visible(page: Page, text: str) -> bool:
    """Visible in one element (Playwright text match) or across adjacent elements of the rendered
    page text, e.g. "73 warning Overall health" shown as three separate labels."""
    if page.get_by_text(text).filter(visible=True).count() > 0:
        return True
    try:
        return bool(stable_evaluate(page, PAGE_HAS_TEXT_JS, text))
    except PlaywrightError:
        return False


def resolve_secret(value: str | None) -> str | None:
    m = SECRET_RE.match(value.strip()) if value else None
    if not m:
        return value
    secret = os.environ.get(f"UITEST_SECRET_{m.group(1)}")
    if secret is None:
        raise StepError("missing_secret", f"Secret {m.group(1)} is not configured on the server "
                                          f"(set the environment variable UITEST_SECRET_{m.group(1)}).")
    return secret


def _poll(page: Page, fn: Callable[[], bool], timeout_ms: int, interval_ms: int = 100) -> bool:
    deadline = time.monotonic() + timeout_ms / 1000
    while True:
        try:
            if fn():
                return True
        except PlaywrightTimeoutError:
            pass
        except PlaywrightError as exc:
            if classify(exc) == "browser_crash":
                raise
        if time.monotonic() >= deadline:
            return False
        page.wait_for_timeout(interval_ms)


class TestRunExecutor:
    def __init__(self, db: Database, settings: Settings, ranker: Callable[..., Decision] = decide,
                 llm: JsonLLM | None = None):
        # ranker: replaceable candidate-selection strategy with decide()'s signature
        # (see evals/healing_ranker_eval.py for the comparison an alternative must pass).
        # llm: model client for agent-mode tests (None: agent tests end with a clear error).
        self.db, self.settings, self.ranker, self.llm = db, settings, ranker, llm

    # -- entry point ---------------------------------------------------------------------

    def execute(self, browser: Browser, project_id: str, test_run_id: str) -> str:
        rc: RunContext | None = None
        status, kind, message = "error", "internal", "The runner failed before executing the test."
        verdict_source: str | None = None
        context: BrowserContext | None = None
        try:
            rc = self._load(project_id, test_run_id)
            if rc.mode == "agent" and self.llm is None:
                raise StepError("agent_llm_error", "Agent-mode tests need a model; configure UITEST_LLM_PROVIDER "
                                                   "(or ANTHROPIC_API_KEY) on the server.")
            context = self._new_context(browser, rc)
            page = context.new_page()
            if rc.mode == "agent":
                from ..agent.runner import AgentRunner

                self._event(rc, "Started in agent mode.")
                status, kind, message, verdict_source = AgentRunner(self, self.llm).run(page, rc)
            else:
                self._event(rc, f"Started (revision plan with {len(rc.plan.steps)} steps).")
                status, kind, message = self._run_steps(page, rc)
                verdict_source = "assertions" if status in ("passed", "failed") else None
        except StepError as exc:
            status, kind, message = "error", exc.kind, exc.message
        except PlaywrightError as exc:
            status, kind, message = "error", classify(exc), str(exc).splitlines()[0][:500]
        except Exception as exc:  # a bug in the runner must not leave the run hanging
            log.exception("test run %s crashed", test_run_id)
            status, kind, message = "error", "internal", f"{type(exc).__name__}: {exc}"[:500]
        finally:
            if context is not None and rc is not None:
                self._close_context(context, rc, status)
        finalize_test_run(self.db, project_id, test_run_id, status, kind if status != "passed" else None,
                          message if status != "passed" else None, rc.healed_steps if rc else 0, verdict_source)
        return status

    def _load(self, project_id: str, test_run_id: str) -> RunContext:
        with self.db.read() as conn:
            tr = runs_repo.get_test_run(conn, project_id, test_run_id)
            project = projects_repo.get_project(conn, project_id)
            revision = tests_repo.get_revision(conn, project_id, tr["revision_id"])
        plan_json = loads(revision["plan_json"])
        mode = revision["mode"]
        if revision["plan_status"] != "ready" or (mode == "plan" and not plan_json):
            raise StepError("invalid_plan", "The test revision has no runnable plan.")
        run_dir = self.settings.artifacts_dir / project_id / test_run_id
        run_dir.mkdir(parents=True, exist_ok=True)
        return RunContext(
            project_id=project_id, run_id=tr["run_id"], test_run_id=test_run_id, test_case_id=tr["test_case_id"],
            base_url=project["base_url"], plan=Plan.model_validate(plan_json) if plan_json else None,
            run_dir=run_dir, mode=mode,
            deadline=time.monotonic() + (self.settings.agent_test_timeout_s if mode == "agent" else self.settings.test_timeout_s),
            goal=revision["instruction"], revision_id=revision["id"],
        )

    def _new_context(self, browser: Browser, rc: RunContext) -> BrowserContext:
        opts: dict[str, Any] = {"viewport": {"width": 1280, "height": 800}}
        if self.settings.video:
            opts["record_video_dir"] = str(rc.run_dir / "video")
        context = browser.new_context(**opts)  # isolated cookies/storage per test run
        context.set_default_timeout(self.settings.action_timeout_ms)
        context.set_default_navigation_timeout(self.settings.navigation_timeout_ms)
        if self.settings.trace_mode != "off":
            context.tracing.start(screenshots=True, snapshots=True)
        return context

    # -- step loop -----------------------------------------------------------------------

    def _run_steps(self, page: Page, rc: RunContext) -> tuple[str, str | None, str | None]:
        steps = rc.plan.steps
        for index, step in enumerate(steps):
            with self.db.tx() as conn:
                cancel = runs_repo.heartbeat(conn, rc.project_id, rc.test_run_id)
            if cancel:
                self._skip_rest(rc, steps, index, "Cancelled by user.")
                return "cancelled", "cancelled", "Cancelled by user."
            if time.monotonic() > rc.deadline:
                self._skip_rest(rc, steps, index, "Test time limit reached.")
                return "error", "timeout", f"The test exceeded its {self.settings.test_timeout_s}s time limit."

            self._event(rc, f"Step {index + 1}/{len(steps)}: {step.description}")
            self._observe(page, rc, index)
            started, t0 = now_iso(), time.monotonic()
            try:
                outcome = self._run_step(page, rc, index, step)
            except StepError as exc:
                outcome = StepOutcome("error", error_kind=exc.kind, error_message=exc.message)
            except PlaywrightTimeoutError as exc:
                outcome = StepOutcome("error", error_kind="action_failed", error_message=str(exc).splitlines()[0][:500])
            except PlaywrightError as exc:
                outcome = StepOutcome("error", error_kind=classify(exc), error_message=str(exc).splitlines()[0][:500])
            duration = int((time.monotonic() - t0) * 1000)

            if outcome.status != "passed" or self.settings.screenshot_mode == "steps":
                self._screenshot(page, rc, index, "failure" if outcome.status != "passed" else "step")
            with self.db.tx() as conn:
                key, layout = layout_mod.track(conn, rc.project_id, rc.test_case_id, rc.test_run_id, step.action,
                                               outcome.fingerprint, outcome.geometry, self.settings.layout_tolerance_px)
                runs_repo.insert_step_result(
                    conn, rc.project_id, rc.test_run_id, step_index=index, step_key=step.id, action=step.action,
                    kind=step.kind, description=step.description, status=outcome.status,
                    target_fingerprint=outcome.fingerprint, locator=outcome.locator, healed=outcome.healed,
                    expected=outcome.expected, actual=outcome.actual, detail=outcome.detail,
                    error_kind=outcome.error_kind, error_message=outcome.error_message,
                    page_id=rc.page_id, page_url=urls.sanitize_url(page.url) if page.url.startswith("http") else None,
                    element=outcome.element, started_at=started, finished_at=now_iso(), duration_ms=duration,
                    origin="plan", layout_key=key, layout=layout,
                )
            self.layout_event(rc, step.description, layout)
            if outcome.status == "passed":
                continue
            self._skip_rest(rc, steps, index + 1, "Not executed: an earlier step did not pass.")
            if outcome.status == "failed":
                return "failed", "assertion_failed", outcome.error_message
            return "error", outcome.error_kind, outcome.error_message
        self._observe(page, rc, len(steps))
        self._screenshot(page, rc, len(steps) - 1, "final")
        return "passed", None, None

    def _run_step(self, page: Page, rc: RunContext, index: int, step: Step) -> StepOutcome:
        if step.action == "navigate":
            return self._navigate(page, rc, step)
        if step.action == "assert":
            return self._assert(page, step)
        if step.action == "wait_for_text":
            if _poll(page, lambda: text_visible(page, step.value or ""), self.settings.action_timeout_ms):
                return StepOutcome("passed")
            return StepOutcome("error", error_kind="wait_timeout",
                               error_message=f'"{step.value}" did not appear within {self.settings.action_timeout_ms} ms.')
        if step.action == "press" and step.target is None:
            page.keyboard.press(step.key or "")
            self._settle(page)
            return StepOutcome("passed")
        return self._act(page, rc, index, step)

    def _navigate(self, page: Page, rc: RunContext, step: Step) -> StepOutcome:
        url = urls.resolve(rc.base_url, step.url or "/")
        if not urls.same_origin(url, rc.base_url):
            raise StepError("invalid_plan", f"{step.url} is outside the project's current application origin.")
        response = None
        for attempt in (1, 2):  # one bounded retry for transient network errors; GET is safe to repeat
            try:
                response = page.goto(url, wait_until="domcontentloaded")
                break
            except PlaywrightTimeoutError:
                return StepOutcome("error", error_kind="navigation_failed",
                                   error_message=f"{urls.sanitize_url(url)} did not load within "
                                                 f"{self.settings.navigation_timeout_ms} ms.")
            except PlaywrightError as exc:
                if classify(exc) != "navigation_failed" or attempt == 2:
                    return StepOutcome("error", error_kind=classify(exc),
                                       error_message=f"Could not open {urls.sanitize_url(url)}: {str(exc).splitlines()[0][:300]}")
                page.wait_for_timeout(1000)
        status = response.status if response else None
        return StepOutcome("passed", actual=f"HTTP {status}" if status else None,
                           detail=f"Loaded with HTTP {status}" if status and status >= 400 else None)

    # -- actions and healing ---------------------------------------------------------------

    def _act(self, page: Page, rc: RunContext, index: int, step: Step) -> StepOutcome:
        target = step.target
        assert target is not None
        value = resolve_secret(step.value) if step.action == "fill" else step.value
        fingerprint = target_fingerprint(step.action, target)
        specs = declared_specs(step.action, target)
        with self.db.read() as conn:
            validated = healing_repo.validated_locator(conn, rc.project_id, rc.test_case_id, fingerprint)
        if validated:
            specs = [{**validated["locator"], "source": "validated_replacement", "locatorId": validated["id"]}] + specs

        resolution = resolve(page, specs, self.settings.action_timeout_ms)
        if resolution.locator is None:
            return self._heal(page, rc, index, step, fingerprint, specs, resolution.attempts, resolution.failure, value)

        element = self._describe(resolution.locator)
        spec = dict(resolution.spec or {})
        spec.setdefault("source", "declared")
        outcome = self._perform(page, step, resolution.locator, value, describe_spec(spec))
        outcome.locator, outcome.element, outcome.fingerprint = spec, element, fingerprint
        if outcome.status == "passed" and spec["source"] == "validated_replacement":
            outcome.detail = "Used a reviewer-validated replacement locator."
        return outcome

    @property
    def visual(self) -> bool:
        """Overlay (pointer, target box, element boxes) when someone can see it: a visible browser
        window or a video recording. UITEST_HIGHLIGHT=on/off overrides."""
        mode = self.settings.highlight
        return mode == "on" or (mode == "auto" and (not self.settings.headless or self.settings.video))

    def show(self, page: Page, js: str, arg: Any = None) -> None:
        """Best effort: the overlay must never fail a step."""
        if not self.visual:
            return
        try:
            page.evaluate(js) if arg is None else page.evaluate(js, arg)
        except PlaywrightError:
            pass

    def _point(self, page: Page, loc: Any, step: Step, value: str | None, sensitive: bool = False) -> None:
        """Moves the pointer to the element, frames it with the action, then pauses so it is seen."""
        if not self.visual:
            return
        label = {"fill": "type", "select": "select", "press": f"press {step.key}"}.get(step.action, step.action)
        if step.action in ("fill", "select") and value is not None:
            hidden = sensitive or (step.target is not None and step.target.is_sensitive()) or is_secret(step.value)
            label += ": " + ("••••" if hidden else value[:40])
        try:
            loc.evaluate(OVERLAY_POINT_JS, label)
            page.wait_for_timeout(400 + self.settings.slow_mo_ms)
        except PlaywrightError:
            pass

    def _perform(self, page: Page, step: Step, loc: Locator, value: str | None, locator_desc: str,
                 sensitive: bool = False) -> StepOutcome:
        """Measures the element (layout tracking), then acts on it."""
        page_key = urls.page_identity(page.url) if page.url.startswith("http") else page.url
        geometry = layout_mod.measure(loc, page_key)
        outcome = self._perform_action(page, step, loc, value, locator_desc, sensitive)
        outcome.geometry = geometry
        return outcome

    def _perform_action(self, page: Page, step: Step, loc: Locator, value: str | None, locator_desc: str,
                        sensitive: bool = False) -> StepOutcome:
        timeout = self.settings.action_timeout_ms
        self._point(page, loc, step, value, sensitive)
        try:
            if step.action == "click":
                loc.click(timeout=timeout)
            elif step.action == "hover":
                loc.hover(timeout=timeout)
            elif step.action == "fill":
                loc.fill(value or "", timeout=timeout)
            elif step.action == "select":
                by_label = loc.evaluate(
                    "(el, v) => Array.from(el.options || []).some(o => o.label === v || o.text.trim() === v)", value)
                loc.select_option(label=value, timeout=timeout) if by_label else loc.select_option(value=value, timeout=timeout)
            elif step.action == "check":
                loc.check(timeout=timeout)
            elif step.action == "uncheck":
                loc.uncheck(timeout=timeout)
            elif step.action == "press":
                loc.press(step.key or "", timeout=timeout)
        except PlaywrightTimeoutError:
            return StepOutcome("error", error_kind="not_actionable",
                               error_message=f"Found {locator_desc} but could not {step.action} it within {timeout} ms "
                                             "(disabled, covered, or not interactable).")
        except PlaywrightError as exc:
            kind = classify(exc)
            return StepOutcome("error", error_kind=kind, error_message=f"{step.action} on {locator_desc} failed: "
                                                                       f"{str(exc).splitlines()[0][:300]}")
        self.show(page, OVERLAY_CLEAR_JS)
        self._settle(page)
        return StepOutcome("passed")

    def layout_event(self, rc: RunContext, description: str, layout: dict[str, Any] | None) -> None:
        if layout_mod.changed(layout):
            self._event(rc, f'Layout change at "{description}": the element {layout_mod.describe(layout)} '
                            "since the last run of this test.", "warning")

    def _heal(self, page: Page, rc: RunContext, index: int, step: Step, fingerprint: str, specs: list[dict[str, Any]],
              attempts: list[dict[str, Any]], failure: str | None, value: str | None) -> StepOutcome:
        target = step.target
        assert target is not None
        desc = describe_target(target)
        tried = "; ".join(f"{a['locator']} ({a['match']}): {a['visibleMatches']} visible" for a in attempts[:8])
        base_kind = "locator_ambiguous" if failure == "ambiguous" else "locator_not_found"
        base_msg = (f"No declared locator for {desc} matched exactly one visible element "
                    f"({'several matched' if failure == 'ambiguous' else 'none matched'}). Tried: {tried}.")
        original = {"target": target.model_dump(exclude_none=True), "declared": [describe_spec(s) for s in specs],
                    "attempts": attempts, "failure": failure}
        if not self.settings.healing_enabled:
            return StepOutcome("error", error_kind=base_kind, error_message=base_msg, fingerprint=fingerprint)

        def record(outcome: str, reason: str, candidates: list[dict[str, Any]], evidence: dict[str, Any],
                   selected: dict[str, Any] | None = None, confidence: float | None = None) -> str:
            with self.db.tx() as conn:
                return healing_repo.insert_attempt(
                    conn, rc.project_id, rc.test_run_id, step_index=index, step_key=step.id, fingerprint=fingerprint,
                    original=original, failure_reason=failure or "not_found", candidates=candidates, selected=selected,
                    confidence=confidence, reason=reason, outcome=outcome, evidence=evidence)

        if rc.healing_used >= self.settings.healing_max_per_run:
            reason = f"Healing budget of {self.settings.healing_max_per_run} attempt(s) per test run is used up."
            record("budget_exhausted", reason, [], {})
            return StepOutcome("error", error_kind=base_kind, error_message=f"{base_msg} {reason}", fingerprint=fingerprint)
        rc.healing_used += 1
        self._event(rc, f"Locator for {desc} failed ({failure}); inspecting the page for a replacement.", "warning")

        inventory = stable_evaluate(page, INVENTORY_JS, history_repo.MAX_ELEMENTS)
        with self.db.tx() as conn:
            page_id, version_id = history_repo.observe_page(
                conn, rc.project_id, page.url, inventory["title"], inventory["elements"], rc.test_run_id, index)
            reference = history_repo.find_reference(conn, rc.project_id, fingerprint, page_id)
        shot = self._screenshot(page, rc, index, "healing")
        history_note: dict[str, Any] = {"available": reference is not None}
        history_element = None
        if reference:
            age_days = (time.time() - _epoch(reference["observedAt"])) / 86400
            stale = age_days > self.settings.history_stale_days
            history_note.update({"stepResultId": reference["stepResultId"], "observedAt": reference["observedAt"],
                                 "ageDays": round(age_days, 2), "stale": stale, "used": not stale})
            history_element = None if stale else reference["element"]
        decision = self.ranker(Reference(step.action, target, history_element), inventory["elements"],
                          min_confidence=self.settings.healing_min_confidence,
                          min_margin=self.settings.healing_min_margin, plausible=self.settings.healing_plausible)
        evidence = {"pageUrl": urls.sanitize_url(page.url), "pageId": page_id, "pageVersionId": version_id,
                    "screenshotArtifactId": shot, "history": history_note, "excluded": decision.excluded,
                    "candidateCount": len(inventory["elements"]), "inventoryTruncated": inventory["truncated"],
                    "thresholds": {"minConfidence": self.settings.healing_min_confidence,
                                   "minMargin": self.settings.healing_min_margin}}
        top = decision.top()

        if decision.selected is None:
            record(decision.outcome, decision.reason, top, evidence)
            kind = "locator_ambiguous" if decision.outcome == "ambiguous" else "locator_not_found"
            return StepOutcome("error", error_kind=kind, fingerprint=fingerprint,
                               error_message=f"{base_msg} Healing did not pick a replacement: {decision.reason}")

        best = decision.selected
        candidate = best.candidate
        chosen = locate_candidate(page, candidate)
        if chosen is None:
            reason = f"{decision.reason} However, no stable locator identifies that element uniquely."
            record("ambiguous", reason, top, evidence, best.summary(), best.score)
            return StepOutcome("error", error_kind="locator_ambiguous", fingerprint=fingerprint,
                               error_message=f"{base_msg} {reason}")
        spec, loc = chosen
        stored_spec = {k: v for k, v in spec.items() if k != "match"}
        with self.db.read() as conn:
            rejected = healing_repo.rejected_specs(conn, rc.project_id, rc.test_case_id, fingerprint)
        if stored_spec in rejected:
            reason = f"The best candidate ({describe_spec(spec)}) was previously rejected by a reviewer."
            record("no_confident_candidate", reason, top, evidence, best.summary(), best.score)
            return StepOutcome("error", error_kind="locator_not_found", fingerprint=fingerprint,
                               error_message=f"{base_msg} {reason}")

        self._event(rc, f"Retrying with healed locator {describe_spec(spec)} (confidence {best.score:.2f}).", "warning")
        outcome = self._perform(page, step, loc, value, describe_spec(spec))
        if outcome.status != "passed":
            reason = f"{decision.reason} Retry with the replacement failed: {outcome.error_message}"
            record("retry_failed", reason, top, evidence, best.summary(), best.score)
            return StepOutcome("error", error_kind="healing_retry_failed", fingerprint=fingerprint,
                               error_message=f"{base_msg} {reason}")
        attempt_id = record("healed", decision.reason, top, evidence, best.summary(), best.score)
        with self.db.tx() as conn:
            locator_id = healing_repo.record_provisional(
                conn, rc.project_id, test_case_id=rc.test_case_id, fingerprint=fingerprint, step_key=step.id,
                original=original, locator=stored_spec, confidence=best.score, attempt_id=attempt_id)
        rc.healed_steps += 1
        self._event(rc, f"Step healed with a provisional locator ({describe_spec(spec)}); review recommended.", "warning")
        element = {k: v for k, v in candidate.items() if k != "idx"}
        return StepOutcome("passed", healed=True, fingerprint=fingerprint, element=element,
                           locator={**spec, "source": "healed", "healingAttemptId": attempt_id, "locatorId": locator_id},
                           detail=f"Healed (provisional): {decision.reason}")

    # -- assertions (never healed) -----------------------------------------------------------

    def _assert(self, page: Page, step: Step) -> StepOutcome:
        a = step.assertion
        assert a is not None
        timeout = self.settings.action_timeout_ms
        exp = a.expected or ""

        def result(ok: bool, expected: str, actual: str) -> StepOutcome:
            if ok:
                return StepOutcome("passed", expected=expected, actual=actual)
            return StepOutcome("failed", expected=expected, actual=actual, error_kind="assertion_failed",
                               error_message=f"Expected {expected}; actual: {actual}.")

        if a.type in ("text_visible", "text_not_visible"):
            want = a.type == "text_visible"
            ok = _poll(page, lambda: text_visible(page, exp) == want, timeout)
            seen = text_visible(page, exp)
            actual = ("the text is visible" if seen else
                      f'text not visible (page title "{page.title()}", URL {urls.sanitize_url(page.url)})')
            return result(ok, f'text "{exp}" {"visible" if want else "not visible"}', actual)

        if a.type in ("element_visible", "element_not_visible"):
            assert a.target is not None
            want = a.type == "element_visible"
            specs = declared_specs("assert", a.target)

            def visible() -> int:
                return max((n for spec in specs for _, _, n in count_visible(page, spec)), default=0)

            ok = _poll(page, lambda: (visible() > 0) == want, timeout)
            n = visible()
            return result(ok, f"{describe_target(a.target)} {'visible' if want else 'not visible'}",
                          f"{n} visible matching element(s)" if n else "no visible matching element")

        if a.type in ("element_text_contains", "input_value_equals"):
            assert a.target is not None
            desc = describe_target(a.target)
            res = resolve(page, declared_specs("assert", a.target), timeout)
            if res.locator is None:
                return result(False, f'{desc} {"containing" if a.type == "element_text_contains" else "with value"} "{exp}"',
                              f"{desc} not found ({res.failure})")
            loc = res.locator
            if a.type == "element_text_contains":
                ok = _poll(page, lambda: norm_text(exp) in norm_text(loc.inner_text(timeout=1000)), timeout)
                return result(ok, f'{desc} containing "{exp}"', f'"{loc.inner_text(timeout=1000)[:300]}"')
            ok = _poll(page, lambda: loc.input_value(timeout=1000) == exp, timeout)
            sensitive = a.target.is_sensitive() or (self._describe(loc) or {}).get("type") == "password"
            actual = "(redacted)" if sensitive else f'"{loc.input_value(timeout=1000)[:300]}"'
            return result(ok, f'{desc} with value "{"(redacted)" if sensitive else exp}"', actual)

        if a.type == "url_contains":
            ok = _poll(page, lambda: exp in page.url, timeout)
            return result(ok, f'URL containing "{exp}"', urls.sanitize_url(page.url))

        ok = _poll(page, lambda: exp.lower() in page.title().lower(), timeout)  # title_contains
        return result(ok, f'page title containing "{exp}"', f'"{page.title()}"')

    # -- evidence and history -----------------------------------------------------------------

    def _observe(self, page: Page, rc: RunContext, index: int) -> None:
        """Records the page in project history the first time a run reaches a new page identity."""
        url = page.url
        if not url.startswith("http") or not urls.same_origin(url, rc.base_url):
            return
        key = urls.page_identity(url)
        if key == rc.page_key:
            return
        try:
            inventory = stable_evaluate(page, INVENTORY_JS, history_repo.MAX_ELEMENTS)
        except PlaywrightError:
            return
        with self.db.tx() as conn:
            rc.page_id, _ = history_repo.observe_page(conn, rc.project_id, url, inventory["title"],
                                                      inventory["elements"], rc.test_run_id, index)
        rc.page_key = key

    def _describe(self, loc: Locator) -> dict[str, Any] | None:
        try:
            return loc.evaluate(DESCRIBE_JS, timeout=1000)
        except PlaywrightError:
            return None

    def _settle(self, page: Page) -> None:
        try:
            page.wait_for_load_state("domcontentloaded", timeout=self.settings.navigation_timeout_ms)
        except PlaywrightError:
            pass

    def _screenshot(self, page: Page, rc: RunContext, index: int, label: str) -> str | None:
        rel = f"{rc.project_id}/{rc.test_run_id}/{max(index, 0):03d}-{label}.png"
        path = self.settings.artifacts_dir / rel
        try:
            page.screenshot(path=str(path), timeout=5000)
        except PlaywrightError:
            return None
        with self.db.tx() as conn:
            return artifacts_repo.add_artifact(conn, rc.project_id, rc.test_run_id, kind="screenshot", rel_path=rel,
                                               content_type="image/png", size_bytes=path.stat().st_size,
                                               step_index=max(index, 0), label=label)

    def _close_context(self, context: BrowserContext, rc: RunContext, status: str) -> None:
        video = None
        try:
            if self.settings.trace_mode != "off":
                keep = self.settings.trace_mode == "always" or status != "passed"
                if keep:
                    rel = f"{rc.project_id}/{rc.test_run_id}/trace.zip"
                    context.tracing.stop(path=str(self.settings.artifacts_dir / rel))
                    self._register(rc, "trace", rel, "application/zip", "Playwright trace")
                else:
                    context.tracing.stop()
            if self.settings.video and context.pages:
                video = context.pages[0].video
            context.close()
            if video is not None:
                source = Path(video.path())
                rel = f"{rc.project_id}/{rc.test_run_id}/recording.webm"
                source.replace(self.settings.artifacts_dir / rel)
                self._register(rc, "video", rel, "video/webm", "Browser recording")
        except (PlaywrightError, OSError):
            log.warning("could not finalize browser context for %s", rc.test_run_id, exc_info=True)

    def _register(self, rc: RunContext, kind: str, rel: str, content_type: str, label: str) -> None:
        path = self.settings.artifacts_dir / rel
        if path.exists():
            with self.db.tx() as conn:
                artifacts_repo.add_artifact(conn, rc.project_id, rc.test_run_id, kind=kind, rel_path=rel,
                                            content_type=content_type, size_bytes=path.stat().st_size, label=label)

    def _skip_rest(self, rc: RunContext, steps: list[Step], start: int, reason: str) -> None:
        with self.db.tx() as conn:
            for index in range(start, len(steps)):
                s = steps[index]
                runs_repo.insert_step_result(conn, rc.project_id, rc.test_run_id, step_index=index, step_key=s.id,
                                             action=s.action, kind=s.kind, description=s.description,
                                             status="skipped", detail=reason, origin="plan")

    def _event(self, rc: RunContext, message: str, level: str = "info") -> None:
        with self.db.tx() as conn:
            runs_repo.add_event(conn, rc.project_id, rc.run_id, rc.test_run_id, message, level)


def _epoch(iso_ts: str) -> float:
    return parse_iso(iso_ts).timestamp()
