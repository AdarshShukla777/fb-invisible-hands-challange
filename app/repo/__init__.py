"""Data access. Every function touching project-owned data takes project_id and filters
on it first; lookups by id use (project_id, id), so a foreign id behaves as not found."""
from __future__ import annotations

import sqlite3
from typing import Any

from ..errors import Invalid

TERMINAL = ("passed", "failed", "error", "cancelled")
ACTIVE = ("queued", "running")
RUN_STATUSES = ("queued", "running", "passed", "failed", "error", "cancelled")


def row(r: sqlite3.Row | None) -> dict[str, Any] | None:
    return dict(r) if r is not None else None


def rows(rs: list[sqlite3.Row]) -> list[dict[str, Any]]:
    return [dict(r) for r in rs]


def in_clause(values: list[Any]) -> str:
    return "(" + ",".join("?" for _ in values) + ")"


def like_pattern(q: str) -> str:
    q = q.strip()
    if len(q) > 200:
        raise Invalid("search text is too long (max 200 characters)", code="invalid_query")
    return "%" + q.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"
