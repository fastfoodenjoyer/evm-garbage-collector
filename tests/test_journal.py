from evm_inventory.journal import Journal


def test_journal_persists_transaction_and_deposit_status(tmp_path):
    with Journal(tmp_path / "journal.sqlite") as journal:
        operation_id = journal.create_operation(wallet="0x" + "1" * 40, action="direct_deposit")
        journal.record_transaction(operation_id, "0x" + "a" * 64)
        journal.record_deposit_status(operation_id, "success")
        row = journal.operation(operation_id)

    assert row["tx_hash"] == "0x" + "a" * 64
    assert row["deposit_status"] == "success"
