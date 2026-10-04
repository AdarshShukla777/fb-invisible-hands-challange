"""Application bugs found by test runs (agent and plan mode), deduplicated per test.

A finished run with an application failure yields one bug per action whose check failed
(agent mode), the agent's own failure verdict when no check failed, or the failed step
(plan mode). The signature (test + action + failing check/step) identifies the same bug in
later runs: found again -> occurrences + 1 (a fixed bug is reopened as a regression); the
same check passing later -> fixed. The tester can dismiss a bug (not a bug) or reopen it."""
from __future__ import annotations

import re
import sqlite3
from typing import Any

from ..errors import Invalid, NotFound
from ..pagination import PageParams, finish, keyset
from ..util import new_id, now_iso
from . import row, rows

STATUSES = ("open", "fixed", "dismissed")


def _signature(*parts: str | None) -> str:
    return " | ".join(re.sub(r"\s+", " ", (p or "")).strip().lower() for p in parts)[:500]


def findings(report: dict[str, Any]) -> list[dict[str, Any]]:
    """The application bugs a finished test run shows (empty unless it failed on the application)."""
    if report.get("status") != "failed" or report.get("errorSource") != "application":
        return []
    steps = report.get("steps", [])

    def evidence(description: str | None) -> tuple[str | None, str | None]:
        step = next((s for s in reversed(steps) if s.get("description") == description
                     and s.get("status") in ("failed", "error")), None)
        return (step or {}).get("pageUrl"), ((step or {}).get("screenshots") or [None])[0]

    out: list[dict[str, Any]] = []
    agent = report.get("agent")
    failure = report.get("failure") or {}
    if agent is not None:
        for e in agent.get("verification", []):
            if e.get("status") != "failed":
                continue
            c = [c for c in e.get("checks", []) if not c.get("passed")][-1]
            page_url, shot = evidence(c["description"])
            out.append({"source": "check", "signature": _signature("check", e["action"], c["description"]),
                        "action": e["action"], "check": c["description"],
                        "expected": c.get("expected") or e.get("expected"), "actual": c.get("actual") or c.get("detail"),
                        "page_url": page_url, "screenshot_url": shot})
        summary = agent.get("summary")
        if out and summary and report.get("errorKind") == "agent_reported_failure":
            for f in out:  # the agent's own words describe the bug best
                f["title"] = summary
        if not out:
            where = failure.get("step") or "the goal"
            page_url, shot = evidence(failure.get("step"))
            out.append({"source": "agent", "signature": _signature("agent", where, None), "action": where,
                        "check": None, "expected": failure.get("expected"),
                        "actual": summary or failure.get("actual") or failure.get("message"),
                        "page_url": page_url, "screenshot_url": shot, "title": summary})
    elif failure.get("step"):
        page_url, shot = evidence(failure["step"])
        out.append({"source": "step", "signature": _signature("step", failure["step"], failure.get("expected")),
                    "action": failure["step"], "check": failure["step"], "expected": failure.get("expected"),
                    "actual": failure.get("actual") or failure.get("message"), "page_url": page_url,
                    "screenshot_url": shot})
    for f in out:
        f.setdefault("title", None)
        if not f["title"]:
            f["title"] = (f'"{f["check"]}" failed after "{f["action"]}"' if f["source"] == "check"
                          else f'{f["action"]}: expected {f["expected"]}, got {f["actual"]}')
        f["title"] = f["title"][:300]
    return out


def record_run(conn: sqlite3.Connection, project_id: str, test_run: dict[str, Any], report: dict[str, Any]) -> list[str]:
    """Called once per finished test run (with its report): records new and recurring bugs and
    marks open bugs fixed when the same check or step passed. Returns the ids of bugs found."""
    if report.get("status") not in ("passed", "failed"):
        return []
    tcid, now = test_run["test_case_id"], now_iso()
    found = []
    for f in findings(report):
        existing = conn.execute("SELECT * FROM bugs WHERE project_id = ? AND test_case_id = ? AND signature = ?",
                                (project_id, tcid, f["signature"])).fetchone()
        if existing is None:
            bid = new_id()
            conn.execute(
                "INSERT INTO bugs (id, project_id, test_case_id, signature, source, title, action, check_text, expected, "
                "actual, page_url, screenshot_url, status, first_test_run_id, last_test_run_id, last_run_id, "
                "first_seen_at, last_seen_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'open', ?, ?, ?, ?, ?, ?)",
                (bid, project_id, tcid, f["signature"], f["source"], f["title"], f["action"], f["check"],
                 _clip(f["expected"]), _clip(f["actual"]), f["page_url"], f["screenshot_url"], test_run["id"],
                 test_run["id"], test_run["run_id"], now, now, now))
        else:
            bid = existing["id"]
            reopened = existing["status"] == "fixed"
            conn.execute(
                "UPDATE bugs SET title = ?, action = ?, expected = ?, actual = ?, page_url = ?, "
                "screenshot_url = COALESCE(?, screenshot_url), occurrences = occurrences + 1, last_test_run_id = ?, "
                "last_run_id = ?, last_seen_at = ?, updated_at = ?, "
                "status = CASE status WHEN 'fixed' THEN 'open' ELSE status END, regressed = regressed OR ?, "
                "fixed_test_run_id = CASE WHEN ? THEN NULL ELSE fixed_test_run_id END "
                "WHERE project_id = ? AND id = ?",
                (f["title"], f["action"], _clip(f["expected"]), _clip(f["actual"]), f["page_url"], f["screenshot_url"],
                 test_run["id"], test_run["run_id"], now, now, int(reopened), int(reopened), project_id, bid))
        found.append(bid)
    passed = {s.get("description") for s in report.get("steps", []) if s.get("status") == "passed"}
    for b in conn.execute("SELECT * FROM bugs WHERE project_id = ? AND test_case_id = ? AND status = 'open'",
                          (project_id, tcid)).fetchall():
        if b["id"] in found:
            continue
        if (b["source"] == "agent" and report["status"] == "passed") or (b["check_text"] and b["check_text"] in passed):
            conn.execute("UPDATE bugs SET status = 'fixed', fixed_test_run_id = ?, updated_at = ? "
                         "WHERE project_id = ? AND id = ?", (test_run["id"], now, project_id, b["id"]))
    return found


def _clip(text: Any) -> str | None:
    return None if text is None else str(text)[:1000]


_SELECT = ("SELECT b.*, tc.title AS test_title FROM bugs b "
           "JOIN test_cases tc ON tc.project_id = b.project_id AND tc.id = b.test_case_id")


def list_bugs(conn: sqlite3.Connection, project_id: str, statuses: list[str] | None, test_case_id: str | None,
              page: PageParams) -> tuple[list[dict[str, Any]], str | None]:
    where, params = " WHERE b.project_id = ?", [project_id]
    if statuses:
        where += f" AND b.status IN ({', '.join('?' * len(statuses))})"
        params += statuses
    if test_case_id:
        where += " AND b.test_case_id = ?"
        params.append(test_case_id)
    extra, kparams, tail = keyset("b.last_seen_at", page, "b.id")
    rs = conn.execute(f"{_SELECT}{where}{extra}{tail}", (*params, *kparams)).fetchall()
    return finish(rows(rs), page, "last_seen_at", f"bugs:{project_id}")


def counts(conn: sqlite3.Connection, project_id: str) -> dict[str, int]:
    got = dict(conn.execute("SELECT status, COUNT(*) FROM bugs WHERE project_id = ? GROUP BY status",
                            (project_id,)).fetchall())
    return {s: got.get(s, 0) for s in STATUSES}


def get_bug(conn: sqlite3.Connection, project_id: str, bug_id: str) -> dict[str, Any]:
    b = row(conn.execute(f"{_SELECT} WHERE b.project_id = ? AND b.id = ?", (project_id, bug_id)).fetchone())
    if b is None:
        raise NotFound("Bug not found in this project.", code="bug_not_found")
    return b


def update_bug(conn: sqlite3.Connection, project_id: str, bug_id: str, status: str | None,
               note: str | None) -> dict[str, Any]:
    get_bug(conn, project_id, bug_id)
    if status is not None and status not in STATUSES:
        raise Invalid(f"status must be one of {', '.join(STATUSES)}", code="invalid_status")
    if note is not None and len(note) > 1000:
        raise Invalid("note is too long (max 1000 characters)", code="invalid_note")
    conn.execute("UPDATE bugs SET status = COALESCE(?, status), note = COALESCE(?, note), updated_at = ? "
                 "WHERE project_id = ? AND id = ?", (status, note, now_iso(), project_id, bug_id))
    return get_bug(conn, project_id, bug_id)


def for_test(conn: sqlite3.Connection, project_id: str, test_case_id: str) -> list[dict[str, Any]]:
    """Open and dismissed bugs of one test, for the agent's memory (most recent first)."""
    return rows(conn.execute(
        "SELECT * FROM bugs WHERE project_id = ? AND test_case_id = ? AND status IN ('open', 'dismissed') "
        "ORDER BY last_seen_at DESC LIMIT 10", (project_id, test_case_id)).fetchall())
