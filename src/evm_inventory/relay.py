"""Relay quotes, exact asset validation and durable request accounting."""

from __future__ import annotations

import json
import re
import time
from datetime import UTC, datetime
from decimal import Decimal
from threading import Lock
from uuid import uuid4

from .diagnostics import exception_diagnostic
from .lifi_quota import LifiQuotaStore, LifiRequestLimit
from .quote_jobs import JobInProgress, QuoteJobs, quote_job_key
from .relay_protocol import verified_transaction
from .transport import is_transient_error

BASE_URL = "https://api.relay.link/"
ZERO_ADDRESS = "0x" + "0" * 40
SOURCE_CHAINS = frozenset({360, 5330, 7777777})


class RelayQuotaStore(LifiQuotaStore):
    # Separate provider scope; deliberately below the documented 50 quotes/minute.
    limits = {"routes": (40, 60, 0), "steps": (40, 60, 0), "public": (100, 60, 0)}


def _decimal(value):
    result = Decimal(str(value))
    if not result.is_finite() or result < 0:
        raise ValueError("Relay amount/valuation must be finite and nonnegative")
    return result


def _address(asset):
    return ZERO_ADDRESS if asset == "native" else asset.lower()


def validate_quote(body, request):
    """A quote is evidence only; it never authorizes execution through LI.FI."""
    details = body["details"]
    for field, expected in (("sender", request["user"]), ("recipient", request["recipient"])):
        if str(details.get(field, "")).lower() != expected.lower():
            raise ValueError(f"Relay {field} does not match request")
    for field, chain, address in (
        ("currencyIn", request["originChainId"], request["originCurrency"]),
        ("currencyOut", request["destinationChainId"], request["destinationCurrency"]),
    ):
        currency = details[field]["currency"]
        if currency["chainId"] != chain or currency["address"].lower() != address:
            raise ValueError(f"Relay {field} asset does not match request")
        if currency["decimals"] != request["expectedDecimals"][field]:
            raise ValueError(f"Relay {field} decimals do not match request")
        if int(details[field]["amount"]) <= 0:
            raise ValueError(f"Relay {field} amount must be positive")
        _decimal(details[field]["amountUsd"])
    if int(details["currencyIn"]["amount"]) != int(request["amount"]):
        raise ValueError("Relay input amount does not match request")
    output = details["currencyOut"]
    if not 0 < int(output["minimumAmount"]) <= int(output["amount"]):
        raise ValueError("Relay minimum output is invalid")
    if not body.get("steps"):
        raise ValueError("Relay quote contains no transaction steps")
    gas = body["fees"]["gas"]
    currency = gas["currency"]
    if (
        currency["chainId"] != request["originChainId"]
        or currency["address"].lower() != ZERO_ADDRESS
        or currency["decimals"] != 18
    ):
        raise ValueError("Relay source gas currency is not native ETH on the source chain")
    _decimal(gas["amountUsd"])
    if int(gas["amount"]) < 0:
        raise ValueError("Relay source gas amount is negative")
    return body


class RelayClient:
    def __init__(self, http_client, *, policy_digest):
        self.http_client = http_client
        self.policy_digest = policy_digest
        self.jobs = QuoteJobs(http_client.db_path, http_client.scope)
        self._lock = Lock()
        self._chains = None
        self.job_cache_hits = 0
        self.chain_catalog = []

    def supported_chain_ids(self):
        with self._lock:
            if self._chains is None:
                response = self.http_client.get(BASE_URL + "chains")
                response.raise_for_status()
                self.chain_catalog = response.json()["chains"]
                self._chains = frozenset(
                    chain["id"]
                    for chain in self.chain_catalog
                    if chain.get("depositEnabled") and not chain.get("disabled")
                )
            return self._chains

    def quote(self, base, target, amount, *, recipient=None, fresh=False):
        request = {
            "user": base["wallet"],
            "recipient": recipient or base["wallet"],
            "originChainId": base["chain_id"],
            "destinationChainId": target.chain_id,
            "originCurrency": _address(base["asset_id"]),
            "destinationCurrency": _address(target.asset_id),
            "amount": str(amount),
            "tradeType": "EXACT_INPUT",
            "includeProtocolData": True,
            "slippageTolerance": "50",
            "expectedDecimals": {"currencyIn": base["decimals"], "currencyOut": target.decimals},
        }
        complete_request = {"provider": "relay", "endpoint": "quote/v2", **request}
        if fresh:
            complete_request["execution_refresh"] = uuid4().hex
        key = quote_job_key(self.jobs.scope, complete_request, self.policy_digest)
        row = self.jobs.read(key)
        if row and row["state"] == "complete":
            self.job_cache_hits += 1
            return self._result(row["result_json"], request, row["observed_at"])
        try:
            claim = self.jobs.claim(key, complete_request, self.policy_digest)
        except JobInProgress as error:
            if error.reason == "complete":
                row = self.jobs.read(key)
                if row and row["state"] == "complete":
                    self.job_cache_hits += 1
                    return self._result(row["result_json"], request, row["observed_at"])
            raise
        try:
            with self.jobs.heartbeat(claim):
                payload = {k: v for k, v in request.items() if k != "expectedDecimals"}
                response = self.http_client.post(
                    BASE_URL + "quote/v2",
                    json=payload,
                    cache_namespace=complete_request.get("execution_refresh"),
                )
                response.raise_for_status()
                body = validate_quote(response.json(), request)
                observed = float(response.headers["x-inventory-observed-at"])
                if not self.jobs.finish(claim, body, observed):
                    raise JobInProgress(reason="job_in_progress")
                return self._result(body, request, observed)
        except Exception as error:
            self.jobs.fail(
                claim,
                error,
                "quota_wait" if isinstance(error, LifiRequestLimit) else "error",
                retry_at=getattr(error, "retry_at", None),
            )
            raise

    def status(self, request_id):
        if not re.fullmatch(r"0x[0-9a-fA-F]{64}", request_id):
            raise ValueError("Invalid Relay request ID")
        response = self.http_client.get(
            BASE_URL + "intents/status/v3",
            params={"requestId": request_id},
            cache_namespace=uuid4().hex,
        )
        response.raise_for_status()
        return response.json()

    @staticmethod
    def _result(body, request, observed):
        validate_quote(body, request)
        details = body["details"]
        incoming, outgoing = details["currencyIn"], details["currencyOut"]
        source_usd = _decimal(incoming["amountUsd"])
        if source_usd <= 0:
            raise ValueError("Relay source valuation is zero")
        output_usd = _decimal(outgoing["amountUsd"])
        minimum_usd = output_usd * Decimal(outgoing["minimumAmount"]) / Decimal(outgoing["amount"])
        gas_usd = _decimal(body["fees"]["gas"]["amountUsd"])
        loss_pct = (source_usd - minimum_usd + gas_usd) / source_usd * 100
        return {
            "provider": "relay",
            "request": request,
            "response": body,
            "observed_at": datetime.fromtimestamp(observed, UTC).isoformat(),
            "source_usd": str(source_usd),
            "minimum_output_usd": str(minimum_usd),
            "source_gas_usd": str(gas_usd),
            "loss_pct": str(loss_pct),
            "source_gas_raw": str(body["fees"]["gas"]["amount"]),
            "to_amount": outgoing["amount"],
            "to_amount_min": outgoing["minimumAmount"],
        }


class _RelayGasReserveChanged(ValueError):
    reason = "relay_gas_reserve_changed"

    def __init__(self, *, balance, amount, fee, reserve):
        self.retry_at = time.time() + 5
        self.diagnostic = {
            "balance_raw": str(balance), "input_raw": str(amount),
            "required_fee_raw": str(fee), "reserve_raw": str(reserve),
        }
        super().__init__(self.reason)


def _amount_too_low(diagnostic):
    """Recognize Relay's structured economic refusal, including a persisted job error."""
    for item in diagnostic.get("exceptions", []):
        if item.get("http_status") == 400:
            try:
                body = json.loads(item.get("http_response_body", ""))
            except (ValueError, TypeError):
                body = None
            if isinstance(body, dict) and body.get("errorCode") == "AMOUNT_TOO_LOW":
                return True
        provider = item.get("provider")
        if isinstance(provider, dict) and _amount_too_low(provider):
            return True
    return False


def _search_error(error, target=None):
    retry_at = getattr(error, "retry_at", None)
    reason = getattr(error, "reason", "relay_quote_failed")
    cause = error
    seen = set()
    while retry_at is None and cause is not None and id(cause) not in seen:
        seen.add(id(cause))
        retry_at = getattr(cause, "retry_at", None)
        if retry_at is not None:
            reason = getattr(cause, "reason", reason)
        cause = cause.__cause__
    if retry_at is None and is_transient_error(error):
        retry_at = time.time() + 5
    diagnostic = exception_diagnostic(error)
    if retry_at is None and _amount_too_low(diagnostic):
        reason = "below_target_minimum"
    result = {"reason": reason, "diagnostic": diagnostic, "retry_at": retry_at}
    if target is not None:
        result["destination_chain_id"] = target.chain_id
    return result


def _fee_budget(planned, *, balance, amount, reserve, buffer, reserve_method):
    fee = planned.max_total_fee_wei
    return {
        "balance_raw": str(balance), "input_raw": str(amount), "required_fee_raw": str(fee),
        "required_total_raw": str(amount + fee),
        "shortfall_raw": str(max(0, amount + fee - balance)),
        "reserve_raw": str(reserve), "reserve_buffer_raw": str(buffer),
        "reserve_multiplier": "1" if reserve_method == "exact_cap" else "1.2",
        "reserve_method": reserve_method, "gas_limit": planned.gas_limit,
        "gas_estimate": planned.gas_estimate,
        "gas_price_wei": planned.gas_price_wei,
        "max_fee_per_gas_wei": planned.max_fee_per_gas_wei,
        "max_priority_fee_per_gas_wei": planned.max_priority_fee_per_gas_wei,
        "additional_fee_raw": str(planned.additional_fee_wei),
        "fee_quote_method": planned.fee_quote_method,
        "fee_quote_block_number": planned.fee_quote_block_number,
    }


def _native_quote(base, client, target, first, gas_estimator, recipient):
    prepare = getattr(gas_estimator, "prepare_relay_fee", None)
    available = getattr(gas_estimator, "relay_fee_available", None)
    if not callable(prepare) or (callable(available) and not available(base["chain_id"])):
        return {**first, "source_gas_estimate_verified": False,
                "source_gas_provider_estimate_raw": first["source_gas_raw"],
                "gas_reserve_sufficient": False, "reason": "relay_source_gas_unverified"}

    balance = int(base["raw_balance"])
    quote, amount = first, balance
    reserve, buffer, method = 0, 0, "rpc_cap_plus_fixed_buffer"
    # One adjustment derives the spendable amount. If its fee exceeds the
    # padded reserve, allow one expansion; continued movement is a timed retry.
    for adjustment in range(3):
        planned = prepare(quote=quote, chain_catalog=client.chain_catalog,
                          wallet=base["wallet"], source_chain_id=base["chain_id"])
        fee = planned.max_total_fee_wei
        if fee <= 0:
            raise ValueError("Relay exact gas fee must be positive")
        if adjustment == 0:
            buffer = (fee * 2 + 9) // 10
        if fee >= balance:
            # No positive input can fit. Record a fee-only shortfall; the
            # full-balance probe itself is not an affordable transaction.
            quote = {**quote, "gas_reserve_sufficient": False,
                     "reason": "insufficient_source_gas"}
            budget = _fee_budget(planned, balance=balance, amount=0, reserve=fee,
                                 buffer=buffer, reserve_method=method)
            break
        if adjustment > 0 and fee <= reserve:
            quote = {**quote, "gas_reserve_sufficient": True}
            budget = _fee_budget(planned, balance=balance, amount=amount, reserve=reserve,
                                 buffer=buffer, reserve_method=method)
            break
        if adjustment == 2:
            raise _RelayGasReserveChanged(balance=balance, amount=amount, fee=fee, reserve=reserve)
        reserve = fee + buffer
        if reserve >= balance:
            # The RPC cap already includes FeePlanner's gas and chain fee
            # bounds. Preserve it when the additional reserve cannot fit.
            reserve, method, buffer = fee, "exact_cap", 0
        amount = balance - reserve
        quote = client.quote(base, target, amount, recipient=recipient)
    return {
        **quote, "source_gas_provider_estimate_raw": quote["source_gas_raw"],
        "source_gas_raw": str(fee), "source_gas_estimate_verified": True,
        "source_gas_reserve_raw": budget["reserve_raw"], "gas_budget": budget,
    }


def _evaluate_candidate(base, target, quote, original_usd, current_usd, loss_limit):
    quote["target"] = {
        "coin": target.coin, "chain": target.chain, "chain_id": target.chain_id,
        "asset_id": target.asset_id, "decimals": target.decimals,
        "minimum_raw": str(target.minimum_raw),
    }
    quote["source_balance_usd"] = str(original_usd)
    quote["current_source_balance_usd"] = str(current_usd)
    quote["deposit_minimum_raw"] = str(target.minimum_raw)
    quote["max_route_loss_pct"] = str(loss_limit)
    quote["deposit_minimum_met"] = int(quote["to_amount_min"]) >= target.minimum_raw
    if quote.get("source_gas_estimate_verified"):
        balance, amount = int(base["raw_balance"]), int(quote["request"]["amount"])
        unit_price = current_usd / balance
        remaining = max(0, balance - amount - int(quote["source_gas_raw"]))
        remaining_usd = remaining * unit_price
        quote["source_gas_usd"] = str(int(quote["source_gas_raw"]) * unit_price)
        quote["unspent_source_reserve_usd"] = str(remaining_usd)
        quote["loss_pct"] = str(
            (original_usd - _decimal(quote["minimum_output_usd"]) - remaining_usd)
            / original_usd * 100
        )
    loss = Decimal(quote["loss_pct"])
    if not loss.is_finite():
        raise ValueError("Relay route loss must be finite")
    quote["within_loss_limit"] = loss <= loss_limit
    if "reason" not in quote:
        quote["reason"] = (
            "bitget_deposit_minimum_not_met" if not quote["deposit_minimum_met"]
            else "max_route_loss_exceeded" if not quote["within_loss_limit"]
            else "relay_route_ready"
        )
    return quote


def relay_gas_revalidation_needed(entry):
    """Identify old provider-hint gas refusals; the caller must also exclude source intents."""
    if (entry.get("routing_provider") != "relay" or entry.get("status") != "manual_review"
            or entry.get("reason") != "insufficient_source_gas"
            or entry.get("asset_id") != "native" or entry.get("decimals") != 18):
        return False
    evidence = [entry, entry.get("relay_selected"), entry.get("relay_probe"),
                *entry.get("relay_candidates", [])]
    for candidate in evidence:
        if not isinstance(candidate, dict):
            continue
        budget = candidate.get("gas_budget", {})
        if candidate.get("source_gas_estimate_verified") is True or (
            isinstance(budget, dict) and budget.get("required_fee_raw") is not None
            and budget.get("gas_limit") is not None and budget.get("fee_quote_method")
        ):
            return False
    return True


def quote_relay_entry(base, client, targets, *, loss_limit, quote_floor, deposit_address=None,
                       gas_estimator=None, original_source_usd=None):
    """Compare every destination using verified RPC fees and the original balance budget."""
    entry = {
        **base,
        "status": "manual_review",
        "routing_provider": "relay",
        "quote_status": "unavailable",
        "execution_supported": False,
        "reason": "relay_quote_unavailable",
        "relay_candidates": [],
        "search": {"complete": False, "errors": [], "pending": []},
    }
    try:
        if original_source_usd is not None:
            original_usd = _decimal(original_source_usd)
            if original_usd <= 0:
                raise ValueError("Relay original source valuation must be positive")
            entry["source_balance_usd"] = str(original_usd)
        if base["chain_id"] not in client.supported_chain_ids():
            entry["reason"] = "unsupported_by_relay"
            return entry
        if not targets:
            raise ValueError("No configured destination enabled for Bitget deposits")
        quotes = []
        for target in targets:
            try:
                first = client.quote(base, target, int(base["raw_balance"]),
                                     recipient=deposit_address)
                original_usd = _decimal(first["source_usd"])
                if original_usd <= 0:
                    raise ValueError("Relay source valuation is zero")
                entry.setdefault("source_balance_usd", str(original_usd))
                entry.setdefault("current_source_balance_usd", str(original_usd))
                original_usd = _decimal(entry["source_balance_usd"])
                current_usd = _decimal(entry["current_source_balance_usd"])
                quote = _native_quote(base, client, target, first, gas_estimator, deposit_address)
                quotes.append(_evaluate_candidate(base, target, quote, original_usd, current_usd,
                                                  loss_limit))
            except Exception as error:
                detail = _search_error(error, target)
                entry.setdefault("diagnostic", detail["diagnostic"])
                entry["search"]["errors"].append(detail)
                if detail["retry_at"] is not None:
                    entry["search"]["pending"].append(detail)
        entry["relay_candidates"] = quotes
        refused = sum(e["reason"] == "below_target_minimum"
                      for e in entry["search"]["errors"])
        entry["search"]["complete"] = len(quotes) + refused == len(targets)
        if not quotes and refused == len(targets):
            entry["reason"] = "below_target_minimum"
            return entry
        if quotes:
            entry["quote_status"] = "available"
        if not entry["search"]["complete"]:
            entry["reason"] = "relay_route_search_incomplete"
            return entry
        if quotes and all(_decimal(q["source_balance_usd"]) < quote_floor for q in quotes):
            entry.update(status="below_quote_floor", reason="below_quote_floor")
            return entry
        if any(not q["source_gas_estimate_verified"] for q in quotes):
            entry["reason"] = "relay_source_gas_unverified"
            return entry
        feasible = [q for q in quotes if q["deposit_minimum_met"] and q["gas_reserve_sufficient"]]
        if not feasible:
            entry["reason"] = (
                "insufficient_source_gas"
                if quotes and all(q["reason"] == "insufficient_source_gas" for q in quotes)
                else "relay_deposit_minimum_or_gas_reserve"
            )
            return entry
        best = min(feasible, key=lambda q: Decimal(q["loss_pct"]))
        entry["relay_selected"] = best
        entry["source_balance_usd"] = best["source_balance_usd"]
        entry["source_gas_reserve_raw"] = best["source_gas_reserve_raw"]
        entry["reason"] = "max_route_loss_exceeded"
        if best["within_loss_limit"]:
            if not deposit_address:
                raise ValueError("Relay execution requires a trusted Bitget recipient")
            verified_transaction(best, client.chain_catalog)
            entry.update(
                status="route_ready",
                reason="relay_route_ready",
                execution_supported=True,
                target=best["target"],
                settlement="bitget",
                deposit_address=deposit_address,
            )

        return entry
    except Exception as error:
        entry["diagnostic"] = exception_diagnostic(error)
        detail = _search_error(error)
        entry["search"]["errors"].append(detail)
        if detail["retry_at"] is not None:
            entry["search"]["pending"].append(detail)
        entry["reason"] = getattr(error, "reason", "relay_quote_failed")
        return entry
