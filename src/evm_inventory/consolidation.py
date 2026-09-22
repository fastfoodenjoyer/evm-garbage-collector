"""Pure valuation and deterministic selection for consolidation routes."""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal, InvalidOperation

from .lifi import (
    LifiError,
    LifiGasCost,
    LifiPriceEvidence,
    LifiRoute,
    validate_bridge_route,
)
from .models import AssetIdentity
from .valuation import (
    FeeQuote,
    QuotePrice,
    exact_decimal_difference,
    exact_decimal_sum,
    exact_percentage,
    quote_valuation,
)

REASON_CODES = frozenset(
    {
        "amount_flow_mismatch",
        "below_target_minimum",
        "incomplete_cost_evidence",
        "invalid_candidate",
        "invalid_loss_limit",
        "invalid_price_evidence",
        "invalid_source_value",
        "loss_threshold_exceeded",
        "missing_destination_price",
        "missing_gas_estimate",
        "missing_gas_price",
        "missing_route_evidence",
        "missing_source_price",
        "no_eligible_candidate",
        "price_identity_mismatch",
        "recipient_mismatch",
        "route_identity_mismatch",
        "stale_price",
        "step_identity_mismatch",
        "tampered_route_evidence",
        "conflicting_price_evidence",
        "zero_source_value",
    }
)


@dataclass(frozen=True, slots=True)
class ConsolidationCandidate:
    """Valued route or route pair; incomplete evidence remains diagnosable."""

    route_ids: tuple[str, ...]
    source_asset: AssetIdentity | None
    destination_asset: AssetIdentity | None
    source_amount: int | None
    destination_amount: int | None
    source_usd: Decimal | None
    conservative_destination_usd: Decimal | None
    wallet_paid_gas_usd: Decimal | None
    loss_usd: Decimal | None
    loss_pct: Decimal | None
    reason: str | None = None
    gas_estimate_complete: bool = False
    gas_estimates: tuple[FeeQuote, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(self, "route_ids", tuple(self.route_ids))
        object.__setattr__(self, "gas_estimates", tuple(self.gas_estimates))
        if self.reason is not None and self.reason not in REASON_CODES:
            raise ValueError("candidate reason is not a stable reason code")

    @property
    def is_valid(self) -> bool:
        return (
            self.reason is None
            and self.loss_usd is not None
            and self.loss_pct is not None
            and self.gas_estimate_complete
            and bool(self.gas_estimates)
            and all(
                isinstance(estimate, FeeQuote) and estimate.price is not None
                for estimate in self.gas_estimates
            )
        )


@dataclass(frozen=True, slots=True)
class GroupAdmission:
    """Immutable admission result for all selected routes in one source group."""

    status: str
    reason: str | None
    source_usd: Decimal | None
    loss_usd: Decimal | None
    loss_pct: Decimal | None
    candidates: tuple[ConsolidationCandidate, ...]

    def __post_init__(self) -> None:
        object.__setattr__(self, "candidates", tuple(self.candidates))
        if self.status not in {"accepted", "manual_review"}:
            raise ValueError("group status is invalid")
        if self.reason is not None and self.reason not in REASON_CODES:
            raise ValueError("group reason is not a stable reason code")


def candidate_from_values(
    *,
    route_ids: Iterable[str],
    source_asset: AssetIdentity,
    destination_asset: AssetIdentity,
    source_amount: int,
    destination_amount: int,
    source_price: QuotePrice | None,
    destination_price: QuotePrice | None,
    wallet_paid_gas: Iterable[FeeQuote],
    gas_estimate_complete: bool,
    now: datetime,
    max_price_age: timedelta,
) -> ConsolidationCandidate:
    """Value exact endpoints and wallet-paid gas, returning stable failures."""

    route_id_tuple = tuple(route_ids)
    gas_quotes = tuple(wallet_paid_gas)
    base = {
        "route_ids": route_id_tuple,
        "source_asset": source_asset,
        "destination_asset": destination_asset,
        "source_amount": source_amount,
        "destination_amount": destination_amount,
    }
    if source_price is None:
        return _rejected(**base, reason="missing_source_price")
    if destination_price is None:
        return _rejected(**base, reason="missing_destination_price")
    if source_price.asset != source_asset or destination_price.asset != destination_asset:
        return _rejected(**base, reason="price_identity_mismatch")
    if not gas_estimate_complete or not gas_quotes:
        return _rejected(**base, reason="missing_gas_estimate")
    try:
        valuation = quote_valuation(
            source_amount=source_amount,
            source_price=source_price,
            destination_amount=destination_amount,
            destination_price=destination_price,
            wallet_paid_gas=gas_quotes,
            now=now,
            max_price_age=max_price_age,
        )
    except ValueError as exc:
        message = str(exc)
        if "stale" in message:
            reason = "stale_price"
        elif "gas" in message or "fee" in message:
            reason = "missing_gas_price"
        else:
            reason = "invalid_candidate"
        return _rejected(**base, reason=reason)

    if valuation.source_usd <= 0:
        return ConsolidationCandidate(
            **base,
            source_usd=valuation.source_usd,
            conservative_destination_usd=valuation.destination_usd,
            wallet_paid_gas_usd=valuation.wallet_paid_gas_usd,
            loss_usd=valuation.loss_usd,
            loss_pct=None,
            reason="zero_source_value",
            gas_estimate_complete=True,
            gas_estimates=gas_quotes,
        )
    return ConsolidationCandidate(
        **base,
        source_usd=valuation.source_usd,
        conservative_destination_usd=valuation.destination_usd,
        wallet_paid_gas_usd=valuation.wallet_paid_gas_usd,
        loss_usd=valuation.loss_usd,
        loss_pct=exact_percentage(valuation.loss_usd, valuation.source_usd),
        gas_estimate_complete=True,
        gas_estimates=gas_quotes,
    )


def candidate_from_route(
    route: LifiRoute,
    *,
    expected_source: AssetIdentity,
    expected_destination: AssetIdentity,
    expected_input_amount: int,
    recipient: str,
    gas_estimate_complete: bool,
    now: datetime,
    max_price_age: timedelta,
    prices: Mapping[AssetIdentity, QuotePrice] | None = None,
    wallet_paid_gas: Iterable[FeeQuote] = (),
    replace_route_gas: bool = False,
) -> ConsolidationCandidate:
    """Value a LI.FI route from exact normalized evidence and its minimum output.

    The destination is always ``route.evidence.destination``. The legacy action
    can describe only the first step and must not be used for valuation.
    LI.FI fee items are intentionally excluded; quoted destination amounts
    already include provider fees. Route gas estimates and separately supplied
    wallet-paid gas are included.
    """

    if not isinstance(route, LifiRoute):
        return _rejected(
            route_ids=(),
            source_asset=None,
            destination_asset=None,
            source_amount=None,
            destination_amount=None,
            reason="invalid_candidate",
        )
    evidence = route.evidence
    route_ids = (route.route_id,)
    if evidence is None:
        return _rejected(
            route_ids=route_ids,
            source_asset=None,
            destination_asset=None,
            source_amount=None,
            destination_amount=None,
            reason="missing_route_evidence",
        )
    try:
        validate_bridge_route(
            route,
            expected_source=expected_source,
            expected_destination=expected_destination,
            recipient=recipient,
            input_amount=expected_input_amount,
        )
    except LifiError:
        return _rejected(
            route_ids=route_ids,
            source_asset=evidence.source,
            destination_asset=evidence.destination,
            source_amount=evidence.input_amount,
            destination_amount=evidence.min_output_amount,
            reason="tampered_route_evidence",
        )
    except ValueError as exc:
        message = str(exc).lower()
        reason = (
            "recipient_mismatch"
            if "recipient" in message
            else "route_identity_mismatch"
            if "identity" in message or "destination" in message or "source" in message
            else "amount_flow_mismatch"
            if "amount" in message
            else "tampered_route_evidence"
        )
        return _rejected(
            route_ids=route_ids,
            source_asset=evidence.source,
            destination_asset=evidence.destination,
            source_amount=evidence.input_amount,
            destination_amount=evidence.min_output_amount,
            reason=reason,
        )
    top_level_steps = tuple(step for step in evidence.steps if not step.is_substep)
    if not top_level_steps:
        return _rejected(
            route_ids=route_ids,
            source_asset=evidence.source,
            destination_asset=evidence.destination,
            source_amount=evidence.input_amount,
            destination_amount=evidence.min_output_amount,
            reason="missing_route_evidence",
        )
    first_step, final_step = top_level_steps[0], top_level_steps[-1]
    base = {
        "route_ids": route_ids,
        "source_asset": first_step.source,
        "destination_asset": final_step.destination,
        "source_amount": evidence.input_amount,
        "destination_amount": evidence.min_output_amount,
    }
    if not evidence.costs_complete:
        return _rejected(**base, reason="incomplete_cost_evidence")
    if (
        first_step.source is None
        or final_step.destination is None
        or first_step.from_amount != evidence.input_amount
        or evidence.source != first_step.source
        or evidence.destination != final_step.destination
        or final_step.to_amount is not None
        and final_step.to_amount != evidence.output_amount
        or any(
            previous.destination != current.source
            or previous.to_amount is None
            or previous.to_amount != current.from_amount
            for previous, current in zip(top_level_steps, top_level_steps[1:])
        )
        or route.from_amount != evidence.input_amount
        or route.to_amount != evidence.output_amount
        or route.to_amount_min != evidence.min_output_amount
        or route.route_id != evidence.route_id
    ):
        return _rejected(**base, reason="step_identity_mismatch")
    if (
        final_step.to_amount is not None
        and evidence.min_output_amount > final_step.to_amount
    ):
        return _rejected(**base, reason="invalid_candidate")

    price_book: dict[AssetIdentity, QuotePrice] = {}
    prices_by_observation: dict[tuple[AssetIdentity, datetime], Decimal] = {}
    for item in evidence.prices:
        if item.price_usd is None:
            continue
        try:
            quote = _price_from_lifi_evidence(item)
        except ValueError:
            return _rejected(**base, reason="invalid_price_evidence")
        observation = (item.asset, quote.observed_at)
        previous_price = prices_by_observation.get(observation)
        if previous_price is not None and previous_price != quote.usd_per_token:
            return _rejected(**base, reason="conflicting_price_evidence")
        prices_by_observation[observation] = quote.usd_per_token
        previous = price_book.get(item.asset)
        if previous is None or quote.observed_at > previous.observed_at:
            price_book[item.asset] = quote
    for asset, quote in (prices or {}).items():
        if not isinstance(asset, AssetIdentity) or not isinstance(quote, QuotePrice):
            return _rejected(**base, reason="invalid_candidate")
        if asset != quote.asset:
            return _rejected(**base, reason="price_identity_mismatch")
        previous = price_book.get(asset)
        if previous is not None and previous.observed_at == quote.observed_at:
            if previous.usd_per_token != quote.usd_per_token:
                return _rejected(**base, reason="conflicting_price_evidence")
        elif previous is None or quote.observed_at > previous.observed_at:
            price_book[asset] = quote

    source_price = price_book.get(evidence.source)
    destination_price = price_book.get(evidence.destination)
    if source_price is None:
        return _rejected(**base, reason="missing_source_price")
    if destination_price is None:
        return _rejected(**base, reason="missing_destination_price")

    gas_quotes: list[FeeQuote] = []
    if not replace_route_gas:
        for gas in route.gas_costs:
            quote = _quote_for_gas(gas, price_book)
            if quote is None:
                if gas.amount == 0:
                    continue
                return _rejected(**base, reason="missing_gas_price")
            gas_quotes.append(FeeQuote(gas.amount, quote))
    gas_quotes.extend(tuple(wallet_paid_gas))
    if not gas_estimate_complete or not gas_quotes:
        return _rejected(**base, reason="missing_gas_estimate")
    return candidate_from_values(
        **base,
        source_price=source_price,
        destination_price=destination_price,
        wallet_paid_gas=gas_quotes,
        gas_estimate_complete=gas_estimate_complete,
        now=now,
        max_price_age=max_price_age,
    )


def combine_candidates(
    first: ConsolidationCandidate,
    second: ConsolidationCandidate,
) -> ConsolidationCandidate:
    """Combine consecutive swap and bridge values using only initial/final output."""

    route_ids = first.route_ids + second.route_ids
    base = {
        "route_ids": route_ids,
        "source_asset": first.source_asset,
        "destination_asset": second.destination_asset,
        "source_amount": first.source_amount,
        "destination_amount": second.destination_amount,
    }
    if not first.is_valid:
        return _rejected(**base, reason=first.reason or "invalid_candidate")
    if not second.is_valid:
        return _rejected(**base, reason=second.reason or "invalid_candidate")
    if first.destination_asset != second.source_asset:
        return _rejected(**base, reason="step_identity_mismatch")
    if first.destination_amount != second.source_amount:
        return _rejected(**base, reason="amount_flow_mismatch")

    assert first.source_usd is not None
    assert second.conservative_destination_usd is not None
    assert first.wallet_paid_gas_usd is not None
    assert second.wallet_paid_gas_usd is not None
    gas_usd = exact_decimal_sum(
        (first.wallet_paid_gas_usd, second.wallet_paid_gas_usd)
    )
    slippage_loss = max(
        Decimal(0),
        exact_decimal_difference(
            first.source_usd, second.conservative_destination_usd
        ),
    )
    loss_usd = exact_decimal_sum((slippage_loss, gas_usd))
    if first.source_usd <= 0:
        return ConsolidationCandidate(
            **base,
            source_usd=first.source_usd,
            conservative_destination_usd=second.conservative_destination_usd,
            wallet_paid_gas_usd=gas_usd,
            loss_usd=loss_usd,
            loss_pct=None,
            reason="zero_source_value",
            gas_estimate_complete=True,
            gas_estimates=first.gas_estimates + second.gas_estimates,
        )
    return ConsolidationCandidate(
        **base,
        source_usd=first.source_usd,
        conservative_destination_usd=second.conservative_destination_usd,
        wallet_paid_gas_usd=gas_usd,
        loss_usd=loss_usd,
        loss_pct=exact_percentage(loss_usd, first.source_usd),
        gas_estimate_complete=True,
        gas_estimates=first.gas_estimates + second.gas_estimates,
    )


def select_candidate(
    candidates: Iterable[ConsolidationCandidate],
) -> ConsolidationCandidate | None:
    """Choose the lowest-loss candidate with deterministic identity tie-breaks."""

    valid = tuple(candidate for candidate in candidates if candidate.is_valid)
    if not valid:
        return None
    return min(valid, key=_candidate_order_key)


def select_candidate_pair(
    candidates: Iterable[ConsolidationCandidate],
) -> ConsolidationCandidate | None:
    """Choose swap→bridge pairs by combined loss, then route IDs."""

    valid = tuple(
        candidate
        for candidate in candidates
        if candidate.is_valid and len(candidate.route_ids) == 2
    )
    if not valid:
        return None
    return min(
        valid,
        key=lambda candidate: (
            candidate.loss_pct,
            candidate.loss_usd,
            candidate.route_ids[0],
            candidate.route_ids[1],
        ),
    )


def admit_group(
    *,
    candidates: Iterable[ConsolidationCandidate],
    source_usd: Decimal | str,
    max_loss_pct: Decimal | str,
) -> GroupAdmission:
    """Admit selected candidates only when summed loss stays within budget."""

    candidate_tuple = tuple(candidates)
    try:
        source_value = _decimal(source_usd, "group source USD")
    except ValueError:
        return _group_rejected(
            "invalid_source_value", candidate_tuple, source_usd=None
        )
    if source_value < 0:
        return _group_rejected(
            "invalid_source_value", candidate_tuple, source_usd=source_value
        )
    try:
        loss_limit = _decimal(max_loss_pct, "maximum loss percent")
    except ValueError:
        return _group_rejected(
            "invalid_loss_limit", candidate_tuple, source_usd=source_value
        )
    if loss_limit <= 0 or loss_limit > 100:
        return _group_rejected(
            "invalid_loss_limit", candidate_tuple, source_usd=source_value
        )
    if source_value == 0:
        return _group_rejected("zero_source_value", candidate_tuple, source_usd=source_value)
    if not candidate_tuple:
        return _group_rejected("no_eligible_candidate", candidate_tuple, source_usd=source_value)

    for candidate in candidate_tuple:
        if not candidate.is_valid:
            return _group_rejected(
                candidate.reason or "invalid_candidate",
                candidate_tuple,
                source_usd=source_value,
            )
    total_loss = exact_decimal_sum(
        candidate.loss_usd
        for candidate in candidate_tuple
        if candidate.loss_usd is not None
    )
    loss_pct = exact_percentage(total_loss, source_value)
    if loss_pct > loss_limit:
        return GroupAdmission(
            status="manual_review",
            reason="loss_threshold_exceeded",
            source_usd=source_value,
            loss_usd=total_loss,
            loss_pct=loss_pct,
            candidates=candidate_tuple,
        )
    return GroupAdmission(
        status="accepted",
        reason=None,
        source_usd=source_value,
        loss_usd=total_loss,
        loss_pct=loss_pct,
        candidates=candidate_tuple,
    )


def _candidate_order_key(candidate: ConsolidationCandidate) -> tuple[object, ...]:
    assert candidate.loss_pct is not None and candidate.loss_usd is not None
    destination = candidate.destination_asset
    identity = (
        destination.chain_id,
        destination.contract_address,
        destination.decimals,
    ) if destination is not None else (0, "", 0)
    return candidate.loss_pct, candidate.loss_usd, identity, candidate.route_ids


def _quote_for_gas(
    gas: LifiGasCost,
    prices: Mapping[AssetIdentity, QuotePrice],
) -> QuotePrice | None:
    if gas.token is None:
        return None
    return prices.get(gas.token)


def _price_from_lifi_evidence(item: LifiPriceEvidence) -> QuotePrice:
    return QuotePrice(item.asset, item.price_usd, _parse_timestamp(item.timestamp))


def _parse_timestamp(value: str) -> datetime:
    if not isinstance(value, str):
        raise ValueError("price timestamp is missing")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError("price timestamp is invalid") from exc
    if parsed.tzinfo is None:
        raise ValueError("price timestamp is invalid")
    return parsed.astimezone(UTC)


def _decimal(value: Decimal | str, field: str) -> Decimal:
    if isinstance(value, float):
        raise ValueError(f"{field} must be supplied as an exact decimal")
    try:
        parsed = Decimal(value)
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise ValueError(f"{field} must be a finite decimal") from exc
    if not parsed.is_finite():
        raise ValueError(f"{field} must be a finite decimal")
    return parsed


def _rejected(
    *,
    route_ids: tuple[str, ...],
    source_asset: AssetIdentity | None,
    destination_asset: AssetIdentity | None,
    source_amount: int | None,
    destination_amount: int | None,
    reason: str,
) -> ConsolidationCandidate:
    return ConsolidationCandidate(
        route_ids=route_ids,
        source_asset=source_asset,
        destination_asset=destination_asset,
        source_amount=source_amount,
        destination_amount=destination_amount,
        source_usd=None,
        conservative_destination_usd=None,
        wallet_paid_gas_usd=None,
        loss_usd=None,
        loss_pct=None,
        reason=reason,
        gas_estimate_complete=False,
        gas_estimates=(),
    )


def _group_rejected(
    reason: str,
    candidates: tuple[ConsolidationCandidate, ...],
    *,
    source_usd: Decimal | None,
) -> GroupAdmission:
    return GroupAdmission(
        status="manual_review",
        reason=reason,
        source_usd=source_usd,
        loss_usd=None,
        loss_pct=None,
        candidates=candidates,
    )
