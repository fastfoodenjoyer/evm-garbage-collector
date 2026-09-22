from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from evm_inventory.consolidation import (
    admit_group,
    candidate_from_route,
    candidate_from_values,
    combine_candidates,
    select_candidate,
    select_candidate_pair,
)
from evm_inventory.lifi import (
    LifiGasCost,
    _route_from_dict,
)
from evm_inventory.models import AssetIdentity
from evm_inventory.valuation import FeeQuote, QuotePrice

NOW = datetime(2026, 9, 22, 12, tzinfo=UTC)
SOURCE = AssetIdentity(1, "0x" + "a" * 40, 6)
INTERMEDIATE = AssetIdentity(10, "0x" + "b" * 40, 6)
DESTINATION = AssetIdentity(8453, "0x" + "c" * 40, 6)
THIRD_CHAIN_GAS = AssetIdentity(42161, "native", 18)
SOURCE_GAS = AssetIdentity(1, "native", 18)
RECIPIENT = "0x" + "d" * 40


def _price(asset: AssetIdentity, usd: str = "1") -> QuotePrice:
    return QuotePrice(asset, usd, NOW)


def _priced_gas(
    asset: AssetIdentity = SOURCE_GAS,
    amount: int = 10**15,
    usd: str = "1",
) -> tuple[FeeQuote, ...]:
    return (FeeQuote(amount, _price(asset, usd)),)


def _values_candidate(**kwargs):
    arguments = {
        "wallet_paid_gas": _priced_gas(),
        "gas_estimate_complete": True,
        "now": NOW,
        "max_price_age": timedelta(minutes=5),
    }
    arguments.update(kwargs)
    return candidate_from_values(**arguments)


def _route_candidate(route, **kwargs):
    arguments = {
        "expected_source": SOURCE,
        "expected_destination": DESTINATION,
        "expected_input_amount": 100_000_000,
        "recipient": RECIPIENT,
        "gas_estimate_complete": True,
        "now": NOW,
        "max_price_age": timedelta(minutes=5),
    }
    arguments.update(kwargs)
    return candidate_from_route(route, **arguments)


def _candidate(
    *,
    route_id: str,
    destination: AssetIdentity = DESTINATION,
    loss: str,
    source_usd: str = "100",
):
    gas_usd = Decimal("0.001")
    return _values_candidate(
        route_ids=(route_id,),
        source_asset=SOURCE,
        destination_asset=destination,
        source_amount=int(Decimal(source_usd) * 1_000_000),
        destination_amount=int(
            (Decimal(source_usd) - Decimal(loss) + gas_usd) * 1_000_000
        ),
        source_price=_price(SOURCE),
        destination_price=_price(destination),
    )


def _route(
    *,
    prices: tuple[tuple[AssetIdentity, str], ...] | None = None,
    gas_costs: tuple[LifiGasCost, ...] | None = None,
    costs_complete: bool = True,
    quote_timestamp: str = NOW.isoformat(),
):
    prices = prices or ((SOURCE, "1"), (DESTINATION, "1"))
    source_prices = [price for asset, price in prices if asset == SOURCE]
    destination_prices = [price for asset, price in prices if asset == DESTINATION]
    actual_gas_costs = gas_costs
    if actual_gas_costs is None:
        actual_gas_costs = (LifiGasCost(10**15, "0.001", SOURCE_GAS, "1", quote_timestamp),)
    raw_gas_costs = [
        {
            "amount": str(cost.amount),
            "amountUSD": cost.amount_usd,
            "priceUSD": cost.price_usd,
            "timestamp": cost.price_timestamp or quote_timestamp,
            "token": {
                "chainId": cost.token.chain_id,
                "address": (
                    "0x0000000000000000000000000000000000000000"
                    if cost.token.is_native
                    else cost.token.contract_address
                ),
                "decimals": cost.token.decimals,
            },
        }
        for cost in actual_gas_costs
        if cost.token is not None
    ]
    fee_costs = [] if not costs_complete or not destination_prices else [
        {
            "amount": "9000000",
            "amountUSD": "9",
            "priceUSD": "1",
            "timestamp": quote_timestamp,
            "token": {
                "chainId": DESTINATION.chain_id,
                "address": DESTINATION.contract_address,
                "decimals": DESTINATION.decimals,
            },
        }
    ]
    step = {
        "id": "step-1",
        "type": "cross",
        "tool": "example-provider",
        "action": {
            "fromChainId": SOURCE.chain_id,
            "toChainId": DESTINATION.chain_id,
            "fromToken": {
                "address": SOURCE.contract_address,
                "decimals": SOURCE.decimals,
                "priceUSD": source_prices[-1] if source_prices else None,
                "priceTimestamp": quote_timestamp,
            },
            "toToken": {
                "address": DESTINATION.contract_address,
                "decimals": DESTINATION.decimals,
                "priceUSD": destination_prices[0] if destination_prices else None,
                "priceTimestamp": quote_timestamp,
            },
            "fromAmount": "100000000",
            "toAddress": RECIPIENT,
        },
        "estimate": {
            "toAmount": "98000000",
            "feeCosts": fee_costs,
        },
    }
    if not costs_complete:
        child = {
            **step,
            "id": "nested-step",
            "estimate": {
                "toAmount": "98000000",
                "gasCosts": [
                    {
                        "amount": "1",
                        "token": {
                            "chainId": SOURCE_GAS.chain_id,
                            "address": "0x0000000000000000000000000000000000000000",
                            "decimals": SOURCE_GAS.decimals,
                        },
                    }
                ],
                "feeCosts": [],
            },
        }
        step["includedSteps"] = [child]
        raw_gas_costs = []
    payload = {
        "id": "route-1",
        "fromAmount": "100000000",
        "toAmount": "98000000",
        "toAmountMin": "97000000",
        "fromTokenPriceUSD": source_prices[0] if source_prices else None,
        "toTokenPriceUSD": destination_prices[0] if destination_prices else None,
        "priceTimestamp": quote_timestamp,
        "gasCosts": raw_gas_costs,
        "feeCosts": fee_costs,
        "steps": [step],
    }
    return _route_from_dict(payload, quote_timestamp=quote_timestamp)


def _multi_step_route():
    first = {
        "id": "swap-step",
        "type": "swap",
        "tool": "swap-provider",
        "action": {
            "fromChainId": SOURCE.chain_id,
            "toChainId": INTERMEDIATE.chain_id,
            "fromToken": {"address": SOURCE.contract_address, "decimals": 6},
            "toToken": {"address": INTERMEDIATE.contract_address, "decimals": 6},
            "fromAmount": "100000000",
            "toAddress": RECIPIENT,
        },
        "estimate": {"toAmount": "99000000"},
    }
    second = {
        "id": "bridge-step",
        "type": "cross",
        "tool": "bridge-provider",
        "action": {
            "fromChainId": INTERMEDIATE.chain_id,
            "toChainId": DESTINATION.chain_id,
            "fromToken": {"address": INTERMEDIATE.contract_address, "decimals": 6},
            "toToken": {"address": DESTINATION.contract_address, "decimals": 6},
            "fromAmount": "99000000",
            "toAddress": RECIPIENT,
        },
        "estimate": {"toAmount": "98000000"},
    }
    payload = {
        "id": "multi-step-route",
        "fromAmount": "100000000",
        "toAmount": "98000000",
        "toAmountMin": "97000000",
        "fromTokenPriceUSD": "1",
        "toTokenPriceUSD": "1",
        "priceTimestamp": NOW.isoformat(),
        "gasCosts": [
            {
                "amount": "1000000000000000",
                "priceUSD": "1",
                "timestamp": NOW.isoformat(),
                "token": {
                    "chainId": SOURCE_GAS.chain_id,
                    "address": "0x0000000000000000000000000000000000000000",
                    "decimals": 18,
                },
            }
        ],
        "steps": [first, second],
    }
    return _route_from_dict(payload, quote_timestamp=NOW.isoformat())


def test_candidate_loss_adds_wallet_paid_gas_but_not_provider_fee_items():
    route = _route(
        prices=(
            (SOURCE, "1"),
            (DESTINATION, "1"),
        ),
        gas_costs=(
            LifiGasCost(
                1_000_000_000_000_000,
                "0.0025",
                SOURCE_GAS,
                "1",
                NOW.isoformat(),
            ),
        ),
    )
    # Provider fee costs are deliberately excluded from the candidate API: the
    # quote output already reflects them and they are not another wallet payment.
    candidate = _route_candidate(
        route,
    )

    assert candidate.destination_asset == DESTINATION
    assert candidate.source_usd == Decimal("100")
    assert candidate.conservative_destination_usd == Decimal("97")
    assert candidate.wallet_paid_gas_usd == Decimal("0.001")
    assert candidate.loss_usd == Decimal("3.001")


def test_candidate_rejects_incomplete_lifi_cost_evidence():
    candidate = _route_candidate(
        _route(costs_complete=False),
    )

    assert candidate.reason == "incomplete_cost_evidence"
    assert candidate.loss_pct is None


def test_candidate_requires_fresh_exact_gas_price_on_a_third_chain():
    route = _route(gas_costs=(LifiGasCost(10**15, None, THIRD_CHAIN_GAS),))

    candidate = _route_candidate(
        route,
    )

    assert candidate.reason == "missing_gas_price"
    assert candidate.loss_usd is None


def test_candidate_uses_route_evidence_final_identity_not_first_step_action():
    candidate = _route_candidate(
        _route(),
    )

    assert candidate.destination_asset == DESTINATION
    assert candidate.destination_asset != INTERMEDIATE


def test_candidate_requires_exact_fresh_source_and_destination_prices():
    route = _route(prices=((SOURCE, "1"),))

    candidate = _route_candidate(
        route,
    )

    assert candidate.reason == "missing_destination_price"
    assert candidate.loss_pct is None


def test_candidate_requires_explicit_complete_priced_gas_estimate():
    incomplete_flag = _values_candidate(
        route_ids=("missing-estimate",),
        source_asset=SOURCE,
        destination_asset=DESTINATION,
        source_amount=100_000_000,
        destination_amount=99_000_000,
        source_price=_price(SOURCE),
        destination_price=_price(DESTINATION),
        wallet_paid_gas=_priced_gas(),
        gas_estimate_complete=False,
    )
    empty_estimate = _values_candidate(
        route_ids=("empty-estimate",),
        source_asset=SOURCE,
        destination_asset=DESTINATION,
        source_amount=100_000_000,
        destination_amount=99_000_000,
        source_price=_price(SOURCE),
        destination_price=_price(DESTINATION),
        wallet_paid_gas=(),
        gas_estimate_complete=True,
    )

    assert incomplete_flag.reason == "missing_gas_estimate"
    assert empty_estimate.reason == "missing_gas_estimate"


def test_candidate_route_requires_a_nonempty_complete_gas_estimate():
    candidate = _route_candidate(
        _route(gas_costs=()),
        gas_estimate_complete=True,
    )

    assert candidate.reason == "missing_gas_estimate"


def test_candidate_validates_route_fingerprint_recipient_and_expected_identity():
    route = _route()
    changed_evidence = replace(route.evidence, payload_sha256="0" * 64)
    tampered = replace(route, evidence=changed_evidence)

    tampered_result = _route_candidate(tampered)
    wrong_recipient = _route_candidate(route, recipient="0x" + "e" * 40)
    wrong_target = _route_candidate(
        route,
        expected_destination=INTERMEDIATE,
    )

    assert tampered_result.reason == "tampered_route_evidence"
    assert wrong_recipient.reason == "recipient_mismatch"
    assert wrong_target.reason == "route_identity_mismatch"


def test_candidate_from_multistep_route_uses_evidence_final_destination():
    route = _multi_step_route()
    candidate = _route_candidate(
        route,
        expected_source=SOURCE,
        expected_destination=DESTINATION,
    )

    assert route.action.destination == INTERMEDIATE
    assert candidate.destination_asset == DESTINATION
    assert candidate.is_valid


def test_candidate_rejects_conflicting_prices_for_same_asset_and_timestamp():
    route = _route(
        prices=((SOURCE, "1"), (SOURCE, "2"), (DESTINATION, "1")),
    )

    candidate = _route_candidate(route)

    assert candidate.reason == "conflicting_price_evidence"


def test_fresher_supplied_price_wins_but_older_price_cannot_override_route_price():
    route = _route(quote_timestamp=(NOW - timedelta(minutes=1)).isoformat())
    fresher = _route_candidate(route, prices={SOURCE: _price(SOURCE, "2")})
    older = _route_candidate(
        route,
        prices={SOURCE: QuotePrice(SOURCE, "3", NOW - timedelta(minutes=2))},
    )

    assert fresher.source_usd == Decimal("200")
    assert older.source_usd == Decimal("100")


def test_admit_group_accepts_the_exact_loss_limit():
    result = admit_group(
        candidates=(_candidate(route_id="at-limit", loss="15"),),
        source_usd="100",
        max_loss_pct="15",
    )

    assert result.status == "accepted"
    assert result.reason is None
    assert result.loss_pct == Decimal("15")


@pytest.mark.parametrize("loss", ["15.0001", "16"])
def test_admit_group_rejects_loss_above_the_limit(loss: str):
    result = admit_group(
        candidates=(_candidate(route_id="over-limit", loss=loss),),
        source_usd="100",
        max_loss_pct="15",
    )

    assert result.status == "manual_review"
    assert result.reason == "loss_threshold_exceeded"


def test_admit_group_sums_candidate_losses_before_comparing_limit():
    result = admit_group(
        candidates=(
            _candidate(route_id="first", loss="14"),
            _candidate(route_id="second", loss="2"),
        ),
        source_usd="100",
        max_loss_pct="15",
    )

    assert result.status == "manual_review"
    assert result.reason == "loss_threshold_exceeded"
    assert result.loss_usd == Decimal("16")
    assert result.loss_pct == Decimal("16")


def test_admit_group_rejects_zero_source_value():
    result = admit_group(candidates=(), source_usd="0", max_loss_pct="15")

    assert result.status == "manual_review"
    assert result.reason == "zero_source_value"
    assert result.loss_pct is None


def test_admit_group_rejects_invalid_loss_limit():
    result = admit_group(
        candidates=(_candidate(route_id="candidate", loss="1"),),
        source_usd="100",
        max_loss_pct="NaN",
    )

    assert result.status == "manual_review"
    assert result.reason == "invalid_loss_limit"


@pytest.mark.parametrize(
    ("source_usd", "max_loss_pct", "reason"),
    [(100, 0.1, "invalid_loss_limit"), (100.0, "15", "invalid_source_value")],
)
def test_admit_group_rejects_float_money_or_threshold(
    source_usd, max_loss_pct, reason
):
    result = admit_group(
        candidates=(_candidate(route_id="candidate", loss="1"),),
        source_usd=source_usd,
        max_loss_pct=max_loss_pct,
    )

    assert result.status == "manual_review"
    assert result.reason == reason


def test_select_candidate_breaks_ties_by_target_identity_then_route_id():
    higher_identity = AssetIdentity(8453, "0x" + "e" * 40, 6)
    tied = (
        _candidate(route_id="z-route", loss="1", destination=higher_identity),
        _candidate(route_id="b-route", loss="1", destination=DESTINATION),
        _candidate(route_id="a-route", loss="1", destination=DESTINATION),
    )

    assert select_candidate(tied).route_ids == ("a-route",)


def test_select_candidate_prefers_percentage_then_usd_loss():
    lower_percentage_higher_usd = _candidate(
        route_id="lower-pct", loss="10", source_usd="1000"
    )
    higher_percentage_lower_usd = _candidate(
        route_id="lower-usd", loss="2", source_usd="100"
    )

    assert (
        select_candidate((higher_percentage_lower_usd, lower_percentage_higher_usd))
        is lower_percentage_higher_usd
    )


def test_select_candidate_uses_usd_loss_when_percentages_tie():
    lower_usd = _candidate(route_id="lower-usd", loss="2", source_usd="100")
    higher_usd = _candidate(route_id="higher-usd", loss="4", source_usd="200")

    assert select_candidate((higher_usd, lower_usd)) is lower_usd


def test_direct_deposit_candidate_is_gas_only():
    gas_only = _values_candidate(
        route_ids=("direct-deposit",),
        source_asset=SOURCE,
        destination_asset=SOURCE,
        source_amount=100_000_000,
        destination_amount=100_000_000,
        source_price=_price(SOURCE),
        destination_price=_price(SOURCE),
        wallet_paid_gas=(FeeQuote(1_000_000_000_000_000, _price(SOURCE_GAS, "2000")),),
        now=NOW,
        max_price_age=timedelta(minutes=5),
    )

    assert gas_only.loss_usd == Decimal("2")
    assert gas_only.loss_pct == Decimal("2")


def test_route_pair_distinguishes_identity_mismatch_from_amount_flow_mismatch():
    first = _values_candidate(
        route_ids=("swap",),
        source_asset=SOURCE,
        destination_asset=INTERMEDIATE,
        source_amount=100_000_000,
        destination_amount=99_000_000,
        source_price=_price(SOURCE),
        destination_price=_price(INTERMEDIATE),
        now=NOW,
        max_price_age=timedelta(minutes=5),
    )
    wrong_identity = _values_candidate(
        route_ids=("bridge",),
        source_asset=DESTINATION,
        destination_asset=DESTINATION,
        source_amount=99_000_000,
        destination_amount=98_000_000,
        source_price=_price(DESTINATION),
        destination_price=_price(DESTINATION),
        now=NOW,
        max_price_age=timedelta(minutes=5),
    )
    wrong_amount = _values_candidate(
        route_ids=("bridge",),
        source_asset=INTERMEDIATE,
        destination_asset=DESTINATION,
        source_amount=98_000_000,
        destination_amount=97_000_000,
        source_price=_price(INTERMEDIATE),
        destination_price=_price(DESTINATION),
        now=NOW,
        max_price_age=timedelta(minutes=5),
    )

    assert combine_candidates(first, wrong_identity).reason == "step_identity_mismatch"
    assert combine_candidates(first, wrong_amount).reason == "amount_flow_mismatch"


def test_pair_selection_uses_combined_loss_then_swap_and_bridge_ids():
    first = _values_candidate(
        route_ids=("swap-z",),
        source_asset=SOURCE,
        destination_asset=INTERMEDIATE,
        source_amount=100_000_000,
        destination_amount=99_000_000,
        source_price=_price(SOURCE),
        destination_price=_price(INTERMEDIATE),
        now=NOW,
        max_price_age=timedelta(minutes=5),
    )
    second_z = _values_candidate(
        route_ids=("bridge-z",),
        source_asset=INTERMEDIATE,
        destination_asset=DESTINATION,
        source_amount=99_000_000,
        destination_amount=98_000_000,
        source_price=_price(INTERMEDIATE),
        destination_price=_price(DESTINATION),
        now=NOW,
        max_price_age=timedelta(minutes=5),
    )
    second_a = _values_candidate(
        route_ids=("bridge-a",),
        source_asset=INTERMEDIATE,
        destination_asset=DESTINATION,
        source_amount=99_000_000,
        destination_amount=98_000_000,
        source_price=_price(INTERMEDIATE),
        destination_price=_price(DESTINATION),
        now=NOW,
        max_price_age=timedelta(minutes=5),
    )
    pair_z = combine_candidates(first, second_z)
    pair_a = combine_candidates(first, second_a)

    assert select_candidate_pair((pair_z, pair_a)).route_ids == ("swap-z", "bridge-a")


def test_pair_selection_orders_by_combined_percentage_then_usd_loss():
    lower_percentage_higher_loss = _values_candidate(
        route_ids=("z-swap", "z-bridge"),
        source_asset=SOURCE,
        destination_asset=DESTINATION,
        source_amount=1_000_000_000,
        destination_amount=980_000_000,
        source_price=_price(SOURCE),
        destination_price=_price(DESTINATION),
        now=NOW,
        max_price_age=timedelta(minutes=5),
    )
    higher_percentage_lower_loss = _values_candidate(
        route_ids=("a-swap", "a-bridge"),
        source_asset=SOURCE,
        destination_asset=DESTINATION,
        source_amount=100_000_000,
        destination_amount=97_000_000,
        source_price=_price(SOURCE),
        destination_price=_price(DESTINATION),
        now=NOW,
        max_price_age=timedelta(minutes=5),
    )

    assert (
        select_candidate_pair(
            (higher_percentage_lower_loss, lower_percentage_higher_loss)
        )
        is lower_percentage_higher_loss
    )


def test_pair_selection_uses_usd_loss_after_combined_percentage_ties():
    lower_loss_later_ids = _values_candidate(
        route_ids=("z-swap", "z-bridge"),
        source_asset=SOURCE,
        destination_asset=DESTINATION,
        source_amount=100_000_000,
        destination_amount=98_001_000,
        source_price=_price(SOURCE),
        destination_price=_price(DESTINATION),
        now=NOW,
        max_price_age=timedelta(minutes=5),
    )
    higher_loss_earlier_ids = _values_candidate(
        route_ids=("a-swap", "a-bridge"),
        source_asset=SOURCE,
        destination_asset=DESTINATION,
        source_amount=200_000_000,
        destination_amount=196_001_000,
        source_price=_price(SOURCE),
        destination_price=_price(DESTINATION),
        now=NOW,
        max_price_age=timedelta(minutes=5),
    )

    assert (
        select_candidate_pair(
            (higher_loss_earlier_ids, lower_loss_later_ids)
        )
        is lower_loss_later_ids
    )


def test_stale_or_mismatched_price_yields_stable_candidate_reason():
    candidate = _values_candidate(
        route_ids=("bad-price",),
        source_asset=SOURCE,
        destination_asset=DESTINATION,
        source_amount=100_000_000,
        destination_amount=99_000_000,
        source_price=QuotePrice(SOURCE, "1", NOW - timedelta(minutes=6)),
        destination_price=_price(DESTINATION),
        now=NOW,
        max_price_age=timedelta(minutes=5),
    )

    assert candidate.reason == "stale_price"


def test_candidate_rejects_a_price_for_the_wrong_exact_asset():
    candidate = _values_candidate(
        route_ids=("wrong-source-price",),
        source_asset=SOURCE,
        destination_asset=DESTINATION,
        source_amount=100_000_000,
        destination_amount=99_000_000,
        source_price=_price(INTERMEDIATE),
        destination_price=_price(DESTINATION),
        now=NOW,
        max_price_age=timedelta(minutes=5),
    )

    assert candidate.reason == "price_identity_mismatch"


@pytest.mark.parametrize("bad_price", ["0", "not-a-price"])
def test_candidate_rejects_invalid_route_price_without_aborting_route_parse(bad_price):
    route = _route(prices=((SOURCE, bad_price), (DESTINATION, "1")))

    candidate = _route_candidate(route)

    assert candidate.reason == "invalid_price_evidence"
