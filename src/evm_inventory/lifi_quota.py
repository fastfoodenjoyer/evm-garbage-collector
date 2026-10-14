"""Atomic quota and dispatch accounting in the shared operational database."""

from __future__ import annotations

import json
import sqlite3
import time
from contextlib import contextmanager
from email.utils import parsedate_to_datetime
from pathlib import Path

from .diagnostics import redact_data, redact_text


class DispatchWait(Exception):
    """A short dispatch delay; no quota reservation has been consumed."""

    def __init__(self, retry_at):
        self.retry_at = retry_at
        super().__init__("LI.FI dispatch pacing")


class LifiRequestLimit(ValueError):
    def __init__(self, bucket, *, reason="quota_wait", retry_at=None, diagnostic=None):
        self.bucket = bucket
        self.reason = reason
        self.retry_at = retry_at
        self.diagnostic = redact_data(diagnostic or {})
        super().__init__("lifi_request_budget_exceeded:" + bucket)


class LifiQuotaStore:
    limits = {"routes": (75, 7200, 5), "steps": (50, 7200, 3), "public": (100, 60, 0)}
    provider_limit_matches_window = True

    def __init__(self, db_path: Path, scope: str, *, clock=None):
        self.db_path, self.scope = db_path, scope
        self.clock = clock or (lambda: time.time())
        with self.db() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS lifi_requests(scope TEXT,bucket TEXT,sent_at REAL);
                CREATE INDEX IF NOT EXISTS lifi_requests_window ON lifi_requests(scope,
                bucket,sent_at);
                CREATE TABLE IF NOT EXISTS lifi_limits(scope TEXT,bucket TEXT,
                blocked_until REAL,PRIMARY KEY(scope,bucket));
                CREATE TABLE IF NOT EXISTS lifi_quota_state(scope TEXT,bucket TEXT,
                remaining INTEGER,reset_at REAL,PRIMARY KEY(scope,bucket));
                CREATE TABLE IF NOT EXISTS lifi_dispatch(scope TEXT PRIMARY KEY,next_at REAL);
                CREATE TABLE IF NOT EXISTS lifi_starts(scope TEXT PRIMARY KEY,last_at REAL);
                CREATE TABLE IF NOT EXISTS lifi_actual_starts(scope TEXT PRIMARY KEY,last_at REAL);
                CREATE TABLE IF NOT EXISTS lifi_attempts(id INTEGER PRIMARY KEY
                 AUTOINCREMENT,scope TEXT,bucket TEXT,request_key TEXT,
                sent_at REAL,started_at REAL,observed_at REAL);
            """)
            for table, additions in {
                "lifi_limits": {"diagnostic": "TEXT"},
                "lifi_quota_state": {
                    "provider_limit": "INTEGER",
                    "observed_at": "REAL",
                    "attempt_id": "INTEGER",
                },
            }.items():
                columns = {row[1] for row in db.execute("PRAGMA table_info(" + table + ")")}
                for column, kind in additions.items():
                    if column not in columns:
                        db.execute(f"ALTER TABLE {table} ADD COLUMN {column} {kind}")

    @contextmanager
    def db(self):
        db = sqlite3.connect(self.db_path, timeout=30)
        try:
            with db:
                yield db
        finally:
            db.close()

    def reserve_dispatch_time(self):
        with self.db() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute(
                "SELECT next_at FROM lifi_dispatch WHERE scope=?", (self.scope,)
            ).fetchone()
            dispatch = max(self.clock(), row[0] if row else 0)
            db.execute(
                "INSERT OR REPLACE INTO lifi_dispatch VALUES (?,?)", (self.scope, dispatch + 1)
            )
            return dispatch

    def _state(self, db, bucket, now):
        limit, window, reserve = self.limits[bucket]
        row = db.execute(
            "SELECT remaining,reset_at,provider_limit FROM lifi_quota_state WHERE scope=? "
            "AND bucket=?",
            (self.scope, bucket),
        ).fetchone()
        fresh = row and row[1] > now
        if fresh and row[2] is not None and self.provider_limit_matches_window:
            limit = min(limit, row[2])
        times = [
            r[0]
            for r in db.execute(
                "SELECT sent_at FROM lifi_requests WHERE scope=? AND bucket=? AND "
                "sent_at>? ORDER BY sent_at",
                (self.scope, bucket, now - window),
            )
        ]
        blocked = db.execute(
            "SELECT blocked_until,diagnostic FROM lifi_limits WHERE scope=? AND bucket=?",
            (self.scope, bucket),
        ).fetchone()
        return (
            {
                "limit": limit,
                "window": window,
                "reserve": reserve,
                "local_used": len(times),
                "remaining": row[0] if fresh else None,
                "reset_at": row[1] if fresh else None,
                "cooldown": blocked[0] if blocked and blocked[0] > now else None,
            },
            times,
            blocked,
        )

    def available_at(self, bucket, *, purpose="planning"):
        """Read capacity without reserving; dispatch must still reserve atomically."""
        now = self.clock()
        with self.db() as db:
            state, times, _ = self._state(db, bucket, now)
        reserve = state["reserve"] if purpose == "planning" else 0
        capacity = state["limit"] - reserve
        retry = [now, state["cooldown"] or now]
        if capacity <= 0:
            retry.append(state["reset_at"] or now + state["window"])
        elif len(times) >= capacity:
            retry.append(times[min(len(times) - 1, len(times) - capacity)] + state["window"])
        if state["remaining"] is not None and state["remaining"] <= reserve:
            retry.append(state["reset_at"] or now + state["window"])
        return max(retry)

    def reserve_request(self, bucket, request_key, *, purpose="planning", paced=False):
        if purpose not in ("planning", "execution"):
            raise ValueError("invalid LI.FI request purpose")
        now = self.clock()
        with self.db() as db:
            db.execute("BEGIN IMMEDIATE")
            state, times, blocked = self._state(db, bucket, now)
            if state["cooldown"]:
                raise LifiRequestLimit(
                    bucket,
                    reason="provider_cooldown",
                    retry_at=state["cooldown"],
                    diagnostic=json.loads(blocked[1] or "{}"),
                )
            reserve = state["reserve"] if purpose == "planning" else 0
            retry = []
            capacity = state["limit"] - reserve
            if capacity <= 0:
                retry.append(state["reset_at"])
            elif len(times) >= capacity:
                # Expire enough oldest reservations to leave one admissible slot.
                retry.append(
                    times[min(len(times) - 1, max(0, len(times) - capacity))] + state["window"]
                )
            if state["remaining"] is not None and state["remaining"] <= reserve:
                retry.append(state["reset_at"])
            if retry:
                raise LifiRequestLimit(bucket, retry_at=max(retry), diagnostic=state)
            if paced:
                last = db.execute(
                    "SELECT last_at FROM lifi_starts WHERE scope=?", (self.scope,)
                ).fetchone()
                if last and last[0] + 1 > now:
                    raise DispatchWait(last[0] + 1)
                db.execute("INSERT OR REPLACE INTO lifi_starts VALUES (?,?)", (self.scope, now))
            db.execute(
                "INSERT INTO lifi_requests(scope,bucket,sent_at) VALUES (?,?,?)",
                (self.scope, bucket, now),
            )
            attempt = db.execute(
                "INSERT INTO lifi_attempts(scope,bucket,request_key,sent_at) VALUES (?,?,?,?)",
                (self.scope, bucket, request_key, now),
            ).lastrowid
            db.execute(
                "UPDATE lifi_quota_state SET remaining=remaining-1 WHERE scope=? AND "
                "bucket=? AND reset_at>?",
                (self.scope, bucket, now),
            )
            return attempt

    def mark_started(self, attempt, *, paced=False):
        with self.db() as db:
            db.execute("BEGIN IMMEDIATE")
            now = self.clock()
            if paced:
                attempt_row = db.execute(
                    "SELECT bucket FROM lifi_attempts WHERE id=? AND scope=?",
                    (attempt, self.scope),
                ).fetchone()
                if attempt_row is None:
                    raise ValueError("unknown LI.FI reservation")
                state, _times, blocked = self._state(db, attempt_row[0], now)
                if state["cooldown"]:
                    raise LifiRequestLimit(
                        attempt_row[0],
                        reason="provider_cooldown",
                        retry_at=state["cooldown"],
                        diagnostic=json.loads(blocked[1] or "{}"),
                    )
                row = db.execute(
                    "SELECT last_at FROM lifi_actual_starts WHERE scope=?", (self.scope,)
                ).fetchone()
                if row and row[0] + 1 > now:
                    raise DispatchWait(row[0] + 1)
                db.execute(
                    "INSERT OR REPLACE INTO lifi_actual_starts VALUES (?,?)",
                    (self.scope, now),
                )
            db.execute(
                "UPDATE lifi_attempts SET started_at=? WHERE id=? AND scope=?",
                (now, attempt, self.scope),
            )

    def observe(self, bucket, attempt, headers, status, body=""):
        now = self.clock()
        headers = {k.lower(): v for k, v in headers.items()}

        def number(name):
            try:
                value = float(headers.get(name, headers.get("x-" + name, "")))
                return value if value >= 0 and value < float("inf") else None
            except (ValueError, TypeError):
                return None

        reset = number("ratelimit-reset")
        reset_at = (reset if reset >= 1_000_000_000 else now + reset) if reset is not None else None
        remaining, limit = number("ratelimit-remaining"), number("ratelimit-limit")
        diagnostic = {}
        if status == 429:
            try:
                parsed_body = json.loads(body)
            except (ValueError, TypeError):
                parsed_body = redact_text(str(body))
            diagnostic = redact_data({"status": status, "body": parsed_body, "headers": headers})
        with self.db() as db:
            db.execute("BEGIN IMMEDIATE")
            db.execute(
                "UPDATE lifi_attempts SET observed_at=? WHERE id=? AND scope=?",
                (now, attempt, self.scope),
            )
            if (
                (remaining is not None or limit is not None)
                and reset_at is not None
                and reset_at > now
            ):
                later = db.execute(
                    "SELECT COUNT(*) FROM lifi_attempts WHERE scope=? AND bucket=? AND id>?",
                    (self.scope, bucket, attempt),
                ).fetchone()[0]
                adjusted = (max(0, int(remaining) - later) if remaining is not None else
                            max(0, int(limit) - 1 - later) if limit is not None else None)
                prior = db.execute(
                    "SELECT remaining,reset_at,provider_limit FROM lifi_quota_state WHERE "
                    "scope=? AND bucket=?",
                    (self.scope, bucket),
                ).fetchone()
                if prior and prior[1] > now:
                    if prior[0] is not None:
                        adjusted = prior[0] if adjusted is None else min(adjusted, prior[0])
                    reset_at = max(reset_at, prior[1])
                    if prior[2] is not None:
                        limit = prior[2] if limit is None else min(limit, prior[2])
                db.execute(
                    "INSERT OR REPLACE INTO "
                    "lifi_quota_state(scope,bucket,remaining,reset_at,"
                    "provider_limit,observed_at,attempt_id) "
                    "VALUES (?,?,?,?,?,?,?)",
                    (
                        self.scope,
                        bucket,
                        adjusted,
                        reset_at,
                        int(limit) if limit is not None and limit >= 1 else None,
                        now,
                        attempt,
                    ),
                )
            if status == 429:
                retry_at = reset_at or now + self.limits[bucket][1]
                raw = headers.get("retry-after")
                if raw:
                    try:
                        retry_at = now + max(1, float(raw))
                    except ValueError:
                        try:
                            retry_at = max(now + 1, parsedate_to_datetime(raw).timestamp())
                        except (ValueError, TypeError, OverflowError):
                            pass
                db.execute(
                    "INSERT INTO lifi_limits(scope,bucket,blocked_until,diagnostic) "
                    "VALUES (?,?,?,?) ON CONFLICT(scope,bucket) DO UPDATE SET "
                    "blocked_until=MAX(blocked_until,excluded.blocked_until),diagnostic=excluded.diagnostic",
                    (self.scope, bucket, retry_at, json.dumps(diagnostic)),
                )
                return LifiRequestLimit(
                    bucket, reason="provider_cooldown", retry_at=retry_at, diagnostic=diagnostic
                )
        return None

    def snapshot(self):
        with self.db() as db:
            return {bucket: self._state(db, bucket, self.clock())[0] for bucket in self.limits}


class AuthenticatedLifiQuotaStore(LifiQuotaStore):
    # Default key allocation: 100 RPM, enforced over a 2h rolling window for quotes.
    # Provider headers/cooldowns may only lower this ceiling; actual starts remain paced.
    limits = {"routes": (12_000, 7200, 5), "steps": (12_000, 7200, 3),
              "public": (100, 60, 0)}
    # Header remaining/reset controls its own window; never mix a minute capacity
    # with the independent two-hour local ledger.
    provider_limit_matches_window = False
