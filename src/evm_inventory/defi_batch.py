"""Durable, randomized scheduling for a reviewed set of DeFi wallets."""

from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import os
import random
import re
import secrets
import sqlite3
import stat
import sys
import time
from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path
from typing import Any

import httpx

from .cli import _execution_rpc_urls, _load_dotenv
from .config import load_catalog
from .defi_operations import execute_defi, quote_defi
from .diagnostics import exception_diagnostic, redact_data, redact_text
from .executor import AmbiguousBroadcast
from .models import ConfigError
from .rpc import RpcReader, quantity
from .store import Store
from .transport import RequestError, Transport
from .workbook import load_wallet_workbook, parse_ordinal_ranges


def _connect(db_path: Path) -> sqlite3.Connection:
    connection = sqlite3.connect(db_path, timeout=30)
    connection.row_factory = sqlite3.Row
    return connection


def _ensure_tables(connection: sqlite3.Connection) -> None:
    connection.executescript("""
        CREATE TABLE IF NOT EXISTS defi_batch_runs (
            run_id TEXT PRIMARY KEY,
            workbook_path TEXT NOT NULL,
            db_path TEXT NOT NULL,
            output_dir TEXT NOT NULL,
            seed_plan_sha256 TEXT NOT NULL,
            gas_cap_wei TEXT NOT NULL,
            delay_min_seconds INTEGER NOT NULL,
            delay_max_seconds INTEGER NOT NULL,
            within_wallet_delay_min_seconds INTEGER NOT NULL DEFAULT 60,
            within_wallet_delay_max_seconds INTEGER NOT NULL DEFAULT 300,
            status TEXT NOT NULL,
            created_at REAL NOT NULL,
            completed_at REAL
        );
        CREATE TABLE IF NOT EXISTS defi_batch_wallets (
            run_id TEXT NOT NULL REFERENCES defi_batch_runs(run_id),
            ordinal INTEGER NOT NULL,
            wallet TEXT NOT NULL,
            queue_index INTEGER,
            status TEXT NOT NULL,
            due_at REAL,
            attempts INTEGER NOT NULL DEFAULT 0,
            ready_expected INTEGER NOT NULL DEFAULT 0,
            ready_observed INTEGER NOT NULL DEFAULT 0,
            last_error TEXT,
            started_at REAL,
            completed_at REAL,
            PRIMARY KEY(run_id, ordinal)
        );
        CREATE TABLE IF NOT EXISTS defi_batch_actions (
            run_id TEXT NOT NULL,
            ordinal INTEGER NOT NULL,
            action_id TEXT NOT NULL,
            plan_sha256 TEXT NOT NULL,
            plan_path TEXT NOT NULL,
            status TEXT NOT NULL,
            tx_hash TEXT,
            reason TEXT,
            updated_at REAL NOT NULL,
            PRIMARY KEY(run_id, ordinal, action_id)
        );
        CREATE TABLE IF NOT EXISTS defi_batch_transactions (
            tx_hash TEXT PRIMARY KEY,
            run_id TEXT NOT NULL,
            ordinal INTEGER NOT NULL,
            action_id TEXT NOT NULL,
            observed_at REAL NOT NULL,
            next_not_before REAL NOT NULL,
            same_wallet_not_before REAL
        );
        CREATE TABLE IF NOT EXISTS defi_batch_outcomes (
            id INTEGER PRIMARY KEY,
            run_id TEXT NOT NULL,
            ordinal INTEGER NOT NULL,
            action_id TEXT,
            protocol_name TEXT NOT NULL,
            chain_id INTEGER,
            withdrawal_size TEXT NOT NULL,
            estimated_usd REAL,
            native_symbol TEXT,
            native_before_wei TEXT,
            native_after_wei TEXT,
            status TEXT NOT NULL,
            reason TEXT,
            error_detail TEXT,
            fee_detail TEXT,
            tx_hash TEXT,
            observed_at REAL NOT NULL,
            UNIQUE(run_id, ordinal, action_id, status)
        );
        CREATE TABLE IF NOT EXISTS defi_batch_baselines (
            run_id TEXT NOT NULL,
            ordinal INTEGER NOT NULL,
            action_id TEXT NOT NULL,
            native_symbol TEXT,
            native_before_wei TEXT,
            PRIMARY KEY(run_id, ordinal, action_id)
        );
    """)
    run_columns = {
        row["name"] for row in connection.execute("PRAGMA table_info(defi_batch_runs)")
    }
    for column, default in (
        ("within_wallet_delay_min_seconds", 60),
        ("within_wallet_delay_max_seconds", 300),
    ):
        if column not in run_columns:
            connection.execute(
                f"ALTER TABLE defi_batch_runs ADD COLUMN {column} "
                f"INTEGER NOT NULL DEFAULT {default}"
            )
    transaction_columns = {
        row["name"] for row in connection.execute("PRAGMA table_info(defi_batch_transactions)")
    }
    if "same_wallet_not_before" not in transaction_columns:
        connection.execute(
            "ALTER TABLE defi_batch_transactions ADD COLUMN same_wallet_not_before REAL"
        )
    outcome_columns = {
        row["name"] for row in connection.execute("PRAGMA table_info(defi_batch_outcomes)")
    }
    if "error_detail" not in outcome_columns:
        connection.execute("ALTER TABLE defi_batch_outcomes ADD COLUMN error_detail TEXT")
    if "fee_detail" not in outcome_columns:
        connection.execute("ALTER TABLE defi_batch_outcomes ADD COLUMN fee_detail TEXT")
    for row in connection.execute(
        "SELECT t.tx_hash, t.observed_at, r.within_wallet_delay_min_seconds AS minimum, "
        "r.within_wallet_delay_max_seconds AS maximum FROM defi_batch_transactions t "
        "JOIN defi_batch_runs r ON r.run_id=t.run_id "
        "WHERE t.same_wallet_not_before IS NULL"
    ).fetchall():
        delay = random.SystemRandom().randint(row["minimum"], row["maximum"])
        connection.execute(
            "UPDATE defi_batch_transactions SET same_wallet_not_before=? WHERE tx_hash=?",
            (row["observed_at"] + delay, row["tx_hash"]),
        )
    connection.commit()


def initialize_batch(
    *,
    db_path: Path,
    workbook_path: Path,
    seed_plan_path: Path,
    run_id: str,
    wallet_count: int = 50,
    wallet_ordinals: str | None = None,
    gas_cap_wei: int = 10**15,
    delay_min_seconds: int = 1200,
    delay_max_seconds: int = 6000,
    within_wallet_delay_min_seconds: int = 60,
    within_wallet_delay_max_seconds: int = 300,
    output_dir: Path,
    rng: random.Random | random.SystemRandom | None = None,
    now: float | None = None,
) -> dict[str, Any]:
    """Queue only wallets with ready actions in the stored seed plan."""

    if (wallet_count <= 0 or gas_cap_wei <= 0
            or not 0 < delay_min_seconds <= delay_max_seconds
            or not 0 < within_wallet_delay_min_seconds <= within_wallet_delay_max_seconds):
        raise ValueError("invalid DeFi batch limits")
    db_path, workbook_path = db_path.resolve(), workbook_path.resolve()
    output_dir, seed_plan_path = output_dir.resolve(), seed_plan_path.resolve()
    now = time.time() if now is None else now
    rows = load_wallet_workbook(
        workbook_path,
        require_deposit_address=False,
        require_rabby_proxy=True,
        ordinal_ranges=parse_ordinal_ranges(
            wallet_ordinals if wallet_ordinals else f"1-{wallet_count}"
        ),
    )
    if not rows or (wallet_ordinals is None and len(rows) != wallet_count):
        raise ValueError("wallet workbook does not contain the requested ordinal range")
    plan_bytes = seed_plan_path.read_bytes()
    digest = hashlib.sha256(plan_bytes).hexdigest()
    with Store(db_path, readonly=True) as store:
        if store.defi_plan(digest) != plan_bytes:
            raise ValueError("seed DeFi plan is not stored in the project database")
    plan = json.loads(plan_bytes)
    if plan.get("schema") != "rabby-defi-withdraw-v1":
        raise ValueError("invalid seed DeFi plan")
    selected_wallets = {row.public_address for row in rows}
    entries = plan.get("entries", [])
    if not isinstance(entries, list) or any(
        not isinstance(entry, dict) or entry.get("wallet") not in selected_wallets
        for entry in entries
    ):
        raise ValueError("seed DeFi plan contains an out-of-scope wallet")
    ready = Counter(
        entry["wallet"] for entry in entries
        if entry.get("status") == "ready" and isinstance(entry.get("action_id"), str)
    )
    queue = [row for row in rows if ready[row.public_address] > 0]
    (rng or random.SystemRandom()).shuffle(queue)
    positions = {row.ordinal: index for index, row in enumerate(queue)}
    output_dir.mkdir(parents=True, exist_ok=True)
    with _connect(db_path) as connection:
        _ensure_tables(connection)
        if connection.execute(
            "SELECT 1 FROM defi_batch_runs WHERE run_id=?", (run_id,)
        ).fetchone():
            raise ValueError("DeFi batch run already exists")
        connection.execute(
            "INSERT INTO defi_batch_runs "
            "(run_id, workbook_path, db_path, output_dir, seed_plan_sha256, gas_cap_wei, "
            "delay_min_seconds, delay_max_seconds, within_wallet_delay_min_seconds, "
            "within_wallet_delay_max_seconds, status, created_at, completed_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (run_id, str(workbook_path), str(db_path), str(output_dir), digest,
             str(gas_cap_wei), delay_min_seconds, delay_max_seconds,
             within_wallet_delay_min_seconds, within_wallet_delay_max_seconds,
             "running", now, None),
        )
        connection.executemany(
            "INSERT INTO defi_batch_wallets "
            "(run_id, ordinal, wallet, queue_index, status, due_at, ready_expected) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            [
                (run_id, row.ordinal, row.public_address, positions.get(row.ordinal),
                 "queued" if row.ordinal in positions else "no_ready",
                 now if positions.get(row.ordinal) == 0 else None,
                 ready[row.public_address])
                for row in rows
            ],
        )
    return {"run_id": run_id, "wallets": len(rows), "queued": len(queue),
            "no_ready": len(rows) - len(queue), "seed_plan_sha256": digest}


def advance_wallet(
    db_path: Path,
    run_id: str,
    ordinal: int,
    *,
    status: str,
    now: float | None = None,
    rng: random.Random | random.SystemRandom | None = None,
) -> int | None:
    """Finish one wallet; only broadcast transactions consume the random delay."""

    if status not in {"done", "no_ready", "manual_review", "failed"}:
        raise ValueError("invalid final wallet status")
    now = time.time() if now is None else now
    with _connect(db_path) as connection:
        run = connection.execute(
            "SELECT * FROM defi_batch_runs WHERE run_id=?", (run_id,)
        ).fetchone()
        wallet = connection.execute(
            "SELECT * FROM defi_batch_wallets WHERE run_id=? AND ordinal=?",
            (run_id, ordinal),
        ).fetchone()
        if run is None or wallet is None or wallet["queue_index"] is None:
            raise ValueError("unknown queued wallet")
        if wallet["status"] not in {"queued", "active"}:
            raise ValueError("wallet is already finished")
        connection.execute(
            "UPDATE defi_batch_wallets SET status=?, completed_at=? "
            "WHERE run_id=? AND ordinal=?", (status, now, run_id, ordinal),
        )
        next_wallet = connection.execute(
            "SELECT ordinal FROM defi_batch_wallets WHERE run_id=? AND queue_index>? "
            "AND status='queued' ORDER BY queue_index LIMIT 1",
            (run_id, wallet["queue_index"]),
        ).fetchone()
        if next_wallet is None:
            connection.execute(
                "UPDATE defi_batch_runs SET status='completed', completed_at=? WHERE run_id=?",
                (now, run_id),
            )
            return None
        connection.execute(
            "UPDATE defi_batch_wallets SET due_at=? WHERE run_id=? AND ordinal=?",
            (now, run_id, next_wallet["ordinal"]),
        )
        return 0


def record_transaction(
    connection: sqlite3.Connection, run: sqlite3.Row, ordinal: int, action_id: str,
    tx_hash: str, *, now: float, rng: random.Random | random.SystemRandom | None = None,
) -> float:
    """Persist randomized deadlines for the same wallet and a different wallet."""

    existing = connection.execute(
        "SELECT next_not_before FROM defi_batch_transactions WHERE tx_hash=?", (tx_hash,)
    ).fetchone()
    if existing:
        return float(existing["next_not_before"])
    randomizer = rng or random.SystemRandom()
    between_wallet_delay = randomizer.randint(
        run["delay_min_seconds"], run["delay_max_seconds"]
    )
    within_wallet_delay = randomizer.randint(
        run["within_wallet_delay_min_seconds"],
        run["within_wallet_delay_max_seconds"],
    )
    due = now + between_wallet_delay
    connection.execute(
        "INSERT INTO defi_batch_transactions "
        "(tx_hash, run_id, ordinal, action_id, observed_at, next_not_before, "
        "same_wallet_not_before) VALUES (?, ?, ?, ?, ?, ?, ?)",
        (tx_hash, run["run_id"], ordinal, action_id, now, due,
         now + within_wallet_delay),
    )
    connection.commit()
    return due


def _next_transaction_due(
    connection: sqlite3.Connection, run_id: str, ordinal: int,
) -> float | None:
    row = connection.execute(
        "SELECT ordinal, next_not_before, same_wallet_not_before "
        "FROM defi_batch_transactions WHERE run_id=? "
        "ORDER BY observed_at DESC LIMIT 1", (run_id,)
    ).fetchone()
    if row is None:
        return None
    deadline = (row["same_wallet_not_before"] if row["ordinal"] == ordinal
                else row["next_not_before"])
    return float(deadline)


def _reconcile_active_wallet_due(connection: sqlite3.Connection, run_id: str) -> None:
    """Replace a persisted old global delay with the same-wallet deadline."""

    active = connection.execute(
        "SELECT ordinal, due_at FROM defi_batch_wallets WHERE run_id=? AND status='active' "
        "ORDER BY queue_index LIMIT 1", (run_id,),
    ).fetchone()
    last = connection.execute(
        "SELECT ordinal, next_not_before, same_wallet_not_before "
        "FROM defi_batch_transactions WHERE run_id=? "
        "ORDER BY observed_at DESC LIMIT 1", (run_id,),
    ).fetchone()
    if (active is not None and last is not None and active["ordinal"] == last["ordinal"]
            and active["due_at"] is not None
            and abs(active["due_at"] - last["next_not_before"]) < 0.001):
        connection.execute(
            "UPDATE defi_batch_wallets SET due_at=? WHERE run_id=? AND ordinal=?",
            (last["same_wallet_not_before"], run_id, active["ordinal"]),
        )
        connection.commit()


def _native_snapshot(chain_id: int, wallet: str) -> tuple[str | None, int | None]:
    """Read current native balance without signing or using a wallet proxy."""

    try:
        _load_dotenv()
        catalog = load_catalog()
        network = next(item for item in catalog.networks if item.chain_id == chain_id)
        url = _execution_rpc_urls(catalog)[chain_id]
        transport = Transport(interval=0)
        try:
            balance = quantity(RpcReader(transport).call(url, "eth_getBalance", [wallet, "latest"]))
        finally:
            transport.close()
        return network.native_symbol, balance
    except (RequestError, OSError, ValueError, StopIteration):
        return None, None


def _size_description(entry: dict[str, Any], result: dict[str, Any] | None = None) -> str:
    if result and isinstance(result.get("received_raw"), str):
        raw = result["received_raw"]
        token = str(result.get("received_token_id", ""))
        if raw.isdecimal() and (
            (entry.get("chain_id") == 56
             and token.lower() == "0xb0d502e938ed5f4df2e681fe6e419ff29631d62b")
            or (entry.get("chain_id") == 1 and token.lower() == "eth")
        ):
            symbol = "STG" if token.lower() != "eth" else "ETH"
            amount = Decimal(raw) / Decimal(10**18)
            return f"{amount} {symbol} (received: {raw} raw units)"
        return f"received {raw} raw units of {token or 'unknown token'}"
    action = entry.get("action") or {}
    params = action.get("str_params") or []
    if (entry.get("protocol_id") == "fuel" and entry.get("chain_id") == 1
            and len(params) == 3 and str(params[2]).isdecimal()):
        raw = str(params[2])
        return f"{Decimal(raw) / Decimal(10**18)} ETH requested ({raw} raw wei)"
    if not params:
        return "amount unavailable"
    safe_params = [
        "[address]" if re.fullmatch(r"0x[0-9a-fA-F]{40}", str(param)) else str(param)
        for param in params
    ]
    return f"{action.get('func', 'withdraw')} raw parameters: {', '.join(safe_params)}"


def _record_outcome(
    connection: sqlite3.Connection, run_id: str, ordinal: int, entry: dict[str, Any],
    *, status: str, reason: str | None, tx_hash: str | None,
    native_symbol: str | None, native_before_wei: int | None,
    native_after_wei: int | None, now: float,
    result: dict[str, Any] | None = None,
    error_detail: str | None = None,
) -> None:
    event = {
        "event": "withdrawal_outcome", "run_id": run_id, "ordinal": ordinal,
        "protocol": entry.get("protocol_name") or entry.get("protocol_id") or "unknown",
        "size": _size_description(entry, result),
        "estimated_usd": entry.get("net_usd_value"),
        "native_symbol": native_symbol,
        "native_before_wei": str(native_before_wei) if native_before_wei is not None else None,
        "native_after_wei": str(native_after_wei) if native_after_wei is not None else None,
        "status": status, "reason": reason, "error_detail": error_detail,
        "fee_detail": json.dumps(result.get("fee_quote"), sort_keys=True)
        if result and isinstance(result.get("fee_quote"), dict) else None,
        "tx_hash": tx_hash, "at": now,
    }
    connection.execute(
        "INSERT OR IGNORE INTO defi_batch_outcomes "
        "(run_id, ordinal, action_id, protocol_name, chain_id, withdrawal_size, "
        "estimated_usd, native_symbol, native_before_wei, native_after_wei, "
        "status, reason, error_detail, fee_detail, tx_hash, observed_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (run_id, ordinal, entry.get("action_id"), event["protocol"], entry.get("chain_id"),
         event["size"], event["estimated_usd"], native_symbol,
         event["native_before_wei"], event["native_after_wei"], status, reason,
         error_detail, event["fee_detail"], tx_hash, now),
    )
    connection.commit()
    print(json.dumps(event, ensure_ascii=False), flush=True)


@dataclass(frozen=True)
class CommandResult:
    returncode: int
    payload: dict[str, Any] | None
    reason: str = ""
    detail: str | None = None


@dataclass(frozen=True)
class BatchOperation:
    kind: str
    ordinal: int
    workbook_path: Path
    db_path: Path
    wallets_path: Path | None = None
    output_path: Path | None = None
    plan_path: Path | None = None
    plan_sha256: str | None = None
    action_id: str | None = None
    max_gas_wei: int | None = None
    execute: bool = False


def _log_operation_failure(
    operation: BatchOperation, result: CommandResult, *, stdout: str,
    started_at: float, elapsed_ms: int,
) -> None:
    print(json.dumps({
        "event": "operation_failure", "operation": operation.kind,
        "ordinal": operation.ordinal,
        "returncode": result.returncode, "reason": result.reason,
        "detail": result.detail, "stdout": redact_text(stdout),
        "context": redact_data({
            "workbook_path": str(operation.workbook_path),
            "db_path": str(operation.db_path),
            "wallets_path": str(operation.wallets_path) if operation.wallets_path else None,
            "output_path": str(operation.output_path) if operation.output_path else None,
            "plan_path": str(operation.plan_path) if operation.plan_path else None,
            "plan_sha256": operation.plan_sha256, "action_id": operation.action_id,
            "max_gas_wei": operation.max_gas_wei, "execute": operation.execute,
        }), "started_at": started_at,
        "elapsed_ms": elapsed_ms, "at": time.time(),
    }), flush=True)


def _log_batch_anomaly(run_id: str, ordinal: int, reason: str, context: Any) -> str:
    detail = json.dumps(redact_data(context), ensure_ascii=False)
    print(json.dumps({
        "event": "batch_anomaly", "run_id": redact_text(run_id),
        "ordinal": ordinal, "reason": reason, "detail": detail, "at": time.time(),
    }, ensure_ascii=False), flush=True)
    return detail


def _run_operation(operation: BatchOperation) -> CommandResult:
    """Invoke the same operations as the CLI, inside the batch worker process."""

    started_at = time.time()
    started_monotonic = time.monotonic()

    def log_failure(failure: CommandResult, stdout: str = "") -> CommandResult:
        _log_operation_failure(
            operation, failure, stdout=stdout, started_at=started_at,
            elapsed_ms=round((time.monotonic() - started_monotonic) * 1000),
        )
        return failure

    try:
        _load_dotenv()
        if operation.kind == "quote-defi":
            if operation.wallets_path is None or operation.output_path is None:
                raise ValueError("incomplete quote operation")
            payload = quote_defi(
                wallets_path=operation.wallets_path,
                workbook_path=operation.workbook_path,
                db_path=operation.db_path, output_path=operation.output_path,
            )
        elif operation.kind == "execute-defi":
            if (operation.plan_path is None or operation.plan_sha256 is None
                    or operation.action_id is None or operation.max_gas_wei is None):
                raise ValueError("incomplete execution operation")
            catalog = load_catalog(None)
            payload = execute_defi(
                plan_path=operation.plan_path, plan_sha256=operation.plan_sha256,
                action_id=operation.action_id, workbook_path=operation.workbook_path,
                db_path=operation.db_path, max_gas_wei=operation.max_gas_wei,
                execute=operation.execute,
                rpc_urls=_execution_rpc_urls(catalog),
            )
        else:
            raise ValueError("unknown DeFi operation")
        return CommandResult(0, payload)
    except Exception as exc:
        message = str(exc)
        reason = (
            "stale_plan" if message.startswith("DeFi plan is stale") else
            "gas_estimate_rpc_error" if re.fullmatch(r"estimate_gas_[a-zA-Z0-9_]+", message)
            else "ethereum_gas_deferred" if message.startswith("ethereum_gas_deferred:")
            else type(exc).__name__
        )
        returncode = (
            4 if isinstance(exc, (httpx.HTTPError, RequestError, AmbiguousBroadcast,
                                  sqlite3.OperationalError)) else
            2 if isinstance(exc, (ConfigError, ValueError, OSError)) else 1
        )
        detail = json.dumps(exception_diagnostic(exc), ensure_ascii=False)
        return log_failure(CommandResult(returncode, None, reason, detail))


def _execute_operation(
    run: sqlite3.Row, action: sqlite3.Row | dict, *, ordinal: int, execute: bool,
) -> BatchOperation:
    return BatchOperation(
        kind="execute-defi", ordinal=ordinal,
        plan_path=Path(action["plan_path"]),
        plan_sha256=action["plan_sha256"], action_id=action["action_id"],
        workbook_path=Path(run["workbook_path"]), db_path=Path(run["db_path"]),
        max_gas_wei=int(run["gas_cap_wei"]), execute=execute,
    )


def _set_action(
    connection: sqlite3.Connection, run_id: str, ordinal: int, action: dict,
    *, status: str, now: float, tx_hash: str | None = None, reason: str | None = None,
) -> None:
    connection.execute(
        "INSERT INTO defi_batch_actions "
        "(run_id, ordinal, action_id, plan_sha256, plan_path, status, tx_hash, reason, updated_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?) "
        "ON CONFLICT(run_id, ordinal, action_id) DO UPDATE SET "
        "plan_sha256=excluded.plan_sha256, plan_path=excluded.plan_path, "
        "status=excluded.status, tx_hash=excluded.tx_hash, "
        "reason=excluded.reason, updated_at=excluded.updated_at",
        (run_id, ordinal, action["action_id"], action["plan_sha256"], action["plan_path"],
         status, tx_hash, reason, now),
    )
    connection.commit()


def _retry_wallet(
    connection: sqlite3.Connection, run_id: str, ordinal: int, *,
    now: float, reason: str, unresolved_action: bool = False,
) -> str:
    connection.execute(
        "UPDATE defi_batch_wallets SET attempts=attempts+1, last_error=?, due_at=? "
        "WHERE run_id=? AND ordinal=?", (reason, now + 1800, run_id, ordinal),
    )
    connection.commit()
    attempts = connection.execute(
        "SELECT attempts FROM defi_batch_wallets WHERE run_id=? AND ordinal=?",
        (run_id, ordinal),
    ).fetchone()[0]
    if attempts >= 3:
        return "manual_review" if unresolved_action else "failed"
    return "retry"


def _invoke_read_only(
    invoke: Callable[[BatchOperation], CommandResult], operation: BatchOperation,
) -> CommandResult:
    """Retry read-only API and RPC calls promptly, without transaction pacing."""

    for attempt in range(3):
        result = invoke(operation)
        if result.returncode != 4:
            return result
        if attempt < 2:
            time.sleep(attempt + 1)
    return result


def _invoke_execute(
    invoke: Callable[[BatchOperation], CommandResult], operation: BatchOperation,
) -> CommandResult:
    """Only pre-broadcast gas-estimate failures are safe to retry immediately."""

    for attempt in range(3):
        result = invoke(operation)
        if result.reason != "gas_estimate_rpc_error" or attempt == 2:
            return result
        time.sleep(attempt + 1)
    return result


def _journal_tx_hash(connection: sqlite3.Connection, wallet: str, action_id: str) -> str | None:
    try:
        row = connection.execute(
            "SELECT s.tx_hash FROM route_positions p JOIN route_steps s ON s.position_id=p.id "
            "WHERE p.position_key=? AND s.tx_hash IS NOT NULL ORDER BY s.id DESC LIMIT 1",
            (f"defi:{wallet}:{action_id}",),
        ).fetchone()
    except sqlite3.OperationalError:
        return None
    return row[0] if row else None


def _journal_has_step(connection: sqlite3.Connection, wallet: str, action_id: str) -> bool:
    try:
        row = connection.execute(
            "SELECT 1 FROM route_positions p JOIN route_steps s ON s.position_id=p.id "
            "WHERE p.position_key=? LIMIT 1",
            (f"defi:{wallet}:{action_id}",),
        ).fetchone()
    except sqlite3.OperationalError:
        return False
    return row is not None


def process_wallet(
    db_path: Path,
    run_id: str,
    ordinal: int,
    *,
    invoke: Callable[[BatchOperation], CommandResult] = _run_operation,
    native_balance: Callable[[int, str], tuple[str | None, int | None]] = _native_snapshot,
    now: float | None = None,
) -> str:
    """Process one wallet using fresh Rabby data in the worker process."""

    now = time.time() if now is None else now
    with _connect(db_path) as connection:
        run = connection.execute(
            "SELECT * FROM defi_batch_runs WHERE run_id=?", (run_id,)
        ).fetchone()
        wallet = connection.execute(
            "SELECT * FROM defi_batch_wallets WHERE run_id=? AND ordinal=?",
            (run_id, ordinal),
        ).fetchone()
        if run is None or wallet is None or wallet["status"] not in {"queued", "active"}:
            raise ValueError("wallet is not queued")
        connection.execute(
            "UPDATE defi_batch_wallets SET status='active', started_at=COALESCE(started_at, ?) "
            "WHERE run_id=? AND ordinal=?", (now, run_id, ordinal),
        )
        connection.commit()

        _ensure_tables(connection)
        unresolved = connection.execute(
            "SELECT * FROM defi_batch_actions WHERE run_id=? AND ordinal=? "
            "AND status='executing' ORDER BY updated_at LIMIT 1", (run_id, ordinal),
        ).fetchone()
        if unresolved is not None:
            recovered = invoke(_execute_operation(run, unresolved, ordinal=ordinal, execute=True))
            if recovered.returncode == 0 and recovered.payload:
                status = recovered.payload.get("status")
                if status not in {"withdrawn", "manual_review"}:
                    _log_batch_anomaly(
                        run_id, ordinal, "invalid_recovery_result", recovered.payload,
                    )
                    return "manual_review"
                tx_hash = recovered.payload.get("tx_hash")
                if isinstance(tx_hash, str):
                    record_transaction(connection, run, ordinal, unresolved["action_id"],
                                       tx_hash, now=time.time())
                _set_action(
                    connection, run_id, ordinal, dict(unresolved), status=status, now=now,
                    tx_hash=tx_hash,
                    reason=recovered.payload.get("reason"),
                )
                old_plan = json.loads(Path(unresolved["plan_path"]).read_bytes())
                entry = next(
                    (item for item in old_plan["entries"]
                     if item.get("action_id") == unresolved["action_id"]), None,
                )
                if entry:
                    symbol, after = native_balance(entry["chain_id"], wallet["wallet"])
                    baseline = connection.execute(
                        "SELECT native_symbol, native_before_wei FROM defi_batch_baselines "
                        "WHERE run_id=? AND ordinal=? AND action_id=?",
                        (run_id, ordinal, unresolved["action_id"]),
                    ).fetchone()
                    _record_outcome(connection, run_id, ordinal, entry, status=status,
                                    reason=recovered.payload.get("reason"), tx_hash=tx_hash,
                                    native_symbol=(baseline["native_symbol"] if baseline else None)
                                    or symbol,
                                    native_before_wei=(int(baseline["native_before_wei"])
                                                       if baseline and baseline["native_before_wei"]
                                                       else None),
                                    native_after_wei=after, now=time.time(),
                                    result=recovered.payload)
            elif recovered.reason == "stale_plan":
                _set_action(connection, run_id, ordinal, dict(unresolved),
                            status="stale", now=now, reason="stale_plan")
            elif recovered.returncode == 4:
                return _retry_wallet(connection, run_id, ordinal, now=now,
                                     reason=recovered.reason, unresolved_action=True)
            else:
                _log_batch_anomaly(run_id, ordinal, "recovery_failed", {
                    "reason": recovered.reason, "detail": recovered.detail,
                    "returncode": recovered.returncode,
                })
                return "manual_review"

        output_dir = Path(run["output_dir"])
        output_dir.mkdir(parents=True, exist_ok=True)
        wallet_file = output_dir / f"wallet-{ordinal}.txt"
        wallet_file.write_text(wallet["wallet"] + "\n", encoding="utf-8")
        plan_path = output_dir / f"plan-{ordinal}-{time.time_ns()}-{secrets.token_hex(3)}.json"
        quote = _invoke_read_only(invoke, BatchOperation(
            kind="quote-defi", ordinal=ordinal, wallets_path=wallet_file,
            workbook_path=Path(run["workbook_path"]), db_path=Path(run["db_path"]),
            output_path=plan_path,
        ))
        if quote.returncode:
            connection.execute(
                "UPDATE defi_batch_wallets SET last_error=? WHERE run_id=? AND ordinal=?",
                (quote.reason, run_id, ordinal),
            )
            connection.commit()
            seed = connection.execute(
                "SELECT plan_json FROM defi_plans WHERE plan_sha256=?",
                (run["seed_plan_sha256"],),
            ).fetchone()
            if seed:
                for entry in json.loads(seed[0]).get("entries", []):
                    if entry.get("wallet") != wallet["wallet"] or entry.get("status") != "ready":
                        continue
                    symbol, balance = native_balance(entry["chain_id"], wallet["wallet"])
                    _record_outcome(
                        connection, run_id, ordinal, entry, status="quote_failed",
                        reason=quote.reason, tx_hash=None, native_symbol=symbol,
                        native_before_wei=balance, native_after_wei=balance,
                        now=time.time(), error_detail=quote.detail,
                    )
            return "failed"
        if not quote.payload or not isinstance(quote.payload.get("plan_sha256"), str):
            _log_batch_anomaly(run_id, ordinal, "invalid_quote_result", quote.payload)
            return "failed"
        plan_bytes = plan_path.read_bytes()
        digest = hashlib.sha256(plan_bytes).hexdigest()
        if digest != quote.payload["plan_sha256"]:
            _log_batch_anomaly(run_id, ordinal, "quote_digest_mismatch", {
                "calculated": digest, "reported": quote.payload["plan_sha256"],
                "plan_path": str(plan_path),
            })
            return "manual_review"
        plan = json.loads(plan_bytes)
        ready = [
            entry for entry in plan.get("entries", [])
            if entry.get("status") == "ready" and entry.get("wallet") == wallet["wallet"]
        ]
        connection.execute(
            "UPDATE defi_batch_wallets SET ready_observed=?, attempts=0, last_error=NULL "
            "WHERE run_id=? AND ordinal=?", (len(ready), run_id, ordinal),
        )
        connection.commit()
        if not ready:
            return "no_ready"
        for entry in ready:
            action_id = entry.get("action_id")
            if not isinstance(action_id, str):
                continue
            previous = connection.execute(
                "SELECT status FROM defi_batch_actions "
                "WHERE run_id=? AND ordinal=? AND action_id=?",
                (run_id, ordinal, action_id),
            ).fetchone()
            if previous and previous["status"] in {"withdrawn", "manual_review", "skipped"}:
                continue
            action = {"action_id": action_id, "plan_sha256": digest,
                      "plan_path": str(plan_path)}
            symbol, before = native_balance(entry["chain_id"], wallet["wallet"])
            preview = _invoke_read_only(
                invoke, _execute_operation(run, action, ordinal=ordinal, execute=False),
            )
            if preview.returncode:
                _set_action(connection, run_id, ordinal, action, status="skipped",
                            now=now, reason=preview.reason)
                after_symbol, after = native_balance(entry["chain_id"], wallet["wallet"])
                _record_outcome(connection, run_id, ordinal, entry, status="skipped",
                                reason=preview.reason, tx_hash=None,
                                native_symbol=symbol or after_symbol, native_before_wei=before,
                                native_after_wei=after, now=time.time(),
                                error_detail=preview.detail)
                continue
            if not preview.payload or preview.payload.get("status") != "preview":
                detail = _log_batch_anomaly(
                    run_id, ordinal, "invalid_preview", preview.payload,
                )
                _set_action(connection, run_id, ordinal, action, status="manual_review",
                            now=now, reason="invalid_preview")
                after_symbol, after = native_balance(entry["chain_id"], wallet["wallet"])
                _record_outcome(connection, run_id, ordinal, entry, status="manual_review",
                                reason="invalid_preview", tx_hash=None,
                                native_symbol=symbol or after_symbol,
                                native_before_wei=before, native_after_wei=after,
                                now=time.time(), error_detail=detail)
                return "manual_review"
            due = _next_transaction_due(connection, run_id, ordinal)
            if due is not None and time.time() < due:
                connection.execute(
                    "UPDATE defi_batch_wallets SET due_at=? WHERE run_id=? AND ordinal=?",
                    (due, run_id, ordinal),
                )
                connection.commit()
                return "deferred"
            connection.execute(
                "INSERT OR REPLACE INTO defi_batch_baselines VALUES (?, ?, ?, ?, ?)",
                (run_id, ordinal, action_id, symbol,
                 str(before) if before is not None else None),
            )
            connection.commit()
            _set_action(connection, run_id, ordinal, action, status="executing", now=now)
            execute_operation = _execute_operation(run, action, ordinal=ordinal, execute=True)
            result = _invoke_execute(invoke, execute_operation)
            for attempt in range(2):
                if (result.returncode != 4 or result.reason == "gas_estimate_rpc_error"
                        or _journal_has_step(connection, wallet["wallet"], action_id)):
                    break
                time.sleep(attempt + 1)
                result = _invoke_execute(invoke, execute_operation)
            if result.returncode:
                if result.reason == "gas_estimate_rpc_error":
                    _set_action(connection, run_id, ordinal, action, status="skipped",
                                now=now, reason=result.reason)
                    after_symbol, after = native_balance(entry["chain_id"], wallet["wallet"])
                    _record_outcome(connection, run_id, ordinal, entry, status="skipped",
                                    reason=result.reason, tx_hash=None,
                                    native_symbol=symbol or after_symbol, native_before_wei=before,
                                    native_after_wei=after, now=time.time(),
                                    error_detail=result.detail)
                    continue
                if result.returncode == 4:
                    tx_hash = _journal_tx_hash(connection, wallet["wallet"], action_id)
                    if tx_hash:
                        record_transaction(connection, run, ordinal, action_id,
                                           tx_hash, now=time.time())
                        return _retry_wallet(connection, run_id, ordinal, now=now,
                                             reason=result.reason, unresolved_action=True)
                    status = ("manual_review" if _journal_has_step(
                        connection, wallet["wallet"], action_id) else "skipped")
                    reason = ("unresolved_intent" if status == "manual_review"
                              else result.reason)
                    _set_action(connection, run_id, ordinal, action, status=status,
                                now=now, reason=reason)
                    after_symbol, after = native_balance(entry["chain_id"], wallet["wallet"])
                    _record_outcome(connection, run_id, ordinal, entry, status=status,
                                    reason=reason, tx_hash=None,
                                    native_symbol=symbol or after_symbol,
                                    native_before_wei=before, native_after_wei=after,
                                    now=time.time(), error_detail=result.detail)
                    if status == "manual_review":
                        return "manual_review"
                    continue
                if result.reason == "stale_plan":
                    _set_action(connection, run_id, ordinal, action,
                                status="skipped", now=now, reason="stale_plan")
                    after_symbol, after = native_balance(entry["chain_id"], wallet["wallet"])
                    _record_outcome(connection, run_id, ordinal, entry, status="skipped",
                                    reason="stale_plan", tx_hash=None,
                                    native_symbol=symbol or after_symbol,
                                    native_before_wei=before, native_after_wei=after,
                                    now=time.time(), error_detail=result.detail)
                    continue
                _set_action(connection, run_id, ordinal, action, status="manual_review",
                            now=now, reason=result.reason)
                after_symbol, after = native_balance(entry["chain_id"], wallet["wallet"])
                _record_outcome(connection, run_id, ordinal, entry, status="manual_review",
                                reason=result.reason, tx_hash=None,
                                native_symbol=symbol or after_symbol,
                                native_before_wei=before, native_after_wei=after,
                                now=time.time(), error_detail=result.detail)
                return "manual_review"
            if not result.payload or result.payload.get("status") not in {
                "withdrawn", "manual_review"
            }:
                detail = _log_batch_anomaly(
                    run_id, ordinal, "invalid_execution_result", result.payload,
                )
                _set_action(connection, run_id, ordinal, action, status="manual_review",
                            now=now, reason="invalid_execution_result")
                after_symbol, after = native_balance(entry["chain_id"], wallet["wallet"])
                _record_outcome(connection, run_id, ordinal, entry, status="manual_review",
                                reason="invalid_execution_result", tx_hash=None,
                                native_symbol=symbol or after_symbol,
                                native_before_wei=before, native_after_wei=after,
                                now=time.time(), error_detail=detail)
                return "manual_review"
            _set_action(
                connection, run_id, ordinal, action, status=result.payload["status"],
                now=now, tx_hash=result.payload.get("tx_hash"),
                reason=result.payload.get("reason"),
            )
            tx_hash = result.payload.get("tx_hash")
            if isinstance(tx_hash, str):
                record_transaction(connection, run, ordinal, action_id, tx_hash, now=time.time())
            after_symbol, after = native_balance(entry["chain_id"], wallet["wallet"])
            _record_outcome(connection, run_id, ordinal, entry,
                            status=result.payload["status"], reason=result.payload.get("reason"),
                            tx_hash=tx_hash, native_symbol=symbol or after_symbol,
                            native_before_wei=before, native_after_wei=after, now=time.time(),
                            result=result.payload)
        return "done"


def batch_status(db_path: Path, run_id: str) -> dict[str, Any]:
    """Return a key-free status snapshot from the shared project database."""

    with _connect(db_path) as connection:
        run = connection.execute(
            "SELECT status, created_at, completed_at FROM defi_batch_runs WHERE run_id=?",
            (run_id,),
        ).fetchone()
        if run is None:
            raise ValueError("unknown DeFi batch run")
        wallet_counts = {
            row["status"]: row["count"] for row in connection.execute(
                "SELECT status, count(*) AS count FROM defi_batch_wallets "
                "WHERE run_id=? GROUP BY status", (run_id,)
            )
        }
        action_counts = {
            row["status"]: row["count"] for row in connection.execute(
                "SELECT status, count(*) AS count FROM defi_batch_actions "
                "WHERE run_id=? GROUP BY status", (run_id,)
            )
        }
        next_wallet = connection.execute(
            "SELECT ordinal, status, due_at, attempts FROM defi_batch_wallets "
            "WHERE run_id=? AND status IN ('queued', 'active') "
            "ORDER BY queue_index LIMIT 1", (run_id,),
        ).fetchone()
        recent = [
            {"ordinal": row["ordinal"], "status": row["status"],
             "completed_at": row["completed_at"]}
            for row in connection.execute(
                "SELECT ordinal, status, completed_at FROM defi_batch_wallets "
                "WHERE run_id=? AND completed_at IS NOT NULL "
                "ORDER BY completed_at DESC LIMIT 5", (run_id,)
            )
        ]
    return {
        "run_id": run_id, "status": run["status"], "created_at": run["created_at"],
        "completed_at": run["completed_at"], "wallets": wallet_counts,
        "actions": action_counts,
        "next_wallet": dict(next_wallet) if next_wallet is not None else None,
        "recent": recent,
    }


def run_worker(db_path: Path, run_id: str) -> int:
    """Run the durable queue; wait only before a subsequent transaction."""

    for stream in (sys.stdout, sys.stderr):
        try:
            if stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
                os.fchmod(stream.fileno(), 0o600)
        except (AttributeError, OSError):
            pass
    db_path.chmod(0o600)
    lock_path = Path(str(db_path) + ".defi-batch.lock")
    with lock_path.open("a+") as lock:
        try:
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            print(json.dumps({"status": "already_running", "run_id": run_id}), flush=True)
            return 0
        with _connect(db_path) as connection:
            _ensure_tables(connection)
            _reconcile_active_wallet_due(connection, run_id)
        while True:
            with _connect(db_path) as connection:
                run = connection.execute(
                    "SELECT status FROM defi_batch_runs WHERE run_id=?", (run_id,)
                ).fetchone()
                if run is None:
                    raise ValueError("unknown DeFi batch run")
                if run["status"] == "completed":
                    print(json.dumps({"status": "completed", "run_id": run_id}), flush=True)
                    return 0
                wallet = connection.execute(
                    "SELECT ordinal, due_at FROM defi_batch_wallets "
                    "WHERE run_id=? AND status IN ('queued', 'active') "
                    "ORDER BY queue_index LIMIT 1", (run_id,),
                ).fetchone()
            if wallet is None or wallet["due_at"] is None:
                raise ValueError("DeFi batch queue is inconsistent")
            remaining = wallet["due_at"] - time.time()
            if remaining > 0:
                time.sleep(min(remaining, 60))
                continue
            ordinal = wallet["ordinal"]
            print(json.dumps({"event": "wallet_started", "ordinal": ordinal,
                              "at": time.time()}), flush=True)
            outcome = process_wallet(db_path, run_id, ordinal)
            if outcome in {"retry", "deferred"}:
                print(json.dumps({"event": "wallet_retry_scheduled", "ordinal": ordinal,
                                  "reason": outcome, "at": time.time()}), flush=True)
                continue
            delay = advance_wallet(db_path, run_id, ordinal, status=outcome)
            print(json.dumps({"event": "wallet_finished", "ordinal": ordinal,
                              "outcome": outcome, "next_delay_seconds": delay,
                              "at": time.time()}), flush=True)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m evm_inventory.defi_batch")
    commands = parser.add_subparsers(dest="command", required=True)
    init = commands.add_parser("init")
    init.add_argument("--db", required=True)
    init.add_argument("--workbook", required=True)
    init.add_argument("--seed-plan", required=True)
    init.add_argument("--run-id", required=True)
    init.add_argument("--output-dir", required=True)
    init.add_argument("--wallet-count", type=int, default=50)
    init.add_argument(
        "--wallet-ordinals",
        help="specific workbook ordinals, e.g. 3,8-10,13-14; overrides --wallet-count",
    )
    init.add_argument("--gas-cap-wei", type=int, default=10**15)
    init.add_argument("--between-wallet-delay-min-seconds", "--delay-min-seconds",
                      dest="delay_min_seconds", type=int, default=1200)
    init.add_argument("--between-wallet-delay-max-seconds", "--delay-max-seconds",
                      dest="delay_max_seconds", type=int, default=6000)
    init.add_argument("--within-wallet-delay-min-seconds", type=int, default=60)
    init.add_argument("--within-wallet-delay-max-seconds", type=int, default=300)
    run = commands.add_parser("run")
    run.add_argument("--db", required=True)
    run.add_argument("--run-id", required=True)
    status = commands.add_parser("status")
    status.add_argument("--db", required=True)
    status.add_argument("--run-id", required=True)
    args = parser.parse_args(argv)
    try:
        if args.command == "init":
            result = initialize_batch(
                db_path=Path(args.db), workbook_path=Path(args.workbook),
                seed_plan_path=Path(args.seed_plan), run_id=args.run_id,
                wallet_count=args.wallet_count, gas_cap_wei=args.gas_cap_wei,
                wallet_ordinals=args.wallet_ordinals,
                delay_min_seconds=args.delay_min_seconds,
                delay_max_seconds=args.delay_max_seconds,
                within_wallet_delay_min_seconds=args.within_wallet_delay_min_seconds,
                within_wallet_delay_max_seconds=args.within_wallet_delay_max_seconds,
                output_dir=Path(args.output_dir),
            )
            print(json.dumps(result))
            return 0
        if args.command == "status":
            print(json.dumps(batch_status(Path(args.db), args.run_id)))
            return 0
        return run_worker(Path(args.db), args.run_id)
    except Exception as exc:
        print(json.dumps({
            "status": "error", "error_type": type(exc).__name__,
            "run_id": redact_text(args.run_id),
            "diagnostic": exception_diagnostic(exc),
        }, ensure_ascii=False), file=sys.stderr, flush=True)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
