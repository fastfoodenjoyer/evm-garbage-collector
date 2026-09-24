"""Bounded, paced JSON transport. Never expose provider URLs in errors."""

import math
import os
import random
import re
import time
import traceback
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from urllib.parse import urlsplit

import httpx


class RequestError(Exception):
    def __init__(
        self, code: str, retry_after: float | None = None,
        *, diagnostic: dict | None = None,
    ):
        super().__init__(code)
        self.code = code
        self.retry_after = retry_after
        self.diagnostic = diagnostic


def resolve_url(template: str) -> str:
    def replace(match):
        value = os.environ.get(match[1])
        if not value:
            raise RequestError("missing_rpc_environment")
        return value

    return re.sub(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}", replace, template)


class Transport:
    def __init__(self, client=None, interval=1.0, sleep=time.sleep, clock=time.monotonic):
        if not isinstance(interval, (int, float)) or not math.isfinite(interval) or interval < 0:
            raise ValueError("interval must be finite and nonnegative")
        self.client = client or httpx.Client(timeout=20, follow_redirects=False)
        self.interval = interval
        self.sleep = sleep
        self.clock = clock
        self.last = {}

    def close(self):
        self.client.close()

    def post(self, url: str, payload: dict) -> dict:
        url = resolve_url(url)
        diagnostic: dict = {"rpc_method": payload.get("method"), "attempts": []}
        try:
            parsed = urlsplit(url)
            if parsed.scheme not in ("http", "https") or not parsed.hostname:
                raise ValueError()
            host = parsed.hostname
        except ValueError:
            raise RequestError("invalid_rpc_url") from None
        for attempt in range(3):
            delay = self.interval - (self.clock() - self.last.get(host, float("-inf")))
            if delay > 0:
                self.sleep(delay)
            self.last[host] = self.clock()
            try:
                response = self.client.post(url, json=payload)
            except httpx.InvalidURL as exc:
                diagnostic["attempts"].append({
                    "attempt": attempt + 1, "exception_type": type(exc).__name__,
                    "exception": str(exc),
                    "traceback": "".join(traceback.format_exception(exc)),
                })
                raise RequestError("invalid_rpc_url", diagnostic=diagnostic) from exc
            except httpx.HTTPError as exc:
                diagnostic["attempts"].append({
                    "attempt": attempt + 1, "exception_type": type(exc).__name__,
                    "exception": str(exc),
                    "traceback": "".join(traceback.format_exception(exc)),
                })
                code, wait = "network_error", 2**attempt + random.random()
            else:
                if response.status_code == 200:
                    try:
                        result = response.json()
                    except ValueError as exc:
                        diagnostic["attempts"].append({
                            "attempt": attempt + 1, "http_status": response.status_code,
                            "response_headers": dict(response.headers),
                            "response_body": response.text,
                        })
                        raise RequestError("invalid_json", diagnostic=diagnostic) from exc
                    if not isinstance(result, dict):
                        diagnostic["attempts"].append({
                            "attempt": attempt + 1, "http_status": response.status_code,
                            "response_headers": dict(response.headers),
                            "response_body": response.text,
                        })
                        raise RequestError("invalid_response", diagnostic=diagnostic)
                    return result
                diagnostic["attempts"].append({
                    "attempt": attempt + 1, "http_status": response.status_code,
                    "response_headers": dict(response.headers),
                    "response_body": response.text,
                })
                if response.status_code in (401, 403):
                    raise RequestError("provider_access_denied", diagnostic=diagnostic)
                if response.status_code not in (408, 429) and response.status_code < 500:
                    raise RequestError(f"http_{response.status_code}", diagnostic=diagnostic)
                code = "rate_limited" if response.status_code == 429 else "provider_unavailable"
                wait = 2**attempt + random.random()
                header = response.headers.get("retry-after")
                if header:
                    try:
                        wait = max(wait, float(header))
                    except ValueError:
                        try:
                            wait = max(
                                wait,
                                (parsedate_to_datetime(header) - datetime.now(UTC)).total_seconds(),
                            )
                        except (ValueError, TypeError):
                            pass
            if wait > 30:
                raise RequestError(code, time.time() + wait, diagnostic=diagnostic)
            if attempt == 2:
                raise RequestError(code, diagnostic=diagnostic)
            self.sleep(wait)
        raise AssertionError("unreachable")
