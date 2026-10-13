"""Exact unsigned canonical source recovery and read-only future gas proofs."""

from __future__ import annotations

import json
from copy import deepcopy
from decimal import Decimal, InvalidOperation, localcontext
from hashlib import sha256

from .route_checkpoints import encode
from .rpc import quantity, uint256
from .weth import WETH_CONTRACTS, withdraw_transaction_fields

_GAS_REASON = "insufficient native balance for gas reserve:"


def _identity(entry):
    return tuple(
        entry.get(k)
        for k in (
            "wallet",
            "deposit_address",
            "chain_id",
            "asset_id",
            "decimals",
            "raw_balance",
            "target",
        )
    )


def eligible_source(journal, entry, wallet):
    """Require the exact full single-member halt, including every unsigned intent row."""
    from .route_execution import _position_key

    try:
        chain, amount = entry["chain_id"], int(entry["raw_balance"])
        group_data, target = entry["group"], entry["target"]
        if (
            not entry.get("automatic_requote")
            or entry.get("canonical_source_bootstrap")
            or entry["asset_id"] != WETH_CONTRACTS.get(chain)
            or entry["decimals"] != 18
            or amount <= 0
            or entry["wallet"].lower() != wallet.public_address.lower()
            or entry["deposit_address"].lower() != wallet.bitget_deposit_address.lower()
            or target["coin"] != "USDC"
            or target["asset_id"] == "native"
            or target["decimals"] != 6
            or int(target["minimum_raw"]) <= 0
            or not target["chain"]
            or group_data["wallet"].lower() != entry["wallet"].lower()
            or group_data["source_chain_id"] != chain
        ):
            return None
        position = journal.connection.execute(
            "SELECT * FROM route_positions WHERE position_key=?",
            (_position_key(entry),),
        ).fetchone()
        if position is None or position["group_id"] is None:
            return None
        group = journal.group(position["group_id"])
        reason = str(group["reason"] or "")
        rows = journal.connection.execute(
            "SELECT id FROM route_positions WHERE group_id=?",
            (group["id"],),
        ).fetchall()
        if (
            group["state"] != "manual_review_group_halted"
            or not reason.startswith(_GAS_REASON)
            or len(rows) != 1
            or position["state"] != "manual_review"
            or position["reason"] != reason
            or position["wallet"] != entry["wallet"].lower()
            or position["source_asset_id"] != entry["asset_id"]
            or int(position["source_amount_raw"]) != amount
            or position["actual_asset_id"] is not None
            or group["wallet"] != entry["wallet"].lower()
            or group["source_chain_id"] != chain
            or group["group_key"] != group_data["key"]
            or group["source_usd"] != group_data["source_usd"]
            or group["loss_budget_pct"] != group_data["max_route_loss_pct"]
            or Decimal(group["loss_budget_pct"]) != Decimal(15)
            or journal.connection.execute(
                "SELECT 1 FROM route_steps WHERE position_id=?", (position["id"],)
            ).fetchone()
            is not None
        ):
            return None
        prepared = journal.checkpoint(position["id"], "prepared")
        if prepared is not None and _identity(prepared) != _identity(entry):
            return None
        with localcontext() as context:
            context.prec = 80
            budget = Decimal(group["source_usd"]) * Decimal(group["loss_budget_pct"]) / 100
            paid = Decimal(group["realized_loss_usd"])
            accounted = Decimal(position["accounted_loss_usd"] or "0")
            if (
                not paid.is_finite()
                or not accounted.is_finite()
                or min(paid, accounted) < 0
                or accounted > paid
                or paid >= budget
            ):
                return None
        return dict(position), group
    except (KeyError, TypeError, ValueError, AttributeError, InvalidOperation):
        return None


def migrate_source_bootstraps(entries, *, journal, wallet, balance):
    """Recheck under the write lock; keep the original position and budget identities."""
    changed = 0
    for entry in entries:
        # The migration checkpoint commits before the containing collection plan is saved.
        # Restore only that exact unsigned state after a crash in this narrow window.
        if not entry.get("canonical_source_bootstrap"):
            from .route_execution import _position_key

            position = journal.connection.execute(
                "SELECT * FROM route_positions WHERE position_key=?",
                (_position_key(entry),),
            ).fetchone()
            if position is not None:
                saved = journal.checkpoint(position["id"], "canonical_source_bootstrap")
                if saved and position["state"] == "planned" and position["group_id"] is not None:
                    group = journal.group(position["group_id"])
                    proposed = {**entry, "canonical_source_bootstrap": saved}
                    try:
                        validate_marker(proposed, wallet)
                    except ValueError:
                        continue
                    members = journal.connection.execute(
                        "SELECT id FROM route_positions WHERE group_id=?",
                        (group["id"],),
                    ).fetchall()
                    if (
                        group["state"] == "active"
                        and len(members) == 1
                        and position["source_asset_id"] == saved["asset_id"]
                        and position["source_amount_raw"] == saved["amount_raw"]
                        and journal.connection.execute(
                            "SELECT 1 FROM route_steps WHERE position_id=?",
                            (position["id"],),
                        ).fetchone()
                        is None
                    ):
                        entry["canonical_source_bootstrap"] = saved
                        changed += 1
                        continue
        proof = eligible_source(journal, entry, wallet)
        if (
            proof is None
            or sum(e.get("group", {}).get("key") == entry["group"]["key"] for e in entries) != 1
        ):
            continue
        if balance(entry) != int(entry["raw_balance"]):
            continue
        marker = None
        with journal.connection:
            journal.connection.execute("BEGIN IMMEDIATE")
            proof = eligible_source(journal, entry, wallet)
            if proof is None:
                continue
            position, group = proof
            marker = dict(
                wallet=wallet.public_address,
                recipient=wallet.bitget_deposit_address,
                chain_id=entry["chain_id"],
                asset_id=entry["asset_id"],
                decimals=18,
                amount_raw=entry["raw_balance"],
                target=deepcopy(entry["target"]),
                group_key=group["group_key"],
                source_usd=group["source_usd"],
                loss_budget_pct=group["loss_budget_pct"],
                paid_loss_usd=group["realized_loss_usd"],
                accounted_loss_usd=position["accounted_loss_usd"] or "0",
                original_reason=group["reason"],
            )
            journal.connection.execute(
                "INSERT INTO route_checkpoints VALUES (?,'canonical_source_bootstrap',?) "
                "ON CONFLICT(position_id,checkpoint_key) DO UPDATE SET "
                "payload_json=excluded.payload_json",
                (position["id"], json.dumps(encode(marker), sort_keys=True)),
            )
            journal.connection.execute(
                "UPDATE route_positions SET state='planned',reason=NULL,"
                "updated_at=CURRENT_TIMESTAMP "
                "WHERE id=?",
                (position["id"],),
            )
            journal.connection.execute(
                "UPDATE route_groups SET state='active',reason=NULL,updated_at=CURRENT_TIMESTAMP "
                "WHERE id=?",
                (group["id"],),
            )
        if marker is not None:
            entry["canonical_source_bootstrap"] = marker
            changed += 1
    return changed


class ForecastRpc:
    """Adds just the owner's future balance to explicitly permitted gas simulations."""

    def __init__(self, rpc, *, url, chain_id, owner, projected_balance):
        if chain_id not in WETH_CONTRACTS or projected_balance <= 0:
            raise ValueError("forecast_chain_or_balance_invalid")
        if quantity(rpc.call(url, "eth_chainId", [])) != chain_id:
            raise ValueError("forecast_chain_mismatch")
        self.rpc, self.url, self.chain_id = rpc, url, chain_id
        self.owner, self.projected_balance = owner, projected_balance
        self._allowed = set()

    def allow(self, request):
        if (
            request.chain_id != self.chain_id
            or request.value < 0
            or request.value > self.projected_balance
        ):
            raise ValueError("forecast_request_identity_invalid")
        self._allowed.add((request.to.lower(), request.data.lower(), request.value))

    def call(self, url, method, params):
        if method.startswith(("eth_send", "eth_sign", "personal_", "wallet_")):
            raise ValueError("forecast_read_only")
        if method != "eth_estimateGas":
            return self.rpc.call(url, method, params)
        if url != self.url:
            # Other-chain estimates use actual funds and may never carry an override.
            raise ValueError("forecast_url_mismatch")
        if (
            len(params) != 2
            or params[1] != "latest"
            or not isinstance(params[0], dict)
            or set(params[0]) != {"from", "to", "data", "value"}
        ):
            raise ValueError("forecast_parameters_invalid")
        tx = params[0]
        try:
            value = quantity(tx["value"])
            allowed = (tx["to"].lower(), tx["data"].lower(), value) in self._allowed
            if (
                tx["from"].lower() != self.owner.lower()
                or not allowed
                or value > self.projected_balance
            ):
                raise ValueError("forecast_transaction_mismatch")
        except (KeyError, AttributeError, TypeError) as exc:
            raise ValueError("forecast_transaction_invalid") from exc
        return self.rpc.call(
            url,
            method,
            [
                tx,
                "latest",
                {
                    self.owner: {"balance": hex(self.projected_balance)},
                },
            ],
        )


def future_bridge_proof(step, marker):
    """Persist exact staged payload/caps as plain data, bounded by generated funds."""
    from .fee_planner import native_asset_identity
    from .lifi import validate_bridge_route
    from .models import AssetIdentity

    route = step["_live_route"]
    evidence = route.evidence
    chain = marker["chain_id"]
    target = marker["target"]
    native = native_asset_identity(chain)
    validate_bridge_route(
        route,
        expected_source=native,
        expected_destination=AssetIdentity(
            target["chain_id"], target["asset_id"], target["decimals"]
        ),
        input_amount=evidence.input_amount,
        recipient=marker["recipient"],
    )
    transactions = step["_prepared_requests"]
    future_balance = int(marker["future_balance_raw"])
    reserve = int(marker["future_source_fee_reserve_raw"])
    normalized = tuple(item for item in evidence.steps if not item.is_substep)
    for tx, item in zip(transactions, normalized, strict=True):
        if tx.chain_id == chain and (item.source != native or tx.value != item.from_amount):
            raise ValueError("forecast_staged_transaction_invalid")
    source_caps = sum(tx.max_total_fee_cap_wei or 0 for tx in transactions if tx.chain_id == chain)
    if (
        not step.get("pending_source_conversion")
        or not transactions
        or len(transactions) != len(route.raw_steps)
        or source_caps <= 0
        or any(
            tx.max_total_fee_cap_wei is None
            or tx.max_total_fee_wei > tx.max_total_fee_cap_wei
            or tx.fee_quote_fallback_reason != "pending_source_conversion"
            for tx in transactions
        )
        or evidence.min_output_amount < int(target["minimum_raw"])
        or future_balance != int(marker["amount_raw"]) - int(marker["source_fee_cap_raw"])
        or future_balance - evidence.input_amount != reserve
        or reserve < 3 * source_caps
    ):
        raise ValueError("forecast_staged_proof_invalid")
    return dict(
        payload_sha256=evidence.payload_sha256,
        source_chain_id=chain,
        owner=marker["wallet"],
        recipient=marker["recipient"],
        projected_balance_raw=str(future_balance),
        transactions=[
            dict(
                chain_id=tx.chain_id,
                to=tx.to.lower(),
                calldata_sha256=sha256(tx.data.lower().encode()).hexdigest(),
                value_raw=str(tx.value),
                fee_cap_raw=str(tx.max_total_fee_cap_wei),
            )
            for tx in transactions
        ],
    )


def source_withdrawal_reserve(entry, request, wallet):
    """Separate proof for a value-zero source withdrawal; no general reserve changes."""
    marker = entry.get("canonical_source_bootstrap")
    if marker is None or not str(entry.get("_journal_step_key", "")).startswith("swap_to_native:"):
        return None
    try:
        chain, amount = int(entry["chain_id"]), int(entry["raw_balance"])
        source_step = entry["steps"][0]
        raw = source_step["_live_route"].raw_steps
        cap = int(marker["source_fee_cap_raw"])
        if (
            not entry.get("automatic_requote")
            or not callable(entry.get("_check_group_loss"))
            or len(entry["steps"]) != 2
            or source_step["kind"] != "swap_to_native"
            or entry["steps"][1]["kind"] != "bridge"
            or len(raw) != 1
            or marker["wallet"].lower() != wallet.public_address.lower()
            or marker["recipient"].lower() != wallet.bitget_deposit_address.lower()
            or marker["target"] != entry["target"]
            or marker["chain_id"] != chain
            or marker["asset_id"] != WETH_CONTRACTS.get(chain)
            or entry["decimals"] != 18
            or marker["decimals"] != 18
            or int(marker["amount_raw"]) != amount
            or entry["asset_id"] != marker["asset_id"]
            or cap <= 0
            or request.max_total_fee_cap_wei != cap
            or request.max_total_fee_wei > cap
            or marker["group_key"] != entry["group"]["key"]
            or marker["source_usd"] != entry["group"]["source_usd"]
            or marker["loss_budget_pct"] != entry["group"]["max_route_loss_pct"]
            or marker["forecast_proof"] != future_bridge_proof(entry["steps"][1], marker)
            or (request.chain_id, request.to.lower(), request.data.lower())
            != withdraw_transaction_fields(raw[0])
            or request.value != 0
            or raw[0]["action"]["fromAddress"].lower() != wallet.public_address.lower()
        ):
            raise ValueError("invalid source withdrawal proof")
    except (KeyError, ValueError, AttributeError, TypeError, IndexError) as exc:
        raise ValueError("canonical_source_bootstrap_reserve_proof_invalid") from exc
    return (cap * 12 + 9) // 10


def source_output(*, amount, paid_gas, initial_native, current_native):
    """Keep pre-existing gas and cap generated funds even when unrelated ETH arrives."""
    output = min(amount - paid_gas, current_native - initial_native)
    if min(amount, paid_gas, initial_native, current_native) < 0 or output <= 0:
        raise ValueError("canonical_source_output_unavailable")
    return output


def validate_marker(entry, wallet):
    marker = entry["canonical_source_bootstrap"]
    try:
        if (
            not entry.get("automatic_requote")
            or entry["decimals"] != 18
            or entry["asset_id"] != WETH_CONTRACTS.get(entry["chain_id"])
            or marker["wallet"].lower() != wallet.public_address.lower()
            or entry["wallet"].lower() != wallet.public_address.lower()
            or marker["recipient"].lower() != wallet.bitget_deposit_address.lower()
            or entry["deposit_address"].lower() != wallet.bitget_deposit_address.lower()
            or marker["target"] != entry["target"]
            or entry["target"]["coin"] != "USDC"
            or any(marker[key] != entry[key] for key in ("chain_id", "asset_id", "decimals"))
            or int(marker["amount_raw"]) != int(entry["raw_balance"])
            or marker["group_key"] != entry["group"]["key"]
            or marker["source_usd"] != entry["group"]["source_usd"]
            or marker["loss_budget_pct"] != entry["group"]["max_route_loss_pct"]
        ):
            raise ValueError("changed marker")
    except (KeyError, TypeError, ValueError, AttributeError) as exc:
        raise ValueError("canonical_source_bootstrap_identity_invalid") from exc
    return marker


def preflight_source_bootstrap(entry, *, wallet, rpc, rpc_urls, jumper, bitget, now_ms):
    """Prove the real withdrawal and staged future bridge without changing actual funds."""
    from dataclasses import replace
    from datetime import UTC, datetime, timedelta

    from .consolidation import candidate_from_route
    from .fee_planner import FeePlanner, native_asset_identity
    from .lifi import validate_bridge_route
    from .live_plan import _route_data, _valuation_data
    from .models import AssetIdentity
    from .planner_gas import PlannerGasEstimator
    from .route_execution import (
        _MAX_PRICE_AGE,
        _asset_balance,
        _canonical_unwrap_route,
        _decimal_string,
        _exact_sum,
        _gas_fee_quote,
        _native_balance,
        _require_live_target,
        _require_valid_candidate,
        _requote_bridge_after_swap,
    )
    from .stages import StageDeadlineExceeded

    marker = dict(validate_marker(entry, wallet))
    live_target = _require_live_target(entry, wallet=wallet, bitget=bitget)
    chain, amount = entry["chain_id"], int(marker["amount_raw"])
    asset = AssetIdentity(chain, entry["asset_id"], 18)
    url = rpc_urls[chain]
    if quantity(rpc.call(url, "eth_chainId", [])) != chain:
        raise ValueError("canonical_source_bootstrap_chain_mismatch")
    if _asset_balance(rpc, url=url, asset=asset, wallet=wallet.public_address) != amount:
        raise ValueError("canonical_source_bootstrap_balance_changed")

    def now():
        return datetime.fromtimestamp(now_ms() / 1000, UTC)

    route = _canonical_unwrap_route(jumper, asset, amount, wallet, now)
    source_request = jumper.step_transaction(route.raw_steps[0])
    expected = withdraw_transaction_fields(route.raw_steps[0])
    if (
        source_request.chain_id,
        source_request.to.lower(),
        source_request.data.lower(),
    ) != expected:
        raise ValueError("canonical_source_bootstrap_transaction_changed")
    if source_request.value != 0:
        raise ValueError("canonical_source_bootstrap_transaction_value")
    previous_cap = int(marker.get("source_fee_cap_raw", "0"))
    # A cached staged amount is stable while the real withdrawal remains within its cap.
    request = FeePlanner(rpc).plan(
        url, replace(source_request, max_total_fee_cap_wei=None), sender=wallet.public_address
    )
    initial = _native_balance(rpc, url=url, wallet=wallet.public_address)
    ceiling_limit = initial * 10 // 12
    actual_cap = request.max_total_fee_wei
    if actual_cap > ceiling_limit:
        raise StageDeadlineExceeded("canonical_source_bootstrap_insufficient_initial_gas")
    if actual_cap <= previous_cap <= ceiling_limit:
        cap = previous_cap
    else:
        padded = (actual_cap * 12 + 9) // 10
        cap = padded if padded <= ceiling_limit else actual_cap
    request = replace(request, max_total_fee_cap_wei=cap)
    future_balance = amount - cap
    if future_balance <= 0:
        raise ValueError("canonical_source_bootstrap_no_future_balance")
    forecast_rpc = ForecastRpc(
        rpc, url=url, chain_id=chain, owner=wallet.public_address, projected_balance=future_balance
    )

    class RouteRpc:
        def call(self, other_url, method, params):
            if other_url == url:
                return forecast_rpc.call(other_url, method, params)
            if method == "eth_estimateGas" and len(params) != 2:
                raise ValueError("forecast_forbidden_other_chain_override")
            return rpc.call(other_url, method, params)

    class ForecastEstimator(PlannerGasEstimator):
        def _estimate_route_transactions(self, route, wallet, prepared_transactions):
            from .valuation import FeeQuote

            quotes = super()._estimate_route_transactions(route, wallet, prepared_transactions)
            if quotes is None:
                return None
            capped = self.prepared_fee_caps[route.route_id]
            stable, conservative = [], []
            for original, planned, quote in zip(prepared_transactions, capped, quotes, strict=True):
                cap = planned.max_total_fee_wei
                if planned.chain_id == chain:
                    previous = original.max_total_fee_cap_wei
                    cap = (
                        previous
                        if previous is not None and cap <= previous
                        else (cap * 12 + 9) // 10
                    )
                    planned = replace(planned, max_total_fee_cap_wei=cap)
                    quote = FeeQuote(cap, quote.price)
                stable.append(planned)
                conservative.append(quote)
            self.prepared_fee_caps[route.route_id] = tuple(stable)
            return tuple(conservative)

        def estimate_prepared_route(self, *, route, wallet, prepared_transactions):
            native = native_asset_identity(chain)
            destination = AssetIdentity(
                entry["target"]["chain_id"],
                entry["target"]["asset_id"],
                entry["target"]["decimals"],
            )
            validate_bridge_route(
                route,
                expected_source=native,
                expected_destination=destination,
                input_amount=route.evidence.input_amount,
                recipient=marker["recipient"],
            )
            normalized = tuple(s for s in route.evidence.steps if not s.is_substep)
            if len(normalized) != len(prepared_transactions):
                raise ValueError("forecast_prepared_shape_invalid")
            for raw, step, tx in zip(
                route.raw_steps, normalized, prepared_transactions, strict=True
            ):
                if tx.chain_id == chain:
                    if (
                        step.source != native
                        or step.from_amount != tx.value
                        or tx.chain_id != step.source.chain_id
                        or raw["action"]["fromAddress"].lower() != marker["wallet"].lower()
                    ):
                        raise ValueError("forecast_native_transaction_identity_invalid")
                    forecast_rpc.allow(tx)
            return super().estimate_prepared_route(
                route=route, wallet=wallet, prepared_transactions=prepared_transactions
            )

    forecast_estimator = ForecastEstimator(
        RouteRpc(), rpc_urls, jumper, retry_transient_errors=True
    )
    source_candidate = candidate_from_route(
        route,
        expected_source=asset,
        expected_destination=native_asset_identity(chain),
        expected_input_amount=amount,
        recipient=wallet.public_address,
        gas_estimate_complete=True,
        now=now(),
        max_price_age=_MAX_PRICE_AGE,
        wallet_paid_gas=(_gas_fee_quote(cap, native_asset_identity(chain), jumper),),
        replace_route_gas=True,
    )
    _require_valid_candidate(source_candidate)
    with localcontext() as context:
        context.prec = 80
        budget = Decimal(marker["source_usd"]) * Decimal(marker["loss_budget_pct"]) / 100
        paid = Decimal(marker["paid_loss_usd"])
        remaining = budget - paid - source_candidate.loss_usd
    if remaining <= 0:
        raise ValueError("canonical_source_bootstrap_loss_threshold_exceeded")
    bridge = next((s for s in entry["steps"] if s.get("kind") == "bridge"), None)
    cached = (
        bridge
        and bridge.get("pending_source_conversion")
        and previous_cap == cap
        and int(marker.get("future_balance_raw", "0")) == future_balance
        and 0
        <= now_ms() - marker.get("forecast_quoted_at_ms", 0)
        < _MAX_PRICE_AGE.total_seconds() * 1000
    )
    if cached:
        cached = all(
            timedelta(0)
            <= now() - datetime.fromisoformat(p.timestamp.replace("Z", "+00:00"))
            <= _MAX_PRICE_AGE
            for p in bridge["_live_route"].evidence.prices
        )
    if cached:
        quotes = forecast_estimator.estimate_prepared_route(
            route=bridge["_live_route"],
            wallet=wallet.public_address,
            prepared_transactions=bridge["_prepared_requests"],
        )
        saved_cap = int(marker.get("future_source_fee_reserve_raw", "0"))
        current_cap = sum(
            q.raw_amount
            for q in quotes or ()
            if q.price and q.price.asset == native_asset_identity(chain)
        )
        cached = bool(quotes and current_cap > 0 and current_cap * 3 <= saved_cap)
        if cached:
            refreshed = candidate_from_route(
                bridge["_live_route"], expected_source=native_asset_identity(chain),
                expected_destination=AssetIdentity(entry["target"]["chain_id"],
                    entry["target"]["asset_id"], entry["target"]["decimals"]),
                expected_input_amount=bridge["_live_route"].evidence.input_amount,
                recipient=marker["recipient"], gas_estimate_complete=True,
                wallet_paid_gas=quotes, replace_route_gas=True, now=now(),
                max_price_age=_MAX_PRICE_AGE,
            )
            cached = (refreshed.is_valid and refreshed.loss_usd <= remaining
                      and refreshed.destination_amount >= live_target.minimum_raw)
            if cached:
                bridge = {**bridge, "_candidate": refreshed,
                          "valuation": _valuation_data(refreshed)}
    if not cached:
        target = entry["target"]
        # Only destination evidence is read here; no fabricated actual balance is introduced.
        seed = {
            **entry,
            "steps": [
                {
                    "kind": "bridge",
                    "route": {
                        "evidence": {
                            "destination": {
                                "chain_id": target["chain_id"],
                                "asset_id": target["asset_id"],
                                "decimals": target["decimals"],
                            },
                        }
                    },
                }
            ],
        }
        bridge = _requote_bridge_after_swap(
            seed,
            future_balance,
            wallet=wallet,
            jumper=jumper,
            bitget=bitget,
            gas_estimator=forecast_estimator,
            now_ms=now_ms,
            remaining_loss_usd=remaining,
            retry_unsigned_preparation=True,
        )
    if not cached:
        marker["forecast_quoted_at_ms"] = now_ms()
    if bridge["_live_route"].evidence.min_output_amount < live_target.minimum_raw:
        raise ValueError("requote_required:below_target_minimum")
    loss = _exact_sum((source_candidate.loss_usd, Decimal(bridge["valuation"]["loss_usd"])))
    if paid + loss > budget:
        raise ValueError("canonical_source_bootstrap_loss_threshold_exceeded")
    prepared = forecast_estimator.prepared_fee_caps.get(
        bridge["route"]["id"], bridge.get("_prepared_requests", ())
    )
    if not prepared and bridge.get("_prepared_request") is not None:
        prepared = (bridge["_prepared_request"],)
    if not prepared:
        raise ValueError("forecast_fee_cap_evidence_missing")
    future_spend = int(bridge["route"]["evidence"]["input_amount"])
    future_reserve = future_balance - future_spend
    marker.update(
        source_fee_cap_raw=str(cap),
        future_balance_raw=str(future_balance),
        future_source_fee_reserve_raw=str(future_reserve),
    )
    bridge = {
        **bridge,
        "pending_source_conversion": True,
        "_prepared_requests": tuple(
            replace(tx, fee_quote_fallback_reason="pending_source_conversion") for tx in prepared
        ),
    }
    if len(prepared) == 1:
        bridge["_prepared_request"] = bridge["_prepared_requests"][0]
    marker["forecast_proof"] = future_bridge_proof(bridge, marker)
    source_step = dict(
        kind="swap_to_native",
        route=_route_data(route),
        _live_route=route,
        _prepared_requests=(request,),
        _prepared_request=request,
    )
    valuation = _valuation_data(source_candidate)
    prior = Decimal(marker["accounted_loss_usd"])
    valuation.update(loss_usd=_decimal_string(_exact_sum((prior, loss))))
    return {
        **entry,
        "steps": [source_step, bridge],
        "quoted_at": now_ms(),
        "canonical_source_bootstrap": marker,
        "_realized_loss_before_current_usd": str(prior),
        "_candidate": source_candidate,
        "_step_candidates": {route.route_id: source_candidate},
        "valuation": valuation,
    }


def require_source_receipt(entry, *, tx_hash, rpc, url, wallet):
    """Bind success to canonical contract, exact owner/calldata and Withdrawal effect."""
    from eth_utils import keccak

    marker = validate_marker(entry, wallet)
    receipt = rpc.call(url, "eth_getTransactionReceipt", [tx_hash])
    transaction = rpc.call(url, "eth_getTransactionByHash", [tx_hash])
    contract, amount = marker["asset_id"], int(marker["amount_raw"])
    expected_topic = "0x" + keccak(text="Withdrawal(address,uint256)").hex()
    owner_topic = "0x" + wallet.public_address[2:].lower().rjust(64, "0")
    try:
        if (
            not isinstance(receipt, dict)
            or not isinstance(transaction, dict)
            or receipt["transactionHash"].lower() != tx_hash.lower()
            or quantity(receipt["status"]) != 1
            or transaction["hash"].lower() != tx_hash.lower()
            or transaction["from"].lower() != wallet.public_address.lower()
            or transaction["to"].lower() != contract
            or transaction["input"].lower() != "0x2e1a7d4d" + f"{amount:064x}"
            or quantity(transaction["value"]) != 0
        ):
            raise ValueError("changed transaction")
        effects = [
            log
            for log in receipt["logs"]
            if log.get("address", "").lower() == contract
            and log.get("topics") == [expected_topic, owner_topic]
            and uint256(log.get("data")) == amount
            and not log.get("removed", False)
        ]
        if len(effects) != 1:
            raise ValueError("missing or duplicate withdrawal effect")
    except (KeyError, ValueError, TypeError, AttributeError) as exc:
        raise ValueError("canonical_source_receipt_mismatch") from exc
    paid = quantity(receipt["gasUsed"]) * quantity(receipt["effectiveGasPrice"]) + quantity(
        receipt.get("l1Fee", "0x0")
    )
    return dict(
        tx_hash=tx_hash,
        chain_id=marker["chain_id"],
        owner=wallet.public_address,
        contract=contract,
        amount_raw=str(amount),
        paid_gas_raw=str(paid),
        status=1,
    )


def confirmed_unsigned_source_bridge(journal, position_id, entry):
    """Grant unsigned bridge retries only after the exact proved canonical withdrawal."""
    marker = entry.get("canonical_source_bootstrap")
    if not marker:
        return False
    try:
        position = journal.position(position_id)
        prepared = journal.checkpoint(position_id, "prepared")
        saved = journal.checkpoint(position_id, "swap_result")
        provenance = journal.checkpoint(position_id, "source_cost_provenance")
        if not prepared or not saved or not provenance or provenance.get("version") != 2:
            return False
        source = prepared["steps"][0]
        route = source["_live_route"]
        key = f"swap_to_native:{route.route_id}:0"
        rows = journal.connection.execute(
            "SELECT * FROM route_steps WHERE position_id=?", (position_id,)
        ).fetchall()
        if len(rows) != 1 or rows[0]["step_key"] != key:
            return False  # Includes every approval, bridge intent and NULL hash.
        intent = rows[0]
        signing = journal.checkpoint(position_id, f"canonical_signing:{key}")
        proof = saved.get("source_receipt_proof")
        if not signing or not isinstance(proof, dict):
            return False
        request = signing["request"]
        signed = signing["entry"]
        amount = int(marker["amount_raw"])
        chain = marker["chain_id"]
        expected = withdraw_transaction_fields(route.raw_steps[0])
        group = journal.group(position["group_id"])
        return bool(
            entry.get("automatic_requote")
            and source["kind"] == "swap_to_native"
            and intent["state"] in {"confirmed", "credited"}
            and intent["tx_hash"]
            and (request.chain_id, request.to.lower(), request.data.lower()) == expected
            and request.value == 0
            and request.max_total_fee_cap_wei == int(marker["source_fee_cap_raw"])
            and request.max_total_fee_wei <= request.max_total_fee_cap_wei
            and sha256(request.data.encode()).hexdigest() == intent["calldata_digest"]
            and signed["wallet"].lower() == position["wallet"] == entry["wallet"].lower()
            and signed["target"] == prepared["target"] == marker["target"] == entry["target"]
            and signed["deposit_address"].lower() == marker["recipient"].lower()
            and prepared["chain_id"] == entry["chain_id"] == group["source_chain_id"] == chain
            and prepared["asset_id"] == position["source_asset_id"] == WETH_CONTRACTS[chain]
            and int(position["source_amount_raw"]) == int(prepared["raw_balance"]) == amount
            and group["group_key"] == marker["group_key"] == entry["group"]["key"]
            and group["source_usd"] == marker["source_usd"]
            and group["loss_budget_pct"] == marker["loss_budget_pct"]
            and proof["status"] == 1
            and proof["chain_id"] == chain
            and proof["owner"].lower() == marker["wallet"].lower() == position["wallet"]
            and proof["contract"] == WETH_CONTRACTS[chain]
            and int(proof["amount_raw"]) == amount
            and proof["tx_hash"] == intent["tx_hash"]
            and 0 <= int(proof["paid_gas_raw"]) <= request.max_total_fee_cap_wei
            and saved["state"] in {"confirmed", "completed"}
            and saved["actual_asset_id"] == "native"
            and 0 < int(saved["actual_balance_raw"]) <= amount - int(proof["paid_gas_raw"])
            and int(saved["protected_native_raw"]) >= 0
        )
    except (KeyError, TypeError, ValueError, AttributeError, IndexError):
        return False


def require_source_bridge_retry_due(journal, position_id, entry, *, now_seconds):
    """Honor the saved provider window for this narrowly unsigned continuation."""
    import math

    from .route_preparation import require_provider_retry_due
    from .stages import StageDeadlineExceeded

    if not confirmed_unsigned_source_bridge(journal, position_id, entry):
        if (entry.get("canonical_source_bootstrap")
                and journal.checkpoint(position_id, "swap_result") is not None
                and journal.has_asset_intent(position_id)
                and journal.connection.execute(
                    "SELECT 1 FROM route_steps WHERE position_id=? AND step_key LIKE 'bridge:%'",
                    (position_id,),
                ).fetchone() is None):
            raise ValueError("canonical_source_confirmation_proof_invalid")
        return require_provider_retry_due(journal, position_id, now_seconds=now_seconds)
    retry = journal.checkpoint(position_id, "automatic_retry") or {}
    retry_at = retry.get("retry_at")
    if (
        isinstance(retry_at, (int, float))
        and not isinstance(retry_at, bool)
        and math.isfinite(retry_at)
        and now_seconds < retry_at
    ):
        raise StageDeadlineExceeded("provider_retry_scheduled")
