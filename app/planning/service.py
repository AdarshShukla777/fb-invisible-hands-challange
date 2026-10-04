"""Chooses a planner and turns an instruction into a PlanOutcome ready to store as a revision.

Modes (UITEST_PLANNER):
  rules - deterministic rule planner only.
  llm   - LLM planner only (fails clearly if no model credentials are configured).
  auto  - rule planner first (instant, free, reproducible); the LLM is consulted only
          when the rules cannot produce a runnable plan and credentials are configured."""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from typing import Any

from ..config import Settings
from .llm import JsonLLM, LLMError
from .llm_planner import LLMPlanner
from .plan import PlanIssue, ValidationResult, validate_plan
from .rules import PlanContext, PlanDraft, RulePlanner

_EXPECTATION_CODES = {"vague_expectation", "llm_clarification", "ambiguous_check"}


@dataclass
class PlanOutcome:
    status: str  # ready | needs_clarification | invalid
    plan: dict[str, Any] | None
    issues: list[dict[str, Any]]
    planner: str


class PlanningService:
    def __init__(self, settings: Settings, llm: JsonLLM | None):
        self.settings = settings
        self.llm = llm
        self.rules = RulePlanner()

    def plan(self, instruction: str, ctx: PlanContext) -> PlanOutcome:
        known = {u for u, _ in ctx.known_pages}

        def validate(raw: dict[str, Any]) -> ValidationResult:
            return validate_plan(raw, base_url=ctx.base_url, instruction=instruction,
                                 max_steps=self.settings.max_steps, known_urls=known)

        mode = self.settings.planner_mode
        if mode == "llm" and self.llm is None:
            return PlanOutcome("invalid", None, [PlanIssue(
                code="planner_unavailable", severity="error",
                message="UITEST_PLANNER=llm but no model is configured (set ANTHROPIC_API_KEY, or UITEST_LLM_PROVIDER=openai_compatible with UITEST_LLM_BASE_URL).",
            ).model_dump()], "none")

        rule_outcome = None
        if mode in {"rules", "auto"}:
            rule_outcome = _finish(self.rules.plan(instruction, ctx), None, validate)
            if mode == "rules" or rule_outcome.status == "ready" or self.llm is None:
                return rule_outcome

        planner = LLMPlanner(self.llm, self.settings.llm_timeout_s, self.settings.llm_max_calls_per_plan)
        try:
            draft, result = planner.plan(instruction, ctx, validate)
        except LLMError as exc:
            note = PlanIssue(code="llm_planner_failed", severity="clarification" if rule_outcome else "error",
                             message=f"The LLM planner could not be used ({exc}).").model_dump()
            if rule_outcome is None:
                return PlanOutcome("invalid", None, [note], planner.name)
            rule_outcome.issues.append(note)
            return rule_outcome
        return _finish(draft, result, validate)

    def agent_outcome(self, instruction: str) -> PlanOutcome:
        """Agent-mode tests are not planned ahead: the agent works from the goal on the live app."""
        if self.llm is None:
            return PlanOutcome("invalid", None, [PlanIssue(
                code="agent_unavailable", severity="error",
                message="Agent mode needs a model: set UITEST_LLM_PROVIDER=openai_compatible with "
                        "UITEST_LLM_BASE_URL and UITEST_LLM_MODEL (or ANTHROPIC_API_KEY) and restart the backend.",
            ).model_dump()], "agent")
        if len(instruction.strip()) < 3:
            return PlanOutcome("needs_clarification", None, [PlanIssue(
                code="empty_goal", severity="clarification", message="Describe what the agent should test.",
            ).model_dump()], "agent")
        return PlanOutcome("ready", None, [], f"agent:{self.llm.model}")

    def plan_many(self, instructions: list[str], ctx: PlanContext) -> list[PlanOutcome]:
        """Independent instructions are planned concurrently (bounded) - each is its own sub-agent task."""
        if len(instructions) <= 1 or self.llm is None or self.settings.planner_mode == "rules":
            return [self.plan(i, ctx) for i in instructions]
        with ThreadPoolExecutor(max_workers=min(self.settings.planner_concurrency, len(instructions))) as pool:
            return list(pool.map(lambda text: self.plan(text, ctx), instructions))


def _finish(draft: PlanDraft, result: ValidationResult | None, validate) -> PlanOutcome:
    issues = list(draft.issues)
    plan = None
    if draft.raw and draft.raw.get("steps"):
        result = result or validate(draft.raw)
        plan = result.plan.model_dump(exclude_none=True) if result.plan else None
        for issue in result.issues:
            if issue.code == "missing_expected_outcome" and any(i.code in _EXPECTATION_CODES for i in issues):
                continue  # already explained more specifically
            issues.append(issue)
    elif not issues:
        issues.append(PlanIssue(code="empty_plan", severity="clarification",
                                message="No executable steps were found. Describe the actions and the expected result."))
    if any(i.severity == "error" for i in issues) or (plan is None and not issues):
        status = "invalid"
    elif issues:
        status = "needs_clarification"
    else:
        status = "ready"
    return PlanOutcome(status, plan, [i.model_dump() for i in issues], draft.planner)
