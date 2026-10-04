"""Layout tracking: did the element a step acts on move or change size since the last run?

Every element action (both modes) measures its target just before acting (GEOMETRY_JS). The
measurement is stored with the step result under a key for "the same step on the same element":
page identity + action + element identity (plan mode: the step target's fingerprint, which stays
the same when healing finds the element under another name). It is compared with the last
earlier run of the same test that measured that step (the first run records the baseline):

  moved_region  the element is in another region (landmark / form), e.g. header -> footer
  moved         its offset inside its region changed by more than the tolerance
  shifted       only its page position changed: the whole region moved (e.g. content above grew)
  resized       its width or height changed by more than the tolerance

Runs are only compared at the same viewport size. A change is reported (report, run events),
never a failure: pixel positions also move with data, fonts and dynamic content."""
from __future__ import annotations

import hashlib
import sqlite3
from typing import Any

from playwright.sync_api import Error as PlaywrightError
from playwright.sync_api import Locator

from ..util import loads
from .dom import GEOMETRY_JS

_SUMMARY = {"moved_region": "moved to another region", "moved": "moved", "shifted": "shifted with its region",
            "resized": "resized"}


def measure(loc: Locator, page_key: str) -> dict[str, Any] | None:
    """Best effort: measuring must never fail a step."""
    try:
        geometry = loc.evaluate(GEOMETRY_JS, timeout=1500)
    except PlaywrightError:
        return None
    return {**geometry, "page": page_key} if geometry else None


def layout_key(page_key: str, action: str, identity: str) -> str:
    return hashlib.sha1(f"{page_key}|{action}|{identity}".encode()).hexdigest()[:24]


def identity_of(el: dict[str, Any] | None) -> str | None:
    """The element's identity (like the locator strategies): never its position."""
    if not el:
        return None
    parts = [el.get("role") or el.get("tag") or "", el.get("testid") or "", el.get("id") or "",
             (el.get("name") or el.get("label") or el.get("placeholder") or "")[:80], el.get("icon") or "",
             str(el.get("rowIndex") or "")]
    return "|".join(parts) if any(parts[1:]) else None  # role alone does not identify an element


def reference(conn: sqlite3.Connection, project_id: str, test_case_id: str, key: str,
              test_run_id: str) -> dict[str, Any] | None:
    r = conn.execute(
        "SELECT sr.layout_json, sr.test_run_id, sr.finished_at FROM step_results sr "
        "JOIN test_runs tr ON tr.project_id = sr.project_id AND tr.id = sr.test_run_id "
        "WHERE sr.project_id = ? AND sr.layout_key = ? AND sr.layout_json IS NOT NULL AND tr.test_case_id = ? "
        "AND sr.test_run_id != ? ORDER BY sr.finished_at DESC LIMIT 1",
        (project_id, key, test_case_id, test_run_id)).fetchone()
    if r is None:
        return None
    box = loads(r["layout_json"], {}).get("box")
    return {"box": box, "testRunId": r["test_run_id"], "at": r["finished_at"]} if box else None


def compare(box: dict[str, Any], ref: dict[str, Any] | None, tolerance: int) -> dict[str, Any] | None:
    """The change from the reference box, or None when there is nothing to compare."""
    if ref is None:
        return None
    old = ref["box"]
    if (old.get("vw"), old.get("vh")) != (box.get("vw"), box.get("vh")):
        return {"kinds": [], "summary": "not compared: the viewport size differs from the reference run"}
    dx, dy = box["x"] - old["x"], box["y"] - old["y"]
    dox, doy = box["ox"] - old["ox"], box["oy"] - old["oy"]
    dw, dh = box["w"] - old["w"], box["h"] - old["h"]
    far = lambda *d: any(abs(v) > tolerance for v in d)  # noqa: E731
    kinds, parts = [], []
    if box.get("region") != old.get("region"):
        kinds.append("moved_region")
        parts.append(f'moved from "{old.get("region")}" to "{box.get("region")}" ({dx:+d}px, {dy:+d}px)')
    elif far(dox, doy):
        kinds.append("moved")
        parts.append(f'moved {dox:+d}px, {doy:+d}px inside "{box.get("region")}"')
    elif far(dx, dy):
        kinds.append("shifted")
        parts.append(f'shifted {dx:+d}px, {dy:+d}px together with "{box.get("region")}"')
    if far(dw, dh):
        kinds.append("resized")
        parts.append(f'resized from {old["w"]}x{old["h"]} to {box["w"]}x{box["h"]}')
    return {"kinds": kinds, "dx": dx, "dy": dy, "dw": dw, "dh": dh, "summary": "; ".join(parts) or "unchanged"}


def track(conn: sqlite3.Connection, project_id: str, test_case_id: str, test_run_id: str, action: str,
          identity: str | None, geometry: dict[str, Any] | None, tolerance: int) -> tuple[str | None, dict | None]:
    """(layout_key, layout record) for a step result; (None, None) when the element was not measured."""
    if not geometry or not identity:
        return None, None
    key = layout_key(geometry.get("page") or "", action, identity)
    box = {k: v for k, v in geometry.items() if k != "page"}
    ref = reference(conn, project_id, test_case_id, key, test_run_id)
    change = compare(box, ref, tolerance)
    record: dict[str, Any] = {"box": box, "page": geometry.get("page"), "reference": None,
                              "change": {"kinds": [], "summary": "baseline: first measurement of this step"}}
    if ref:
        record["reference"] = {"testRunId": ref["testRunId"], "at": ref["at"], "box": ref["box"]}
        record["change"] = change
    return key, record


def changed(record: dict[str, Any] | None) -> bool:
    return bool(record and (record.get("change") or {}).get("kinds"))


def describe(record: dict[str, Any]) -> str:
    kinds = record["change"]["kinds"]
    return ", ".join(_SUMMARY[k] for k in kinds) + f': {record["change"]["summary"]}'
