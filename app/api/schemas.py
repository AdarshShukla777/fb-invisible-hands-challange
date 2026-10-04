"""Request bodies, query-parameter parsing and response serializers (camelCase JSON)."""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field
from pydantic.alias_generators import to_camel

from ..errors import Invalid
from ..util import iso, loads


class ApiModel(BaseModel):
    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True, extra="forbid")


class ProjectIn(ApiModel):
    name: str = Field(min_length=1, max_length=200)
    url: str = Field(min_length=1, max_length=2000)


class ProjectPatch(ApiModel):
    name: str | None = Field(default=None, min_length=1, max_length=200)
    url: str | None = Field(default=None, min_length=1, max_length=2000)


class TestsIn(ApiModel):
    text: str | None = Field(default=None, max_length=20000)
    instructions: list[str] | None = Field(default=None, max_length=20)
    # plan: validated structured plan (strict). agent: browser-use style agent with run memory.
    mode: Literal["plan", "agent"] = "plan"


class TestUpdateIn(ApiModel):
    instruction: str = Field(min_length=1, max_length=4000)
    mode: Literal["plan", "agent"] | None = None


class RunIn(ApiModel):
    test_ids: list[str] | None = Field(default=None, max_length=100)


class ReviewIn(ApiModel):
    decision: Literal["validate", "reject"]
    note: str | None = Field(default=None, max_length=1000)


# -- query parameter helpers --------------------------------------------------------------

def csv_param(value: str | None, allowed: set[str] | tuple[str, ...], name: str) -> list[str] | None:
    if value is None or not value.strip():
        return None
    items = list(dict.fromkeys(v.strip() for v in value.split(",") if v.strip()))
    bad = [v for v in items if v not in allowed]
    if bad:
        raise Invalid(f"Unknown {name}: {', '.join(bad)}. Allowed: {', '.join(sorted(allowed))}.", code="invalid_filter")
    return items


def time_param(value: str | None, name: str) -> str | None:
    if value is None or not value.strip():
        return None
    try:
        dt = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError:
        raise Invalid(f"{name} must be an ISO 8601 timestamp.", code="invalid_filter") from None
    return iso(dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc))


def time_range(since: str | None, until: str | None) -> tuple[str | None, str | None]:
    s, u = time_param(since, "since"), time_param(until, "until")
    if s and u and s >= u:
        raise Invalid("since must be earlier than until.", code="invalid_filter")
    return s, u


def listing(items: list[Any], next_cursor: str | None) -> dict[str, Any]:
    return {"items": items, "nextCursor": next_cursor}


# -- serializers ----------------------------------------------------------------------------

def project(p: dict[str, Any]) -> dict[str, Any]:
    return {"id": p["id"], "name": p["name"], "url": p["base_url"], "createdAt": p["created_at"], "updatedAt": p["updated_at"]}


def message(m: dict[str, Any]) -> dict[str, Any]:
    return {"id": m["id"], "projectId": m["project_id"], "role": m["role"], "text": m["text"], "createdAt": m["created_at"]}


def test_case(tc: dict[str, Any], with_plan: bool = False) -> dict[str, Any]:
    out = {
        "id": tc["id"], "projectId": tc["project_id"], "title": tc["title"], "instruction": tc["instruction"],
        "status": tc["last_run_status"], "planStatus": tc["plan_status"], "issues": loads(tc["issues_json"], []),
        "mode": tc["mode"],
        "revision": {"id": tc["current_revision_id"], "number": tc["revision_number"], "planner": tc["planner"],
                     "createdAt": tc["revision_created_at"]},
        "lastRunAt": tc["last_run_at"], "archived": tc["archived_at"] is not None,
        "createdAt": tc["created_at"], "updatedAt": tc["updated_at"],
    }
    if with_plan:
        out["plan"] = loads(tc["plan_json"])
    return out


def revision(r: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": r["id"], "testCaseId": r["test_case_id"], "number": r["revision_number"], "instruction": r["instruction"],
        "planStatus": r["plan_status"], "issues": loads(r["issues_json"], []), "planner": r["planner"],
        "mode": r["mode"],
        "plan": loads(r["plan_json"]), "createdAt": r["created_at"],
    }


def test_run(tr: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": tr["id"], "runId": tr["run_id"], "testCaseId": tr["test_case_id"], "revisionId": tr["revision_id"],
        "revisionNumber": tr["revision_number"], "title": tr["title"], "instruction": tr["instruction"],
        "status": tr["status"], "verdictSource": tr["verdict_source"], "errorKind": tr["error_kind"],
        "errorSource": tr["error_source"],
        "errorMessage": tr["error_message"], "healedSteps": tr["healed_steps"],
        "cancelRequested": bool(tr["cancel_requested"]), "queuedAt": tr["queued_at"], "startedAt": tr["started_at"],
        "finishedAt": tr["finished_at"], "durationMs": tr["duration_ms"],
    }


def run(r: dict[str, Any]) -> dict[str, Any]:
    trs = [test_run(t) for t in r.get("test_runs", [])]
    counts: dict[str, int] = {}
    for t in trs:
        counts[t["status"]] = counts.get(t["status"], 0) + 1
    return {"id": r["id"], "projectId": r["project_id"], "status": r["status"], "createdAt": r["created_at"],
            "finishedAt": r["finished_at"], "skipped": loads(r["skipped_json"], []), "counts": counts, "testRuns": trs}


def event(e: dict[str, Any]) -> dict[str, Any]:
    return {"id": e["id"], "testRunId": e["test_run_id"], "at": e["at"], "level": e["level"], "message": e["message"]}


def page(p: dict[str, Any], stale_before: str) -> dict[str, Any]:
    out = {"id": p["id"], "url": p["url_key"], "title": p["title"], "firstSeenAt": p["first_seen_at"],
           "lastSeenAt": p["last_seen_at"], "stale": p["last_seen_at"] < stale_before,
           "latestVersionId": p["latest_version_id"]}
    if "latest_version" in p:
        out["latestVersion"] = page_version(p["latest_version"], with_elements=True) if p["latest_version"] else None
    return out


def page_version(v: dict[str, Any], with_elements: bool = False) -> dict[str, Any]:
    out = {"id": v["id"], "pageId": v["page_id"], "version": v["version"], "title": v["title"],
           "elementCount": v["element_count"], "firstSeenAt": v["first_seen_at"], "lastSeenAt": v["last_seen_at"]}
    if with_elements:
        out["elements"] = loads(v["elements_json"], [])
    return out


def locator(entry: dict[str, Any]) -> dict[str, Any]:
    return {"id": entry["id"], "testCaseId": entry["test_case_id"], "targetFingerprint": entry["target_fingerprint"],
            "stepKey": entry["step_key"], "original": loads(entry["original_json"]), "locator": loads(entry["locator_json"]),
            "status": entry["status"], "confidence": entry["confidence"], "healingAttemptId": entry["healing_attempt_id"],
            "createdAt": entry["created_at"], "reviewedAt": entry["reviewed_at"], "reviewNote": entry["review_note"]}


def healing_attempt(a: dict[str, Any]) -> dict[str, Any]:
    return {"id": a["id"], "testRunId": a["test_run_id"], "stepIndex": a["step_index"], "stepKey": a["step_key"],
            "outcome": a["outcome"], "failureReason": a["failure_reason"], "original": loads(a["original_locator_json"]),
            "candidates": loads(a["candidates_json"], []), "selected": loads(a["selected_json"]),
            "confidence": a["confidence"], "reason": a["selection_reason"], "evidence": loads(a["evidence_json"], {}),
            "createdAt": a["created_at"]}
