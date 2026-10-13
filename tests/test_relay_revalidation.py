from types import SimpleNamespace

import pytest

from evm_inventory.journal import Journal
from evm_inventory.relay_revalidation import revalidate_legacy_gas
from evm_inventory.route_execution import _position_key

WALLET = "0x" + "1" * 40
RECIPIENT = "0x" + "2" * 40


def entry():
    return dict(wallet=WALLET, chain_id=10, asset_id="native", decimals=18, symbol="ETH",
        raw_balance="100", routing_provider="relay", status="manual_review",
        reason="insufficient_source_gas", source_balance_usd="2", automatic_requote=True,
        group={"key": "original"}, relay_probe={"request": {
            "user": WALLET, "recipient": RECIPIENT, "originChainId": 10, "amount": "100"}})


def run(entries, path, monkeypatch, *, balance=200):
    calls = []
    rpc = SimpleNamespace(call=lambda _, method, __: hex(10 if method == "eth_chainId"
                                                        else balance))
    clients = SimpleNamespace(relay_for_wallet=lambda _: object())

    def quote(base, *a, **kw):
        calls.append((base, kw))
        return {**base, "routing_provider": "relay", "status": "manual_review",
                "reason": "max_route_loss_exceeded", "source_gas_estimate_verified": True}

    monkeypatch.setattr("evm_inventory.relay_revalidation.quote_relay_entry", quote)
    changed = revalidate_legacy_gas(entries, db_path=path, rpc=rpc, clients=clients, targets=(),
        wallet=SimpleNamespace(public_address=WALLET, bitget_deposit_address=RECIPIENT),
        rpc_urls={10: "fake"}, target_policy=SimpleNamespace(digest="policy"),
        loss_limit="15", quote_floor="0.01")
    return changed, calls


@pytest.mark.parametrize("balance,expected", [(200, "100"), (70, "70")])
def test_legacy_revalidation_retains_authority_budget_and_plan_identity(tmp_path, monkeypatch,
                                                                      balance, expected):
    e = entry()
    changed, calls = run([e], tmp_path / "fake.sqlite", monkeypatch, balance=balance)
    assert changed == 1
    assert calls[0][0]["raw_balance"] == expected
    assert calls[0][1]["original_source_usd"] == "2"
    assert calls[0][1]["deposit_address"] == RECIPIENT
    assert e["group"] == {"key": "original"}
    assert e["reason"] == "max_route_loss_exceeded"
    assert run([e], tmp_path / "fake.sqlite", monkeypatch)[0] == 0


@pytest.mark.parametrize("tx_hash", [None, "0x" + "a" * 64])
def test_legacy_revalidation_never_refreshes_any_source_intent(tmp_path, monkeypatch, tx_hash):
    e = entry()
    path = tmp_path / "fake.sqlite"
    with Journal(path) as journal:
        p = journal.get_or_create_position(position_key=_position_key(e), wallet=WALLET)
        journal.record_step_intent(position_id=p["id"], step_key="relay_deposit", nonce=0,
            calldata_digest="a" * 64, signed_payload_digest="b" * 64, tx_hash=tx_hash)
    changed, calls = run([e], path, monkeypatch)
    assert changed == 0 and calls == []
    assert e["reason"] == "insufficient_source_gas"


def test_legacy_revalidation_rejects_changed_recipient_before_rpc(tmp_path, monkeypatch):
    e = entry()
    e["relay_probe"]["request"]["recipient"] = "0x" + "3" * 40
    changed, calls = run([e], tmp_path / "fake.sqlite", monkeypatch)
    assert changed == 1 and calls == []
    assert e["reason"] == "relay_source_gas_revalidation_failed"
    assert e["search"]["errors"][0]["retry_at"] is None
