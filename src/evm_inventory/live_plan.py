"""Build read-only, policy-constrained consolidation candidates."""

from __future__ import annotations

import csv
import json
import math
import time
from collections import Counter, defaultdict
from collections.abc import Callable, Iterable
from copy import copy
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal, InvalidOperation, localcontext
from hashlib import sha256
from pathlib import Path
from typing import Protocol

import httpx

from .asset_policy import denied_entry
from .bitget_catalog import BitgetDepositTarget, deposit_targets, fetch_public_coins
from .celo import lifi_token_address, normalize_celo_balances
from .consolidation import (
    ConsolidationCandidate,
    GroupAdmission,
    admit_group,
    candidate_from_route,
    candidate_from_values,
    combine_candidates,
    select_candidate,
)
from .diagnostics import exception_diagnostic
from .executor import GAS_RESERVE_MULTIPLIER
from .fee_planner import native_asset_identity
from .lifi import (
    LifiClient,
    LifiPriceEvidence,
    LifiRoute,
    LifiRouteRequest,
    _route_from_dict,
    validate_bridge_route,
)
from .lifi_quota import LifiRequestLimit
from .models import AssetIdentity, ConfigError
from .network_policy import EXCLUDED_NETWORKS
from .planner_gas import PlannerGasEstimator
from .quote_jobs import JobInProgress
from .quote_parallel import CachedQuoteClient, complete_parallel
from .relay import SOURCE_CHAINS as RELAY_SOURCE_CHAINS
from .relay import quote_relay_entry
from .route_targets import TargetPolicy, load_target_policy, resolve_targets
from .stages import Deadline, StageDeadlineExceeded, check_deadline, deadline_scope, run_parallel
from .valuation import FeeQuote, QuotePrice, raw_to_decimal
from .weth import WETH_CONTRACTS, unwrap_route_payload

_PRICE_MAX_AGE = timedelta(minutes=5)
_ASSET_ACTIONS = frozenset({"swap", "unwrap", "review", "deny"})
_STABLE_SYMBOLS = frozenset({"USDC", "USDT"})
_NATIVE_LIFI_ADDRESS = "0x0000000000000000000000000000000000000000"


class QuoteClient(Protocol):
    def routes(self, request: LifiRouteRequest) -> tuple[LifiRoute, ...]: ...

    def token_price(self, asset: AssetIdentity) -> LifiPriceEvidence: ...


class RouteSearchIncomplete(ValueError):
    """A required quote could not be obtained; optimality is unproven."""

    def __init__(self, reason, error):
        self.reason = reason
        self.retry_at = getattr(error, "retry_at", None)
        self.diagnostic = exception_diagnostic(error)
        super().__init__(reason)

    def data(self):
        return {"reason": self.reason, "retry_at": self.retry_at, "diagnostic": self.diagnostic}


class GasEstimator(Protocol):
    def __call__(self, **kwargs) -> tuple[FeeQuote, ...] | None: ...


@dataclass(frozen=True, slots=True)
class _Input:
    base: dict
    asset: AssetIdentity
    action: str
    target: BitgetDepositTarget | None
    deposit_address: str | None
    source_price: QuotePrice | None
    source_usd: Decimal | None
    native_target: BitgetDepositTarget | None = None


@dataclass(frozen=True, slots=True)
class _RouteChoice:
    candidate: ConsolidationCandidate
    steps: tuple[tuple[str, LifiRoute], ...]
    target: BitgetDepositTarget


@dataclass(frozen=True, slots=True)
class _SearchResult:
    choices: tuple[_RouteChoice, ...]
    failed: tuple[ConsolidationCandidate, ...]
    complete: bool
    quoted: int
    prepared: int
    pruned: int
    errors: tuple[str, ...] = ()
    pending: tuple[dict, ...] = ()
    evidence_expires_at: datetime | None = None

    def data(self):
        return {
            "complete": self.complete,
            "quoted": self.quoted,
            "prepared": self.prepared,
            "pruned": self.pruned,
            "errors": list(self.errors),
            "pending": list(self.pending),
            "evidence_expires_at": (
                self.evidence_expires_at.isoformat() if self.evidence_expires_at else None
            ),
        }


def create_live_plan(
    balances_path: Path,
    *,
    deposit_addresses: dict[str, str],
    allowlist_path: Path = Path("config/swap-allowlist.json"),
    quote_floor: str = "0.01",
    max_route_loss_pct: Decimal = Decimal("15"),
    client: QuoteClient | None = None,
    targets: tuple[BitgetDepositTarget, ...] | None = None,
    target_policy: TargetPolicy | None = None,
    wallet_addresses: set[str] | None = None,
    now_ms: Callable[[], int] | None = None,
    gas_estimator: GasEstimator | None = None,
    stage_seconds: float = 30,
    workers: int = 16,
    target_workers: int = 8,
    allow_partial_search: bool = False,
    runtime_seconds: float | None = None,
) -> dict:
    """Quote exact enabled Bitget targets and enforce loss per wallet/network."""

    clock_ms = now_ms or _utc_now_ms
    quoted_at = int(clock_ms())

    def valuation_now() -> datetime:
        return datetime.fromtimestamp(clock_ms() / 1000, UTC)

    limit = _loss_limit(max_route_loss_pct)
    floor = _quote_floor(quote_floor)
    actions = _actions(allowlist_path)
    if isinstance(stage_seconds, bool) or not math.isfinite(stage_seconds) or stage_seconds <= 0:
        raise ConfigError("quote stage_seconds must be finite and greater than zero")
    if (
        isinstance(workers, bool)
        or not isinstance(workers, int)
        or not 1 <= workers <= 32
        or isinstance(target_workers, bool)
        or not isinstance(target_workers, int)
        or not 1 <= target_workers <= 8
    ):
        raise ConfigError("quote workers must be 1..32 and target_workers 1..8")
    enabled_targets = targets
    if target_policy is None:
        target_policy = (
            TargetPolicy.from_targets(targets)
            if targets
            else load_target_policy(Path("config/route-targets.json"))
        )
    search_scope = {
        "policy_digest": target_policy.digest,
        "requested_targets": [t.data() for t in target_policy.targets],
        "enabled_targets": [],
        "unavailable_targets": [],
        "mode": "configured_targets",
        "global_cheapest": False,
    }
    if client is None:
        raise ConfigError("LI.FI client with wallet proxy is required")
    quote_client = client
    addresses = {wallet.lower(): address for wallet, address in deposit_addresses.items()}
    selected_wallets = (
        {wallet.lower() for wallet in wallet_addresses} if wallet_addresses is not None else None
    )

    excluded_entries: list[dict] = []
    executable: list[_Input] = []
    with balances_path.open(newline="") as stream:
        for row in normalize_celo_balances(csv.DictReader(stream)):
            if int(row["chain_id"]) in EXCLUDED_NETWORKS:
                continue
            if (
                selected_wallets is not None
                and row.get("wallet", "").lower() not in selected_wallets
            ):
                continue
            if row.get("status") not in {"success", "provider_only"} or (
                int(row["raw_balance"]) <= 0 and not row.get("balance_conflict")
            ):
                continue
            base = _base(row)
            if row.get("balance_conflict"):
                excluded_entries.append(
                    {
                        **_manual(base, "conflicting_native_balance"),
                        "conflicting_amounts": row["conflicting_amounts"],
                    }
                )
                continue
            action = actions.get((base["chain_id"], base["asset_id"]), "deny")
            if action == "deny":
                excluded_entries.append(denied_entry(base, policy=actions, path=allowlist_path))
                continue
            if row.get("status") != "success":
                excluded_entries.append(_manual(base, "balance_not_rpc_verified"))
                continue
            if action == "review":
                excluded_entries.append(_manual(base, "policy_review"))
                continue
            if base["decimals"] is None:
                excluded_entries.append(_manual(base, "asset_decimals_unavailable"))
                continue
            asset = AssetIdentity(base["chain_id"], base["asset_id"], base["decimals"])
            address = addresses.get(base["wallet"])
            executable.append(_Input(base, asset, action, None, address, None, None))

    groups: dict[tuple[str, int], list[_Input]] = defaultdict(list)
    for item in executable:
        groups[(item.base["wallet"], item.asset.chain_id)].append(item)

    entries = list(excluded_entries)
    wallets = sorted({wallet for wallet, _chain in groups})
    quote_timing = {}
    for wallet in wallets:
        deadline = (Deadline(seconds=stage_seconds, label=f"quote:{wallet}", hard_limit=False)
                    if runtime_seconds is None else
                    Deadline(seconds=runtime_seconds, label=f"quote:{wallet}",
                             max_seconds=max(30, runtime_seconds)))
        timing = {"target_seconds": stage_seconds if runtime_seconds is None else runtime_seconds,
                  "elapsed_seconds": 0, "budget_exceeded": False}
        quote_timing[wallet] = timing
        select_client = getattr(quote_client, "for_wallet", None)
        scoped_client = select_client(wallet) if select_client is not None else quote_client
        wallet_client = CachedQuoteClient(scoped_client, workers=target_workers)
        wallet_client.allow_partial_search = allow_partial_search
        wallet_client.reserve_native_gas = allow_partial_search or isinstance(
            gas_estimator, PlannerGasEstimator
        )
        wallet_gas_estimator = gas_estimator
        if isinstance(gas_estimator, PlannerGasEstimator):
            wallet_gas_estimator = copy(gas_estimator)
            wallet_gas_estimator.retry_transient_errors = allow_partial_search
            wallet_gas_estimator.price_client = (
                wallet_client
                if gas_estimator.price_client is quote_client
                else CachedQuoteClient(gas_estimator.price_client, workers=target_workers)
            )
        wallet_groups = [
            (
                key,
                sorted(
                    groups[key], key=lambda item: (item.asset.contract_address, item.asset.decimals)
                ),
            )
            for key in sorted(groups)
            if key[0] == wallet
        ]
        with deadline_scope(deadline):
            if enabled_targets is None:
                catalog = run_parallel(
                    lambda _unused: deposit_targets(fetch_public_coins()),
                    (None,),
                    deadline=deadline,
                    workers=1,
                )
                if catalog.pending or catalog.errors:
                    reason = (
                        "stage_deadline_exceeded"
                        if catalog.pending
                        or any(
                            isinstance(error, StageDeadlineExceeded)
                            for error in catalog.errors.values()
                        )
                        else "target_catalog_unavailable"
                    )
                    entries.extend(
                        _manual(item.base, reason)
                        for _key, items in wallet_groups
                        for item in items
                    )
                    timing.update(
                        elapsed_seconds=deadline.elapsed, budget_exceeded=deadline.budget_exceeded
                    )
                    continue
                enabled_targets = tuple(catalog.completed[0])
            wallet_targets = tuple(
                sorted(
                    (
                        target
                        for target in enabled_targets
                        if target.chain_id not in EXCLUDED_NETWORKS
                    ),
                    key=lambda item: (item.chain_id, item.asset_id, item.coin, item.chain),
                )
            )

            resolution = resolve_targets(target_policy, wallet_targets)
            support_method = getattr(scoped_client, "supported_chain_ids", None)
            supported = None
            if support_method is not None:
                try:
                    supported = support_method()
                except Exception as error:
                    entries.extend(
                        {
                            **_manual(item.base, "lifi_chain_catalog_unavailable"),
                            "diagnostic": exception_diagnostic(error),
                        }
                        for _key, items in wallet_groups
                        for item in items
                    )
                    timing.update(
                        elapsed_seconds=deadline.elapsed, budget_exceeded=deadline.budget_exceeded
                    )
                    if allow_partial_search:
                        _apply_relay_fallback(entries, wallet, quote_client, resolution.enabled,
                                              limit, floor, addresses.get(wallet))
                    continue
                supported_groups = []
                for key, items in wallet_groups:
                    if key[1] not in supported:
                        # Direct deposits use this network's RPC and Bitget catalog;
                        # they have no dependency on a bridge provider's chain list.
                        direct = [item for item in items if _target(item.asset, wallet_targets)]
                        if direct:
                            supported_groups.append((key, direct))
                        items = [item for item in items if item not in direct]
                        relay_factory = getattr(quote_client, "relay_for_wallet", None)
                        if relay_factory is not None and (
                            key[1] in RELAY_SOURCE_CHAINS or allow_partial_search
                        ):
                            relay_client = relay_factory(wallet)
                            entries.extend(
                                quote_relay_entry(
                                    item.base, relay_client, resolution.enabled,
                                    loss_limit=limit, quote_floor=floor,
                                    deposit_address=item.deposit_address,
                                )
                                for item in items
                            )
                        else:
                            entries.extend(
                                {
                                    **_manual(item.base, "unsupported_by_lifi"),
                                    "routing_provider": "lifi",
                                    "alternative_provider_required": True,
                                }
                                for item in items
                            )
                    else:
                        supported_groups.append((key, items))
                wallet_groups = supported_groups
            route_targets = tuple(
                t for t in resolution.enabled if supported is None or t.chain_id in supported
            )
            search_scope.update(
                enabled_targets=[_target_data(t) for t in route_targets],
                unavailable_targets=list(resolution.unavailable)
                + [
                    {**_target_data(t), "reason": "unsupported_by_lifi"}
                    for t in resolution.enabled
                    if supported is not None and t.chain_id not in supported
                ],
            )
            if isinstance(scoped_client, LifiClient):
                scoped_client.policy_digest = target_policy.digest
                scoped_client.catalog_digest = sha256(
                    json.dumps(
                        [_target_data(target) for target in wallet_targets], sort_keys=True
                    ).encode()
                ).hexdigest()

            def plan_group(job):
                group_key, group_items = job
                return _prepare_group(
                    group_key,
                    group_items,
                    wallet_targets,
                    wallet_client,
                    limit,
                    floor,
                    valuation_now,
                    wallet_gas_estimator,
                    quoted_at,
                    route_targets=(route_targets if supported is None
                                   or group_key[1] in supported else ()),
                )

            results = run_parallel(plan_group, wallet_groups, deadline=deadline, workers=workers)
            for index, (_key, group_items) in enumerate(wallet_groups):
                if index in results.completed:
                    entries.extend(results.completed[index])
                else:
                    error = results.errors.get(index)
                    reason = (
                        "stage_deadline_exceeded"
                        if index in results.pending or isinstance(error, StageDeadlineExceeded)
                        else getattr(error, "reason", "quote_group_failed")
                    )
                    entries.extend(
                        {
                            **_manual(
                                item.base,
                                "quota_wait"
                                if reason in {"quota_wait", "provider_cooldown"}
                                else "route_search_incomplete",
                            ),
                            "search": {
                                "complete": False,
                                "errors": [reason],
                                "pending": [
                                    {
                                        "reason": reason,
                                        "retry_at": getattr(error, "retry_at", None),
                                        "diagnostic": exception_diagnostic(error),
                                    }
                                ]
                                if error is not None
                                else [],
                            },
                        }
                        for item in group_items
                    )

        if allow_partial_search:
            with deadline_scope(deadline):
                _apply_relay_fallback(entries, wallet, quote_client, resolution.enabled,
                                      limit, floor, addresses.get(wallet))
        timing.update(elapsed_seconds=deadline.elapsed, budget_exceeded=deadline.budget_exceeded)

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
    _invalidate_expired_comparisons(entries, valuation_now(), allow_requote=allow_partial_search)
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
        "gas_diagnostics": list(getattr(gas_estimator, "diagnostics", ())),
        "route_diagnostics": list(getattr(quote_client, "route_diagnostics", ())),
        "lifi_requests": getattr(quote_client, "request_metrics", {}),
        "relay_requests": getattr(quote_client, "relay_request_metrics", {}),
        "relay_summary": {
            "positions": sum(e.get("routing_provider") == "relay" for e in entries),
            "quotes_available": sum(e.get("quote_status") == "available" for e in entries),
            "candidates": sum(len(e.get("relay_candidates", [])) for e in entries),
            "execution_supported": any(
                e.get("routing_provider") == "relay" and e.get("execution_supported")
                for e in entries
            ),
        },
        "quote_timing": quote_timing,
        "search_scope": search_scope,
        "selection_policy": "best_available" if allow_partial_search else "complete_comparison",
        "entries": entries,
    }


def _apply_relay_fallback(entries, wallet, clients, targets, limit, floor, recipient):
    factory = getattr(clients, "relay_for_wallet", None)
    if factory is None:
        return
    for index, entry in enumerate(entries):
        if (entry.get("wallet") != wallet or entry.get("asset_id") != "native"
                or entry.get("decimals") != 18 or entry.get("status") != "manual_review"
                or entry.get("routing_provider") == "relay"
                or entry.get("reason") in {"policy_review", "conflicting_native_balance"}):
            continue
        alternative = quote_relay_entry(_base(entry), factory(wallet), targets, loss_limit=limit,
                                         quote_floor=floor,
                                         deposit_address=recipient)
        if alternative.get("status") == "route_ready" or alternative.get("reason") == (
            "insufficient_source_gas"
        ):
            alternative["provider_fallback"] = {
                "from": "lifi", "reason": entry.get("reason"), "search": entry.get("search")
            }
            entries[index] = alternative


def _invalidate_expired_comparisons(entries: list[dict], now: datetime, *,
                                    allow_requote: bool = False) -> None:
    expired_groups = {
        entry["group"]["key"]
        for entry in entries
        if isinstance(entry.get("group"), dict)
        and (
            entry.get("status") in {"direct_deposit", "route_ready"}
            or entry.get("search", {}).get("complete") is True
        )
        and entry.get("search", {}).get("evidence_expires_at")
        and now > _time(entry["search"]["evidence_expires_at"])
    }
    for entry in entries:
        group = entry.get("group")
        if not isinstance(group, dict) or group["key"] not in expired_groups:
            continue
        search = entry.get("search", {})
        search.update(
            complete=False, errors=sorted(set((*search.get("errors", []), "stale_quote")))
        )
        entry["search"] = search
        if allow_requote:
            entry["execution_requote_required"] = True
            continue
        entry["status"] = "manual_review"
        entry["reason"] = "stale_quote"
        entry["group"] = {**group, "status": "manual_review", "reason": "stale_quote"}
        if "best_candidate" in entry:
            entry["provisional_candidate"] = entry.pop("best_candidate")
        elif "valuation" in entry:
            entry["provisional_candidate"] = {
                "valuation": entry["valuation"],
                "steps": entry.get("steps", []),
                "target": entry.get("target"),
            }
        for key in (
            "target",
            "deposit_address",
            "steps",
            "route",
            "valuation",
            "reservations",
            "local_unwrap",
        ):
            entry.pop(key, None)


def _prepare_group(
    group_key, items, targets, client, limit, floor, now, gas_estimator, quoted_at, *, route_targets
):
    prices = complete_parallel(
        lambda item: _token_price(client, item.asset), items, workers=client.workers
    )
    excluded = []
    prepared = []
    for item, price in zip(items, prices, strict=True):
        check_deadline()
        source_usd = _usd_value(int(item.base["raw_balance"]), item.asset, price, now())
        price_data = {
            "source_price": {
                "asset": _asset_data(item.asset),
                "price_usd": _decimal(price.usd_per_token),
                "source": price.source,
                "reported_price_usd": price.reported_usd_per_token,
                "observed_at": price.observed_at.isoformat(),
            }
            if price is not None
            else None,
            "source_usd": _decimal(source_usd),
            "quote_floor_usd": _decimal(floor),
        }
        item = replace(item, base={**item.base, **price_data})
        if source_usd is not None and source_usd < floor:
            excluded.append({**item.base, "status": "dust", "reason": "below_quote_floor"})
            continue
        if item.deposit_address is None:
            excluded.append({**item.base, "status": "missing_deposit_address",
                             "reason": "missing_deposit_address"})
            continue
        target = _target(item.asset, targets)
        native_target = None
        if item.asset.decimals == 18 and item.asset.contract_address == WETH_CONTRACTS.get(
            item.asset.chain_id
        ):
            native_target = _target(
                native_asset_identity(item.asset.chain_id),
                (target for target in targets if target.coin.upper() == "ETH"),
            )
        prepared.append(
            replace(
                item,
                target=target,
                source_price=price,
                source_usd=source_usd,
                native_target=native_target,
            )
        )
    if prepared:
        excluded.extend(
            _plan_group(
                group_key, prepared, route_targets, client, limit, now, gas_estimator, quoted_at
            )
        )
    return excluded


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
    direct_items = list(items)
    direct_choices = complete_parallel(
        lambda item: _route_choices(item, targets, client, limit, now, gas_estimator),
        direct_items,
        workers=client.workers,
    )
    direct_by_id = {
        id(item): choices for item, choices in zip(direct_items, direct_choices, strict=True)
    }
    for item in items:
        check_deadline()
        search = direct_by_id[id(item)]
        item.base["search"] = search.data()
        choices, failed = search.choices, search.failed
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
            if getattr(client, "route_rate_error", None):
                failed_candidate = _with_reason(
                    failed_candidate, str(client.route_rate_error).split(":", 1)[0]
                )
            choices_by_item.append((item, None, failed_candidate))
        else:
            selected = next(choice for choice in choices if choice.candidate is best)
            choices_by_item.append((item, selected, best))

    final_now = now()
    for item, selected, candidate in choices_by_item:
        if selected is None:
            continue
        reason = _selected_choice_freshness(item, selected, candidate, final_now)
        if reason is not None:
            search = direct_by_id[id(item)]
            search = replace(
                search, complete=False, errors=tuple(sorted(set((*search.errors, reason))))
            )
            direct_by_id[id(item)] = search
            item.base["search"] = search.data()

    for item in items:
        search = direct_by_id[id(item)]
        record = getattr(client, "record_search", None)
        if record is not None:
            record(
                f"{item.base['wallet']}:{item.asset.chain_id}:{item.asset.contract_address}",
                {
                    **search.data(),
                    "amount_raw": item.base["raw_balance"],
                    "targets": [_target_data(target) for target in targets],
                    "quoted_at": quoted_at,
                },
            )

    source_values = [item.source_usd for item in items]
    group_source_usd = (
        _exact_sum(value for value in source_values if value is not None)
        if all(value is not None for value in source_values)
        else None
    )
    admission_candidates = tuple(choice for _item, _selected, choice in choices_by_item)
    partial_allowed = getattr(client, "allow_partial_search", False)
    if partial_allowed:
        eligible = [(item, candidate) for item, selected, candidate in choices_by_item
                    if selected is not None and candidate.is_valid]
        admission_candidates = tuple(candidate for _item, candidate in eligible)
        group_source_usd = _exact_sum(item.source_usd for item, _candidate in eligible)
    if any(not search.complete for search in direct_by_id.values()) and not partial_allowed:
        admission = GroupAdmission(
            status="manual_review",
            reason=(
                "quota_wait"
                if any(
                    p.get("reason") in {"quota_wait", "provider_cooldown"}
                    for search in direct_by_id.values()
                    for p in search.pending
                )
                else "route_search_incomplete"
            ),
            source_usd=group_source_usd,
            loss_usd=None,
            loss_pct=None,
            candidates=admission_candidates,
        )
    elif group_source_usd is None:
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
        item.base["search"]["selection_policy"] = (
            "best_available" if partial_allowed else "complete_comparison"
        )
        if admission.status != "accepted" or selected is None or not candidate.is_valid:
            reason = (candidate.reason if partial_allowed and
                      (selected is None or not candidate.is_valid)
                      else admission.reason) or "no_eligible_candidate"
            if selected is None and item.base.get("insufficient_source_gas"):
                reason = "insufficient_source_gas"
            entry = {
                **item.base,
                "status": "manual_review",
                "reason": reason,
                "group": group_data,
                (
                    "best_candidate" if item.base["search"]["complete"] else "provisional_candidate"
                ): _candidate_data(candidate),
            }
            if selected is not None and item.base["search"]["complete"]:
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
        if selected.steps and selected.steps[0][0] == "unwrap_to_native":
            entry["local_unwrap"] = True
        result.append(entry)
    return result


def _selected_choice_freshness(
    item: _Input, choice: _RouteChoice, candidate: ConsolidationCandidate, now: datetime
) -> str | None:
    """Recheck the group's selected evidence after all comparisons have finished."""
    prices = [item.source_price]
    prices.extend(fee.price for fee in candidate.gas_estimates)
    for _kind, route in choice.steps:
        if route.evidence is None:
            return "missing_route_evidence"
        age = (now - _time(route.evidence.quote_timestamp)).total_seconds()
        if not 0 <= age <= 240:
            return "route_quote_stale"
        prices.extend(_route_prices(route).values())
    for price in prices:
        if price is None:
            return "missing_price_evidence"
        age = now - price.observed_at
        if age < timedelta(0) or age > _PRICE_MAX_AGE:
            return "stale_price"
    return None


def _route_choices(
    item: _Input,
    targets: tuple[BitgetDepositTarget, ...],
    client: QuoteClient,
    limit: Decimal,
    now: Callable[[], datetime],
    gas_estimator: GasEstimator | None,
) -> _SearchResult:
    quoted: list[_RouteChoice] = []
    failed: list[ConsolidationCandidate] = []
    errors: list[str] = []
    pending: list[dict] = []
    started_at = now()
    expiries = [started_at + timedelta(seconds=240)]
    if item.source_price is not None:
        expiries.append(item.source_price.observed_at + _PRICE_MAX_AGE)

    def retain_candidate(candidate):
        if candidate.price_expires_at is not None:
            expiries.append(candidate.price_expires_at)
        expiries.extend(
            fee.price.observed_at + _PRICE_MAX_AGE
            for fee in candidate.gas_estimates
            if fee.price is not None
        )

    def retain_route(route):
        if route.evidence is not None:
            expiries.append(_time(route.evidence.quote_timestamp) + timedelta(seconds=240))
            expiries.extend(
                price.observed_at + _PRICE_MAX_AGE for price in _route_prices(route).values()
            )

    if not targets and item.target is None and item.native_target is None:
        errors.append("no_enabled_deposit_target")
    if item.target is not None:
        if int(item.base["raw_balance"]) >= item.target.minimum_raw:
            candidate = _direct_candidate(item, client, gas_estimator, now)
            retain_candidate(candidate)
            if candidate.is_valid:
                quoted.append(_RouteChoice(candidate, (), item.target))
            else:
                failed.append(candidate)
                errors.append(candidate.reason or "direct_preparation_failed")
        # The same asset/network target is represented by the direct transfer.
        targets = tuple(
            target
            for target in targets
            if not (
                target.chain_id == item.asset.chain_id
                and target.asset_id == item.asset.contract_address
            )
        )

    def request(source, destination, amount, recipient):
        try:
            return _request_routes(
                client,
                source,
                destination,
                amount,
                item.base["wallet"],
                recipient,
                observed=lambda timestamp: expiries.append(timestamp + timedelta(seconds=240)),
                fallback_observed_at=now(),
            )
        except RouteSearchIncomplete as error:
            errors.append(str(error))
            pending.append(
                {
                    **error.data(),
                    "source": _asset_data(source),
                    "destination": _asset_data(destination),
                }
            )
            return ()

    prepared: dict[tuple, ConsolidationCandidate] = {}
    native_prepared: dict[tuple, tuple[_RouteChoice, ConsolidationCandidate]] = {}

    def prepare_native(choice):
        """Refine spendable input against RPC fee caps using the executor's reserve rule."""
        balance = int(item.base["raw_balance"])
        initial_key = (choice.steps[0][1].evidence.payload_sha256, item.deposit_address)
        if initial_key in native_prepared:
            return native_prepared[initial_key]
        for attempt in range(3):
            kind, route = choice.steps[0]
            key = (route.evidence.payload_sha256, kind, item.deposit_address)
            if key not in prepared:
                prepared[key] = _route_candidate(
                    route, item, client, now, gas_estimator, expected_source=item.asset,
                    expected_destination=route.evidence.destination,
                    expected_amount=route.evidence.input_amount, recipient=item.deposit_address,
                    purpose=kind, prepare=True,
                )
            candidate = prepared[key]
            if not candidate.is_valid:
                return choice, candidate
            source_fee = sum(fee.raw_amount for fee in candidate.gas_estimates
                             if fee.price is not None and fee.price.asset == item.asset)
            reserve = source_fee * GAS_RESERVE_MULTIPLIER
            remaining = balance - route.evidence.input_amount
            if remaining == reserve or (attempt == 2 and remaining >= reserve):
                if candidate.destination_amount < choice.target.minimum_raw:
                    return choice, _with_reason(candidate, "below_target_minimum")
                result = choice, candidate
                native_prepared[initial_key] = result
                native_prepared[(route.evidence.payload_sha256, item.deposit_address)] = result
                return choice, candidate
            amount = balance - reserve
            if amount <= 0:
                return choice, _with_reason(candidate, "insufficient_source_gas")
            alternatives = [
                _route_choice(
                    replacement, item, choice.target, client, now, gas_estimator,
                    expected_source=item.asset, expected_destination=route.evidence.destination,
                    expected_amount=amount, recipient=item.deposit_address,
                    steps=((kind, replacement),), prepare=False,
                    check_target_minimum=False,
                )
                for replacement in request(item.asset, route.evidence.destination, amount,
                                           item.deposit_address)
            ]
            valid = [c for c in alternatives if c.candidate.is_valid]
            if not valid:
                return choice, _with_reason(candidate, "native_reserve_requote_unavailable")
            winner = select_candidate(c.candidate for c in valid)
            choice = next(c for c in valid if c.candidate is winner)
            retain_route(choice.steps[0][1])
        return choice, _with_reason(candidate, "native_gas_reserve_unstable")

    def collect(source, amount):
        def defer(alternatives):
            if alternatives:
                errors.append("alternative_destination_not_requested")
                pending.extend({**_target_data(t),
                                "reason": "alternative_destination_not_requested"}
                               for t in alternatives)

        def target_quotes(target):
            destination = AssetIdentity(target.chain_id, target.asset_id, _target_decimals(target))
            result = []
            quote_amount = amount
            routes = request(source, destination, quote_amount, item.deposit_address)
            if source.is_native and getattr(client, "reserve_native_gas", False) and routes:
                reserves = [sum(cost.amount for cost in route.gas_costs if cost.token == source)
                            for route in routes]
                reserves = [value for value in reserves if value > 0]
                if reserves:
                    reserve = min(reserves) * GAS_RESERVE_MULTIPLIER * 2
                    quote_amount = amount - reserve
                    if quote_amount <= 0:
                        # A provider hint cannot prove an RPC gas shortfall. Prepare
                        # a smaller input, then refine to balance minus the exact cap.
                        quote_amount = max(1, amount // 2)
                    routes = request(source, destination, quote_amount, item.deposit_address)
            for route in routes:
                retain_route(route)
                choice = _route_choice(
                    route,
                    item,
                    target,
                    client,
                    now,
                    gas_estimator,
                    expected_source=source,
                    expected_destination=destination,
                    expected_amount=quote_amount,
                    recipient=item.deposit_address,
                    steps=(("bridge", route),),
                    prepare=False,
                    check_target_minimum=not (
                        source.is_native and getattr(client, "reserve_native_gas", False)
                    ),
                )
                retain_candidate(choice.candidate)
                result.append(choice)
            return result

        if getattr(client, "allow_partial_search", False):
            # A prepared direct transfer already meets the collection policy.
            if any(not c.steps and c.candidate.loss_pct <= limit for c in quoted):
                defer(targets)
                return
            for index, target in enumerate(targets):
                batch = sorted(target_quotes(target), key=lambda c: (
                    c.candidate.loss_pct, c.candidate.loss_usd, c.candidate.route_ids
                ))
                for route_index, choice in enumerate(batch):
                    if not choice.candidate.is_valid:
                        failed.append(choice.candidate)
                        continue
                    kind, route = choice.steps[0]
                    if source.is_native and getattr(client, "reserve_native_gas", False):
                        choice, candidate = prepare_native(choice)
                    else:
                        key = (route.evidence.payload_sha256, kind, item.deposit_address)
                        if key not in prepared:
                            prepared[key] = _route_candidate(
                                route, item, client, now, gas_estimator,
                                expected_source=source,
                                expected_destination=route.evidence.destination,
                                expected_amount=route.evidence.input_amount,
                                recipient=item.deposit_address, purpose=kind, prepare=True,
                            )
                        candidate = prepared[key]
                    retain_candidate(candidate)
                    if not candidate.is_valid:
                        failed.append(candidate)
                        errors.append(candidate.reason or "candidate_preparation_failed")
                        continue
                    quoted.append(_RouteChoice(candidate, choice.steps, choice.target))
                    if candidate.loss_pct <= limit:
                        defer(targets[index + 1:])
                        if route_index < len(batch) - 1:
                            errors.append("alternative_routes_not_prepared")
                        return
            return

        for batch in complete_parallel(target_quotes, targets, workers=client.workers):
            for choice in batch:
                if choice.candidate.is_valid:
                    quoted.append(choice)
                else:
                    failed.append(choice.candidate)
                    if choice.candidate.reason in {
                        "missing_source_price",
                        "missing_destination_price",
                        "missing_gas_price",
                        "stale_price",
                        "invalid_price_evidence",
                    }:
                        errors.append(choice.candidate.reason)

    # LI.FI aggregates swaps/bridges for each configured destination itself.
    if item.native_target is not None:
        local = _local_unwrap_choice(item, client, now, gas_estimator)
        retain_route(local.steps[0][1])
        retain_candidate(local.candidate)
        if local.candidate.is_valid:
            quoted.append(local)
        else:
            failed.append(local.candidate)
            errors.append(local.candidate.reason or "local_unwrap_quote_failed")
    collect(item.asset, int(item.base["raw_balance"]))

    if errors and not getattr(client, "allow_partial_search", False):
        provisional = select_candidate(choice.candidate for choice in quoted)
        choices = tuple(choice for choice in quoted if choice.candidate is provisional)
        return _SearchResult(
            choices,
            tuple(failed),
            False,
            len(quoted),
            0,
            0,
            tuple(sorted(set(errors))),
            tuple(pending),
            min(expiries),
        )

    # Zero gas is a proven lower bound because wallet-paid gas is nonnegative.
    # Strict inequality preserves deterministic tie-breaking on equal costs.
    quoted.sort(
        key=lambda choice: (
            choice.candidate.loss_pct,
            choice.candidate.loss_usd,
            choice.candidate.route_ids,
        )
    )
    best: _RouteChoice | None = None
    pruned = 0
    for choice in quoted:
        check_deadline()
        if best is not None and best.candidate.loss_pct < choice.candidate.loss_pct:
            pruned += 1
            continue
        current = choice.candidate if not choice.steps else None
        if (item.asset.is_native and getattr(client, "reserve_native_gas", False)
                and len(choice.steps) == 1 and choice.steps[0][0] == "bridge"):
            choice, current = prepare_native(choice)
            retain_candidate(current)
            if not current.is_valid:
                failed.append(current)
                errors.append(current.reason)
                continue
            refined = _RouteChoice(current, choice.steps, choice.target)
            winner = select_candidate((best.candidate, current) if best else (current,))
            if winner is current:
                best = refined
            continue
        for index, (kind, route) in enumerate(choice.steps):
            source = route.evidence.source
            destination = route.evidence.destination
            recipient = (
                item.base["wallet"]
                if kind in {"swap_to_native", "unwrap_to_native"}
                else item.deposit_address
            )
            key = (route.evidence.payload_sha256, kind, recipient)
            if key not in prepared:
                prepared[key] = _route_candidate(
                    route,
                    item,
                    client,
                    now,
                    gas_estimator,
                    expected_source=source,
                    expected_destination=destination,
                    expected_amount=route.evidence.input_amount,
                    recipient=recipient,
                    purpose=kind if index == 0 else "bridge_after_swap",
                    prepare=True,
                )
                if kind == "unwrap_to_native" and prepared[key].is_valid:
                    native_item = replace(
                        item,
                        asset=destination,
                        target=choice.target,
                        source_price=_token_price(client, destination),
                    )
                    prepared[key] = combine_candidates(
                        prepared[key], _direct_candidate(native_item, client, gas_estimator, now)
                    )
            candidate = prepared[key]
            retain_candidate(candidate)
            current = candidate if current is None else combine_candidates(current, candidate)
            if not candidate.is_valid:
                errors.append(candidate.reason or "candidate_preparation_failed")
                break
        if current.is_valid:
            if current.source_usd != choice.candidate.source_usd:
                errors.append("valuation_changed_during_search")
            refined = _RouteChoice(current, choice.steps, choice.target)
            winner = select_candidate((best.candidate, current) if best else (current,))
            if winner is current:
                best = refined
        else:
            failed.append(current)
    return _SearchResult(
        (best,) if best is not None else (),
        tuple(failed),
        not errors,
        len(quoted),
        len(prepared),
        pruned,
        tuple(sorted(set(errors))),
        tuple(pending),
        evidence_expires_at=min(expiries),
    )


def _local_unwrap_choice(
    item: _Input,
    client: QuoteClient,
    now: Callable[[], datetime],
    gas_estimator: GasEstimator | None,
) -> _RouteChoice:
    """Quote a canonical withdrawal locally; its dependent deposit is priced on refinement."""
    target = item.native_target
    assert target is not None
    native = native_asset_identity(item.asset.chain_id)
    native_price = _token_price(client, native)
    request = LifiRouteRequest(
        from_chain_id=item.asset.chain_id,
        to_chain_id=item.asset.chain_id,
        from_token_address=item.asset.contract_address,
        to_token_address=_lifi_address(native),
        from_amount=item.base["raw_balance"],
        from_address=item.base["wallet"],
        to_address=item.base["wallet"],
    )
    payload = unwrap_route_payload(
        request,
        _decimal(item.source_price.usd_per_token) if item.source_price else "0",
        _decimal(native_price.usd_per_token) if native_price else "0",
    )
    action = payload["steps"][0]["action"]
    for token, price in (("fromToken", item.source_price), ("toToken", native_price)):
        if price is not None:
            action[token]["priceTimestamp"] = price.observed_at.isoformat()
    route = _route_from_dict(
        payload,
        quote_timestamp=now().isoformat(),
    )
    return _route_choice(
        route,
        item,
        target,
        client,
        now,
        gas_estimator,
        expected_source=item.asset,
        expected_destination=native,
        expected_amount=int(item.base["raw_balance"]),
        recipient=item.base["wallet"],
        steps=(("unwrap_to_native", route),),
        prepare=False,
    )


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
    purpose: str | None = None,
    prepare: bool = True,
    check_target_minimum: bool = True,
) -> _RouteChoice:
    try:
        validate_bridge_route(
            route,
            expected_source=expected_source,
            expected_destination=expected_destination,
            recipient=recipient,
            input_amount=expected_amount,
        )
        early_reason = (
            "below_target_minimum"
            if check_target_minimum and route.to_amount_min < target.minimum_raw else None
        )
    except ValueError:
        early_reason = "route_identity_mismatch"
    if early_reason:
        candidate = candidate_from_values(
            route_ids=(route.route_id,),
            source_asset=expected_source,
            destination_asset=expected_destination,
            source_amount=expected_amount,
            destination_amount=route.to_amount_min,
            source_price=item.source_price,
            destination_price=None,
            wallet_paid_gas=(),
            gas_estimate_complete=False,
            now=now(),
            max_price_age=_PRICE_MAX_AGE,
        )
        return _RouteChoice(_with_reason(candidate, early_reason), steps, target)
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
        purpose=purpose or steps[-1][0],
        prepare=prepare,
    )
    if check_target_minimum and candidate.is_valid and candidate.destination_amount is not None:
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
    prepare: bool = True,
) -> ConsolidationCandidate:
    estimates = (
        _estimate_extra_gas(
            gas_estimator,
            purpose=purpose,
            route=route,
            wallet=item.base["wallet"],
            source_asset=item.asset,
            target=None,
            amount=expected_amount,
            recipient=recipient,
        )
        if prepare
        else (FeeQuote(0, item.source_price),)
    )
    requires_approval = _requires_approval(route) if prepare else False
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
    for asset in (expected_source, expected_destination):
        if (
            asset.chain_id == 1
            and asset.decimals == 18
            and (
                asset.is_native
                or asset.contract_address == "0xc02aaa39b223fe8d0a0e5c4f27ead9083c756cc2"
            )
        ):
            checked = _token_price(client, asset)
            if checked is None:
                return candidate_from_values(
                    route_ids=(route.route_id,),
                    source_asset=expected_source,
                    destination_asset=expected_destination,
                    source_amount=expected_amount,
                    destination_amount=route.to_amount_min,
                    source_price=None,
                    destination_price=None,
                    wallet_paid_gas=(),
                    gas_estimate_complete=False,
                    now=now(),
                    max_price_age=_PRICE_MAX_AGE,
                )
            prices[asset] = checked
    if item.source_price is not None:
        prices[item.asset] = item.source_price
    for estimate in priced_estimates:
        if estimate.price is not None and estimate.price.asset not in {
            expected_source,
            expected_destination,
        }:
            prices[estimate.price.asset] = estimate.price
    if route.evidence is not None:
        for cost in route.gas_costs:
            if cost.token is not None and cost.token not in prices:
                fetched = _token_price(client, cost.token)
                if fetched is not None:
                    prices[cost.token] = fetched
    candidate = candidate_from_route(
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
            (not prepare or isinstance(gas_estimator, PlannerGasEstimator))
            and bool(priced_estimates)
        ),
    )
    return replace(
        candidate,
        price_expires_at=min(
            (price.observed_at + _PRICE_MAX_AGE for price in prices.values()),
            default=None,
        ),
    )


def _request_routes(
    client: QuoteClient,
    source: AssetIdentity,
    destination: AssetIdentity,
    amount: int,
    wallet: str,
    recipient: str,
    *,
    observed: Callable[[datetime], None] | None = None,
    fallback_observed_at: datetime | None = None,
) -> tuple[LifiRoute, ...]:
    request = LifiRouteRequest(
        from_chain_id=source.chain_id,
        to_chain_id=destination.chain_id,
        from_token_address=_lifi_address(source),
        to_token_address=_lifi_address(destination),
        from_amount=str(amount),
        from_address=wallet,
        to_address=recipient,
        from_token_decimals=source.decimals,
        to_token_decimals=destination.decimals,
    )
    try:
        routes = client.routes(request)
        if observed is not None:
            timestamp = getattr(client, "route_observed_at", {}).get(request)
            observed(_time(timestamp) if timestamp is not None else fallback_observed_at)
    except StageDeadlineExceeded:
        raise
    except (TimeoutError, httpx.TimeoutException) as error:
        check_deadline()
        raise RouteSearchIncomplete("quote_timeout", error) from error
    except (LifiRequestLimit, JobInProgress) as error:
        raise RouteSearchIncomplete(error.reason, error) from error
    except (OSError, ValueError, httpx.HTTPError) as error:
        reason = str(getattr(client, "route_rate_error", None) or "quote_request_failed")
        raise RouteSearchIncomplete(reason, error) from error
    return tuple(routes)


def _estimate_extra_gas(estimator: GasEstimator | None, **kwargs) -> tuple[FeeQuote, ...] | None:
    if estimator is None:
        return None
    try:
        check_deadline()
        result = estimator(**kwargs)
        check_deadline()
    except (StageDeadlineExceeded, LifiRequestLimit, JobInProgress):
        raise
    except (TimeoutError, httpx.TimeoutException):
        check_deadline()
        return None
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
    except (StageDeadlineExceeded, LifiRequestLimit, JobInProgress):
        raise
    except (TimeoutError, httpx.TimeoutException):
        check_deadline()
        return None
    except (OSError, ValueError, TypeError, InvalidOperation, httpx.HTTPError):
        return None


def _quote_price(evidence: LifiPriceEvidence) -> QuotePrice:
    if evidence.price_usd is None:
        raise ValueError("price evidence has no value")
    observed_at = _time(evidence.timestamp)
    return QuotePrice(
        evidence.asset,
        evidence.price_usd,
        observed_at,
        evidence.source,
        evidence.reported_price_usd,
    )


def _usd_value(
    amount: int,
    asset: AssetIdentity,
    price: QuotePrice | None,
    now: datetime,
) -> Decimal | None:
    if price is None or price.asset != asset:
        return None
    if price.observed_at > now or now - price.observed_at > _PRICE_MAX_AGE:
        return None
    try:
        token_amount = raw_to_decimal(amount, asset)
        unit_price = price.usd_per_token
        assert isinstance(unit_price, Decimal)
        with localcontext() as context:
            context.prec = max(
                context.prec,
                len(token_amount.as_tuple().digits) + len(unit_price.as_tuple().digits) + 2,
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
                    "source": price.source,
                    "reported_price_usd": price.reported_price_usd,
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
            str(candidate.source_amount) if candidate.source_amount is not None else None
        ),
        "destination_amount": (
            str(candidate.destination_amount) if candidate.destination_amount is not None else None
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
                "source": item.price.source if item.price else None,
                "reported_price_usd": item.price.reported_usd_per_token if item.price else None,
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
        "decimals": int(row["decimals"]) if row.get("decimals") not in {None, ""} else None,
        **({"balance_aliases": row["balance_aliases"]} if "balance_aliases" in row else {}),
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
    return lifi_token_address(asset.chain_id, asset.contract_address)


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
