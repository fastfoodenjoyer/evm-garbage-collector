"""Relay quotes, exact asset validation and durable request accounting."""

from __future__ import annotations

import re
from datetime import UTC, datetime
from decimal import ROUND_CEILING, Decimal
from threading import Lock
from uuid import uuid4

from .diagnostics import exception_diagnostic
from .lifi_quota import LifiQuotaStore, LifiRequestLimit
from .quote_jobs import JobInProgress, QuoteJobs, quote_job_key
from .relay_protocol import verified_transaction

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


def quote_relay_entry(base, client, targets, *, loss_limit, quote_floor, deposit_address=None):
    """Compare all configured destinations, preserving failures and native gas reserve."""
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
        if base["chain_id"] not in client.supported_chain_ids():
            entry["reason"] = "unsupported_by_relay"
            return entry
        if not targets:
            raise ValueError("No configured destination enabled for Bitget deposits")
        amount = int(base["raw_balance"])
        first = None
        probe_errors = []
        probe_target = None
        for target in targets:
            try:
                first = client.quote(base, target, amount, recipient=deposit_address)
                probe_target = target
                break
            except Exception as error:
                probe_errors.append(
                    {
                        "destination_chain_id": target.chain_id,
                        "diagnostic": exception_diagnostic(error),
                        "retry_at": getattr(error, "retry_at", None),
                    }
                )
        if first is None:
            entry["search"]["errors"] = probe_errors
            entry["search"]["pending"] = [e for e in probe_errors if e["retry_at"] is not None]
            entry["reason"] = "relay_route_search_incomplete"
            return entry
        entry["relay_probe_errors"] = probe_errors
        entry["source_balance_usd"] = first["source_usd"]
        if _decimal(first["source_usd"]) < quote_floor:
            entry.update(status="below_quote_floor", reason="below_quote_floor")
            return entry
        if base["asset_id"] == "native":
            reserve = int(
                (Decimal(first["source_gas_raw"]) * Decimal("1.2")).to_integral_value(
                    rounding=ROUND_CEILING
                )
            )
            entry["source_gas_reserve_raw"] = str(reserve)
            amount -= reserve
            if amount <= 0:
                entry.update(reason="insufficient_source_gas", relay_probe=first)
                return entry
        quotes = []
        for target in targets:
            try:
                quote = (
                    first
                    if amount == int(base["raw_balance"]) and target == probe_target
                    else client.quote(base, target, amount, recipient=deposit_address)
                )
                quote["target"] = {
                    "coin": target.coin,
                    "chain": target.chain,
                    "chain_id": target.chain_id,
                    "asset_id": target.asset_id,
                    "decimals": target.decimals,
                    "minimum_raw": str(target.minimum_raw),
                }
                quote["deposit_minimum_met"] = int(quote["to_amount_min"]) >= target.minimum_raw
                quote["gas_reserve_sufficient"] = base["asset_id"] != "native" or int(
                    quote["source_gas_raw"]
                ) <= int(entry["source_gas_reserve_raw"])
                if base["asset_id"] == "native":
                    # Gas comes from the original balance; unspent reserve is retained.
                    remaining_raw = max(
                        0, int(entry["source_gas_reserve_raw"]) - int(quote["source_gas_raw"])
                    )
                    unit_price = _decimal(quote["source_usd"]) / Decimal(amount)
                    remaining_usd = Decimal(remaining_raw) * unit_price
                    source_usd = _decimal(entry["source_balance_usd"])
                    quote["unspent_source_reserve_usd"] = str(remaining_usd)
                    quote["loss_pct"] = str(
                        (source_usd - _decimal(quote["minimum_output_usd"]) - remaining_usd)
                        / source_usd
                        * 100
                    )
                quote["within_loss_limit"] = Decimal(quote["loss_pct"]) <= loss_limit
                quotes.append(quote)
            except Exception as error:
                entry["search"]["errors"].append(
                    {
                        "destination_chain_id": target.chain_id,
                        "diagnostic": exception_diagnostic(error),
                    }
                )
                retry_at = getattr(error, "retry_at", None)
                if retry_at is not None:
                    entry["search"]["pending"].append(
                        {
                            "reason": getattr(error, "reason", "relay_quote_failed"),
                            "retry_at": retry_at,
                            "diagnostic": exception_diagnostic(error),
                        }
                    )
        entry["relay_candidates"] = quotes
        entry["search"]["complete"] = len(quotes) == len(targets)
        if not entry["search"]["complete"]:
            entry["reason"] = "relay_route_search_incomplete"
            return entry
        entry["quote_status"] = "available"
        feasible = [q for q in quotes if q["deposit_minimum_met"] and q["gas_reserve_sufficient"]]
        if not feasible:
            entry["reason"] = "relay_deposit_minimum_or_gas_reserve"
            return entry
        best = min(feasible, key=lambda q: Decimal(q["loss_pct"]))
        entry["relay_selected"] = best
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
        entry["search"]["errors"].append(entry["diagnostic"])
        entry["reason"] = getattr(error, "reason", "relay_quote_failed")
        return entry
