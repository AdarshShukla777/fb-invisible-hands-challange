"""Test-run reports, built from stored rows only.

The same builder serves live progress (while running) and the persisted report written
once when the test run finishes. Persisted reports are immutable snapshots: later edits,
locator reviews or new runs never change them."""
from __future__ import annotations

import sqlite3
from typing import Any

from .agent import memory as agent_memory
from .repo import artifacts as artifacts_repo
from .repo import healing as healing_repo
from .repo import runs as runs_repo
from .util import loads, now_iso

# Where a problem came from. Only assertion failures are attributed to the application.
ERROR_SOURCES = {
    "assertion_failed": "application",
    "locator_not_found": "agent",
    "locator_ambiguous": "agent",
    "healing_retry_failed": "agent",
    "not_actionable": "agent",
    "wait_timeout": "agent",
    "action_failed": "agent",
    "missing_secret": "agent",
    "invalid_plan": "agent",
    "navigation_failed": "infrastructure",
    "timeout": "infrastructure",
    "interrupted": "infrastructure",
    "internal": "infrastructure",
    "browser_crash": "browser",
    "browser_launch_failed": "browser",
    # agent mode
    "agent_reported_failure": "application",  # the agent judged that the app misbehaved
    "check_failed": "application",  # the agent claimed a pass, but one of its own checks failed
    "agent_unverified": "agent",  # the agent claimed a pass without checking every result
    "agent_blocked": "agent",
    "agent_step_limit": "agent",
    "agent_stuck": "agent",
    "agent_llm_error": "agent",
}


def artifact_url(project_id: str, artifact_id: str) -> str:
    return f"/api/projects/{project_id}/artifacts/{artifact_id}"


def build_report(conn: sqlite3.Connection, project_id: str, test_run_id: str) -> dict[str, Any]:
    tr = runs_repo.get_test_run(conn, project_id, test_run_id)
    revision = conn.execute("SELECT * FROM test_case_revisions WHERE project_id = ? AND id = ?",
                            (project_id, tr["revision_id"])).fetchone()
    plan = loads(revision["plan_json"], {"steps": []}) or {"steps": []}
    results = {r["step_index"]: r for r in runs_repo.step_results(conn, project_id, test_run_id)}
    arts = artifacts_repo.for_test_run(conn, project_id, test_run_id)
    attempts = healing_repo.attempts_for_test_run(conn, project_id, test_run_id)
    art_by_id = {a["id"]: a for a in arts}
    shots_by_step: dict[int, list[str]] = {}
    for a in arts:
        if a["kind"] == "screenshot" and a["step_index"] is not None:
            shots_by_step.setdefault(a["step_index"], []).append(artifact_url(project_id, a["id"]))

    active = tr["status"] in ("queued", "running")
    mode = revision["mode"]
    # Plan mode: one entry per planned step. Agent mode: the steps the agent actually took.
    planned_steps = plan.get("steps", []) if mode == "plan" else [
        {"id": r["step_key"], "action": r["action"], "description": r["description"]}
        for _, r in sorted(results.items())
    ]
    steps: list[dict[str, Any]] = []
    for index, planned in enumerate(planned_steps):
        r = results.get(index)
        step: dict[str, Any] = {
            "index": index, "key": planned.get("id"), "action": planned.get("action"),
            "kind": "assertion" if planned.get("action") == "assert" else "action",
            "description": planned.get("description"),
            "status": r["status"] if r else ("pending" if active else "skipped"),
        }
        if r:
            step.update({
                "expected": r["expected"], "actual": r["actual"], "detail": r["detail"],
                "errorKind": r["error_kind"], "errorMessage": r["error_message"],
                "locator": loads(r["locator_json"]), "healed": bool(r["healed"]), "origin": r["origin"],
                "pageUrl": r["page_url"], "startedAt": r["started_at"], "durationMs": r["duration_ms"],
                "screenshots": shots_by_step.get(index, []), "layout": loads(r["layout_json"]),
            })
        steps.append(step)

    assertions = [s for s in steps if s["kind"] == "assertion"]
    failed_step = next((s for s in steps if s["status"] in ("failed", "error")), None)
    failure = None
    agent = None
    if mode == "agent":
        memory = agent_memory.for_test_run(conn, project_id, test_run_id)
        details = (memory or {}).get("details", {})
        agent = {
            "verdictSource": tr["verdict_source"], "outcome": (memory or {}).get("outcome"),
            "summary": (memory or {}).get("summary"), "verified": (memory or {}).get("verified", False),
            "missing": (memory or {}).get("missing", []), "notes": (memory or {}).get("notes", []),
            "agentChosenValues": details.get("agentValues", []), "deadEnds": details.get("deadEnds", []),
            "replay": details.get("replay", {}), "reasoning": details.get("reasoning", []),
            "llmCalls": details.get("llmCalls", 0), "agentSteps": details.get("agentSteps", 0),
            "verification": details.get("verification", []), "pushbacks": details.get("pushbacks", 0),
        }
        if tr["status"] in ("failed", "error"):  # in agent mode single step errors are recoverable
            last = steps[-1]["description"] if steps else None
            check = next((s for s in reversed(assertions) if s["status"] == "failed"), None)                 if tr["status"] == "failed" else None
            if check:  # the evidence: the agent's own check that failed
                failure = {"stepIndex": check["index"], "stepKey": check["key"], "step": check["description"],
                           "expected": check.get("expected"), "actual": check.get("actual")}
            else:
                failure = {"stepIndex": None, "stepKey": None,
                           "step": failed_step["description"] if failed_step else (f"After: {last}" if last else None),
                           "expected": f"Goal achieved: {revision['instruction']}",
                           "actual": agent["summary"] or tr["error_message"]}
            failure.update(errorKind=tr["error_kind"], errorSource=tr["error_source"], message=tr["error_message"])
    elif failed_step:
        kind = failed_step.get("errorKind")
        failure = {
            "stepIndex": failed_step["index"], "stepKey": failed_step["key"], "step": failed_step["description"],
            "expected": failed_step.get("expected"), "actual": failed_step.get("actual"),
            "errorKind": kind, "errorSource": ERROR_SOURCES.get(kind or ""), "message": failed_step.get("errorMessage"),
        }
    elif tr["status"] == "error":
        failure = {"stepIndex": None, "stepKey": None, "step": None, "expected": None, "actual": None,
                   "errorKind": tr["error_kind"], "errorSource": tr["error_source"], "message": tr["error_message"]}

    healing = []
    for a in attempts:
        evidence = loads(a["evidence_json"], {})
        if evidence.get("screenshotArtifactId") in art_by_id:
            evidence["screenshotUrl"] = artifact_url(project_id, evidence["screenshotArtifactId"])
        healing.append({
            "id": a["id"], "stepIndex": a["step_index"], "stepKey": a["step_key"], "outcome": a["outcome"],
            "failureReason": a["failure_reason"], "original": loads(a["original_locator_json"]),
            "candidates": loads(a["candidates_json"], []), "selected": loads(a["selected_json"]),
            "confidence": a["confidence"], "reason": a["selection_reason"], "evidence": evidence,
            "createdAt": a["created_at"],
        })

    report = {
        "testRunId": tr["id"], "runId": tr["run_id"], "projectId": project_id,
        "testCase": {"id": tr["test_case_id"], "title": tr["title"]},
        "revision": {"id": revision["id"], "number": revision["revision_number"], "instruction": revision["instruction"],
                     "planner": revision["planner"], "mode": mode, "createdAt": revision["created_at"]},
        "status": tr["status"], "verdictSource": tr["verdict_source"], "agent": agent,
        "errorKind": tr["error_kind"], "errorSource": tr["error_source"], "errorMessage": tr["error_message"],
        "timing": {"queuedAt": tr["queued_at"], "startedAt": tr["started_at"], "finishedAt": tr["finished_at"],
                   "durationMs": tr["duration_ms"]},
        "steps": steps,
        "assertions": {
            "total": len(assertions),
            "passed": sum(1 for s in assertions if s["status"] == "passed"),
            "failed": sum(1 for s in assertions if s["status"] == "failed"),
            "notRun": sum(1 for s in assertions if s["status"] in ("skipped", "pending")),
        },
        "failure": failure,
        "healedSteps": sum(1 for s in steps if s.get("healed")),
        "healing": healing,
        "artifacts": [{"id": a["id"], "kind": a["kind"], "label": a["label"], "stepIndex": a["step_index"],
                       "contentType": a["content_type"], "sizeBytes": a["size_bytes"],
                       "url": artifact_url(project_id, a["id"])} for a in arts],
        "layout": _layout(steps),
        "generatedAt": now_iso(),
    }
    report["summary"] = _summary(report) + _layout_note(report["layout"])
    return report


def _agent_summary(r: dict[str, Any]) -> str:
    status, ag = r["status"], r["agent"]
    replay = ag.get("replay") or {}
    deviated = ""
    if replay.get("deviations"):
        changes = "; ".join(f'"{d["description"]}" -> "{d.get("resolvedBy") or "not resolved"}"'
                            for d in replay["deviations"][:3])
        deviated = f" Differed from the last passing run at {len(replay['deviations'])} step(s): {changes}."
    if status == "passed" and r["verdictSource"] == "replay":
        return (f"Passed (replayed from memory): {replay.get('replayed', 0)} remembered step(s), "
                f"{r['assertions']['passed']} remembered check(s) held.{deviated}")
    checks = r["assertions"]["passed"]
    if status == "passed":
        effects = ag.get("verification") or []
        checked = sum(1 for e in effects if e["status"] == "checked")
        covered = sum(1 for e in effects if e["status"] == "covered")
        proof = f"{checks} passing check(s)" if checks else "no explicit check - agent's judgment only"
        if effects:
            proof += f"; {checked} action result(s) checked" + (f", {covered} proven by the next step" if covered else "")
        return f"Passed (agent-judged, {proof}): {ag.get('summary') or 'goal achieved'}.{deviated}"
    if status == "failed" and r["errorKind"] == "check_failed":
        return f"Failed (a check failed; the agent had claimed a pass): {r['errorMessage']}.{deviated}"
    if status == "failed":
        return f"Failed (agent-judged): {ag.get('summary') or r['errorMessage']}.{deviated}"
    if status == "error":
        missing = f" Missing: {'; '.join(ag['missing'])}." if ag.get("missing") else ""
        return (f"Error ({r['errorSource'] or 'unknown'} / {r['errorKind'] or 'unknown'}): "
                f"{r['errorMessage'] or 'no details'}.{missing} No verdict on application behavior.{deviated}")
    return "Cancelled before completion." if status == "cancelled" else f"{status.capitalize()}."


def _layout(steps: list[dict[str, Any]]) -> dict[str, Any]:
    """Layout tracking for the run: every acted-on element measured, compared with the last run of this
    test that measured the same step, and the ones that moved or resized."""
    measured = [s for s in steps if s.get("layout")]
    compared = [s for s in measured if s["layout"].get("reference")]
    changes = [{"stepIndex": s["index"], "step": s["description"], "kinds": s["layout"]["change"]["kinds"],
                "summary": s["layout"]["change"]["summary"], "box": s["layout"]["box"],
                "reference": s["layout"]["reference"]}
               for s in compared if (s["layout"].get("change") or {}).get("kinds")]
    return {"measured": len(measured), "compared": len(compared), "baseline": len(measured) - len(compared),
            "changed": changes}


def _layout_note(layout: dict[str, Any]) -> str:
    if layout["changed"]:
        return (f" Layout: {len(layout['changed'])} of {layout['compared']} compared element(s) moved or resized "
                "since the last run of this test.")
    if layout["compared"]:
        return f" Layout: {layout['compared']} element(s) compared with the last run, none moved."
    return ""


def _summary(r: dict[str, Any]) -> str:
    if r.get("agent") is not None:
        return _agent_summary(r)
    status, a = r["status"], r["assertions"]
    healed = f" {r['healedSteps']} step(s) passed only after locator healing (provisional; review recommended)." \
        if r["healedSteps"] else ""
    if status == "passed":
        return f"Passed: {a['passed']}/{a['total']} assertion(s) held.{healed}"
    f = r["failure"] or {}
    if status == "failed":
        return f"Failed at {f.get('stepKey')}: expected {f.get('expected')}; actual {f.get('actual')}.{healed}"
    if status == "error":
        where = f" at {f['stepKey']}" if f.get("stepKey") else ""
        return (f"Error ({r['errorSource'] or 'unknown'} / {r['errorKind'] or 'unknown'}){where}: "
                f"{r['errorMessage'] or 'no details'}. No verdict on application behavior.")
    if status == "cancelled":
        return "Cancelled before completion."
    return f"{status.capitalize()}."
