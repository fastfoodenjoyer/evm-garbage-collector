"""Read-only LI.FI route quotations.

The client requests route metadata only. It does not request transaction calldata,
sign messages, or submit transactions.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol


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
    first_step: dict[str, Any]


@dataclass(frozen=True, slots=True)
class TransactionRequest:
    chain_id: int
    to: str
    data: str
    value: int
    gas_limit: int
    gas_price_wei: int


class LifiClient:
    """Request cheapest LI.FI routes through the current public API."""

    endpoint = "https://api.jumper.xyz/pipeline/v1/advanced/routes"

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

    def step_transaction(self, step: dict[str, Any]) -> TransactionRequest:
        """Build unsigned calldata for one quoted step; no transaction is signed or sent."""

        response = self.http_client.post(
            self.endpoint.rsplit("/", 1)[0] + "/stepTransaction",
            json=step,
            headers={
                "referer": "https://jumper.exchange/",
                "origin": "https://jumper.exchange",
                "x-lifi-integrator": "jumper.exchange",
            },
            timeout=30,
        )
        response.raise_for_status()
        body = response.json()
        if not isinstance(body, dict) or not isinstance(body.get("transactionRequest"), dict):
            raise ValueError("Jumper response does not contain a transaction request")
        transaction = body["transactionRequest"]
        try:
            return TransactionRequest(
                chain_id=int(transaction["chainId"]),
                to=_address(transaction["to"]),
                data=_hex_data(transaction["data"]),
                value=int(str(transaction["value"]), 0),
                gas_limit=int(str(transaction["gasLimit"]), 0),
                gas_price_wei=int(str(transaction["gasPrice"]), 0),
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise ValueError("Jumper transaction request is malformed") from exc


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
        steps = value.get("steps", [])
        if not isinstance(steps, list) or not steps or not isinstance(steps[0], dict):
            raise ValueError("LI.FI route has no executable step")
        return LifiRoute(
            route_id=str(value["id"]),
            from_amount=int(value["fromAmount"]),
            to_amount=int(value["toAmount"]),
            to_amount_min=int(value["toAmountMin"]),
            gas_costs=gas_costs,
            tools=tools,
            first_step=steps[0],
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise ValueError("LI.FI route is malformed") from exc


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
