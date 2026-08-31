from __future__ import annotations

import hashlib
import hmac
import json
import os
from typing import Any, Iterable


def _secret() -> bytes:
    value = os.getenv("SESSION_SECRET")
    if not value:
        # Development/tests need deterministic handles; production already rejects a
        # missing SESSION_SECRET during application startup.
        value = "forwantofanail-development-session-secret"
    return value.encode("utf-8")


def opaque_handle(prefix: str, *parts: Any) -> str:
    encoded = json.dumps(parts, sort_keys=True, separators=(",", ":"), default=str).encode("utf-8")
    digest = hmac.new(_secret(), encoded, hashlib.sha256).hexdigest()
    return f"{prefix}_{digest}"


def token_binding(raw_token: str) -> str:
    return hashlib.sha256(raw_token.encode("utf-8")).hexdigest()


def matches(value: str, expected: str) -> bool:
    return isinstance(value, str) and hmac.compare_digest(value, expected)


def find_matching_handle(value: str, candidates: Iterable[tuple[str, Any]]) -> Any | None:
    found = None
    for expected, candidate in candidates:
        if matches(value, expected):
            found = candidate
    return found
