import json

import pytest
from test_live_plan import NOW, WALLET, _allowlist, _balance_rows, _quoted_route, _RecordingQuotes

from evm_inventory.bitget_catalog import BitgetDepositTarget
from evm_inventory.collection import _unresolved
from evm_inventory.lifi import _route_from_dict
from evm_inventory.lifi_quota import LifiRequestLimit
from evm_inventory.live_plan import create_live_plan

BALANCE = 10**13
TOKEN = "0x" + "b" * 40


def quote_with_fee(request, amount=BALANCE, included=False, fee_chain=10):
    route = _quoted_route(
        request, route_id=str(request.to_chain_id), output_amount=19000, gas_amount=1
    )
    payload = json.loads(route.evidence.canonical_payload_json)
    fee = {
        "amount": str(amount),
        "included": included,
        "amountUSD": "0.02",
        "priceUSD": "2000",
        "timestamp": NOW.isoformat(),
        "token": {"chainId": fee_chain, "address": "0x" + "0" * 40, "decimals": 18},
    }
    payload["feeCosts"] = [fee]
    return _route_from_dict(payload, quote_timestamp=NOW.isoformat())


def run_plan(tmp_path, responder, gas):
    targets = tuple(
        BitgetDepositTarget("USDC", chain, TOKEN, 1, decimals=6) for chain in (8453, 42161)
    )
    quotes = _RecordingQuotes(responder)
    result = create_live_plan(
        _balance_rows(
            tmp_path,
            [
                dict(
                    wallet=WALLET,
                    chain_id=10,
                    asset_id="native",
                    raw_balance=str(BALANCE),
                    decimals=18,
                    symbol="ETH",
                    status="success",
                )
            ],
        ),
        deposit_addresses={WALLET: "0x" + "d" * 40},
        allowlist_path=_allowlist(tmp_path, [(10, "native", "swap")]),
        targets=targets,
        client=quotes,
        gas_estimator=gas,
        now_ms=lambda: int(NOW.timestamp() * 1000),
    )
    return result["entries"][0]


def test_upfront_native_fee_alone_exceeds_balance_is_terminal_not_gas_failure(tmp_path):
    def no_simulation(**kwargs):
        raise AssertionError("unaffordable upfront fee must be rejected before simulation")

    entry = run_plan(tmp_path, lambda r: (quote_with_fee(r),), no_simulation)
    assert entry["reason"] == "source_native_fee_exceeds_balance"
    assert entry["search"]["complete"] is True
    assert not _unresolved(entry)


def test_upfront_fee_rejection_keeps_waiting_for_unqueried_quota_alternative(tmp_path):
    def respond(request):
        if request.to_chain_id == 42161:
            raise LifiRequestLimit("routes", retry_at=2000.0)
        return (quote_with_fee(request),)

    entry = run_plan(tmp_path, respond, lambda **kw: None)
    assert entry["search"]["complete"] is False
    assert _unresolved(entry)


@pytest.mark.parametrize("included,chain", [(True, 10), (None, 10), (False, 8453)])
def test_only_explicit_extra_source_native_fee_is_terminal(tmp_path, included, chain):
    entry = run_plan(
        tmp_path,
        lambda r: (quote_with_fee(r, included=included, fee_chain=chain),),
        lambda **kw: None,
    )
    assert entry["reason"] != "source_native_fee_exceeds_balance"
    assert _unresolved(entry)


def test_fee_refusal_does_not_hide_pending_relay_fallback():
    entry = {
        "status": "manual_review",
        "reason": "source_native_fee_exceeds_balance",
        "search": {"complete": True, "errors": [], "pending": []},
        "relay_fallback": {
            "reason": "relay_route_search_incomplete",
            "search": {
                "complete": False,
                "errors": ["quota_wait"],
                "pending": [{"reason": "quota_wait", "retry_at": 2000}],
            },
        },
    }
    assert _unresolved(entry)


def test_fee_refusal_keeps_unverified_relay_gas_even_with_complete_quote_search():
    entry = {
        "status": "manual_review",
        "reason": "source_native_fee_exceeds_balance",
        "search": {"complete": True, "errors": [], "pending": []},
        "relay_fallback": {
            "reason": "relay_source_gas_unverified",
            "search": {"complete": True, "errors": [], "pending": []},
        },
    }
    assert _unresolved(entry)


@pytest.mark.parametrize("cached", [False, True])
def test_relay_amount_too_low_is_terminal_economic_refusal(cached):
    from decimal import Decimal

    import httpx

    from evm_inventory.diagnostics import exception_diagnostic
    from evm_inventory.quote_jobs import JobInProgress
    from evm_inventory.relay import quote_relay_entry

    class Client:
        def supported_chain_ids(self):
            return {10}

        def quote(self, *args, **kwargs):
            req = httpx.Request("POST", "https://api.relay.link/quote/v2")
            error = httpx.HTTPStatusError(
                "bad request",
                request=req,
                response=httpx.Response(
                    400, json={"errorCode": "AMOUNT_TOO_LOW", "message": "too small"}, request=req
                ),
            )
            if cached:
                raise JobInProgress(reason="bad_request", diagnostic=exception_diagnostic(error))
            raise error

    base = {
        "wallet": WALLET,
        "chain_id": 10,
        "asset_id": "native",
        "decimals": 18,
        "raw_balance": str(BALANCE),
    }
    result = quote_relay_entry(
        base,
        Client(),
        (BitgetDepositTarget("USDC", 8453, TOKEN, 1, decimals=6),),
        loss_limit=Decimal("15"),
        quote_floor=Decimal(".01"),
        deposit_address="0x" + "d" * 40,
    )
    assert result["reason"] == "below_target_minimum"
    assert result["search"]["complete"] is True
    assert not _unresolved(result)


def test_unaffordable_direction_does_not_hide_affordable_alternative(tmp_path):
    from evm_inventory.models import AssetIdentity
    from evm_inventory.valuation import FeeQuote, QuotePrice

    prepared = []

    def gas(**kwargs):
        prepared.append(kwargs["route"].route_id)
        return (FeeQuote(1, QuotePrice(AssetIdentity(10, "native", 18), "2000", NOW)),)

    entry = run_plan(
        tmp_path,
        lambda r: (quote_with_fee(r, amount=BALANCE if r.to_chain_id == 8453 else 1),),
        gas,
    )
    assert entry["status"] == "route_ready"
    assert entry["target"]["chain_id"] == 42161
    assert prepared == ["42161"]
