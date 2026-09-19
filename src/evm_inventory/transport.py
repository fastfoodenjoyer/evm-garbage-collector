"""Bounded, paced JSON transport. Never expose provider URLs in errors."""
import math
import os
import random
import re
import time
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from urllib.parse import urlsplit

import httpx


class RequestError(Exception):
    def __init__(self, code: str, retry_after: float | None = None):
        super().__init__(code)
        self.code = code
        self.retry_after = retry_after


def resolve_url(template: str) -> str:
    def replace(match):
        value = os.environ.get(match[1])
        if not value:
            raise RequestError('missing_rpc_environment')
        return value
    return re.sub(r'\$\{([A-Za-z_][A-Za-z0-9_]*)\}', replace, template)


class Transport:
    def __init__(self, client=None, interval=1.0, sleep=time.sleep, clock=time.monotonic):
        if not isinstance(interval, (int, float)) or not math.isfinite(interval) or interval < 0:
            raise ValueError('interval must be finite and nonnegative')
        self.client = client or httpx.Client(timeout=20, follow_redirects=False)
        self.interval = interval
        self.sleep = sleep
        self.clock = clock
        self.last = {}

    def close(self):
        self.client.close()

    def post(self, url: str, payload: dict) -> dict:
        url = resolve_url(url)
        try:
            parsed = urlsplit(url)
            if parsed.scheme not in ('http', 'https') or not parsed.hostname:
                raise ValueError()
            host = parsed.hostname
        except ValueError:
            raise RequestError('invalid_rpc_url') from None
        for attempt in range(3):
            delay = self.interval - (self.clock() - self.last.get(host, float('-inf')))
            if delay > 0:
                self.sleep(delay)
            self.last[host] = self.clock()
            try:
                response = self.client.post(url, json=payload)
            except httpx.InvalidURL:
                raise RequestError('invalid_rpc_url') from None
            except httpx.HTTPError:
                code, wait = 'network_error', 2**attempt + random.random()
            else:
                if response.status_code == 200:
                    try:
                        result = response.json()
                    except ValueError:
                        raise RequestError('invalid_json') from None
                    if not isinstance(result, dict):
                        raise RequestError('invalid_response')
                    return result
                if response.status_code in (401, 403):
                    raise RequestError('provider_access_denied')
                if response.status_code not in (408, 429) and response.status_code < 500:
                    raise RequestError(f'http_{response.status_code}')
                code = 'rate_limited' if response.status_code == 429 else 'provider_unavailable'
                wait = 2**attempt + random.random()
                header = response.headers.get('retry-after')
                if header:
                    try:
                        wait = max(wait, float(header))
                    except ValueError:
                        try:
                            wait = max(wait, (parsedate_to_datetime(header) - datetime.now(UTC)).total_seconds())
                        except (ValueError, TypeError):
                            pass
            if wait > 30:
                raise RequestError(code, time.time() + wait)
            if attempt == 2:
                raise RequestError(code)
            self.sleep(wait)
        raise AssertionError('unreachable')
