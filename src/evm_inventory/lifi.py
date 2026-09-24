"""Read-only LI.FI route quotations.

The client requests route metadata only. It does not request transaction calldata,
sign messages, or submit transactions.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from hashlib import sha256
from typing import Any, Protocol

from .models import AssetIdentity


class _Response(Protocol):
    def raise_for_status(self) -> None: ...

    def json(self) -> object: ...


class _HttpClient(Protocol):
    def post(self, url: str, *, json: dict, headers: dict, timeout: float) -> _Response: ...

    def get(
        self, url: str, *, params: dict, headers: dict, timeout: float
    ) -> _Response: ...


class LifiError(ValueError):
    """Raised when LI.FI returns malformed or internally inconsistent evidence."""


@dataclass(frozen=True, slots=True)
class LifiRouteRequest:
    from_chain_id: int
    to_chain_id: int
    from_token_address: str
    to_token_address: str
    from_amount: str
    from_address: str
    to_address: str
    slippage: float = 0.005


@dataclass(frozen=True, slots=True)
class LifiGasCost:
    amount: int
    amount_usd: str | None
    token: AssetIdentity | None = None
    price_usd: str | None = None
    price_timestamp: str | None = None


@dataclass(frozen=True, slots=True)
class LifiFeeCost:
    amount: int
    token: AssetIdentity
    amount_usd: str | None
    price_usd: str | None
    price_timestamp: str | None


@dataclass(frozen=True, slots=True)
class LifiBridgeAction:
    provider: str
    source: AssetIdentity
    destination: AssetIdentity
    from_amount: int
    recipient: str | None
    raw_step: dict[str, Any]


@dataclass(frozen=True, slots=True)
class LifiStepEvidence:
    """Normalized identity and amount data for one executable route step."""

    step_type: str | None
    provider: str | None
    source: AssetIdentity | None
    destination: AssetIdentity | None
    from_amount: int | None
    to_amount: int | None
    recipient: str | None
    payload_sha256: str
    source_price_usd: str | None = None
    destination_price_usd: str | None = None
    source_price_timestamp: str | None = None
    destination_price_timestamp: str | None = None
    is_substep: bool = False
    normalization_error: str | None = None


@dataclass(frozen=True, slots=True)
class LifiPriceEvidence:
    """A quote price tied to an exact asset identity and observation time."""

    asset: AssetIdentity
    price_usd: str | None
    timestamp: str

    def __post_init__(self) -> None:
        if not isinstance(self.asset, AssetIdentity):
            raise ValueError("LI.FI price asset is invalid")
        _parse_timestamp(self.timestamp)
        if self.price_usd is not None and not isinstance(self.price_usd, str):
            raise ValueError("LI.FI price evidence must be a string")


@dataclass(frozen=True, slots=True)
class LifiRouteEvidence:
    """Immutable quote evidence used to validate a route immediately before use."""

    route_id: str
    steps: tuple[LifiStepEvidence, ...]
    source: AssetIdentity
    destination: AssetIdentity
    input_amount: int
    output_amount: int
    min_output_amount: int
    final_recipient: str | None
    quote_timestamp: str
    prices: tuple[LifiPriceEvidence, ...]
    payload_sha256: str
    canonical_payload_json: str
    costs_complete: bool = True
    costs_incomplete_reason: str | None = None

    def __post_init__(self) -> None:
        _parse_timestamp(self.quote_timestamp)


@dataclass(frozen=True, slots=True)
class LifiRoute:
    route_id: str
    from_amount: int
    to_amount: int
    to_amount_min: int
    gas_costs: tuple[LifiGasCost, ...]
    tools: tuple[str, ...]
    first_step: dict[str, Any]
    fee_costs: tuple[LifiFeeCost, ...] = ()
    action: LifiBridgeAction | None = None
    raw_steps: tuple[dict[str, Any], ...] = ()
    source_price_usd: str | None = None
    destination_price_usd: str | None = None
    price_timestamp: str | None = None
    evidence: LifiRouteEvidence | None = None


@dataclass(frozen=True, slots=True)
class TransactionRequest:
    chain_id: int
    to: str
    data: str
    value: int
    gas_limit: int
    gas_price_wei: int | None
    max_fee_per_gas_wei: int | None = None
    max_priority_fee_per_gas_wei: int | None = None
    additional_fee_wei: int = 0
    fee_quote_method: str | None = None
    fee_quote_fallback_reason: str | None = None
    max_total_fee_cap_wei: int | None = None
    gas_estimate: int | None = None
    fee_quote_block_number: int | None = None
    base_fee_per_gas_wei: int | None = None

    @property
    def maximum_fee_per_gas_wei(self) -> int:
        if self.max_fee_per_gas_wei is not None:
            return self.max_fee_per_gas_wei
        if self.gas_price_wei is None:
            raise ValueError("transaction fee quote is missing")
        return self.gas_price_wei

    @property
    def max_total_fee_wei(self) -> int:
        return self.gas_limit * self.maximum_fee_per_gas_wei + self.additional_fee_wei


class LifiClient:
    """Request cheapest LI.FI routes through the current public API."""

    endpoint = "https://api.jumper.xyz/pipeline/v1/advanced/routes"
    token_endpoint = "https://li.quest/v1/token"
    status_endpoint = "https://li.quest/v1/status"

    def __init__(self, http_client: _HttpClient):
        self.http_client = http_client

    def routes(self, request: LifiRouteRequest) -> tuple[LifiRoute, ...]:
        payload = {
            "fromAddress": request.from_address,
            "toAddress": request.to_address,
            "fromAmount": request.from_amount,
            "fromChainId": request.from_chain_id,
            "fromTokenAddress": request.from_token_address,
            "toChainId": request.to_chain_id,
            "toTokenAddress": request.to_token_address,
            "options": {
                "integrator": "jumper.exchange",
                "order": "CHEAPEST",
                "slippage": request.slippage,
                "maxPriceImpact": 0.4,
                "allowSwitchChain": True,
            },
        }
        response = self._post(payload)
        body = response.json()
        if not isinstance(body, dict) or not isinstance(body.get("routes"), list):
            raise LifiError("LI.FI response does not contain routes")
        retrieved_at = _utc_timestamp()
        routes = tuple(
            _route_from_dict(item, quote_timestamp=retrieved_at)
            for item in body["routes"]
        )
        return tuple(self._price_missing_gas_tokens(route) for route in routes)

    def token_price(self, asset: AssetIdentity) -> LifiPriceEvidence:
        """Fetch the USD price for one exact LI.FI token identity, without side effects."""

        if not isinstance(asset, AssetIdentity):
            raise ValueError("LI.FI token price identity is invalid")
        token = (
            "0x0000000000000000000000000000000000000000"
            if asset.is_native
            else asset.contract_address
        )
        response = self.http_client.get(
            self.token_endpoint,
            params={"chain": asset.chain_id, "token": token},
            headers={"x-lifi-integrator": "jumper.exchange"},
            timeout=30,
        )
        response.raise_for_status()
        body = response.json()
        if not isinstance(body, dict):
            raise ValueError("LI.FI token price response is malformed")
        try:
            response_address = _address(body["address"])
            response_chain = _required_integer(body["chainId"])
            response_decimals = _required_integer(body["decimals"])
            price = _optional_string(body["priceUSD"])
        except (KeyError, TypeError, ValueError) as exc:
            raise ValueError("LI.FI token price response is malformed") from exc
        if price is None:
            raise ValueError("LI.FI token price response is missing priceUSD")
        if (
            response_chain != asset.chain_id
            or response_address != token
            or response_decimals != asset.decimals
        ):
            raise ValueError("LI.FI token price identity does not match requested asset")
        try:
            parsed_price = Decimal(price)
        except (InvalidOperation, TypeError, ValueError) as exc:
            raise ValueError("LI.FI token price response has an invalid priceUSD") from exc
        if not parsed_price.is_finite() or parsed_price <= 0:
            raise ValueError("LI.FI token price response has an invalid priceUSD")
        return LifiPriceEvidence(asset, price, _utc_timestamp())

    def transaction_status(
        self,
        *,
        tx_hash: str,
        from_chain_id: int,
        to_chain_id: int,
        bridge: str,
    ) -> dict[str, Any]:
        """Read the provider-correlated status for one cross-chain transaction."""

        if not isinstance(tx_hash, str) or not tx_hash:
            raise ValueError("LI.FI transaction hash is invalid")
        if (
            isinstance(from_chain_id, bool)
            or not isinstance(from_chain_id, int)
            or from_chain_id <= 0
            or isinstance(to_chain_id, bool)
            or not isinstance(to_chain_id, int)
            or to_chain_id <= 0
        ):
            raise ValueError("LI.FI status chain identity is invalid")
        if not isinstance(bridge, str) or not bridge.strip():
            raise ValueError("LI.FI status bridge identity is invalid")
        response = self.http_client.get(
            self.status_endpoint,
            params={
                "txHash": tx_hash,
                "fromChain": from_chain_id,
                "toChain": to_chain_id,
                "bridge": bridge,
            },
            headers={"x-lifi-integrator": "jumper.exchange"},
            timeout=30,
        )
        response.raise_for_status()
        body = response.json()
        if not isinstance(body, dict) or not isinstance(body.get("status"), str):
            raise ValueError("LI.FI transaction status response is malformed")
        if body["status"].upper() not in {
            "PENDING",
            "DONE",
            "NOT_FOUND",
            "INVALID",
            "FAILED",
        }:
            raise ValueError("LI.FI transaction status response is unknown")
        return body

    def _price_missing_gas_tokens(self, route: LifiRoute) -> LifiRoute:
        if route.evidence is None:
            return route
        prices = list(route.evidence.prices)
        gas_costs: list[LifiGasCost] = []
        for gas in route.gas_costs:
            if gas.token is None or gas.price_usd:
                gas_costs.append(gas)
                continue
            quote_price = next(
                (
                    price
                    for price in prices
                    if price.asset == gas.token and price.price_usd is not None
                ),
                None,
            )
            if quote_price is None:
                quote_price = self.token_price(gas.token)
                prices.append(quote_price)
            gas_costs.append(
                replace(
                    gas,
                    price_usd=quote_price.price_usd,
                    price_timestamp=quote_price.timestamp,
                )
            )
        evidence = replace(route.evidence, prices=tuple(prices))
        return replace(route, gas_costs=tuple(gas_costs), evidence=evidence)

    def _post(self, payload: dict) -> _Response:
        headers = {
            "referer": "https://jumper.exchange/",
            "origin": "https://jumper.exchange",
            "x-lifi-integrator": "jumper.exchange",
        }
        for attempt in range(3):
            try:
                response = self.http_client.post(
                    self.endpoint, json=payload, headers=headers, timeout=30
                )
                response.raise_for_status()
                return response
            except Exception as exc:
                status = getattr(getattr(exc, "response", None), "status_code", None)
                if status not in {429, 500, 502, 503, 504} or attempt == 2:
                    raise
                time.sleep(0.5 * (2**attempt))
        raise AssertionError("unreachable")

    def step_transaction(self, step: dict[str, Any]) -> TransactionRequest:
        """Build unsigned calldata for one quoted step; no transaction is signed or sent."""

        response = self._post_step(step)
        body = response.json()
        if not isinstance(body, dict) or not isinstance(body.get("transactionRequest"), dict):
            raise ValueError("Jumper response does not contain a transaction request")
        transaction = body["transactionRequest"]
        try:
            return TransactionRequest(
                chain_id=_required_integer(transaction["chainId"], "chain ID"),
                to=_address(transaction["to"]),
                data=_hex_data(transaction["data"]),
                value=int(str(transaction["value"]), 0),
                gas_limit=int(str(transaction["gasLimit"]), 0),
                gas_price_wei=int(str(transaction["gasPrice"]), 0),
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise ValueError("Jumper transaction request is malformed") from exc

    def _post_step(self, step: dict[str, Any]) -> _Response:
        headers = {
            "referer": "https://jumper.exchange/",
            "origin": "https://jumper.exchange",
            "x-lifi-integrator": "jumper.exchange",
        }
        for attempt in range(3):
            try:
                response = self.http_client.post(
                    self.endpoint.rsplit("/", 1)[0] + "/stepTransaction",
                    json=step,
                    headers=headers,
                    timeout=30,
                )
                response.raise_for_status()
                return response
            except Exception as exc:
                status = getattr(getattr(exc, "response", None), "status_code", None)
                if status not in {429, 500, 502, 503, 504} or attempt == 2:
                    raise
                time.sleep(0.5 * (2**attempt))
        raise AssertionError("unreachable")


def _route_from_dict(
    value: object, *, quote_timestamp: str | None = None
) -> LifiRoute:
    if not isinstance(value, dict):
        raise LifiError("LI.FI route is invalid")
    try:
        steps = value.get("steps", [])
        if (
            not isinstance(steps, list)
            or not steps
            or any(not isinstance(step, dict) for step in steps)
        ):
            raise ValueError("LI.FI route has no executable step")
        first_step = steps[0]
        try:
            action = _bridge_action_from_step(first_step)
        except ValueError:
            action = None
        valuation_step_gas, valuation_step_fees = _step_estimate_costs(
            steps, include_nested=False
        )
        evidence_step_gas, evidence_step_fees = _step_estimate_costs(
            steps, include_nested=True
        )
        route_gas_costs = _gas_costs(value["gasCosts"]) if "gasCosts" in value else ()
        route_fee_costs = _fee_costs(value["feeCosts"]) if "feeCosts" in value else ()
        gas_costs = route_gas_costs or valuation_step_gas
        fee_costs = route_fee_costs or valuation_step_fees
        route_id = value["id"]
        if not isinstance(route_id, str) or not route_id.strip():
            raise ValueError("LI.FI route ID is required")
        from_amount = _required_integer(value["fromAmount"], "route amount")
        to_amount = _required_nonnegative_integer(value["toAmount"], "route output amount")
        to_amount_min = _required_nonnegative_integer(
            value["toAmountMin"], "route minimum output"
        )
        if to_amount_min > to_amount:
            raise LifiError(
                "LI.FI route minimum output cannot exceed quoted output"
            )
        nested_gas_costs, nested_fee_costs = _nested_step_estimate_costs(steps)
        incomplete_cost_reasons = []
        if nested_gas_costs and not route_gas_costs:
            incomplete_cost_reasons.append(
                "nested gas costs require a route-level aggregate"
            )
        if nested_fee_costs and not route_fee_costs:
            incomplete_cost_reasons.append(
                "nested fee costs require a route-level aggregate"
            )
        snapshot_time = quote_timestamp or _utc_timestamp()
        steps_evidence = _normalize_steps(steps)
        route_steps = tuple(step for step in steps_evidence if not step.is_substep)
        normalized_steps = tuple(step for step in steps_evidence if step.source is not None)
        if not route_steps or any(step.normalization_error for step in route_steps):
            detail = next(
                (
                    step.normalization_error
                    for step in route_steps
                    if step.normalization_error
                ),
                "step is malformed",
            )
            raise LifiError(
                f"LI.FI route has an unnormalizable executable step: {detail}"
            )
        top_level_steps = tuple(step for step in normalized_steps if not step.is_substep)
        if not top_level_steps or not top_level_steps[-1].destination:
            raise ValueError("LI.FI route endpoints are missing")
        gas_costs = tuple(
            replace(cost, price_timestamp=cost.price_timestamp or snapshot_time)
            for cost in gas_costs
        )
        fee_costs = tuple(
            replace(cost, price_timestamp=cost.price_timestamp or snapshot_time)
            for cost in fee_costs
        )
        price_timestamp = _price_timestamp(value) or snapshot_time
        canonical_payload = _canonical_payload_json(value)
        quote_prices = _route_prices(
            value,
            normalized_steps,
            top_level_steps,
            (*evidence_step_gas, *route_gas_costs),
            (*evidence_step_fees, *route_fee_costs),
            quote_timestamp=snapshot_time,
        )
        evidence = LifiRouteEvidence(
            route_id=route_id,
            steps=steps_evidence,
            source=top_level_steps[0].source,
            destination=top_level_steps[-1].destination,
            input_amount=from_amount,
            output_amount=to_amount,
            min_output_amount=to_amount_min,
            final_recipient=top_level_steps[-1].recipient,
            quote_timestamp=snapshot_time,
            prices=quote_prices,
            payload_sha256=sha256(canonical_payload.encode("utf-8")).hexdigest(),
            canonical_payload_json=canonical_payload,
            costs_complete=not incomplete_cost_reasons,
            costs_incomplete_reason="; ".join(incomplete_cost_reasons) or None,
        )
        return LifiRoute(
            route_id=route_id,
            from_amount=from_amount,
            to_amount=to_amount,
            to_amount_min=to_amount_min,
            gas_costs=gas_costs,
            fee_costs=fee_costs,
            tools=tuple(step["tool"] for step in steps if isinstance(step.get("tool"), str)),
            first_step=first_step,
            action=action,
            raw_steps=tuple(steps),
            source_price_usd=_optional_string(value.get("fromTokenPriceUSD")),
            destination_price_usd=_optional_string(value.get("toTokenPriceUSD")),
            price_timestamp=price_timestamp,
            evidence=evidence,
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise LifiError(f"LI.FI route is malformed: {exc}") from exc


def validate_bridge_route(
    route: LifiRoute,
    *,
    expected_source: AssetIdentity,
    expected_destination: AssetIdentity,
    recipient: str,
    input_amount: int,
    route_id: str | None = None,
) -> LifiRoute:
    """Validate quote evidence against the exact source, target, amount, and recipient."""

    if not isinstance(route, LifiRoute):
        raise ValueError("LI.FI route is invalid")
    if route_id is not None and route.route_id != route_id:
        raise ValueError("LI.FI route ID does not match persisted route")
    _validate_route_evidence_integrity(route)
    evidence = route.evidence
    if evidence is None or not evidence.steps:
        raise ValueError("LI.FI route evidence is missing")
    if any(step.normalization_error for step in evidence.steps):
        if any(step.is_substep and step.normalization_error for step in evidence.steps):
            raise ValueError("LI.FI route contains unnormalizable substeps")
        raise ValueError("LI.FI route contains an unnormalizable executable step")
    top_level_steps = tuple(step for step in evidence.steps if not step.is_substep)
    if not top_level_steps:
        raise ValueError("LI.FI route has no top-level executable steps")
    _validate_nested_step_tree(json.loads(evidence.canonical_payload_json)["steps"])
    for previous, current in zip(top_level_steps, top_level_steps[1:], strict=False):
        if previous.destination != current.source:
            raise ValueError("LI.FI route step identity continuity does not match")
        if previous.to_amount is None or current.from_amount != previous.to_amount:
            raise ValueError("LI.FI route step amount continuity does not match")
    final_step = top_level_steps[-1]
    if final_step.destination != evidence.destination:
        raise ValueError("LI.FI route final endpoint does not match top-level step")
    if final_step.recipient != evidence.final_recipient:
        raise ValueError("LI.FI route final recipient does not match top-level step")
    if final_step.to_amount is not None and final_step.to_amount != evidence.output_amount:
        raise ValueError("LI.FI route output amount does not match final top-level step")
    if (
        evidence.source != expected_source
        or evidence.destination != expected_destination
    ):
        raise ValueError(
            "LI.FI route source identity or destination identity does not match expectation"
        )
    if (
        evidence.input_amount != input_amount
        or route.from_amount != input_amount
        or top_level_steps[0].from_amount != input_amount
    ):
        raise ValueError("LI.FI route amount does not match persisted input amount")
    if evidence.final_recipient is None:
        raise ValueError("LI.FI route final transfer recipient is missing")
    if evidence.final_recipient != _address(recipient):
        raise ValueError("LI.FI route final transfer recipient does not match recipient")
    return route


def _validate_route_evidence_integrity(route: LifiRoute) -> None:
    evidence = route.evidence
    if not isinstance(evidence, LifiRouteEvidence):
        raise LifiError("LI.FI route evidence integrity failed: evidence is missing")
    try:
        payload = json.loads(evidence.canonical_payload_json)
        if not isinstance(payload, dict):
            raise ValueError("canonical payload is not an object")
        canonical = _canonical_payload_json(payload)
        if canonical != evidence.canonical_payload_json:
            raise ValueError("canonical payload encoding changed")
        if sha256(canonical.encode("utf-8")).hexdigest() != evidence.payload_sha256:
            raise ValueError("payload fingerprint does not match canonical payload")
        expected = _route_from_dict(payload, quote_timestamp=evidence.quote_timestamp)
    except (json.JSONDecodeError, KeyError, TypeError, ValueError) as exc:
        raise LifiError(f"LI.FI route evidence integrity failed: {exc}") from exc

    expected_evidence = expected.evidence
    if expected_evidence is None:
        raise LifiError("LI.FI route evidence integrity failed: parsed evidence is missing")
    if (
        route.route_id != expected.route_id
        or route.from_amount != expected.from_amount
        or route.to_amount != expected.to_amount
        or route.to_amount_min != expected.to_amount_min
        or route.first_step != expected.first_step
        or route.raw_steps != expected.raw_steps
        or route.tools != expected.tools
        or route.action != expected.action
        or route.fee_costs != expected.fee_costs
        or route.source_price_usd != expected.source_price_usd
        or route.destination_price_usd != expected.destination_price_usd
        or route.price_timestamp != expected.price_timestamp
    ):
        raise LifiError("LI.FI route evidence integrity failed: route fields changed")
    if (
        evidence.route_id != expected_evidence.route_id
        or evidence.steps != expected_evidence.steps
        or evidence.source != expected_evidence.source
        or evidence.destination != expected_evidence.destination
        or evidence.input_amount != expected_evidence.input_amount
        or evidence.output_amount != expected_evidence.output_amount
        or evidence.min_output_amount != expected_evidence.min_output_amount
        or evidence.final_recipient != expected_evidence.final_recipient
        or evidence.payload_sha256 != expected_evidence.payload_sha256
        or evidence.canonical_payload_json != expected_evidence.canonical_payload_json
        or evidence.costs_complete != expected_evidence.costs_complete
        or evidence.costs_incomplete_reason
        != expected_evidence.costs_incomplete_reason
    ):
        raise LifiError("LI.FI route evidence integrity failed: normalized evidence changed")
    if len(route.gas_costs) != len(expected.gas_costs):
        raise LifiError("LI.FI route evidence integrity failed: gas costs changed")
    expected_prices = expected_evidence.prices
    if evidence.prices[: len(expected_prices)] != expected_prices:
        raise LifiError("LI.FI route evidence integrity failed: quote prices changed")
    for extra_price in evidence.prices[len(expected_prices) :]:
        if not any(
            expected_cost.token == extra_price.asset
            and expected_cost.price_usd is None
            and actual_cost.price_usd == extra_price.price_usd
            and actual_cost.price_timestamp == extra_price.timestamp
            for expected_cost, actual_cost in zip(
                expected.gas_costs, route.gas_costs, strict=True
            )
        ):
            raise LifiError("LI.FI route evidence integrity failed: unexpected price added")
    for expected_cost, actual_cost in zip(expected.gas_costs, route.gas_costs, strict=True):
        if (
            actual_cost.amount != expected_cost.amount
            or actual_cost.amount_usd != expected_cost.amount_usd
            or actual_cost.token != expected_cost.token
        ):
            raise LifiError("LI.FI route evidence integrity failed: gas cost identity changed")
        if expected_cost.price_usd is not None:
            if (
                actual_cost.price_usd != expected_cost.price_usd
                or actual_cost.price_timestamp != expected_cost.price_timestamp
            ):
                raise LifiError("LI.FI route evidence integrity failed: gas quote changed")
        elif actual_cost.price_usd is None:
            if actual_cost.price_timestamp != expected_cost.price_timestamp:
                raise LifiError("LI.FI route evidence integrity failed: gas timestamp changed")
        elif not any(
            price.asset == actual_cost.token
            and price.price_usd == actual_cost.price_usd
            and price.timestamp == actual_cost.price_timestamp
            for price in evidence.prices
        ):
            raise LifiError("LI.FI route evidence integrity failed: gas price is unproven")


def _bridge_action_from_step(step: dict[str, Any]) -> LifiBridgeAction:
    action = step.get("action")
    if not isinstance(action, dict):
        raise ValueError("LI.FI route action is malformed")
    provider = step.get("tool")
    if not isinstance(provider, str) or not provider.strip():
        raise ValueError("LI.FI route provider is missing")
    try:
        source = _identity_from_token(action["fromChainId"], action["fromToken"])
        destination = _identity_from_token(action["toChainId"], action["toToken"])
        return LifiBridgeAction(
            provider=provider.strip().lower(),
            source=source,
            destination=destination,
            from_amount=_required_integer(action["fromAmount"], "step amount"),
                recipient=(
                    _address(action["toAddress"])
                    if action.get("toAddress") is not None
                    else None
                ),
            raw_step=step,
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise ValueError("LI.FI route action is malformed") from exc


def _identity_from_token(chain_id: object, token: object) -> AssetIdentity:
    if not isinstance(token, dict):
        raise ValueError("LI.FI token is malformed")
    address = token.get("address")
    if isinstance(address, str) and address.lower() == "0x0000000000000000000000000000000000000000":
        address = "native"
    return AssetIdentity(_required_integer(chain_id, "chain ID"), address, token.get("decimals"))


def _gas_costs(value: object) -> tuple[LifiGasCost, ...]:
    if not isinstance(value, list):
        raise ValueError("LI.FI gas costs are malformed")
    costs: list[LifiGasCost] = []
    for cost in value:
        if not isinstance(cost, dict):
            raise ValueError("LI.FI gas cost is malformed")
        token = cost.get("token")
        if not isinstance(token, dict):
            raise ValueError("LI.FI gas cost token identity is missing")
        costs.append(
            LifiGasCost(
                amount=_required_nonnegative_integer(cost["amount"], "gas amount"),
                amount_usd=_optional_string(cost.get("amountUSD")),
                token=_identity_from_token(token.get("chainId"), token),
                price_usd=_optional_string(cost.get("priceUSD")),
                price_timestamp=_price_timestamp(cost),
            )
        )
    return tuple(costs)


def _fee_costs(value: object) -> tuple[LifiFeeCost, ...]:
    if not isinstance(value, list):
        raise ValueError("LI.FI fee costs are malformed")
    costs: list[LifiFeeCost] = []
    for cost in value:
        if not isinstance(cost, dict):
            raise ValueError("LI.FI fee cost is malformed")
        token = cost.get("token")
        if not isinstance(token, dict):
            raise LifiError("LI.FI fee cost token identity is malformed")
        costs.append(
            LifiFeeCost(
                amount=_required_nonnegative_integer(cost["amount"], "fee amount"),
                token=_identity_from_token(token.get("chainId"), token),
                amount_usd=_optional_string(cost.get("amountUSD")),
                price_usd=_optional_string(cost.get("priceUSD")),
                price_timestamp=_price_timestamp(cost),
            )
        )
    return tuple(costs)


def _optional_string(value: object) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise ValueError("LI.FI price value is malformed")
    return value


def _price_timestamp(cost: dict[str, Any]) -> str | None:
    value = cost.get("priceTimestamp", cost.get("timestamp"))
    return _optional_string(value)


def _normalize_steps(steps: list[dict[str, Any]]) -> tuple[LifiStepEvidence, ...]:
    normalized: list[LifiStepEvidence] = []

    def visit(step: object, *, is_substep: bool) -> None:
        if not isinstance(step, dict):
            normalized.append(_invalid_step(step, is_substep=is_substep))
            return
        try:
            action = step.get("action")
            if not isinstance(action, dict):
                raise ValueError("action is missing")
            source = _identity_from_token(action["fromChainId"], action["fromToken"])
            destination = _identity_from_token(action["toChainId"], action["toToken"])
            raw_amount = _required_nonnegative_integer(
                action["fromAmount"], "step input amount"
            )
            step_type = step.get("type")
            provider = step.get("tool")
            if not isinstance(step_type, str) or not step_type.strip():
                raise ValueError("step type is missing")
            if not isinstance(provider, str) or not provider.strip():
                raise ValueError("provider is missing")
            estimate = step.get("estimate", {})
            if not isinstance(estimate, dict):
                raise ValueError("estimate is malformed")
            output = estimate.get("toAmount")
            recipient = action.get("toAddress")
            normalized.append(
                LifiStepEvidence(
                    step_type=step_type.strip().lower(),
                    provider=provider.strip().lower(),
                    source=source,
                    destination=destination,
                    from_amount=raw_amount,
                    to_amount=(
                        _required_nonnegative_integer(output, "step output amount")
                        if output is not None
                        else None
                    ),
                    recipient=_address(recipient) if recipient is not None else None,
                    payload_sha256=_payload_sha256(step),
                    source_price_usd=_optional_string(
                        action["fromToken"].get("priceUSD")
                    ),
                    destination_price_usd=_optional_string(
                        action["toToken"].get("priceUSD")
                    ),
                    source_price_timestamp=_price_timestamp(action["fromToken"]),
                    destination_price_timestamp=_price_timestamp(action["toToken"]),
                    is_substep=is_substep,
                )
            )
        except (KeyError, TypeError, ValueError) as exc:
            normalized.append(_invalid_step(step, is_substep=is_substep, error=str(exc)))
        for key in ("includedSteps", "steps", "substeps"):
            nested = step.get(key)
            if nested is None:
                continue
            if not isinstance(nested, list):
                normalized.append(
                    _invalid_step(nested, is_substep=True, error="substeps are malformed")
                )
                continue
            for child in nested:
                visit(child, is_substep=True)

    for item in steps:
        visit(item, is_substep=False)
    return tuple(normalized)


def _invalid_step(
    value: object, *, is_substep: bool, error: str = "step is malformed"
) -> LifiStepEvidence:
    return LifiStepEvidence(
        step_type=None,
        provider=None,
        source=None,
        destination=None,
        from_amount=None,
        to_amount=None,
        recipient=None,
        payload_sha256=_payload_sha256(value),
        is_substep=is_substep,
        normalization_error=error,
    )


def _route_prices(
    value: dict[str, Any],
    steps: tuple[LifiStepEvidence, ...],
    top_level_steps: tuple[LifiStepEvidence, ...],
    gas_costs: tuple[LifiGasCost, ...],
    fee_costs: tuple[LifiFeeCost, ...],
    *,
    quote_timestamp: str,
) -> tuple[LifiPriceEvidence, ...]:
    prices: list[LifiPriceEvidence] = []
    for step in steps:
        if step.source is not None and step.source_price_usd is not None:
            prices.append(
                LifiPriceEvidence(
                    step.source,
                    step.source_price_usd,
                    step.source_price_timestamp or quote_timestamp,
                )
            )
        if step.destination is not None and step.destination_price_usd is not None:
            prices.append(
                LifiPriceEvidence(
                    step.destination,
                    step.destination_price_usd,
                    step.destination_price_timestamp or quote_timestamp,
                )
            )
    if top_level_steps[0].source is not None:
        prices.append(
            LifiPriceEvidence(
                top_level_steps[0].source,
                _optional_string(value.get("fromTokenPriceUSD")),
                _price_timestamp(value) or quote_timestamp,
            )
        )
    if top_level_steps[-1].destination is not None:
        prices.append(
            LifiPriceEvidence(
                top_level_steps[-1].destination,
                _optional_string(value.get("toTokenPriceUSD")),
                _price_timestamp(value) or quote_timestamp,
            )
        )
    for cost in (*gas_costs, *fee_costs):
        if cost.token is None:
            continue
        prices.append(
            LifiPriceEvidence(
                cost.token,
                cost.price_usd,
                cost.price_timestamp or quote_timestamp,
            )
        )
    return tuple(prices)


def _step_estimate_costs(
    steps: list[dict[str, Any]],
    *,
    include_nested: bool,
) -> tuple[tuple[LifiGasCost, ...], tuple[LifiFeeCost, ...]]:
    gas_costs: list[LifiGasCost] = []
    fee_costs: list[LifiFeeCost] = []
    for step in _walk_steps(steps, include_nested=include_nested):
        estimate = step.get("estimate", {})
        if not isinstance(estimate, dict):
            raise ValueError("LI.FI route estimate is malformed")
        gas_costs.extend(_gas_costs(estimate.get("gasCosts", [])))
        fee_costs.extend(_fee_costs(estimate.get("feeCosts", [])))
    return tuple(gas_costs), tuple(fee_costs)


def _nested_step_estimate_costs(
    steps: list[dict[str, Any]],
) -> tuple[tuple[LifiGasCost, ...], tuple[LifiFeeCost, ...]]:
    root_ids = {id(step) for step in steps}
    gas_costs: list[LifiGasCost] = []
    fee_costs: list[LifiFeeCost] = []
    for step in _walk_steps(steps, include_nested=True):
        if id(step) in root_ids:
            continue
        estimate = step.get("estimate", {})
        if not isinstance(estimate, dict):
            raise ValueError("LI.FI nested step estimate is malformed")
        gas_costs.extend(_gas_costs(estimate.get("gasCosts", [])))
        fee_costs.extend(_fee_costs(estimate.get("feeCosts", [])))
    return tuple(gas_costs), tuple(fee_costs)


def _validate_nested_step_tree(steps: list[dict[str, Any]]) -> None:
    def facts(step: dict[str, Any]):
        action = step.get("action")
        estimate = step.get("estimate", {})
        if not isinstance(action, dict) or not isinstance(estimate, dict):
            raise ValueError("LI.FI nested step cannot be normalized")
        source = _identity_from_token(action["fromChainId"], action["fromToken"])
        destination = _identity_from_token(action["toChainId"], action["toToken"])
        input_amount = _required_nonnegative_integer(
            action["fromAmount"], "nested step input amount"
        )
        raw_output = estimate.get("toAmount")
        output_amount = (
            _required_nonnegative_integer(raw_output, "nested step output amount")
            if raw_output is not None
            else None
        )
        return source, destination, input_amount, output_amount

    def children_of(step: dict[str, Any]) -> list[dict[str, Any]]:
        child_groups = []
        for key in ("includedSteps", "steps", "substeps"):
            children = step.get(key)
            if children is None:
                continue
            if not isinstance(children, list):
                raise ValueError("LI.FI nested step list is malformed")
            if children:
                child_groups.append(children)
        if len(child_groups) > 1:
            raise ValueError("LI.FI nested step groups are ambiguous")
        children = child_groups[0] if child_groups else []
        if any(not isinstance(child, dict) for child in children):
            raise ValueError("LI.FI nested step cannot be normalized")
        return children

    def validate_parent(step: dict[str, Any]) -> None:
        parent_source, parent_destination, parent_input, parent_output = facts(step)
        children = children_of(step)
        if children:
            child_facts = [facts(child) for child in children]
            first = child_facts[0]
            if first[0] != parent_source or first[2] != parent_input:
                raise ValueError("LI.FI nested step input does not match parent")
            for previous, current in zip(child_facts, child_facts[1:], strict=False):
                if previous[1] != current[0]:
                    raise ValueError("LI.FI nested step identity continuity does not match")
                if previous[3] is None or current[2] != previous[3]:
                    raise ValueError("LI.FI nested step amount continuity does not match")
            last = child_facts[-1]
            if last[1] != parent_destination:
                raise ValueError("LI.FI nested step endpoint does not match parent")
            if parent_output is not None and last[3] != parent_output:
                raise ValueError("LI.FI nested step output does not match parent")
            for child in children:
                validate_parent(child)

    for step in steps:
        validate_parent(step)


def _walk_steps(steps: list[dict[str, Any]], *, include_nested: bool):
    for step in steps:
        yield step
        if include_nested:
            for key in ("includedSteps", "steps", "substeps"):
                nested = step.get(key)
                if isinstance(nested, list):
                    yield from _walk_steps(
                        [item for item in nested if isinstance(item, dict)],
                        include_nested=True,
                    )


def _payload_sha256(value: object) -> str:
    canonical = _canonical_payload_json(value)
    return sha256(canonical.encode("utf-8")).hexdigest()


def _canonical_payload_json(value: object) -> str:
    return json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False
    )


def _parse_timestamp(value: str) -> datetime:
    if not isinstance(value, str):
        raise ValueError("LI.FI price timestamp is missing")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError("LI.FI price timestamp is invalid") from exc
    if parsed.tzinfo is None:
        raise ValueError("LI.FI price timestamp is invalid")
    return parsed.astimezone(UTC)


def _required_integer(value: object, field: str = "integer") -> int:
    if isinstance(value, bool):
        raise LifiError(f"LI.FI {field} must be an integer")
    if isinstance(value, int):
        return value
    if isinstance(value, str):
        try:
            return int(value)
        except ValueError as exc:
            raise LifiError(f"LI.FI {field} must be an integer") from exc
    raise LifiError(f"LI.FI {field} must be an integer")


def _required_nonnegative_integer(value: object, field: str = "integer") -> int:
    parsed = _required_integer(value, field)
    if parsed < 0:
        raise LifiError(f"LI.FI {field} must be non-negative")
    return parsed


def _utc_timestamp() -> str:
    return datetime.now(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _address(value: object) -> str:
    if not isinstance(value, str) or len(value) != 42 or not value.startswith("0x"):
        raise ValueError("invalid address")
    int(value[2:], 16)
    return value.lower()


def _hex_data(value: object) -> str:
    if not isinstance(value, str) or not value.startswith("0x"):
        raise ValueError("invalid calldata")
    int(value[2:] or "0", 16)
    return value.lower()
