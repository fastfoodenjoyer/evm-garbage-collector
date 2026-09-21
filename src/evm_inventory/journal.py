"""Durable transaction and Bitget-deposit state for sequential execution."""

from __future__ import annotations

import sqlite3
from pathlib import Path


class Journal:
    def __init__(self, path: Path):
        self.connection = sqlite3.connect(path)
        self.connection.row_factory = sqlite3.Row
        self.connection.execute("""
            CREATE TABLE IF NOT EXISTS operations (
              id INTEGER PRIMARY KEY, wallet TEXT NOT NULL, action TEXT NOT NULL,
              state TEXT NOT NULL DEFAULT 'planned', tx_hash TEXT,
              deposit_status TEXT, reason TEXT,
              created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
              updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            )
        """)
        columns = {
            row["name"] for row in self.connection.execute("PRAGMA table_info(operations)")
        }
        if "reason" not in columns:
            self.connection.execute("ALTER TABLE operations ADD COLUMN reason TEXT")
        self.connection.commit()

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.connection.close()

    def create_operation(self, *, wallet: str, action: str) -> int:
        cursor = self.connection.execute(
            "INSERT INTO operations(wallet, action) VALUES (?, ?)", (wallet.lower(), action)
        )
        self.connection.commit()
        return int(cursor.lastrowid)

    def record_transaction(self, operation_id: int, tx_hash: str) -> None:
        self._update(
            operation_id,
            "submitted",
            tx_hash=tx_hash,
            unless_states=("deferred", "approval_completed_route_deferred"),
        )

    def record_deposit_status(self, operation_id: int, status: str) -> None:
        state = "completed" if status == "success" else "deposit_pending"
        self._update(
            operation_id,
            state,
            status=status,
            unless_states=("deferred", "approval_completed_route_deferred"),
        )

    def record_deferred(self, operation_id: int, reason: str) -> None:
        self._update(operation_id, "deferred", reason=reason)

    def record_approval_completed_route_deferred(
        self, operation_id: int, approval_tx_hash: str, reason: str
    ) -> None:
        self._update(
            operation_id,
            "approval_completed_route_deferred",
            tx_hash=approval_tx_hash,
            reason=reason,
        )

    def operation(self, operation_id: int) -> dict:
        row = self.connection.execute(
            "SELECT * FROM operations WHERE id = ?", (operation_id,)
        ).fetchone()
        if row is None:
            raise KeyError(operation_id)
        return dict(row)

    def _update(
        self,
        operation_id: int,
        state: str,
        *,
        tx_hash: str | None = None,
        status: str | None = None,
        reason: str | None = None,
        unless_states: tuple[str, ...] = (),
    ) -> None:
        query = (
            "UPDATE operations SET state=?, tx_hash=COALESCE(?, tx_hash), "
            "deposit_status=COALESCE(?, deposit_status), "
            "reason=COALESCE(?, reason), "
            "updated_at=CURRENT_TIMESTAMP WHERE id=?"
        )
        parameters: tuple[str | int | None, ...] = (state, tx_hash, status, reason, operation_id)
        if unless_states:
            placeholders = ", ".join("?" for _ in unless_states)
            query += f" AND state NOT IN ({placeholders})"
            parameters += unless_states
        self.connection.execute(query, parameters)
        self.connection.commit()
