import threading
import time
from collections import Counter

from test_live_plan import (
    DESTINATION,
    NOW,
    SOURCE,
    WALLET,
    _allowlist,
    _balance_rows,
    _quoted_route,
)

from evm_inventory.bitget_catalog import BitgetDepositTarget
from evm_inventory.lifi import LifiPriceEvidence
from evm_inventory.live_plan import create_live_plan
from evm_inventory.models import AssetIdentity
from evm_inventory.valuation import FeeQuote, QuotePrice


def _rows(chains):
    return [
        dict(
            wallet=WALLET,
            chain_id=chain,
            asset_id=SOURCE,
            raw_balance=100_000_000,
            decimals=6,
            symbol="USDC",
            status="success",
        )
        for chain in chains
    ]


def test_targets_and_groups_run_concurrently_and_prices_are_singleflight(tmp_path):
    active = 0
    peak = 0
    prices = Counter()
    lock = threading.Lock()

    class Quotes:
        def token_price(self, asset):
            with lock:
                prices[asset] += 1
            time.sleep(0.015)
            return LifiPriceEvidence(asset, "2000" if asset.is_native else "1", NOW.isoformat())

        def routes(self, request):
            nonlocal active, peak
            with lock:
                active += 1
                peak = max(peak, active)
            time.sleep(0.06)
            with lock:
                active -= 1
            return (
                _quoted_route(
                    request,
                    route_id=f"{request.from_chain_id}-{request.to_chain_id}",
                    output_amount=99_000_000,
                ),
            )

    targets = tuple(
        BitgetDepositTarget("USDC", chain, DESTINATION, 1) for chain in [8453, 42161, 137, 56]
    )
    start = time.monotonic()
    plan = create_live_plan(
        _balance_rows(tmp_path, _rows([10, 1])),
        deposit_addresses={WALLET: "0x" + "d" * 40},
        allowlist_path=_allowlist(tmp_path, [(c, SOURCE, "swap") for c in [10, 1]]),
        client=Quotes(),
        targets=targets,
        stage_seconds=2,
        gas_estimator=lambda **kw: (
            FeeQuote(
                1000,
                QuotePrice(AssetIdentity(kw["source_asset"].chain_id, "native", 18), "2000", NOW),
            ),
        ),
        now_ms=lambda: int(NOW.timestamp() * 1000),
    )
    assert peak >= 4
    assert time.monotonic() - start < 0.4
    assert [entry["status"] for entry in plan["entries"]] == ["route_ready", "route_ready"]
    assert max(prices.values()) == 1


def test_deadline_keeps_completed_groups_and_rejects_entire_incomplete_group(tmp_path):
    class Quotes:
        def token_price(self, asset):
            if asset.chain_id == 10:
                time.sleep(0.4)
            return LifiPriceEvidence(asset, "1", NOW.isoformat())

        def routes(self, request):
            return (_quoted_route(request, route_id="ready", output_amount=99_000_000),)

    start = time.monotonic()
    plan = create_live_plan(
        _balance_rows(tmp_path, _rows([10, 1])),
        deposit_addresses={WALLET: "0x" + "d" * 40},
        allowlist_path=_allowlist(tmp_path, [(c, SOURCE, "swap") for c in [10, 1]]),
        client=Quotes(),
        targets=(BitgetDepositTarget("USDC", 8453, DESTINATION, 1),),
        stage_seconds=0.12,
        gas_estimator=lambda **kw: (),
        now_ms=lambda: int(NOW.timestamp() * 1000),
    )
    assert time.monotonic() - start < 0.3
    by_chain = {entry["chain_id"]: entry for entry in plan["entries"]}
    assert by_chain[1]["status"] == "route_ready"
    assert by_chain[10]["reason"] == "stage_deadline_exceeded"


def test_price_cache_keeps_full_asset_identity(tmp_path):
    class WrongPrices:
        def token_price(self, asset):
            return LifiPriceEvidence(
                AssetIdentity(asset.chain_id, asset.contract_address, 18), "1", NOW.isoformat()
            )

        def routes(self, request):
            return ()

    plan = create_live_plan(
        _balance_rows(tmp_path, _rows([10])),
        deposit_addresses={WALLET: "0x" + "d" * 40},
        allowlist_path=_allowlist(tmp_path, [(10, SOURCE, "swap")]),
        client=WrongPrices(),
        targets=(BitgetDepositTarget("USDC", 8453, DESTINATION, 1),),
        now_ms=lambda: int(NOW.timestamp() * 1000),
    )
    assert plan["entries"][0]["reason"] == "missing_source_price"


def test_unfinished_target_invalidates_all_positions_in_its_group(tmp_path):
    other = "0x" + "e" * 40
    rows = _rows([10, 10])
    rows[1]["asset_id"] = other

    class Quotes:
        def token_price(self, asset):
            return LifiPriceEvidence(asset, "1", NOW.isoformat())

        def routes(self, request):
            if request.to_chain_id == 42161:
                time.sleep(0.4)
            return (_quoted_route(request, route_id="complete", output_amount=99_000_000),)

    plan = create_live_plan(
        _balance_rows(tmp_path, rows),
        deposit_addresses={WALLET: "0x" + "d" * 40},
        allowlist_path=_allowlist(tmp_path, [(10, SOURCE, "swap"), (10, other, "swap")]),
        client=Quotes(),
        targets=(
            BitgetDepositTarget("USDC", 8453, DESTINATION, 1),
            BitgetDepositTarget("USDC", 42161, DESTINATION, 1),
        ),
        stage_seconds=0.12,
        gas_estimator=lambda **kw: (),
        now_ms=lambda: int(NOW.timestamp() * 1000),
    )
    assert len(plan["entries"]) == 2
    assert all(entry["reason"] == "stage_deadline_exceeded" for entry in plan["entries"])
    assert all(entry["status"] == "manual_review" for entry in plan["entries"])


def test_blocked_catalog_is_included_in_quote_deadline(tmp_path, monkeypatch):
    def blocked_catalog():
        time.sleep(0.4)
        return ()

    monkeypatch.setattr("evm_inventory.live_plan.fetch_public_coins", blocked_catalog)
    start = time.monotonic()
    plan = create_live_plan(
        _balance_rows(tmp_path, _rows([10])),
        deposit_addresses={WALLET: "0x" + "d" * 40},
        allowlist_path=_allowlist(tmp_path, [(10, SOURCE, "swap")]),
        stage_seconds=0.1,
    )
    assert time.monotonic() - start < 0.3
    assert plan["entries"][0]["reason"] == "stage_deadline_exceeded"


def test_exact_price_singleflight_deduplicates_parallel_requests():
    from evm_inventory.quote_parallel import CachedQuoteClient, complete_parallel
    from evm_inventory.stages import Deadline, deadline_scope

    calls = Counter()
    lock = threading.Lock()

    class Quotes:
        def token_price(self, asset):
            with lock:
                calls[asset] += 1
            time.sleep(0.025)
            return LifiPriceEvidence(asset, "1", NOW.isoformat())

    client = CachedQuoteClient(Quotes(), workers=8)
    six = AssetIdentity(10, SOURCE, 6)
    eighteen = AssetIdentity(10, SOURCE, 18)
    with deadline_scope(Deadline(1, "quote")):
        prices = complete_parallel(client.token_price, [six] * 6 + [eighteen] * 2, workers=8)
    assert calls == {six: 1, eighteen: 1}
    assert [price.asset for price in prices] == [six] * 6 + [eighteen] * 2


def test_direct_positions_share_native_gas_prices_without_mutating_estimator(tmp_path):
    from evm_inventory.planner_gas import PlannerGasEstimator

    other = "0x" + "e" * 40
    rows = _rows([10, 10])
    rows[1]["asset_id"] = other
    calls = Counter()
    lock = threading.Lock()

    class Quotes:
        def token_price(self, asset):
            with lock:
                calls[asset] += 1
            time.sleep(0.015)
            return LifiPriceEvidence(asset, "2000" if asset.is_native else "1", NOW.isoformat())

        def routes(self, request):
            raise AssertionError("direct deposits must not request a route")

    class Gas(PlannerGasEstimator):
        def __call__(self, **kwargs):
            native = AssetIdentity(kwargs["source_asset"].chain_id, "native", 18)
            evidence = self.price_client.token_price(native)
            return (FeeQuote(1000, QuotePrice(native, evidence.price_usd, NOW)),)

    quotes = Quotes()
    estimator = Gas(None, {}, quotes)
    plan = create_live_plan(
        _balance_rows(tmp_path, rows),
        deposit_addresses={WALLET: "0x" + "d" * 40},
        allowlist_path=_allowlist(tmp_path, [(10, SOURCE, "swap"), (10, other, "swap")]),
        client=quotes,
        targets=(
            BitgetDepositTarget("USDC", 10, SOURCE, 1),
            BitgetDepositTarget("USDC", 10, other, 1),
        ),
        gas_estimator=estimator,
        now_ms=lambda: int(NOW.timestamp() * 1000),
    )
    assert all(entry["status"] == "direct_deposit" for entry in plan["entries"])
    assert calls[AssetIdentity(10, "native", 18)] == 1
    assert estimator.price_client is quotes
