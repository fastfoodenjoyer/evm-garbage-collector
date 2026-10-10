from decimal import Decimal

from test_live_plan import (
    DESTINATION,
    NOW,
    SOURCE,
    WALLET,
    _allowlist,
    _balance_rows,
    _quoted_route,
    _RecordingQuotes,
)

from evm_inventory.bitget_catalog import BitgetDepositTarget
from evm_inventory.live_plan import create_live_plan
from evm_inventory.models import AssetIdentity
from evm_inventory.valuation import FeeQuote, QuotePrice


def plan_for(tmp_path, targets, quotes, gas, **kwargs):
    now_ms = kwargs.pop("now_ms", lambda: int(NOW.timestamp() * 1000))
    return create_live_plan(
        _balance_rows(
            tmp_path,
            [
                dict(
                    wallet=WALLET,
                    chain_id=8453,
                    asset_id=SOURCE,
                    raw_balance="10000000",
                    decimals=6,
                    symbol="USDC",
                    status="success",
                )
            ],
        ),
        deposit_addresses={WALLET: "0x" + "d" * 40},
        allowlist_path=_allowlist(tmp_path, [(8453, SOURCE, "swap")]),
        targets=targets,
        **kwargs,
        client=quotes,
        gas_estimator=gas,
        now_ms=now_ms,
    )


def gas_quote(raw):
    return (FeeQuote(raw, QuotePrice(AssetIdentity(8453, "native", 18), "2000", NOW)),)


def test_configured_subset_keeps_all_routes_for_third_selected_target(tmp_path):
    targets = tuple(
        BitgetDepositTarget("USDC", 10 + i, "0x" + str(i + 3) * 40, 1, decimals=6) for i in range(5)
    )

    def respond(req):
        return tuple(
            _quoted_route(
                req,
                route_id=f"{req.to_chain_id}-{i}",
                output_amount=9_000_000 if req.to_chain_id != 14 or i < 2 else 9_999_999,
                gas_amount=1,
            )
            for i in range(3)
        )

    quotes = _RecordingQuotes(respond)
    prepared = []

    def gas(**kw):
        prepared.append(kw["route"].route_id)
        return gas_quote(1)

    from evm_inventory.route_targets import TargetPolicy

    plan = plan_for(
        tmp_path, targets, quotes, gas, target_policy=TargetPolicy.from_targets(targets[2:])
    )
    assert len(quotes.requests) == 3
    assert plan["entries"][0]["route"]["id"] == "14-2"
    assert prepared == ["14-2"]
    assert plan["entries"][0]["search"]["complete"] is True


def test_reprice_first_candidate_and_prepare_possible_challenger(tmp_path):
    target = BitgetDepositTarget("USDC", 10, DESTINATION, 1, decimals=6)
    quotes = _RecordingQuotes(
        lambda req: tuple(
            _quoted_route(req, route_id=k, output_amount=v, gas_amount=1)
            for k, v in [("a", 10_000_000), ("b", 9_990_000), ("c", 8_000_000)]
        )
    )
    prepared = []

    def gas(**kw):
        route = kw["route"].route_id
        prepared.append(route)
        return gas_quote(10**14 if route == "a" else 1)

    p = plan_for(tmp_path, (target,), quotes, gas)
    assert p["entries"][0]["route"]["id"] == "b"
    assert prepared == ["a", "b"]


def test_failed_quote_does_not_claim_best_even_with_valid_candidate(tmp_path):
    targets = tuple(
        BitgetDepositTarget("USDC", 10 + i, DESTINATION, 1, decimals=6) for i in range(2)
    )

    def respond(req):
        if req.to_chain_id == 11:
            raise TimeoutError("network timeout")
        return (_quoted_route(req, route_id="available", output_amount=10_000_000, gas_amount=1),)

    p = plan_for(tmp_path, targets, _RecordingQuotes(respond), lambda **kw: gas_quote(1))
    assert p["entries"][0]["status"] == "manual_review"
    assert p["entries"][0]["reason"] == "route_search_incomplete"
    assert p["entries"][0]["search"]["complete"] is False


def test_choice_matches_exhaustive_cost_comparison(tmp_path):
    targets = tuple(
        BitgetDepositTarget("USDC", 10 + i, "0x" + str(i + 3) * 40, 1, decimals=6) for i in range(3)
    )
    outputs = {f"{10 + i}-{j}": 10_000_000 - i * 700 - j * 110 for i in range(3) for j in range(4)}
    gas = {key: (int(key.split("-")[0]) % 3) * 10**13 + int(key[-1]) * 10**11 for key in outputs}
    prepared = []

    def respond(req):
        return tuple(
            _quoted_route(
                req,
                route_id=f"{req.to_chain_id}-{j}",
                output_amount=outputs[f"{req.to_chain_id}-{j}"],
                gas_amount=1,
            )
            for j in range(4)
        )

    def estimate(**kw):
        key = kw["route"].route_id
        prepared.append(key)
        return gas_quote(gas[key])

    plan = plan_for(tmp_path, targets, _RecordingQuotes(respond), estimate)
    expected = min(
        outputs,
        key=lambda key: (
            (Decimal(10_000_000 - outputs[key] + 1) / 10**6)
            + Decimal(gas[key] + 1) * 2000 / 10**18,
            key,
        ),
    )
    assert plan["entries"][0]["route"]["id"] == expected
    assert plan["entries"][0]["search"]["complete"] is True
    assert len(prepared) < len(outputs)


def test_unknown_gas_does_not_discard_possible_winner(tmp_path):
    target = BitgetDepositTarget("USDC", 10, DESTINATION, 1, decimals=6)
    quotes = _RecordingQuotes(
        lambda req: tuple(
            _quoted_route(req, route_id=k, output_amount=v, gas_amount=1)
            for k, v in [("unknown", 10_000_000), ("known", 9_999_000)]
        )
    )

    def estimate(**kw):
        return None if kw["route"].route_id == "unknown" else gas_quote(1)

    plan = plan_for(tmp_path, (target,), quotes, estimate)
    entry = plan["entries"][0]
    assert entry["reason"] == "route_search_incomplete"
    assert entry["search"]["complete"] is False
    assert "best_candidate" not in entry
    assert "missing_gas_estimate" in entry["search"]["errors"]


def test_nonstable_asset_requests_only_three_configured_pairs(tmp_path):
    from evm_inventory.route_targets import TargetPolicy

    targets = tuple(BitgetDepositTarget("USDC", c, DESTINATION, 1) for c in (10, 137, 42161))
    rows = [
        dict(
            wallet=WALLET,
            chain_id=8453,
            asset_id=SOURCE,
            raw_balance="10000000",
            decimals=6,
            symbol="OTHER",
            status="success",
        )
    ]
    quotes = _RecordingQuotes(
        lambda r: (_quoted_route(r, route_id=str(r.to_chain_id), output_amount=9_999_000),)
    )
    p = create_live_plan(
        _balance_rows(tmp_path, rows),
        deposit_addresses={WALLET: "0x" + "d" * 40},
        allowlist_path=_allowlist(tmp_path, [(8453, SOURCE, "swap")]),
        targets=targets,
        target_policy=TargetPolicy.from_targets(targets),
        client=quotes,
        gas_estimator=lambda **kw: gas_quote(1),
        now_ms=lambda: int(NOW.timestamp() * 1000),
    )
    assert len(quotes.requests) == 3
    assert all(r.from_token_address == SOURCE for r in quotes.requests)
    assert p["search_scope"]["global_cheapest"] is False
    assert p["entries"][0]["search"]["complete"] is True


def test_direct_target_is_kept_outside_configured_route_targets(tmp_path):
    from evm_inventory.route_targets import TargetPolicy

    direct = BitgetDepositTarget("USDC", 8453, SOURCE, 1)
    alternative = BitgetDepositTarget("USDC", 10, DESTINATION, 1)
    quotes = _RecordingQuotes(lambda r: ())
    p = create_live_plan(
        _balance_rows(
            tmp_path,
            [
                dict(
                    wallet=WALLET,
                    chain_id=8453,
                    asset_id=SOURCE,
                    raw_balance="10000000",
                    decimals=6,
                    symbol="USDC",
                    status="success",
                )
            ],
        ),
        deposit_addresses={WALLET: "0x" + "d" * 40},
        allowlist_path=_allowlist(tmp_path, [(8453, SOURCE, "swap")]),
        targets=(direct, alternative),
        target_policy=TargetPolicy.from_targets((alternative,)),
        client=quotes,
        gas_estimator=lambda **kw: gas_quote(1),
        now_ms=lambda: int(NOW.timestamp() * 1000),
    )
    assert p["entries"][0]["status"] == "direct_deposit"
    assert len(quotes.requests) == 1
    assert p["entries"][0]["target"]["chain_id"] == 8453


def test_quota_wait_retains_retry_and_provider_diagnostic(tmp_path):
    from evm_inventory.lifi_quota import LifiRequestLimit

    target = BitgetDepositTarget("USDC", 10, DESTINATION, 1, decimals=6)

    def respond(req):
        raise LifiRequestLimit("routes", retry_at=1770000000, diagnostic={"remaining": 5})

    plan = plan_for(tmp_path, (target,), _RecordingQuotes(respond), lambda **kw: gas_quote(1))
    entry = plan["entries"][0]
    assert entry["group"]["reason"] == "quota_wait"
    assert entry["search"]["pending"][0]["retry_at"] == 1770000000
    diagnostic = entry["search"]["pending"][0]["diagnostic"]
    assert diagnostic["exceptions"][0]["provider"]["remaining"] == 5


def _weth_plan(tmp_path, gas, *, bridge_output=1_999_000_000, now_ms=None):
    import json

    from evm_inventory.lifi import LifiPriceEvidence, _route_from_dict
    from evm_inventory.route_targets import TargetPolicy
    from evm_inventory.weth import WETH_CONTRACTS

    targets = tuple(
        BitgetDepositTarget("USDC", chain, DESTINATION, 1, decimals=6) for chain in (10, 137, 42161)
    )
    native_target = BitgetDepositTarget("ETH", 8453, "native", 1, "Base", 18)

    def respond(request):
        quoted = _quoted_route(
            request,
            route_id=f"bridge-{request.to_chain_id}",
            output_amount=bridge_output,
            gas_amount=1,
        )
        payload = json.loads(quoted.evidence.canonical_payload_json)
        payload["steps"][0]["action"]["fromToken"].update(decimals=18, priceUSD="2000")
        payload["fromTokenPriceUSD"] = "2000"
        return (_route_from_dict(payload, quote_timestamp=NOW.isoformat()),)

    class Quotes(_RecordingQuotes):
        def token_price(self, asset):
            price = (
                "2000" if asset.is_native or asset.contract_address == WETH_CONTRACTS[8453] else "1"
            )
            return LifiPriceEvidence(asset, price, NOW.isoformat())

    quotes = Quotes(respond)
    plan = create_live_plan(
        _balance_rows(
            tmp_path,
            [
                dict(
                    wallet=WALLET,
                    chain_id=8453,
                    asset_id=WETH_CONTRACTS[8453],
                    raw_balance=str(10**18),
                    decimals=18,
                    symbol="WETH",
                    status="success",
                )
            ],
        ),
        deposit_addresses={WALLET: "0x" + "d" * 40},
        allowlist_path=_allowlist(tmp_path, [(8453, WETH_CONTRACTS[8453], "unwrap")]),
        targets=(*targets, native_target),
        target_policy=TargetPolicy.from_targets(targets),
        client=quotes,
        gas_estimator=gas,
        now_ms=now_ms or (lambda: int(NOW.timestamp() * 1000)),
    )
    return plan, quotes


def test_canonical_unwrap_competes_with_three_routes_without_a_fourth_request(tmp_path):
    prepared = []

    def gas(**kwargs):
        prepared.append(kwargs["purpose"])
        return gas_quote(10**14)

    plan, quotes = _weth_plan(tmp_path, gas)
    entry = plan["entries"][0]
    assert len(quotes.requests) == 3
    assert all(request.to_address != WALLET for request in quotes.requests)
    assert entry["status"] == "route_ready"
    assert entry["local_unwrap"] is True
    assert entry["steps"][0]["kind"] == "unwrap_to_native"
    assert entry["target"]["asset_id"] == "native"
    assert entry["target"]["chain_id"] == 8453
    assert Decimal(entry["valuation"]["loss_usd"]) == Decimal("0.4")
    assert prepared == ["unwrap_to_native", "direct_deposit"]


def test_expensive_canonical_unwrap_can_lose_to_cheaper_bridge(tmp_path):
    prepared = []

    def gas(**kwargs):
        prepared.append(kwargs["purpose"])
        return gas_quote(10**15 if kwargs["purpose"] == "unwrap_to_native" else 1)

    plan, quotes = _weth_plan(tmp_path, gas)
    entry = plan["entries"][0]
    assert len(quotes.requests) == 3
    assert entry["status"] == "route_ready"
    assert not entry.get("local_unwrap")
    assert entry["steps"][0]["kind"] == "bridge"
    assert prepared[:2] == ["unwrap_to_native", "direct_deposit"]
    assert "bridge" in prepared


def test_missing_dependent_native_deposit_gas_keeps_search_incomplete(tmp_path):
    def gas(**kwargs):
        return None if kwargs["purpose"] == "direct_deposit" else gas_quote(1)

    plan, quotes = _weth_plan(tmp_path, gas)
    entry = plan["entries"][0]
    assert len(quotes.requests) == 3
    assert entry["status"] == "manual_review"
    assert entry["search"]["complete"] is False
    assert "missing_gas_estimate" in entry["search"]["errors"]
    assert "best_candidate" not in entry


def test_no_enabled_route_or_native_target_reports_target_unavailability(tmp_path):
    from evm_inventory.route_targets import TargetPolicy

    unavailable = BitgetDepositTarget("USDC", 10, DESTINATION, 1, decimals=6)
    quotes = _RecordingQuotes(lambda request: ())
    plan = plan_for(
        tmp_path,
        (),
        quotes,
        lambda **kwargs: gas_quote(1),
        target_policy=TargetPolicy.from_targets((unavailable,)),
    )
    entry = plan["entries"][0]
    assert quotes.requests == []
    assert entry["status"] == "manual_review"
    assert entry["search"]["errors"] == ["no_enabled_deposit_target"]


def test_final_group_gate_rejects_original_route_quote_aged_during_gas_refinement(tmp_path):
    from datetime import timedelta

    current = NOW

    def gas(**kwargs):
        nonlocal current
        current = NOW + timedelta(seconds=270)
        return gas_quote(1)

    plan, _ = _weth_plan(tmp_path, gas, now_ms=lambda: int(current.timestamp() * 1000))
    entry = plan["entries"][0]
    assert entry["status"] == "manual_review"
    assert entry["search"]["complete"] is False
    assert "route_quote_stale" in entry["search"]["errors"]
    assert "best_candidate" not in entry


def test_final_plan_gate_invalidates_earlier_wallet_after_later_wallet_delays(tmp_path):
    from datetime import timedelta

    later_wallet = "0x" + "2" * 40
    current = NOW
    target = BitgetDepositTarget("USDC", 8453, SOURCE, 1, decimals=6)

    def gas(**kwargs):
        nonlocal current
        if kwargs["wallet"] == later_wallet:
            current = NOW + timedelta(seconds=270)
        return gas_quote(1)

    plan = create_live_plan(
        _balance_rows(
            tmp_path,
            [
                dict(
                    wallet=wallet,
                    chain_id=8453,
                    asset_id=SOURCE,
                    raw_balance="10000000",
                    decimals=6,
                    symbol="USDC",
                    status="success",
                )
                for wallet in (WALLET, later_wallet)
            ],
        ),
        deposit_addresses={wallet: "0x" + "d" * 40 for wallet in (WALLET, later_wallet)},
        allowlist_path=_allowlist(tmp_path, [(8453, SOURCE, "swap")]),
        targets=(target,),
        client=_RecordingQuotes(lambda request: ()),
        gas_estimator=gas,
        now_ms=lambda: int(current.timestamp() * 1000),
    )
    earlier = next(entry for entry in plan["entries"] if entry["wallet"] == WALLET)
    assert earlier["status"] == "manual_review"
    assert earlier["reason"] == "stale_quote"
    assert earlier["search"]["complete"] is False
    assert earlier["provisional_candidate"]
    assert not any(key in earlier for key in ("steps", "target", "valuation", "reservations"))


def test_stale_pruned_lower_bound_invalidates_an_otherwise_fresh_winner(tmp_path):
    import json
    from datetime import timedelta

    from evm_inventory.lifi import _route_from_dict

    current = NOW
    target = BitgetDepositTarget("USDC", 10, DESTINATION, 1, decimals=6)

    def respond(request):
        winner = _quoted_route(request, route_id="winner", output_amount=10_000_000, gas_amount=1)
        loser = _quoted_route(request, route_id="pruned", output_amount=8_000_000, gas_amount=1)
        loser = _route_from_dict(
            json.loads(loser.evidence.canonical_payload_json),
            quote_timestamp=(NOW - timedelta(seconds=200)).isoformat(),
        )
        return winner, loser

    def gas(**kwargs):
        nonlocal current
        current = NOW + timedelta(seconds=50)
        return gas_quote(1)

    plan = plan_for(
        tmp_path,
        (target,),
        _RecordingQuotes(respond),
        gas,
        now_ms=lambda: int(current.timestamp() * 1000),
    )
    entry = plan["entries"][0]
    assert entry["search"]["pruned"] == 1
    assert entry["status"] == "manual_review"
    assert entry["reason"] == "stale_quote"
    assert "provisional_candidate" in entry
    assert "steps" not in entry


def test_original_empty_response_timestamp_expires_comparison(tmp_path):
    from datetime import timedelta

    current = NOW
    targets = tuple(
        BitgetDepositTarget("USDC", chain, DESTINATION, 1, decimals=6) for chain in (10, 137)
    )
    quotes = _RecordingQuotes(lambda request: ())
    quotes.route_observed_at = {}

    def respond(request):
        if request.to_chain_id == 137:
            quotes.route_observed_at[request] = (NOW - timedelta(seconds=200)).isoformat()
            return ()
        quotes.route_observed_at[request] = NOW.isoformat()
        return (_quoted_route(request, route_id="winner", output_amount=10_000_000, gas_amount=1),)

    quotes.responder = respond

    def gas(**kwargs):
        nonlocal current
        current = NOW + timedelta(seconds=50)
        return gas_quote(1)

    plan = plan_for(tmp_path, targets, quotes, gas, now_ms=lambda: int(current.timestamp() * 1000))
    entry = plan["entries"][0]
    assert entry["status"] == "manual_review"
    assert entry["reason"] == "stale_quote"
    assert entry["search"]["evidence_expires_at"] == (NOW + timedelta(seconds=40)).isoformat()


def test_stale_pruned_destination_price_invalidates_fresh_route_timestamps(tmp_path):
    import json
    from datetime import timedelta

    from evm_inventory.lifi import _route_from_dict

    current = NOW
    target = BitgetDepositTarget("USDC", 10, DESTINATION, 1, decimals=6)

    def respond(request):
        winner = _quoted_route(request, route_id="winner", output_amount=10_000_000, gas_amount=1)
        loser = _quoted_route(request, route_id="pruned", output_amount=8_000_000, gas_amount=1)
        payload = json.loads(loser.evidence.canonical_payload_json)
        payload["steps"][0]["action"]["toToken"]["priceTimestamp"] = (
            NOW - timedelta(seconds=250)
        ).isoformat()
        payload["priceTimestamp"] = (NOW - timedelta(seconds=250)).isoformat()
        return winner, _route_from_dict(payload, quote_timestamp=NOW.isoformat())

    def gas(**kwargs):
        nonlocal current
        current = NOW + timedelta(seconds=60)
        return gas_quote(1)

    plan = plan_for(
        tmp_path,
        (target,),
        _RecordingQuotes(respond),
        gas,
        now_ms=lambda: int(current.timestamp() * 1000),
    )
    entry = plan["entries"][0]
    assert entry["search"]["pruned"] == 1
    assert entry["reason"] == "stale_quote"
    assert entry["search"]["evidence_expires_at"] == (NOW + timedelta(seconds=50)).isoformat()


def test_pruned_external_destination_price_expiry_is_retained(tmp_path):
    from datetime import timedelta

    from evm_inventory.lifi import LifiPriceEvidence

    current = NOW
    targets = (
        BitgetDepositTarget("USDC", 10, DESTINATION, 1, decimals=6),
        BitgetDepositTarget("ETH", 1, "native", 1, decimals=18),
    )

    class Quotes(_RecordingQuotes):
        def token_price(self, asset):
            if asset.chain_id == 1 and asset.is_native:
                return LifiPriceEvidence(asset, "2000", (NOW - timedelta(seconds=250)).isoformat())
            return super().token_price(asset)

    quotes = Quotes(
        lambda req: (
            _quoted_route(
                req,
                route_id=str(req.to_chain_id),
                output_amount=10_000_000 if req.to_chain_id == 10 else 2_000_000_000_000_000,
                gas_amount=1,
            ),
        )
    )

    def gas(**kw):
        nonlocal current
        current = NOW + timedelta(seconds=60)
        return gas_quote(1)

    plan = plan_for(tmp_path, targets, quotes, gas, now_ms=lambda: int(current.timestamp() * 1000))
    assert plan["entries"][0]["reason"] == "stale_quote"
    assert plan["entries"][0]["search"]["complete"] is False
