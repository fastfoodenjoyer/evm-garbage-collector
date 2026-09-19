"""Small transactional SQLite persistence layer for inventory runs."""

from __future__ import annotations

import json
import sqlite3
import time
import uuid
from pathlib import Path
from typing import Any

try:
    import fcntl
except ImportError:  # pragma: no cover
    fcntl = None


class Store:
    SCHEMA_VERSION = 1

    def __init__(self, path: str | Path, readonly: bool = False):
        self.path = Path(path)
        self.readonly = readonly
        self._lock = None
        self._pass_conflicts = set()
        self.db = None
        try:
            if readonly:
                self.db = sqlite3.connect(f"file:{self.path}?mode=ro", uri=True)
            else:
                self.path.parent.mkdir(parents=True, exist_ok=True)
                self._lock = open(str(self.path) + ".lock", "a+")
                if fcntl is not None:
                    try:
                        fcntl.flock(self._lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                    except BlockingIOError as exc:
                        raise sqlite3.OperationalError(
                            "database is locked by another writer"
                        ) from exc
                self.db = sqlite3.connect(self.path)
                self.db.execute("PRAGMA foreign_keys=ON")
            self.db.row_factory = sqlite3.Row
            version = self.db.execute("PRAGMA user_version").fetchone()[0]
            if version == 0 and not readonly:
                self._setup()
                version = self.SCHEMA_VERSION
            if version != self.SCHEMA_VERSION:
                raise ValueError(f"unsupported schema version: {version}")
        except Exception:
            self.close()
            raise

    def _setup(self) -> None:
        self.db.executescript(
            """
            CREATE TABLE IF NOT EXISTS runs (
                run_id TEXT PRIMARY KEY, snapshot TEXT NOT NULL, status TEXT NOT NULL,
                created_at REAL NOT NULL
            );
            CREATE TABLE IF NOT EXISTS jobs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                run_id TEXT NOT NULL REFERENCES runs(run_id), wallet TEXT NOT NULL,
                chain_id INTEGER NOT NULL, asset_id TEXT NOT NULL, kind TEXT NOT NULL,
                metadata TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'pending',
                attempts INTEGER NOT NULL DEFAULT 0, retry_after REAL,
                result TEXT,
                UNIQUE(run_id, wallet, chain_id, asset_id)
            );
            CREATE TABLE IF NOT EXISTS passes (
                run_id TEXT NOT NULL REFERENCES runs(run_id), wallet TEXT NOT NULL,
                chain_id INTEGER NOT NULL, block TEXT NOT NULL,
                PRIMARY KEY(run_id, wallet, chain_id)
            );
            PRAGMA user_version = 1;
            """
        )
        self.db.commit()

    def _write_guard(self) -> None:
        if self.readonly:
            raise sqlite3.OperationalError("attempt to write a readonly database")

    def close(self) -> None:
        if getattr(self, "db", None) is not None:
            self.db.close()
            self.db = None
        if self._lock is not None:
            if fcntl is not None:
                fcntl.flock(self._lock.fileno(), fcntl.LOCK_UN)
            self._lock.close()
            self._lock = None

    def __enter__(self) -> Store:
        return self

    def __exit__(self, *_: Any) -> None:
        self.close()

    def create_run(self, snapshot: dict[str, Any]) -> str:
        self._write_guard()
        run_id = uuid.uuid4().hex[:12]
        self.db.execute("INSERT INTO runs(run_id,snapshot,status,created_at) VALUES (?, ?, 'pending', ?)",
                        (run_id, json.dumps(snapshot), time.time()))
        self.db.commit()
        return run_id

    def run(self, run_id: str) -> dict[str, Any]:
        row = self.db.execute("SELECT * FROM runs WHERE run_id=?", (run_id,)).fetchone()
        if row is None:
            raise ValueError(f"unknown run: {run_id}")
        return {"run_id": row["run_id"], "snapshot": json.loads(row["snapshot"]),
                "status": row["status"]}

    def set_status(self, run_id: str, status: str) -> None:
        self._write_guard()
        cur = self.db.execute("UPDATE runs SET status=? WHERE run_id=?", (status, run_id))
        if cur.rowcount == 0:
            raise ValueError(f"unknown run: {run_id}")
        self.db.commit()

    def ensure_job(self, run_id: str, wallet: str, chain_id: int, asset_id: str,
                   kind: str = "mandatory", metadata: dict[str, Any] | None = None) -> int:
        self._write_guard()
        if not self.db.execute("SELECT 1 FROM runs WHERE run_id=?", (run_id,)).fetchone():
            raise ValueError(f"unknown run: {run_id}")
        meta = metadata or {}
        self.db.execute(
            "INSERT OR IGNORE INTO jobs(run_id,wallet,chain_id,asset_id,kind,metadata) "
            "VALUES(?,?,?,?,?,?)",
            (run_id, wallet, chain_id, asset_id, kind, json.dumps(meta)),
        )
        row = self.db.execute(
            "SELECT id FROM jobs WHERE run_id=? AND wallet=? AND chain_id=? AND asset_id=?",
            (run_id, wallet, chain_id, asset_id),
        ).fetchone()
        self.db.commit()
        return int(row[0])

    def jobs(self, run_id: str, wallet: str | None = None,
             chain_id: int | None = None) -> list[dict[str, Any]]:
        sql, args = "SELECT * FROM jobs WHERE run_id=?", [run_id]
        if wallet is not None:
            sql += " AND wallet=?"
            args.append(wallet)
        if chain_id is not None:
            sql += " AND chain_id=?"
            args.append(chain_id)
        rows = self.db.execute(sql + " ORDER BY id", args).fetchall()
        return [{**dict(row), "retry_after": (float(row["retry_after"])
                 if row["retry_after"] is not None else None),
                 "metadata": json.loads(row["metadata"]),
                 "result": json.loads(row["result"]) if row["result"] is not None else None}
                for row in rows]

    def record(self, job_id: int, result: dict[str, Any], status: str = "success",
               retry_after: Any = None) -> None:
        self._write_guard()
        if retry_after is not None:
            retry_after = float(retry_after)
        cur = self.db.execute(
            "UPDATE jobs SET result=?, status=?, attempts=attempts+1, retry_after=? WHERE id=?",
            (json.dumps(result), status, retry_after, job_id),
        )
        if cur.rowcount == 0:
            raise ValueError(f"unknown job: {job_id}")
        self.db.commit()

    def get_pass(self, run_id: str, wallet: str, chain_id: int) -> dict[str, Any] | None:
        row = self.db.execute("SELECT block FROM passes WHERE run_id=? AND wallet=? AND chain_id=?",
                              (run_id, wallet, chain_id)).fetchone()
        return json.loads(row[0]) if row else None

    def save_pass(self, run_id: str, wallet: str, chain_id: int, block: dict[str, Any]) -> None:
        self._write_guard()
        row = self.db.execute(
            "SELECT block FROM passes WHERE run_id=? AND wallet=? AND chain_id=?",
            (run_id, wallet, chain_id),
        ).fetchone()
        if row is not None:
            key = (run_id, wallet, chain_id)
            if json.loads(row[0]) != block:
                if key in self._pass_conflicts:
                    raise ValueError("network pass is immutable")
                self._pass_conflicts.add(key)
            return
        self.db.execute("INSERT INTO passes VALUES(?,?,?,?)",
                        (run_id, wallet, chain_id, json.dumps(block)))
        self.db.commit()

    def save_discovery_page(self, job_id: int, contracts: list[dict[str, Any]],
                            next_cursor: str | None) -> None:
        self._write_guard()
        with self.db:
            parent = self.db.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
            if parent is None:
                raise ValueError(f"unknown job: {job_id}")
            for contract in contracts:
                address = contract["address"]
                self.db.execute(
                    "INSERT OR IGNORE INTO jobs(run_id,wallet,chain_id,asset_id,kind,metadata) "
                    "VALUES(?,?,?,?,?,?)",
                    (parent["run_id"], parent["wallet"], parent["chain_id"], address,
                     "discovered", json.dumps(contract)),
                )
            result = json.loads(parent["result"]) if parent["result"] else {}
            result["cursor"] = next_cursor
            history = result.setdefault("cursor_history", [])
            if next_cursor not in history:
                history.append(next_cursor)
            self.db.execute("UPDATE jobs SET result=?, status=? WHERE id=?",
                            (json.dumps(result), "success" if next_cursor is None else "pending", job_id))
