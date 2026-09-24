"""Detailed error records with credentials removed before persistence."""

from __future__ import annotations

import os
import re
import traceback
from typing import Any

import httpx

_URL = re.compile(r"(?i)(?:https?|socks5h?)://[^\s<>\"']+")
_PRIVATE_HEX = re.compile(r"(?i)(?<![0-9a-f])(?:0x)?[0-9a-f]{64}(?![0-9a-f])")
_BEARER = re.compile(r"(?i)\bbearer\s+[^\s,;]+")
_ASSIGNMENT = re.compile(
    r"(?i)\b(api[_-]?key|access[_-]?token|password|secret|private[_-]?key)"
    r"\s*[:=]\s*[^\s,;]+"
)
_SECRET_FIELD = re.compile(
    r"(?i)(authorization|cookie|password|secret|private.?key|api.?key|token|proxy)"
)
_SECRET_ENV = re.compile(r"(?i)(API_KEY|TOKEN|SECRET|PASSWORD|PRIVATE_KEY|PROXY)")


def redact_text(value: str) -> str:
    """Preserve the error text while removing credentials and signed payloads."""

    for key, secret in sorted(os.environ.items(), key=lambda item: len(item[1]), reverse=True):
        if _SECRET_ENV.search(key) and len(secret) >= 8:
            value = value.replace(secret, "[REDACTED_SECRET]")
    value = _URL.sub("[REDACTED_URL]", value)
    value = _BEARER.sub("Bearer [REDACTED_SECRET]", value)
    value = _ASSIGNMENT.sub(lambda match: f"{match.group(1)}=[REDACTED_SECRET]", value)
    return _PRIVATE_HEX.sub("[REDACTED_HEX64]", value)


def redact_data(value: Any) -> Any:
    if isinstance(value, dict):
        return {
            str(key): "[REDACTED_SECRET]" if _SECRET_FIELD.search(str(key))
            else redact_data(item)
            for key, item in value.items()
        }
    if isinstance(value, (list, tuple)):
        return [redact_data(item) for item in value]
    if isinstance(value, str):
        return redact_text(value)
    if value is None or isinstance(value, (bool, int, float)):
        return value
    return redact_text(repr(value))


def exception_diagnostic(exc: BaseException) -> dict[str, Any]:
    """Retain the exception chain, traceback, and provider context."""

    exceptions = []
    seen: set[int] = set()
    current: BaseException | None = exc
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        item: dict[str, Any] = {
            "type": type(current).__name__, "message": redact_text(str(current)),
        }
        code = getattr(current, "code", None)
        if code is not None:
            item["code"] = redact_data(code)
        diagnostic = getattr(current, "diagnostic", None)
        if diagnostic is not None:
            item["provider"] = redact_data(diagnostic)
        if isinstance(current, httpx.HTTPError):
            try:
                request = current.request
            except RuntimeError:
                request = None
            if request is not None:
                item["http_method"] = request.method
                item["http_url"] = redact_text(str(request.url))
        if isinstance(current, httpx.HTTPStatusError):
            item["http_status"] = current.response.status_code
            item["http_response_headers"] = redact_data(dict(current.response.headers))
            item["http_response_body"] = redact_text(current.response.text)
        exceptions.append(item)
        current = current.__cause__ or (
            None if current.__suppress_context__ else current.__context__
        )
    return {
        "exceptions": exceptions,
        "traceback": redact_text("".join(traceback.format_exception(exc))),
    }
