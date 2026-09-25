"""Build read-only, policy-constrained consolidation candidates."""

from __future__ import annotations

import csv
import json
import time
from collections import Counter, defaultdict
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal, InvalidOperation, localcontext
from pathlib import Path
from typing import Protocol

import httpx

from .bitget_catalog import BitgetDepositTarget, deposit_targets, fetch_public_coins
from .consolidation import (
    ConsolidationCandidate,
    GroupAdmission,
    admit_group,
    candidate_from_route,
    candidate_from_values,
    combine_candidates,
    select_candidate,
)
from .fee_planner import native_asset_identity
from .lifi import LifiClient, LifiPriceEvidence, LifiRoute, LifiRouteRequest
from .models import AssetIdentity, ConfigError
from .planner_gas import PlannerGasEstimator
from .valuation import FeeQuote, QuotePrice, raw_to_decimal

_PRICE_MAX_AGE = timedelta(minutes=5)
_ASSET_ACTIONS = frozenset({"swap", "unwrap", "review", "deny"})
_STABLE_SYMBOLS = frozenset({"USDC", "USDT"})
_NATIVE_LIFI_ADDRESS = "0x0000000000000000000000000000000000000000"


class QuoteClient(Protocol):
    def routes(self, request: LifiRouteRequest) -> tuple[LifiRoute, ...]: ...

    def token_price(self, asset: AssetIdentity) -> LifiPriceEvidence: ...


class GasEstimator(Protocol):
    def __call__(self, **kwargs) -> tuple[FeeQuote, ...] | None: ...


@dataclass(frozen=True, slots=True)
class _Input:
    base: dict
    asset: AssetIdentity
    action: str
    target: BitgetDepositTarget | None
    deposit_address: str
    source_price: QuotePrice | None
    source_usd: Decimal | None


@dataclass(frozen=True, slots=True)
class _RouteChoice:
    candidate: ConsolidationCandidate
    steps: tuple[tuple[str, LifiRoute], ...]
    target: BitgetDepositTarget


def create_live_plan(
    balances_path: Path,
    *,
    deposit_addresses: dict[str, str],
    allowlist_path: Path = Path("config/swap-allowlist.json"),
    quote_floor: str = "0.01",
    max_route_loss_pct: Decimal = Decimal("15"),
    client: QuoteClient | None = None,
    targets: tuple[BitgetDepositTarget, ...] | None = None,
    wallet_addresses: set[str] | None = None,
    now_ms: Callable[[], int] | None = None,
    gas_estimator: GasEstimator | None = None,
) -> dict:
    """Quote exact enabled Bitget targets and enforce loss per wallet/network."""

    clock_ms = now_ms or _utc_now_ms
    quoted_at = int(clock_ms())
    def valuation_now() -> datetime:
        return datetime.fromtimestamp(clock_ms() / 1000, UTC)
    limit = _loss_limit(max_route_loss_pct)
    floor = _quote_floor(quote_floor)
    actions = _actions(allowlist_path)
    enabled_targets = tuple(
        sorted(
            targets if targets is not None else deposit_targets(fetch_public_coins()),
            key=lambda item: (item.chain_id, item.asset_id, item.coin, item.chain),
        )
    )
    quote_client = client or LifiClient(httpx.Client(timeout=30))
    addresses = {wallet.lower(): address for wallet, address in deposit_addresses.items()}
    selected_wallets = (
        {wallet.lower() for wallet in wallet_addresses}
        if wallet_addresses is not None
        else None
    )

    excluded_entries: list[dict] = []
    executable: list[_Input] = []
    with balances_path.open(newline="") as stream:
        for row in csv.DictReader(stream):
            if (
                selected_wallets is not None
                and row.get("wallet", "").lower() not in selected_wallets
            ):
                continue
            if row.get("status") != "success" or int(row["raw_balance"]) <= 0:
                continue
            base = _base(row)
            asset = AssetIdentity(base["chain_id"], base["asset_id"], base["decimals"])
            action = actions.get((asset.chain_id, asset.contract_address), "deny")
            if action == "deny":
                excluded_entries.append({**base, "status": "denied"})
                continue
            if action == "review":
                excluded_entries.append(_manual(base, "policy_review"))
                continue
            price = _token_price(quote_client, asset)
            source_usd = _usd_value(
                int(base["raw_balance"]), asset, price, valuation_now()
            )
            if source_usd is not None and source_usd < floor:
                excluded_entries.append({**base, "status": "dust"})
                continue
            address = addresses.get(base["wallet"])
            if address is None:
                excluded_entries.append(
                    {**base, "status": "missing_deposit_address"}
                )
                continue
            target = _target(asset, enabled_targets)
            if target is not None and int(base["raw_balance"]) < target.minimum_raw:
                excluded_entries.append(
                    _manual(base, "direct_deposit_below_minimum", target)
                )
                continue

            executable.append(
                _Input(
                    base=base,
                    asset=asset,
                    action=action,
                    target=target,
                    deposit_address=address,
                    source_price=price,
                    source_usd=source_usd,
                )
            )

    groups: dict[tuple[str, int], list[_Input]] = defaultdict(list)
    for item in executable:
        groups[(item.base["wallet"], item.asset.chain_id)].append(item)

    entries = list(excluded_entries)
    for group_key in sorted(groups):
        group_items = sorted(
            groups[group_key],
            key=lambda item: (item.asset.contract_address, item.asset.decimals),
        )
        entries.extend(
            _plan_group(
                group_key,
                group_items,
                enabled_targets,
                quote_client,
                limit,
                valuation_now,
                gas_estimator,
                quoted_at,
            )
        )

    for entry in entries:
        entry["quoted_at"] = quoted_at
    entries.sort(
        key=lambda item: (
            item["wallet"],
            item["chain_id"],
            item["asset_id"],
            tuple(step.get("route", {}).get("id", "") for step in item.get("steps", ())),
        )
    )
    counts = Counter(entry["status"] for entry in entries)
    return {
        "version": 4,
        "mode": "read_only_quote",
        "quoted_at": quoted_at,
        "max_route_loss_pct": _decimal(limit),
        "execution": {
            "sequential": True,
            "requires_explicit_execute": True,
            "requires_fresh_quote_and_balance_observation": True,
        },
        "summary": dict(sorted(counts.items())),
        "entries": entries,
    }


def _plan_group(
    group_key: tuple[str, int],
    items: list[_Input],
    targets: tuple[BitgetDepositTarget, ...],
    client: QuoteClient,
    limit: Decimal,
    now: Callable[[], datetime],
    gas_estimator: GasEstimator | None,
    quoted_at: int,
) -> list[dict]:
    choices_by_item: list[tuple[_Input, _RouteChoice | None, ConsolidationCandidate]] = []
    for item in items:
        choices, failed = _route_choices(item, targets, client, limit, now, gas_estimator)
        best = select_candidate(choice.candidate for choice in choices)
        if best is None:
            failed_candidate = _best_failed(failed)
            if failed_candidate is None:
                failed_candidate = candidate_from_values(
                    route_ids=(),
                    source_asset=item.asset,
                    destination_asset=item.asset,
                    source_amount=int(item.base["raw_balance"]),
                    destination_amount=0,
                    source_price=item.source_price,
                    destination_price=None,
                    wallet_paid_gas=(),
                    gas_estimate_complete=False,
                    now=now(),
                    max_price_age=_PRICE_MAX_AGE,
                )
            choices_by_item.append((item, None, failed_candidate))
        else:
            selected = next(choice for choice in choices if choice.candidate is best)
            choices_by_item.append((item, selected, best))

    source_values = [item.source_usd for item in items]
    group_source_usd = (
        _exact_sum(value for value in source_values if value is not None)
        if all(value is not None for value in source_values)
        else None
    )
    admission_candidates = tuple(choice for _item, _selected, choice in choices_by_item)
    if group_source_usd is None:
        admission = GroupAdmission(
            status="manual_review",
            reason="missing_source_price",
            source_usd=None,
            loss_usd=None,
            loss_pct=None,
            candidates=admission_candidates,
        )
    else:
        admission = admit_group(
            candidates=admission_candidates,
            source_usd=group_source_usd,
            max_loss_pct=limit,
        )

    group_data = _group_data(group_key, admission, limit, quoted_at)
    result: list[dict] = []
    for item, selected, candidate in choices_by_item:
        if admission.status != "accepted":
            reason = admission.reason or "no_eligible_candidate"
            entry = {
                **item.base,
                "status": "manual_review",
                "reason": reason,
                "group": group_data,
                "best_candidate": _candidate_data(candidate),
            }
            if selected is not None:
                entry["target"] = _target_data(selected.target)
                entry["deposit_address"] = item.deposit_address
                entry["steps"] = _steps_data(selected.steps)
                entry["valuation"] = _valuation_data(candidate)
            else:
                entry["candidate_reason"] = candidate.reason
            result.append(entry)
            continue

        assert selected is not None
        entry = {
            **item.base,
            "status": "direct_deposit" if not selected.steps else "route_ready",
            "target": _target_data(selected.target),
            "deposit_address": item.deposit_address,
            "steps": (
                _steps_data(selected.steps)
                if selected.steps
                else [
                    {
                        "kind": "direct_deposit",
                        "asset": _asset_data(item.asset),
                        "amount_raw": item.base["raw_balance"],
                        "recipient": item.deposit_address,
                        "target": _target_data(selected.target),
                    }
                ]
            ),
            "route": _route_data(selected.steps[-1][1]) if len(selected.steps) == 1 else None,
            "valuation": _valuation_data(candidate),
            "group": group_data,
            "reservations": _reservations(
                candidate.gas_estimates,
                source_chain_id=item.asset.chain_id,
                target_chain_id=selected.target.chain_id,
                direct=not selected.steps,
            ),
        }
        if entry["route"] is None:
            del entry["route"]
        result.append(entry)
    return result


def _route_choices(
    item: _Input,
    targets: tuple[BitgetDepositTarget, ...],
    client: QuoteClient,
    limit: Decimal,
    now: Callable[[], datetime],
    gas_estimator: GasEstimator | None,
) -> tuple[list[_RouteChoice], list[ConsolidationCandidate]]:
    if item.target is not None:
        candidate = _direct_candidate(item, client, gas_estimator, now)
        choice = _RouteChoice(candidate, (), item.target)
        return ([choice] if candidate.is_valid else []), ([] if candidate.is_valid else [candidate])

    if not targets:
        return [], []

    routes_by_target: list[_RouteChoice] = []
    failed: list[ConsolidationCandidate] = []
    if item.asset.is_native or item.base["symbol"].upper() in _STABLE_SYMBOLS:
        for target in targets:
            routes = _request_routes(
                client,
                item.asset,
                AssetIdentity(target.chain_id, target.asset_id, _target_decimals(target)),
                int(item.base["raw_balance"]),
                item.base["wallet"],
                item.deposit_address,
            )
            for route in routes:
                choice = _route_choice(
                    route,
                    item,
                    target,
                    client,
                    now,
                    gas_estimator,
                    expected_source=item.asset,
                    expected_destination=AssetIdentity(
                        target.chain_id, target.asset_id, _target_decimals(target)
                    ),
                    expected_amount=int(item.base["raw_balance"]),
                    recipient=item.deposit_address,
                    steps=(('bridge', route),),
                )
                if choice.candidate.is_valid:
                    routes_by_target.append(choice)
                else:
                    failed.append(choice.candidate)
        return routes_by_target, failed

    native = native_asset_identity(item.asset.chain_id)
    swap_routes = _request_routes(
        client,
        item.asset,
        native,
        int(item.base["raw_balance"]),
        item.base["wallet"],
        item.base["wallet"],
    )
    if not swap_routes:
        # Keep target enumeration observable even when LI.FI cannot quote the
        # preparatory swap. These direct candidates are still validated against
        # exact source/target identities and may be usable for provider paths
        # that bridge the token without converting it to native first.
        for target in targets:
            target_asset = AssetIdentity(
                target.chain_id, target.asset_id, _target_decimals(target)
            )
            for route in _request_routes(
                client,
                item.asset,
                target_asset,
                int(item.base["raw_balance"]),
                item.base["wallet"],
                item.deposit_address,
            ):
                choice = _route_choice(
                    route,
                    item,
                    target,
                    client,
                    now,
                    gas_estimator,
                    expected_source=item.asset,
                    expected_destination=target_asset,
                    expected_amount=int(item.base["raw_balance"]),
                    recipient=item.deposit_address,
                    steps=(("bridge", route),),
                )
                if choice.candidate.is_valid:
                    routes_by_target.append(choice)
                else:
                    failed.append(choice.candidate)
    for swap in swap_routes:
        swap_candidate = _route_candidate(
            swap,
            item,
            client,
            now,
            gas_estimator,
            expected_source=item.asset,
            expected_destination=native,
            expected_amount=int(item.base["raw_balance"]),
            recipient=item.base["wallet"],
            purpose="swap_to_native",
        )
        if not swap_candidate.is_valid:
            failed.append(swap_candidate)
            continue
        assert swap.evidence is not None
        bridge_amount = swap.evidence.min_output_amount
        for target in targets:
            target_asset = AssetIdentity(
                target.chain_id, target.asset_id, _target_decimals(target)
            )
            bridge_routes = _request_routes(
                client,
                native,
                target_asset,
                bridge_amount,
                item.base["wallet"],
                item.deposit_address,
            )
            for bridge in bridge_routes:
                bridge_candidate = _route_candidate(
                    bridge,
                    item,
                    client,
                    now,
                    gas_estimator,
                    expected_source=native,
                    expected_destination=target_asset,
                    expected_amount=bridge_amount,
                    recipient=item.deposit_address,
                    purpose="bridge_after_swap",
                )
                candidate = combine_candidates(swap_candidate, bridge_candidate)
                choice = _RouteChoice(
                    candidate,
                    (("swap_to_native", swap), ("bridge", bridge)),
                    target,
                )
                (routes_by_target if candidate.is_valid else failed).append(
                    choice if candidate.is_valid else candidate
                )
    return routes_by_target, failed


def _direct_candidate(
    item: _Input,
    client: QuoteClient,
    gas_estimator: GasEstimator | None,
    now: Callable[[], datetime],
) -> ConsolidationCandidate:
    target = item.target
    assert target is not None
    estimates = _estimate_extra_gas(
        gas_estimator,
        purpose="direct_deposit",
        route=None,
        wallet=item.base["wallet"],
        source_asset=item.asset,
        target=target,
        amount=int(item.base["raw_balance"]),
        recipient=item.deposit_address,
    )
    estimates = _price_fee_quotes(estimates, client)
    complete = estimates is not None and bool(estimates)
    return candidate_from_values(
        route_ids=(),
        source_asset=item.asset,
        destination_asset=item.asset,
        source_amount=int(item.base["raw_balance"]),
        destination_amount=int(item.base["raw_balance"]),
        source_price=item.source_price,
        destination_price=item.source_price,
        wallet_paid_gas=estimates or (),
        gas_estimate_complete=complete,
        now=now(),
        max_price_age=_PRICE_MAX_AGE,
    )


def _route_choice(
    route: LifiRoute,
    item: _Input,
    target: BitgetDepositTarget,
    client: QuoteClient,
    now: Callable[[], datetime],
    gas_estimator: GasEstimator | None,
    *,
    expected_source: AssetIdentity,
    expected_destination: AssetIdentity,
    expected_amount: int,
    recipient: str,
    steps: tuple[tuple[str, LifiRoute], ...],
) -> _RouteChoice:
    candidate = _route_candidate(
        route,
        item,
        client,
        now,
        gas_estimator,
        expected_source=expected_source,
        expected_destination=expected_destination,
        expected_amount=expected_amount,
        recipient=recipient,
        purpose=steps[-1][0],
    )
    if candidate.is_valid and candidate.destination_amount is not None:
        if candidate.destination_amount < target.minimum_raw:
            candidate = _with_reason(candidate, "below_target_minimum")
    return _RouteChoice(candidate, steps, target)


def _route_candidate(
    route: LifiRoute,
    item: _Input,
    client: QuoteClient,
    now: Callable[[], datetime],
    gas_estimator: GasEstimator | None,
    *,
    expected_source: AssetIdentity,
    expected_destination: AssetIdentity,
    expected_amount: int,
    recipient: str,
    purpose: str,
) -> ConsolidationCandidate:
    estimates = _estimate_extra_gas(
        gas_estimator,
        purpose=purpose,
        route=route,
        wallet=item.base["wallet"],
        source_asset=item.asset,
        target=None,
        amount=expected_amount,
        recipient=recipient,
    )
    requires_approval = _requires_approval(route)
    gas_complete = (gas_estimator is None or estimates is not None) and (
        not requires_approval or gas_estimator is not None
    )
    priced_estimates = (
        _price_fee_quotes(estimates, client)
        if estimates is not None
        else (() if gas_estimator is None and not requires_approval else None)
    )
    if priced_estimates is None:
        gas_complete = False
        priced_estimates = ()
    prices = _route_prices(route)
    if item.source_price is not None:
        prices[item.asset] = item.source_price
    for estimate in priced_estimates:
        if estimate.price is not None:
            prices[estimate.price.asset] = estimate.price
    if route.evidence is not None:
        for cost in route.gas_costs:
            if cost.token is not None and cost.token not in prices:
                fetched = _token_price(client, cost.token)
                if fetched is not None:
                    prices[cost.token] = fetched
    return candidate_from_route(
        route,
        expected_source=expected_source,
        expected_destination=expected_destination,
        expected_input_amount=expected_amount,
        recipient=recipient,
        gas_estimate_complete=gas_complete,
        now=now(),
        max_price_age=_PRICE_MAX_AGE,
        prices=prices,
        wallet_paid_gas=priced_estimates,
        replace_route_gas=(
            isinstance(gas_estimator, PlannerGasEstimator)
            and bool(priced_estimates)
        ),
    )


def _request_routes(
    client: QuoteClient,
    source: AssetIdentity,
    destination: AssetIdentity,
    amount: int,
    wallet: str,
    recipient: str,
) -> tuple[LifiRoute, ...]:
    request = LifiRouteRequest(
        from_chain_id=source.chain_id,
        to_chain_id=destination.chain_id,
        from_token_address=_lifi_address(source),
        to_token_address=_lifi_address(destination),
        from_amount=str(amount),
        from_address=wallet,
        to_address=recipient,
    )
    try:
        routes = client.routes(request)
    except (OSError, ValueError, httpx.HTTPError):
        return ()
    return tuple(sorted(routes, key=lambda route: route.route_id))


def _estimate_extra_gas(estimator: GasEstimator | None, **kwargs) -> tuple[FeeQuote, ...] | None:
    if estimator is None:
        return None
    try:
        result = estimator(**kwargs)
    except Exception:
        return None
    if result is None:
        return None
    try:
        estimates = tuple(result)
    except TypeError:
        return None
    if any(not isinstance(item, FeeQuote) for item in estimates):
        return None
    return estimates


def _price_fee_quotes(
    estimates: tuple[FeeQuote, ...] | None, client: QuoteClient
) -> tuple[FeeQuote, ...] | None:
    if estimates is None:
        return None
    result: list[FeeQuote] = []
    for estimate in estimates:
        if estimate.price is not None:
            result.append(estimate)
            continue
        return None
    return tuple(result)


def _requires_approval(route: LifiRoute) -> bool:
    def contains(value) -> bool:
        if isinstance(value, dict):
            if value.get("approvalAddress"):
                return True
            return any(contains(item) for item in value.values())
        if isinstance(value, list):
            return any(contains(item) for item in value)
        return False

    return any(contains(step) for step in route.raw_steps)


def _route_prices(route: LifiRoute) -> dict[AssetIdentity, QuotePrice]:
    prices: dict[AssetIdentity, QuotePrice] = {}
    if route.evidence is None:
        return prices
    for item in route.evidence.prices:
        if not item.price_usd:
            continue
        quote = _quote_price(item)
        previous = prices.get(item.asset)
        if previous is None or quote.observed_at > previous.observed_at:
            prices[item.asset] = quote
    return prices


def _token_price(client: QuoteClient, asset: AssetIdentity) -> QuotePrice | None:
    method = getattr(client, "token_price", None)
    if method is None:
        return None
    try:
        evidence = method(asset)
        if not isinstance(evidence, LifiPriceEvidence) or evidence.asset != asset:
            return None
        return _quote_price(evidence)
    except (OSError, ValueError, TypeError, InvalidOperation, httpx.HTTPError):
        return None


def _quote_price(evidence: LifiPriceEvidence) -> QuotePrice:
    if evidence.price_usd is None:
        raise ValueError("price evidence has no value")
    observed_at = _time(evidence.timestamp)
    return QuotePrice(evidence.asset, evidence.price_usd, observed_at)


def _usd_value(
    amount: int,
    asset: AssetIdentity,
    price: QuotePrice | None,
    now: datetime,
) -> Decimal | None:
    if price is None or price.asset != asset:
        return None
    if (
        price.observed_at > now
        or now - price.observed_at > _PRICE_MAX_AGE
    ):
        return None
    try:
        token_amount = raw_to_decimal(amount, asset)
        unit_price = price.usd_per_token
        assert isinstance(unit_price, Decimal)
        with localcontext() as context:
            context.prec = max(
                context.prec,
                len(token_amount.as_tuple().digits)
                + len(unit_price.as_tuple().digits)
                + 2,
            )
            return token_amount * unit_price
    except (ValueError, InvalidOperation):
        return None


def _route_data(route: LifiRoute) -> dict:
    evidence = route.evidence
    return {
        "id": route.route_id,
        "from_amount": str(route.from_amount),
        "to_amount": str(route.to_amount),
        "to_amount_min": str(route.to_amount_min),
        "tools": list(route.tools),
        "step": route.first_step,
        "raw_steps": list(route.raw_steps),
        "evidence": {
            "payload_sha256": evidence.payload_sha256,
            "canonical_payload_json": evidence.canonical_payload_json,
            "route_id": evidence.route_id,
            "source": _asset_data(evidence.source),
            "destination": _asset_data(evidence.destination),
            "input_amount": str(evidence.input_amount),
            "output_amount": str(evidence.output_amount),
            "min_output_amount": str(evidence.min_output_amount),
            "final_recipient": evidence.final_recipient,
            "quote_timestamp": evidence.quote_timestamp,
            "costs_complete": evidence.costs_complete,
            "costs_incomplete_reason": evidence.costs_incomplete_reason,
            "prices": [
                {
                    "asset": _asset_data(price.asset),
                    "price_usd": price.price_usd,
                    "timestamp": price.timestamp,
                }
                for price in evidence.prices
            ],
            "steps": [
                {
                    "step_type": step.step_type,
                    "provider": step.provider,
                    "source": _asset_data(step.source) if step.source else None,
                    "destination": _asset_data(step.destination) if step.destination else None,
                    "from_amount": str(step.from_amount) if step.from_amount is not None else None,
                    "to_amount": str(step.to_amount) if step.to_amount is not None else None,
                    "recipient": step.recipient,
                    "payload_sha256": step.payload_sha256,
                    "is_substep": step.is_substep,
                    "normalization_error": step.normalization_error,
                }
                for step in evidence.steps
            ],
        },
    }


def _candidate_data(candidate: ConsolidationCandidate) -> dict:
    return {
        "route_ids": list(candidate.route_ids),
        "source_asset": _asset_data(candidate.source_asset),
        "destination_asset": _asset_data(candidate.destination_asset),
        "source_amount": (
            str(candidate.source_amount)
            if candidate.source_amount is not None
            else None
        ),
        "destination_amount": (
            str(candidate.destination_amount)
            if candidate.destination_amount is not None
            else None
        ),
        "reason": candidate.reason,
        "valuation": _valuation_data(candidate),
    }


def _steps_data(steps: tuple[tuple[str, LifiRoute], ...]) -> list[dict]:
    return [{"kind": kind, "route": _route_data(route)} for kind, route in steps]


def _valuation_data(candidate: ConsolidationCandidate) -> dict:
    return {
        "source_usd": _decimal(candidate.source_usd),
        "destination_usd": _decimal(candidate.conservative_destination_usd),
        "wallet_paid_gas_usd": _decimal(candidate.wallet_paid_gas_usd),
        "loss_usd": _decimal(candidate.loss_usd),
        "loss_pct": _decimal(candidate.loss_pct),
        "gas_estimates": [
            {
                "raw_amount": str(item.raw_amount),
                "asset": _asset_data(item.price.asset) if item.price else None,
                "price_usd": _decimal(item.price.usd_per_token) if item.price else None,
                "observed_at": item.price.observed_at.isoformat() if item.price else None,
            }
            for item in candidate.gas_estimates
        ],
    }


def _group_data(
    key: tuple[str, int], admission: GroupAdmission, limit: Decimal, quoted_at: int
) -> dict:
    return {
        "key": f"{key[0]}:{key[1]}:{quoted_at}",
        "wallet": key[0],
        "source_chain_id": key[1],
        "status": admission.status,
        "reason": admission.reason,
        "source_usd": _decimal(admission.source_usd),
        "loss_usd": _decimal(admission.loss_usd),
        "loss_pct": _decimal(admission.loss_pct),
        "max_route_loss_pct": _decimal(limit),
    }


def _asset_data(asset: AssetIdentity | None) -> dict | None:
    if asset is None:
        return None
    return {
        "chain_id": asset.chain_id,
        "asset_id": asset.contract_address,
        "decimals": asset.decimals,
    }


def _base(row) -> dict:
    return {
        "wallet": row["wallet"].lower(),
        "chain_id": int(row["chain_id"]),
        "asset_id": row["asset_id"].lower(),
        "symbol": row.get("symbol", ""),
        "raw_balance": str(row["raw_balance"]),
        "decimals": int(row["decimals"]),
    }


def _actions(path: Path) -> dict[tuple[int, str], str]:
    data = json.loads(path.read_text(encoding="utf-8"))
    actions = {}
    for item in data.get("assets", []):
        if not isinstance(item, dict):
            continue
        action = item.get("action")
        if not isinstance(action, str) or action not in _ASSET_ACTIONS:
            raise ConfigError("execution allowlist asset action is invalid")
        identity = (int(item["chain_id"]), str(item["asset_id"]).lower())
        if identity in actions:
            raise ConfigError("execution allowlist has duplicate asset identity")
        actions[identity] = action
    return actions


def _target(identity: AssetIdentity, targets: Iterable[BitgetDepositTarget]):
    return next(
        (
            item
            for item in targets
            if item.chain_id == identity.chain_id
            and item.asset_id.lower() == identity.contract_address
            and _target_decimals(item) == identity.decimals
        ),
        None,
    )


def _target_decimals(target: BitgetDepositTarget) -> int:
    return 18 if target.asset_id == "native" else target.decimals


def _lifi_address(asset: AssetIdentity) -> str:
    return _NATIVE_LIFI_ADDRESS if asset.is_native else asset.contract_address


def _target_data(target: BitgetDepositTarget) -> dict:
    return {
        "coin": target.coin,
        "chain_id": target.chain_id,
        "chain": target.chain,
        "asset_id": target.asset_id.lower(),
        "minimum_raw": str(target.minimum_raw),
        "decimals": _target_decimals(target),
    }


def _reservations(
    estimates: tuple[FeeQuote, ...],
    *,
    source_chain_id: int | None = None,
    target_chain_id: int | None = None,
    direct: bool = False,
) -> dict:
    source = destination = 0
    final_deposit = 0
    for estimate in estimates:
        if estimate.price is None or not estimate.price.asset.is_native:
            continue
        chain_id = estimate.price.asset.chain_id
        if direct:
            if chain_id == target_chain_id:
                final_deposit += estimate.raw_amount
        elif chain_id == source_chain_id:
            source += estimate.raw_amount
        elif chain_id == target_chain_id:
            destination += estimate.raw_amount
    return {
        "source_native_gas_cap_raw": str(source),
        "destination_native_gas_cap_raw": str(destination),
        "final_deposit_native_gas_cap_raw": str(final_deposit),
    }


def _manual(base: dict, reason: str, target: BitgetDepositTarget | None = None) -> dict:
    result = {**base, "status": "manual_review", "reason": reason}
    if target is not None:
        result["target"] = _target_data(target)
    return result


def _best_failed(candidates: list[ConsolidationCandidate]) -> ConsolidationCandidate | None:
    if not candidates:
        return None
    valued = [candidate for candidate in candidates if candidate.loss_pct is not None]
    if valued:
        return select_candidate(valued)
    return min(candidates, key=lambda item: (item.reason or "", item.route_ids))


def _with_reason(candidate: ConsolidationCandidate, reason: str) -> ConsolidationCandidate:
    return ConsolidationCandidate(
        route_ids=candidate.route_ids,
        source_asset=candidate.source_asset,
        destination_asset=candidate.destination_asset,
        source_amount=candidate.source_amount,
        destination_amount=candidate.destination_amount,
        source_usd=candidate.source_usd,
        conservative_destination_usd=candidate.conservative_destination_usd,
        wallet_paid_gas_usd=candidate.wallet_paid_gas_usd,
        loss_usd=candidate.loss_usd,
        loss_pct=candidate.loss_pct,
        reason=reason,
        gas_estimate_complete=candidate.gas_estimate_complete,
        gas_estimates=candidate.gas_estimates,
    )


def _quote_floor(value: str) -> Decimal:
    try:
        result = Decimal(value)
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise ConfigError("quote floor must be a positive decimal") from exc
    if not result.is_finite() or result <= 0:
        raise ConfigError("quote floor must be a positive decimal")
    return result


def _loss_limit(value: Decimal) -> Decimal:
    try:
        limit = Decimal(value)
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise ConfigError(
            "maximum route loss must be a decimal from 0 exclusive to 100 inclusive"
        ) from exc
    if not limit.is_finite() or limit <= 0 or limit > 100:
        raise ConfigError("maximum route loss must be a decimal from 0 exclusive to 100 inclusive")
    return limit


def _time(value: str) -> datetime:
    if not isinstance(value, str):
        raise ValueError("quote price timestamp is missing")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError("quote price timestamp is invalid") from exc
    if parsed.tzinfo is None:
        raise ValueError("quote price timestamp is invalid")
    return parsed.astimezone(UTC)


def _decimal(value: Decimal | None) -> str | None:
    if value is None:
        return None
    return format(value.normalize(), "f") if value else "0"


def _exact_sum(values: Iterable[Decimal]) -> Decimal:
    terms = tuple(values)
    nonzero = tuple(value for value in terms if value)
    if not nonzero:
        return Decimal(0)
    max_adjusted = max(value.adjusted() for value in nonzero)
    min_exponent = min(value.as_tuple().exponent for value in nonzero)
    precision = max_adjusted - min_exponent + len(str(len(terms))) + 2
    with localcontext() as context:
        context.prec = max(context.prec, precision)
        return sum(terms, start=Decimal(0))


def _utc_now_ms() -> int:
    return time.time_ns() // 1_000_000
