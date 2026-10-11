from datetime import UTC, datetime, timedelta
from hashlib import sha256
from types import SimpleNamespace

import pytest

import evm_inventory.route_execution as execution
from evm_inventory.journal import Journal
from evm_inventory.lifi import TransactionRequest
from evm_inventory.stages import Deadline, deadline_scope

NOW = datetime(2026, 10, 11, 21, 0, tzinfo=UTC)
OLD_HASH = "0x" + "a" * 64
NEW_HASH = "0x" + "b" * 64
WALLET = SimpleNamespace(public_address="0x" + "1" * 40,
                         bitget_deposit_address="0x" + "2" * 40)


def setup_position(journal, chain=534352):
    group = journal.get_or_create_group(group_key="direct", wallet=WALLET.public_address,
        source_chain_id=chain, loss_budget_pct="15", source_usd="100")
    position = journal.get_or_create_position(position_key="direct", wallet=WALLET.public_address,
        group_id=group["id"], source_asset_id="native", source_amount_raw="1000")
    step = journal.record_step_intent(position_id=position["id"], step_key="direct_deposit",
        nonce=9, calldata_digest=sha256(b"0x").hexdigest(), signed_payload_digest="c" * 64)
    journal.record_broadcast_attempt(step["id"], OLD_HASH)
    journal.connection.execute("UPDATE route_steps SET created_at=? WHERE id=?",
        ((NOW - timedelta(minutes=10)).replace(tzinfo=None).isoformat(), step["id"]))
    journal.connection.commit()
    request = TransactionRequest(chain, WALLET.bitget_deposit_address, "0x", 970, 10, 1)
    entry = {"wallet": WALLET.public_address, "chain_id": chain, "asset_id": "native",
             "decimals": 18, "raw_balance": "1000", "status": "direct_deposit",
             "automatic_requote": True, "_journal_position_id": position["id"],
             "_direct_transaction": request, "valuation": {"loss_usd": "1"},
             "target": {"coin": "ETH", "chain": "Scroll", "minimum_raw": "1"}}
    return entry, journal.step(step["id"]), position["id"]


@pytest.mark.parametrize("nonce,visible,loss,eligible", [
    (9, False, "1", True), (10, False, "1", False), (9, True, "1", False),
    (9, False, "16", False),
])
def test_scroll_missing_transfer_replaces_only_unused_nonce_and_respects_budget(
    tmp_path, monkeypatch, nonce, visible, loss, eligible,
):
    def call(_, method, params):
        return {"eth_chainId": hex(534352), "eth_getTransactionCount": hex(nonce),
                "eth_getTransactionByHash": {"hash": OLD_HASH} if visible else None}[method]
    def fresh(e, **_):
        assert e["_replacement_nonce"] == 9
        assert e["_replacement_original_request"].value == 970
        return {**e, "valuation": {"loss_usd": loss}, "_direct_transaction":
                TransactionRequest(534352, WALLET.bitget_deposit_address, "0x", 900, 10, 2,
                                   additional_fee_wei=20)}
    monkeypatch.setattr(execution, "_preflight_live_entry", fresh)
    with Journal(tmp_path / "inventory.sqlite") as journal:
        entry, saved, pid = setup_position(journal)
        kwargs = dict(entry=entry, saved=saved, position_id=pid, wallet=WALLET,
                      rpc=SimpleNamespace(call=call), rpc_urls={534352: "rpc"},
                      jumper=object(), bitget=object(), journal=journal,
                      now_ms=lambda: int(NOW.timestamp() * 1000))
        if loss == "16":
            with pytest.raises(execution.GroupLossLimitExceeded):
                execution._recover_missing_scroll_direct(**kwargs)
        else:
            result = execution._recover_missing_scroll_direct(**kwargs)
            assert bool(result) == eligible
        if eligible:
            recovery = journal.checkpoint(pid, "direct_recovery")
            assert recovery["replacement_nonce"] == 9
            assert recovery["original_entry"]["_direct_transaction"].value == 970
            assert journal.step(saved["id"])["tx_hash"] == OLD_HASH
            assert journal.step(saved["id"])["state"] == "submitted"
        else:
            assert journal.checkpoint(pid, "direct_recovery") is None


@pytest.mark.parametrize("winner", [OLD_HASH, NEW_HASH])
def test_scroll_replacement_resume_observes_both_hashes_without_another_transfer(
    tmp_path, monkeypatch, winner,
):
    calls = []
    def call(_, method, params):
        assert method == "eth_getTransactionReceipt"
        calls.append(params[0])
        return {"status": "0x1", "transactionHash": winner} if params[0] == winner else None
    with Journal(tmp_path / "inventory.sqlite") as journal:
        entry, _, pid = setup_position(journal)
        retry = journal.record_step_intent(position_id=pid, step_key="direct_deposit:replacement1",
            nonce=9, calldata_digest=sha256(b"0x").hexdigest(), signed_payload_digest="d"*64)
        journal.record_broadcast_attempt(retry["id"], NEW_HASH)
        journal.save_checkpoint(pid, "direct_recovery", {
            "kind": "scroll_l1_fee", "step_key": "direct_deposit:replacement1",
            "replacement_nonce": 9, "original_entry": entry, "failed_tx_hash": OLD_HASH,
            "failed_fee_usd": "0"})
        monkeypatch.setattr(execution, "_execute_entry", lambda **_: pytest.fail("duplicate send"))
        monkeypatch.setattr(execution, "_replay_missing_direct", lambda **_: None)
        bitget = SimpleNamespace(wait_for_deposit_seen=lambda **kw: "success"
                                 if kw["tx_hash"] == winner else None)
        with deadline_scope(Deadline(30)):
            result = execution._submit_live_step(entry, {"kind": "direct_deposit"},
                wallet=WALLET, rpc=SimpleNamespace(call=call), rpc_urls={534352: "rpc"},
                jumper=object(), bitget=bitget, journal=journal, now_ms=lambda: 1000)
        assert result["state"] == "completed"
        assert OLD_HASH in calls and winner in calls
        expected_key = "direct_deposit" if winner == OLD_HASH else "direct_deposit:replacement1"
        assert execution._direct_step_key(journal, pid) == expected_key


def test_recovery_checkpoint_before_prepared_restores_key_and_rechecks_unsigned_budget(
    tmp_path, monkeypatch,
):
    sends = []
    with Journal(tmp_path / "inventory.sqlite") as journal:
        entry, old, pid = setup_position(journal, chain=42220)
        journal.record_receipt(old["id"], status="reverted", finality_block=10)
        journal.save_checkpoint(pid, "direct_recovery", {
            "step_key": "direct_deposit:recovery1", "failed_fee_usd": "2"})
        def fresh(e, **_):
            assert e["_journal_step_key"] == "direct_deposit:recovery1"
            return {**e, "valuation": {"loss_usd": "1"}}
        def send(**kw):
            sends.append(kw["entry"])
            return NEW_HASH
        monkeypatch.setattr(execution, "_preflight_live_entry", fresh)
        monkeypatch.setattr(execution, "_execute_entry", send)
        with deadline_scope(Deadline(30)):
            result = execution._submit_live_step(entry, {"kind": "direct_deposit"},
                wallet=WALLET, rpc=object(), rpc_urls={42220: "rpc"}, jumper=object(),
                bitget=SimpleNamespace(wait_for_deposit_seen=lambda **_: "success"),
                journal=journal, now_ms=lambda: 1000)
        assert result["state"] == "completed"
        assert len(sends) == 1
        assert sends[0]["_journal_step_key"] == "direct_deposit:recovery1"
        assert sends[0]["valuation"]["loss_usd"] == "3"


@pytest.mark.parametrize("mismatch", [False, True])
def test_replacement_rebroadcast_preserves_exact_signed_payload_and_nonce(
    tmp_path, monkeypatch, mismatch,
):
    from evm_inventory.executor import sign_transaction, signed_transaction_hash
    from evm_inventory.workbook import WalletWorkbookRow

    wallet = WalletWorkbookRow(2, 1, "0x19e7e376e7c213b7e7e7e46cc70a5dd086daff2a",
                               "0x" + "1"*64, WALLET.bitget_deposit_address)
    request = TransactionRequest(534352, wallet.bitget_deposit_address, "0x", 100, 25201, 1)
    raw = sign_transaction(request, private_key=wallet.private_key,
                           expected_sender=wallet.public_address, nonce=9)
    tx_hash = signed_transaction_hash(raw)
    sends = []
    def call(_, method, params):
        if method == "eth_getTransactionCount":
            return "0x9"
        assert method in {"eth_getTransactionReceipt", "eth_getTransactionByHash"}
        return None
    monkeypatch.setattr(execution, "broadcast_signed_transaction", lambda *_, **kw: (
        sends.append(kw["raw_transaction"]) or tx_hash))
    with Journal(tmp_path / "inventory.sqlite") as journal:
        entry, _, pid = setup_position(journal)
        step = journal.record_step_intent(position_id=pid, step_key="direct_deposit:replacement1",
            nonce=9, calldata_digest=sha256(b"0x").hexdigest(),
            signed_payload_digest=("d"*64 if mismatch else sha256(raw.encode()).hexdigest()))
        journal.record_broadcast_attempt(step["id"], tx_hash)
        kwargs = dict(entry={**entry, "_direct_transaction": request},
                      saved=journal.step(step["id"]), wallet=wallet,
                      rpc=SimpleNamespace(call=call), rpc_urls={534352: "rpc"}, journal=journal)
        if mismatch:
            with pytest.raises(execution.DepositSettlementFailed, match="payload_mismatch"):
                execution._replay_missing_direct(**kwargs)
            assert sends == []
        else:
            execution._replay_missing_direct(**kwargs)
            assert sends == [raw]
            assert journal.step(step["id"])["nonce"] == 9


def test_crash_between_durable_intent_and_send_already_preserves_transaction_hash(
    tmp_path, monkeypatch,
):
    from evm_inventory.executor import broadcast_durable_transaction

    with Journal(tmp_path / "inventory.sqlite") as journal:
        _, _, pid = setup_position(journal)
        monkeypatch.setattr(journal, "record_broadcast_attempt", lambda *_: (
            (_ for _ in ()).throw(RuntimeError("simulated_crash"))))
        with pytest.raises(RuntimeError, match="simulated_crash"):
            broadcast_durable_transaction(object(), url="rpc", journal=journal,
                position_id=pid, step_key="direct_deposit:replacement1", nonce=9,
                calldata="0x", signed_transaction="0x1234", tx_hash=NEW_HASH)
        saved = journal.latest_step(position_id=pid, step_key="direct_deposit:replacement1")
        assert saved["tx_hash"] == NEW_HASH
        assert saved["state"] == "submitted"
        assert saved["nonce"] == 9
        assert saved["broadcast_attempts"] == 0


def test_capped_erc20_direct_signs_exact_preflight_request_without_hidden_repricing(monkeypatch):
    request = TransactionRequest(56, "0x" + "3" * 40, "0xa9059cbb", 0, 25201, 10,
                                 max_total_fee_cap_wei=252010)
    signed = []
    monkeypatch.setattr(execution, "FeePlanner", lambda _: pytest.fail("hidden repricing"))
    monkeypatch.setattr(execution, "_native_balance", lambda *_, **__: 1_000_000)
    monkeypatch.setattr(execution, "pending_nonce", lambda *_, **__: 9)
    monkeypatch.setattr(execution, "sign_transaction", lambda r, **_: (
        signed.append(r) or "0x1234"))
    monkeypatch.setattr(execution, "broadcast_signed_transaction", lambda *_, **__: NEW_HASH)
    monkeypatch.setattr(execution, "wait_for_receipt", lambda *_, **__: {"status": "0x1"})
    execution._execute_entry(entry={"chain_id": 56, "asset_id": request.to,
        "status": "direct_deposit"}, wallet=SimpleNamespace(public_address=WALLET.public_address,
        private_key="unused"), rpc=object(), jumper=object(), rpc_urls={56: "rpc"},
        prepared_request=request)
    assert signed == [request]
