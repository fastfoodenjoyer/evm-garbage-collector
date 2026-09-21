import sqlite3

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
