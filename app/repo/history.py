"""Project page history: pages (by normalized identity), versions (distinct element
structures) and observations (which run saw which version, when)."""
from __future__ import annotations

import hashlib
import re
import sqlite3
from dataclasses import dataclass
from typing import Any

from ..errors import Invalid, NotFound
from ..pagination import PageParams, finish, keyset
from ..urls import page_identity, sanitize_url
from ..util import dumps, loads, new_id, now_iso
from . import like_pattern, row, rows

MAX_ELEMENTS = 400
_ELEMENT_FIELDS = ("tag", "type", "role", "name", "label", "placeholder", "testid", "testidAttr", "id", "nameAttr", "icon", "row", "rowIndex",
                   "text", "href", "visible", "enabled", "editable", "clickable", "context", "css")


def clean_element(el: dict[str, Any]) -> dict[str, Any]:
    """Whitelist of stored element metadata (the extractor never reads form values)."""
    return {k: el.get(k) for k in _ELEMENT_FIELDS if el.get(k) not in (None, "")}


def _structure_hash(elements: list[dict[str, Any]]) -> str:
    """Identity of the page structure; digits are masked so counters/dates don't create versions."""
    def mask(v: Any) -> str:
        return re.sub(r"\d+", "#", str(v or ""))
    keys = sorted(
        "|".join(mask(e.get(k)) for k in ("tag", "type", "role", "name", "testid", "id", "nameAttr", "placeholder", "icon"))
        for e in elements
    )
    return hashlib.sha1("\n".join(keys).encode()).hexdigest()


def ensure_page(conn: sqlite3.Connection, project_id: str, url: str, title: str | None = None) -> str:
    key = page_identity(url)
    ts = now_iso()
    existing = conn.execute("SELECT id FROM pages WHERE project_id = ? AND url_key = ?", (project_id, key)).fetchone()
    if existing:
        conn.execute("UPDATE pages SET last_seen_at = ?, title = COALESCE(?, title) WHERE id = ?", (ts, title, existing[0]))
        return existing[0]
    pid = new_id()
    conn.execute("INSERT INTO pages (id, project_id, url_key, title, first_seen_at, last_seen_at) VALUES (?, ?, ?, ?, ?, ?)",
                 (pid, project_id, key, title, ts, ts))
    return pid


def observe_page(conn: sqlite3.Connection, project_id: str, url: str, title: str | None,
                 elements: list[dict[str, Any]], test_run_id: str | None, step_index: int | None) -> tuple[str, str]:
    """Records that a run saw this page; returns (page_id, page_version_id)."""
    cleaned = [clean_element(e) for e in elements[:MAX_ELEMENTS]]
    page_id = ensure_page(conn, project_id, url, title)
    content_hash = _structure_hash(cleaned)
    ts = now_iso()
    version = conn.execute("SELECT id FROM page_versions WHERE project_id = ? AND page_id = ? AND content_hash = ?",
                           (project_id, page_id, content_hash)).fetchone()
    if version:
        version_id = version[0]
        conn.execute("UPDATE page_versions SET last_seen_at = ?, title = ? WHERE id = ?", (ts, title, version_id))
    else:
        version_id = new_id()
        number = conn.execute("SELECT COALESCE(MAX(version), 0) + 1 FROM page_versions WHERE page_id = ?",
                              (page_id,)).fetchone()[0]
        conn.execute(
            "INSERT INTO page_versions (id, project_id, page_id, version, content_hash, title, elements_json, element_count, "
            "first_seen_at, last_seen_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (version_id, project_id, page_id, number, content_hash, title, dumps(cleaned), len(cleaned), ts, ts),
        )
    conn.execute("UPDATE pages SET latest_version_id = ? WHERE id = ?", (version_id, page_id))
    conn.execute(
        "INSERT INTO page_observations (id, project_id, page_id, page_version_id, test_run_id, step_index, url, observed_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (new_id(), project_id, page_id, version_id, test_run_id, step_index, sanitize_url(url), ts),
    )
    return page_id, version_id


def find_reference(conn: sqlite3.Connection, project_id: str, fingerprint: str, page_id: str | None
                   ) -> dict[str, Any] | None:
    """Last clean (non-healed) success for the same target on the same page of the same project."""
    if page_id is None:
        return None
    r = conn.execute(
        "SELECT id, element_json, finished_at FROM step_results "
        "WHERE project_id = ? AND target_fingerprint = ? AND page_id = ? AND status = 'passed' AND healed = 0 "
        "AND element_json IS NOT NULL ORDER BY finished_at DESC LIMIT 1",
        (project_id, fingerprint, page_id),
    ).fetchone()
    if r is None:
        return None
    return {"stepResultId": r["id"], "element": loads(r["element_json"]), "observedAt": r["finished_at"]}


def known_pages(conn: sqlite3.Connection, project_id: str, limit: int = 50) -> list[tuple[str, str | None]]:
    return [(r[0], r[1]) for r in conn.execute(
        "SELECT url_key, title FROM pages WHERE project_id = ? ORDER BY last_seen_at DESC, id DESC LIMIT ?",
        (project_id, limit),
    )]


@dataclass
class PageFilters:
    q: str | None = None
    freshness: str = "all"  # all | fresh | stale
    stale_before: str | None = None


def list_pages(conn: sqlite3.Connection, project_id: str, f: PageFilters, page: PageParams
               ) -> tuple[list[dict[str, Any]], str | None]:
    clauses, params = ["project_id = ?"], [project_id]
    if f.q:
        clauses.append("(url_key LIKE ? ESCAPE '\\' OR title LIKE ? ESCAPE '\\')")
        pattern = like_pattern(f.q)
        params += [pattern, pattern]
    if f.freshness not in {"all", "fresh", "stale"}:
        raise Invalid("freshness must be all, fresh or stale", code="invalid_filter")
    if f.freshness != "all":
        clauses.append("last_seen_at >= ?" if f.freshness == "fresh" else "last_seen_at < ?")
        params.append(f.stale_before)
    where, kparams, tail = keyset("last_seen_at", page)
    rs = conn.execute(f"SELECT * FROM pages WHERE {' AND '.join(clauses)}{where}{tail}", (*params, *kparams)).fetchall()
    return finish(rows(rs), page, "last_seen_at", f"pages:{project_id}")


def get_page(conn: sqlite3.Connection, project_id: str, page_id: str) -> dict[str, Any]:
    p = row(conn.execute("SELECT * FROM pages WHERE project_id = ? AND id = ?", (project_id, page_id)).fetchone())
    if p is None:
        raise NotFound("Page not found in this project.", code="page_not_found")
    p["latest_version"] = row(conn.execute("SELECT * FROM page_versions WHERE project_id = ? AND id = ?",
                                           (project_id, p["latest_version_id"])).fetchone()) if p["latest_version_id"] else None
    return p


def list_versions(conn: sqlite3.Connection, project_id: str, page_id: str, page: PageParams
                  ) -> tuple[list[dict[str, Any]], str | None]:
    get_page(conn, project_id, page_id)
    where, params, tail = keyset("last_seen_at", page)
    rs = conn.execute(f"SELECT * FROM page_versions WHERE project_id = ? AND page_id = ?{where}{tail}",
                      (project_id, page_id, *params)).fetchall()
    return finish(rows(rs), page, "last_seen_at", f"page-versions:{project_id}:{page_id}")


def observations_for_run(conn: sqlite3.Connection, project_id: str, test_run_id: str) -> list[dict[str, Any]]:
    return rows(conn.execute(
        "SELECT o.*, p.url_key, v.version FROM page_observations o JOIN pages p ON p.id = o.page_id "
        "JOIN page_versions v ON v.id = o.page_version_id WHERE o.project_id = ? AND o.test_run_id = ? "
        "ORDER BY o.observed_at, o.id", (project_id, test_run_id),
    ).fetchall())
