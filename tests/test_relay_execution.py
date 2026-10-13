from types import SimpleNamespace

import pytest
from eth_utils import keccak

from evm_inventory.journal import Journal
from evm_inventory.relay_execution import _destination_transfer, _observe, _prepare
from evm_inventory.rpc import RpcError

SOURCE_HASH = "0x" + "1" * 64
DESTINATION_HASH = "0x" + "2" * 64
WALLET = "0x" + "3" * 40
RECIPIENT = "0x" + "4" * 40
TOKEN = "0x" + "5" * 40
TRANSFER = "0x" + keccak(text="Transfer(address,address,uint256)").hex()


def _checkpoint():
    return {"source_chain_id": 1868, "recipient": RECIPIENT,
        "target": {"chain_id": 8453, "asset_id": TOKEN, "coin": "USDC", "chain": "BASE"},
        "quote": {"to_amount_min": "100"}, "request_id": "0x" + "a" * 64,
        "started_ms": 100000}


def _receipt(data=None):
    return {"transactionHash": DESTINATION_HASH, "status": "0x1", "blockNumber": "0x10",
        "logs": [{"address": TOKEN, "topics": [TRANSFER,
            "0x" + WALLET[2:].rjust(64, "0"), "0x" + RECIPIENT[2:].rjust(64, "0")],
            "data": data if data is not None else "0x" + f"{150:064x}"}]}


def _rpc(receipt):
    def call(url, method, params):
        if method == "eth_chainId":
            return hex(1868 if url == "source" else 8453)
        if method == "eth_getBlockByNumber":
            return {"number": "0x12"}
        if method == "eth_getTransactionReceipt":
            if params == [SOURCE_HASH]:
                return {"transactionHash": SOURCE_HASH, "status": "0x1", "blockNumber": "0x10"}
            assert params == [DESTINATION_HASH]
            return receipt
        pytest.fail("Observer must only read source/destination receipts: " + method)
    return SimpleNamespace(call=call)


def test_relay_destination_decodes_abi_transfer_word_with_leading_zeroes():
    assert _destination_transfer(_rpc(_receipt()), {8453: "destination"}, _checkpoint(),
                                 [DESTINATION_HASH]) == DESTINATION_HASH


@pytest.mark.parametrize("data", ["0x1", "0x", "0x" + "00" * 31, "0x" + "00" * 33])
def test_relay_destination_rejects_transfer_data_that_is_not_one_uint256(data):
    with pytest.raises(RpcError, match="invalid_abi_uint256"):
        _destination_transfer(_rpc(_receipt(data)), {8453: "destination"}, _checkpoint(),
                              [DESTINATION_HASH])


def test_relay_observer_pending_to_credit_uses_saved_hashes_without_send(tmp_path):
    statuses = iter([None, "pending", "success"])
    polls = []
    def deposit_status(**kwargs):
        polls.append(kwargs)
        return next(statuses)
    client = SimpleNamespace(status=lambda _: {"status": "success", "originChainId": 1868,
        "destinationChainId": 8453, "inTxHashes": [SOURCE_HASH], "txHashes": [DESTINATION_HASH]})
    with Journal(tmp_path / "inventory.sqlite") as journal:
        position = journal.get_or_create_position(position_key="relay", wallet=WALLET)
        journal.record_step_intent(position_id=position["id"], step_key="relay_deposit", nonce=1,
            calldata_digest="b" * 64, signed_payload_digest="c" * 64, tx_hash=SOURCE_HASH)
        journal.record_position_state(position["id"], "bridge_submitted")
        checkpoint = _checkpoint()
        journal.save_checkpoint(position["id"], "relay_execution", checkpoint)
        kw = dict(client=client, rpc=_rpc(_receipt()),
                  rpc_urls={1868: "source", 8453: "destination"},
                  bitget=SimpleNamespace(deposit_status=deposit_status))
        assert _observe(journal, position, checkpoint, **kw) == "pending"
        assert _observe(journal, position, checkpoint, **kw) == "pending"
        assert _observe(journal, position, checkpoint, **kw) == "completed"
        assert journal.position(position["id"])["state"] == "completed"
        credit = journal.checkpoint(position["id"], "bitget_credit")
        assert credit["status"] == "success" and credit["tx_hash"] == DESTINATION_HASH
    assert len(polls) == 3
    assert all(p["tx_hash"] == DESTINATION_HASH and p["recipient"] == RECIPIENT for p in polls)


def _native_prepare(estimates=(100,), *, balance=10000, gas_raw=126, reserve_raw=152,
                    minimum_raw=1000, received_pct="98", original_usd="10"):
    from decimal import Decimal

    from test_relay_planner_gas import BASE, RECIPIENT, TARGET, WALLET, FakeRpc, Quotes

    rpc = FakeRpc(estimates)
    rpc.balance = balance
    client = Quotes(minimum_raw=minimum_raw, received_pct=received_pct)
    entry = {**BASE, "deposit_address": RECIPIENT, "automatic_requote": True,
             "source_balance_usd": original_usd, "source_gas_reserve_raw": str(reserve_raw),
             "relay_selected": {"source_gas_raw": str(gas_raw),
                                "source_gas_reserve_raw": str(reserve_raw),
                                "request": {"amount": str(balance - reserve_raw)}}}
    wallet = SimpleNamespace(public_address=WALLET, bitget_deposit_address=RECIPIENT)
    return entry, wallet, client, rpc, TARGET, Decimal("15")


def test_native_relay_preparation_reuses_padded_amount_when_fee_moves_by_one(monkeypatch):
    from test_relay_protocol import NOW

    monkeypatch.setattr("evm_inventory.relay_protocol.time.time", lambda: NOW)
    args = _native_prepare((101, 102, 103, 104))
    quote, tx = _prepare(*args[:4], "fake-rpc", *args[4:])
    assert args[2].calls == [(8453, 9848)]
    assert int(quote["request"]["amount"]) + tx.max_total_fee_wei <= args[3].balance


def test_native_relay_fee_spike_only_lowers_unsigned_input(monkeypatch):
    from test_relay_protocol import NOW

    monkeypatch.setattr("evm_inventory.relay_protocol.time.time", lambda: NOW)
    args = _native_prepare((200, 201))
    quote, tx = _prepare(*args[:4], "fake-rpc", *args[4:])
    amounts = [amount for _, amount in args[2].calls]
    assert amounts == [9848, 9704]
    assert int(quote["request"]["amount"]) + tx.max_total_fee_wei <= args[3].balance


def test_native_relay_provider_hint_never_forces_half_balance_or_proves_insufficient(monkeypatch):
    from test_relay_protocol import NOW

    monkeypatch.setattr("evm_inventory.relay_protocol.time.time", lambda: NOW)
    args = _native_prepare(balance=140, reserve_raw=50, gas_raw=200, original_usd="0.14")
    with pytest.raises(ValueError, match="loss"):
        _prepare(*args[:4], "fake-rpc", *args[4:])
    assert args[2].calls == [(8453, 90), (8453, 14)]


def test_native_relay_unstable_caps_schedule_retry_with_numeric_diagnostic(monkeypatch):
    from test_relay_protocol import NOW

    monkeypatch.setattr("evm_inventory.relay_protocol.time.time", lambda: NOW)
    args = _native_prepare((200, 400, 800, 1600))
    with pytest.raises(ValueError) as failure:
        _prepare(*args[:4], "fake-rpc", *args[4:])
    assert getattr(failure.value, "reason", None) == "relay_gas_reserve_changed"
    assert getattr(failure.value, "retry_at", None) == NOW + 5
    assert failure.value.diagnostic["required_fee_raw"] == "966"
    assert len(args[2].calls) == 3


@pytest.mark.parametrize("kwargs,reason", [
    ({"minimum_raw": 99}, "deposit minimum"),
    ({"received_pct": "70"}, "loss"),
    ({"original_usd": "20"}, "loss"),
])
def test_native_relay_final_exact_quote_keeps_original_budget_and_minimum(monkeypatch, kwargs,
                                                                       reason):
    from test_relay_protocol import NOW

    monkeypatch.setattr("evm_inventory.relay_protocol.time.time", lambda: NOW)
    args = _native_prepare(**kwargs)
    with pytest.raises(ValueError, match=reason):
        _prepare(*args[:4], "fake-rpc", *args[4:])


def test_native_relay_pending_balance_is_rechecked_after_quote_and_fee(monkeypatch):
    from test_relay_protocol import NOW

    monkeypatch.setattr("evm_inventory.relay_protocol.time.time", lambda: NOW)
    args = _native_prepare()
    original = args[3].call

    def call(url, method, params):
        if method == "eth_getBalance" and params[-1] == "pending":
            return "0x1"
        return original(url, method, params)

    args[3].call = call
    with pytest.raises(ValueError, match="pending balance"):
        _prepare(*args[:4], "fake-rpc", *args[4:])


def test_relay_unsigned_retry_gate_preserves_window_and_broadcast_observation(tmp_path):
    from evm_inventory.relay_execution import _require_unsigned_retry_due
    from evm_inventory.stages import StageDeadlineExceeded

    with Journal(tmp_path / "fake.sqlite") as journal:
        p = journal.get_or_create_position(position_key="relay", wallet="0x" + "1" * 40)
        journal.save_checkpoint(p["id"], "relay_retry", dict(retry_at=2000, reason="quota_wait"))
        with pytest.raises(StageDeadlineExceeded, match="provider_retry_scheduled"):
            _require_unsigned_retry_due(journal, p["id"], now_seconds=1000)
        _require_unsigned_retry_due(journal, p["id"], now_seconds=2000)
        journal.record_step_intent(position_id=p["id"], step_key="relay_deposit", nonce=0,
            calldata_digest="a" * 64, signed_payload_digest="b" * 64, tx_hash=None)
        with pytest.raises(ValueError, match="durable_asset_intent_hash_missing"):
            _require_unsigned_retry_due(journal, p["id"], now_seconds=1000)
        journal.connection.execute("UPDATE route_steps SET tx_hash=? WHERE position_id=?",
                                   ("0x" + "c" * 64, p["id"]))
        journal.connection.commit()
        _require_unsigned_retry_due(journal, p["id"], now_seconds=1000)
