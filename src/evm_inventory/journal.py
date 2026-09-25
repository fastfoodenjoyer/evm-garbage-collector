"""Durable transaction and Bitget-deposit state for sequential execution."""

from __future__ import annotations

import re
import sqlite3
from decimal import Decimal, InvalidOperation, localcontext
from pathlib import Path

_GROUP_STATES = {
    "planned",
    "active",
    "deposit_pending",
    "gas_deferred",
    "completed",
    "manual_review_after_swap",
    "manual_review_group_threshold_exceeded",
    "manual_review_group_halted",
}
_GROUP_TRANSITIONS = {
    "planned": {
        "active",
        "gas_deferred",
        "completed",
        "manual_review_after_swap",
        "manual_review_group_threshold_exceeded",
        "manual_review_group_halted",
    },
    "active": {
        "deposit_pending",
        "gas_deferred",
        "completed",
        "manual_review_after_swap",
        "manual_review_group_threshold_exceeded",
        "manual_review_group_halted",
    },
    "deposit_pending": {
        "active", "completed", "deposit_pending", "manual_review_group_halted",
    },
    "gas_deferred": {"active", "gas_deferred"},
    "completed": set(),
    "manual_review_after_swap": set(),
    "manual_review_group_threshold_exceeded": set(),
    "manual_review_group_halted": set(),
}
_POSITION_TRANSITIONS = {
    "planned": {
        "gas_deferred",
        "swap_submitted",
        "direct_deposit_submitted",
        "source_asset_converted_bridge_pending",
        "completed",
        "manual_review_after_swap",
        "manual_review_group_threshold_exceeded",
        "manual_review_group_halted",
        "manual_review",
        "bridge_submitted",
    },
    "swap_submitted": {
        "source_asset_converted_bridge_pending",
        "manual_review_after_swap",
        "manual_review_group_threshold_exceeded",
        "manual_review_group_halted",
        "manual_review",
    },
    "source_asset_converted_bridge_pending": {
        "requote_required",
        "bridge_submitted",
        "manual_review_after_swap",
        "manual_review_group_threshold_exceeded",
        "manual_review_group_halted",
        "manual_review",
    },
    "requote_required": {
        "bridge_submitted",
        "manual_review_after_swap",
        "manual_review_group_threshold_exceeded",
    },
    "bridge_submitted": {
        "bridge_timeout",
        "completed",
        "manual_review_after_swap",
        "manual_review_group_threshold_exceeded",
        "manual_review_group_halted",
        "manual_review",
    },
    "bridge_timeout": {
        "requote_required",
        "completed",
        "manual_review_after_swap",
        "manual_review_group_halted",
    },
    "direct_deposit_submitted": {
        "deposit_pending",
        "gas_deferred",
        "completed",
        "manual_review_group_threshold_exceeded",
        "manual_review_group_halted",
        "manual_review",
    },
    "deposit_pending": {"completed", "manual_review"},
    "gas_deferred": {
        "direct_deposit_submitted", "bridge_submitted", "swap_submitted",
        "manual_review", "gas_deferred",
    },
    "completed": set(),
    "manual_review_after_swap": set(),
    "manual_review_group_threshold_exceeded": set(),
    "manual_review_group_halted": set(),
    "manual_review": set(),
}
_STEP_TRANSITIONS = {
    "planned": {"submitted", "failed", "manual_review"},
    "submitted": {
        "confirmed", "deposit_seen", "reverted", "failed", "bridge_timeout",
        "manual_review",
    },
    "deposit_seen": {"credited", "manual_review", "failed"},
    "confirmed": {"awaiting_bridge", "credited", "completed", "bridge_timeout", "manual_review"},
    "awaiting_bridge": {"credited", "bridge_timeout", "manual_review"},
    "bridge_timeout": {"requote_required", "credited", "manual_review"},
    "requote_required": {"submitted", "manual_review"},
    "credited": set(),
    "completed": set(),
    "reverted": set(),
    "failed": set(),
    "manual_review": set(),
}


class Journal:
    def __init__(self, path: Path, *, readonly: bool = False):
        self.connection = (
            sqlite3.connect(f"file:{path}?mode=ro", uri=True)
            if readonly else sqlite3.connect(path)
        )
        self.connection.row_factory = sqlite3.Row
        if readonly:
            return
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
        if "approval_tx_hash" not in columns:
            self.connection.execute("ALTER TABLE operations ADD COLUMN approval_tx_hash TEXT")
        self.connection.execute("""
            CREATE TABLE IF NOT EXISTS route_groups (
              id INTEGER PRIMARY KEY,
              group_key TEXT NOT NULL UNIQUE,
              wallet TEXT NOT NULL,
              source_chain_id INTEGER NOT NULL,
              loss_budget_pct TEXT NOT NULL,
              source_usd TEXT NOT NULL,
              projected_loss_usd TEXT,
              projected_loss_pct TEXT,
              realized_loss_usd TEXT NOT NULL DEFAULT '0',
              state TEXT NOT NULL DEFAULT 'planned',
              reason TEXT,
              created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
              updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            )
        """)
        self.connection.execute("""
            CREATE TABLE IF NOT EXISTS route_positions (
              id INTEGER PRIMARY KEY,
              position_key TEXT NOT NULL UNIQUE,
              wallet TEXT NOT NULL,
              group_id INTEGER REFERENCES route_groups(id),
              source_asset_id TEXT,
              source_amount_raw TEXT,
              actual_asset_id TEXT,
              actual_balance_raw TEXT,
              accounted_loss_usd TEXT,
              reason TEXT,
              state TEXT NOT NULL DEFAULT 'planned',
              created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
              updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            )
        """)
        self._migrate_columns(
            "route_positions",
            {
                "group_id": "INTEGER REFERENCES route_groups(id)",
                "source_asset_id": "TEXT",
                "source_amount_raw": "TEXT",
                "actual_asset_id": "TEXT",
                "actual_balance_raw": "TEXT",
                "accounted_loss_usd": "TEXT",
                "reason": "TEXT",
            },
        )
        self.connection.execute("""
            CREATE TABLE IF NOT EXISTS route_steps (
              id INTEGER PRIMARY KEY,
              position_id INTEGER NOT NULL REFERENCES route_positions(id),
              step_key TEXT NOT NULL,
              nonce INTEGER NOT NULL,
              state TEXT NOT NULL DEFAULT 'planned',
              calldata_digest TEXT NOT NULL,
              signed_payload_digest TEXT NOT NULL,
              tx_hash TEXT,
              broadcast_attempts INTEGER NOT NULL DEFAULT 0,
              receipt_status TEXT,
              finality_block INTEGER,
              balance_baseline_raw INTEGER,
              expected_delta_raw INTEGER,
              balance_baseline_raw_text TEXT,
              expected_delta_raw_text TEXT,
              timeout_report TEXT,
              created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
              updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
              UNIQUE(position_id, step_key, nonce)
            )
        """)
        self._migrate_columns(
            "route_steps",
            {
                "balance_baseline_raw_text": "TEXT",
                "expected_delta_raw_text": "TEXT",
            },
        )
        self.connection.execute("""
            CREATE TABLE IF NOT EXISTS route_events (
              id INTEGER PRIMARY KEY,
              position_id INTEGER NOT NULL REFERENCES route_positions(id),
              event_type TEXT NOT NULL,
              old_route_id TEXT,
              old_payload_hash TEXT,
              new_route_id TEXT,
              new_payload_hash TEXT,
              input_amount_raw TEXT,
              actual_asset_id TEXT,
              reason TEXT,
              created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            )
        """)
        self.connection.commit()

    def _migrate_columns(self, table: str, columns: dict[str, str]) -> None:
        existing = {
            row["name"]
            for row in self.connection.execute(f"PRAGMA table_info({table})")
        }
        for name, declaration in columns.items():
            if name not in existing:
                self.connection.execute(
                    f"ALTER TABLE {table} ADD COLUMN {name} {declaration}"
                )

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.connection.close()

    def has_route_tables(self) -> bool:
        names = {
            row[0] for row in self.connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )
        }
        return {"route_groups", "route_positions", "route_steps", "route_events"} <= names

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

    def record_deposit_status(
        self, operation_id: int, status: str, reason: str | None = None
    ) -> None:
        state = "completed" if status == "success" else "deposit_pending"
        self._update(
            operation_id,
            state,
            status=status, reason=reason,
            unless_states=("deferred", "approval_completed_route_deferred"),
        )

    def record_deferred(self, operation_id: int, reason: str) -> None:
        self._update(operation_id, "deferred", reason=reason)

    def record_approval_hash(self, operation_id: int, approval_tx_hash: str) -> None:
        self._update(operation_id, "submitted", approval_tx_hash=approval_tx_hash)

    def record_approval_completed_route_deferred(
        self, operation_id: int, approval_tx_hash: str, reason: str
    ) -> None:
        self._update(
            operation_id,
            "approval_completed_route_deferred",
            approval_tx_hash=approval_tx_hash,
            reason=reason,
        )

    def operation(self, operation_id: int) -> dict:
        row = self.connection.execute(
            "SELECT * FROM operations WHERE id = ?", (operation_id,)
        ).fetchone()
        if row is None:
            raise KeyError(operation_id)
        return dict(row)

    def get_or_create_group(
        self,
        *,
        group_key: str,
        wallet: str,
        source_chain_id: int,
        loss_budget_pct: str,
        source_usd: str,
    ) -> dict:
        """Persist one wallet/source-network loss budget without sensitive data."""

        budget = _decimal_text(loss_budget_pct, "loss budget")
        source_value = _decimal_text(source_usd, "group source USD")
        if not group_key or not wallet or source_chain_id <= 0:
            raise ValueError("invalid route group identity")
        if Decimal(budget) <= 0 or Decimal(budget) > 100 or Decimal(source_value) <= 0:
            raise ValueError("invalid route group valuation")
        normalized_wallet = wallet.lower()
        self.connection.execute(
            """INSERT OR IGNORE INTO route_groups(
                group_key, wallet, source_chain_id, loss_budget_pct, source_usd
            ) VALUES (?, ?, ?, ?, ?)""",
            (group_key, normalized_wallet, source_chain_id, budget, source_value),
        )
        row = self.connection.execute(
            "SELECT * FROM route_groups WHERE group_key=?", (group_key,)
        ).fetchone()
        assert row is not None
        if (
            row["wallet"] != normalized_wallet
            or row["source_chain_id"] != source_chain_id
            or row["loss_budget_pct"] != budget
            or row["source_usd"] != source_value
        ):
            raise ValueError("conflicting route group identity or budget")
        self.connection.commit()
        return dict(row)

    def group(self, group_id: int) -> dict:
        row = self.connection.execute(
            "SELECT * FROM route_groups WHERE id=?", (group_id,)
        ).fetchone()
        if row is None:
            raise KeyError(group_id)
        return dict(row)

    def reactivate_known_direct_deposit(self, group_id: int, position_key: str) -> bool:
        """Recover a legacy API failure only when exactly one known send exists."""
        rows = self.connection.execute(
            "SELECT id, position_key, state FROM route_positions WHERE group_id=?",
            (group_id,),
        ).fetchall()
        if len(rows) != 1 or rows[0]["position_key"] != position_key:
            return False
        position = rows[0]
        if position["state"] not in {"manual_review", "deposit_pending"}:
            return False
        step = self.latest_step(position_id=position["id"], step_key="direct_deposit")
        if step is None or not step["tx_hash"] or step["state"] in {"failed", "reverted"}:
            return False
        self.connection.execute(
            """UPDATE route_positions SET state='deposit_pending',
               updated_at=CURRENT_TIMESTAMP WHERE id=?""",
            (position["id"],),
        )
        self.connection.execute(
            """UPDATE route_groups SET state='deposit_pending',
               updated_at=CURRENT_TIMESTAMP WHERE id=?""",
            (group_id,),
        )
        self.connection.commit()
        return True

    def reactivate_legacy_gas_deferral(self, group_id: int, position_key: str) -> bool:
        """Retry an old pre-signature gas-cap refusal without changing the cap."""
        rows = self.connection.execute(
            "SELECT id, position_key, state, reason FROM route_positions WHERE group_id=?",
            (group_id,),
        ).fetchall()
        if len(rows) != 1 or rows[0]["position_key"] != position_key:
            return False
        position = rows[0]
        if (
            position["state"] != "manual_review"
            or not str(position["reason"] or "").startswith("ethereum_gas_deferred:")
        ):
            return False
        broadcast = self.connection.execute(
            "SELECT 1 FROM route_steps WHERE position_id=? AND tx_hash IS NOT NULL LIMIT 1",
            (position["id"],),
        ).fetchone()
        if broadcast is not None:
            return False
        self.connection.execute(
            "UPDATE route_positions SET state='gas_deferred' WHERE id=?",
            (position["id"],),
        )
        self.connection.execute(
            "UPDATE route_groups SET state='gas_deferred' WHERE id=?",
            (group_id,),
        )
        self.connection.commit()
        return True

    def record_group_projection(
        self, group_id: int, *, projected_loss_usd: str, projected_loss_pct: str
    ) -> None:
        loss = _decimal_text(projected_loss_usd, "projected group loss")
        percentage = _decimal_text(projected_loss_pct, "projected group loss percent")
        group = self.group(group_id)
        if group["state"] not in {"planned", "active"}:
            raise ValueError("cannot update a terminal route group projection")
        self.connection.execute(
            """UPDATE route_groups SET projected_loss_usd=?, projected_loss_pct=?,
               updated_at=CURRENT_TIMESTAMP WHERE id=?""",
            (loss, percentage, group_id),
        )
        self.connection.commit()

    def record_group_realized_loss(self, group_id: int, *, realized_loss_usd: str) -> None:
        loss = _decimal_text(realized_loss_usd, "realised group loss")
        group = self.group(group_id)
        if Decimal(loss) < Decimal(group["realized_loss_usd"]):
            raise ValueError("realised group loss cannot move backwards")
        if group["state"] not in {"planned", "active", "deposit_pending"}:
            raise ValueError("cannot update a terminal route group realised loss")
        self.connection.execute(
            """UPDATE route_groups SET realized_loss_usd=?, updated_at=CURRENT_TIMESTAMP
               WHERE id=?""",
            (loss, group_id),
        )
        self.connection.commit()

    def account_position_loss(self, position_id: int, *, loss_usd: str) -> str:
        """Count a broadcast position's loss once, including across restarts."""
        loss = _decimal_text(loss_usd, "position realised loss")
        position = self.position(position_id)
        group_id = position["group_id"]
        if group_id is None:
            raise ValueError("position has no route group")
        existing = position["accounted_loss_usd"]
        if existing is not None:
            # Reconciliation can revalue the same transfer after a restart.
            # The first recorded cost remains authoritative for this position.
            return self.group(group_id)["realized_loss_usd"]
        with localcontext() as context:
            context.prec = 80
            total = Decimal(self.group(group_id)["realized_loss_usd"]) + Decimal(loss)
        with self.connection:
            self.connection.execute(
                "UPDATE route_positions SET accounted_loss_usd=? WHERE id=?",
                (loss, position_id),
            )
            self.connection.execute(
                """UPDATE route_groups SET realized_loss_usd=?,
                   updated_at=CURRENT_TIMESTAMP WHERE id=?""",
                (format(total, "f"), group_id),
            )
        return format(total, "f")

    def record_group_state(
        self, group_id: int, state: str, *, reason: str | None = None
    ) -> None:
        group = self.group(group_id)
        if state not in _GROUP_STATES:
            raise ValueError("invalid route group state")
        current = group["state"]
        if state != current and state not in _GROUP_TRANSITIONS.get(current, set()):
            raise ValueError(f"invalid group transition: {current} -> {state}")
        self.connection.execute(
            """UPDATE route_groups SET state=?, reason=COALESCE(?, reason),
               updated_at=CURRENT_TIMESTAMP WHERE id=?""",
            (state, reason, group_id),
        )
        self.connection.commit()

    def get_or_create_position(
        self,
        *,
        position_key: str,
        wallet: str,
        group_id: int | None = None,
        source_asset_id: str | None = None,
        source_amount_raw: str | None = None,
    ) -> dict:
        """Return the durable, key-free identity for one consolidating position."""

        if not position_key or not wallet:
            raise ValueError("position key and wallet are required")
        source_amount = (
            _raw_amount_text(source_amount_raw, "source amount")
            if source_amount_raw is not None
            else None
        )
        self.connection.execute(
            """INSERT OR IGNORE INTO route_positions(
                position_key, wallet, group_id, source_asset_id, source_amount_raw
            ) VALUES (?, ?, ?, ?, ?)""",
            (position_key, wallet.lower(), group_id, source_asset_id, source_amount),
        )
        row = self.connection.execute(
            "SELECT * FROM route_positions WHERE position_key = ?", (position_key,)
        ).fetchone()
        assert row is not None
        for column, expected in (
            ("group_id", group_id),
            ("source_asset_id", source_asset_id),
            ("source_amount_raw", source_amount),
        ):
            if expected is not None and row[column] != expected:
                raise ValueError("conflicting durable route position")
        self.connection.commit()
        return dict(row)

    def position(self, position_id: int) -> dict:
        row = self.connection.execute(
            "SELECT * FROM route_positions WHERE id=?", (position_id,)
        ).fetchone()
        if row is None:
            raise KeyError(position_id)
        return dict(row)

    def group_positions(self, group_id: int) -> list[dict]:
        rows = self.connection.execute(
            "SELECT * FROM route_positions WHERE group_id=? ORDER BY id", (group_id,)
        ).fetchall()
        return [dict(row) for row in rows]

    def record_position_state(
        self,
        position_id: int,
        state: str,
        *,
        actual_asset_id: str | None = None,
        actual_balance_raw: str | int | None = None,
        reason: str | None = None,
    ) -> None:
        position = self.position(position_id)
        if state not in _POSITION_TRANSITIONS:
            raise ValueError("invalid route position state")
        current = position["state"]
        if state != current and state not in _POSITION_TRANSITIONS[current]:
            raise ValueError(f"invalid position transition: {current} -> {state}")
        raw_balance = (
            _raw_amount_text(actual_balance_raw, "actual balance")
            if actual_balance_raw is not None
            else None
        )
        self.connection.execute(
            """UPDATE route_positions SET state=?,
               actual_asset_id=COALESCE(?, actual_asset_id),
               actual_balance_raw=COALESCE(?, actual_balance_raw),
               reason=COALESCE(?, reason), updated_at=CURRENT_TIMESTAMP WHERE id=?""",
            (state, actual_asset_id, raw_balance, reason, position_id),
        )
        self.connection.commit()

    def record_requote_after_swap(
        self,
        position_id: int,
        *,
        old_route_id: str,
        old_payload_hash: str,
        new_route_id: str,
        new_payload_hash: str,
        input_amount_raw: str | int,
        actual_asset_id: str,
        reason: str | None = None,
    ) -> int:
        position = self.position(position_id)
        if not old_route_id or not new_route_id or not actual_asset_id:
            raise ValueError("requote evidence identity is incomplete")
        _require_sha256(old_payload_hash, "old payload hash")
        _require_sha256(new_payload_hash, "new payload hash")
        amount = _raw_amount_text(input_amount_raw, "requote input amount")
        if amount == "0":
            raise ValueError("requote input amount must be positive")
        if position["state"] not in {
            "source_asset_converted_bridge_pending",
            "bridge_timeout",
        }:
            raise ValueError("requote_after_swap requires a settled source swap")
        previous = self.connection.execute(
            """SELECT 1 FROM route_events
               WHERE position_id=? AND event_type='requote_after_swap' LIMIT 1""",
            (position_id,),
        ).fetchone()
        if previous is not None:
            raise ValueError("only one post-swap bridge requote is permitted")
        if "requote_required" not in _POSITION_TRANSITIONS[position["state"]]:
            raise ValueError("position cannot enter requote_required")
        cursor = self.connection.execute(
            """INSERT INTO route_events(
                position_id, event_type, old_route_id, old_payload_hash,
                new_route_id, new_payload_hash, input_amount_raw,
                actual_asset_id, reason
            ) VALUES (?, 'requote_after_swap', ?, ?, ?, ?, ?, ?, ?)""",
            (
                position_id,
                old_route_id,
                old_payload_hash.lower(),
                new_route_id,
                new_payload_hash.lower(),
                amount,
                actual_asset_id,
                reason,
            ),
        )
        self.connection.execute(
            """UPDATE route_positions SET state='requote_required', actual_asset_id=?,
               actual_balance_raw=?, reason=COALESCE(?, reason),
               updated_at=CURRENT_TIMESTAMP WHERE id=?""",
            (actual_asset_id, amount, reason, position_id),
        )
        self.connection.commit()
        return int(cursor.lastrowid)

    def record_requote_after_bridge(
        self,
        position_id: int,
        *,
        old_route_id: str,
        old_payload_hash: str,
        new_route_id: str,
        new_payload_hash: str,
        input_amount_raw: str | int,
        actual_asset_id: str,
    ) -> int:
        """Persist the fresh route selected after an intermediate bridge arrival."""

        self.position(position_id)
        if not old_route_id or not new_route_id or not actual_asset_id:
            raise ValueError("dependent requote evidence identity is incomplete")
        _require_sha256(old_payload_hash, "old payload hash")
        _require_sha256(new_payload_hash, "new payload hash")
        amount = _raw_amount_text(input_amount_raw, "dependent requote input amount")
        if amount == "0":
            raise ValueError("dependent requote input amount must be positive")
        cursor = self.connection.execute(
            """INSERT INTO route_events(
                position_id, event_type, old_route_id, old_payload_hash,
                new_route_id, new_payload_hash, input_amount_raw, actual_asset_id
            ) VALUES (?, 'requote_after_bridge', ?, ?, ?, ?, ?, ?)""",
            (
                position_id,
                old_route_id,
                old_payload_hash.lower(),
                new_route_id,
                new_payload_hash.lower(),
                amount,
                actual_asset_id.lower(),
            ),
        )
        self.connection.commit()
        return int(cursor.lastrowid)

    def position_events(self, position_id: int) -> list[dict]:
        rows = self.connection.execute(
            "SELECT * FROM route_events WHERE position_id=? ORDER BY id DESC",
            (position_id,),
        ).fetchall()
        return [dict(row) for row in rows]

    def record_step_intent(
        self,
        *,
        position_id: int,
        step_key: str,
        nonce: int,
        calldata_digest: str,
        signed_payload_digest: str,
    ) -> dict:
        """Persist an intent before broadcasting, storing only one-way digests."""

        if not step_key or nonce < 0 or not calldata_digest or not signed_payload_digest:
            raise ValueError("invalid durable step intent")
        self.connection.execute(
            """INSERT OR IGNORE INTO route_steps(
                position_id, step_key, nonce, calldata_digest, signed_payload_digest
            ) VALUES (?, ?, ?, ?, ?)""",
            (position_id, step_key, nonce, calldata_digest, signed_payload_digest),
        )
        row = self.connection.execute(
            "SELECT * FROM route_steps WHERE position_id=? AND step_key=? AND nonce=?",
            (position_id, step_key, nonce),
        ).fetchone()
        assert row is not None
        if (
            row["calldata_digest"] != calldata_digest
            or row["signed_payload_digest"] != signed_payload_digest
        ):
            raise ValueError("conflicting durable step intent")
        self.connection.commit()
        return dict(row)

    def step(self, step_id: int) -> dict:
        row = self.connection.execute("SELECT * FROM route_steps WHERE id=?", (step_id,)).fetchone()
        if row is None:
            raise KeyError(step_id)
        return dict(row)

    def latest_step(self, *, position_id: int, step_key: str) -> dict | None:
        row = self.connection.execute(
            """SELECT * FROM route_steps WHERE position_id=? AND step_key=?
               ORDER BY id DESC LIMIT 1""",
            (position_id, step_key),
        ).fetchone()
        return dict(row) if row is not None else None

    def unresolved_direct_deposit(
        self, *, wallet: str, chain_id: int, asset_id: str, exclude_position_id: int
    ) -> dict | None:
        row = self.connection.execute(
            """SELECT p.id AS position_id, s.tx_hash FROM route_positions p
               JOIN route_groups g ON g.id=p.group_id
               JOIN route_steps s ON s.position_id=p.id
               WHERE p.wallet=? AND g.source_chain_id=? AND p.source_asset_id=?
                 AND p.id<>? AND p.state<>'completed'
                 AND s.step_key='direct_deposit' AND s.tx_hash IS NOT NULL
               ORDER BY s.id DESC LIMIT 1""",
            (wallet.lower(), chain_id, asset_id.lower(), exclude_position_id),
        ).fetchone()
        return dict(row) if row is not None else None

    def record_broadcast_attempt(self, step_id: int, tx_hash: str) -> None:
        self._transition_step(step_id, "submitted")
        self.connection.execute(
            """UPDATE route_steps SET tx_hash=COALESCE(tx_hash, ?),
               broadcast_attempts=broadcast_attempts+1, updated_at=CURRENT_TIMESTAMP WHERE id=?""",
            (tx_hash, step_id),
        )
        self.connection.commit()

    def record_receipt(
        self, step_id: int, *, status: str, finality_block: int | None = None
    ) -> None:
        self._transition_step(step_id, status)
        self.connection.execute(
            """UPDATE route_steps SET receipt_status=?, finality_block=?,
               updated_at=CURRENT_TIMESTAMP WHERE id=?""",
            (status, finality_block, step_id),
        )
        self.connection.commit()

    def record_balance_baseline(
        self, step_id: int, *, balance_raw: str | int, expected_delta_raw: str | int
    ) -> None:
        baseline = _raw_amount_text(balance_raw, "balance baseline")
        expected = _raw_amount_text(expected_delta_raw, "expected balance delta")
        legacy_baseline = _sqlite_integer(baseline)
        legacy_expected = _sqlite_integer(expected)
        self.connection.execute(
            """UPDATE route_steps SET
               balance_baseline_raw=COALESCE(balance_baseline_raw, ?),
               expected_delta_raw=COALESCE(expected_delta_raw, ?),
               balance_baseline_raw_text=COALESCE(balance_baseline_raw_text, ?),
               expected_delta_raw_text=COALESCE(expected_delta_raw_text, ?),
               updated_at=CURRENT_TIMESTAMP WHERE id=?""",
            (legacy_baseline, legacy_expected, baseline, expected, step_id),
        )
        self.connection.commit()

    def record_timeout_report(self, step_id: int, report: str) -> None:
        self._transition_step(step_id, "bridge_timeout")
        self.connection.execute(
            """UPDATE route_steps SET timeout_report=?,
               updated_at=CURRENT_TIMESTAMP WHERE id=?""",
            (report, step_id),
        )
        self.connection.commit()

    def record_step_state(self, step_id: int, state: str) -> None:
        self._transition_step(step_id, state)
        self.connection.commit()

    def _transition_step(self, step_id: int, state: str) -> None:
        row = self.connection.execute(
            "SELECT state FROM route_steps WHERE id=?", (step_id,)
        ).fetchone()
        if row is None:
            raise KeyError(step_id)
        current = str(row["state"])
        if state not in _STEP_TRANSITIONS:
            raise ValueError("invalid route step state")
        if state != current and state not in _STEP_TRANSITIONS[current]:
            raise ValueError(f"invalid route step transition: {current} -> {state}")
        self.connection.execute(
            "UPDATE route_steps SET state=?, updated_at=CURRENT_TIMESTAMP WHERE id=?",
            (state, step_id),
        )

    def route_step_state_counts(self) -> dict[str, int]:
        rows = self.connection.execute(
            "SELECT state, COUNT(*) AS count FROM route_steps GROUP BY state"
        ).fetchall()
        return {str(row["state"]): int(row["count"]) for row in rows}

    def route_group_state_counts(self) -> dict[str, int]:
        rows = self.connection.execute(
            "SELECT state, COUNT(*) AS count FROM route_groups GROUP BY state"
        ).fetchall()
        return {str(row["state"]): int(row["count"]) for row in rows}

    def route_position_state_counts(self) -> dict[str, int]:
        rows = self.connection.execute(
            "SELECT state, COUNT(*) AS count FROM route_positions GROUP BY state"
        ).fetchall()
        return {str(row["state"]): int(row["count"]) for row in rows}

    def route_reason_counts(self) -> list[dict[str, str | int]]:
        rows = self.connection.execute(
            """SELECT reason, COUNT(*) AS count FROM route_positions
               WHERE reason IS NOT NULL GROUP BY reason ORDER BY reason"""
        ).fetchall()
        return [
            {"reason": str(row["reason"]), "count": int(row["count"])}
            for row in rows
        ]

    def _update(
        self,
        operation_id: int,
        state: str,
        *,
        tx_hash: str | None = None,
        status: str | None = None,
        reason: str | None = None,
        approval_tx_hash: str | None = None,
        unless_states: tuple[str, ...] = (),
    ) -> None:
        query = (
            "UPDATE operations SET state=?, tx_hash=COALESCE(?, tx_hash), "
            "approval_tx_hash=COALESCE(?, approval_tx_hash), "
            "deposit_status=COALESCE(?, deposit_status), "
            "reason=COALESCE(?, reason), "
            "updated_at=CURRENT_TIMESTAMP WHERE id=?"
        )
        parameters: tuple[str | int | None, ...] = (
            state, tx_hash, approval_tx_hash, status, reason, operation_id
        )
        if unless_states:
            placeholders = ", ".join("?" for _ in unless_states)
            query += f" AND state NOT IN ({placeholders})"
            parameters += unless_states
        self.connection.execute(query, parameters)
        self.connection.commit()


def _decimal_text(value: str, field: str) -> str:
    if isinstance(value, (bool, float)) or not isinstance(value, (str, Decimal)):
        raise ValueError(f"{field} must be an exact decimal string")
    try:
        parsed = Decimal(value)
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise ValueError(f"{field} must be a finite non-negative decimal") from exc
    if not parsed.is_finite() or parsed < 0:
        raise ValueError(f"{field} must be a finite non-negative decimal")
    return format(parsed.normalize(), "f") if parsed else "0"


def _raw_amount_text(value: str | int, field: str) -> str:
    if isinstance(value, bool) or not isinstance(value, (str, int)):
        raise ValueError(f"{field} must be a non-negative integer")
    if isinstance(value, str) and not re.fullmatch(r"\d+", value):
        raise ValueError(f"{field} must be a non-negative integer")
    amount = int(value)
    if amount < 0:
        raise ValueError(f"{field} must be a non-negative integer")
    return str(amount)


def _require_sha256(value: str, field: str) -> None:
    if not isinstance(value, str) or not re.fullmatch(r"[0-9a-fA-F]{64}", value):
        raise ValueError(f"{field} must be a 32-byte SHA-256 hex digest")


def _sqlite_integer(value: str) -> int | None:
    parsed = int(value)
    return parsed if parsed <= 2**63 - 1 else None
