import copy
from types import SimpleNamespace

import pytest
from eth_abi import encode
from eth_account._utils.encode_typed_data.encoding_and_hashing import hash_struct
from eth_utils import keccak

from evm_inventory.bitget_catalog import BitgetDepositTarget
from evm_inventory.relay import quote_relay_entry
from evm_inventory.relay_protocol import ORDER_TYPES, verified_transaction

WALLET = "0x" + "1" * 40
RECIPIENT = "0x" + "2" * 40
USDC = "0x833589fcd6edb6e08f4c7c32d4f71b54bda02913"
ZERO = "0x" + "0" * 40
DEPOSITORY = "0x" + "3" * 40
NOW = 1791790000
CHAINS = [
    {"id": 1868, "vmType": "evm", "depositEnabled": True,
     "protocol": {"v2": {"chainId": "soneium", "depository": DEPOSITORY}}},
    {"id": 8453, "vmType": "evm", "depositEnabled": True,
     "protocol": {"v2": {"chainId": "base", "depository": "0x" + "4" * 40}}},
    {"id": 42161, "vmType": "evm", "depositEnabled": True,
     "protocol": {"v2": {"chainId": "arbitrum", "depository": "0x" + "5" * 40}}},
]


def _quote(refunds=None, *, amount=10**15):
    if refunds is None:
        refunds = [
            {"chainId": "soneium", "recipient": WALLET, "currency": ZERO,
             "minimumAmount": "0", "deadline": NOW + 604800, "extraData": "0x"},
            {"chainId": "base", "recipient": RECIPIENT, "currency": USDC,
             "minimumAmount": "0", "deadline": NOW + 604800, "extraData": "0x"},
        ]
    order = {
        "version": "v1", "solverChainId": "base", "solver": "0x" + "6" * 40,
        "salt": 1, "inputs": [{"payment": {"chainId": "soneium", "currency": ZERO,
                    "amount": str(amount), "weight": "1"}, "refunds": copy.deepcopy(refunds)}],
        "output": {"chainId": "base", "payments": [{"recipient": RECIPIENT,
            "currency": USDC, "minimumAmount": "1990000", "expectedAmount": "2000000"}],
            "deadline": NOW + 300, "calls": [], "extraData": "0x"},
        "fees": [],
    }
    order_id = "0x" + hash_struct("Order", ORDER_TYPES, order).hex()
    calldata = "0x" + (keccak(text="depositNative(address,bytes32)")[:4] + encode(
        ["address", "bytes32"], [WALLET, bytes.fromhex(order_id[2:])])).hex()
    request = {"user": WALLET, "recipient": RECIPIENT, "originChainId": 1868,
               "destinationChainId": 8453, "originCurrency": ZERO,
               "destinationCurrency": USDC, "amount": str(amount),
               "expectedDecimals": {"currencyIn": 18, "currencyOut": 6}}
    return {
        "request": request, "to_amount": "2000000", "to_amount_min": "1990000",
        "source_usd": "2.1", "minimum_output_usd": "1.99", "source_gas_usd": "0.00001",
        "source_gas_raw": "1000", "loss_pct": "5.24",
        "response": {"protocol": {"v2": {"hubType": "onchain", "orderData": order,
            "orderId": order_id, "paymentDetails": {"depository": DEPOSITORY,
            "chainId": "soneium", "currency": ZERO, "amount": str(amount)}}},
            "steps": [{"id": "deposit", "kind": "transaction", "requestId": "0x" + "7" * 64,
                "items": [{"data": {"to": DEPOSITORY, "from": WALLET, "chainId": 1868,
                                     "value": str(amount), "data": calldata}}]}]},
    }


@pytest.fixture(autouse=True)
def _clock(monkeypatch):
    monkeypatch.setattr("evm_inventory.relay_protocol.time.time", lambda: NOW)


def test_relay_accepts_refunds_to_exact_source_and_approved_deposit_target():
    quote = _quote()
    tx = verified_transaction(quote, CHAINS)
    assert tx.chain_id == 1868 and tx.to == DEPOSITORY
    assert tx.value == int(quote["request"]["amount"])


@pytest.mark.parametrize("index,field,value", [
    (0, "recipient", RECIPIENT), (1, "recipient", WALLET),
    (0, "recipient", "0x" + "a" * 40), (1, "recipient", "0x" + "a" * 40),
    (0, "chainId", "arbitrum"), (1, "chainId", "arbitrum"),
    (0, "currency", USDC), (1, "currency", ZERO),
    (1, "currency", "0x" + "a" * 40),
])
def test_relay_rejects_refund_outside_exact_authorized_identities(index, field, value):
    refunds = _quote()["response"]["protocol"]["v2"]["orderData"]["inputs"][0]["refunds"]
    refunds[index][field] = value
    with pytest.raises(ValueError, match="refund"):
        verified_transaction(_quote(refunds), CHAINS)


def test_relay_refund_change_still_requires_matching_order_hash():
    quote = _quote()
    quote["response"]["protocol"]["v2"]["orderData"]["inputs"][0]["refunds"][1][
        "minimumAmount"] = "1"
    with pytest.raises(ValueError, match="order hash"):
        verified_transaction(quote, CHAINS)


def test_relay_planner_advances_quote_with_approved_destination_refund():
    from decimal import Decimal

    client = SimpleNamespace(supported_chain_ids=lambda: {1868, 8453}, chain_catalog=CHAINS,
        quote=lambda _base, _target, amount, **_: _quote(amount=amount))
    entry = quote_relay_entry(
        {"wallet": WALLET, "chain_id": 1868, "asset_id": "native", "decimals": 18,
         "symbol": "ETH", "raw_balance": str(10**15)}, client,
        [BitgetDepositTarget("USDC", 8453, USDC, 10000, "BASE")],
        loss_limit=Decimal("15"), quote_floor=Decimal("0.01"), deposit_address=RECIPIENT,
    )
    assert entry["status"] == "route_ready", entry.get("diagnostic")
    assert entry["execution_supported"] is True


def test_live_plan_uses_verified_relay_fallback_while_lifi_waits(tmp_path):
    from datetime import UTC, datetime

    from test_live_plan import _allowlist, _balance_rows, _RecordingQuotes

    from evm_inventory.lifi import LifiPriceEvidence
    from evm_inventory.lifi_quota import LifiRequestLimit
    from evm_inventory.live_plan import create_live_plan

    def routes(_):
        raise LifiRequestLimit("routes", retry_at=NOW + 7200)

    relay = SimpleNamespace(supported_chain_ids=lambda: {1868, 8453}, chain_catalog=CHAINS,
        quote=lambda _base, _target, amount, **_: _quote(amount=amount))
    quotes = _RecordingQuotes(routes)
    quotes.relay_for_wallet = lambda _: relay
    # Keep fixture prices at the same acquisition clock as the saved protocol order.
    quotes.token_price = lambda asset: LifiPriceEvidence(
        asset, "2000" if asset.is_native else "1", datetime.fromtimestamp(NOW, UTC).isoformat())
    plan = create_live_plan(
        _balance_rows(tmp_path, [{"wallet": WALLET, "chain_id": 1868, "asset_id": "native",
            "raw_balance": str(10**15), "decimals": 18, "symbol": "ETH", "status": "success"}]),
        deposit_addresses={WALLET: RECIPIENT},
        allowlist_path=_allowlist(tmp_path, [(1868, "native", "swap")]), client=quotes,
        targets=(BitgetDepositTarget("USDC", 8453, USDC, 10000, "BASE"),),
        now_ms=lambda: NOW * 1000, allow_partial_search=True,
    )
    entry = plan["entries"][0]
    assert entry["status"] == "route_ready", entry
    assert entry["routing_provider"] == "relay"
    assert entry["provider_fallback"]["search"]["errors"] == ["quota_wait"]


@pytest.mark.parametrize("failure", ["unsupported", "loss", "refund"])
def test_failed_relay_fallback_preserves_specific_provider_reason(failure):
    from decimal import Decimal

    from evm_inventory.live_plan import _apply_relay_fallback

    def quote(_base, _target, amount, **_):
        result = _quote(amount=amount)
        if failure == "refund":
            result["response"]["protocol"]["v2"]["orderData"]["inputs"][0]["refunds"][1][
                "recipient"] = "0x" + "a" * 40
        return result

    client = SimpleNamespace(supported_chain_ids=lambda: set() if failure == "unsupported"
                             else {1868, 8453}, chain_catalog=CHAINS, quote=quote)
    entry = {"wallet": WALLET, "chain_id": 1868, "asset_id": "native", "decimals": 18,
             "symbol": "ETH", "raw_balance": str(10**15), "status": "manual_review",
             "reason": "quota_wait", "search": {"complete": False, "errors": ["quota_wait"]}}
    entries = [entry]
    _apply_relay_fallback(entries, WALLET, SimpleNamespace(relay_for_wallet=lambda _: client),
        [BitgetDepositTarget("USDC", 8453, USDC, 10000, "BASE")],
        Decimal("1" if failure == "loss" else "15"), Decimal("0.01"), RECIPIENT)
    assert entries[0]["reason"] == "quota_wait"
    fallback = entries[0]["relay_fallback"]
    expected = {"unsupported": "unsupported_by_relay", "loss": "max_route_loss_exceeded",
                "refund": "relay_quote_failed"}[failure]
    assert fallback["reason"] == expected
    if failure == "refund":
        assert "authorized source or destination" in fallback["diagnostic"]["exceptions"][0][
            "message"]
    elif failure == "loss":
        assert fallback["candidates"][0]["within_loss_limit"] is False
        assert "response" not in fallback["candidates"][0]
