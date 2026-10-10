"""Durable quote responses and expiring claims in the operational SQLite database."""

from __future__ import annotations

import hashlib
import json
import sqlite3
import threading
import time
import uuid
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .diagnostics import exception_diagnostic


def _json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)


def quote_job_key(scope: str, request: dict, policy_digest: str) -> str:
    """Hash the entire request and policy, including endpoint and token identities."""
    return hashlib.sha256(_json([scope, request, policy_digest]).encode()).hexdigest()


@dataclass(frozen=True)
class Claim:
    job_key: str
    attempt_id: str
    lease_owner: str


class JobInProgress(ValueError):
    """A persisted job cannot currently be dispatched by this invocation."""

    def __init__(self, *, reason="job_in_progress", retry_at=None, diagnostic=None):
        self.reason = reason
        self.retry_at = retry_at
        self.diagnostic = diagnostic or {}
        super().__init__(reason)


class QuoteJobs:
    lease_seconds = 60
    states = frozenset(
        {"pending", "inflight", "complete", "no_route", "quota_wait", "error", "expired"}
    )

    def __init__(self, db_path: Path, scope: str, *, clock=None):
        self.db_path = db_path
        self.scope = scope
        self.clock = clock or time.time
        self.lease_owner = uuid.uuid4().hex
        self._attempted: set[str] = set()
        self._failures: dict[str, tuple[dict, float | None]] = {}
        with self.db() as db:
            db.execute("""CREATE TABLE IF NOT EXISTS quote_jobs (
                scope TEXT NOT NULL, job_key TEXT NOT NULL, search_key TEXT NOT NULL,
                state TEXT NOT NULL, observed_at REAL, expires_at REAL, retry_at REAL,
                request_json TEXT, result_json TEXT, diagnostic_json TEXT,
                updated_at REAL NOT NULL, lease_owner TEXT, attempt_id TEXT,
                lease_until REAL, PRIMARY KEY(scope, job_key))""")

    @contextmanager
    def db(self):
        db = sqlite3.connect(self.db_path, timeout=30)
        db.row_factory = sqlite3.Row
        try:
            with db:
                yield db
        finally:
            db.close()

    @staticmethod
    def _decode(row):
        result = dict(row)
        for column in ("request_json", "result_json", "diagnostic_json"):
            result[column] = json.loads(result[column]) if result[column] else None
        return result

    def read(self, job_key: str) -> dict | None:
        """Read reusable results or persisted failures without extending freshness."""
        now = self.clock()
        with self.db() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute(
                "SELECT * FROM quote_jobs WHERE scope=? AND job_key=?", (self.scope, job_key)
            ).fetchone()
            if row is None:
                return None
            if row["state"] in ("complete", "no_route"):
                if row["expires_at"] is None or row["expires_at"] <= now:
                    db.execute(
                        "UPDATE quote_jobs SET state='expired', updated_at=? "
                        "WHERE scope=? AND job_key=?",
                        (now, self.scope, job_key),
                    )
                    return None
                return self._decode(row)
            if row["state"] in ("error", "quota_wait"):
                return self._decode(row)
            return None

    def claim(self, job_key: str, request: dict, policy_digest: str, search_key: str = "") -> Claim:
        """Claim atomically, preserving failures that must not be repeatedly sent."""
        now = self.clock()
        with self.db() as db:
            db.execute("BEGIN IMMEDIATE")
            if job_key in self._failures:
                diagnostic, retry_at = self._failures[job_key]
                raise JobInProgress(
                    reason=diagnostic.get("reason", "error"),
                    retry_at=retry_at,
                    diagnostic=diagnostic,
                )
            row = db.execute(
                "SELECT * FROM quote_jobs WHERE scope=? AND job_key=?", (self.scope, job_key)
            ).fetchone()
            if row:
                diagnostic = json.loads(row["diagnostic_json"] or "{}")
                state = row["state"]
                if state == "inflight" and row["lease_until"] > now:
                    raise JobInProgress(retry_at=row["lease_until"])
                if state in ("complete", "no_route") and row["expires_at"] > now:
                    raise JobInProgress(reason=state, retry_at=row["expires_at"])
                if state == "error":
                    if diagnostic.get("reason") == "bad_request":
                        raise JobInProgress(reason="bad_request", diagnostic=diagnostic)
                    if job_key in self._attempted or row["lease_owner"] == self.lease_owner:
                        raise JobInProgress(
                            reason="error", retry_at=row["retry_at"], diagnostic=diagnostic
                        )
                if state in ("quota_wait", "error") and row["retry_at"] is not None:
                    if row["retry_at"] > now:
                        raise JobInProgress(
                            reason=state, retry_at=row["retry_at"], diagnostic=diagnostic
                        )
            token = Claim(job_key, uuid.uuid4().hex, self.lease_owner)
            db.execute(
                """INSERT INTO quote_jobs
                (scope,job_key,search_key,state,request_json,updated_at,lease_owner,
                 attempt_id,lease_until) VALUES (?,?,?,'inflight',?,?,?,?,?)
                ON CONFLICT(scope,job_key) DO UPDATE SET search_key=excluded.search_key,
                state='inflight',request_json=excluded.request_json,
                updated_at=excluded.updated_at,lease_owner=excluded.lease_owner,
                attempt_id=excluded.attempt_id,lease_until=excluded.lease_until,
                observed_at=NULL,expires_at=NULL,retry_at=NULL,result_json=NULL,
                diagnostic_json=NULL""",
                (
                    self.scope,
                    job_key,
                    search_key,
                    _json(request),
                    now,
                    token.lease_owner,
                    token.attempt_id,
                    now + self.lease_seconds,
                ),
            )
            self._attempted.add(job_key)
            return token

    def _cas(self, token: Claim, fields: dict) -> bool:
        if token.lease_owner != self.lease_owner:
            return False
        now = self.clock()
        assignments = ",".join(f"{key}=?" for key in fields)
        with self.db() as db:
            cursor = db.execute(
                f"UPDATE quote_jobs SET {assignments} WHERE scope=? "
                "AND job_key=? AND state='inflight' AND lease_owner=? "
                "AND attempt_id=? AND lease_until>?",
                (
                    *fields.values(),
                    self.scope,
                    token.job_key,
                    token.lease_owner,
                    token.attempt_id,
                    now,
                ),
            )
            return cursor.rowcount == 1

    def renew(self, token: Claim) -> bool:
        now = self.clock()
        return self._cas(token, {"lease_until": now + self.lease_seconds, "updated_at": now})

    @contextmanager
    def heartbeat(self, token: Claim, *, interval: float = 10):
        """Keep a claim alive through pacing and retries; join the worker on exit."""
        if not 0 < interval <= 10:
            raise ValueError("heartbeat interval must be between 0 and 10 seconds")
        stopped = threading.Event()

        def keep_alive():
            while not stopped.wait(interval):
                if not self.renew(token):
                    break

        worker = threading.Thread(
            target=keep_alive, name=f"quote-lease-{token.attempt_id}", daemon=True
        )
        worker.start()
        try:
            yield token
        finally:
            stopped.set()
            worker.join()

    def finish(
        self, token: Claim, response_json: dict, observed_at: float, ttl: float = 240
    ) -> bool:
        state = "no_route" if response_json.get("routes") == [] else "complete"
        return self._cas(
            token,
            {
                "state": state,
                "result_json": _json(response_json),
                "observed_at": observed_at,
                "expires_at": observed_at + ttl,
                "retry_at": None,
                "diagnostic_json": None,
                "lease_until": None,
                "updated_at": self.clock(),
            },
        )

    def fail(
        self,
        token: Claim,
        error: BaseException,
        state: str = "error",
        retry_at: float | None = None,
    ) -> bool:
        if state not in ("error", "quota_wait"):
            raise ValueError("failure state must be error or quota_wait")
        diagnostic = exception_diagnostic(error)
        if any(
            400 <= item.get("http_status", 0) < 500 and item["http_status"] != 429
            for item in diagnostic["exceptions"]
        ):
            diagnostic["reason"] = "bad_request"
        saved = self._cas(
            token,
            {
                "state": state,
                "diagnostic_json": _json(diagnostic),
                "retry_at": retry_at,
                "lease_until": None,
                "updated_at": self.clock(),
            },
        )
        if state == "error" and token.lease_owner == self.lease_owner:
            self._failures[token.job_key] = (diagnostic, retry_at)
        return saved
