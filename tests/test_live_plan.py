import csv
import json
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

import pytest

from evm_inventory.bitget_catalog import BitgetDepositTarget
from evm_inventory.lifi import LifiPriceEvidence, LifiRouteRequest, _route_from_dict
from evm_inventory.live_plan import create_live_plan
from evm_inventory.models import AssetIdentity
from evm_inventory.valuation import FeeQuote, QuotePrice


def test_live_plan_stages_existing_base_usdc_for_one_final_deposit(tmp_path):
    balances = tmp_path / "balances.csv"
    balances.write_text(
        "wallet,chain_id,asset_id,raw_balance,decimals,symbol,status\n"
        + "0x"
        + "1" * 40
        + ",8453,0x833589fcd6edb6e08f4c7c32d4f71b54bda02913,10000,6,USDC,success\n"
    )
    allowlist = tmp_path / "allowlist.json"
    allowlist.write_text(
        '{"assets":[{"chain_id":8453,"asset_id":"0x833589fcd6edb6e08f4c7c32d4f71b54bda02913","action":"swap"}]}'
    )
    targets = (
        BitgetDepositTarget("USDC", 8453, "0x833589fcd6edb6e08f4c7c32d4f71b54bda02913", 9_997),
    )

    plan = create_live_plan(
        balances,
        deposit_addresses={"0x" + "1" * 40: "0x" + "2" * 40},
        allowlist_path=allowlist,
        client=_RecordingQuotes(lambda _request: pytest.fail("direct deposit must not route")),
        targets=targets,
        gas_estimator=lambda **_kwargs: (
            FeeQuote(
                10_000_000_000,
                QuotePrice(AssetIdentity(8453, "native", 18), "2000", NOW),
            ),
        ),
        now_ms=lambda: int(NOW.timestamp() * 1000),
    )

    assert [entry["status"] for entry in plan["entries"]] == ["direct_deposit"]
    assert plan["entries"][0]["steps"][0]["kind"] == "direct_deposit"


def test_live_plan_rejects_route_without_a_complete_gas_estimate(tmp_path):
    balances = tmp_path / "balances.csv"
    balances.write_text(
        "wallet,chain_id,asset_id,raw_balance,decimals,symbol,status\n"
        + "0x"
        + "1" * 40
        + ",10,native,1000000000000000000,18,ETH,success\n"
    )
    allowlist = tmp_path / "allowlist.json"
    allowlist.write_text('{"assets":[{"chain_id":10,"asset_id":"native","action":"swap"}]}')
    target = BitgetDepositTarget("USDC", 8453, "0x833589fcd6edb6e08f4c7c32d4f71b54bda02913", 1)
    route_without_gas = _quoted_route(
        LifiRouteRequest(
            10,
            target.chain_id,
            "0x0000000000000000000000000000000000000000",
            target.asset_id,
            "1000000000000000000",
            "0x" + "1" * 40,
            "0x" + "2" * 40,
        ),
        route_id="missing-gas",
        output_amount=9_000,
        gas_amount=None,
    )
    quotes = _RecordingQuotes(lambda _request: (route_without_gas,))
    plan = create_live_plan(
        balances,
        deposit_addresses={"0x" + "1" * 40: "0x" + "2" * 40},
        allowlist_path=allowlist,
        client=quotes,
        targets=(target,),
        now_ms=lambda: int(NOW.timestamp() * 1000),
    )

    assert len(quotes.requests) == 1
    assert plan["entries"][0]["status"] == "manual_review"
    assert plan["entries"][0]["reason"] == "missing_gas_estimate"


def test_live_plan_quotes_ohno_only_on_blast_with_the_configured_allowlist(tmp_path):
    contract = "0x000000daa580e54635a043d2773f2c698593836a"
    balances = tmp_path / "balances.csv"
    balances.write_text(
        "wallet,chain_id,asset_id,raw_balance,decimals,symbol,status\n"
        + "0x"
        + "1" * 40
        + f",81457,{contract},10000000000000000,18,OHNO,success\n"
        + "0x"
        + "1" * 40
        + f",10,{contract},10000000000000000,18,OHNO,success\n"
    )
    target = BitgetDepositTarget("USDC", 8453, "0x833589fcd6edb6e08f4c7c32d4f71b54bda02913", 1)

    plan = create_live_plan(
        balances,
        deposit_addresses={"0x" + "1" * 40: "0x" + "2" * 40},
        allowlist_path=Path(__file__).parents[1] / "config" / "swap-allowlist.json",
        client=_RecordingQuotes(lambda _request: ()),
        targets=(target,),
    )

    assert [entry["status"] for entry in plan["entries"][:2]] == [
        "denied",
        "manual_review",
    ]


def test_live_plan_queries_each_enabled_bitget_target_without_bridge_mappings(tmp_path):
    balances = _balances(
        tmp_path, chain_id=10, asset_id=SOURCE, raw_balance=2_000_000, decimals=6
    )
    targets = (
        BitgetDepositTarget("USDC", 8453, "0x" + "b" * 40, 1),
        BitgetDepositTarget("USDC", 42161, "0x" + "c" * 40, 1),
    )

    class RecordingQuotes:
        def __init__(self):
            self.requests = []

        def routes(self, request):
            self.requests.append(request)
            return ()

    quotes = RecordingQuotes()
    plan = create_live_plan(
        balances,
        deposit_addresses={WALLET: "0x" + "2" * 40},
        allowlist_path=_asset_allowlist(tmp_path),
        client=quotes,
        targets=targets,
    )

    assert {
        (request.to_chain_id, request.to_token_address) for request in quotes.requests
    } == {
        (target.chain_id, target.asset_id) for target in targets
    } | {(10, "0x0000000000000000000000000000000000000000")}
    assert plan["entries"]


SOURCE = "0x" + "a" * 40
DESTINATION = "0x" + "b" * 40
WALLET = "0x" + "1" * 40


def test_live_plan_leaves_non_stable_at_source_native_pending_actual_output(tmp_path):
    balances = _balances(tmp_path, chain_id=10, asset_id=SOURCE, raw_balance=2_000_000, decimals=6)
    class RecordingQuotes:
        def __init__(self):
            self.requests = []

        def routes(self, request):
            self.requests.append(request)
            return ()

    client = RecordingQuotes()
    plan = create_live_plan(
        balances,
        deposit_addresses={WALLET: "0x" + "2" * 40},
        allowlist_path=_asset_allowlist(tmp_path),
        client=client,
        targets=(BitgetDepositTarget("USDC", 8453, DESTINATION, 1),),
    )

    assert plan["entries"][0]["status"] == "manual_review"
    assert len(client.requests) == 2


def test_live_plan_v4_queries_each_target_and_selects_lowest_total_loss(tmp_path):
    source = AssetIdentity(10, "native", 18)
    base = BitgetDepositTarget("USDC", 8453, "0x" + "b" * 40, 1)
    arbitrum = BitgetDepositTarget("USDC", 42161, "0x" + "c" * 40, 1)
    balances = _balance_rows(
        tmp_path,
        [
            {
                "wallet": WALLET,
                "chain_id": source.chain_id,
                "asset_id": source.contract_address,
                "raw_balance": 10**18,
                "decimals": source.decimals,
                "symbol": "ETH",
                "status": "success",
            }
        ],
    )
    quotes = _RecordingQuotes(
        lambda request: (
            _quoted_route(
                request,
                route_id=f"target-{request.to_chain_id}",
                output_amount=1_990_000_000 if request.to_chain_id == 8453 else 1_950_000_000,
            ),
        )
    )

    plan = create_live_plan(
        balances,
        deposit_addresses={WALLET: "0x" + "d" * 40},
        allowlist_path=_allowlist(tmp_path, [(10, "native", "swap")]),
        client=quotes,
        targets=(base, arbitrum),
        now_ms=lambda: int(NOW.timestamp() * 1000),
        max_route_loss_pct=Decimal("15"),
    )

    assert plan["version"] == 4
    assert {
        (request.to_chain_id, request.to_token_address) for request in quotes.requests
    } == {(base.chain_id, base.asset_id), (arbitrum.chain_id, arbitrum.asset_id)}
    entry = plan["entries"][0]
    assert entry["status"] == "route_ready"
    assert entry["target"]["chain_id"] == base.chain_id
    assert entry["route"]["id"] == "target-8453"
    assert entry["route"]["evidence"]["payload_sha256"]


def test_live_plan_v4_quotes_swap_to_native_then_bridges_actual_minimum_output(tmp_path):
    source = AssetIdentity(10, SOURCE, 6)
    native = AssetIdentity(10, "native", 18)
    target = BitgetDepositTarget("USDC", 8453, DESTINATION, 1)
    balances = _balance_rows(
        tmp_path,
        [
            {
                "wallet": WALLET,
                "chain_id": source.chain_id,
                "asset_id": source.contract_address,
                "raw_balance": 100_000_000,
                "decimals": source.decimals,
                "symbol": "TOKEN",
                "status": "success",
            }
        ],
    )

    def quote(request):
        if request.to_chain_id == source.chain_id:
            assert request.to_token_address == _lifi_address(native)
            assert request.to_address == WALLET
            return (_quoted_route(request, route_id="swap-native", output_amount=5 * 10**16),)
        assert request.from_token_address == _lifi_address(native)
        assert request.from_amount == str(5 * 10**16 - 1)
        assert request.to_chain_id == target.chain_id
        assert request.to_token_address == target.asset_id
        assert request.to_address == "0x" + "d" * 40
        return (_quoted_route(request, route_id="bridge-target", output_amount=96_000_000),)

    quotes = _RecordingQuotes(quote)
    plan = create_live_plan(
        balances,
        deposit_addresses={WALLET: "0x" + "d" * 40},
        allowlist_path=_allowlist(tmp_path, [(10, SOURCE, "swap")]),
        client=quotes,
        targets=(target,),
        gas_estimator=lambda **_kwargs: (),
        now_ms=lambda: int(NOW.timestamp() * 1000),
    )

    assert len(quotes.requests) == 2
    entry = plan["entries"][0]
    assert entry["status"] == "route_ready"
    assert [step["route"]["id"] for step in entry["steps"]] == [
        "swap-native",
        "bridge-target",
    ]
    assert entry["steps"][1]["route"]["from_amount"] == str(5 * 10**16 - 1)


def test_live_plan_group_limit_ignores_denied_review_dust_and_missing_address(tmp_path):
    executable = AssetIdentity(10, SOURCE, 6)
    excluded_review = "0x" + "e" * 40
    excluded_denied = "0x" + "f" * 40
    balances = _balance_rows(
        tmp_path,
        [
            {
                "wallet": WALLET,
                "chain_id": 10,
                "asset_id": executable.contract_address,
                "raw_balance": 100_000_000,
                "decimals": 6,
                "symbol": "TOKEN",
                "status": "success",
            },
            {
                "wallet": WALLET,
                "chain_id": 10,
                "asset_id": excluded_review,
                "raw_balance": 10**15,
                "decimals": 6,
                "symbol": "REVIEW",
                "status": "success",
            },
            {
                "wallet": WALLET,
                "chain_id": 10,
                "asset_id": excluded_denied,
                "raw_balance": 10**15,
                "decimals": 6,
                "symbol": "DENIED",
                "status": "success",
            },
            {
                "wallet": "0x" + "2" * 40,
                "chain_id": 10,
                "asset_id": SOURCE,
                "raw_balance": 1,
                "decimals": 6,
                "symbol": "DUST",
                "status": "success",
            },
            {
                "wallet": "0x" + "3" * 40,
                "chain_id": 10,
                "asset_id": SOURCE,
                "raw_balance": 100_000_000,
                "decimals": 6,
                "symbol": "NO_ADDRESS",
                "status": "success",
            },
        ],
    )
    target = BitgetDepositTarget("USDC", 8453, DESTINATION, 1)
    quotes = _RecordingQuotes(
        lambda request: (_quoted_route(request, route_id="lossy", output_amount=80_000_000),)
    )
    allowlist = _allowlist(
        tmp_path,
        [
            (10, SOURCE, "swap"),
            (10, excluded_review, "review"),
            (10, excluded_denied, "deny"),
        ],
    )

    plan = create_live_plan(
        balances,
        deposit_addresses={WALLET: "0x" + "d" * 40},
        allowlist_path=allowlist,
        quote_floor="0.01",
        client=quotes,
        targets=(target,),
        gas_estimator=lambda **_kwargs: (),
        now_ms=lambda: int(NOW.timestamp() * 1000),
        max_route_loss_pct=Decimal("15"),
    )

    executable_entry = next(item for item in plan["entries"] if item["wallet"] == WALLET)
    assert executable_entry["status"] == "manual_review"
    assert executable_entry["reason"] == "loss_threshold_exceeded"
    assert executable_entry["group"]["source_usd"] == "100"
    assert {item["status"] for item in plan["entries"]} >= {
        "denied",
        "manual_review",
        "dust",
        "missing_deposit_address",
    }
    assert len(quotes.requests) == 2


def test_live_plan_prices_direct_deposit_gas_and_does_not_request_a_route(tmp_path):
    source = AssetIdentity(8453, DESTINATION, 6)
    target = BitgetDepositTarget("USDC", 8453, DESTINATION, 1)
    balances = _balance_rows(
        tmp_path,
        [
            {
                "wallet": WALLET,
                "chain_id": source.chain_id,
                "asset_id": source.contract_address,
                "raw_balance": 100_000_000,
                "decimals": source.decimals,
                "symbol": "USDC",
                "status": "success",
            }
        ],
    )
    quotes = _RecordingQuotes(lambda _request: pytest.fail("direct deposit must not route"))
    gas = FeeQuote(
        1_000_000_000_000_000,
        QuotePrice(AssetIdentity(8453, "native", 18), "2000", NOW),
    )

    plan = create_live_plan(
        balances,
        deposit_addresses={WALLET: "0x" + "d" * 40},
        allowlist_path=_allowlist(tmp_path, [(8453, DESTINATION, "swap")]),
        client=quotes,
        targets=(target,),
        gas_estimator=lambda **_kwargs: (gas,),
        now_ms=lambda: int(NOW.timestamp() * 1000),
    )

    entry = plan["entries"][0]
    assert entry["status"] == "direct_deposit"
    assert entry["valuation"]["loss_usd"] == "2"
    assert entry["reservations"]["final_deposit_native_gas_cap_raw"] == "1000000000000000"
    assert quotes.requests == []


def test_live_plan_stable_swap_targets_the_exact_enabled_bitget_asset(tmp_path):
    source = AssetIdentity(10, SOURCE, 6)
    target = BitgetDepositTarget("USDT", 8453, "0x" + "e" * 40, 1)
    balances = _balance_rows(
        tmp_path,
        [
            {
                "wallet": WALLET,
                "chain_id": source.chain_id,
                "asset_id": source.contract_address,
                "raw_balance": 100_000_000,
                "decimals": source.decimals,
                "symbol": "USDC",
                "status": "success",
            }
        ],
    )
    quotes = _RecordingQuotes(
        lambda request: (
            _quoted_route(
                request,
                route_id="usdc-to-bitget-usdt",
                output_amount=99_000_000,
            ),
        )
    )

    plan = create_live_plan(
        balances,
        deposit_addresses={WALLET: "0x" + "d" * 40},
        allowlist_path=_allowlist(tmp_path, [(10, SOURCE, "swap")]),
        client=quotes,
        targets=(target,),
        gas_estimator=lambda **_kwargs: (),
        now_ms=lambda: int(NOW.timestamp() * 1000),
    )

    assert len(quotes.requests) == 1
    assert quotes.requests[0].to_chain_id == target.chain_id
    assert quotes.requests[0].to_token_address == target.asset_id
    assert plan["entries"][0]["target"]["asset_id"] == target.asset_id


def test_live_plan_includes_approval_gas_before_applying_group_limit(tmp_path):
    source = AssetIdentity(10, SOURCE, 6)
    target = BitgetDepositTarget("USDC", 8453, DESTINATION, 1)
    balances = _balance_rows(
        tmp_path,
        [
            {
                "wallet": WALLET,
                "chain_id": source.chain_id,
                "asset_id": source.contract_address,
                "raw_balance": 100_000_000,
                "decimals": source.decimals,
                "symbol": "USDC",
                "status": "success",
            }
        ],
    )
    route = _quoted_route(
        LifiRouteRequest(
            from_chain_id=source.chain_id,
            to_chain_id=target.chain_id,
            from_token_address=source.contract_address,
            to_token_address=target.asset_id,
            from_amount="100000000",
            from_address=WALLET,
            to_address="0x" + "d" * 40,
        ),
        route_id="approval-required-route",
        output_amount=100_000_000,
        approval_address="0x" + "9" * 40,
    )
    quotes = _RecordingQuotes(lambda _request: (route,))
    callback_calls = []
    approval_gas = FeeQuote(
        10_000_000_000_000_000,
        QuotePrice(AssetIdentity(10, "native", 18), "2000", NOW),
    )

    def gas_estimator(**kwargs):
        callback_calls.append(kwargs)
        return (approval_gas,)

    plan = create_live_plan(
        balances,
        deposit_addresses={WALLET: "0x" + "d" * 40},
        allowlist_path=_allowlist(tmp_path, [(10, SOURCE, "swap")]),
        client=quotes,
        targets=(target,),
        gas_estimator=gas_estimator,
        now_ms=lambda: int(NOW.timestamp() * 1000),
        max_route_loss_pct=Decimal("15"),
    )

    entry = plan["entries"][0]
    assert callback_calls
    assert callback_calls[0]["route"].evidence.route_id == "approval-required-route"
    assert entry["status"] == "manual_review"
    assert entry["reason"] == "loss_threshold_exceeded"
    assert Decimal(entry["valuation"]["wallet_paid_gas_usd"]) >= Decimal("22")


NOW = datetime(2026, 9, 22, 12, tzinfo=UTC)


class _RecordingQuotes:
    def __init__(self, responder):
        self.responder = responder
        self.requests = []

    def routes(self, request):
        self.requests.append(request)
        return tuple(self.responder(request))

    def token_price(self, asset):
        price = "2000" if asset.is_native else "1"
        return LifiPriceEvidence(asset, price, NOW.isoformat())


def _balance_rows(tmp_path, rows):
    path = tmp_path / "balances-v4.csv"
    with path.open("w", newline="") as stream:
        writer = csv.DictWriter(
            stream,
            fieldnames=[
                "wallet",
                "chain_id",
                "asset_id",
                "raw_balance",
                "decimals",
                "symbol",
                "status",
            ],
        )
        writer.writeheader()
        writer.writerows(rows)
    return path


def _allowlist(tmp_path, rows):
    path = tmp_path / "allowlist-v4.json"
    path.write_text(
        json.dumps(
            {
                "assets": [
                    {"chain_id": chain_id, "asset_id": asset_id, "action": action}
                    for chain_id, asset_id, action in rows
                ]
            }
        ),
        encoding="utf-8",
    )
    return path


def _quoted_route(
    request,
    *,
    route_id,
    output_amount,
    gas_amount=10**15,
    approval_address=None,
):
    timestamp = NOW.isoformat().replace("+00:00", "Z")
    source = _asset_from_request_token(
        request.from_chain_id, request.from_token_address
    )
    destination = _asset_from_request_token(
        request.to_chain_id, request.to_token_address
    )
    native = AssetIdentity(request.from_chain_id, "native", 18)
    source_price = "2000" if source.is_native else "1"
    destination_price = "2000" if destination.is_native else "1"
    return _route_from_dict(
        {
            "id": route_id,
            "fromAmount": request.from_amount,
            "toAmount": str(output_amount),
            "toAmountMin": str(output_amount - 1),
            "fromTokenPriceUSD": source_price,
            "toTokenPriceUSD": destination_price,
            "priceTimestamp": timestamp,
            "gasCosts": [
                {
                    "amount": str(gas_amount),
                    "amountUSD": "2",
                    "priceUSD": "2000",
                    "timestamp": timestamp,
                    "token": {
                        "chainId": native.chain_id,
                        "address": _lifi_address(native),
                        "decimals": native.decimals,
                    },
                }
            ] if gas_amount is not None else [],
            "feeCosts": [],
            "steps": [
                {
                    "id": f"step-{route_id}",
                    "type": "cross",
                    "tool": "mock-provider",
                    "action": {
                        "fromChainId": source.chain_id,
                        "toChainId": destination.chain_id,
                        "fromToken": {
                            "address": _lifi_address(source),
                            "decimals": source.decimals,
                            "priceUSD": source_price,
                            "priceTimestamp": timestamp,
                        },
                        "toToken": {
                            "address": _lifi_address(destination),
                            "decimals": destination.decimals,
                            "priceUSD": destination_price,
                            "priceTimestamp": timestamp,
                        },
                        "fromAmount": request.from_amount,
                        "toAddress": request.to_address,
                    },
                    "estimate": {
                        "toAmount": str(output_amount),
                        **({"approvalAddress": approval_address} if approval_address else {}),
                    },
                }
            ],
        },
        quote_timestamp=timestamp,
    )


def _asset_from_request_token(chain_id, token_address):
    if token_address.lower() == "0x0000000000000000000000000000000000000000":
        return AssetIdentity(chain_id, "native", 18)
    return AssetIdentity(chain_id, token_address, 6)


def _lifi_address(asset):
    return (
        "0x0000000000000000000000000000000000000000"
        if asset.is_native
        else asset.contract_address
    )


def _balances(tmp_path, *, chain_id, asset_id, raw_balance, decimals):
    path = tmp_path / "balances.csv"
    path.write_text(
        "wallet,chain_id,asset_id,raw_balance,decimals,symbol,status\n"
        f"{WALLET},{chain_id},{asset_id},{raw_balance},{decimals},USD,success\n"
    )
    return path


def _asset_allowlist(tmp_path):
    path = tmp_path / "allowlist.json"
    path.write_text(
        json.dumps(
            {
                "assets": [{"chain_id": 10, "asset_id": SOURCE, "action": "swap"}],
            }
        )
    )
    return path
