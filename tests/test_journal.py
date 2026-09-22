import sqlite3

import pytest

from evm_inventory.journal import Journal


def test_journal_persists_transaction_and_deposit_status(tmp_path):
    with Journal(tmp_path / "journal.sqlite") as journal:
        operation_id = journal.create_operation(wallet="0x" + "1" * 40, action="direct_deposit")
        journal.record_transaction(operation_id, "0x" + "a" * 64)
        journal.record_deposit_status(operation_id, "success")
        row = journal.operation(operation_id)

    assert row["tx_hash"] == "0x" + "a" * 64
    assert row["deposit_status"] == "success"


def test_journal_migrates_legacy_operations_and_records_deferred_reason(tmp_path):
    path = tmp_path / "journal.sqlite"
    connection = sqlite3.connect(path)
    connection.execute("""
        CREATE TABLE operations (
          id INTEGER PRIMARY KEY, wallet TEXT NOT NULL, action TEXT NOT NULL,
          state TEXT NOT NULL DEFAULT 'planned', tx_hash TEXT,
          deposit_status TEXT, created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
          updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        )
    """)
    connection.execute(
        "INSERT INTO operations(wallet, action, state, tx_hash, deposit_status) "
        "VALUES (?, ?, ?, ?, ?)",
        ("0x" + "1" * 40, "direct_deposit", "submitted", "0x" + "a" * 64, "pending"),
    )
    connection.commit()
    connection.close()

    with Journal(path) as journal:
        journal.record_deferred(1, "insufficient native gas")
        row = journal.operation(1)

    assert row["wallet"] == "0x" + "1" * 40
    assert row["tx_hash"] == "0x" + "a" * 64
    assert row["deposit_status"] == "pending"
    assert row["state"] == "deferred"
    assert row["reason"] == "insufficient native gas"


def test_journal_records_approval_completed_route_deferred_hash_and_reason(tmp_path):
    with Journal(tmp_path / "journal.sqlite") as journal:
        operation_id = journal.create_operation(
            wallet="0x" + "1" * 40, action="approve_then_deposit"
        )
        approval_hash = "0x" + "a" * 64
        journal.record_approval_completed_route_deferred(
            operation_id, approval_hash, "insufficient native gas for deposit"
        )
        row = journal.operation(operation_id)

    assert row["state"] == "approval_completed_route_deferred"
    assert row["tx_hash"] is None
    assert row["approval_tx_hash"] == approval_hash
    assert row["reason"] == "insufficient native gas for deposit"


def test_journal_retains_approval_hash_separately_from_final_transaction(tmp_path):
    with Journal(tmp_path / "journal.sqlite") as journal:
        operation_id = journal.create_operation(wallet="0x" + "1" * 40, action="route_ready")
        journal.record_approval_hash(operation_id, "0x" + "a" * 64)
        journal.record_transaction(operation_id, "0x" + "b" * 64)
        row = journal.operation(operation_id)

    assert row["approval_tx_hash"] == "0x" + "a" * 64
    assert row["tx_hash"] == "0x" + "b" * 64


def test_deposit_status_does_not_overwrite_deferred_terminal_states(tmp_path):
    with Journal(tmp_path / "journal.sqlite") as journal:
        deferred_id = journal.create_operation(wallet="0x" + "1" * 40, action="direct_deposit")
        route_deferred_id = journal.create_operation(
            wallet="0x" + "2" * 40, action="approve_then_deposit"
        )
        journal.record_deferred(deferred_id, "insufficient native gas")
        journal.record_approval_completed_route_deferred(
            route_deferred_id, "0x" + "a" * 64, "insufficient native gas for deposit"
        )
        journal.record_deposit_status(deferred_id, "success")
        journal.record_deposit_status(route_deferred_id, "pending")

        deferred = journal.operation(deferred_id)
        route_deferred = journal.operation(route_deferred_id)

    assert deferred["state"] == "deferred"
    assert deferred["deposit_status"] is None
    assert route_deferred["state"] == "approval_completed_route_deferred"
    assert route_deferred["deposit_status"] is None


def test_transaction_does_not_overwrite_deferred_terminal_states(tmp_path):
    with Journal(tmp_path / "journal.sqlite") as journal:
        deferred_id = journal.create_operation(wallet="0x" + "1" * 40, action="direct_deposit")
        route_deferred_id = journal.create_operation(
            wallet="0x" + "2" * 40, action="approve_then_deposit"
        )
        journal.record_deferred(deferred_id, "insufficient native gas")
        journal.record_approval_completed_route_deferred(
            route_deferred_id, "0x" + "a" * 64, "insufficient native gas for deposit"
        )
        journal.record_transaction(deferred_id, "0x" + "b" * 64)
        journal.record_transaction(route_deferred_id, "0x" + "b" * 64)

        deferred = journal.operation(deferred_id)
        route_deferred = journal.operation(route_deferred_id)

    assert deferred["state"] == "deferred"
    assert deferred["reason"] == "insufficient native gas"
    assert deferred["tx_hash"] is None
    assert route_deferred["state"] == "approval_completed_route_deferred"
    assert route_deferred["reason"] == "insufficient native gas for deposit"
    assert route_deferred["tx_hash"] is None
    assert route_deferred["approval_tx_hash"] == "0x" + "a" * 64


def test_journal_persists_idempotent_position_step_intent_without_sensitive_payloads(tmp_path):
    with Journal(tmp_path / "journal.sqlite") as journal:
        position = journal.get_or_create_position(
            position_key="wallet:1:asset", wallet="0x" + "1" * 40
        )
        first = journal.record_step_intent(
            position_id=position["id"],
            step_key="bridge",
            nonce=7,
            calldata_digest="a" * 64,
            signed_payload_digest="b" * 64,
        )
        second = journal.record_step_intent(
            position_id=position["id"],
            step_key="bridge",
            nonce=7,
            calldata_digest="a" * 64,
            signed_payload_digest="b" * 64,
        )
        journal.record_broadcast_attempt(first["id"], "0x" + "c" * 64)
        journal.record_receipt(first["id"], status="confirmed", finality_block=123)
        journal.record_balance_baseline(first["id"], balance_raw=100, expected_delta_raw=90)
        journal.record_timeout_report(first["id"], "no_correlated_arrival")
        row = journal.step(first["id"])

        columns = {
            item["name"] for item in journal.connection.execute("PRAGMA table_info(route_steps)")
        }

    assert first["id"] == second["id"]
    assert row["nonce"] == 7
    assert row["calldata_digest"] == "a" * 64
    assert row["signed_payload_digest"] == "b" * 64
    assert row["tx_hash"] == "0x" + "c" * 64
    assert row["broadcast_attempts"] == 1
    assert row["receipt_status"] == "confirmed"
    assert row["finality_block"] == 123
    assert row["balance_baseline_raw"] == 100
    assert row["expected_delta_raw"] == 90
    assert row["timeout_report"] == "no_correlated_arrival"
    assert "calldata" not in columns
    assert "signed_payload" not in columns


def test_journal_rejects_conflicting_intent_for_same_position_step_nonce(tmp_path):
    with Journal(tmp_path / "journal.sqlite") as journal:
        position = journal.get_or_create_position(
            position_key="wallet:1:asset", wallet="0x" + "1" * 40
        )
        journal.record_step_intent(
            position_id=position["id"], step_key="bridge", nonce=7,
            calldata_digest="a" * 64, signed_payload_digest="b" * 64,
        )

        with pytest.raises(ValueError, match="conflicting durable step intent"):
            journal.record_step_intent(
                position_id=position["id"], step_key="bridge", nonce=7,
                calldata_digest="d" * 64, signed_payload_digest="b" * 64,
            )


def test_journal_persists_group_budget_projection_and_realised_loss(tmp_path):
    wallet = "0x" + "1" * 40
    with Journal(tmp_path / "journal.sqlite") as journal:
        group = journal.get_or_create_group(
            group_key=f"{wallet}:10",
            wallet=wallet,
            source_chain_id=10,
            loss_budget_pct="15",
            source_usd="100",
        )
        journal.record_group_state(group["id"], "active")
        journal.record_group_projection(
            group["id"], projected_loss_usd="12.5", projected_loss_pct="12.5"
        )
        journal.record_group_realized_loss(
            group["id"], realized_loss_usd="4.25"
        )
        row = journal.group(group["id"])

    assert row["group_key"] == f"{wallet}:10"
    assert row["loss_budget_pct"] == "15"
    assert row["projected_loss_usd"] == "12.5"
    assert row["projected_loss_pct"] == "12.5"
    assert row["realized_loss_usd"] == "4.25"
    assert row["state"] == "active"


def test_journal_persists_post_swap_requote_evidence_and_partial_asset_state(tmp_path):
    wallet = "0x" + "1" * 40
    balance_raw = "90000000000000000000000000000000000001"
    with Journal(tmp_path / "journal.sqlite") as journal:
        group = journal.get_or_create_group(
            group_key=f"{wallet}:10",
            wallet=wallet,
            source_chain_id=10,
            loss_budget_pct="15",
            source_usd="100",
        )
        position = journal.get_or_create_position(
            position_key=f"{wallet}:10:{'0x' + 'a' * 40}",
            wallet=wallet,
            group_id=group["id"],
            source_asset_id="0x" + "a" * 40,
            source_amount_raw="100000000000000000000000000000000000001",
        )
        journal.record_position_state(
            position["id"],
            "source_asset_converted_bridge_pending",
            actual_asset_id="native",
            actual_balance_raw=balance_raw,
        )
        journal.record_requote_after_swap(
            position["id"],
            old_route_id="planned-bridge",
            old_payload_hash="a" * 64,
            new_route_id="replacement-bridge",
            new_payload_hash="b" * 64,
            input_amount_raw=balance_raw,
            actual_asset_id="native",
        )
        journal.record_position_state(
            position["id"], "manual_review_after_swap", reason="no_eligible_bridge"
        )
        row = journal.position(position["id"])
        event = journal.position_events(position["id"])[0]

    assert row["state"] == "manual_review_after_swap"
    assert row["actual_asset_id"] == "native"
    assert row["actual_balance_raw"] == balance_raw
    assert event["event_type"] == "requote_after_swap"
    assert event["old_route_id"] == "planned-bridge"
    assert event["old_payload_hash"] == "a" * 64
    assert event["new_route_id"] == "replacement-bridge"
    assert event["new_payload_hash"] == "b" * 64
    assert event["input_amount_raw"] == balance_raw


def test_journal_rejects_backward_position_and_step_transitions(tmp_path):
    with Journal(tmp_path / "journal.sqlite") as journal:
        position = journal.get_or_create_position(
            position_key="wallet:10:asset", wallet="0x" + "1" * 40
        )
        journal.record_position_state(
            position["id"],
            "source_asset_converted_bridge_pending",
            actual_asset_id="native",
            actual_balance_raw="100",
        )
        with pytest.raises(ValueError, match="invalid position transition"):
            journal.record_position_state(position["id"], "planned")

        step = journal.record_step_intent(
            position_id=position["id"],
            step_key="swap",
            nonce=1,
            calldata_digest="a" * 64,
            signed_payload_digest="b" * 64,
        )
        journal.record_broadcast_attempt(step["id"], "0x" + "c" * 64)
        with pytest.raises(ValueError, match="invalid route step transition"):
            journal.record_step_state(step["id"], "planned")


def test_journal_persists_group_threshold_halt_state(tmp_path):
    wallet = "0x" + "1" * 40
    with Journal(tmp_path / "journal.sqlite") as journal:
        group = journal.get_or_create_group(
            group_key=f"{wallet}:10",
            wallet=wallet,
            source_chain_id=10,
            loss_budget_pct="15",
            source_usd="100",
        )
        journal.record_group_state(
            group["id"],
            "manual_review_group_threshold_exceeded",
            reason="projected loss exceeded the configured group budget",
        )
        row = journal.group(group["id"])

    assert row["state"] == "manual_review_group_threshold_exceeded"
    assert row["reason"] == "projected loss exceeded the configured group budget"
