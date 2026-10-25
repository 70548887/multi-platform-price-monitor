"""Bound credential envelopes shared by the local connector and collector."""

from __future__ import annotations

import json
import math
import re
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlsplit

from .base import CollectorError


SESSION_PREFIX = "pm-session-v1:"
SESSION_FORMAT = "pm-session-v1"
_NATIVE_HOSTS = {"xiaohong66.com", "www.xiaohong66.com"}


def normalize_https_origin(value: str) -> str:
    try:
        parts = urlsplit(value)
        if (parts.scheme != "https" or not parts.hostname or parts.username or parts.password
                or parts.path not in {"", "/"} or parts.query or parts.fragment
                or parts.port not in {None, 443}):
            raise ValueError
    except ValueError:
        raise CollectorError("Credential origin must be a plain HTTPS origin") from None
    return "https://" + parts.hostname.lower().rstrip(".")


def session_origin_matches(value: str, expected: str) -> bool:
    actual_origin, expected_origin = normalize_https_origin(value), normalize_https_origin(expected)
    if actual_origin == expected_origin:
        return True
    return urlsplit(actual_origin).hostname in _NATIVE_HOSTS and urlsplit(expected_origin).hostname in _NATIVE_HOSTS


def _cookie(value: Any, origin: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise CollectorError("Invalid session cookie")
    host = str(urlsplit(origin).hostname)
    domain = str(value.get("domain", host)).lower().rstrip(".")
    plain_domain = domain.lstrip(".")
    if not plain_domain or not (host == plain_domain or host.endswith("." + plain_domain)):
        raise CollectorError("Session cookie domain does not match its source origin")
    # Do not allow a native source cookie to widen to a public suffix.
    if host in _NATIVE_HOSTS and plain_domain not in _NATIVE_HOSTS:
        raise CollectorError("Session cookie domain is outside the source platform")
    if host not in _NATIVE_HOSTS and plain_domain != host:
        raise CollectorError("Custom source cookies must be bound to the exact source host")
    name, content = value.get("name"), value.get("value")
    path = value.get("path", "/")
    if not isinstance(name, str) or not re.fullmatch(r"[!#$%&'*+.^_`|~0-9A-Za-z-]{1,256}", name):
        raise CollectorError("Invalid session cookie name")
    if not isinstance(content, str) or any(ord(char) < 32 or ord(char) >= 127 for char in content):
        raise CollectorError("Invalid session cookie value")
    if not isinstance(path, str) or not path.startswith("/") or any(ord(char) < 32 for char in path):
        raise CollectorError("Invalid session cookie path")
    expiry = value.get("expires", -1)
    if isinstance(expiry, bool) or not isinstance(expiry, (int, float)) or not math.isfinite(expiry):
        raise CollectorError("Invalid session cookie expiry")
    secure = value.get("secure", False)
    if not isinstance(secure, bool):
        raise CollectorError("Invalid session cookie secure flag")
    return {"name": name, "value": content, "domain": domain, "path": path, "secure": secure, "expires": expiry}


@dataclass(slots=True)
class SourceSession:
    origin: str
    token: str
    cookies: list[dict[str, Any]]


def encode_session_envelope(origin: str, *, token: str = "", cookies: list[dict[str, Any]] | None = None) -> str:
    origin = normalize_https_origin(origin)
    if not isinstance(token, str) or any(ord(char) < 32 or ord(char) >= 127 for char in token):
        raise CollectorError("Invalid source authorization value")
    values = cookies or []
    if len(values) > 64:
        raise CollectorError("Source session contains too many cookies")
    normalized = [_cookie(value, origin) for value in values]
    if not token and not normalized:
        raise CollectorError("Source session contains no credentials")
    payload: dict[str, Any] = {"format": SESSION_FORMAT, "origin": origin, "cookies": normalized}
    if token:
        payload["token"] = token
    result = SESSION_PREFIX + json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
    if len(result) > 16384:
        raise CollectorError("Source session exceeds the supported encrypted-session size")
    return result


def decode_session_envelope(value: str, expected_origin: str) -> SourceSession:
    expected_origin = normalize_https_origin(expected_origin)
    if not isinstance(value, str):
        raise CollectorError("Invalid source session")
    if not value.startswith(SESSION_PREFIX):
        if any(ord(char) < 32 or ord(char) >= 127 for char in value):
            raise CollectorError("Invalid source authorization value")
        return SourceSession(expected_origin, value.strip(), [])
    if len(value) > 16384:
        raise CollectorError("Source session exceeds the supported encrypted-session size")
    try:
        payload = json.loads(value[len(SESSION_PREFIX):])
    except (ValueError, UnicodeError):
        raise CollectorError("Invalid source session envelope") from None
    if not isinstance(payload, dict) or payload.get("format") != SESSION_FORMAT:
        raise CollectorError("Unsupported source session envelope")
    origin = normalize_https_origin(str(payload.get("origin", "")))
    if not session_origin_matches(origin, expected_origin):
        raise CollectorError("Source session belongs to a different origin")
    cookies = payload.get("cookies", [])
    if not isinstance(cookies, list):
        raise CollectorError("Invalid source session cookie list")
    token = payload.get("token", "")
    # Validate with the same rules used by the browser-side exporter.
    validated = encode_session_envelope(origin, token=token, cookies=cookies)
    clean = json.loads(validated[len(SESSION_PREFIX):])
    return SourceSession(origin, clean.get("token", ""), clean["cookies"])
