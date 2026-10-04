"""URL handling for page history.

page_identity() decides which URLs are "the same page":
  * scheme/host lower-cased, default ports and credentials dropped;
  * trailing slash and duplicate slashes normalized;
  * ID-like path segments (numbers, UUIDs, long hex/tokens) become {id}, so /orders/1001 and
    /orders/1002 share history; in the query only opaque tokens and numeric values of
    id-named parameters (?id=17) are templated;
  * tracking parameters (utm_*, gclid, ...) and sensitive parameters (token, code, ...) are dropped;
  * all other query parameters are kept and sorted, because they can change what the
    page shows (e.g. ?tab=billing);
  * fragments are dropped unless they carry a client-side route (#/settings).

sanitize_url() is used whenever a URL is stored or reported: it keeps the URL
readable but replaces sensitive query values with REDACTED and removes user:pass@."""
from __future__ import annotations

import re
from urllib.parse import parse_qsl, urlencode, urljoin, urlsplit, urlunsplit

_TRACKING = {"gclid", "fbclid", "msclkid", "dclid", "mc_cid", "mc_eid", "_ga", "_gl", "igshid", "ref_src", "yclid"}
_SENSITIVE_EXACT = {
    "code", "state", "nonce", "sig", "signature", "key", "otp", "sid", "jwt", "auth", "ticket",
    "pwd", "pass", "passwd", "password", "hash", "hmac",
}
_SENSITIVE_PARTS = ("token", "secret", "password", "session", "apikey", "api_key", "access_key", "credential", "auth")
_ID_SEGMENT = re.compile(
    r"^(?:\d+|[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}|[0-9a-f]{16,}|(?=[a-z0-9_-]*\d)[a-z0-9_-]{24,})$",
    re.IGNORECASE,
)
_DEFAULT_PORTS = {"http": 80, "https": 443}


def is_sensitive_param(name: str) -> bool:
    n = name.lower()
    return n in _SENSITIVE_EXACT or any(part in n for part in _SENSITIVE_PARTS)


def _is_tracking(name: str) -> bool:
    n = name.lower()
    return n.startswith("utm_") or n in _TRACKING


def _netloc(parts) -> str:
    host = (parts.hostname or "").lower()
    if ":" in host:  # IPv6
        host = f"[{host}]"
    port = parts.port
    if port and port != _DEFAULT_PORTS.get(parts.scheme.lower()):
        host = f"{host}:{port}"
    return host


_OPAQUE_VALUE = re.compile(
    r"^(?:[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}|[0-9a-f]{16,}|(?=[a-z0-9_-]*\d)[a-z0-9_-]{24,})$",
    re.IGNORECASE,
)
_ID_PARAM = re.compile(r"^(?:id|uid|pid|.+[_-]id|.+[a-z]Id)$")


def _is_id_value(name: str, value: str) -> bool:
    """Query values keep their identity (?tab=2, ?step=3 are different pages) unless they are
    opaque tokens, or numeric values of id-named parameters (?id=17, ?order_id=9)."""
    return bool(_OPAQUE_VALUE.match(value)) or (value.isdigit() and bool(_ID_PARAM.match(name)))


def _template_path(path: str) -> str:
    segments = [s for s in path.split("/") if s]
    templated = ["{id}" if _ID_SEGMENT.match(s) else s for s in segments]
    return "/" + "/".join(templated)


def page_identity(url: str) -> str:
    parts = urlsplit(url.strip())
    scheme = parts.scheme.lower()
    path = _template_path(re.sub(r"/{2,}", "/", parts.path or "/"))
    query = sorted(
        (k, "{id}" if _is_id_value(k, v) else v)
        for k, v in parse_qsl(parts.query, keep_blank_values=True)
        if not _is_tracking(k) and not is_sensitive_param(k)
    )
    fragment = parts.fragment
    if fragment.startswith("/") or fragment.startswith("!/"):
        route, _, _ = fragment.lstrip("!").partition("?")
        fragment = _template_path(route)
    else:
        fragment = ""
    return urlunsplit((scheme, _netloc(parts), path, urlencode(query, safe="{}"), fragment))


def sanitize_url(url: str) -> str:
    parts = urlsplit(url)
    if parts.scheme not in {"http", "https"}:
        return url
    query = [(k, "REDACTED" if is_sensitive_param(k) else v) for k, v in parse_qsl(parts.query, keep_blank_values=True)]
    return urlunsplit((parts.scheme, _netloc(parts), parts.path, urlencode(query), parts.fragment))


def normalize_base_url(raw: str) -> str | None:
    """Validates a project URL (http/https with a host). Returns a canonical form or None."""
    s = raw.strip()
    if not s:
        return None
    if not re.match(r"^[a-z][a-z0-9+.-]*://", s, re.IGNORECASE):
        s = "http://" + s
    try:
        parts = urlsplit(s)
        parts.port  # raises on invalid port
    except ValueError:
        return None
    if parts.scheme.lower() not in {"http", "https"} or not parts.hostname:
        return None
    path = parts.path or "/"
    return urlunsplit((parts.scheme.lower(), _netloc(parts), path, parts.query, ""))


def origin(url: str) -> str:
    parts = urlsplit(url)
    return f"{parts.scheme.lower()}://{_netloc(parts)}"


def resolve(base_url: str, target: str) -> str:
    return urljoin(base_url, target.strip())


def same_origin(url: str, base_url: str) -> bool:
    return origin(url) == origin(base_url)
