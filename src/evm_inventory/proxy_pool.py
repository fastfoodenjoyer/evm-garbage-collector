"""Wallet-assigned LI.FI transports with durable quota and failover state."""

from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from hashlib import sha256
from urllib.parse import urlsplit

import httpx

from .lifi_http import LifiHttpClient, request_bucket
from .lifi_quota import LifiRequestLimit
from .stages import check_deadline


class ProxyPoolExhausted(LifiRequestLimit):
    """All assigned transports are temporarily unavailable for this bucket."""


class ProxyPoolHttpClient:
    supports_cache_namespace = True
    manages_retries = True

    def __init__(self, clients: list[LifiHttpClient], *, wallet: str):
        if not clients:
            raise ValueError("assigned transport pool must not be empty")
        self.clients = clients
        self.db_path = clients[0].db_path
        self.clock = clients[0].clock
        self.scope = sha256(("lifi-wallet:" + wallet.lower()).encode()).hexdigest()
        self.legacy_scopes = tuple(dict.fromkeys(c.scope for c in clients))
        self._ids = [c.transport_id for c in clients]
        self._switches = 0
        self._local_limited = 0
        self._failovers = 0
        self._pool_waits = 0
        with self._db() as db:
            db.execute(
                "CREATE TABLE IF NOT EXISTS lifi_transport_pools "
                "(scope TEXT PRIMARY KEY,current_id TEXT,assignment_json TEXT)"
            )
            db.execute(
                "CREATE TABLE IF NOT EXISTS lifi_transport_health "
                "(transport_id TEXT PRIMARY KEY,retry_at REAL)"
            )
            db.execute(
                "INSERT INTO lifi_transport_pools VALUES (?,?,?) "
                "ON CONFLICT(scope) DO UPDATE SET assignment_json=excluded.assignment_json",
                (self.scope, self._ids[0], json.dumps(self._ids)),
            )
            row = db.execute(
                "SELECT current_id FROM lifi_transport_pools WHERE scope=?", (self.scope,)
            ).fetchone()
            if row[0] not in self._ids:
                db.execute(
                    "UPDATE lifi_transport_pools SET current_id=? WHERE scope=?",
                    (self._ids[0], self.scope),
                )
        for child in clients:
            child.max_attempts = 1
            child.quota_wait_seconds = 0
            child.request_scope = self.scope
        self._migrate_cache()

    @contextmanager
    def _db(self):
        db = sqlite3.connect(self.db_path, timeout=30)
        try:
            with db:
                yield db
        finally:
            db.close()

    def _migrate_cache(self):
        # Carry forward fresh legacy HTTP results without extending their acquisition time.
        with self._db() as db:
            for old_scope in {c.scope for c in self.clients}:
                db.execute(
                    "INSERT OR IGNORE INTO lifi_read_cache "
                    "(scope,cache_key,expires_at,body,observed_at) "
                    "SELECT ?,cache_key,expires_at,body,observed_at FROM lifi_read_cache "
                    "WHERE scope=? AND expires_at>? AND observed_at IS NOT NULL",
                    (self.scope, old_scope, self.clock()),
                )

    def _index(self):
        with self._db() as db:
            current = db.execute(
                "SELECT current_id FROM lifi_transport_pools WHERE scope=?", (self.scope,)
            ).fetchone()[0]
        return self._ids.index(current) if current in self._ids else 0

    def _select(self, index):
        with self._db() as db:
            result = db.execute(
                "UPDATE lifi_transport_pools SET current_id=? WHERE scope=? AND current_id<>?",
                (self._ids[index], self.scope, self._ids[index]),
            )
            self._switches += result.rowcount

    def _health_wait(self, index):
        with self._db() as db:
            row = db.execute(
                "SELECT retry_at FROM lifi_transport_health WHERE transport_id=?",
                (self._ids[index],),
            ).fetchone()
        return row[0] if row and row[0] > self.clock() else None

    def _cooldown(self, index):
        retry = self.clock() + 60
        with self._db() as db:
            db.execute(
                "INSERT INTO lifi_transport_health VALUES (?,?) "
                "ON CONFLICT(transport_id) DO UPDATE SET retry_at=excluded.retry_at",
                (self._ids[index], retry),
            )
        return retry

    def available_at(self, bucket):
        """Earliest usable assignment; reservations still happen atomically before dispatch."""
        times = []
        for i, child in enumerate(self.clients):
            retry = child.quota.available_at(bucket, purpose=child.purpose)
            retry = max(retry, self._health_wait(i) or self.clock())
            times.append(retry)
        return min(times)

    def recheck_wait(self, diagnostic):
        provider = next(
            (
                item.get("provider", {})
                for item in diagnostic.get("exceptions", [])
                if item.get("provider", {}).get("pool_scope") == self.scope[:32]
            ),
            {},
        )
        diagnostic = provider or diagnostic
        bucket = diagnostic.get("bucket")
        if (
            diagnostic.get("pool_scope") != self.scope[:32]
            or bucket not in self.clients[0].quota.limits
        ):
            return None
        return self.available_at(bucket)

    def retry_at_for_pending(self, pending):
        """Re-evaluate a saved LI.FI quota wait against current assignments."""
        diagnostic = pending.get("diagnostic", {})
        for item in diagnostic.get("exceptions", []):
            provider = item.get("provider", {})
            bucket = provider.get("bucket")
            if bucket not in self.clients[0].quota.limits:
                bucket = {(75, 7200): "routes", (50, 7200): "steps", (100, 60): "public"}.get(
                    (provider.get("limit"), provider.get("window"))
                )
            if bucket is None:
                message = item.get("message", "")
                prefix = "lifi_request_budget_exceeded:"
                if message.startswith(prefix):
                    candidate = message[len(prefix) :]
                    if candidate in self.clients[0].quota.limits:
                        bucket = candidate
            if bucket and item.get("type") in {"LifiRequestLimit", "ProxyPoolExhausted"}:
                return self.available_at(bucket)
        return None

    def get(self, url, **kwargs):
        return self._request("get", url, kwargs)

    def post(self, url, **kwargs):
        return self._request("post", url, kwargs)

    def _request(self, method, url, kwargs):
        if urlsplit(url).netloc != "li.quest" or urlsplit(url).scheme != "https":
            return getattr(self.clients[0], method)(url, **kwargs)
        cached = self.clients[0].cached_response(method.upper(), url, kwargs)
        if cached is not None:
            return cached
        start = self._index()
        unavailable = []
        for offset in range(len(self.clients)):
            check_deadline()
            i = (start + offset) % len(self.clients)
            health = self._health_wait(i)
            if health:
                unavailable.append({"transport_index": i + 1, "reason": "transport_cooldown"})
                continue
            child = self.clients[i]
            ready_at = child.quota.available_at(request_bucket(url), purpose=child.purpose)
            if ready_at > self.clock():
                self._local_limited += 1
                unavailable.append({"transport_index": i + 1, "reason": "quota_wait"})
                continue
            if offset:
                self._failovers += 1
            try:
                response = getattr(child, method)(url, **dict(kwargs))
            except LifiRequestLimit as error:
                unavailable.append({"transport_index": i + 1, "reason": error.reason})
                continue
            except httpx.TransportError as error:
                self._cooldown(i)
                unavailable.append({"transport_index": i + 1, "reason": type(error).__name__})
                continue
            if response.status_code in {500, 502, 503, 504}:
                self._cooldown(i)
                unavailable.append({"transport_index": i + 1, "http_status": response.status_code})
                continue
            self._select(i)
            return response
        bucket = request_bucket(url)
        self._pool_waits += 1
        raise ProxyPoolExhausted(
            bucket,
            reason="proxy_pool_exhausted",
            retry_at=self.available_at(bucket),
            diagnostic={
                "reason": "proxy_pool_exhausted",
                "bucket": bucket,
                "pool_scope": self.scope[:32],
                "pool_size": len(self.clients),
                "unavailable": unavailable,
            },
        )

    @property
    def metrics(self):
        values = [c.metrics for c in self.clients]
        result = {
            k: sum(v.get(k, 0) for v in values)
            for k, value in values[0].items()
            if isinstance(value, (int, float))
        }
        result["limited"] += self._local_limited
        result["quota_waits"] += self._local_limited
        result["retries"] += self._failovers
        result["pool_waits"] = self._pool_waits
        result["http_attempts_by_bucket"] = {
            b: sum(v["http_attempts_by_bucket"].get(b, 0) for v in values)
            for b in values[0]["http_attempts_by_bucket"]
        }
        result["quota"] = {str(i + 1): v["quota"] for i, v in enumerate(values)}
        result.update(
            transport_index=self._index() + 1,
            pool_size=len(values),
            transport_switches=self._switches,
        )
        return result

    def record_search(self, key, data):
        self.clients[0].record_search(key, data)

    def record_retry(self):
        self.clients[self._index()].record_retry()

    def close(self):
        for child in self.clients:
            child.close()
