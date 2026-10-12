"""Sequential, explicit-only execution of prepared direct-deposit or Jumper routes."""

from __future__ import annotations

import json
import os
import random
import sys
import time
from collections.abc import Callable
from concurrent.futures import FIRST_COMPLETED, Future, ThreadPoolExecutor, wait
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal, InvalidOperation, localcontext
from hashlib import sha256
from pathlib import Path
from threading import Event

import httpx

from .bitget import BitgetClient
from .bitget_catalog import BitgetDepositTarget
from .celo import CELO_CHAIN_ID, CELO_TOKEN_ADDRESS
from .consolidation import (
    candidate_from_route,
    candidate_from_values,
    combine_candidates,
)
from .diagnostics import exception_diagnostic
from .executor import (
    GAS_RESERVE_MULTIPLIER,
    AmbiguousBroadcast,
    EthereumGasDeferred,
    ExecutionRpc,
    approve_transaction,
    broadcast_durable_transaction,
    broadcast_signed_transaction,
    pending_nonce,
    recover_durable_broadcast,
    require_ethereum_gas_below_limit,
    require_ethereum_planned_gas_price_valid,
    require_native_reserve,
    sign_transaction,
    signed_transaction_hash,
    token_allowance,
    wait_for_receipt,
)
from .fee_planner import FeePlanner, native_asset_identity
from .journal import Journal
from .lifi import (
    LifiClient,
    LifiError,
    LifiRoute,
    LifiRouteRequest,
    TransactionRequest,
    _route_from_dict,
    validate_bridge_route,
)
from .lifi_quota import LifiRequestLimit
from .live_plan import _route_data, _valuation_data
from .models import AssetIdentity
from .network_policy import uses_excluded_network
from .planner_gas import PlannerGasEstimator
from .quote_jobs import JobInProgress
from .rpc import quantity
from .stages import (
    Deadline,
    StageDeadlineExceeded,
    budget_sleep,
    check_deadline,
    current_deadline,
    deadline_scope,
)
from .transport import Transport, is_transient_error
from .valuation import FeeQuote, QuotePrice, raw_to_decimal
from .wallet_lifi import WalletLifiClients
from .weth import (
    WETH_CONTRACTS,
    WETH_WITHDRAW_TOOL,
    unwrap_route_payload,
    withdraw_transaction_fields,
)
from .weth_continuation import unsigned_delivery, withdrawal_reserve
from .workbook import (
    DEPOSIT_ADDRESS_WARNING,
    WalletWorkbookRow,
    require_trusted_deposit_addresses,
    require_unique_deposit_addresses,
)

_MAX_PRICE_AGE = timedelta(minutes=5)
_BRIDGE_STATUS_POLL_SECONDS = 15
_NATIVE_LIFI_ADDRESS = "0x0000000000000000000000000000000000000000"


class GroupLossLimitExceeded(ValueError):
    """Raised when a fresh dependent route would exceed the configured budget."""


class DepositNotSeen(ValueError):
    """Bitget did not recognize a broadcast deposit before the sighting deadline."""


class DepositSettlementFailed(ValueError):
    """Bitget reported a failed deposit or missed the final settlement deadline."""


class _DepositBackground:
    """Poll seen deposits concurrently; persist results on the main thread."""

    def __init__(self, *, bitget: BitgetClient, journal: Journal):
        self.bitget = bitget
        self.journal = journal
        self.stop_event = Event()
        self.pool = ThreadPoolExecutor(max_workers=8, thread_name_prefix="bitget-deposit")
        self.watches: dict[int, tuple[Future, int, int, Decimal]] = {}

    def watch(self, *, entry: dict, position_id: int, recipient: str, loss_usd: str) -> None:
        if position_id in self.watches:
            return
        position = self.journal.position(position_id)
        step = self.journal.latest_step(
            position_id=position_id, step_key=_direct_step_key(self.journal, position_id))
        if step is None or not step["tx_hash"]:
            raise ValueError("seen deposit has no durable transaction hash")
        started_ms = (
            int(datetime.fromisoformat(step["created_at"]).replace(tzinfo=UTC).timestamp() * 1000)
            - 60_000
        )
        future = self.pool.submit(
            self.bitget.wait_for_deposit,
            tx_hash=step["tx_hash"],
            started_ms=started_ms,
            coin=str(entry["target"]["coin"]),
            chain=str(entry["target"]["chain"]),
            recipient=recipient,
            minimum_raw=int(entry["target"]["minimum_raw"]),
            stop_event=self.stop_event,
        )
        self.watches[position_id] = (
            future,
            int(position["group_id"]),
            int(step["id"]),
            _exact_money(loss_usd, "pending deposit loss"),
        )

    def raise_if_failed(self) -> None:
        for position_id, (future, group_id, _, _) in self.watches.items():
            if not future.done():
                continue
            try:
                status = future.result()
            except Exception as exc:
                reason = _execution_reason(exc)
                self._record_failure(position_id, group_id, reason)
                raise DepositSettlementFailed(reason) from exc
            if str(status or "").lower() != "success":
                reason = "bitget_deposit_not_credited"
                self._record_failure(position_id, group_id, reason)
                raise DepositSettlementFailed(reason)

    def _record_failure(self, position_id: int, group_id: int, reason: str) -> None:
        step_id = self.watches[position_id][2]
        self.journal.record_step_state(step_id, "manual_review")
        self.journal.record_position_state(position_id, "manual_review", reason=reason)
        self.journal.record_group_state(group_id, "manual_review_group_halted", reason=reason)
        self.stop_event.set()

    def finish(self) -> int:
        remaining = set(self.watches)
        while remaining:
            wait([self.watches[pid][0] for pid in remaining], return_when=FIRST_COMPLETED)
            for position_id in tuple(remaining):
                future, group_id, step_id, _loss = self.watches[position_id]
                if not future.done():
                    continue
                remaining.remove(position_id)
                try:
                    status = future.result()
                except Exception as exc:
                    status = None
                    reason = _execution_reason(exc)
                else:
                    reason = "bitget_deposit_not_credited"
                if str(status or "").lower() != "success":
                    self._record_failure(position_id, group_id, reason)
                    raise DepositSettlementFailed(reason)
                self.journal.record_step_state(step_id, "credited")
                self.journal.record_position_state(position_id, "completed")
                if all(
                    position["state"] == "completed"
                    for position in self.journal.group_positions(group_id)
                ):
                    self.journal.record_group_state(group_id, "completed")
        return len(self.watches)

    def close(self) -> None:
        self.stop_event.set()
        self.pool.shutdown(wait=True, cancel_futures=True)

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()


def _asset_from_data(value: object) -> AssetIdentity:
    if not isinstance(value, dict):
        raise ValueError("route asset identity is missing")
    try:
        return AssetIdentity(int(value["chain_id"]), str(value["asset_id"]), int(value["decimals"]))
    except (KeyError, TypeError, ValueError) as exc:
        raise ValueError("route asset identity is invalid") from exc


def _asset_dict(asset: AssetIdentity) -> dict:
    return {
        "chain_id": asset.chain_id,
        "asset_id": asset.contract_address,
        "decimals": asset.decimals,
    }


def _lifi_token(asset: AssetIdentity) -> str:
    return _NATIVE_LIFI_ADDRESS if asset.is_native else asset.contract_address


def _rpc_url(rpc_urls: dict[int, str], chain_id: int) -> str:
    value = rpc_urls.get(chain_id)
    if not isinstance(value, str) or not value:
        raise ValueError("RPC URL is unavailable for route execution")
    return value


def _asset_balance(rpc: ExecutionRpc, *, url: str, asset: AssetIdentity, wallet: str) -> int:
    if asset.is_native:
        return _native_balance(rpc, url=url, wallet=wallet)
    return _token_balance(rpc, url=url, token=asset.contract_address, wallet=wallet)


def _quote_price(jumper: LifiClient, asset: AssetIdentity) -> QuotePrice:
    evidence = jumper.token_price(asset)
    if evidence.asset != asset or evidence.price_usd is None:
        raise ValueError("requote_required:price_identity_or_value_invalid")
    try:
        observed = datetime.fromisoformat(evidence.timestamp.replace("Z", "+00:00"))
        price = Decimal(evidence.price_usd)
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise ValueError("requote_required:price_evidence_invalid") from exc
    if observed.tzinfo is None or not price.is_finite() or price <= 0:
        raise ValueError("requote_required:price_evidence_invalid")
    return QuotePrice(
        asset, price, observed.astimezone(UTC), evidence.source, evidence.reported_price_usd
    )


def _token_quote_price(jumper: LifiClient, asset: AssetIdentity) -> QuotePrice:
    return _quote_price(jumper, asset)


def _canonical_unwrap_route(jumper, asset, amount, wallet, now_fn):
    native = native_asset_identity(asset.chain_id)
    prices = (_quote_price(jumper, asset), _quote_price(jumper, native))
    now = now_fn()
    if any(not timedelta(0) <= now - p.observed_at <= _MAX_PRICE_AGE for p in prices):
        raise ValueError("requote_required:stale_price")
    request = LifiRouteRequest(asset.chain_id, asset.chain_id,
        _lifi_token(asset), _lifi_token(native), str(amount), wallet.public_address,
        wallet.public_address)
    return _route_from_dict(unwrap_route_payload(request,
        str(prices[0].usd_per_token), str(prices[1].usd_per_token)),
        quote_timestamp=min(p.observed_at for p in prices).isoformat())


def _gas_fee_quote(raw_amount: int, native_asset: AssetIdentity, jumper: LifiClient) -> FeeQuote:
    return FeeQuote(raw_amount, _quote_price(jumper, native_asset))


def _candidate_valuation(candidate) -> dict:
    return _valuation_data(candidate)


def _require_valid_candidate(candidate) -> None:
    if not candidate.is_valid:
        reason = candidate.reason or "invalid_candidate"
        raise ValueError(f"requote_required:{reason}")


def _require_live_target(
    entry: dict, *, wallet: WalletWorkbookRow, bitget: BitgetClient
) -> BitgetDepositTarget:
    target = entry.get("target")
    if not isinstance(target, dict):
        raise ValueError("requote_required:target_missing")
    try:
        planned = BitgetDepositTarget(
            str(target["coin"]),
            int(target["chain_id"]),
            str(target["asset_id"]).lower(),
            int(target["minimum_raw"]),
            str(target["chain"]),
            int(target["decimals"]),
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise ValueError("requote_required:target_invalid") from exc
    revalidate = getattr(bitget, "revalidate_deposit_target", None)
    if not callable(revalidate):
        raise ValueError("requote_required:live_target_validation_unavailable")
    try:
        live = revalidate(target, recipient=wallet.bitget_deposit_address)
    except StageDeadlineExceeded:
        raise
    except Exception as exc:
        raise ValueError("requote_required:live_target_validation_failed") from exc
    if (
        live.chain_id != planned.chain_id
        or live.asset_id.lower() != planned.asset_id
        or live.decimals != planned.decimals
    ):
        raise ValueError("requote_required:target_identity_changed")
    if live.minimum_raw > planned.minimum_raw:
        raise ValueError("requote_required:target_minimum_changed")
    return replace(live, minimum_raw=planned.minimum_raw)


def _require_target_minimum(
    entry: dict,
    bitget: BitgetClient,
    wallet: WalletWorkbookRow,
    candidate,
) -> None:
    target = _require_live_target(entry, wallet=wallet, bitget=bitget)
    amount = candidate.destination_amount
    if (
        candidate.destination_asset is None
        or candidate.destination_asset.chain_id != target.chain_id
        or candidate.destination_asset.contract_address != target.asset_id
        or candidate.destination_asset.decimals != target.decimals
    ):
        raise ValueError("requote_required:target_identity_mismatch")
    if amount is None or amount < target.minimum_raw:
        raise ValueError("requote_required:below_target_minimum")


class ExecutedTransactionHash(str):
    def __new__(cls, tx_hash: str, approval_tx_hash: str | None):
        result = super().__new__(cls, tx_hash)
        result.approval_tx_hash = approval_tx_hash
        return result


class RouteQuoteExpired(ValueError):
    def __init__(self, reason: str, approval_tx_hash: str | None):
        super().__init__(reason)
        self.approval_tx_hash = approval_tx_hash


_ROUTE_TRANSITIONS = {
    ("planned", "submit"): "submitted",
    ("submitted", "confirm"): "confirmed",
    ("confirmed", "await_bridge"): "awaiting_bridge",
    ("awaiting_bridge", "credit"): "credited",
    ("awaiting_bridge", "timeout"): "bridge_timeout",
    ("submitted", "swap_reverted"): "manual_review",
    ("confirmed", "bridge_reverted"): "manual_review",
}


def advance_route_state(state: str, event: str) -> str:
    """Advance one dependency-aware route state, refusing implicit retries."""

    try:
        return _ROUTE_TRANSITIONS[(state, event)]
    except KeyError as exc:
        raise ValueError(f"invalid route transition: {state} -> {event}") from exc


def revalidate_route_evidence(
    planned: LifiRoute,
    current: LifiRoute,
    *,
    expected_source: AssetIdentity,
    expected_destination: AssetIdentity,
    expected_input_amount: int,
    recipient: str,
) -> LifiRoute:
    """Require a fresh quote to preserve the approved route's exact evidence."""

    if not isinstance(planned, LifiRoute) or not isinstance(current, LifiRoute):
        raise ValueError("requote_required:route_evidence_missing")
    saved = planned.evidence
    fresh = current.evidence
    if saved is None or fresh is None:
        raise ValueError("requote_required:route_evidence_missing")
    if current.route_id != planned.route_id or fresh.route_id != saved.route_id:
        raise ValueError("requote_required:route_id_changed")
    if fresh.payload_sha256 != saved.payload_sha256:
        raise ValueError("requote_required:route_payload_changed")
    if (
        fresh.source != expected_source
        or fresh.destination != expected_destination
        or fresh.input_amount != expected_input_amount
        or fresh.min_output_amount != saved.min_output_amount
    ):
        raise ValueError("requote_required:route_identity_amount_or_minimum_changed")
    try:
        validate_bridge_route(
            current,
            expected_source=expected_source,
            expected_destination=expected_destination,
            input_amount=expected_input_amount,
            recipient=recipient,
        )
    except ValueError as exc:
        raise ValueError("requote_required:route_validation_failed") from exc
    return current


def _account_confirmed_local_unwrap_loss(journal: Journal, position_id: int, entry: dict) -> None:
    """Retain paid conversion costs when a dependent deposit halts the group."""
    if not entry.get("local_unwrap"):
        entry = journal.checkpoint(position_id, "canonical_prepared") or {}
        if not entry.get("local_unwrap"):
            return
    for step in entry.get("steps", []):
        if step.get("kind") != "unwrap_to_native":
            continue
        progress = (
            journal.checkpoint(position_id, f"route:unwrap_to_native:{step['route']['id']}") or {}
        )
        result = progress.get("unwrap_result")
        if not isinstance(result, dict):
            continue
        confirmed_loss = _exact_money(result["realized_loss_usd"], "unwrap loss")
        direct = journal.latest_step(position_id=position_id, step_key="direct_deposit")
        if direct is not None and direct["tx_hash"]:
            prepared = journal.checkpoint(position_id, "direct_prepared")
            if prepared is not None:
                confirmed_loss = _exact_sum((confirmed_loss, _entry_loss(prepared)))
        journal.account_position_loss(position_id, loss_usd=_decimal_string(confirmed_loss))


def _observe_halted_route_credits(entries, *, journal, group_id, submit_step):
    """A failed sibling cannot prevent observing an already delivered deposit."""
    counts = {"group_id": group_id, "completed": 0, "deposit_pending": 0, "manual_review": 0}
    for entry in entries:
        row = journal.connection.execute(
            "SELECT id,state FROM route_positions WHERE position_key=? AND group_id=?",
            (_position_key(entry), group_id),
        ).fetchone()
        if row is None:
            counts["manual_review"] += 1
            continue
        if row["state"] == "completed" and not route_credit_unverified(entry, journal, row["id"]):
            counts["completed"] += 1
            continue
        if row["state"] not in {"deposit_pending", "completed"} or not journal.checkpoint(
                row["id"], "route_deposit"):
            counts["manual_review"] += 1
            continue
        try:
            result = submit_step({**entry, "_journal_position_id": row["id"]},
                                 {"kind": "observe_route_deposit"})
            if result.get("state") == "completed":
                if row["state"] != "completed":
                    journal.account_position_loss(
                        row["id"], loss_usd=str(result["realized_loss_usd"]),
                    )
                journal.record_position_state(row["id"], "completed")
                counts["completed"] += 1
            elif result.get("state") == "deposit_pending":
                counts["deposit_pending"] += 1
            else:
                raise ValueError("deposit reconciliation returned no result")
        except DepositSettlementFailed as error:
            if row["state"] != "completed":
                journal.record_position_state(row["id"], "manual_review", reason=str(error))
            counts["manual_review"] += 1
        except Exception as error:
            journal.save_checkpoint(
                row["id"], "route_credit_diagnostic", exception_diagnostic(error),
            )
            counts["deposit_pending"] += 1
    return counts


def execute_group_entries(
    entries: list[dict],
    *,
    journal: Journal,
    max_route_loss_pct: Decimal,
    preflight: Callable[[dict], dict],
    submit_step: Callable[[dict, dict], dict],
    requote_after_swap: Callable[[dict, int], dict] | None = None,
    before_step: Callable[[], None] | None = None,
) -> dict[str, int]:
    """Revalidate and execute one loss-bounded wallet/source-network group.

    The injected functions are read-only preflight, one-step submission, and a
    single dependent bridge re-quote. This orchestration persists group and
    position state before it moves on to another balance.
    """

    if any(uses_excluded_network(entry) for entry in entries):
        return {"excluded": len(entries)}
    if not entries:
        return {"completed": 0}
    first = entries[0]
    group_data = first.get("group")
    if not isinstance(group_data, dict):
        raise ValueError("route entry is missing its source group")
    wallet = str(first["wallet"]).lower()
    source_chain_id = int(first["chain_id"])
    source_usd = _exact_money(group_data.get("source_usd"), "group source value")
    loss_limit = _exact_money(max_route_loss_pct, "maximum loss percent")
    planned_loss_limit = _exact_money(
        group_data.get("max_route_loss_pct"), "planned maximum loss percent"
    )
    if source_usd <= 0 or loss_limit <= 0 or loss_limit > 100:
        raise ValueError("invalid route group loss budget")
    if planned_loss_limit != loss_limit:
        raise ValueError("execution loss limit differs from the approved plan")
    group_key = str(group_data.get("key") or f"{wallet}:{source_chain_id}")
    group = journal.get_or_create_group(
        group_key=group_key,
        wallet=wallet,
        source_chain_id=source_chain_id,
        loss_budget_pct=_decimal_string(loss_limit),
        source_usd=_decimal_string(source_usd),
    )
    if (
        group["state"] == "manual_review_group_halted"
        and len(entries) == 1
        and first.get("status") == "direct_deposit"
        and (
            journal.reactivate_known_direct_deposit(group["id"], _position_key(first))
            or journal.reactivate_legacy_gas_deferral(group["id"], _position_key(first))
        )
    ):
        group = journal.group(group["id"])
    if group["state"] == "completed":
        observed = {"group_id": group["id"], "completed": 0, "deposit_pending": 0}
        for entry in entries:
            position = journal.connection.execute(
                "SELECT id,state FROM route_positions WHERE position_key=? AND group_id=?",
                (_position_key(entry), group["id"]),
            ).fetchone()
            if position is None or position["state"] != "completed":
                raise ValueError("terminal_route_group_position_mismatch")
            if route_credit_unverified(entry, journal, position["id"]):
                try:
                    result = submit_step({**entry, "_journal_position_id": position["id"]},
                                         {"kind": "observe_route_deposit"})
                except (StageDeadlineExceeded, DepositNotSeen):
                    result = {"state": "deposit_pending"}
                observed["completed" if result.get("state") == "completed"
                         else "deposit_pending"] += 1
            else:
                observed["completed"] += 1
        return observed
    if (group["state"] == "manual_review_group_halted"
            and all(e.get("automatic_requote") for e in entries)
            and journal.reactivate_unsigned_weth_continuation(
                group["id"], {_position_key(e) for e in entries})):
        group = journal.group(group["id"])
    if group["state"] in {
        "manual_review_after_swap",
        "manual_review_group_threshold_exceeded",
        "manual_review_group_halted",
    }:
        return _observe_halted_route_credits(
            entries, journal=journal, group_id=group["id"], submit_step=submit_step,
        )
    journal.record_group_state(group["id"], "active")

    positions = []
    for entry in entries:
        if (
            str(entry.get("wallet", "")).lower() != wallet
            or int(entry.get("chain_id", 0)) != source_chain_id
        ):
            raise ValueError("route group contains a different wallet or source network")
        position = journal.get_or_create_position(
            position_key=_position_key(entry),
            wallet=wallet,
            group_id=group["id"],
            source_asset_id=str(entry["asset_id"]).lower(),
            source_amount_raw=str(entry["raw_balance"]),
        )
        positions.append(position)

    counts = {
        "group_id": group["id"],
        "completed": 0,
        "deposit_pending": 0,
        "gas_deferred": 0,
        "manual_review": 0,
        "manual_review_after_swap": 0,
        "manual_review_group_threshold_exceeded": 0,
        "manual_review_group_halted": 0,
    }
    realized = Decimal(group["realized_loss_usd"])
    budget_usd = _exact_product(source_usd, loss_limit) / Decimal(100)

    def halt_remaining(
        start: int, *, state: str, reason: str, current_position_state: str | None = None
    ) -> None:
        group_state = (
            "manual_review_group_threshold_exceeded"
            if state == "manual_review_group_threshold_exceeded"
            else "manual_review_after_swap"
            if state == "manual_review_after_swap"
            else "manual_review_group_halted"
        )
        for index in range(max(0, start - 1), len(entries)):
            _account_confirmed_local_unwrap_loss(journal, positions[index]["id"], entries[index])
        for index in range(start, len(entries)):
            target_state = (
                current_position_state if index == start and current_position_state else state
            )
            current = journal.position(positions[index]["id"])
            if current["state"] not in {
                "completed",
                "manual_review",
                "manual_review_after_swap",
                "manual_review_group_threshold_exceeded",
                "manual_review_group_halted",
            }:
                journal.record_position_state(positions[index]["id"], target_state, reason=reason)
            counts[target_state] = counts.get(target_state, 0) + 1
        journal.record_group_state(group["id"], group_state, reason=reason)

    def yield_stage(position_id: int, exc: StageDeadlineExceeded) -> dict[str, int]:
        counts["pending" if journal.has_broadcast(position_id) else "deferred"] = 1
        journal.record_position_state(
            position_id, journal.position(position_id)["state"], reason=str(exc)
        )
        journal.record_group_state(group["id"], "active", reason=str(exc))
        return counts

    for index, entry in enumerate(entries):
        if current_deadline() is not None and current_deadline().expired:
            return yield_stage(
                positions[index]["id"], StageDeadlineExceeded("stage_deadline_exceeded")
            )
        if before_step is not None:
            before_step()
        if entry.get("_source_gas_held") and not journal.has_broadcast(positions[index]["id"]):
            return yield_stage(positions[index]["id"],
                               StageDeadlineExceeded("source_gas_for_unsettled_tokens"))
        position_id = positions[index]["id"]
        position_state = journal.position(position_id)["state"]
        if position_state == "completed":
            if route_credit_unverified(entry, journal, position_id):
                try:
                    result = submit_step({**entry, "_journal_position_id": position_id},
                                         {"kind": "observe_route_deposit"})
                except (StageDeadlineExceeded, DepositNotSeen):
                    result = {"state": "deposit_pending"}
                counts["completed" if result.get("state") == "completed"
                       else "deposit_pending"] += 1
            else:
                counts["completed"] += 1
            continue
        unresolved = journal.unresolved_direct_deposit(
            wallet=wallet,
            chain_id=source_chain_id,
            asset_id=str(entry["asset_id"]),
            exclude_position_id=position_id,
        )
        if unresolved is not None:
            raise ValueError(
                "unresolved prior direct deposit for this wallet, chain and asset: "
                f"{unresolved['tx_hash']}"
            )
        if position_state in {"deposit_pending", "direct_deposit_submitted"}:
            saved = journal.latest_step(position_id=position_id, step_key="direct_deposit")
            if position_state == "deposit_pending" or (saved and saved["tx_hash"]):
                try:
                    result = submit_step(
                        {
                            **(journal.checkpoint(position_id, "direct_prepared") or entry),
                            "_journal_position_id": position_id,
                        },
                        {"kind": "observe_route_deposit" if journal.checkpoint(
                            position_id, "route_deposit") else "direct_deposit"},
                    )
                    if result.get("state") == "deposit_pending":
                        realized = Decimal(
                            journal.account_position_loss(
                                position_id,
                                loss_usd=str(result.get("realized_loss_usd", _entry_loss(entry))),
                            )
                        )
                        journal.record_position_state(
                            position_id, "deposit_pending", reason=result.get("reason")
                        )
                        counts["deposit_pending"] += 1
                        continue
                    if result.get("state") != "completed":
                        raise ValueError("deposit reconciliation returned no result")
                    realized = Decimal(
                        journal.account_position_loss(
                            position_id,
                            loss_usd=str(result.get("realized_loss_usd", _entry_loss(entry))),
                        )
                    )
                    journal.record_position_state(position_id, "completed")
                    counts["completed"] += 1
                    continue
                except (DepositNotSeen, DepositSettlementFailed, GroupLossLimitExceeded) as exc:
                    reason = _execution_reason(exc)
                    journal.record_position_state(position_id, "manual_review", reason=reason)
                    counts["manual_review"] += 1
                    halt_remaining(index + 1, state="manual_review_group_halted", reason=reason)
                    return counts
                except StageDeadlineExceeded as exc:
                    return yield_stage(position_id, exc)
                except Exception as exc:
                    if isinstance(exc, AmbiguousBroadcast) and current_deadline() is not None:
                        return yield_stage(
                            position_id, StageDeadlineExceeded(f"broadcast_pending:{exc.tx_hash}")
                        )
                    journal.record_position_state(
                        position_id, "deposit_pending", reason=_execution_reason(exc)
                    )
                    counts["deposit_pending"] += 1
                    journal.record_group_state(group["id"], "deposit_pending")
                    return counts
        try:
            saved_preflight = journal.checkpoint(position_id, "prepared")
            frozen = (journal.has_asset_broadcast(position_id) if entry.get("automatic_requote")
                      else journal.has_broadcast(position_id))
            if saved_preflight is not None and frozen:
                fresh = saved_preflight
            else:
                fresh = preflight(entry)
            if not isinstance(fresh, dict):
                raise ValueError("route preflight returned no fresh plan")
            current_loss = _entry_loss(fresh)
            future_loss = _exact_sum(_entry_loss(item) for item in entries[index + 1 :])
        except EthereumGasDeferred as exc:
            journal.record_position_state(position_id, "gas_deferred", reason=exc.reason)
            journal.record_group_state(group["id"], "gas_deferred", reason=exc.reason)
            counts["gas_deferred"] += 1
            return counts
        except StageDeadlineExceeded as exc:
            return yield_stage(position_id, exc)
        except Exception as exc:
            if entry.get("automatic_requote") and _retryable_provider_error(exc):
                journal.save_checkpoint(position_id, "automatic_retry", {
                    "reason": _execution_reason(exc), "retry_at": getattr(exc, "retry_at", None)
                })
                return yield_stage(position_id, StageDeadlineExceeded(_execution_reason(exc)))
            if isinstance(exc, AmbiguousBroadcast) and current_deadline() is not None:
                return yield_stage(
                    position_id, StageDeadlineExceeded(f"broadcast_pending:{exc.tx_hash}")
                )
            reason = _execution_reason(exc)
            journal.record_position_state(position_id, "manual_review", reason=reason)
            counts["manual_review"] += 1
            halt_remaining(index + 1, state="manual_review_group_halted", reason=reason)
            if index + 1 == len(entries):
                journal.record_group_state(group["id"], "manual_review_group_halted", reason=reason)
            return counts

        accounted = Decimal(journal.position(position_id)["accounted_loss_usd"] or "0")
        projected = _exact_sum((realized, max(Decimal(0), current_loss - accounted), future_loss))
        projected_pct = _exact_percentage(projected, source_usd)
        journal.record_group_projection(
            group["id"],
            projected_loss_usd=_decimal_string(projected),
            projected_loss_pct=_decimal_string(projected_pct),
        )
        if projected > budget_usd:
            halt_remaining(
                index,
                state="manual_review_group_threshold_exceeded",
                reason="loss_threshold_exceeded",
            )
            return counts

        steps = fresh.get("steps")
        if not isinstance(steps, list) or not steps:
            reason = "route plan has no executable steps"
            journal.record_position_state(position_id, "manual_review", reason=reason)
            counts["manual_review"] += 1
            halt_remaining(index + 1, state="manual_review_group_halted", reason=reason)
            if index + 1 == len(entries):
                journal.record_group_state(group["id"], "manual_review_group_halted", reason=reason)
            return counts

        def check_dependent_route_loss(position_loss: Decimal) -> None:
            dependent_projection = _exact_sum((realized - accounted, position_loss, future_loss))
            dependent_pct = _exact_percentage(dependent_projection, source_usd)
            journal.record_group_projection(
                group["id"],
                projected_loss_usd=_decimal_string(dependent_projection),
                projected_loss_pct=_decimal_string(dependent_pct),
            )
            if dependent_projection > budget_usd:
                raise GroupLossLimitExceeded("loss_threshold_exceeded_after_intermediate_bridge")

        journal.save_checkpoint(position_id, "prepared", fresh)
        first_step = steps[0]
        if first_step.get("kind") == "swap_to_native":
            saved_swap = journal.checkpoint(position_id, "swap_result")
            if saved_swap is None:
                journal.record_position_state(position_id, "swap_submitted")
            try:
                swap_result = saved_swap or submit_step(
                    {
                        **fresh,
                        "_journal_position_id": position_id,
                        "_check_group_loss": check_dependent_route_loss,
                    },
                    first_step,
                )
                if swap_result.get("state") not in {"confirmed", "completed"}:
                    raise ValueError("source swap did not confirm")
                actual_asset = str(swap_result["actual_asset_id"])
                actual_balance = _raw_amount(swap_result["actual_balance_raw"])
                if saved_swap is None:
                    realized = _add_realized(realized, swap_result.get("realized_loss_usd", "0"))
                    journal.save_checkpoint(position_id, "swap_result", swap_result)
                journal.record_group_realized_loss(
                    group["id"], realized_loss_usd=_decimal_string(realized)
                )
                if saved_swap is None:
                    journal.record_position_state(
                        position_id,
                        "source_asset_converted_bridge_pending",
                        actual_asset_id=actual_asset,
                        actual_balance_raw=actual_balance,
                    )
            except StageDeadlineExceeded as exc:
                return yield_stage(position_id, exc)
            except Exception as exc:
                if entry.get("automatic_requote") and _retryable_provider_error(exc):
                    return yield_stage(position_id, StageDeadlineExceeded(_execution_reason(exc)))
                if isinstance(exc, AmbiguousBroadcast) and current_deadline() is not None:
                    return yield_stage(
                        position_id, StageDeadlineExceeded(f"broadcast_pending:{exc.tx_hash}")
                    )
                reason = _execution_reason(exc)
                journal.record_position_state(position_id, "manual_review", reason=reason)
                counts["manual_review"] += 1
                halt_remaining(index + 1, state="manual_review_group_halted", reason=reason)
                if index + 1 == len(entries):
                    journal.record_group_state(
                        group["id"], "manual_review_group_halted", reason=reason
                    )
                return counts

            original_bridge = next(
                (step for step in steps[1:] if step.get("kind") == "bridge"), None
            )
            try:
                if requote_after_swap is None or original_bridge is None:
                    raise ValueError("post-swap bridge re-quote is unavailable")
                saved_replacement = journal.checkpoint(position_id, "replacement")
                replacement = saved_replacement or requote_after_swap(fresh, actual_balance)
                if not isinstance(replacement, dict):
                    raise ValueError("post-swap bridge re-quote returned no route")
                old_route = original_bridge.get("route", {})
                new_route = replacement.get("route", replacement)
                evidence = new_route.get("evidence", {})
                if saved_replacement is None:
                    journal.record_requote_after_swap(
                        position_id,
                        old_route_id=str(old_route["id"]),
                        old_payload_hash=str(old_route["evidence"]["payload_sha256"]),
                        new_route_id=str(new_route["id"]),
                        new_payload_hash=str(evidence["payload_sha256"]),
                        input_amount_raw=_raw_amount(new_route.get("from_amount")),
                        actual_asset_id=actual_asset,
                    )
                next_steps = [first_step, replacement]
                fresh = {
                    **fresh,
                    "steps": next_steps,
                    "_journal_position_id": position_id,
                    "_actual_asset_id": actual_asset,
                    "_actual_balance_raw": str(actual_balance),
                }
                saved_bridge = journal.checkpoint(position_id, "bridge_prepared")
                if saved_bridge is not None and journal.has_broadcast(position_id):
                    fresh = saved_bridge
                else:
                    fresh = preflight(fresh)
                journal.save_checkpoint(position_id, "replacement", replacement)
                journal.save_checkpoint(position_id, "bridge_prepared", fresh)
                replacement = fresh["steps"][-1]
                current_loss = _entry_loss(fresh)
                projected = _exact_sum((realized, current_loss, future_loss))
                projected_pct = _exact_percentage(projected, source_usd)
                journal.record_group_projection(
                    group["id"],
                    projected_loss_usd=_decimal_string(projected),
                    projected_loss_pct=_decimal_string(projected_pct),
                )
                if projected > budget_usd:
                    journal.record_position_state(
                        position_id,
                        "manual_review_after_swap",
                        reason="loss_threshold_exceeded_after_swap",
                    )
                    counts["manual_review_after_swap"] += 1
                    halt_remaining(
                        index + 1,
                        state="manual_review_group_threshold_exceeded",
                        reason="loss_threshold_exceeded_after_swap",
                    )
                    return counts
                journal.record_position_state(position_id, "bridge_submitted")
                bridge_result = submit_step(
                    {
                        **fresh,
                        "_check_group_loss": check_dependent_route_loss,
                    },
                    replacement,
                )
                if bridge_result.get("state") == "deposit_pending":
                    return yield_stage(position_id, StageDeadlineExceeded("deposit_pending"))
                if bridge_result.get("state") not in {"confirmed", "completed"}:
                    raise ValueError("replacement bridge did not confirm")
                realized = _add_realized(realized, bridge_result.get("realized_loss_usd", "0"))
                journal.record_group_realized_loss(
                    group["id"], realized_loss_usd=_decimal_string(realized)
                )
            except GroupLossLimitExceeded as exc:
                reason = _execution_reason(exc)
                halt_remaining(
                    index,
                    state="manual_review_group_threshold_exceeded",
                    reason=reason,
                )
                return counts
            except StageDeadlineExceeded as exc:
                return yield_stage(position_id, exc)
            except Exception as exc:
                if entry.get("automatic_requote") and _retryable_provider_error(exc):
                    return yield_stage(position_id, StageDeadlineExceeded(_execution_reason(exc)))
                if isinstance(exc, AmbiguousBroadcast) and current_deadline() is not None:
                    return yield_stage(
                        position_id, StageDeadlineExceeded(f"broadcast_pending:{exc.tx_hash}")
                    )
                reason = _execution_reason(exc)
                current = journal.position(position_id)
                if current["state"] != "manual_review_after_swap":
                    journal.record_position_state(
                        position_id,
                        "manual_review_after_swap",
                        actual_asset_id=actual_asset,
                        actual_balance_raw=actual_balance,
                        reason=reason,
                    )
                counts["manual_review_after_swap"] += 1
                halt_remaining(
                    index + 1,
                    state="manual_review_group_halted",
                    reason=reason,
                )
                if index + 1 == len(entries):
                    journal.record_group_state(
                        group["id"], "manual_review_after_swap", reason=reason
                    )
                return counts
        else:
            for step in steps:
                try:
                    if step.get("kind") == "direct_deposit":
                        journal.record_position_state(position_id, "direct_deposit_submitted")
                    else:
                        journal.record_position_state(position_id, "bridge_submitted")
                    result = submit_step(
                        {
                            **fresh,
                            "_journal_position_id": position_id,
                            "_check_group_loss": check_dependent_route_loss,
                        },
                        step,
                    )
                    if result.get("state") == "deposit_pending":
                        realized = Decimal(
                            journal.account_position_loss(
                                position_id,
                                loss_usd=str(result.get("realized_loss_usd", _entry_loss(fresh))),
                            )
                        )
                        journal.record_position_state(
                            position_id, "deposit_pending", reason=result.get("reason")
                        )
                        counts["deposit_pending"] += 1
                        break
                    if result.get("state") not in {"confirmed", "completed"}:
                        raise ValueError("route step did not confirm")
                    if (step.get("kind") == "direct_deposit" or fresh.get("local_unwrap")
                            or journal.checkpoint(position_id, "canonical_prepared")):
                        realized = Decimal(
                            journal.account_position_loss(
                                position_id,
                                loss_usd=str(result.get("realized_loss_usd", _entry_loss(fresh))),
                            )
                        )
                    else:
                        realized = _add_realized(realized, result.get("realized_loss_usd", "0"))
                        journal.record_group_realized_loss(
                            group["id"], realized_loss_usd=_decimal_string(realized)
                        )
                except GroupLossLimitExceeded as exc:
                    reason = _execution_reason(exc)
                    halt_remaining(
                        index,
                        state="manual_review_group_threshold_exceeded",
                        reason=reason,
                    )
                    return counts
                except EthereumGasDeferred as exc:
                    if step.get("kind") != "direct_deposit":
                        reason = _execution_reason(exc)
                        journal.record_position_state(position_id, "manual_review", reason=reason)
                        counts["manual_review"] += 1
                        halt_remaining(index + 1, state="manual_review_group_halted", reason=reason)
                        if index + 1 == len(entries):
                            journal.record_group_state(
                                group["id"], "manual_review_group_halted", reason=reason
                            )
                        return counts
                    journal.record_position_state(position_id, "gas_deferred", reason=exc.reason)
                    journal.record_group_state(group["id"], "gas_deferred", reason=exc.reason)
                    counts["gas_deferred"] += 1
                    return counts
                except (DepositNotSeen, DepositSettlementFailed) as exc:
                    reason = _execution_reason(exc)
                    journal.record_position_state(position_id, "manual_review", reason=reason)
                    counts["manual_review"] += 1
                    halt_remaining(index + 1, state="manual_review_group_halted", reason=reason)
                    return counts
                except StageDeadlineExceeded as exc:
                    return yield_stage(position_id, exc)
                except Exception as exc:
                    if entry.get("automatic_requote") and _retryable_provider_error(exc):
                        return yield_stage(
                            position_id, StageDeadlineExceeded(_execution_reason(exc))
                        )
                    if isinstance(exc, AmbiguousBroadcast) and current_deadline() is not None:
                        return yield_stage(
                            position_id, StageDeadlineExceeded(f"broadcast_pending:{exc.tx_hash}")
                        )
                    reason = _execution_reason(exc)
                    if step.get("kind") == "direct_deposit":
                        saved = journal.latest_step(
                            position_id=position_id, step_key="direct_deposit"
                        )
                        if saved is not None and saved["tx_hash"]:
                            journal.record_position_state(
                                position_id, "deposit_pending", reason=reason
                            )
                            counts["deposit_pending"] += 1
                            journal.record_group_state(
                                group["id"], "deposit_pending", reason=reason
                            )
                            return counts
                    journal.record_position_state(position_id, "manual_review", reason=reason)
                    counts["manual_review"] += 1
                    halt_remaining(
                        index + 1,
                        state="manual_review_group_halted",
                        reason=reason,
                    )
                    if index + 1 == len(entries):
                        journal.record_group_state(
                            group["id"], "manual_review_group_halted", reason=reason
                        )
                    return counts

        if journal.position(position_id)["state"] != "deposit_pending":
            journal.record_position_state(position_id, "completed")
            counts["completed"] += 1

    has_pending = any(
        journal.position(position["id"])["state"] == "deposit_pending" for position in positions
    )
    journal.record_group_state(group["id"], "deposit_pending" if has_pending else "completed")
    return counts


def requote_after_swap_output(
    entry: dict, *, observed_output_raw: int, requote: Callable[[int], dict]
) -> dict:
    """Re-quote a dependent bridge from settled swap output under its original cap."""

    if (
        isinstance(observed_output_raw, bool)
        or not isinstance(observed_output_raw, int)
        or observed_output_raw <= 0
    ):
        raise ValueError("observed swap output must be a positive integer")
    try:
        cap = Decimal(str(entry["reservations"]["whole_position_loss_usd"]))
        route = requote(observed_output_raw)
        loss = Decimal(str(route["valuation"]["loss_usd"]))
    except (KeyError, InvalidOperation) as exc:
        raise ValueError("re-quote is missing a valid loss cap") from exc
    if not cap.is_finite() or not loss.is_finite() or loss > cap:
        raise ValueError("re-quoted bridge exceeds original loss cap")
    return route


def reconcile_bridge_observation(
    journal: Journal,
    *,
    step_id: int,
    observed_balance_raw: int,
    correlated_arrival_raw: int | None,
    confirmed_at_ms: int,
    now_ms: int,
) -> str:
    """Attribute a bridge only to provider-correlated arrival, never balance alone."""

    step = journal.step(step_id)
    if step["state"] in {"credited", "manual_review"}:
        return step["state"]
    baseline, expected = step["balance_baseline_raw"], step["expected_delta_raw"]
    if baseline is None or expected is None:
        raise ValueError("bridge observation baseline is missing")
    if correlated_arrival_raw is not None:
        if correlated_arrival_raw <= 0 or observed_balance_raw < baseline + correlated_arrival_raw:
            journal.record_timeout_report(step_id, "correlated_arrival_not_observed_in_balance")
            journal.record_step_state(step_id, "manual_review")
            return "manual_review"
        # Fee-reduced bridge arrivals are valid when the provider correlation
        # explains a positive actual amount; the final-deposit minimum is
        # independently revalidated before any exchange transfer.
        journal.record_step_state(step_id, "credited")
        return "credited"
    if now_ms - confirmed_at_ms >= 30 * 60 * 1000:
        journal.record_timeout_report(step_id, "no_correlated_arrival")
        return "no_correlated_arrival"
    journal.record_step_state(step_id, "awaiting_bridge")
    return "awaiting_bridge"


def resume_routes_read_only(*, journal_path: Path) -> dict[str, object]:
    """Return durable route status without loading credentials or performing I/O."""

    with Journal(journal_path, readonly=True) as journal:
        if journal.has_route_tables():
            step_counts = journal.route_step_state_counts()
            position_counts = journal.route_position_state_counts()
            group_counts = journal.route_group_state_counts()
            reasons = journal.route_reason_counts()
        else:
            step_counts = position_counts = group_counts = {}
            reasons = []
    return {
        "status": "read_only",
        "groups": group_counts,
        "positions": position_counts,
        "steps": step_counts,
        "reasons": reasons,
        "bridge_timeout": step_counts.get("bridge_timeout", 0),
        "manual_review": step_counts.get("manual_review", 0),
        "pending": sum(
            count
            for state, count in step_counts.items()
            if state not in {"bridge_timeout", "manual_review", "credited", "completed"}
        ),
    }


def _execution_deadline(seconds, entries):
    continuous = bool(entries) and all(e.get("automatic_requote") for e in entries)
    if continuous and current_deadline() is not None:
        check_deadline()
        remaining = current_deadline().remaining()
        if remaining is not None:
            seconds = min(seconds, remaining)
    return Deadline(seconds, "execution", max_seconds=86_400 if continuous else 30)


def execute_entries(
    entries: list[dict],
    *,
    wallets: dict[str, WalletWorkbookRow],
    rpc_urls: dict[int, str],
    journal_path: Path,
    execute: bool,
    delay_min_seconds: int = 0,
    delay_max_seconds: int = 0,
    sleep=time.sleep,
    rng: random.Random | None = None,
    wallet_batches: tuple[tuple[str, ...], ...] | None = None,
    now_ms: Callable[[], int] | None = None,
    max_route_loss_pct: Decimal = Decimal("15"),
    preflight_entry: Callable[[dict], dict] | None = None,
    submit_route_step: Callable[[dict, dict], dict] | None = None,
    requote_bridge: Callable[[dict, int], dict] | None = None,
    stage_seconds: float = 30,
    allow_duplicate_deposit_addresses: bool = False,
    trust_deposit_addresses: bool = False,
) -> dict[str, int]:
    """Execute one wallet at a time; refuses unless the caller set ``execute=True``."""

    excluded_groups = {
        str(entry.get("group", {}).get("key"))
        for entry in entries
        if uses_excluded_network(entry) and isinstance(entry.get("group"), dict)
    }
    entries = [
        entry for entry in entries
        if not uses_excluded_network(entry)
        and not (isinstance(entry.get("group"), dict)
                 and str(entry["group"].get("key")) in excluded_groups)
    ]
    require_trusted_deposit_addresses(trust_deposit_addresses)
    if not execute:
        raise ValueError("refusing to broadcast without --execute")
    if not entries or not all(e.get("automatic_requote") for e in entries):
        print(DEPOSIT_ADDRESS_WARNING, file=sys.stderr, flush=True)
    if allow_duplicate_deposit_addresses:
        print(
            "Проверка уникальности депозитных адресов отключена явным флагом.",
            file=sys.stderr, flush=True,
        )
    require_unique_deposit_addresses(
        ((row.row_number, row.bitget_deposit_address) for row in wallets.values()),
        allow_duplicates=allow_duplicate_deposit_addresses,
    )
    if delay_min_seconds < 0 or delay_max_seconds < delay_min_seconds:
        raise ValueError("invalid execution delay range")
    now_ms = now_ms or _utc_now_ms
    _execution_deadline(stage_seconds, entries)  # validate before allocating clients
    relay_entries = [e for e in entries if e.get("routing_provider") == "relay"]
    if relay_entries:
        if preflight_entry is not None or submit_route_step is not None:
            raise ValueError("Relay execution cannot use LI.FI injected step callbacks")
        from .relay_execution import execute_relay_entries

        relay_summary = execute_relay_entries(
            relay_entries, wallets=wallets, rpc_urls=rpc_urls,
            journal_path=journal_path, bitget=_bitget_client(),
            target_validator=_require_live_target, position_key=_position_key,
            max_route_loss_pct=max_route_loss_pct, stage_seconds=stage_seconds,
            execute=execute, trust_deposit_addresses=trust_deposit_addresses,
        )
        remaining = [e for e in entries if e.get("routing_provider") != "relay"]
        if remaining:
            other = execute_entries(
                remaining, wallets=wallets, rpc_urls=rpc_urls,
                journal_path=journal_path, execute=execute,
                delay_min_seconds=delay_min_seconds, delay_max_seconds=delay_max_seconds,
                sleep=sleep, rng=rng, wallet_batches=wallet_batches, now_ms=now_ms,
                max_route_loss_pct=max_route_loss_pct, requote_bridge=requote_bridge,
                stage_seconds=stage_seconds,
                allow_duplicate_deposit_addresses=allow_duplicate_deposit_addresses,
                trust_deposit_addresses=trust_deposit_addresses,
            )
            for key, value in other.items():
                relay_summary[key] = relay_summary.get(key, 0) + value
        return relay_summary
    _validate_final_deposit_targets(entries)
    v4_entries = [
        entry
        for entry in entries
        if entry.get("status") in {"route_ready", "direct_deposit"}
        and isinstance(entry.get("group"), dict)
    ]
    if v4_entries and (preflight_entry is not None or submit_route_step is not None):
        if preflight_entry is None or submit_route_step is None:
            raise ValueError("version-4 execution requires preflight and submit callbacks")
        return _execute_injected_groups(
            v4_entries,
            journal_path=journal_path,
            max_route_loss_pct=max_route_loss_pct,
            preflight_entry=preflight_entry,
            submit_route_step=submit_route_step,
            requote_bridge=requote_bridge,
            stage_seconds=stage_seconds,
        )
    if v4_entries:
        return _execute_live_groups(
            v4_entries,
            wallets=wallets,
            rpc_urls=rpc_urls,
            journal_path=journal_path,
            max_route_loss_pct=max_route_loss_pct,
            now_ms=now_ms,
            delay_min_seconds=delay_min_seconds,
            delay_max_seconds=delay_max_seconds,
            sleep=sleep,
            rng=rng,
            stage_seconds=stage_seconds,
        )
    _validate_route_quote_freshness(entries, now_ms=now_ms)
    rng = rng or random.Random()
    transport = Transport(interval=0.2)
    rpc = ExecutionRpc(transport)
    lifi_clients = WalletLifiClients(wallets.values(), db_path=journal_path, purpose="execution")
    bitget = _bitget_client()
    summary = {"submitted": 0, "skipped": 0, "failed": 0, "deferred": 0}
    by_wallet: dict[str, list[dict]] = {}
    for entry in entries:
        if entry.get("status") in {"route_ready", "direct_deposit", "post_bridge_deposit"}:
            by_wallet.setdefault(str(entry["wallet"]).lower(), []).append(entry)
    try:
        with Journal(journal_path) as journal:
            batches = wallet_batches or (tuple(sorted(by_wallet)),)
            for batch_index, batch in enumerate(batches):
                wallet_order = list(batch)
                rng.shuffle(wallet_order)
                for wallet in wallet_order:
                    wallet_entries = by_wallet.get(wallet, [])
                    if not wallet_entries:
                        continue
                    source = wallets.get(wallet)
                    if source is None:
                        summary["skipped"] += len(wallet_entries)
                        continue
                    jumper = lifi_clients.for_wallet(wallet)
                    with deadline_scope(_execution_deadline(stage_seconds, wallet_entries)):
                        for entry in wallet_entries:
                            operation_id = journal.create_operation(
                                wallet=wallet, action=str(entry["status"])
                            )
                            position = journal.get_or_create_position(
                                position_key=_position_key(entry), wallet=wallet
                            )
                            if not journal.has_broadcast(position["id"]):
                                _validate_route_quote_freshness([entry], now_ms=now_ms)
                            try:
                                check_deadline()
                                if not journal.has_broadcast(position["id"]):
                                    _validate_route_quote_freshness([entry], now_ms=now_ms)
                                    if entry["status"] in {"direct_deposit", "post_bridge_deposit"}:
                                        _revalidate_final_deposit_target(bitget, entry, source)
                                started_ms = int(time.time() * 1000)
                                tx_hash = _execute_entry(
                                    entry=entry,
                                    wallet=source,
                                    rpc=rpc,
                                    jumper=jumper,
                                    rpc_urls=rpc_urls,
                                    now_ms=now_ms,
                                    journal=journal,
                                    position_id=position["id"],
                                )
                                journal.record_transaction(operation_id, tx_hash)
                                if approval_tx_hash := getattr(tx_hash, "approval_tx_hash", None):
                                    journal.record_approval_hash(operation_id, approval_tx_hash)
                                if entry.get("settlement") == "wallet":
                                    journal.record_deposit_status(operation_id, "staged")
                                elif entry["status"] in {"direct_deposit", "post_bridge_deposit"}:
                                    status = bitget.wait_for_deposit(
                                        tx_hash=tx_hash,
                                        started_ms=started_ms,
                                        coin=str(entry["target"]["coin"]),
                                        chain=str(entry["target"]["chain"]),
                                        recipient=source.bitget_deposit_address,
                                        minimum_raw=int(entry["target"]["minimum_raw"]),
                                    )
                                    journal.record_deposit_status(
                                        operation_id, (status or "timeout").lower()
                                    )
                                summary["submitted"] += 1
                            except StageDeadlineExceeded as exc:
                                if journal.has_broadcast(position["id"]):
                                    summary["pending"] = summary.get("pending", 0) + 1
                                else:
                                    journal.record_deferred(operation_id, str(exc))
                                    summary["deferred"] += 1
                                break
                            except AmbiguousBroadcast as exc:
                                journal.record_transaction(operation_id, exc.tx_hash)
                                summary["submitted"] += 1
                            except RouteQuoteExpired as exc:
                                if exc.approval_tx_hash is None:
                                    journal.record_deferred(operation_id, str(exc))
                                else:
                                    journal.record_approval_completed_route_deferred(
                                        operation_id, exc.approval_tx_hash, str(exc)
                                    )
                                summary["deferred"] += 1
                            except EthereumGasDeferred as exc:
                                if exc.approval_tx_hash is None:
                                    journal.record_deferred(operation_id, exc.reason)
                                else:
                                    journal.record_approval_completed_route_deferred(
                                        operation_id, exc.approval_tx_hash, exc.reason
                                    )
                                summary["deferred"] += 1
                            except Exception as exc:
                                journal.record_deposit_status(
                                    operation_id, "execution_failed", f"{type(exc).__name__}: {exc}"
                                )
                                summary["failed"] += 1
                    if wallet != wallet_order[-1]:
                        sleep(rng.randint(delay_min_seconds, delay_max_seconds))
                if batch_index != len(batches) - 1 and batch:
                    sleep(rng.randint(delay_min_seconds, delay_max_seconds))
    finally:
        lifi_clients.close()
        transport.close()
    return summary


def _validate_route_quote_freshness(entries: list[dict], *, now_ms: Callable[[], int]) -> None:
    current_ms = now_ms()
    for entry in entries:
        if entry.get("status") != "route_ready":
            continue
        quoted_at = entry.get("quoted_at")
        if isinstance(quoted_at, bool) or not isinstance(quoted_at, int):
            raise ValueError("route quote must have an integer quoted_at timestamp")
        age_ms = current_ms - quoted_at
        if age_ms < 0 or age_ms > 900_000:
            raise ValueError("route quote is outside the 15-minute execution window")


def _execute_injected_groups(
    entries: list[dict],
    *,
    journal_path: Path,
    max_route_loss_pct: Decimal,
    preflight_entry: Callable[[dict], dict],
    submit_route_step: Callable[[dict, dict], dict],
    requote_bridge: Callable[[dict, int], dict] | None,
    stage_seconds: float = 30,
) -> dict[str, int]:
    groups: dict[str, list[dict]] = {}
    for entry in entries:
        group = entry["group"]
        key = str(group.get("key") or f"{entry['wallet']}:{entry['chain_id']}")
        groups.setdefault(key, []).append(entry)
    summary: dict[str, int] = {}
    deadlines: dict[str, Deadline] = {}
    with Journal(journal_path) as journal:
        for key in sorted(groups):
            wallet_deadline = deadlines.setdefault(
                str(groups[key][0]["wallet"]).lower(), _execution_deadline(stage_seconds, entries)
            )
            with deadline_scope(wallet_deadline):
                result = execute_group_entries(
                    groups[key],
                    journal=journal,
                    max_route_loss_pct=max_route_loss_pct,
                    preflight=preflight_entry,
                    submit_step=submit_route_step,
                    requote_after_swap=requote_bridge,
                )
            for state, count in result.items():
                if state != "group_id" and isinstance(count, int):
                    summary[state] = summary.get(state, 0) + count
    return summary


def _execute_live_groups(
    entries: list[dict],
    *,
    wallets: dict[str, WalletWorkbookRow],
    rpc_urls: dict[int, str],
    journal_path: Path,
    max_route_loss_pct: Decimal,
    now_ms: Callable[[], int],
    delay_min_seconds: int,
    delay_max_seconds: int,
    sleep: Callable[[float], None],
    rng: random.Random | None,
    stage_seconds: float = 30,
) -> dict[str, int]:
    transport = Transport(interval=0.2)
    rpc = ExecutionRpc(transport)
    lifi_clients = WalletLifiClients(wallets.values(), db_path=journal_path, purpose="execution")
    bitget = _bitget_client()
    groups: dict[str, list[dict]] = {}
    for entry in entries:
        group = entry["group"]
        group_key = str(group.get("key") or f"{entry['wallet']}:{entry['chain_id']}")
        groups.setdefault(group_key, []).append(entry)
    summary: dict[str, int] = {}
    deadlines: dict[str, Deadline] = {}
    randomizer = rng or random.Random()
    ordered_groups = sorted(groups)
    randomizer.shuffle(ordered_groups)
    wallet_order = list(
        dict.fromkeys(str(groups[key][0]["wallet"]).lower() for key in ordered_groups)
    )
    ordered_groups.sort(key=lambda key: wallet_order.index(str(groups[key][0]["wallet"]).lower()))
    previous_wallet: str | None = None

    try:
        with (
            Journal(journal_path) as journal,
            _DepositBackground(bitget=bitget, journal=journal) as background,
        ):
            for group_key in ordered_groups:
                background.raise_if_failed()
                group_entries = groups[group_key]
                wallet_address = str(group_entries[0]["wallet"]).lower()
                wallet = wallets.get(wallet_address)
                if wallet is None:
                    summary["skipped"] = summary.get("skipped", 0) + len(group_entries)
                    continue
                if previous_wallet is not None and previous_wallet != wallet_address:
                    sleep(randomizer.randint(delay_min_seconds, delay_max_seconds))
                previous_wallet = wallet_address
                jumper = lifi_clients.for_wallet(wallet_address)
                gas_estimator = PlannerGasEstimator(
                    rpc, rpc_urls, jumper,
                    retry_transient_errors=all(e.get("automatic_requote") for e in group_entries),
                )

                def preflight(entry: dict) -> dict:
                    position = journal.get_or_create_position(
                        position_key=_position_key(entry), wallet=entry["wallet"]
                    )
                    approval_hashes = []
                    if entry.get("automatic_requote") and not journal.has_asset_broadcast(
                        position["id"]
                    ):
                        for approval in journal.connection.execute(
                            "SELECT tx_hash FROM route_steps WHERE position_id=? "
                            "AND step_key LIKE 'approval:%' AND tx_hash IS NOT NULL",
                            (position["id"],),
                        ).fetchall():
                            wait_for_receipt(rpc, url=_rpc_url(rpc_urls, entry["chain_id"]),
                                             tx_hash=approval[0])
                            approval_hashes.append((entry["chain_id"], approval[0]))
                    fresh = _preflight_live_entry(
                        entry,
                        wallet=wallet,
                        rpc=rpc,
                        rpc_urls=rpc_urls,
                        jumper=jumper,
                        bitget=bitget,
                        gas_estimator=gas_estimator,
                        now_ms=now_ms,
                    )
                    if approval_hashes:
                        expense = _actual_gas_paid_usd(
                            approval_hashes, rpc=rpc, rpc_urls=rpc_urls, jumper=jumper,
                        )
                        fresh["_realized_loss_before_current_usd"] = _decimal_string(expense)
                        fresh["valuation"]["loss_usd"] = _decimal_string(
                            _exact_sum((_entry_loss(fresh), expense))
                        )
                    return fresh

                def submit(entry: dict, step: dict) -> dict:
                    background.raise_if_failed()

                    def requote_dependent(
                        original: dict, asset: AssetIdentity, amount: int
                    ) -> dict:
                        return _requote_from_intermediate(
                            original,
                            asset,
                            amount,
                            wallet=wallet,
                            rpc=rpc,
                            rpc_urls=rpc_urls,
                            jumper=jumper,
                            bitget=bitget,
                            gas_estimator=gas_estimator,
                            now_ms=now_ms,
                        )

                    result = _submit_live_step(
                        {
                            **entry,
                            "_requote_after_intermediate": requote_dependent,
                            "_sleep": sleep,
                            "_before_sign": background.raise_if_failed,
                        },
                        step,
                        wallet=wallet,
                        rpc=rpc,
                        rpc_urls=rpc_urls,
                        jumper=jumper,
                        bitget=bitget,
                        journal=journal,
                        now_ms=now_ms,
                    )
                    if result.get("state") == "deposit_pending" and current_deadline() is None:
                        background.watch(
                            entry=entry,
                            position_id=int(entry["_journal_position_id"]),
                            recipient=wallet.bitget_deposit_address,
                            loss_usd=str(result.get("realized_loss_usd", _entry_loss(entry))),
                        )
                    return result

                def requote(entry: dict, amount: int) -> dict:
                    return _requote_bridge_after_swap(
                        entry,
                        amount,
                        wallet=wallet,
                        jumper=jumper,
                        bitget=bitget,
                        gas_estimator=gas_estimator,
                        now_ms=now_ms,
                    )

                wallet_deadline = deadlines.setdefault(
                        wallet_address, _execution_deadline(stage_seconds, entries)
                )
                with deadline_scope(wallet_deadline):
                    result = execute_group_entries(
                        group_entries,
                        journal=journal,
                        max_route_loss_pct=max_route_loss_pct,
                        preflight=preflight,
                        submit_step=submit,
                        requote_after_swap=requote,
                        before_step=background.raise_if_failed,
                    )
                for state, count in result.items():
                    if state != "group_id" and isinstance(count, int):
                        summary[state] = summary.get(state, 0) + count
            settled = background.finish()
            summary["completed"] = summary.get("completed", 0) + settled
            summary["deposit_pending"] = summary.get("deposit_pending", 0) - settled
    finally:
        lifi_clients.close()
        transport.close()
    return summary


def _preflight_live_entry(
    entry: dict,
    *,
    wallet: WalletWorkbookRow,
    rpc: ExecutionRpc,
    rpc_urls: dict[int, str],
    jumper: LifiClient,
    bitget: BitgetClient,
    gas_estimator: PlannerGasEstimator,
    now_ms: Callable[[], int],
) -> dict:
    target = _require_live_target(entry, wallet=wallet, bitget=bitget)
    if entry.get("local_unwrap") and entry["status"] != "direct_deposit":
        steps = entry.get("steps", [])
        if (
            target.chain_id != int(entry["chain_id"])
            or target.asset_id != "native"
            or target.decimals != 18
        ):
            raise ValueError("requote_required:local_unwrap_target_invalid")
        if len(steps) != 1 or steps[0].get("kind") != "unwrap_to_native":
            raise ValueError("requote_required:local_unwrap_steps_invalid")
    now = datetime.fromtimestamp(now_ms() / 1000, UTC)
    if entry["status"] == "direct_deposit":
        asset = AssetIdentity(
            int(entry["chain_id"]), str(entry["asset_id"]), int(entry["decimals"])
        )
        target = _require_live_target(entry, wallet=wallet, bitget=bitget)
        if (
            asset.chain_id != target.chain_id
            or asset.contract_address != target.asset_id
            or asset.decimals != target.decimals
        ):
            raise ValueError("requote_required:target_identity_mismatch")
        url = _rpc_url(rpc_urls, asset.chain_id)
        balance = _asset_balance(rpc, url=url, asset=asset, wallet=wallet.public_address)
        if entry.get("_actual_balance_raw") is not None:
            balance = min(balance, _raw_amount(entry["_actual_balance_raw"]))
        if balance <= 0:
            raise ValueError("requote_required:insufficient_spendable_balance")
        fresh = {**entry, "raw_balance": str(balance)}
        request = _direct_request(fresh, wallet=wallet, rpc=rpc, url=url)
        if request.gas_limit <= 0:
            request = FeePlanner(rpc).plan(url, request, sender=wallet.public_address)
        request = replace(request, max_total_fee_cap_wei=request.max_total_fee_wei)
        if _is_celo_erc20(asset):
            balance = int(request.data[-64:], 16)
            fresh["raw_balance"] = str(balance)
        if request.value < target.minimum_raw and asset.is_native:
            raise ValueError("requote_required:below_target_minimum")
        if balance < target.minimum_raw and not asset.is_native:
            raise ValueError("requote_required:below_target_minimum")
        native = native_asset_identity(asset.chain_id)
        fee = _gas_fee_quote(request.max_total_fee_wei, native, jumper)
        price = _token_quote_price(jumper, asset)
        candidate = candidate_from_values(
            route_ids=(),
            source_asset=asset,
            destination_asset=asset,
            source_amount=(request.value if asset.is_native else balance),
            destination_amount=(request.value if asset.is_native else balance),
            source_price=price,
            destination_price=price,
            wallet_paid_gas=(fee,),
            gas_estimate_complete=True,
            now=datetime.fromtimestamp(now_ms() / 1000, UTC),
            max_price_age=_MAX_PRICE_AGE,
        )
        _require_valid_candidate(candidate)
        fresh["steps"] = [
            {
                "kind": "direct_deposit",
                "asset": _asset_dict(asset),
                "amount_raw": str(request.value if asset.is_native else balance),
                "recipient": wallet.bitget_deposit_address,
                "target": entry["target"],
            }
        ]
        fresh["_direct_transaction"] = request
        fresh["_candidate"] = candidate
        fresh["valuation"] = _candidate_valuation(candidate)
        fresh["reservations"] = {
            **entry.get("reservations", {}),
            "final_deposit_native_gas_cap_raw": str(fee.raw_amount),
        }
        return fresh

    steps = entry.get("steps")
    if not isinstance(steps, list) or not steps:
        raise ValueError("requote_required:route_steps_missing")
    actual_balance_raw = entry.get("_actual_balance_raw")
    actual_asset_id = entry.get("_actual_asset_id")
    if actual_balance_raw is not None:
        if len(steps) != 2 or steps[-1].get("kind") != "bridge":
            raise ValueError("requote_required:post_swap_route_shape_changed")
        bridge_candidate, bridge_step = _refresh_route_step(
            entry,
            steps[-1],
            wallet=wallet,
            jumper=jumper,
            gas_estimator=gas_estimator,
            now=now,
            now_fn=lambda: datetime.fromtimestamp(now_ms() / 1000, UTC),
            bitget=bitget,
            source_amount=int(actual_balance_raw),
            actual_asset_id=str(actual_asset_id),
        )
        fresh = {**entry, "steps": [steps[0], bridge_step]}
        fresh["_candidate"] = bridge_candidate
        fresh["_step_candidates"] = {bridge_step["route"]["id"]: bridge_candidate}
        fresh["valuation"] = _candidate_valuation(bridge_candidate)
        return fresh

    candidates = []
    fresh_steps = []
    step_candidates: dict[str, object] = {}
    for step in steps:
        candidate, fresh_step = _refresh_route_step(
            entry,
            step,
            wallet=wallet,
            jumper=jumper,
            gas_estimator=gas_estimator,
            now=now,
            now_fn=lambda: datetime.fromtimestamp(now_ms() / 1000, UTC),
            bitget=bitget,
        )
        candidates.append(candidate)
        fresh_steps.append(fresh_step)
        step_candidates[fresh_step["route"]["id"]] = candidate
    candidate = candidates[0]
    for next_candidate in candidates[1:]:
        candidate = combine_candidates(candidate, next_candidate)
    if entry.get("local_unwrap"):
        native = native_asset_identity(target.chain_id)
        direct_gas = gas_estimator(
            purpose="direct_deposit",
            route=None,
            wallet=wallet.public_address,
            source_asset=native,
            target=target,
            amount=candidate.destination_amount,
            recipient=wallet.bitget_deposit_address,
        )
        price = _quote_price(jumper, native)
        direct_candidate = candidate_from_values(
            route_ids=(),
            source_asset=native,
            destination_asset=native,
            source_amount=candidate.destination_amount,
            destination_amount=candidate.destination_amount,
            source_price=price,
            destination_price=price,
            wallet_paid_gas=direct_gas or (),
            gas_estimate_complete=direct_gas is not None,
            now=datetime.fromtimestamp(now_ms() / 1000, UTC),
            max_price_age=_MAX_PRICE_AGE,
        )
        _require_valid_candidate(direct_candidate)
        candidate = combine_candidates(candidate, direct_candidate)
        gas_cap = sum(fee.raw_amount for fee in candidates[0].gas_estimates)
        direct_cap = sum(fee.raw_amount for fee in direct_gas)
        if (
            candidate.destination_amount - gas_cap - GAS_RESERVE_MULTIPLIER * direct_cap
            < target.minimum_raw
        ):
            raise ValueError("requote_required:below_target_minimum")
        entry = {**entry, "reservations": {
            **entry.get("reservations", {}),
            "final_deposit_native_gas_cap_raw": str(direct_cap),
        }}
    _require_valid_candidate(candidate)
    fresh = {**entry, "steps": fresh_steps}
    if entry.get("automatic_requote"):
        fresh["quoted_at"] = now_ms()
        if len(fresh_steps) == 1:
            fresh["route"] = fresh_steps[0]["route"]
    fresh["_candidate"] = candidate
    fresh["_step_candidates"] = step_candidates
    fresh["valuation"] = _candidate_valuation(candidate)
    return fresh


def _refresh_route_step(
    entry: dict,
    step: dict,
    *,
    wallet: WalletWorkbookRow,
    jumper: LifiClient,
    gas_estimator: PlannerGasEstimator,
    now: datetime,
    bitget: BitgetClient,
    now_fn: Callable[[], datetime] | None = None,
    source_amount: int | None = None,
    actual_asset_id: str | None = None,
    _spend_amount: int | None = None,
    _reserve_attempt: int = 0,
) -> tuple[object, dict]:
    route_data = step.get("route")
    if not isinstance(route_data, dict) or not isinstance(route_data.get("evidence"), dict):
        raise ValueError("requote_required:route_evidence_missing")
    evidence = route_data["evidence"]
    source = _asset_from_data(evidence["source"])
    destination = _asset_from_data(evidence["destination"])
    amount = int(evidence["input_amount"])
    if source_amount is not None and source_amount < amount:
        raise ValueError("requote_required:insufficient_generated_native_balance")
    if actual_asset_id is not None and source.contract_address != actual_asset_id:
        raise ValueError("requote_required:actual_asset_identity_changed")
    recipient = (
        wallet.public_address
        if step.get("kind") in {"swap_to_native", "unwrap_to_native"}
        else wallet.bitget_deposit_address
    )
    planned = _route_from_dict(
        json.loads(evidence["canonical_payload_json"]),
        quote_timestamp=evidence["quote_timestamp"],
    )
    if step.get("kind") == "unwrap_to_native":
        if not entry.get("local_unwrap") or len(planned.raw_steps) != 1:
            raise ValueError("requote_required:local_unwrap_steps_invalid")
        withdraw_transaction_fields(planned.raw_steps[0])
        target = _require_live_target(entry, wallet=wallet, bitget=bitget)
        if (
            destination != native_asset_identity(int(entry["chain_id"]))
            or source
            != AssetIdentity(int(entry["chain_id"]), str(entry["asset_id"]), int(entry["decimals"]))
            or amount != int(entry["raw_balance"])
            or planned.raw_steps[0]["action"]["fromAddress"].lower()
            != wallet.public_address.lower()
            or (target.chain_id, target.asset_id, target.decimals)
            != (destination.chain_id, destination.contract_address, destination.decimals)
        ):
            raise ValueError("requote_required:local_unwrap_identity_invalid")
    live = step.get("_live_route")
    if step.get("kind") == "unwrap_to_native" and entry.get("automatic_requote"):
        planned = live = _canonical_unwrap_route(
            jumper, source, amount, wallet, now_fn or (lambda: now))
    allow_replacement = bool(entry.get("automatic_requote")) and (
        source_amount is None and not entry.get("_journal_position_id")
        and step.get("kind") != "unwrap_to_native"
    )
    native_available = None
    prepared_cap = 0
    if allow_replacement and source.is_native:
        native_available = _asset_balance(
            gas_estimator.rpc, url=_rpc_url(gas_estimator.rpc_urls, source.chain_id),
            asset=source, wallet=wallet.public_address,
        )
        hint = sum(cost.amount for cost in planned.gas_costs if cost.token == source)
        saved_cap = entry.get("reservations", {}).get("source_native_gas_cap_raw")
        prepared_cap = _raw_amount(saved_cap) if saved_cap is not None else 0
        if _spend_amount is not None:
            amount = _spend_amount
        else:
            reserve = ((prepared_cap * GAS_RESERVE_MULTIPLIER * 12 + 9) // 10
                       if prepared_cap else hint * GAS_RESERVE_MULTIPLIER * 2)
            hinted_available = native_available - reserve
            amount = min(amount, hinted_available if hinted_available > 0
                         else max(1, native_available // 2))
        if native_available <= 0:
            raise ValueError("insufficient_source_gas")
        if amount <= 0:
            raise ValueError("insufficient_source_gas")
        if amount != planned.evidence.input_amount:
            live = None
    if not isinstance(live, LifiRoute):
        request = LifiRouteRequest(
            from_chain_id=source.chain_id,
            to_chain_id=destination.chain_id,
            from_token_address=_lifi_token(source),
            to_token_address=_lifi_token(destination),
            from_amount=str(amount),
            from_address=wallet.public_address,
            to_address=recipient,
        )
        routes = jumper.routes(request)
        live = next(
            (route for route in routes if route.route_id == route_data.get("id")),
            None,
        )
        if live is None and allow_replacement:
            for option in routes:
                try:
                    validate_bridge_route(option, expected_source=source,
                                          expected_destination=destination,
                                          input_amount=amount, recipient=recipient)
                except ValueError:
                    continue
                live = option
                break
        if live is None:
            raise ValueError("requote_required:route_id_changed")
    if allow_replacement:
        validate_bridge_route(live, expected_source=source, expected_destination=destination,
                              input_amount=amount, recipient=recipient)
    else:
        revalidate_route_evidence(
            planned,
            live,
            expected_source=source,
            expected_destination=destination,
            expected_input_amount=amount,
            recipient=recipient,
        )
    if native_available is not None:
        available = native_available
    elif source_amount is not None:
        available = source_amount
    elif not (
        step.get("kind") == "bridge" and entry.get("steps", [])[0].get("kind") == "swap_to_native"
    ):
        available = _asset_balance(
            gas_estimator.rpc,
            url=_rpc_url(gas_estimator.rpc_urls, source.chain_id),
            asset=source,
            wallet=wallet.public_address,
        )
    else:
        available = amount
    if available < amount:
        raise ValueError("requote_required:insufficient_spendable_balance")
    prepared_requests = tuple(jumper.step_transaction(raw_step) for raw_step in live.raw_steps)
    extra_gas = gas_estimator.estimate_prepared_route(
        route=live,
        wallet=wallet.public_address,
        prepared_transactions=prepared_requests,
    )
    if native_available is not None and extra_gas:
        fee_cap = sum(f.raw_amount for f in extra_gas if f.price and f.price.asset == source)
        spendable = native_available - GAS_RESERVE_MULTIPLIER * fee_cap
        if amount > spendable or (not prepared_cap and _reserve_attempt == 0
                                  and amount < spendable):
            if spendable <= 0:
                raise ValueError("insufficient_source_gas")
            if _reserve_attempt >= 2:
                raise ValueError("native_gas_reserve_unstable")
            padded_spendable = native_available - (
                fee_cap * GAS_RESERVE_MULTIPLIER * 12 + 9) // 10
            return _refresh_route_step(
                entry, {k: v for k, v in step.items() if k != "_live_route"}, wallet=wallet,
                jumper=jumper, gas_estimator=gas_estimator, now=now, bitget=bitget,
                now_fn=now_fn,
                _spend_amount=padded_spendable if padded_spendable > 0 else spendable,
                _reserve_attempt=_reserve_attempt + 1,
            )
    prepared_requests = getattr(gas_estimator, "prepared_fee_caps", {}).get(
        live.route_id, prepared_requests
    )
    candidate = candidate_from_route(
        live,
        expected_source=source,
        expected_destination=destination,
        expected_input_amount=amount,
        recipient=recipient,
        gas_estimate_complete=extra_gas is not None,
        now=now_fn() if now_fn is not None else now,
        max_price_age=_MAX_PRICE_AGE,
        wallet_paid_gas=extra_gas or (),
        replace_route_gas=bool(extra_gas),
    )
    _require_valid_candidate(candidate)
    if step.get("kind") not in {"swap_to_native", "unwrap_to_native"}:
        try:
            _require_target_minimum(entry, bitget, wallet, candidate)
        except ValueError as error:
            if (str(error) != "requote_required:below_target_minimum"
                    or native_available is None or not extra_gas
                    or _reserve_attempt >= 2 or amount >= spendable):
                raise
            return _refresh_route_step(
                entry, {k: v for k, v in step.items() if k != "_live_route"}, wallet=wallet,
                jumper=jumper, gas_estimator=gas_estimator, now=now, bitget=bitget,
                now_fn=now_fn, _spend_amount=spendable,
                _reserve_attempt=_reserve_attempt + 1,
            )
    fresh_step = {
        **step,
        "route": _route_data(live),
        "_live_route": live,
        "_prepared_requests": prepared_requests,
        "_approval_fee_caps": dict(getattr(gas_estimator, "approval_fee_caps", {})),
    }
    if len(prepared_requests) == 1:
        fresh_step["_prepared_request"] = prepared_requests[0]
    return candidate, fresh_step


def _requote_from_intermediate(
    entry: dict,
    asset: AssetIdentity,
    amount: int,
    *,
    wallet: WalletWorkbookRow,
    rpc: ExecutionRpc,
    rpc_urls: dict[int, str],
    jumper: LifiClient,
    bitget: BitgetClient,
    gas_estimator: PlannerGasEstimator,
    now_ms: Callable[[], int],
) -> dict:
    """Build a fresh, gas-priced route from a correlated intermediate arrival."""

    target = _require_live_target(entry, wallet=wallet, bitget=bitget)
    native_target_resolver = getattr(bitget, "native_deposit_target", None)
    if (entry.get("automatic_requote") and entry.get("_journal_position_id")
            and asset.contract_address == WETH_CONTRACTS.get(asset.chain_id)
            and asset.decimals == 18 and callable(native_target_resolver)):
        native_target = native_target_resolver(asset.chain_id)
        if native_target is not None:
            native_target_data = {
                "coin": native_target.coin, "chain_id": native_target.chain_id,
                "asset_id": native_target.asset_id, "decimals": native_target.decimals,
                "chain": native_target.chain, "minimum_raw": str(native_target.minimum_raw),
            }
            route = _canonical_unwrap_route(jumper, asset, amount, wallet,
                lambda: datetime.fromtimestamp(now_ms() / 1000, UTC))
            dynamic = {key: value for key, value in entry.items()
                       if key not in {"_actual_asset_id", "_actual_balance_raw"}}
            dynamic.update(status="route_ready", local_unwrap=True, chain_id=asset.chain_id,
                asset_id=asset.contract_address, decimals=18, raw_balance=str(amount),
                target=native_target_data, quoted_at=now_ms(),
                steps=[{"kind": "unwrap_to_native", "route": _route_data(route)}],
                canonical_continuation={"wallet": wallet.public_address,
                    "recipient": wallet.bitget_deposit_address, "amount_raw": str(amount),
                    "chain_id": asset.chain_id, "target": native_target_data,
                    "previous_target": entry["target"]})
            return _preflight_live_entry(dynamic, wallet=wallet, rpc=rpc, rpc_urls=rpc_urls,
                jumper=jumper, bitget=bitget, gas_estimator=gas_estimator, now_ms=now_ms)
    dynamic = {
        **entry,
        "status": "direct_deposit",
        "chain_id": asset.chain_id,
        "asset_id": asset.contract_address,
        "decimals": asset.decimals,
        "raw_balance": str(amount),
        "_actual_asset_id": asset.contract_address,
        "_actual_balance_raw": str(amount),
        "quoted_at": now_ms(),
    }
    if (
        asset.chain_id == target.chain_id
        and asset.contract_address == target.asset_id
        and asset.decimals == target.decimals
    ):
        dynamic["steps"] = [{"kind": "direct_deposit"}]
        return _preflight_live_entry(
            dynamic,
            wallet=wallet,
            rpc=rpc,
            rpc_urls=rpc_urls,
            jumper=jumper,
            bitget=bitget,
            gas_estimator=gas_estimator,
            now_ms=now_ms,
        )

    request = LifiRouteRequest(
        from_chain_id=asset.chain_id,
        to_chain_id=target.chain_id,
        from_token_address=_lifi_token(asset),
        to_token_address=_lifi_token(
            AssetIdentity(target.chain_id, target.asset_id, target.decimals)
        ),
        from_amount=str(amount),
        from_address=wallet.public_address,
        to_address=wallet.bitget_deposit_address,
    )
    routes = jumper.routes(request)
    candidates = []
    stale_price = False
    for route in routes:
        try:
            validate_bridge_route(
                route,
                expected_source=asset,
                expected_destination=AssetIdentity(
                    target.chain_id, target.asset_id, target.decimals
                ),
                recipient=wallet.bitget_deposit_address,
                input_amount=amount,
            )
            prepared = tuple(jumper.step_transaction(raw_step) for raw_step in route.raw_steps)
            extra_gas = gas_estimator.estimate_prepared_route(
                route=route,
                wallet=wallet.public_address,
                prepared_transactions=prepared,
            )
            candidate = candidate_from_route(
                route,
                expected_source=asset,
                expected_destination=AssetIdentity(
                    target.chain_id, target.asset_id, target.decimals
                ),
                expected_input_amount=amount,
                recipient=wallet.bitget_deposit_address,
                gas_estimate_complete=extra_gas is not None,
                now=datetime.fromtimestamp(now_ms() / 1000, UTC),
                max_price_age=_MAX_PRICE_AGE,
                wallet_paid_gas=extra_gas or (),
                replace_route_gas=bool(extra_gas),
            )
            _require_valid_candidate(candidate)
            _require_target_minimum(dynamic, bitget, wallet, candidate)
        except (ValueError, LifiError) as exc:
            stale_price |= str(exc) == "requote_required:stale_price"
            continue
        candidates.append((route, candidate, prepared))
    if not candidates:
        if stale_price:
            raise ValueError("requote_required:stale_price")
        raise ValueError("requote_required:no_eligible_route_after_intermediate_bridge")
    route, candidate, prepared = min(
        candidates,
        key=lambda item: (
            item[1].loss_pct,
            item[1].loss_usd,
            item[0].route_id,
        ),
    )
    route_step = {
        "kind": "bridge",
        "route": _route_data(route),
        "_live_route": route,
        "_prepared_requests": prepared,
    }
    dynamic.update(
        {
            "status": "route_ready",
            "steps": [route_step],
            "_candidate": candidate,
            "_step_candidates": {route.route_id: candidate},
            "valuation": _candidate_valuation(candidate),
        }
    )
    return dynamic


def _requote_bridge_after_swap(
    entry: dict,
    amount: int,
    *,
    wallet: WalletWorkbookRow,
    jumper: LifiClient,
    bitget: BitgetClient,
    gas_estimator: PlannerGasEstimator,
    now_ms: Callable[[], int],
) -> dict:
    planned_bridge = next((step for step in entry["steps"] if step.get("kind") == "bridge"), None)
    if planned_bridge is None:
        raise ValueError("requote_required:planned_bridge_missing")
    route_data = planned_bridge["route"]
    target_identity = _asset_from_data(route_data["evidence"]["destination"])
    source = native_asset_identity(int(entry["chain_id"]))
    if amount <= 0:
        raise ValueError("requote_required:empty_swap_output")
    live_target = _require_live_target(entry, wallet=wallet, bitget=bitget)
    candidates = []
    stale_price = False
    initial_request = LifiRouteRequest(
        from_chain_id=source.chain_id,
        to_chain_id=target_identity.chain_id,
        from_token_address=_lifi_token(source),
        to_token_address=_lifi_token(target_identity),
        from_amount=str(amount),
        from_address=wallet.public_address,
        to_address=wallet.bitget_deposit_address,
    )
    for initial_route in jumper.routes(initial_request):
        initial_transactions = tuple(
            jumper.step_transaction(raw_step) for raw_step in initial_route.raw_steps
        )
        initial_gas = gas_estimator.estimate_prepared_route(
            route=initial_route,
            wallet=wallet.public_address,
            prepared_transactions=initial_transactions,
        )
        if not initial_gas:
            continue
        source_gas = sum(
            quote.raw_amount
            for quote in initial_gas
            if quote.price is not None and quote.price.asset == source
        )
        spendable = amount - source_gas * GAS_RESERVE_MULTIPLIER
        if source_gas <= 0 or spendable <= 0:
            continue
        reserved = amount - spendable
        adjusted_request = LifiRouteRequest(
            from_chain_id=source.chain_id,
            to_chain_id=target_identity.chain_id,
            from_token_address=_lifi_token(source),
            to_token_address=_lifi_token(target_identity),
            from_amount=str(spendable),
            from_address=wallet.public_address,
            to_address=wallet.bitget_deposit_address,
        )
        for route in jumper.routes(adjusted_request):
            prepared_requests = tuple(
                jumper.step_transaction(raw_step) for raw_step in route.raw_steps
            )
            extra_gas = gas_estimator.estimate_prepared_route(
                route=route,
                wallet=wallet.public_address,
                prepared_transactions=prepared_requests,
            )
            if not extra_gas:
                continue
            final_source_gas = sum(
                quote.raw_amount
                for quote in extra_gas
                if quote.price is not None and quote.price.asset == source
            )
            if final_source_gas <= 0 or reserved < final_source_gas * GAS_RESERVE_MULTIPLIER:
                continue
            candidate = candidate_from_route(
                route,
                expected_source=source,
                expected_destination=target_identity,
                expected_input_amount=spendable,
                recipient=wallet.bitget_deposit_address,
                gas_estimate_complete=True,
                now=datetime.fromtimestamp(now_ms() / 1000, UTC),
                max_price_age=_MAX_PRICE_AGE,
                wallet_paid_gas=extra_gas,
                replace_route_gas=True,
            )
            stale_price |= candidate.reason == "stale_price"
            if (
                candidate.is_valid
                and candidate.destination_amount is not None
                and candidate.destination_amount >= live_target.minimum_raw
            ):
                candidates.append((route, candidate, prepared_requests))
    if not candidates:
        if stale_price:
            raise ValueError("requote_required:stale_price")
        raise ValueError("no eligible bridge route")
    route, candidate, prepared_requests = min(
        candidates,
        key=lambda pair: (
            pair[1].loss_pct,
            pair[1].loss_usd,
            pair[1].route_ids,
        ),
    )
    result = {
        "kind": "bridge",
        "route": _route_data(route),
        "_live_route": route,
        "valuation": _candidate_valuation(candidate),
    }
    if len(prepared_requests) == 1:
        result["_prepared_request"] = prepared_requests[0]
    return result


def _pending_bridge_sleep(sleep: Callable[[float], None]) -> None:
    if current_deadline() is not None:
        raise StageDeadlineExceeded("bridge_pending")
    budget_sleep(_BRIDGE_STATUS_POLL_SECONDS, sleep=sleep)


def _await_cross_chain_transfer(
    *,
    jumper: LifiClient,
    rpc: ExecutionRpc,
    rpc_urls: dict[int, str],
    journal: Journal,
    position_id: int,
    step_key: str,
    tx_hash: str,
    bridge: str,
    source: AssetIdentity,
    destination: AssetIdentity,
    sender: str,
    recipient: str,
    baseline_raw: int | None,
    sleep: Callable[[float], None],
) -> int:
    """Wait for LI.FI's correlated completion and confirm the exact destination asset."""

    saved = journal.latest_step(position_id=position_id, step_key=step_key)
    step_id = saved["id"] if saved is not None else None
    if saved is not None:
        state = str(saved["state"])
        if state == "submitted":
            journal.record_step_state(step_id, "confirmed")
            state = "confirmed"
        if state == "confirmed":
            journal.record_step_state(step_id, "awaiting_bridge")

    while True:
        check_deadline()
        try:
            status = jumper.transaction_status(
                tx_hash=tx_hash,
                from_chain_id=source.chain_id,
                to_chain_id=destination.chain_id,
                bridge=bridge,
            )
        except httpx.TransportError:
            _pending_bridge_sleep(sleep)
            continue
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code not in {408, 429, 500, 502, 503, 504}:
                raise
            _pending_bridge_sleep(sleep)
            continue
        except StageDeadlineExceeded:
            raise
        except TimeoutError:
            _pending_bridge_sleep(sleep)
            continue
        state = str(status.get("status", "")).upper()
        substatus = str(status.get("substatus", "")).upper()
        if state == "NOT_FOUND":
            _pending_bridge_sleep(sleep)
            continue
        sending = status.get("sending")
        if not isinstance(sending, dict):
            raise ValueError("lifi_bridge_status_missing_source_evidence")
        sending_hash = sending.get("txHash")
        if not isinstance(sending_hash, str) or sending_hash.lower() != tx_hash.lower():
            raise ValueError("lifi_bridge_status_source_hash_mismatch")
        observed_sender = status.get("fromAddress")
        if isinstance(observed_sender, str) and observed_sender.lower() != sender.lower():
            raise ValueError("lifi_bridge_status_sender_mismatch")
        if state == "PENDING":
            _pending_bridge_sleep(sleep)
            continue
        if state != "DONE" or substatus not in {"COMPLETED", "PARTIAL"}:
            raise ValueError(f"lifi_bridge_terminal_status:{state}:{substatus}")
        if not isinstance(observed_sender, str):
            raise ValueError("lifi_bridge_status_sender_missing")
        receiving = status.get("receiving")
        if not isinstance(receiving, dict):
            raise ValueError("lifi_bridge_status_missing_destination_evidence")
        if str(status.get("toAddress", "")).lower() != recipient.lower():
            raise ValueError("lifi_bridge_status_recipient_mismatch")

        receiving_hash = receiving.get("txHash")
        if not isinstance(receiving_hash, str) or not receiving_hash:
            raise ValueError("lifi_bridge_status_destination_hash_missing")
        received_raw = _raw_amount(receiving.get("amount"))
        token = receiving.get("token")
        identity_fields = {"address", "chainId", "decimals"}
        if isinstance(token, dict) and identity_fields.intersection(token):
            if not identity_fields.issubset(token):
                raise ValueError("lifi_bridge_status_asset_incomplete")
            try:
                observed_asset = AssetIdentity(
                    int(token["chainId"]),
                    "native"
                    if str(token["address"]).lower() == _NATIVE_LIFI_ADDRESS
                    else str(token["address"]),
                    int(token["decimals"]),
                )
            except (TypeError, ValueError) as exc:
                raise ValueError("lifi_bridge_status_asset_invalid") from exc
            if observed_asset != destination:
                raise ValueError("lifi_bridge_status_asset_mismatch")

        if recipient.lower() == sender.lower():
            if baseline_raw is None:
                raise ValueError("lifi_bridge_balance_baseline_missing")
            observed_balance = _asset_balance(
                rpc,
                url=_rpc_url(rpc_urls, destination.chain_id),
                asset=destination,
                wallet=recipient,
            )
            if observed_balance < baseline_raw + received_raw:
                _pending_bridge_sleep(sleep)
                continue

        journal.save_checkpoint(position_id, f"bridge_delivery:{step_key}", {
            "source_hash": tx_hash, "tx_hash": receiving_hash, "recipient": recipient,
            "chain_id": destination.chain_id, "asset_id": destination.contract_address,
            "decimals": destination.decimals, "amount_raw": str(received_raw),
        })
        if step_id is not None:
            latest_state = str(journal.step(step_id)["state"])
            if latest_state == "confirmed":
                journal.record_step_state(step_id, "awaiting_bridge")
                latest_state = "awaiting_bridge"
            if latest_state == "awaiting_bridge":
                journal.record_step_state(step_id, "credited")
        return received_raw


def route_credit_unverified(entry, journal, position_id):
    return (bool(entry.get("automatic_requote")) and entry.get("status") == "route_ready"
            and entry.get("routing_provider") != "relay" and not entry.get("local_unwrap")
            and isinstance(entry.get("target"), dict)
            and any(s.get("kind") == "bridge" for s in entry.get("steps", []))
            and (journal.checkpoint(position_id, "bitget_credit") or {}).get("status") != "success")


def _observe_route_deposit(entry, *, wallet, jumper, rpc, rpc_urls, bitget, journal,
                            position_id, now_ms):
    if journal.checkpoint(position_id, "route_deposit"):
        if journal.checkpoint(position_id, "route_deposit")["target"] != entry.get("target"):
            entry = _saved_native_settlement_entry(entry, wallet, journal, position_id)
        return _poll_route_deposit(entry, wallet=wallet, bitget=bitget, journal=journal,
                                   position_id=position_id, now_ms=now_ms)
    prepared = (journal.checkpoint(position_id, "bridge_prepared")
                or journal.checkpoint(position_id, "prepared"))
    if not isinstance(prepared, dict):
        raise DepositNotSeen("route_deposit_observation_context_missing")
    seen = set()
    while True:
        step = prepared["steps"][-1]
        if step.get("kind") == "direct_deposit":
            direct = journal.checkpoint(position_id, "direct_prepared") or prepared
            saved = journal.latest_step(position_id=position_id,
                                        step_key=_direct_step_key(journal, position_id))
            if saved is None or not saved["tx_hash"]:
                raise DepositNotSeen("route_deposit_direct_hash_missing")
            started_ms = int(datetime.fromisoformat(saved["created_at"]).replace(
                tzinfo=UTC).timestamp() * 1000) - 60_000
            journal.save_checkpoint(position_id, "route_deposit", {
                "target": direct["target"], "recipient": wallet.bitget_deposit_address,
                "tx_hash": saved["tx_hash"], "source_hash": saved["tx_hash"],
                "step_key": saved["step_key"], "started_ms": started_ms,
                "realized_loss_usd": _decimal_string(_exact_sum((
                    _entry_loss(direct), Decimal(direct.get(
                        "_realized_loss_before_current_usd", "0"))))),
            })
            observed = entry
            if direct["target"] != entry.get("target"):
                observed = _saved_native_settlement_entry(entry, wallet, journal, position_id)
            return _poll_route_deposit(observed, wallet=wallet, bitget=bitget, journal=journal,
                                       position_id=position_id, now_ms=now_ms)
        if step.get("kind") not in {"bridge", "unwrap_to_native"} or not isinstance(
                step.get("_live_route"), LifiRoute):
            raise DepositNotSeen("route_deposit_observation_context_invalid")
        key = f"route:{step['kind']}:{step['route']['id']}"
        if key in seen:
            raise ValueError("route_deposit_observation_context_cycle")
        seen.add(key)
        progress = journal.checkpoint(position_id, key) or {}
        continuation = progress.get("continuation")
        if not isinstance(continuation, dict):
            break
        prepared = continuation
    if progress.get("next_index", 0) != len(step["_live_route"].raw_steps):
        raise DepositNotSeen("route_deposit_delivery_incomplete")
    normalized = tuple(s for s in step["_live_route"].evidence.steps if not s.is_substep)
    return _settle_route_deposit(
        prepared, step, normalized[-1], result={"realized_loss_usd": _decimal_string(
            _exact_sum((_entry_loss(prepared), Decimal(prepared.get(
                "_realized_loss_before_current_usd", "0")))))}, wallet=wallet,
        jumper=jumper, rpc=rpc, rpc_urls=rpc_urls, bitget=bitget, journal=journal,
        position_id=position_id, now_ms=now_ms, sleep=entry.get("_sleep", time.sleep),
    )


def _saved_native_settlement_entry(entry, wallet, journal, position_id):
    """Bind an altered final target to the saved canonical conversion and direct intent."""
    prepared = (journal.checkpoint(position_id, "bridge_prepared")
                or journal.checkpoint(position_id, "prepared") or {})
    seen = set()
    migrated = False
    while prepared.get("steps"):
        step = prepared["steps"][-1]
        if step.get("kind") == "direct_deposit":
            direct = journal.checkpoint(position_id, "direct_prepared")
            if (not migrated or not direct or direct.get("target") != prepared.get("target")
                    or direct.get("chain_id") != prepared["target"]["chain_id"]
                    or direct.get("asset_id") != "native" or direct.get("decimals") != 18):
                break
            return direct
        if step.get("kind") not in {"bridge", "unwrap_to_native"}:
            break
        key = f"route:{step['kind']}:{step['route']['id']}"
        if key in seen:
            break
        seen.add(key)
        progress = journal.checkpoint(position_id, key) or {}
        child = progress.get("continuation")
        if not isinstance(child, dict):
            break
        if child.get("target") != prepared.get("target"):
            marker = child.get("canonical_continuation") or {}
            source_key = marker.get("source_step_key", "")
            prefix = f"bridge:{step['route']['id']}:"
            normalized = tuple(s for s in step["_live_route"].evidence.steps if not s.is_substep)
            try:
                index = int(source_key.removeprefix(prefix))
                source_step = (normalized[index]
                               if source_key.startswith(prefix) and index >= 0 else None)
            except (ValueError, IndexError):
                source_step = None
            saved = journal.latest_step(position_id=position_id, step_key=source_key)
            delivery = journal.checkpoint(position_id, f"bridge_delivery:{source_key}") or {}
            target = child.get("target") or {}
            if (migrated or source_step is None or source_step.destination is None
                    or (source_step.destination.chain_id, source_step.destination.contract_address,
                        source_step.destination.decimals)
                    != (child.get("chain_id"), child.get("asset_id"), 18)
                    or str(source_step.recipient).lower() != wallet.public_address.lower()
                    or marker.get("previous_target") != prepared.get("target")
                    or marker.get("target") != target
                    or marker.get("wallet", "").lower() != wallet.public_address.lower()
                    or marker.get("recipient", "").lower() != wallet.bitget_deposit_address.lower()
                    or child.get("asset_id") != WETH_CONTRACTS.get(child.get("chain_id"))
                    or (target.get("coin"), target.get("chain_id"), target.get("asset_id"),
                        target.get("decimals")) != ("ETH", child.get("chain_id"), "native", 18)
                    or not saved or saved["state"] != "credited"
                    or saved["tx_hash"] != delivery.get("source_hash")
                    or delivery.get("recipient", "").lower() != wallet.public_address.lower()
                    or (delivery.get("chain_id"), delivery.get("asset_id"),
                        delivery.get("decimals"))
                    != (child.get("chain_id"), child.get("asset_id"), 18)
                    or delivery.get("amount_raw") != marker.get("amount_raw")):
                break
            migrated = True
        if step.get("kind") == "unwrap_to_native":
            unwrap = journal.latest_step(position_id=position_id,
                step_key=f"unwrap_to_native:{step['route']['id']}:0")
            if not unwrap or not unwrap["tx_hash"] or not progress.get("unwrap_result"):
                break
            withdraw_transaction_fields(step["_live_route"].raw_steps[0])
        prepared = child
    raise ValueError("route_deposit_native_continuation_identity_mismatch")


def _settle_route_deposit(entry, step, final_step, *, result, wallet, jumper, rpc,
                          rpc_urls, bitget, journal, position_id, now_ms, sleep):
    target = entry["target"]
    destination = final_step.destination
    if (destination is None or final_step.source is None
            or (destination.chain_id, destination.contract_address, destination.decimals)
            != (int(target["chain_id"]), str(target["asset_id"]).lower(), int(target["decimals"]))
            or str(final_step.recipient).lower() != wallet.bitget_deposit_address.lower()):
        raise ValueError("route_deposit_identity_mismatch")
    step_key = f"{step.get('kind')}:{step['route']['id']}:{len(step['_live_route'].raw_steps)-1}"
    saved = journal.latest_step(position_id=position_id, step_key=step_key)
    if saved is None or not saved["tx_hash"]:
        raise ValueError("route_deposit_hash_missing")
    tx_hash = str(saved["tx_hash"])
    if final_step.source.chain_id != destination.chain_id:
        delivery = journal.checkpoint(position_id, f"bridge_delivery:{step_key}")
        if delivery is None:
            _await_cross_chain_transfer(
                jumper=jumper, rpc=rpc, rpc_urls=rpc_urls, journal=journal,
                position_id=position_id, step_key=step_key, tx_hash=tx_hash,
                bridge=final_step.provider or "", source=final_step.source,
                destination=destination, sender=wallet.public_address,
                recipient=wallet.bitget_deposit_address, baseline_raw=None, sleep=sleep,
            )
            delivery = journal.checkpoint(position_id, f"bridge_delivery:{step_key}")
        if (delivery is None or str(delivery.get("source_hash")).lower() != tx_hash.lower()
                or str(delivery.get("recipient")).lower() != wallet.bitget_deposit_address.lower()
                or (delivery.get("chain_id"), delivery.get("asset_id"), delivery.get("decimals"))
                != (destination.chain_id, destination.contract_address, destination.decimals)):
            raise ValueError("route_deposit_delivery_mismatch")
        tx_hash = str(delivery["tx_hash"])
    started_ms = int(datetime.fromisoformat(saved["created_at"]).replace(tzinfo=UTC).timestamp()
                     * 1000) - 60_000
    journal.save_checkpoint(position_id, "route_deposit", {
        "target": target, "recipient": wallet.bitget_deposit_address,
        "tx_hash": tx_hash, "source_hash": saved["tx_hash"], "step_key": step_key,
        "started_ms": started_ms, "realized_loss_usd": result["realized_loss_usd"],
    })
    return _poll_route_deposit(entry, wallet=wallet, bitget=bitget, journal=journal,
                               position_id=position_id, now_ms=now_ms)


def _poll_route_deposit(entry, *, wallet, bitget, journal, position_id, now_ms):
    context = journal.checkpoint(position_id, "route_deposit")
    if (not isinstance(context, dict) or context.get("target") != entry.get("target")
            or str(context.get("recipient")).lower() != wallet.bitget_deposit_address.lower()):
        raise ValueError("route_deposit_observation_identity_mismatch")
    saved = journal.latest_step(position_id=position_id, step_key=context["step_key"])
    if saved is None or saved["tx_hash"] != context.get("source_hash"):
        raise ValueError("route_deposit_observation_source_mismatch")
    target, tx_hash = context["target"], context["tx_hash"]
    result = {"realized_loss_usd": context["realized_loss_usd"]}
    try:
        status = bitget.wait_for_deposit_seen(
            tx_hash=tx_hash, started_ms=context["started_ms"], coin=str(target["coin"]),
            chain=str(target["chain"]), recipient=wallet.bitget_deposit_address,
            minimum_raw=int(target["minimum_raw"]),
        )
    except StageDeadlineExceeded:
        raise
    except Exception as error:
        raise DepositNotSeen(_execution_reason(error)) from error
    status = str(status or "").lower()
    if status in {"fail", "failed"}:
        raise DepositSettlementFailed("bitget_deposit_failed")
    if status not in {"", "pending", "success"}:
        raise DepositSettlementFailed("bitget_deposit_unknown_status")
    if status != "success":
        return {**result, "state": "deposit_pending",
                "reason": "bitget_seen_pending" if status else "bitget_deposit_not_seen"}
    if journal.step(saved["id"])["state"] == "submitted":
        journal.record_step_state(saved["id"], "deposit_seen")
    if journal.step(saved["id"])["state"] != "credited":
        journal.record_step_state(saved["id"], "credited")
    journal.save_checkpoint(position_id, "bitget_credit", {
        "tx_hash": tx_hash, "coin": target["coin"], "chain": target["chain"],
        "recipient": wallet.bitget_deposit_address, "status": "success", "observed_at": now_ms(),
    })
    return {**result, "state": "completed"}


def _actual_gas_paid_usd(
    tx_hashes: list[tuple[int, str]],
    *,
    rpc: ExecutionRpc,
    rpc_urls: dict[int, str],
    jumper: LifiClient,
) -> Decimal:
    total = Decimal(0)
    native_prices: dict[int, QuotePrice] = {}
    for chain_id, tx_hash in tx_hashes:
        receipt = rpc.call(_rpc_url(rpc_urls, chain_id), "eth_getTransactionReceipt", [tx_hash])
        if not isinstance(receipt, dict):
            raise ValueError("route_step_receipt_missing")
        if receipt.get("gasUsed") is None or receipt.get("effectiveGasPrice") is None:
            raise ValueError("route_step_receipt_gas_cost_missing")
        gas_raw = quantity(receipt["gasUsed"]) * quantity(receipt["effectiveGasPrice"]) + (
            quantity(receipt["l1Fee"]) if receipt.get("l1Fee") is not None else 0
        )
        if gas_raw <= 0:
            continue
        price = native_prices.get(chain_id)
        if price is None:
            price = _quote_price(jumper, native_asset_identity(chain_id))
            native_prices[chain_id] = price
        assert isinstance(price.usd_per_token, Decimal)
        total = _exact_sum((total, raw_to_decimal(gas_raw, price.asset) * price.usd_per_token))
    return total


def _continue_local_unwrap(
    entry: dict,
    result: dict,
    *,
    position_id: int,
    progress_key: str,
    progress: dict,
    wallet: WalletWorkbookRow,
    rpc: ExecutionRpc,
    rpc_urls: dict[int, str],
    jumper: LifiClient,
    bitget: BitgetClient,
    journal: Journal,
    now_ms: Callable[[], int],
) -> dict:
    """Persist the withdrawal output before preparing its dependent deposit."""
    target = _require_live_target(entry, wallet=wallet, bitget=bitget)
    native = native_asset_identity(int(entry["chain_id"]))
    if (target.chain_id, target.asset_id, target.decimals) != (
        native.chain_id,
        native.contract_address,
        native.decimals,
    ):
        raise ValueError("requote_required:local_unwrap_target_invalid")
    amount = _raw_amount(result["actual_balance_raw"])
    if result["actual_asset_id"] != native.contract_address:
        raise ValueError("requote_required:local_unwrap_output_invalid")
    progress["unwrap_result"] = result
    journal.save_checkpoint(position_id, progress_key, progress)
    journal.record_position_state(
        position_id,
        "source_asset_converted_bridge_pending",
        actual_asset_id=native.contract_address,
        actual_balance_raw=amount,
    )
    requote = entry.get("_requote_after_intermediate")
    if not callable(requote):
        raise ValueError("requote_required:local_unwrap_deposit_preflight_unavailable")
    dynamic = requote(entry, native, amount)
    if (
        not isinstance(dynamic, dict)
        or dynamic.get("status") != "direct_deposit"
        or len(dynamic.get("steps", [])) != 1
        or dynamic["steps"][0].get("kind") != "direct_deposit"
        or dynamic.get("_actual_asset_id") != native.contract_address
        or _raw_amount(dynamic.get("_actual_balance_raw")) > amount
    ):
        raise ValueError("requote_required:local_unwrap_deposit_shape_invalid")
    accrued = _exact_money(result["realized_loss_usd"], "unwrap loss")
    group_check = entry.get("_check_group_loss")
    if callable(group_check):
        group_check(_exact_sum((accrued, _entry_loss(dynamic))))
    dynamic.update(
        _journal_position_id=position_id,
        _realized_loss_before_current_usd=_decimal_string(accrued),
    )
    dynamic.pop("_observation_restore", None)
    dynamic.update({key: value for key, value in entry.items() if callable(value)})
    progress["continuation"] = dynamic
    journal.save_checkpoint(position_id, progress_key, progress)
    return _submit_live_step(
        dynamic,
        dynamic["steps"][0],
        wallet=wallet,
        rpc=rpc,
        rpc_urls=rpc_urls,
        jumper=jumper,
        bitget=bitget,
        journal=journal,
        now_ms=now_ms,
    )


def _submit_live_step(
    entry: dict,
    step: dict,
    *,
    wallet: WalletWorkbookRow,
    rpc: ExecutionRpc,
    rpc_urls: dict[int, str],
    jumper: LifiClient,
    bitget: BitgetClient,
    journal: Journal,
    now_ms: Callable[[], int],
) -> dict:
    position_id = int(entry["_journal_position_id"])
    if step.get("kind") == "observe_route_deposit":
        return _observe_route_deposit(
            entry, wallet=wallet, jumper=jumper, rpc=rpc, rpc_urls=rpc_urls,
            bitget=bitget, journal=journal, position_id=position_id, now_ms=now_ms,
        )
    if step.get("kind") == "direct_deposit":
        journal.save_checkpoint(position_id, "direct_prepared", entry)
        recovery = journal.checkpoint(position_id, "direct_recovery")
        step_key = _direct_step_key(journal, position_id)
        if recovery:
            entry = {**entry, "_journal_step_key": step_key, "_observation_restore": True}
        if recovery and recovery.get("kind") == "scroll_l1_fee":
            original_receipt = rpc.call(_rpc_url(rpc_urls, int(entry["chain_id"])),
                "eth_getTransactionReceipt", [recovery["failed_tx_hash"]])
            if isinstance(original_receipt, dict):
                if quantity(original_receipt.get("status")) != 1:
                    raise DepositSettlementFailed("original_replacement_transaction_reverted")
                recovery["winner_step_key"] = "direct_deposit"
                journal.save_checkpoint(position_id, "direct_recovery", recovery)
                entry = {**recovery["original_entry"], "_journal_position_id": position_id}
                step_key = "direct_deposit"
            else:
                entry.update(
                    _replacement_nonce=recovery["replacement_nonce"],
                    _replacement_original_request=recovery["original_entry"]["_direct_transaction"],
                    _actual_balance_raw=str(recovery["original_entry"]["_direct_transaction"].value),
                )
        saved = journal.latest_step(position_id=position_id, step_key=step_key)
        if (saved and saved["tx_hash"] and not recovery and entry.get("automatic_requote")
                and entry.get("chain_id") == 534352 and entry.get("asset_id") == "native"):
            receipt = rpc.call(_rpc_url(rpc_urls, 534352),
                               "eth_getTransactionReceipt", [saved["tx_hash"]])
            if receipt is None:
                replacement = _recover_missing_scroll_direct(
                    entry=entry, saved=saved, position_id=position_id, wallet=wallet,
                    rpc=rpc, rpc_urls=rpc_urls, jumper=jumper, bitget=bitget,
                    journal=journal, now_ms=now_ms,
                )
                if replacement is not None:
                    entry = replacement
                    step_key = entry["_journal_step_key"]
                    saved = None
        if (saved is not None and saved["tx_hash"] and entry.get("automatic_requote")
                and (entry.get("chain_id"), entry.get("asset_id"), entry.get("decimals"))
                == (CELO_CHAIN_ID, CELO_TOKEN_ADDRESS, 18)):
            receipt = rpc.call(_rpc_url(rpc_urls, int(entry["chain_id"])),
                               "eth_getTransactionReceipt", [saved["tx_hash"]])
            if isinstance(receipt, dict) and quantity(receipt.get("status")) == 0:
                entry = _recover_reverted_celo_direct(
                    entry=entry, saved=saved, receipt=receipt, position_id=position_id,
                    wallet=wallet, rpc=rpc, rpc_urls=rpc_urls, jumper=jumper,
                    bitget=bitget, journal=journal, now_ms=now_ms,
                )
                step_key = entry["_journal_step_key"]
                saved = None
        if saved is not None and saved["tx_hash"]:
            tx_hash = str(saved["tx_hash"])
            started_ms = (
                int(
                    datetime.fromisoformat(saved["created_at"]).replace(tzinfo=UTC).timestamp()
                    * 1000
                )
                - 60_000
            )
            if current_deadline() is not None:
                request = entry.get("_direct_transaction")
                if (entry.get("automatic_requote") and isinstance(request, TransactionRequest)
                        and request.max_total_fee_cap_wei is not None):
                    _replay_missing_direct(entry=entry, saved=saved, wallet=wallet, rpc=rpc,
                                           rpc_urls=rpc_urls, journal=journal)
                receipt = wait_for_receipt(
                    rpc,
                    url=_rpc_url(
                        rpc_urls,
                        int(saved["chain_id"]) if "chain_id" in saved else int(entry["chain_id"]),
                    ),
                    tx_hash=tx_hash,
                )
                if recovery and recovery.get("kind") == "scroll_l1_fee":
                    recovery["winner_step_key"] = step_key
                    journal.save_checkpoint(position_id, "direct_recovery", recovery)
            # Recovery observes the known transaction; it never prepares or signs another.
        else:
            if entry.get("_observation_restore") and (
                current_deadline() is not None or entry.get("local_unwrap")
            ):
                entry = _preflight_live_entry(
                    entry,
                    wallet=wallet,
                    rpc=rpc,
                    rpc_urls=rpc_urls,
                    jumper=jumper,
                    bitget=bitget,
                    gas_estimator=PlannerGasEstimator(rpc, rpc_urls, jumper),
                    now_ms=now_ms,
                )
                group_check = entry.get("_check_group_loss")
                if callable(group_check):
                    group_check(_exact_sum((_entry_loss(entry), Decimal(entry.get(
                        "_realized_loss_before_current_usd", "0")))))
                if recovery:
                    loss = _exact_sum((_entry_loss(entry), Decimal(recovery["failed_fee_usd"])))
                    group = journal.group(journal.position(position_id)["group_id"])
                    budget = Decimal(group["source_usd"]) * Decimal(group["loss_budget_pct"]) / 100
                    if loss > budget:
                        raise GroupLossLimitExceeded("reverted_deposit_recovery_exceeds_budget")
                    entry["valuation"] = {**entry["valuation"], "loss_usd": _decimal_string(loss)}
                journal.save_checkpoint(position_id, "direct_prepared", entry)
            request = entry.get("_direct_transaction")
            if not isinstance(request, TransactionRequest):
                raise ValueError("direct deposit preflight is missing a transaction")
            started_ms = now_ms()
            tx_hash = _execute_entry(
                entry=entry,
                wallet=wallet,
                rpc=rpc,
                jumper=jumper,
                rpc_urls=rpc_urls,
                now_ms=now_ms,
                journal=journal,
                position_id=position_id,
                prepared_request=request,
            )
            saved = journal.latest_step(position_id=position_id, step_key=step_key)
        realized_before = _exact_money(
            entry.get("_realized_loss_before_current_usd", "0"),
            "realized loss before direct deposit",
        )
        loss_usd = _decimal_string(
            _exact_sum((realized_before,
                        _exact_money(entry["valuation"]["loss_usd"], "direct deposit loss")))
        )
        try:
            status = bitget.wait_for_deposit_seen(
                tx_hash=tx_hash,
                started_ms=started_ms,
                coin=str(entry["target"]["coin"]),
                chain=str(entry["target"]["chain"]),
                recipient=wallet.bitget_deposit_address,
                minimum_raw=int(entry["target"]["minimum_raw"]),
            )
        except StageDeadlineExceeded:
            raise
        except Exception as exc:
            raise DepositNotSeen(_execution_reason(exc)) from exc
        normalized_status = str(status or "").lower()
        if not normalized_status and current_deadline() is not None:
            return {
                "state": "deposit_pending",
                "reason": "bitget_deposit_not_seen",
                "realized_loss_usd": loss_usd,
            }
        if not normalized_status:
            raise DepositNotSeen("bitget_deposit_not_seen")
        if normalized_status in {"fail", "failed"}:
            raise DepositSettlementFailed("bitget_deposit_failed")
        if normalized_status not in {"pending", "success"}:
            raise DepositSettlementFailed("bitget_deposit_unknown_status")
        if saved is not None:
            if saved["state"] == "submitted":
                journal.record_step_state(saved["id"], "deposit_seen")
            if normalized_status == "success" and saved["state"] in {
                "submitted",
                "deposit_seen",
                "confirmed",
                "awaiting_bridge",
            }:
                journal.record_step_state(saved["id"], "credited")
        if normalized_status == "pending":
            return {
                "state": "deposit_pending",
                "reason": "bitget_seen_pending",
                "realized_loss_usd": loss_usd,
            }
        journal.save_checkpoint(position_id, "bitget_credit", {
            "tx_hash": tx_hash, "coin": entry["target"]["coin"],
            "chain": entry["target"]["chain"], "recipient": wallet.bitget_deposit_address,
            "status": "success", "observed_at": now_ms(),
        })
        return {
            "state": "completed",
            "realized_loss_usd": loss_usd,
        }

    route_data = step.get("route")
    if not isinstance(route_data, dict):
        raise ValueError("requote_required:route_transaction_missing")
    live_route = step.get("_live_route")
    prepared_requests = step.get("_prepared_requests")
    if not isinstance(live_route, LifiRoute) or not isinstance(prepared_requests, tuple):
        raise ValueError("requote_required:prepared_transaction_missing")
    evidence = live_route.evidence
    if evidence is None or len(prepared_requests) != len(live_route.raw_steps):
        raise ValueError("requote_required:route_step_evidence_mismatch")
    route_steps = tuple(item for item in evidence.steps if not item.is_substep)
    if len(route_steps) != len(live_route.raw_steps):
        raise ValueError("requote_required:route_step_evidence_mismatch")
    for index, route_step in enumerate(route_steps[:-1]):
        if (
            route_step.source is None
            or route_step.destination is None
            or route_step.source == route_step.destination
            or route_step.recipient is None
            or route_step.recipient.lower() != wallet.public_address.lower()
        ):
            raise ValueError("requote_required:unsupported_dependent_route_step")
        if route_steps[index + 1].source != route_step.destination:
            raise ValueError("requote_required:route_step_identity_mismatch")

    progress_key = f"route:{step.get('kind')}:{route_data['id']}"
    progress = journal.checkpoint(position_id, progress_key) or {}
    continuation = progress.get("continuation")
    if isinstance(continuation, dict):
        proof = unsigned_delivery(journal, position_id)
        requote = entry.get("_requote_after_intermediate")
        if proof and proof["progress_key"] == progress_key and callable(requote):
            delivery = proof["delivery"]
            asset = AssetIdentity(delivery["chain_id"], delivery["asset_id"], delivery["decimals"])
            refreshed = requote({**continuation, "_journal_position_id": position_id},
                                asset, int(delivery["amount_raw"]))
            if refreshed.get("canonical_continuation"):
                accrued = Decimal(continuation["_realized_loss_before_current_usd"])
                group_check = entry.get("_check_group_loss")
                if not callable(group_check):
                    raise ValueError("canonical_continuation_group_check_missing")
                group_check(_exact_sum((accrued, _entry_loss(refreshed))))
                refreshed.update(_realized_loss_before_current_usd=_decimal_string(accrued),
                                 _journal_position_id=position_id)
                refreshed["canonical_continuation"]["source_progress_key"] = progress_key
                refreshed["canonical_continuation"]["source_step_key"] = proof["source_step_key"]
                progress["continuation"] = refreshed
                journal.save_checkpoint(position_id, progress_key, progress)
                journal.save_checkpoint(position_id, "canonical_prepared", refreshed)
                journal.save_checkpoint(position_id, "canonical_continuation_migration", {
                    "source_hash": proof["source_hash"], "delivery": delivery,
                    "previous_target": continuation["target"], "target": refreshed["target"],
                    "paid_loss_usd": _decimal_string(accrued),
                })
                continuation = refreshed
        continuation.update({key: value for key, value in entry.items() if callable(value)})
        continuation["_observation_restore"] = True
        return _submit_live_step(
            continuation,
            continuation["steps"][0],
            wallet=wallet,
            rpc=rpc,
            rpc_urls=rpc_urls,
            jumper=jumper,
            bitget=bitget,
            journal=journal,
            now_ms=now_ms,
        )
    if step.get("kind") == "unwrap_to_native" and progress.get("unwrap_result"):
        return _continue_local_unwrap(
            entry, progress["unwrap_result"], position_id=position_id,
            progress_key=progress_key, progress=progress, wallet=wallet, rpc=rpc,
            rpc_urls=rpc_urls, jumper=jumper, bitget=bitget, journal=journal,
            now_ms=now_ms,
        )
    initial_native = progress.get("initial_native")
    destination = _asset_from_data(route_data["evidence"]["destination"])
    if step.get("kind") in {"swap_to_native", "unwrap_to_native"} and initial_native is None:
        initial_native = _asset_balance(
            rpc,
            url=_rpc_url(rpc_urls, destination.chain_id),
            asset=destination,
            wallet=wallet.public_address,
        )

    carried_asset_id = entry.get("_actual_asset_id")
    carried_amount = (
        int(entry["_actual_balance_raw"]) if entry.get("_actual_balance_raw") is not None else None
    )
    tx_hash = None
    carried_asset_id = progress.get("carried_asset_id", carried_asset_id)
    carried_amount = progress.get("carried_amount", carried_amount)
    paid_native_gas = progress.get("paid_native_gas", 0)
    confirmed_hashes: list[tuple[int, str]] = progress.get("confirmed_hashes", [])
    sleep = entry.get("_sleep", time.sleep)
    realized_before = _exact_money(
        entry.get("_realized_loss_before_current_usd", "0"),
        "realized loss before dependent route",
    )
    for index, (raw_step, normalized, prepared_request) in enumerate(
        zip(live_route.raw_steps, route_steps, prepared_requests, strict=True)
    ):
        if (
            not isinstance(prepared_request, TransactionRequest)
            or normalized.source is None
            or normalized.destination is None
            or normalized.from_amount is None
        ):
            raise ValueError("requote_required:route_step_evidence_mismatch")
        if index < progress.get("next_index", 0):
            continue
        step_amount = normalized.from_amount
        step_source = normalized.source
        step_key = f"{step.get('kind')}:{route_data['id']}:{index}"
        known_step = journal.latest_step(position_id=position_id, step_key=step_key)
        known_broadcast = known_step is not None and bool(known_step["tx_hash"])
        available = (
            step_amount
            if known_broadcast
            else _asset_balance(
                rpc,
                url=_rpc_url(rpc_urls, step_source.chain_id),
                asset=step_source,
                wallet=wallet.public_address,
            )
        )
        if carried_asset_id == step_source.contract_address and carried_amount is not None:
            available = min(available, carried_amount)
        if available < step_amount:
            raise ValueError("requote_required:insufficient_intermediate_balance")
        if step_source.is_native and prepared_request.value != step_amount:
            raise ValueError("requote_required:native_route_input_mismatch")

        step_key = f"{step.get('kind')}:{route_data['id']}:{index}"
        route_entry = {
            **entry,
            "status": "route_ready",
            "asset_id": step_source.contract_address,
            "chain_id": step_source.chain_id,
            "raw_balance": str(step_amount),
            "route": {**route_data, "step": raw_step},
            "_journal_step_key": step_key,
            "_approval_fee_caps": step.get("_approval_fee_caps", {}),
        }
        if carried_asset_id == step_source.contract_address and carried_amount is not None:
            route_entry["_actual_asset_id"] = carried_asset_id
            route_entry["_actual_balance_raw"] = str(carried_amount)

        output_baseline = (
            progress.get("output_baseline") if progress.get("index") == index else None
        )
        if index < len(route_steps) - 1 and output_baseline is None:
            output_baseline = _asset_balance(
                rpc,
                url=_rpc_url(rpc_urls, normalized.destination.chain_id),
                asset=normalized.destination,
                wallet=wallet.public_address,
            )
        is_cross_chain = step_source.chain_id != normalized.destination.chain_id
        if is_cross_chain and index < len(route_steps) - 1:
            if output_baseline is None or normalized.to_amount is None:
                raise ValueError("requote_required:intermediate_bridge_evidence_missing")
            route_entry["_bridge_balance_baseline_raw"] = str(output_baseline)
            route_entry["_bridge_expected_delta_raw"] = str(normalized.to_amount)
        progress.update(
            initial_native=initial_native,
            index=index,
            output_baseline=output_baseline,
            carried_asset_id=carried_asset_id,
            carried_amount=carried_amount,
            paid_native_gas=paid_native_gas,
            confirmed_hashes=confirmed_hashes,
        )
        journal.save_checkpoint(position_id, progress_key, progress)
        if not known_broadcast and current_deadline() is not None:
            # Saved routes authorize observation only. A new signature gets
            # exact evidence, fee, current price age and deposit checks again.
            candidate, refreshed = _refresh_route_step(
                entry,
                step,
                wallet=wallet,
                jumper=jumper,
                gas_estimator=PlannerGasEstimator(
                    rpc, rpc_urls, jumper,
                    retry_transient_errors=bool(entry.get("automatic_requote")),
                ),
                now=datetime.fromtimestamp(now_ms() / 1000, tz=UTC),
                now_fn=lambda: datetime.fromtimestamp(now_ms() / 1000, UTC),
                bitget=bitget,
                source_amount=int(route_data["evidence"]["input_amount"]),
            )
            if step.get("kind") == "unwrap_to_native":
                local_fresh = _preflight_live_entry(
                    entry, wallet=wallet, rpc=rpc, rpc_urls=rpc_urls,
                    jumper=jumper, bitget=bitget,
                    gas_estimator=PlannerGasEstimator(rpc, rpc_urls, jumper),
                    now_ms=now_ms,
                )
                candidate = local_fresh["_candidate"]
                refreshed = local_fresh["steps"][0]
                entry["_step_candidates"] = local_fresh["_step_candidates"]
                route_entry["reservations"] = local_fresh["reservations"]
                route_entry["quoted_at"] = local_fresh["quoted_at"]
                if entry.get("canonical_continuation"):
                    # Persist the exact refreshed loss/caps before a durable intent can exist.
                    parent_key = entry["canonical_continuation"].get("source_progress_key")
                    parent = journal.checkpoint(position_id, parent_key) if parent_key else None
                    if not parent or not isinstance(parent.get("continuation"), dict):
                        raise ValueError("canonical_continuation_parent_checkpoint_missing")
                    parent["continuation"] = local_fresh
                    journal.save_checkpoint(position_id, parent_key, parent)
                    journal.save_checkpoint(position_id, "canonical_prepared", local_fresh)
            group_check = entry.get("_check_group_loss")
            if callable(group_check):
                group_check(
                    _exact_sum((realized_before, _exact_money(candidate.loss_usd, "route loss")))
                )
            prepared_request = refreshed["_prepared_requests"][index]
            step.update(refreshed)
        tx_hash = _execute_entry(
            entry=route_entry,
            wallet=wallet,
            rpc=rpc,
            jumper=jumper,
            rpc_urls=rpc_urls,
            now_ms=now_ms,
            journal=journal,
            position_id=position_id,
            prepared_request=prepared_request,
        )
        tx_hash_text = str(tx_hash)
        approval_hash = getattr(tx_hash, "approval_tx_hash", None)
        if approval_hash:
            confirmed_hashes.append((step_source.chain_id, str(approval_hash)))
        confirmed_hashes.append((step_source.chain_id, tx_hash_text))
        if step_source.chain_id == destination.chain_id and destination.is_native:
            receipt = rpc.call(
                _rpc_url(rpc_urls, step_source.chain_id),
                "eth_getTransactionReceipt",
                [str(tx_hash)],
            )
            if not isinstance(receipt, dict):
                raise ValueError("route_step_receipt_missing")
            paid_native_gas += quantity(receipt.get("gasUsed")) * quantity(
                receipt.get("effectiveGasPrice")
            ) + (quantity(receipt["l1Fee"]) if receipt.get("l1Fee") is not None else 0)

        if is_cross_chain:
            recipient = normalized.recipient
            if recipient is None:
                raise ValueError("requote_required:cross_chain_recipient_missing")
            step_key = str(route_entry["_journal_step_key"])
            received_raw = _await_cross_chain_transfer(
                jumper=jumper,
                rpc=rpc,
                rpc_urls=rpc_urls,
                journal=journal,
                position_id=position_id,
                step_key=step_key,
                tx_hash=tx_hash_text,
                bridge=normalized.provider or str(raw_step.get("tool", "")),
                source=step_source,
                destination=normalized.destination,
                sender=wallet.public_address,
                recipient=recipient,
                baseline_raw=output_baseline,
                sleep=sleep,
            )
            if index < len(route_steps) - 1:
                if recipient.lower() != wallet.public_address.lower():
                    raise ValueError("requote_required:intermediate_recipient_is_not_wallet")
                journal.record_position_state(
                    position_id,
                    journal.position(position_id)["state"],
                    actual_asset_id=normalized.destination.contract_address,
                    actual_balance_raw=received_raw,
                )
                requote = entry.get("_requote_after_intermediate")
                if not callable(requote):
                    raise ValueError("requote_required:dependent_bridge_requote_unavailable")
                candidate_map = entry.get("_step_candidates", {})
                candidate = candidate_map.get(route_data["id"])
                if candidate is None:
                    candidate = entry.get("_candidate")
                source_value = getattr(candidate, "source_usd", None)
                if source_value is None:
                    raise ValueError("requote_required:route_source_valuation_missing")
                output_price = _quote_price(jumper, normalized.destination)
                assert isinstance(output_price.usd_per_token, Decimal)
                output_value = (
                    raw_to_decimal(received_raw, normalized.destination)
                    * output_price.usd_per_token
                )
                actual_gas_usd = _actual_gas_paid_usd(
                    confirmed_hashes,
                    rpc=rpc,
                    rpc_urls=rpc_urls,
                    jumper=jumper,
                )
                segment_loss = _exact_sum(
                    (
                        max(
                            Decimal(0),
                            _exact_money(source_value, "route source value") - output_value,
                        ),
                        actual_gas_usd,
                    )
                )
                accrued_loss = _exact_sum((realized_before, segment_loss))
                dynamic_entry = requote(entry, normalized.destination, received_raw)
                if not isinstance(dynamic_entry, dict):
                    raise ValueError("requote_required:dependent_route_requote_invalid")
                next_steps = dynamic_entry.get("steps")
                if not isinstance(next_steps, list) or not next_steps:
                    raise ValueError("requote_required:dependent_route_steps_missing")
                next_step = next_steps[0]
                if not isinstance(next_step, dict):
                    raise ValueError("requote_required:dependent_route_step_invalid")
                next_route = next_step.get("route")
                old_evidence = route_data.get("evidence")
                new_evidence = next_route.get("evidence") if isinstance(next_route, dict) else None
                if not isinstance(old_evidence, dict):
                    raise ValueError("requote_required:dependent_route_evidence_missing")
                if isinstance(new_evidence, dict) and isinstance(next_route, dict):
                    new_route_id = str(next_route["id"])
                    new_payload_hash = str(new_evidence["payload_sha256"])
                elif next_step.get("kind") == "direct_deposit":
                    new_route_id = "direct-deposit"
                    new_payload_hash = sha256(
                        json.dumps(next_step, sort_keys=True, separators=(",", ":")).encode("utf-8")
                    ).hexdigest()
                else:
                    raise ValueError("requote_required:dependent_route_evidence_missing")
                journal.record_requote_after_bridge(
                    position_id,
                    old_route_id=str(route_data["id"]),
                    old_payload_hash=str(old_evidence["payload_sha256"]),
                    new_route_id=new_route_id,
                    new_payload_hash=new_payload_hash,
                    input_amount_raw=received_raw,
                    actual_asset_id=normalized.destination.contract_address,
                )
                next_loss = _entry_loss(dynamic_entry)
                group_loss_check = entry.get("_check_group_loss")
                if callable(group_loss_check):
                    group_loss_check(_exact_sum((accrued_loss, next_loss)))
                dynamic_entry = {
                    **dynamic_entry,
                    "_realized_loss_before_current_usd": _decimal_string(accrued_loss),
                    "_requote_after_intermediate": requote,
                    "_check_group_loss": group_loss_check,
                    "_sleep": sleep,
                }
                if dynamic_entry.get("canonical_continuation"):
                    dynamic_entry["canonical_continuation"]["source_progress_key"] = progress_key
                    dynamic_entry["canonical_continuation"]["source_step_key"] = step_key
                    journal.save_checkpoint(position_id, "canonical_prepared", dynamic_entry)
                progress["continuation"] = dynamic_entry
                journal.save_checkpoint(position_id, progress_key, progress)
                return _submit_live_step(
                    dynamic_entry,
                    next_step,
                    wallet=wallet,
                    rpc=rpc,
                    rpc_urls=rpc_urls,
                    jumper=jumper,
                    bitget=bitget,
                    journal=journal,
                    now_ms=now_ms,
                )
            if recipient.lower() != wallet.public_address.lower():
                target = entry.get("target")
                if isinstance(target, dict) and received_raw < int(target["minimum_raw"]):
                    raise ValueError("bitget_deposit_below_minimum")

        if output_baseline is not None:
            output_balance = _asset_balance(
                rpc,
                url=_rpc_url(rpc_urls, normalized.destination.chain_id),
                asset=normalized.destination,
                wallet=wallet.public_address,
            )
            carried_amount = output_balance - output_baseline
            if carried_amount <= 0:
                raise ValueError("requote_required:intermediate_output_not_observed")
            carried_asset_id = normalized.destination.contract_address
            journal.record_position_state(
                position_id,
                journal.position(position_id)["state"],
                actual_asset_id=carried_asset_id,
                actual_balance_raw=carried_amount,
            )

        progress.update(
            next_index=index + 1,
            carried_asset_id=carried_asset_id,
            carried_amount=carried_amount,
            paid_native_gas=paid_native_gas,
            confirmed_hashes=confirmed_hashes,
        )
        journal.save_checkpoint(position_id, progress_key, progress)

    candidate_map = entry.get("_step_candidates", {})
    candidate = candidate_map.get(route_data["id"])
    if candidate is None and entry.get("_candidate") is not None:
        candidate = entry["_candidate"]
    if candidate is None:
        raise ValueError("route preflight valuation is missing")
    result = {
        "state": "confirmed",
        "realized_loss_usd": _decimal_string(
            _exact_sum((realized_before, _exact_money(candidate.loss_usd, "route loss")))
        ),
    }
    if step.get("kind") in {"swap_to_native", "unwrap_to_native"}:
        final_native = _asset_balance(
            rpc,
            url=_rpc_url(rpc_urls, destination.chain_id),
            asset=destination,
            wallet=wallet.public_address,
        )
        net_output = final_native - int(initial_native)
        generated_output = net_output + paid_native_gas
        if step.get("kind") == "unwrap_to_native":
            net_output = min(net_output, int(evidence.input_amount) - paid_native_gas)
            generated_output = net_output + paid_native_gas
        if generated_output <= 0 or net_output <= 0:
            raise ValueError("source_swap_generated_no_native_asset")
        result["actual_asset_id"] = destination.contract_address
        result["actual_balance_raw"] = str(net_output)
        result["generated_output_raw"] = str(generated_output)
    if step.get("kind") == "unwrap_to_native":
        return _continue_local_unwrap(
            entry, result, position_id=position_id, progress_key=progress_key,
            progress=progress, wallet=wallet, rpc=rpc, rpc_urls=rpc_urls,
            jumper=jumper, bitget=bitget, journal=journal, now_ms=now_ms,
        )
    if step.get("kind") == "bridge" and isinstance(entry.get("target"), dict):
        return _settle_route_deposit(
            entry, step, route_steps[-1], result=result, wallet=wallet, jumper=jumper,
            rpc=rpc, rpc_urls=rpc_urls, bitget=bitget, journal=journal,
            position_id=position_id, now_ms=now_ms, sleep=sleep,
        )
    return result


def _utc_now_ms() -> int:
    return time.time_ns() // 1_000_000


def _execute_entry(
    *,
    entry: dict,
    wallet: WalletWorkbookRow,
    rpc: ExecutionRpc,
    jumper: LifiClient,
    rpc_urls: dict[int, str],
    now_ms: Callable[[], int] = _utc_now_ms,
    journal: Journal | None = None,
    position_id: int | None = None,
    prepared_request: TransactionRequest | None = None,
) -> str:
    chain_id = int(entry["chain_id"])
    url = rpc_urls[chain_id]
    if prepared_request is not None:
        request = prepared_request
    elif entry["status"] == "route_ready":
        step = entry["route"]["step"]
        request = jumper.step_transaction(step)
    else:
        request = _direct_request(entry, wallet=wallet, rpc=rpc, url=url)
    if request.chain_id != chain_id:
        raise ValueError("route transaction chain does not match source balance")
    step_key = str(entry.get("_journal_step_key", entry["status"]))
    if journal is not None and position_id is not None:
        saved = journal.latest_step(position_id=position_id, step_key=step_key)
        signing = journal.checkpoint(position_id, f"canonical_signing:{step_key}")
        if saved and saved["tx_hash"] and signing and entry.get("canonical_continuation"):
            signed_entry = {**signing["entry"],
                **{key: value for key, value in entry.items() if callable(value)}}
            withdrawal_reserve(signed_entry, signing["request"], wallet)
            _replay_missing_direct(entry={**signed_entry,
                "_direct_transaction": signing["request"]}, saved=saved, wallet=wallet,
                rpc=rpc, rpc_urls=rpc_urls, journal=journal)
            wait_for_receipt(rpc, url=url, tx_hash=saved["tx_hash"])
            return ExecutedTransactionHash(saved["tx_hash"], None)
        recovered = recover_durable_broadcast(
            rpc, url=url, journal=journal, position_id=position_id, step_key=step_key
        )
        if recovered is not None:
            wait_for_receipt(rpc, url=url, tx_hash=recovered)
            return ExecutedTransactionHash(recovered, None)
    pending_approval = request.fee_quote_fallback_reason == "pending_approval"
    if pending_approval and (entry["asset_id"] == "native" or entry["status"] != "route_ready"
                             or request.max_total_fee_cap_wei is None):
        raise ValueError("pending approval estimate requires a capped ERC20 route")
    if not pending_approval and not (
        prepared_request is not None
        and entry["status"] == "direct_deposit"
        and prepared_request.gas_limit > 0
    ):
        request = FeePlanner(rpc).plan(url, request, sender=wallet.public_address)
    require_ethereum_planned_gas_price_valid(request)
    balance = _native_balance(rpc, url=url, wallet=wallet.public_address)
    generated_native_cap = (
        entry.get("_actual_balance_raw") if entry.get("_actual_asset_id") == "native" else None
    )
    if generated_native_cap is not None:
        cap = _raw_amount(generated_native_cap)
        if request.value > cap or balance < request.value:
            raise ValueError("requote_required:insufficient_generated_native_balance")
        reserve_balance = min(cap, balance) - request.value
    else:
        shared_amount = (int(request.data[-64:], 16) if request.chain_id == CELO_CHAIN_ID
                         and request.to.lower() == CELO_TOKEN_ADDRESS
                         and request.data.startswith("0xa9059cbb") else 0)
        reserve_balance = balance - request.value - shared_amount
    canonical_reserve = withdrawal_reserve(entry, request, wallet)
    require_native_reserve(balance=reserve_balance,
        gas_cost=(canonical_reserve if canonical_reserve is not None
                  else request.max_total_fee_wei),
        multiplier=(1 if canonical_reserve is not None else GAS_RESERVE_MULTIPLIER))
    asset_id = str(entry["asset_id"])
    approval_tx_hash = None
    before_sign = entry.get("_before_sign")
    if callable(before_sign):
        before_sign()
    if entry["status"] == "route_ready" and asset_id != "native":
        approval_tx_hash = _approve_if_needed(
            entry,
            request,
            wallet=wallet,
            rpc=rpc,
            url=url,
            journal=(journal if "_journal_step_key" in entry else None),
            position_id=(position_id if "_journal_step_key" in entry else None),
            route_step_key=step_key,
        )
        if pending_approval:
            request = FeePlanner(rpc).plan(url, request, sender=wallet.public_address)
            require_ethereum_planned_gas_price_valid(request)
            require_native_reserve(
                balance=_native_balance(rpc, url=url, wallet=wallet.public_address) - request.value,
                gas_cost=request.max_total_fee_wei,
            )
    nonce = pending_nonce(rpc, url=url, wallet=wallet.public_address)
    if entry.get("_replacement_nonce") is not None:
        latest = quantity(rpc.call(
            url, "eth_getTransactionCount", [wallet.public_address, "latest"]))
        replacement_nonce = int(entry["_replacement_nonce"])
        if latest > replacement_nonce or nonce > replacement_nonce + 1:
            raise StageDeadlineExceeded("replacement_nonce_consumed_waiting_receipt")
        nonce = replacement_nonce
    try:
        if callable(before_sign):
            before_sign()
        require_ethereum_gas_below_limit(rpc, url=url, request=request)
        if entry["status"] == "route_ready":
            try:
                _validate_route_quote_freshness([entry], now_ms=now_ms)
            except ValueError as exc:
                raise RouteQuoteExpired(str(exc), approval_tx_hash) from exc
        check_deadline()
        raw = sign_transaction(
            request,
            private_key=wallet.private_key,
            expected_sender=wallet.public_address,
            nonce=nonce,
        )
    except EthereumGasDeferred as exc:
        exc.approval_tx_hash = approval_tx_hash
        raise
    if journal is not None and position_id is not None:
        if entry["status"] == "direct_deposit":
            journal.save_checkpoint(position_id, "direct_prepared",
                                    {**entry, "_direct_transaction": request})
        elif entry.get("canonical_continuation"):
            journal.save_checkpoint(position_id, f"canonical_signing:{step_key}",
                                    {"entry": entry, "request": request})
        tx_hash = broadcast_durable_transaction(
            rpc,
            url=url,
            journal=journal,
            position_id=position_id,
            step_key=step_key,
            nonce=nonce,
            calldata=request.data,
            signed_transaction=raw,
            tx_hash=signed_transaction_hash(raw),
            balance_baseline_raw=entry.get("_bridge_balance_baseline_raw"),
            expected_delta_raw=entry.get("_bridge_expected_delta_raw"),
        )
        if tx_hash is None:
            raise AmbiguousBroadcast(signed_transaction_hash(raw))
    else:
        tx_hash = broadcast_signed_transaction(rpc, url=url, raw_transaction=raw)
    wait_for_receipt(rpc, url=url, tx_hash=tx_hash)
    return ExecutedTransactionHash(tx_hash, approval_tx_hash)


def _position_key(entry: dict) -> str:
    group = entry.get("group")
    group_key = (
        str(group["key"])
        if isinstance(group, dict) and group.get("key")
        else f"{str(entry['wallet']).lower()}:{entry['chain_id']}"
    )
    return ":".join((group_key, str(entry["asset_id"]).lower()))


def _exact_money(value: object, label: str) -> Decimal:
    if isinstance(value, (bool, float)) or value is None:
        raise ValueError(f"{label} is missing or inexact")
    try:
        result = Decimal(value)
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise ValueError(f"{label} is invalid") from exc
    if not result.is_finite() or result < 0:
        raise ValueError(f"{label} is invalid")
    return result


def _entry_loss(entry: dict) -> Decimal:
    valuation = entry.get("valuation")
    if not isinstance(valuation, dict):
        raise ValueError("fresh route valuation is missing")
    return _exact_money(valuation.get("loss_usd"), "route loss")


def _add_realized(total: Decimal, loss: object) -> Decimal:
    value = _exact_money(loss, "realised route loss")
    return _exact_sum((total, value))


def _exact_sum(values) -> Decimal:
    terms = tuple(values)
    nonzero = tuple(term for term in terms if term)
    if not nonzero:
        return Decimal(0)
    max_adjusted = max(term.adjusted() for term in nonzero)
    min_exponent = min(term.as_tuple().exponent for term in nonzero)
    precision = max_adjusted - min_exponent + len(str(len(terms))) + 2
    with localcontext() as context:
        context.prec = max(context.prec, precision)
        return sum(terms, start=Decimal(0))


def _exact_product(left: Decimal, right: Decimal) -> Decimal:
    precision = len(left.as_tuple().digits) + len(right.as_tuple().digits) + 2
    with localcontext() as context:
        context.prec = max(context.prec, precision)
        return left * right


def _exact_percentage(numerator: Decimal, denominator: Decimal) -> Decimal:
    precision = len(numerator.as_tuple().digits) + len(denominator.as_tuple().digits) + 50
    with localcontext() as context:
        context.prec = max(context.prec, precision)
        return numerator / denominator * Decimal(100)


def _decimal_string(value: Decimal) -> str:
    return format(value.normalize(), "f") if value else "0"


def _raw_amount(value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, (str, int)):
        raise ValueError("actual balance amount is invalid")
    if isinstance(value, str) and not value.isdecimal():
        raise ValueError("actual balance amount is invalid")
    amount = int(value)
    if amount <= 0:
        raise ValueError("actual balance amount must be positive")
    return amount


def _execution_reason(exc: Exception) -> str:
    message = str(exc).strip()
    if message and len(message) <= 200:
        return message
    return f"execution_error:{type(exc).__name__}"


def _retryable_provider_error(error):
    if isinstance(error, (LifiRequestLimit, JobInProgress)):
        return True
    if isinstance(error, ValueError) and str(error) in {
        "requote_required:stale_price", "native_gas_reserve_unstable"
    }:
        return True
    return is_transient_error(error)


def _recover_reverted_celo_direct(
    *, entry, saved, receipt, position_id, wallet, rpc, rpc_urls, jumper, bitget, journal, now_ms,
):
    """Recover one finalized legacy CELO sweep that spent its shared gas balance."""
    original = entry.get("_direct_transaction")
    if (not entry.get("automatic_requote")
            or (entry.get("chain_id"), entry.get("asset_id"), entry.get("decimals"))
            != (CELO_CHAIN_ID, CELO_TOKEN_ADDRESS, 18)
            or saved["step_key"] != "direct_deposit"
            or journal.checkpoint(position_id, "direct_recovery") is not None
            or not isinstance(original, TransactionRequest)
            or original.to.lower() != CELO_TOKEN_ADDRESS or original.value != 0
            or len(original.data) != 138 or not original.data.startswith("0xa9059cbb")
            or int(original.data[10:74], 16) != int(wallet.bitget_deposit_address, 16)
            or sha256(original.data.encode()).hexdigest() != saved["calldata_digest"]
            or quantity(receipt.get("status")) != 0
            or str(receipt.get("transactionHash", "")).lower() != saved["tx_hash"].lower()
            or str(receipt.get("from", "")).lower() != wallet.public_address.lower()
            or str(receipt.get("to", "")).lower() != CELO_TOKEN_ADDRESS):
        raise DepositSettlementFailed("source_transaction_reverted")
    url = _rpc_url(rpc_urls, CELO_CHAIN_ID)
    if quantity(rpc.call(url, "eth_chainId", [])) != CELO_CHAIN_ID:
        raise DepositSettlementFailed("reverted_deposit_chain_mismatch")
    block = quantity(receipt["blockNumber"])
    finalized = rpc.call(url, "eth_getBlockByNumber", ["finalized", False])
    if not isinstance(finalized, dict) or quantity(finalized.get("number")) < block:
        raise StageDeadlineExceeded("reverted_deposit_waiting_finality")
    before = quantity(rpc.call(url, "eth_getBalance", [wallet.public_address, hex(block - 1)]))
    if int(original.data[-64:], 16) != before or before != int(entry["raw_balance"]):
        raise DepositSettlementFailed("reverted_deposit_cause_unverified")
    position = journal.position(position_id)
    group = journal.group(position["group_id"])
    count = journal.connection.execute(
        "SELECT count(*) FROM route_positions WHERE group_id=?", (group["id"],)
    ).fetchone()[0]
    if count != 1:
        raise DepositSettlementFailed("reverted_deposit_group_not_isolated")
    fresh = _preflight_live_entry(
        entry, wallet=wallet, rpc=rpc, rpc_urls=rpc_urls, jumper=jumper, bitget=bitget,
        gas_estimator=PlannerGasEstimator(rpc, rpc_urls, jumper), now_ms=now_ms,
    )
    paid_raw = (quantity(receipt["gasUsed"]) * quantity(receipt["effectiveGasPrice"])
                + quantity(receipt.get("l1Fee", "0x0")))
    price = _token_quote_price(jumper, native_asset_identity(CELO_CHAIN_ID))
    if not timedelta(0) <= datetime.fromtimestamp(now_ms() / 1000, UTC) - price.observed_at <= (
        _MAX_PRICE_AGE
    ):
        raise ValueError("stale_price_for_reverted_deposit_fee")
    paid_usd = raw_to_decimal(paid_raw, price.asset) * price.usd_per_token
    loss = _exact_sum((_entry_loss(fresh), paid_usd))
    budget = Decimal(group["source_usd"]) * Decimal(group["loss_budget_pct"]) / 100
    if loss > budget:
        raise GroupLossLimitExceeded("reverted_deposit_recovery_exceeds_original_budget")
    fresh["valuation"] = {**fresh["valuation"], "loss_usd": _decimal_string(loss)}
    fresh["_journal_step_key"] = "direct_deposit:recovery1"
    journal.record_receipt(saved["id"], status="reverted", finality_block=block)
    journal.save_checkpoint(position_id, "direct_recovery", {
        "step_key": fresh["_journal_step_key"], "failed_tx_hash": saved["tx_hash"],
        "failed_fee_raw": str(paid_raw), "failed_fee_usd": _decimal_string(paid_usd),
        "finalized_height": quantity(finalized["number"]),
    })
    journal.save_checkpoint(position_id, "direct_prepared", fresh)
    return fresh


def _direct_step_key(journal, position_id):
    recovery = journal.checkpoint(position_id, "direct_recovery")
    return recovery.get("winner_step_key", recovery["step_key"]) if recovery else "direct_deposit"


def _recover_missing_scroll_direct(
    *, entry, saved, position_id, wallet, rpc, rpc_urls, jumper, bitget, journal, now_ms,
):
    """Replace only a missing legacy Scroll sweep that omitted its L1 fee.

    Both hashes remain durable and share one nonce, so at most one can execute.
    No other missing transaction is eligible for this migration.
    """
    original = entry.get("_direct_transaction")
    created = datetime.fromisoformat(saved["created_at"]).replace(tzinfo=UTC)
    now = datetime.fromtimestamp(now_ms() / 1000, UTC)
    if (not entry.get("automatic_requote")
            or (entry.get("chain_id"), entry.get("asset_id"), entry.get("decimals"))
            != (534352, "native", 18)
            or saved["step_key"] != "direct_deposit"
            or journal.checkpoint(position_id, "direct_recovery") is not None
            or now - created < timedelta(minutes=2)
            or not isinstance(original, TransactionRequest) or original.chain_id != 534352
            or original.to.lower() != wallet.bitget_deposit_address.lower()
            or original.data != "0x" or original.value <= 0 or original.additional_fee_wei != 0
            or sha256(original.data.encode()).hexdigest() != saved["calldata_digest"]):
        return None
    url = _rpc_url(rpc_urls, 534352)
    if quantity(rpc.call(url, "eth_chainId", [])) != 534352:
        raise DepositSettlementFailed("missing_deposit_chain_mismatch")
    if rpc.call(url, "eth_getTransactionByHash", [saved["tx_hash"]]) is not None:
        return None
    for tag in ("latest", "pending"):
        if quantity(rpc.call(url, "eth_getTransactionCount", [wallet.public_address, tag])) != (
            saved["nonce"]
        ):
            return None
    group = journal.group(journal.position(position_id)["group_id"])
    if len(journal.group_positions(group["id"])) != 1:
        return None
    fresh = _preflight_live_entry(
        {**entry, "_replacement_nonce": saved["nonce"],
         "_replacement_original_request": original, "_actual_balance_raw": str(original.value)},
        wallet=wallet, rpc=rpc, rpc_urls=rpc_urls, jumper=jumper, bitget=bitget,
        gas_estimator=PlannerGasEstimator(rpc, rpc_urls, jumper), now_ms=now_ms,
    )
    request = fresh["_direct_transaction"]
    if request.additional_fee_wei <= 0 or request.value > original.value:
        raise DepositSettlementFailed("missing_deposit_replacement_shape_invalid")
    budget = Decimal(group["source_usd"]) * Decimal(group["loss_budget_pct"]) / 100
    if _entry_loss(fresh) > budget:
        raise GroupLossLimitExceeded("missing_deposit_replacement_exceeds_original_budget")
    fresh["_journal_step_key"] = "direct_deposit:replacement1"
    journal.save_checkpoint(position_id, "direct_recovery", {
        "kind": "scroll_l1_fee", "step_key": fresh["_journal_step_key"],
        "replacement_nonce": saved["nonce"], "failed_tx_hash": saved["tx_hash"],
        "failed_fee_usd": "0", "original_entry": entry,
    })
    journal.save_checkpoint(position_id, "direct_prepared", fresh)
    return fresh


def _replay_missing_direct(*, entry, saved, wallet, rpc, rpc_urls, journal):
    """Rebroadcast identical bytes only; never change a durable signed intent."""
    url = _rpc_url(rpc_urls, int(entry["chain_id"]))
    if rpc.call(url, "eth_getTransactionReceipt", [saved["tx_hash"]]) is not None:
        return
    if rpc.call(url, "eth_getTransactionByHash", [saved["tx_hash"]]) is not None:
        return
    if quantity(rpc.call(url, "eth_getTransactionCount", [wallet.public_address, "latest"])) > (
        saved["nonce"]
    ):
        return
    request = entry["_direct_transaction"]
    raw = sign_transaction(request, private_key=wallet.private_key,
                           expected_sender=wallet.public_address, nonce=saved["nonce"])
    if (signed_transaction_hash(raw) != saved["tx_hash"]
            or sha256(raw.encode()).hexdigest() != saved["signed_payload_digest"]
            or sha256(request.data.encode()).hexdigest() != saved["calldata_digest"]):
        raise DepositSettlementFailed("durable_replacement_payload_mismatch")
    journal.record_broadcast_attempt(saved["id"], saved["tx_hash"])
    returned = broadcast_signed_transaction(rpc, url=url, raw_transaction=raw)
    if returned.lower() != saved["tx_hash"].lower():
        raise DepositSettlementFailed("durable_replacement_hash_mismatch")


def _direct_request(
    entry: dict, *, wallet: WalletWorkbookRow, rpc: ExecutionRpc, url: str
) -> TransactionRequest:
    target = entry["target"]
    chain_id = int(entry["chain_id"])
    asset_id = str(entry["asset_id"])
    if asset_id == "native":
        balance = _native_balance(rpc, url=url, wallet=wallet.public_address)
        if entry.get("_actual_balance_raw") is not None:
            balance = min(balance, _raw_amount(entry["_actual_balance_raw"]))
        quote = FeePlanner(rpc).plan(
            url,
            TransactionRequest(chain_id, wallet.bitget_deposit_address, "0x", 0, 0, 0),
            sender=wallet.public_address,
            **({"replacement_for": entry["_replacement_original_request"]}
               if entry.get("_replacement_original_request") is not None else {}),
        )
        amount = balance - GAS_RESERVE_MULTIPLIER * quote.max_total_fee_wei
        if amount < int(target["minimum_raw"]):
            raise ValueError("native balance is below Bitget minimum after gas reserve")
        return replace(quote, value=amount)
    amount = int(entry["raw_balance"])
    if entry["status"] == "post_bridge_deposit":
        amount = _wait_for_staged_balance(
            rpc,
            url=url,
            token=asset_id,
            wallet=wallet.public_address,
            expected_minimum=amount,
        )
        if amount < int(target["minimum_raw"]):
            raise ValueError("staged USDC is below Bitget minimum")
    recipient = wallet.bitget_deposit_address[2:].lower().rjust(64, "0")
    data = "0xa9059cbb" + recipient + hex(amount)[2:].rjust(64, "0")
    if chain_id == CELO_CHAIN_ID and asset_id == CELO_TOKEN_ADDRESS:
        balance = min(amount, _native_balance(rpc, url=url, wallet=wallet.public_address))
        spend = balance // 2
        for attempt in range(4):
            data = "0xa9059cbb" + recipient + hex(spend)[2:].rjust(64, "0")
            planned = FeePlanner(rpc).plan(
                url, TransactionRequest(chain_id, asset_id, data, 0, 0, 0),
                sender=wallet.public_address,
            )
            reserve = GAS_RESERVE_MULTIPLIER * planned.max_total_fee_wei
            if (balance - spend == reserve or attempt == 3 and balance - spend >= reserve):
                if spend < int(target["minimum_raw"]):
                    raise ValueError("CELO balance is below Bitget minimum after gas reserve")
                return planned
            spend = balance - reserve
            if spend < int(target["minimum_raw"]):
                raise ValueError("CELO balance is below Bitget minimum after gas reserve")
        raise ValueError("CELO gas reserve is unstable")
    return TransactionRequest(chain_id, asset_id, data, 0, 0, 0)


def _is_celo_erc20(asset: AssetIdentity) -> bool:
    return asset == AssetIdentity(CELO_CHAIN_ID, CELO_TOKEN_ADDRESS, 18)


def _approve_if_needed(
    entry: dict,
    request: TransactionRequest,
    *,
    wallet: WalletWorkbookRow,
    rpc: ExecutionRpc,
    url: str,
    journal: Journal | None = None,
    position_id: int | None = None,
    route_step_key: str = "route_ready",
) -> str | None:
    spender = entry["route"]["step"].get("estimate", {}).get("approvalAddress")
    if entry["route"]["step"].get("tool") == WETH_WITHDRAW_TOOL:
        chain, contract, calldata = withdraw_transaction_fields(entry["route"]["step"])
        if (request.chain_id, request.to.lower(), request.data.lower(), request.value) != (
            chain, contract, calldata, 0
        ):
            raise ValueError("canonical WETH withdrawal transaction mismatch")
        return None
    asset_id = str(entry["asset_id"])
    amount = int(entry["raw_balance"])
    if not isinstance(spender, str):
        raise ValueError("Jumper route is missing an approval address")
    allowed = (
        token_allowance(
            rpc,
            url=url,
            token=asset_id,
            owner=wallet.public_address,
            spender=spender,
        )
        >= amount
    )
    if allowed:
        return None
    approval_step_key = f"approval:{route_step_key}"
    if journal is not None and position_id is not None:
        recovered = recover_durable_broadcast(
            rpc,
            url=url,
            journal=journal,
            position_id=position_id,
            step_key=approval_step_key,
        )
        if recovered is not None:
            wait_for_receipt(rpc, url=url, tx_hash=recovered)
            if (
                token_allowance(
                    rpc,
                    url=url,
                    token=asset_id,
                    owner=wallet.public_address,
                    spender=spender,
                )
                < amount
            ):
                raise ValueError("approval confirmed without sufficient token allowance")
            return recovered
    approval = approve_transaction(
        chain_id=request.chain_id,
        token=asset_id,
        spender=spender,
        amount=amount,
        gas_price_wei=0,
    )
    approval_cap = entry.get("_approval_fee_caps", {}).get(
        f"{request.chain_id}:{asset_id}:{spender.lower()}"
    )
    approval = FeePlanner(rpc).plan(url, approval, sender=wallet.public_address,
                                    max_total_fee_wei=approval_cap)
    require_ethereum_planned_gas_price_valid(approval)
    balance = _native_balance(rpc, url=url, wallet=wallet.public_address)
    require_native_reserve(balance=balance, gas_cost=approval.max_total_fee_wei)
    nonce = pending_nonce(rpc, url=url, wallet=wallet.public_address)
    require_ethereum_gas_below_limit(rpc, url=url, request=approval)
    check_deadline()
    raw = sign_transaction(
        approval,
        private_key=wallet.private_key,
        expected_sender=wallet.public_address,
        nonce=nonce,
    )
    if journal is not None and position_id is not None:
        tx_hash = broadcast_durable_transaction(
            rpc,
            url=url,
            journal=journal,
            position_id=position_id,
            step_key=approval_step_key,
            nonce=nonce,
            calldata=approval.data,
            signed_transaction=raw,
            tx_hash=signed_transaction_hash(raw),
        )
        if tx_hash is None:
            raise AmbiguousBroadcast(signed_transaction_hash(raw))
    else:
        tx_hash = broadcast_signed_transaction(rpc, url=url, raw_transaction=raw)
    wait_for_receipt(rpc, url=url, tx_hash=tx_hash)
    return tx_hash


def _native_balance(rpc: ExecutionRpc, *, url: str, wallet: str) -> int:
    return _quantity(rpc.call(url, "eth_getBalance", [wallet, "latest"]))


def _token_balance(rpc: ExecutionRpc, *, url: str, token: str, wallet: str) -> int:
    data = "0x70a08231" + wallet[2:].lower().rjust(64, "0")
    result = rpc.call(url, "eth_call", [{"to": token, "data": data}, "latest"])
    from .rpc import uint256

    return uint256(result)


def _wait_for_staged_balance(
    rpc: ExecutionRpc,
    *,
    url: str,
    token: str,
    wallet: str,
    expected_minimum: int,
    timeout_seconds: int = 21_600,
    poll_seconds: int = 60,
) -> int:
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        amount = _token_balance(rpc, url=url, token=token, wallet=wallet)
        if amount >= expected_minimum:
            return amount
        if current_deadline() is not None:
            raise StageDeadlineExceeded("staged_balance_pending")
        budget_sleep(poll_seconds)
    raise TimeoutError("staged USDC did not arrive before timeout")


def _quantity(value: object) -> int:
    from .rpc import quantity

    return quantity(value)


def _validate_final_deposit_targets(entries: list[dict]) -> None:
    for entry in entries:
        if entry.get("status") not in {"direct_deposit", "post_bridge_deposit"}:
            continue
        target = entry.get("target")
        if (
            not isinstance(target, dict)
            or not isinstance(target.get("chain"), str)
            or not target["chain"]
        ):
            raise ValueError("final deposit is missing Bitget exchange chain")


def _revalidate_final_deposit_target(
    bitget: BitgetClient, entry: dict, wallet: WalletWorkbookRow
) -> None:
    """Recheck exchange metadata/minimum before signing; trust the workbook address."""

    # Test doubles intentionally need only the deposit-status interface.  The
    # concrete client always provides this live re-admission method.
    revalidate = getattr(bitget, "revalidate_deposit_target", None)
    if revalidate is None:
        return
    live = revalidate(entry["target"], recipient=wallet.bitget_deposit_address)
    if int(entry["raw_balance"]) < live.minimum_raw:
        raise ValueError("amount is below live Bitget deposit minimum")


def _bitget_client() -> BitgetClient:
    keys = ("BITGET_API_KEY", "BITGET_SECRET_KEY", "BITGET_PASSPHRASE")
    missing = [key for key in keys if not os.environ.get(key)]
    if missing:
        raise ValueError("Bitget API credentials are required for execution")
    return BitgetClient(
        api_key=os.environ["BITGET_API_KEY"],
        secret_key=os.environ["BITGET_SECRET_KEY"],
        passphrase=os.environ["BITGET_PASSPHRASE"],
    )
