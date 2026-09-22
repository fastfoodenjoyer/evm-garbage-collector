"""Transactional SQLite persistence for the current wallet inventory."""

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
    """One current inventory, keyed by normalized wallet, chain, and asset."""

    SCHEMA_VERSION = 2

    def __init__(self, path: str | Path, readonly: bool = False):
        self.path = Path(path)
        self.readonly = readonly
        self._lock = None
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
            if version == 0 and not readonly and self._is_empty_schema():
                self._setup()
                version = self.SCHEMA_VERSION
            if version != self.SCHEMA_VERSION:
                raise ValueError(f"unsupported schema version: {version}")
            self._validate_schema()
        except Exception:
            self.close()
            raise

    def _is_empty_schema(self) -> bool:
        row = self.db.execute(
            "SELECT count(*) FROM sqlite_master "
            "WHERE type IN ('table', 'index', 'trigger', 'view') "
            "AND name NOT LIKE 'sqlite_%'"
        ).fetchone()
        return row[0] == 0

    def _setup(self) -> None:
        database_uuid = str(uuid.uuid4())
        statements = (
            """
            CREATE TABLE wallets (
                id INTEGER PRIMARY KEY,
                address TEXT NOT NULL UNIQUE
            )
            """,
            """
            CREATE TABLE assets (
                id INTEGER PRIMARY KEY,
                wallet_id INTEGER NOT NULL REFERENCES wallets(id),
                chain_id INTEGER NOT NULL,
                asset_id TEXT NOT NULL,
                kind TEXT NOT NULL,
                metadata TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'pending',
                attempts INTEGER NOT NULL DEFAULT 0,
                retry_after REAL,
                result TEXT,
                created_at REAL NOT NULL,
                modified_at REAL NOT NULL,
                UNIQUE(wallet_id, chain_id, asset_id)
            )
            """,
            """
            CREATE TABLE discovery_state (
                wallet_id INTEGER NOT NULL REFERENCES wallets(id),
                chain_id INTEGER NOT NULL,
                cursor TEXT,
                cursor_history TEXT NOT NULL,
                status TEXT NOT NULL,
                retry_after REAL,
                result TEXT,
                error TEXT,
                created_at REAL NOT NULL,
                modified_at REAL NOT NULL,
                PRIMARY KEY(wallet_id, chain_id)
            )
            """,
            """
            CREATE TABLE passes (
                wallet_id INTEGER NOT NULL REFERENCES wallets(id),
                chain_id INTEGER NOT NULL,
                block TEXT NOT NULL,
                created_at REAL NOT NULL,
                modified_at REAL NOT NULL,
                PRIMARY KEY(wallet_id, chain_id)
            )
            """,
            """
            CREATE TABLE metadata (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            )
            """
        )
        self.db.execute("BEGIN IMMEDIATE")
        try:
            for statement in statements:
                self.db.execute(statement)
            self.db.execute(
                "INSERT INTO metadata(key, value) VALUES ('database_uuid', ?)",
                (database_uuid,),
            )
            self.db.execute(f"PRAGMA user_version = {self.SCHEMA_VERSION}")
            self.db.commit()
        except Exception:
            self.db.rollback()
            raise

    def _validate_schema(self) -> None:
        expected_columns = {
            "wallets": {"id", "address"},
            "assets": {
                "id", "wallet_id", "chain_id", "asset_id", "kind", "metadata",
                "status", "attempts", "retry_after", "result", "created_at", "modified_at",
            },
            "discovery_state": {
                "wallet_id", "chain_id", "cursor", "cursor_history", "status", "retry_after",
                "result", "error", "created_at", "modified_at",
            },
            "passes": {"wallet_id", "chain_id", "block", "created_at", "modified_at"},
            "metadata": {"key", "value"},
        }
        tables = {
            row[0]
            for row in self.db.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )
        }
        missing = expected_columns.keys() - tables
        if missing:
            raise ValueError(f"schema v2 missing required tables: {', '.join(sorted(missing))}")
        for table, expected in expected_columns.items():
            columns = {row[1] for row in self.db.execute(f"PRAGMA table_info({table})")}
            if not expected <= columns:
                raise ValueError(f"schema v2 has incomplete table: {table}")
        required_keys = (
            ("wallets", ("address",), "wallet address constraint"),
            (
                "assets",
                ("wallet_id", "chain_id", "asset_id"),
                "assets identity constraint",
            ),
            (
                "discovery_state",
                ("wallet_id", "chain_id"),
                "discovery state identity constraint",
            ),
            ("passes", ("wallet_id", "chain_id"), "passes identity constraint"),
        )
        for table, columns, description in required_keys:
            if not self._has_unique_key(table, columns):
                raise ValueError(f"schema v2 missing {description}")
        rows = self.db.execute(
            "SELECT value FROM metadata WHERE key='database_uuid'"
        ).fetchall()
        if len(rows) != 1:
            raise ValueError("schema v2 has no usable database UUID")
        try:
            parsed = uuid.UUID(rows[0][0])
        except (AttributeError, TypeError, ValueError) as exc:
            raise ValueError("schema v2 has no usable database UUID") from exc
        if str(parsed) != rows[0][0]:
            raise ValueError("schema v2 has no usable database UUID")

    def _has_unique_key(self, table: str, columns: tuple[str, ...]) -> bool:
        for index in self.db.execute(f"PRAGMA index_list({table})"):
            if not index[2]:
                continue
            name = index[1]
            indexed_columns = tuple(
                row[2] for row in self.db.execute(f"PRAGMA index_info({name!r})")
            )
            if indexed_columns == columns:
                return True
        return False

    @staticmethod
    def _wallet(wallet: str) -> str:
        return wallet.lower()

    @staticmethod
    def _asset(asset_id: str) -> str:
        return "native" if asset_id.lower() == "native" else asset_id.lower()

    @staticmethod
    def _json(value: Any) -> str:
        return json.dumps(value, sort_keys=True, separators=(",", ":"))

    def _wallet_id(self, wallet: str) -> int:
        address = self._wallet(wallet)
        self.db.execute("INSERT OR IGNORE INTO wallets(address) VALUES (?)", (address,))
        row = self.db.execute(
            "SELECT id FROM wallets WHERE address=?", (address,)
        ).fetchone()
        return int(row[0])

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

    def database_uuid(self) -> str:
        row = self.db.execute("SELECT value FROM metadata WHERE key='database_uuid'").fetchone()
        if row is None:  # pragma: no cover - schema invariant
            raise ValueError("database UUID is missing")
        return str(row[0])

    def upsert_asset(
        self,
        wallet: str,
        chain_id: int,
        asset_id: str,
        kind: str = "mandatory",
        metadata: dict[str, Any] | None = None,
    ) -> int:
        self._write_guard()
        now = time.time()
        with self.db:
            wallet_id = self._wallet_id(wallet)
            self.db.execute(
                """
                INSERT INTO assets(
                    wallet_id, chain_id, asset_id, kind, metadata, created_at, modified_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(wallet_id, chain_id, asset_id) DO UPDATE SET
                    kind=excluded.kind, metadata=excluded.metadata, modified_at=excluded.modified_at
                """,
                (
                    wallet_id,
                    int(chain_id),
                    self._asset(asset_id),
                    kind,
                    self._json(metadata or {}),
                    now,
                    now,
                ),
            )
            row = self.db.execute(
                "SELECT id FROM assets WHERE wallet_id=? AND chain_id=? AND asset_id=?",
                (wallet_id, int(chain_id), self._asset(asset_id)),
            ).fetchone()
        return int(row[0])

    def assets(
        self, wallet: str | None = None, chain_id: int | None = None
    ) -> list[dict[str, Any]]:
        sql = (
            "SELECT assets.*, wallets.address AS wallet FROM assets "
            "JOIN wallets ON wallets.id=assets.wallet_id"
        )
        conditions: list[str] = []
        args: list[Any] = []
        if wallet is not None:
            conditions.append("wallets.address=?")
            args.append(self._wallet(wallet))
        if chain_id is not None:
            conditions.append("assets.chain_id=?")
            args.append(int(chain_id))
        if conditions:
            sql += " WHERE " + " AND ".join(conditions)
        rows = self.db.execute(
            sql + " ORDER BY wallets.address, assets.chain_id, assets.asset_id", args
        ).fetchall()
        return [self._asset_row(row) for row in rows]

    @staticmethod
    def _asset_row(row: sqlite3.Row) -> dict[str, Any]:
        return {
            "id": int(row["id"]),
            "wallet": row["wallet"],
            "chain_id": int(row["chain_id"]),
            "asset_id": row["asset_id"],
            "kind": row["kind"],
            "metadata": json.loads(row["metadata"]),
            "status": row["status"],
            "attempts": int(row["attempts"]),
            "retry_after": float(row["retry_after"]) if row["retry_after"] is not None else None,
            "result": json.loads(row["result"]) if row["result"] is not None else None,
            "created_at": float(row["created_at"]),
            "modified_at": float(row["modified_at"]),
        }

    def record_asset(
        self,
        asset_id: int,
        result: dict[str, Any],
        status: str = "success",
        retry_after: Any = None,
    ) -> None:
        self._write_guard()
        retry = float(retry_after) if retry_after is not None else None
        with self.db:
            cur = self.db.execute(
                """
                UPDATE assets
                SET result=?, status=?, attempts=attempts+1, retry_after=?, modified_at=?
                WHERE id=?
                """,
                (self._json(result), status, retry, time.time(), int(asset_id)),
            )
            if cur.rowcount == 0:
                raise ValueError(f"unknown asset: {asset_id}")

    def discovery_state(self, wallet: str, chain_id: int) -> dict[str, Any] | None:
        row = self.db.execute(
            """
            SELECT discovery_state.*, wallets.address AS wallet FROM discovery_state
            JOIN wallets ON wallets.id=discovery_state.wallet_id
            WHERE wallets.address=? AND discovery_state.chain_id=?
            """,
            (self._wallet(wallet), int(chain_id)),
        ).fetchone()
        return self._discovery_row(row) if row else None

    def discovery_states(self) -> list[dict[str, Any]]:
        rows = self.db.execute(
            """
            SELECT discovery_state.*, wallets.address AS wallet FROM discovery_state
            JOIN wallets ON wallets.id=discovery_state.wallet_id
            ORDER BY wallets.address, discovery_state.chain_id
            """
        ).fetchall()
        return [self._discovery_row(row) for row in rows]

    @staticmethod
    def _discovery_row(row: sqlite3.Row) -> dict[str, Any]:
        return {
            "wallet": row["wallet"], "chain_id": int(row["chain_id"]), "cursor": row["cursor"],
            "cursor_history": json.loads(row["cursor_history"]), "status": row["status"],
            "retry_after": float(row["retry_after"]) if row["retry_after"] is not None else None,
            "result": json.loads(row["result"]) if row["result"] is not None else None,
            "error": json.loads(row["error"]) if row["error"] is not None else None,
            "created_at": float(row["created_at"]), "modified_at": float(row["modified_at"]),
        }

    def write_discovery_state(
        self, wallet: str, chain_id: int, *, cursor: str | None = None,
        cursor_history: list[str | None] | None = None, status: str = "pending",
        retry_after: Any = None, result: dict[str, Any] | None = None,
        error: dict[str, Any] | None = None,
    ) -> None:
        self._write_guard()
        now = time.time()
        retry = float(retry_after) if retry_after is not None else None
        with self.db:
            wallet_id = self._wallet_id(wallet)
            self.db.execute(
                """
                INSERT INTO discovery_state(
                    wallet_id, chain_id, cursor, cursor_history, status, retry_after, result, error,
                    created_at, modified_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(wallet_id, chain_id) DO UPDATE SET
                    cursor=excluded.cursor,
                    cursor_history=excluded.cursor_history,
                    status=excluded.status,
                    retry_after=excluded.retry_after,
                    result=excluded.result,
                    error=excluded.error,
                    modified_at=excluded.modified_at
                """,
                (wallet_id, int(chain_id), cursor, self._json(cursor_history or []), status, retry,
                 self._json(result) if result is not None else None,
                 self._json(error) if error is not None else None, now, now),
            )

    def get_pass(self, wallet: str, chain_id: int) -> dict[str, Any] | None:
        row = self.db.execute(
            """
            SELECT block FROM passes JOIN wallets ON wallets.id=passes.wallet_id
            WHERE wallets.address=? AND passes.chain_id=?
            """,
            (self._wallet(wallet), int(chain_id)),
        ).fetchone()
        return json.loads(row["block"]) if row else None

    def save_pass(self, wallet: str, chain_id: int, block: dict[str, Any]) -> None:
        self._write_guard()
        now = time.time()
        with self.db:
            wallet_id = self._wallet_id(wallet)
            self.db.execute(
                """
                INSERT INTO passes(wallet_id, chain_id, block, created_at, modified_at)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(wallet_id, chain_id) DO UPDATE SET
                    block=excluded.block, modified_at=excluded.modified_at
                """,
                (wallet_id, int(chain_id), self._json(block), now, now),
            )

    def passes(self) -> list[dict[str, Any]]:
        rows = self.db.execute(
            """
            SELECT passes.*, wallets.address AS wallet FROM passes
            JOIN wallets ON wallets.id=passes.wallet_id
            ORDER BY wallets.address, passes.chain_id
            """
        ).fetchall()
        return [
            {
                "wallet": row["wallet"], "chain_id": int(row["chain_id"]),
                "block": json.loads(row["block"]), "created_at": float(row["created_at"]),
                "modified_at": float(row["modified_at"]),
            }
            for row in rows
        ]
