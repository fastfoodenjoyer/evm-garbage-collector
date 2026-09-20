"""Read-only LI.FI route quotations.

The client requests route metadata only. It does not request transaction calldata,
sign messages, or submit transactions.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol


class _Response(Protocol):
    def raise_for_status(self) -> None: ...

    def json(self) -> object: ...


class _HttpClient(Protocol):
    def post(self, url: str, *, json: dict, headers: dict, timeout: float) -> _Response: ...


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


@dataclass(frozen=True, slots=True)
class LifiRoute:
    route_id: str
    from_amount: int
    to_amount: int
    to_amount_min: int
    gas_costs: tuple[LifiGasCost, ...]
    tools: tuple[str, ...]


class LifiClient:
    """Request cheapest LI.FI routes through the current public API."""

    endpoint = "https://li.quest/v1/advanced/routes"

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
        response = self.http_client.post(
            self.endpoint,
            json=payload,
            headers={
                "referer": "https://jumper.exchange/",
                "origin": "https://jumper.exchange",
                "x-lifi-integrator": "jumper.exchange",
            },
            timeout=30,
        )
        response.raise_for_status()
        body = response.json()
        if not isinstance(body, dict) or not isinstance(body.get("routes"), list):
            raise ValueError("LI.FI response does not contain routes")
        return tuple(_route_from_dict(item) for item in body["routes"])


def _route_from_dict(value: object) -> LifiRoute:
    if not isinstance(value, dict):
        raise ValueError("LI.FI route is invalid")
    try:
        gas_costs = tuple(
            LifiGasCost(amount=int(cost["amount"]), amount_usd=cost.get("amountUSD"))
            for cost in value.get("gasCosts", [])
            if isinstance(cost, dict)
        )
        tools = tuple(
            step["tool"]
            for step in value.get("steps", [])
            if isinstance(step, dict) and isinstance(step.get("tool"), str)
        )
        return LifiRoute(
            route_id=str(value["id"]),
            from_amount=int(value["fromAmount"]),
            to_amount=int(value["toAmount"]),
            to_amount_min=int(value["toAmountMin"]),
            gas_costs=gas_costs,
            tools=tools,
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise ValueError("LI.FI route is malformed") from exc
