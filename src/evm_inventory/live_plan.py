"""Build live Jumper quotes that meet Bitget's current deposit minimums."""

from __future__ import annotations

import csv
import json
from collections import Counter
from pathlib import Path
from typing import Protocol

import httpx

from .bitget_catalog import BitgetDepositTarget, deposit_targets, fetch_public_coins
from .lifi import LifiClient, LifiRouteRequest

_BASE_USDC = (8453, "0x833589fcd6edb6e08f4c7c32d4f71b54bda02913")


class QuoteClient(Protocol):
    def routes(self, request: LifiRouteRequest): ...


def create_live_plan(
    balances_path: Path,
    *,
    deposit_addresses: dict[str, str],
    allowlist_path: Path = Path("config/swap-allowlist.json"),
    quote_floor: str = "0.01",
    client: QuoteClient | None = None,
    targets: tuple[BitgetDepositTarget, ...] | None = None,
) -> dict:
    """Quote only allowlisted balances and retain routes to a wallet-owned staging balance.

    This function makes read-only HTTP requests. It neither asks Jumper for signed
    data nor sends a transaction.
    """

    policy = _policy(allowlist_path)
    targets = targets or deposit_targets(fetch_public_coins())
    client = client or LifiClient(httpx.Client(timeout=30))
    rows: list[dict] = []
    counts: Counter[str] = Counter()
    with balances_path.open(newline="") as stream:
        for row in csv.DictReader(stream):
            if row.get("status") != "success" or int(row["raw_balance"]) <= 0:
                continue
            status, item = _quote_row(
                row,
                policy=policy,
                targets=targets,
                deposit_addresses=deposit_addresses,
                quote_floor=quote_floor,
                client=client,
            )
            counts[status] += 1
            rows.append(item)
    deposits = _staged_deposits(rows, targets=targets, deposit_addresses=deposit_addresses)
    rows.extend(deposits)
    counts["post_bridge_deposit"] += len(deposits)
    return {
        "version": 2,
        "mode": "read_only_quote",
        "execution": {
            "sequential": True,
            "delay_min_seconds": 1800,
            "delay_max_seconds": 10800,
            "gas_reserve_multiplier": 5,
            "requires_explicit_execute": True,
        },
        "summary": dict(sorted(counts.items())),
        "entries": rows,
    }


def _quote_row(
    row: dict[str, str],
    *,
    policy: dict[tuple[int, str], str],
    targets: tuple[BitgetDepositTarget, ...],
    deposit_addresses: dict[str, str],
    quote_floor: str,
    client: QuoteClient,
) -> tuple[str, dict]:
    wallet = row["wallet"].lower()
    chain_id = int(row["chain_id"])
    asset_id = row["asset_id"].lower()
    raw_balance = int(row["raw_balance"])
    decimals = int(row["decimals"])
    result = _base_entry(row)
    action = policy.get((chain_id, asset_id), "deny")
    if action == "deny":
        return "denied", {**result, "status": "denied"}
    if action == "review":
        return "manual_review", {**result, "status": "manual_review"}
    if raw_balance < _floor_raw(quote_floor, decimals):
        return "dust", {**result, "status": "dust"}
    address = deposit_addresses.get(wallet)
    if address is None:
        return "missing_deposit_address", {**result, "status": "missing_deposit_address"}
    direct = _direct_target(chain_id, asset_id, targets)
    if direct and (chain_id, asset_id) == _BASE_USDC:
        return "stage_existing", {
            **result,
            "status": "stage_existing",
            "target": _target_data(direct),
        }
    if direct and raw_balance >= direct.minimum_raw:
        return "direct_deposit", {
            **result,
            "status": "direct_deposit",
            "target": _target_data(direct),
            "deposit_address": address,
            "requires_gas_preflight": True,
        }
    target = _route_target(targets)
    if target is None:
        return "no_bitget_target", {**result, "status": "no_bitget_target"}
    request = LifiRouteRequest(
        from_chain_id=chain_id,
        to_chain_id=target.chain_id,
        from_token_address=_token_address(asset_id),
        to_token_address=_token_address(target.asset_id),
        from_amount=str(raw_balance),
        from_address=wallet,
        to_address=wallet,
    )
    try:
        routes = client.routes(request)
    except (httpx.HTTPError, ValueError):
        return "quote_failed", {**result, "status": "quote_failed", "target": _target_data(target)}
    if asset_id == "native":
        reserve = 5 * _source_gas_cost(routes, chain_id)
        spendable = raw_balance - reserve
        if spendable < _floor_raw(quote_floor, decimals):
            return "dust_after_gas", {**result, "status": "dust_after_gas"}
        request = LifiRouteRequest(
            from_chain_id=request.from_chain_id,
            to_chain_id=request.to_chain_id,
            from_token_address=request.from_token_address,
            to_token_address=request.to_token_address,
            from_amount=str(spendable),
            from_address=request.from_address,
            to_address=request.to_address,
        )
        try:
            routes = client.routes(request)
        except (httpx.HTTPError, ValueError):
            return "quote_failed", {
                **result,
                "status": "quote_failed",
                "target": _target_data(target),
            }
    viable = [route for route in routes if route.to_amount_min > 0]
    if not viable:
        return "no_staging_route", {
            **result,
            "status": "no_staging_route",
            "target": _target_data(target),
        }
    route = max(viable, key=lambda item: item.to_amount_min)
    return "route_ready", {
        **result,
        "status": "route_ready",
        "target": _target_data(target),
        "deposit_address": address,
        "settlement": "wallet",
        "route": {
            "id": route.route_id,
            "from_amount": str(route.from_amount),
            "to_amount": str(route.to_amount),
            "to_amount_min": str(route.to_amount_min),
            "tools": list(route.tools),
            "step": route.first_step,
        },
        "requires_gas_preflight": True,
    }


def _base_entry(row: dict[str, str]) -> dict:
    return {
        "wallet": row["wallet"].lower(),
        "chain_id": int(row["chain_id"]),
        "asset_id": row["asset_id"].lower(),
        "symbol": row.get("symbol", ""),
        "raw_balance": row["raw_balance"],
        "decimals": int(row["decimals"]),
    }


def _policy(path: Path) -> dict[tuple[int, str], str]:
    data = json.loads(path.read_text(encoding="utf-8"))
    return {
        (int(item["chain_id"]), item["asset_id"].lower()): item["action"]
        for item in data["assets"]
    }


def _floor_raw(value: str, decimals: int) -> int:
    from decimal import Decimal

    return int(Decimal(value) * (10**decimals))


def _token_address(asset_id: str) -> str:
    return "0x0000000000000000000000000000000000000000" if asset_id == "native" else asset_id


def _direct_target(
    chain_id: int, asset_id: str, targets: tuple[BitgetDepositTarget, ...]
) -> BitgetDepositTarget | None:
    return next(
        (item for item in targets if item.chain_id == chain_id and item.asset_id == asset_id), None
    )


def _route_target(targets: tuple[BitgetDepositTarget, ...]) -> BitgetDepositTarget | None:
    return next(
        (item for item in targets if (item.chain_id, item.asset_id) == _BASE_USDC),
        next((item for item in targets if item.coin == "USDC"), None),
    )


def _target_data(target: BitgetDepositTarget) -> dict:
    return {
        "coin": target.coin,
        "chain_id": target.chain_id,
        "asset_id": target.asset_id,
        "minimum_raw": str(target.minimum_raw),
    }


def _staged_deposits(
    entries: list[dict],
    *,
    targets: tuple[BitgetDepositTarget, ...],
    deposit_addresses: dict[str, str],
) -> list[dict]:
    target = _route_target(targets)
    if target is None:
        return []
    totals: Counter[str] = Counter()
    for entry in entries:
        if entry["status"] == "stage_existing":
            totals[entry["wallet"]] += int(entry["raw_balance"])
        elif entry["status"] == "route_ready":
            totals[entry["wallet"]] += int(entry["route"]["to_amount_min"])
    return [
        {
            "wallet": wallet,
            "chain_id": target.chain_id,
            "asset_id": target.asset_id,
            "symbol": target.coin,
            "raw_balance": str(amount),
            "decimals": 6,
            "status": "post_bridge_deposit",
            "target": _target_data(target),
            "deposit_address": deposit_addresses[wallet],
            "requires_gas_preflight": True,
        }
        for wallet, amount in sorted(totals.items())
        if amount >= target.minimum_raw and wallet in deposit_addresses
    ]


def _source_gas_cost(routes: tuple, source_chain_id: int) -> int:
    """Extract source-chain native gas from Jumper's rich step estimate."""

    if not routes:
        return 0
    costs = routes[0].first_step.get("estimate", {}).get("gasCosts", [])
    return sum(
        int(cost.get("amount", 0))
        for cost in costs
        if isinstance(cost, dict)
        and cost.get("token", {}).get("chainId") == source_chain_id
        and cost.get("token", {}).get("address") == "0x0000000000000000000000000000000000000000"
    )
