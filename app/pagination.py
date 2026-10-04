"""Bounded keyset pagination with deterministic ordering (sort column, then id).

A cursor encodes the last row's (sort value, id) plus the listing scope, so a
cursor from one listing/sort/project cannot be replayed against another."""
from __future__ import annotations

import base64
import json
from dataclasses import dataclass
from typing import Any, Sequence

from .errors import Invalid

DEFAULT_LIMIT = 50
MAX_LIMIT = 100


@dataclass(frozen=True)
class PageParams:
    limit: int
    cursor: tuple[str, str] | None
    order: str  # "asc" | "desc"


def parse_page(limit: int | None, cursor: str | None, order: str | None, scope: str) -> PageParams:
    if limit is None:
        limit = DEFAULT_LIMIT
    if not 1 <= limit <= MAX_LIMIT:
        raise Invalid(f"limit must be between 1 and {MAX_LIMIT}", code="invalid_limit")
    order = (order or "desc").lower()
    if order not in {"asc", "desc"}:
        raise Invalid("order must be 'asc' or 'desc'", code="invalid_order")
    return PageParams(limit=limit, cursor=decode_cursor(cursor, f"{scope}:{order}") if cursor else None, order=order)


def encode_cursor(sort_value: str, row_id: str, scope: str) -> str:
    raw = json.dumps([scope, sort_value, row_id], separators=(",", ":")).encode()
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def decode_cursor(token: str, scope: str) -> tuple[str, str]:
    try:
        padded = token + "=" * (-len(token) % 4)
        data = json.loads(base64.urlsafe_b64decode(padded.encode()))
        cur_scope, sort_value, row_id = data
        if not (isinstance(sort_value, str) and isinstance(row_id, str)):
            raise ValueError
    except Exception:
        raise Invalid("cursor is malformed", code="invalid_cursor") from None
    if cur_scope != scope:
        raise Invalid("cursor does not belong to this listing, sort order or project", code="invalid_cursor")
    return sort_value, row_id


def keyset(column: str, page: PageParams, id_column: str = "id") -> tuple[str, list[Any], str]:
    """Returns (extra WHERE clause, params, ORDER BY + LIMIT clause). Fetches limit+1
    rows so the caller can tell whether another page exists."""
    op = "<" if page.order == "desc" else ">"
    direction = "DESC" if page.order == "desc" else "ASC"
    where, params = "", []
    if page.cursor:
        where = f" AND ({column} {op} ? OR ({column} = ? AND {id_column} {op} ?))"
        params = [page.cursor[0], page.cursor[0], page.cursor[1]]
    return where, params, f" ORDER BY {column} {direction}, {id_column} {direction} LIMIT {page.limit + 1}"


def finish(rows: Sequence[Any], page: PageParams, sort_key: str, scope: str) -> tuple[list[Any], str | None]:
    items = list(rows[: page.limit])
    if len(rows) <= page.limit:
        return items, None
    last = items[-1]
    return items, encode_cursor(last[sort_key], last["id"], f"{scope}:{page.order}")
