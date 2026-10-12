"""Deduplicated LI.FI reads with shared durable quota and paced dispatch."""

from __future__ import annotations

import json
import math
import time
from contextlib import contextmanager
from hashlib import sha256
from pathlib import Path
from threading import BoundedSemaphore, Lock

import httpx

from .lifi_quota import (
    AuthenticatedLifiQuotaStore,
    DispatchWait,
    LifiQuotaStore,
    LifiRequestLimit,
)
from .stages import StageDeadlineExceeded, check_deadline, current_deadline, request_timeout

_REGISTRY_LOCK = Lock()
_SCOPE_SLOTS: dict[tuple[str, str], BoundedSemaphore] = {}
_KEY_LOCKS: dict[tuple[str, str, str], Lock] = {}


class LifiHttpClient:
    supports_cache_namespace = True

    def __init__(
        self,
        client: httpx.Client,
        *,
        db_path: Path,
        proxy: str,
        purpose="planning",
        clock=None,
        sleep=None,
        base_url="https://li.quest/",
        provider="lifi",
        quota_store=LifiQuotaStore,
        api_key: str = "",
        max_attempts: int = 3,
        quota_wait_seconds: float = 0,
    ):
        if purpose not in ("planning", "execution"):
            raise ValueError("invalid LI.FI request purpose")
        if (isinstance(max_attempts, bool) or not isinstance(max_attempts, int)
                or not 1 <= max_attempts <= 10):
            raise ValueError("max_attempts must be an integer between 1 and 10")
        if not math.isfinite(quota_wait_seconds) or quota_wait_seconds < 0:
            raise ValueError("quota_wait_seconds must be finite and nonnegative")
        self.client = client
        self.base_url = base_url
        self.api_key = api_key
        self.max_attempts = max_attempts
        self.quota_wait_seconds = quota_wait_seconds
        scope_input = ("lifi-key:" + api_key) if api_key and provider == "lifi" else (
            proxy if provider == "lifi" else provider + ":" + proxy
        )
        if api_key and provider == "lifi":
            quota_store = AuthenticatedLifiQuotaStore
        self.scope = sha256(scope_input.encode()).hexdigest()
        self.db_path = db_path
        self.purpose = purpose
        self.clock = clock or (lambda: time.time())
        self.sleep = sleep or time.sleep
        self.quota = quota_store(db_path, self.scope, clock=self.clock)
        self._registry_key = (str(Path(db_path).resolve()), self.scope)
        self._lock = Lock()
        self._metrics = {
            "requests": 0,
            "http_attempts": 0,
            "retries": 0,
            "reservations": 0,
            "cache_hits": 0,
            "singleflight_hits": 0,
            "limited": 0,
            "quota_waits": 0,
            "provider_429": 0,
            "http_attempts_by_bucket": {b: 0 for b in self.quota.limits},
        }
        with _REGISTRY_LOCK:
            self._slots = _SCOPE_SLOTS.setdefault(self._registry_key, BoundedSemaphore(2))
        with self._db() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS lifi_read_cache (
                    scope TEXT, cache_key TEXT, expires_at REAL, body TEXT,
                    PRIMARY KEY(scope, cache_key));
                CREATE TABLE IF NOT EXISTS lifi_search_progress (
                    scope TEXT, search_key TEXT, updated_at REAL, payload TEXT,
                    PRIMARY KEY(scope, search_key));
            """)
            columns = {row[1] for row in db.execute("PRAGMA table_info(lifi_read_cache)")}
            if "observed_at" not in columns:
                db.execute("ALTER TABLE lifi_read_cache ADD COLUMN observed_at REAL")

    @property
    def metrics(self):
        with self._lock:
            result = {
                **self._metrics,
                "http_attempts_by_bucket": dict(self._metrics["http_attempts_by_bucket"]),
            }
        result["quota"] = self.quota.snapshot()
        return result

    def _increment(self, key):
        with self._lock:
            self._metrics[key] = self._metrics.get(key, 0) + 1

    def record_retry(self):
        self._increment("retries")

    def record_search(self, key: str, data: dict):
        with self._db() as db:
            db.execute(
                "INSERT OR REPLACE INTO lifi_search_progress VALUES (?, ?, ?, ?)",
                (self.scope, key, self.clock(), json.dumps(data, sort_keys=True)),
            )

    def _db(self):
        return self.quota.db()

    @contextmanager
    def _request_slot(self, key):
        with _REGISTRY_LOCK:
            key_lock = _KEY_LOCKS.setdefault((*self._registry_key, key), Lock())

        def acquire(lock):
            deadline = current_deadline()
            if deadline is None or deadline.remaining() is None:
                lock.acquire()
            elif not lock.acquire(timeout=deadline.remaining()):
                raise StageDeadlineExceeded("LI.FI request queue deadline exceeded")

        waited = key_lock.locked()
        acquire(key_lock)
        try:
            yield waited
        finally:
            key_lock.release()

    @contextmanager
    def _inflight(self):
        deadline = current_deadline()
        if deadline is None or deadline.remaining() is None:
            self._slots.acquire()
        elif not self._slots.acquire(timeout=deadline.remaining()):
            raise StageDeadlineExceeded("LI.FI request queue deadline exceeded")
        try:
            yield
        finally:
            self._slots.release()

    def get(self, url, **kwargs):
        return self._request("GET", url, kwargs)

    def post(self, url, **kwargs):
        return self._request("POST", url, kwargs)

    def _request(self, method, url, kwargs):
        if not url.startswith(self.base_url):
            return self.client.request(method, url, **kwargs)
        if self.api_key:
            header = "x-lifi-api-key" if self.base_url == "https://li.quest/" else "x-api-key"
            kwargs["headers"] = {**kwargs.get("headers", {}), header: self.api_key}
        wait_until = self.clock() + self.quota_wait_seconds
        for number in range(self.max_attempts):
            try:
                response = self._request_once(method, url, dict(kwargs))
            except LifiRequestLimit as error:
                if (error.retry_at is None or error.retry_at > wait_until
                        or number + 1 == self.max_attempts):
                    raise
                delay = max(0, error.retry_at - self.clock())
            except httpx.TransportError:
                if number + 1 == self.max_attempts:
                    raise
                delay = 2 ** number
            else:
                if response.status_code not in {500, 502, 503, 504}:
                    return response
                if number + 1 == self.max_attempts:
                    return response
                delay = 2 ** number
            self.record_retry()
            while delay > 0:
                check_deadline()
                step = min(1, delay)
                self.sleep(step)
                delay -= step
            check_deadline()
        raise RuntimeError("HTTP retry loop exhausted")

    def _request_once(self, method, url, kwargs):
        cache_namespace = kwargs.pop("cache_namespace", None)
        if not url.startswith(self.base_url):
            return self.client.request(method, url, **kwargs)
        bucket = (
            "steps"
            if url.endswith("/stepTransaction")
            else "routes"
            if url.endswith(("/routes", "/quote", "/quote/v2"))
            else "public"
        )
        ttl = (
            240
            if bucket == "routes"
            else 120
            if bucket == "steps"
            else 10
            if url.endswith("/status")
            else 60
        )
        key = sha256(
            json.dumps(
                [method, url, kwargs.get("params"), kwargs.get("json"), cache_namespace],
                sort_keys=True
            ).encode()
        ).hexdigest()
        with self._request_slot(key) as waited:
            check_deadline()
            with self._db() as db:
                cached = db.execute(
                    "SELECT body,observed_at FROM lifi_read_cache WHERE scope=? AND "
                    "cache_key=? AND expires_at>? AND observed_at IS NOT NULL",
                    (self.scope, key, self.clock()),
                ).fetchone()
            if cached:
                self._increment("cache_hits")
                if waited:
                    self._increment("singleflight_hits")
                return httpx.Response(
                    200,
                    json=json.loads(cached[0]),
                    request=httpx.Request(method, url),
                    headers={"x-inventory-observed-at": str(cached[1])},
                )
            try:
                with self._inflight():
                    dispatch = self.quota.reserve_dispatch_time()
                    while dispatch > self.clock():
                        check_deadline()
                        self.sleep(min(1, max(0, dispatch - self.clock())))
                    check_deadline()
                    kwargs["timeout"] = request_timeout(kwargs.get("timeout", 30))
                    while True:
                        check_deadline()
                        try:
                            attempt = self.quota.reserve_request(
                                bucket, key, purpose=self.purpose, paced=True
                            )
                            break
                        except DispatchWait as delay:
                            self.sleep(min(1, max(0, delay.retry_at - self.clock())))
                    self._increment("reservations")
                    while True:
                        check_deadline()
                        try:
                            self.quota.mark_started(attempt, paced=True)
                            break
                        except DispatchWait as delay:
                            self.sleep(min(1, max(0, delay.retry_at - self.clock())))
                    kwargs["timeout"] = request_timeout(kwargs.get("timeout", 30))
                    self._increment("requests")
                    self._increment("http_attempts")
                    self._increment(bucket + "_requests")
                    with self._lock:
                        self._metrics["http_attempts_by_bucket"][bucket] += 1
                    response = self.client.request(method, url, **kwargs)
                    finished = self.clock()
                    response.headers["x-inventory-observed-at"] = str(finished)
                    limited = self.quota.observe(
                        bucket, attempt, response.headers, response.status_code, response.text
                    )
                    if limited:
                        self._increment("provider_429")
                        raise limited
                    if response.is_success:
                        with self._db() as db:
                            db.execute(
                                "INSERT OR REPLACE INTO "
                                "lifi_read_cache(scope,cache_key,expires_at,body,observed_at) "
                                "VALUES (?,?,?,?,?)",
                                (self.scope, key, finished + ttl, response.text, finished),
                            )
                    return response
            except LifiRequestLimit:
                self._increment("limited")
                self._increment("quota_waits")
                raise

    def close(self):
        self.client.close()
