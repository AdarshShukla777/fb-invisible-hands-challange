"""Per-test agent memory: stored after every agent run, read before the next one.

A memory row holds the path that worked (with stable locators for replay), values the agent
chose itself, notes it wrote for the next run, information it was missing, and dead ends.
Values typed into sensitive fields are never stored, and every stored text is redacted
against the sensitive values seen in the run."""
from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlsplit

from ..repo import bugs as bugs_repo
from ..util import dumps, loads, new_id, now_iso

RECENT_RUNS = 3


def redact(text: Any, secrets: set[str]) -> Any:
    if isinstance(text, str):
        for s in sorted(secrets, key=len, reverse=True):
            if s:
                text = text.replace(s, "••••")
        return text
    if isinstance(text, list):
        return [redact(t, secrets) for t in text]
    if isinstance(text, dict):
        return {k: redact(v, secrets) for k, v in text.items()}
    return text


def record(conn: sqlite3.Connection, project_id: str, *, test_case_id: str, revision_id: str, test_run_id: str,
           outcome: str, summary: str | None, verified: bool, trajectory: list[dict], notes: list[str],
           missing: list[str], details: dict[str, Any], secrets: set[str]) -> str:
    mid = new_id()
    conn.execute(
        "INSERT INTO agent_memories (id, project_id, test_case_id, revision_id, test_run_id, outcome, summary, verified, "
        "trajectory_json, notes_json, missing_json, details_json, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (mid, project_id, test_case_id, revision_id, test_run_id, outcome, redact(summary, secrets), int(verified),
         dumps(redact(trajectory, secrets)), dumps(redact(notes, secrets)), dumps(redact(missing, secrets)),
         dumps(redact(details, secrets)), now_iso()),
    )
    return mid


def for_test_run(conn: sqlite3.Connection, project_id: str, test_run_id: str) -> dict[str, Any] | None:
    r = conn.execute("SELECT * FROM agent_memories WHERE project_id = ? AND test_run_id = ?",
                     (project_id, test_run_id)).fetchone()
    return _decode(r) if r else None


def _decode(r: sqlite3.Row) -> dict[str, Any]:
    return {"id": r["id"], "testRunId": r["test_run_id"], "revisionId": r["revision_id"], "outcome": r["outcome"],
            "summary": r["summary"], "verified": bool(r["verified"]), "trajectory": loads(r["trajectory_json"], []),
            "notes": loads(r["notes_json"], []), "missing": loads(r["missing_json"], []),
            "details": loads(r["details_json"], {}), "createdAt": r["created_at"]}


@dataclass
class Memory:
    path: list[dict[str, Any]] = field(default_factory=list)  # steps of the last passing run
    source_test_run_id: str | None = None
    source_at: str | None = None
    notes: list[str] = field(default_factory=list)
    missing: list[str] = field(default_factory=list)
    agent_values: list[dict[str, Any]] = field(default_factory=list)
    last_problem: str | None = None
    runs: int = 0
    bugs: list[dict[str, Any]] = field(default_factory=list)  # open and dismissed bugs of this test

    def prompt_block(self) -> str:
        if not self.runs:
            return "MEMORY: none - this is the first run of this test."
        lines = [f"MEMORY FROM {self.runs} EARLIER RUN(S) OF THIS TEST:"]
        if self.path:
            lines.append(f"Steps that worked in the last passing run ({self.source_at}):")
            lines += [f"  {i}. {_step_line(s)}" for i, s in enumerate(self.path, 1)]
        else:
            lines.append("No earlier run has passed yet.")
        if self.agent_values:
            lines.append("Values the agent chose itself last time (reuse them for consistency): "
                         + "; ".join(f'{v["field"]} = "{v["value"]}"' for v in self.agent_values))
        if self.notes:
            lines.append("Notes for this run:")
            lines += [f"  - {n}" for n in self.notes]
        if self.missing:
            lines.append("Information that was missing before:")
            lines += [f"  - {m}" for m in self.missing]
        if self.last_problem:
            lines.append(f"Most recent problem: {self.last_problem}")
        open_bugs = [b for b in self.bugs if b["status"] == "open"]
        dismissed = [b for b in self.bugs if b["status"] == "dismissed"]
        if open_bugs:
            lines.append("Bugs earlier runs found (check whether each is still there; if it is, finish failed):")
            lines += [f"  - {_bug_line(b)}" for b in open_bugs]
        if dismissed:
            lines.append("The tester marked these as expected behaviour, not bugs (do not fail the test for them):")
            lines += [f"  - {_bug_line(b)}" + (f' (tester: {b["note"]})' if b.get("note") else "") for b in dismissed]
        return "\n".join(lines)

    def as_api(self) -> dict[str, Any]:
        return {"runs": self.runs, "lastPassingTestRunId": self.source_test_run_id, "lastPassingAt": self.source_at,
                "steps": self.path, "agentChosenValues": self.agent_values, "notes": self.notes,
                "missing": self.missing, "lastProblem": self.last_problem,
                "bugs": [{"id": b["id"], "title": b["title"], "status": b["status"], "occurrences": b["occurrences"]}
                         for b in self.bugs]}


def _bug_line(b: dict[str, Any]) -> str:
    where = f' after "{b["action"]}"' if b.get("action") and b.get("source") == "check" else ""
    seen = f', seen {b["occurrences"]}x' if b["occurrences"] > 1 else ""
    return (f'{b["title"]}{where}: expected {b.get("expected") or "?"}, got {b.get("actual") or "?"}'
            f'{" (regression)" if b.get("regressed") else ""}{seen}')[:400]


def _step_line(s: dict[str, Any]) -> str:
    page = urlsplit(s.get("page") or "").path or ""
    source = f" (value from {s['valueSource']})" if s.get("valueSource") else ""
    if s.get("sensitive"):
        source = f" (value from {s.get('valueSource') or 'unknown'}; not stored - find it again)"
    result = {"checked": " [result checked next]", "covered": " [next step proves it]"}.get(s.get("result"), "")
    return f"[{page}] {s['description']}{source}{result}"


def load(conn: sqlite3.Connection, project_id: str, test_case_id: str) -> Memory:
    """Memory for the next run: the last passing path plus notes/missing info of recent runs."""
    rows = conn.execute(
        "SELECT * FROM agent_memories WHERE project_id = ? AND test_case_id = ? ORDER BY created_at DESC LIMIT ?",
        (project_id, test_case_id, RECENT_RUNS),
    ).fetchall()
    # Only passes recorded with result checking (details.gate) are replayed: an older pass may have
    # clicked through without checking what its actions did, and replaying it would repeat that.
    last_pass = next((r for r in conn.execute(
        "SELECT * FROM agent_memories WHERE project_id = ? AND test_case_id = ? AND outcome = 'passed' "
        "ORDER BY created_at DESC LIMIT 20", (project_id, test_case_id),
    ).fetchall() if loads(r["details_json"], {}).get("gate")), None)
    total = conn.execute("SELECT COUNT(*) FROM agent_memories WHERE project_id = ? AND test_case_id = ?",
                         (project_id, test_case_id)).fetchone()[0]
    memory = Memory(runs=total, bugs=bugs_repo.for_test(conn, project_id, test_case_id))
    recent = [_decode(r) for r in rows]
    if last_pass:
        lp = _decode(last_pass)
        memory.path = lp["trajectory"]
        memory.source_test_run_id, memory.source_at = lp["testRunId"], lp["createdAt"]
        memory.agent_values = lp["details"].get("agentValues", [])
        recent.append(lp)
    for m in recent:
        for n in m["notes"]:
            if n not in memory.notes:
                memory.notes.append(n)
        for x in m["missing"]:
            if x not in memory.missing:
                memory.missing.append(x)
    memory.notes, memory.missing = memory.notes[:8], memory.missing[:5]
    if recent and recent[0]["outcome"] != "passed":
        memory.last_problem = f'{recent[0]["outcome"]}: {recent[0]["summary"] or "no summary"}'
    return memory
