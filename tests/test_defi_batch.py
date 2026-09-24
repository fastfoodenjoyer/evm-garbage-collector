import hashlib
import json
import random
import sqlite3
from pathlib import Path

import httpx
import pytest
from openpyxl import load_workbook

from evm_inventory.defi_batch import (
    BatchOperation,
    CommandResult,
    _ensure_tables,
    _next_transaction_due,
    _reconcile_active_wallet_due,
    _run_operation,
    _size_description,
    advance_wallet,
    initialize_batch,
    process_wallet,
    record_transaction,
)
from evm_inventory.store import Store
from evm_inventory.workbook import create_wallet_template


def _inputs(tmp_path):
    workbook_path = tmp_path / "wallets.xlsx"
    create_wallet_template(workbook_path)
    workbook = load_workbook(workbook_path)
    for ordinal in range(1, 4):
        workbook["Wallets"].append([
            ordinal, f"0x{ordinal:040x}", "0x" + "a" * 64, "", "",
            f"http://user:secret-{ordinal}@proxy-{ordinal}.example:8080",
        ])
    workbook.save(workbook_path)
    plan = {
        "schema": "rabby-defi-withdraw-v1", "created_at": 1,
        "entries": [
            {"wallet": f"0x{ordinal:040x}", "status": "ready", "action_id": f"{ordinal:064x}"}
            for ordinal in (1, 3)
        ],
        "summary": {"ready": 2, "manual_review": 0},
    }
    plan_path = tmp_path / "seed.json"
    plan_bytes = (json.dumps(plan) + "\n").encode()
    plan_path.write_bytes(plan_bytes)
    db_path = tmp_path / "inventory.sqlite"
    with Store(db_path) as store:
        store.save_defi_plan(hashlib.sha256(plan_bytes).hexdigest(), plan_bytes)
    return workbook_path, plan_path, db_path


def test_initialize_batch_shuffles_only_ready_wallets_without_persisting_secrets(tmp_path):
    workbook, plan, db = _inputs(tmp_path)

    initialize_batch(
        db_path=db, workbook_path=workbook, seed_plan_path=plan,
        run_id="first-three", wallet_count=3, gas_cap_wei=10**15,
        delay_min_seconds=1200, delay_max_seconds=6000,
        output_dir=tmp_path / "batch", rng=random.Random(7), now=1000,
    )

    with sqlite3.connect(db) as conn:
        rows = conn.execute(
            "SELECT ordinal, status, queue_index, due_at FROM defi_batch_wallets "
            "WHERE run_id='first-three' ORDER BY ordinal"
        ).fetchall()
    assert [(r[0], r[1]) for r in rows] == [(1, "queued"), (2, "no_ready"), (3, "queued")]
    assert sorted(r[2] for r in rows if r[2] is not None) == [0, 1]
    assert sum(r[3] == 1000 for r in rows) == 1
    assert b"secret-" not in db.read_bytes()
    assert b"0xaaaaaaaa" not in db.read_bytes()


def test_advance_wallet_does_not_delay_next_wallet_without_transaction(tmp_path):
    workbook, plan, db = _inputs(tmp_path)
    initialize_batch(
        db_path=db, workbook_path=workbook, seed_plan_path=plan,
        run_id="first-three", wallet_count=3, gas_cap_wei=10**15,
        delay_min_seconds=1200, delay_max_seconds=6000,
        output_dir=tmp_path / "batch", rng=random.Random(7), now=1000,
    )
    with sqlite3.connect(db) as conn:
        first = conn.execute(
            "SELECT ordinal FROM defi_batch_wallets WHERE run_id='first-three' AND queue_index=0"
        ).fetchone()[0]
    delay = advance_wallet(db, "first-three", first, status="done", now=2000,
                           rng=random.Random(11))

    assert delay == 0
    with sqlite3.connect(db) as conn:
        next_due = conn.execute(
            "SELECT due_at FROM defi_batch_wallets WHERE run_id='first-three' AND queue_index=1"
        ).fetchone()[0]
        finished = conn.execute(
            "SELECT status FROM defi_batch_wallets WHERE run_id='first-three' AND ordinal=?",
            (first,),
        ).fetchone()[0]
    assert next_due == 2000
    assert finished == "done"


def test_transaction_delay_is_persisted_and_idempotent(tmp_path):
    workbook, plan, db = _inputs(tmp_path)
    initialize_batch(
        db_path=db, workbook_path=workbook, seed_plan_path=plan,
        run_id="first-three", wallet_count=3, output_dir=tmp_path / "batch", now=1000,
    )
    with sqlite3.connect(db) as raw:
        raw.row_factory = sqlite3.Row
        run = raw.execute("SELECT * FROM defi_batch_runs WHERE run_id='first-three'").fetchone()
        due = record_transaction(raw, run, 1, "a" * 64, "0x" + "b" * 64,
                                 now=2000, rng=random.Random(11))
        repeated = record_transaction(raw, run, 1, "a" * 64, "0x" + "b" * 64,
                                      now=3000, rng=random.Random(12))
        count = raw.execute("SELECT count(*) FROM defi_batch_transactions").fetchone()[0]
        same_wallet_due = _next_transaction_due(raw, "first-three", 1)
        other_wallet_due = _next_transaction_due(raw, "first-three", 3)
    assert 3200 <= due <= 8000
    assert 2060 <= same_wallet_due <= 2300
    assert other_wallet_due == due
    assert repeated == due
    assert count == 1


def test_custom_within_wallet_range_and_existing_wait_reconciliation(tmp_path):
    workbook, plan, db = _inputs(tmp_path)
    initialize_batch(
        db_path=db, workbook_path=workbook, seed_plan_path=plan,
        run_id="first-three", wallet_count=3, output_dir=tmp_path / "batch",
        within_wallet_delay_min_seconds=90, within_wallet_delay_max_seconds=90,
        now=1000,
    )
    with sqlite3.connect(db) as raw:
        raw.row_factory = sqlite3.Row
        run = raw.execute("SELECT * FROM defi_batch_runs WHERE run_id='first-three'").fetchone()
        first = raw.execute(
            "SELECT ordinal FROM defi_batch_wallets WHERE run_id='first-three' AND queue_index=0"
        ).fetchone()[0]
        between_due = record_transaction(raw, run, first, "a" * 64, "0x" + "b" * 64,
                                         now=2000, rng=random.Random(11))
        raw.execute(
            "UPDATE defi_batch_wallets SET status='active', due_at=? "
            "WHERE run_id='first-three' AND ordinal=?", (between_due, first),
        )
        _reconcile_active_wallet_due(raw, "first-three")
        active_due = raw.execute(
            "SELECT due_at FROM defi_batch_wallets WHERE run_id='first-three' AND ordinal=?",
            (first,),
        ).fetchone()[0]
    assert active_due == 2090
    assert between_due >= 3200


def test_old_batch_tables_gain_within_wallet_deadline(tmp_path):
    db = tmp_path / "old.sqlite"
    with sqlite3.connect(db) as raw:
        raw.executescript("""
            CREATE TABLE defi_batch_runs (
                run_id TEXT PRIMARY KEY, workbook_path TEXT NOT NULL,
                db_path TEXT NOT NULL, output_dir TEXT NOT NULL,
                seed_plan_sha256 TEXT NOT NULL, gas_cap_wei TEXT NOT NULL,
                delay_min_seconds INTEGER NOT NULL, delay_max_seconds INTEGER NOT NULL,
                status TEXT NOT NULL, created_at REAL NOT NULL, completed_at REAL
            );
            CREATE TABLE defi_batch_transactions (
                tx_hash TEXT PRIMARY KEY, run_id TEXT NOT NULL, ordinal INTEGER NOT NULL,
                action_id TEXT NOT NULL, observed_at REAL NOT NULL,
                next_not_before REAL NOT NULL
            );
            CREATE TABLE defi_batch_outcomes (id INTEGER PRIMARY KEY, status TEXT);
        """)
        raw.execute("INSERT INTO defi_batch_runs VALUES (?, '', '', '', '', '', 1200, "
                    "6000, 'running', 0, NULL)", ("old-run",))
        raw.execute("INSERT INTO defi_batch_transactions VALUES (?, ?, 15, ?, 2000, 5000)",
                    ("0x" + "b" * 64, "old-run", "a" * 64))
        raw.row_factory = sqlite3.Row
        _ensure_tables(raw)
        run = raw.execute("SELECT * FROM defi_batch_runs WHERE run_id='old-run'").fetchone()
        same_due = _next_transaction_due(raw, "old-run", 15)
        between_due = _next_transaction_due(raw, "old-run", 16)
        outcome_columns = {row[1] for row in raw.execute("PRAGMA table_info(defi_batch_outcomes)")}
    assert (run["within_wallet_delay_min_seconds"],
            run["within_wallet_delay_max_seconds"]) == (60, 300)
    assert 2060 <= same_due <= 2300
    assert between_due == 5000
    assert "error_detail" in outcome_columns


def test_cli_failure_logs_full_stderr_without_proxy_credentials(monkeypatch, capsys):
    detail = "ethereum_gas_deferred:source=rpc;value_wei=851018418;threshold_wei=500000000"
    secret = "socks5://user:pass@proxy.example:1080"
    def fail(**kwargs):
        raise ValueError(f"{detail};proxy={secret}")
    monkeypatch.setattr("evm_inventory.defi_batch.execute_defi", fail)
    monkeypatch.setattr("evm_inventory.defi_batch.load_catalog", lambda _path: None)
    monkeypatch.setattr("evm_inventory.defi_batch._execution_rpc_urls", lambda _catalog: {})

    result = _run_operation(BatchOperation(
        kind="execute-defi", ordinal=1, plan_path=Path("plan-1.json"),
        plan_sha256="abc", action_id="def", workbook_path=Path("wallets.xlsx"),
        db_path=Path("db.sqlite"), max_gas_wei=1,
    ))
    output = capsys.readouterr().out

    assert result.reason == "ethereum_gas_deferred"
    assert detail in result.detail
    assert "[REDACTED_URL]" in result.detail
    assert json.loads(output)["detail"] == result.detail
    assert secret not in output


def test_cli_unknown_failure_keeps_message_without_credentials(monkeypatch, capsys):
    secret = "http://estimate_gas_secret_token@proxy.example:8080"
    def fail(**kwargs):
        raise RuntimeError(f"unexpected failure at {secret}")
    monkeypatch.setattr("evm_inventory.defi_batch.quote_defi", fail)

    result = _run_operation(BatchOperation(
        kind="quote-defi", ordinal=1, wallets_path=Path("wallet-1.txt"),
        workbook_path=Path("wallets.xlsx"), db_path=Path("db.sqlite"),
        output_path=Path("plan-1.json"),
    ))
    output = capsys.readouterr().out

    assert result.reason == "RuntimeError"
    assert "unexpected failure at [REDACTED_URL]" in result.detail
    event = json.loads(output)
    assert event["stdout"] == ""
    assert event["ordinal"] == 1
    assert event["operation"] == "quote-defi"
    assert event["context"]["wallets_path"] == "wallet-1.txt"
    assert event["elapsed_ms"] >= 0
    assert secret not in output


def test_cli_external_api_failure_keeps_safe_error_type(monkeypatch, capsys):
    def fail(**kwargs):
        raise httpx.ConnectTimeout("provider did not answer")
    monkeypatch.setattr("evm_inventory.defi_batch.quote_defi", fail)

    result = _run_operation(BatchOperation(
        kind="quote-defi", ordinal=1, wallets_path=Path("wallet-1.txt"),
        workbook_path=Path("wallets.xlsx"), db_path=Path("db.sqlite"),
        output_path=Path("plan-1.json"),
    ))

    assert result.reason == "ConnectTimeout"
    assert "provider did not answer" in result.detail
    assert json.loads(capsys.readouterr().out)["detail"] == result.detail


def test_rabby_429_is_classified_as_rate_limit_after_client_retries(monkeypatch, capsys):
    request = httpx.Request("GET", "https://api.rabby.io/v1/chain/list")
    response = httpx.Response(429, request=request, json={"message": "too many requests"})

    def fail(**kwargs):
        raise httpx.HTTPStatusError("too many requests", request=request, response=response)

    monkeypatch.setattr("evm_inventory.defi_batch.quote_defi", fail)
    result = _run_operation(BatchOperation(
        kind="quote-defi", ordinal=3, wallets_path=Path("wallet-3.txt"),
        workbook_path=Path("wallets.xlsx"), db_path=Path("db.sqlite"),
        output_path=Path("plan-3.json"),
    ))

    assert result.returncode == 4
    assert result.reason == "http_429"
    assert "too many requests" in result.detail
    assert json.loads(capsys.readouterr().out)["reason"] == "http_429"


def test_quote_429_requeues_wallet_without_skipping(tmp_path):
    workbook, plan, db = _inputs(tmp_path)
    initialize_batch(
        db_path=db, workbook_path=workbook, seed_plan_path=plan,
        run_id="first-three", wallet_count=3, output_dir=tmp_path / "batch", now=1000,
    )
    with sqlite3.connect(db) as conn:
        ordinal = conn.execute(
            "SELECT ordinal FROM defi_batch_wallets "
            "WHERE run_id='first-three' AND queue_index=0"
        ).fetchone()[0]
    calls = []

    def invoke(operation):
        calls.append(operation)
        return CommandResult(4, None, "http_429", detail="Rabby HTTP 429 after 4 attempts")

    assert process_wallet(db, "first-three", ordinal, invoke=invoke, now=1000) == "retry"
    assert len(calls) == 1
    with sqlite3.connect(db) as conn:
        wallet = conn.execute(
            "SELECT status,attempts,due_at,last_error FROM defi_batch_wallets "
            "WHERE run_id='first-three' AND ordinal=?", (ordinal,),
        ).fetchone()
        action_count = conn.execute("SELECT count(*) FROM defi_batch_actions").fetchone()[0]
    assert wallet == ("active", 1, 2800, "http_429")
    assert action_count == 0


def test_verified_outputs_report_each_token_without_protocol_rules():
    entry = {"action": {"str_params": []}}
    result = {
        "received_assets": [
            {"token_id": "0x" + "1" * 40, "raw": "15", "amount": "0.15", "symbol": "AAA"},
            {"token_id": "0x" + "2" * 40, "raw": "20"},
        ],
    }
    assert _size_description(entry, result) == (
        "received 0.15 AAA (15 raw units of 0x" + "1" * 40 + ")"
        + ", 20 raw units of 0x" + "2" * 40
    )


def test_requested_size_omits_wallet_address():
    entry = {
        "protocol_id": "fuel", "chain_id": 1,
        "action": {"func": "withdraw(address,address,uint240)()", "str_params": [
            "0x" + "0" * 40, "0x" + "a" * 40, "1000000000000000",
        ]},
    }
    assert _size_description(entry) == (
        "withdraw(address,address,uint240)() raw parameters: "
        "[address], [address], 1000000000000000"
    )


def test_process_wallet_quotes_previews_then_executes_and_records_result(tmp_path):
    workbook, plan, db = _inputs(tmp_path)
    initialize_batch(
        db_path=db, workbook_path=workbook, seed_plan_path=plan,
        run_id="first-three", wallet_count=3, gas_cap_wei=10**15,
        delay_min_seconds=1200, delay_max_seconds=6000,
        output_dir=tmp_path / "batch", rng=random.Random(7), now=1000,
    )
    with sqlite3.connect(db) as conn:
        ordinal, wallet = conn.execute(
            "SELECT ordinal, wallet FROM defi_batch_wallets "
            "WHERE run_id='first-three' AND queue_index=0"
        ).fetchone()
    calls = []
    action_id = "a" * 64

    def invoke(operation):
        calls.append(operation)
        if operation.kind == "quote-defi":
            path = operation.output_path
            payload = {
                "schema": "rabby-defi-withdraw-v1", "created_at": 1000,
                "entries": [{"wallet": wallet, "status": "ready", "action_id": action_id,
                             "chain_id": 1, "protocol_name": "Test Pool",
                             "action": {"func": "redeem(uint256)()", "str_params": ["42"]},
                             "net_usd_value": 1.5}],
                "summary": {"ready": 1, "manual_review": 0},
            }
            data = (json.dumps(payload) + "\n").encode()
            Path(path).write_bytes(data)
            return CommandResult(0, {"plan_sha256": hashlib.sha256(data).hexdigest()})
        if operation.execute:
            return CommandResult(0, {"status": "withdrawn", "tx_hash": "0x" + "b" * 64})
        return CommandResult(0, {"status": "preview"})

    result = process_wallet(db, "first-three", ordinal, invoke=invoke,
                            native_balance=lambda _chain, _wallet: ("ETH", 100), now=1000)

    assert result == "done"
    assert len(calls) == 3
    assert not calls[1].execute
    assert calls[2].execute
    with sqlite3.connect(db) as conn:
        status, tx_hash = conn.execute(
            "SELECT status, tx_hash FROM defi_batch_actions WHERE run_id='first-three'"
        ).fetchone()
    assert status == "withdrawn"
    assert tx_hash == "0x" + "b" * 64
    with sqlite3.connect(db) as conn:
        event = conn.execute(
            "SELECT protocol_name, withdrawal_size, native_before_wei, native_after_wei "
            "FROM defi_batch_outcomes"
        ).fetchone()
    assert event == ("Test Pool", "redeem(uint256)() raw parameters: 42", "100", "100")


def test_second_action_of_same_wallet_uses_short_delay(tmp_path):
    workbook, plan, db = _inputs(tmp_path)
    initialize_batch(
        db_path=db, workbook_path=workbook, seed_plan_path=plan,
        run_id="first-three", wallet_count=3, output_dir=tmp_path / "batch", now=1000,
    )
    with sqlite3.connect(db) as conn:
        ordinal, wallet = conn.execute(
            "SELECT ordinal, wallet FROM defi_batch_wallets "
            "WHERE run_id='first-three' AND queue_index=0"
        ).fetchone()
    execute_calls = []

    def invoke(operation):
        if operation.kind == "quote-defi":
            path = operation.output_path
            payload = {"schema": "rabby-defi-withdraw-v1", "entries": [
                {"wallet": wallet, "status": "ready", "action_id": letter * 64,
                 "chain_id": 1, "protocol_name": "Test Pool",
                 "action": {"func": "redeem(uint256)()", "str_params": ["42"]}}
                for letter in ("a", "c")
            ]}
            data = (json.dumps(payload) + "\n").encode()
            path.write_bytes(data)
            return CommandResult(0, {"plan_sha256": hashlib.sha256(data).hexdigest()})
        if operation.execute:
            execute_calls.append(operation)
            return CommandResult(0, {"status": "withdrawn", "tx_hash": "0x" + "b" * 64})
        return CommandResult(0, {"status": "preview"})

    assert process_wallet(db, "first-three", ordinal, invoke=invoke,
                          native_balance=lambda _chain, _wallet: ("ETH", 100),
                          now=1000) == "deferred"
    assert len(execute_calls) == 1
    with sqlite3.connect(db) as conn:
        observed, between_due, within_due = conn.execute(
            "SELECT observed_at,next_not_before,same_wallet_not_before "
            "FROM defi_batch_transactions"
        ).fetchone()
        active_due = conn.execute(
            "SELECT due_at FROM defi_batch_wallets WHERE run_id='first-three' AND ordinal=?",
            (ordinal,),
        ).fetchone()[0]
    assert 60 <= within_due - observed <= 300
    assert 1200 <= between_due - observed <= 6000
    assert active_due == within_due


def test_process_wallet_does_not_sign_when_preview_fails(tmp_path):
    workbook, plan, db = _inputs(tmp_path)
    initialize_batch(
        db_path=db, workbook_path=workbook, seed_plan_path=plan,
        run_id="first-three", wallet_count=3, gas_cap_wei=10**15,
        delay_min_seconds=1200, delay_max_seconds=6000,
        output_dir=tmp_path / "batch", rng=random.Random(7), now=1000,
    )
    with sqlite3.connect(db) as conn:
        ordinal, wallet = conn.execute(
            "SELECT ordinal, wallet FROM defi_batch_wallets "
            "WHERE run_id='first-three' AND queue_index=0"
        ).fetchone()
    calls = []

    def invoke(operation):
        calls.append(operation)
        if operation.kind == "quote-defi":
            path = operation.output_path
            payload = {
                "schema": "rabby-defi-withdraw-v1", "created_at": 1000,
                "entries": [{"wallet": wallet, "status": "ready", "action_id": "a" * 64,
                             "chain_id": 1, "protocol_name": "Test Pool",
                             "action": {"func": "redeem(uint256)()", "str_params": ["42"]}}],
            }
            data = (json.dumps(payload) + "\n").encode()
            Path(path).write_bytes(data)
            return CommandResult(0, {"plan_sha256": hashlib.sha256(data).hexdigest()})
        return CommandResult(2, None, "gas_cap_exceeded")

    assert process_wallet(db, "first-three", ordinal, invoke=invoke,
                          native_balance=lambda _chain, _wallet: ("ETH", 100), now=1000) == "done"
    assert len(calls) == 2
    assert all(not operation.execute for operation in calls)
    with sqlite3.connect(db) as conn:
        status = conn.execute(
            "SELECT status FROM defi_batch_actions WHERE run_id='first-three'"
        ).fetchone()[0]
    assert status == "skipped"


@pytest.mark.parametrize("stage", ["preview", "execute"])
def test_exhausted_rabby_429_never_marks_action_skipped(tmp_path, stage):
    workbook, plan, db = _inputs(tmp_path)
    initialize_batch(
        db_path=db, workbook_path=workbook, seed_plan_path=plan,
        run_id="first-three", wallet_count=3, output_dir=tmp_path / "batch", now=1000,
    )
    with sqlite3.connect(db) as conn:
        ordinal, wallet = conn.execute(
            "SELECT ordinal, wallet FROM defi_batch_wallets "
            "WHERE run_id='first-three' AND queue_index=0"
        ).fetchone()
    calls = []

    def invoke(operation):
        calls.append(operation)
        if operation.kind == "quote-defi":
            payload = {"schema": "rabby-defi-withdraw-v1", "entries": [{
                "wallet": wallet, "status": "ready", "action_id": "a" * 64,
                "chain_id": 1, "protocol_name": "Test Pool", "net_usd_value": 2,
                "action": {"func": "redeem(uint256)()", "str_params": ["42"]},
            }]}
            data = (json.dumps(payload) + "\n").encode()
            operation.output_path.write_bytes(data)
            return CommandResult(0, {"plan_sha256": hashlib.sha256(data).hexdigest()})
        if stage == "execute" and not operation.execute:
            return CommandResult(0, {"status": "preview"})
        return CommandResult(4, None, "http_429", detail="Rabby HTTP 429 after 4 attempts")

    assert process_wallet(
        db, "first-three", ordinal, invoke=invoke,
        native_balance=lambda _chain, _wallet: ("ETH", 100), now=1000,
    ) == "manual_review"
    assert len(calls) == (2 if stage == "preview" else 3)
    assert calls[-1].execute == (stage == "execute")
    with sqlite3.connect(db) as conn:
        action = conn.execute("SELECT status,reason FROM defi_batch_actions").fetchone()
        outcome = conn.execute(
            "SELECT status,reason,error_detail FROM defi_batch_outcomes"
        ).fetchone()
    assert action == ("manual_review", "http_429")
    assert outcome == ("manual_review", "http_429", "Rabby HTTP 429 after 4 attempts")


def test_gas_estimate_failure_retries_immediately_and_records_final_failure(tmp_path, monkeypatch):
    workbook, plan, db = _inputs(tmp_path)
    initialize_batch(
        db_path=db, workbook_path=workbook, seed_plan_path=plan,
        run_id="first-three", wallet_count=3, output_dir=tmp_path / "batch", now=1000,
    )
    with sqlite3.connect(db) as conn:
        ordinal, wallet = conn.execute(
            "SELECT ordinal, wallet FROM defi_batch_wallets "
            "WHERE run_id='first-three' AND queue_index=0"
        ).fetchone()
    waits = []
    monkeypatch.setattr("evm_inventory.defi_batch.time.sleep", waits.append)
    calls = []

    def invoke(operation):
        calls.append(operation)
        if operation.kind == "quote-defi":
            path = operation.output_path
            payload = {"schema": "rabby-defi-withdraw-v1", "entries": [{
                "wallet": wallet, "status": "ready", "action_id": "a" * 64,
                "chain_id": 1, "protocol_name": "Test Pool", "net_usd_value": 2,
                "action": {"func": "redeem(uint256)()", "str_params": ["42"]},
            }]}
            data = (json.dumps(payload) + "\n").encode()
            path.write_bytes(data)
            return CommandResult(0, {"plan_sha256": hashlib.sha256(data).hexdigest()})
        return CommandResult(4, None, "gas_estimate_rpc_error",
                             detail="estimate_gas_rpc_timeout")

    assert process_wallet(db, "first-three", ordinal, invoke=invoke,
                          native_balance=lambda _chain, _wallet: ("ETH", 100), now=1000) == "done"
    assert len(calls) == 4
    assert waits == [1, 2]
    assert all(not operation.execute for operation in calls)
    with sqlite3.connect(db) as conn:
        event = conn.execute(
            "SELECT protocol_name, withdrawal_size, native_before_wei, native_after_wei, "
            "reason, error_detail FROM defi_batch_outcomes"
        ).fetchone()
        transactions = conn.execute("SELECT count(*) FROM defi_batch_transactions").fetchone()[0]
    assert event == ("Test Pool", "redeem(uint256)() raw parameters: 42", "100", "100",
                     "gas_estimate_rpc_error", "estimate_gas_rpc_timeout")
    assert transactions == 0


def test_execution_safety_failure_records_protocol_size_and_balances(tmp_path):
    workbook, plan, db = _inputs(tmp_path)
    initialize_batch(
        db_path=db, workbook_path=workbook, seed_plan_path=plan,
        run_id="first-three", wallet_count=3, output_dir=tmp_path / "batch", now=1000,
    )
    with sqlite3.connect(db) as conn:
        ordinal, wallet = conn.execute(
            "SELECT ordinal, wallet FROM defi_batch_wallets "
            "WHERE run_id='first-three' AND queue_index=0"
        ).fetchone()

    def invoke(operation):
        if operation.kind == "quote-defi":
            path = operation.output_path
            payload = {"schema": "rabby-defi-withdraw-v1", "entries": [{
                "wallet": wallet, "status": "ready", "action_id": "a" * 64,
                "chain_id": 1, "protocol_id": "fuel", "protocol_name": "Fuel",
                "net_usd_value": 2.67,
                "action": {"func": "withdraw(address,address,uint240)()",
                           "str_params": ["0x" + "0" * 40, wallet, "1000000000000000"]},
            }]}
            data = (json.dumps(payload) + "\n").encode()
            path.write_bytes(data)
            return CommandResult(0, {"plan_sha256": hashlib.sha256(data).hexdigest()})
        if operation.execute:
            return CommandResult(
                2, None, "ethereum_gas_deferred",
                detail="ethereum_gas_deferred:source=rpc;value_wei=851018418;threshold_wei=500000000",
            )
        return CommandResult(0, {"status": "preview"})

    assert process_wallet(db, "first-three", ordinal, invoke=invoke,
                          native_balance=lambda _chain, _wallet: ("ETH", 100),
                          now=1000) == "manual_review"
    with sqlite3.connect(db) as conn:
        action = conn.execute("SELECT status,reason FROM defi_batch_actions").fetchone()
        outcome = conn.execute(
            "SELECT protocol_name,withdrawal_size,native_before_wei,native_after_wei,"
            "reason,error_detail "
            "FROM defi_batch_outcomes"
        ).fetchone()
    assert action == ("manual_review", "ethereum_gas_deferred")
    assert outcome == ("Fuel", "withdraw(address,address,uint240)() raw parameters: "
                       "[address], [address], 1000000000000000",
                       "100", "100", "ethereum_gas_deferred",
                       "ethereum_gas_deferred:source=rpc;value_wei=851018418;threshold_wei=500000000")


def test_execution_rpc_failure_without_journal_intent_retries_without_long_wait(tmp_path,
                                                                                 monkeypatch):
    workbook, plan, db = _inputs(tmp_path)
    initialize_batch(
        db_path=db, workbook_path=workbook, seed_plan_path=plan,
        run_id="first-three", wallet_count=3, output_dir=tmp_path / "batch", now=1000,
    )
    with sqlite3.connect(db) as conn:
        ordinal, wallet = conn.execute(
            "SELECT ordinal, wallet FROM defi_batch_wallets "
            "WHERE run_id='first-three' AND queue_index=0"
        ).fetchone()
    waits = []
    monkeypatch.setattr("evm_inventory.defi_batch.time.sleep", waits.append)
    execute_calls = []

    def invoke(operation):
        if operation.kind == "quote-defi":
            path = operation.output_path
            payload = {"schema": "rabby-defi-withdraw-v1", "entries": [{
                "wallet": wallet, "status": "ready", "action_id": "a" * 64,
                "chain_id": 1, "protocol_name": "Test Pool", "net_usd_value": 1,
                "action": {"func": "redeem(uint256)()", "str_params": ["42"]},
            }]}
            data = (json.dumps(payload) + "\n").encode()
            path.write_bytes(data)
            return CommandResult(0, {"plan_sha256": hashlib.sha256(data).hexdigest()})
        if operation.execute:
            execute_calls.append(operation)
            return CommandResult(4, None, "transient")
        return CommandResult(0, {"status": "preview"})

    assert process_wallet(db, "first-three", ordinal, invoke=invoke,
                          native_balance=lambda _chain, _wallet: ("ETH", 100),
                          now=1000) == "done"
    assert len(execute_calls) == 3
    assert waits == [1, 2]
    with sqlite3.connect(db) as conn:
        assert conn.execute("SELECT status,reason FROM defi_batch_actions").fetchone() == (
            "skipped", "transient")
        assert conn.execute("SELECT count(*) FROM defi_batch_transactions").fetchone()[0] == 0
