"""Small shared helpers: ids, timestamps, JSON."""
from __future__ import annotations

import json
import threading
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any

_clock_lock = threading.Lock()
_last_now = datetime.min.replace(tzinfo=timezone.utc)


def new_id() -> str:
    return uuid.uuid4().hex


def utcnow() -> datetime:
    """Strictly increasing within the process (the Windows clock can repeat values), so
    rows created in one request keep their insertion order when sorted by timestamp."""
    global _last_now
    with _clock_lock:
        now = datetime.now(timezone.utc)
        if now <= _last_now:
            now = _last_now + timedelta(microseconds=1)
        _last_now = now
        return now


def iso(dt: datetime) -> str:
    """Fixed-width UTC timestamp, so string order equals time order."""
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def now_iso() -> str:
    return iso(utcnow())


def parse_iso(value: str) -> datetime:
    dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def dumps(obj: Any) -> str:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def loads(raw: str | None, default: Any = None) -> Any:
    if raw is None:
        return default
    return json.loads(raw)
