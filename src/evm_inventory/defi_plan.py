"""Key-free, read-only plans for Rabby's supported DeFi withdrawal actions."""

from __future__ import annotations

import hashlib
import json
import re
import time
from collections import Counter
from decimal import Decimal, InvalidOperation
from typing import Any, Protocol

from .rabby import encode_action

_ADDRESS = re.compile(r"^0x[0-9a-fA-F]{40}$")


class RabbyPortfolio(Protocol):
    def chain_ids(self) -> dict[str, int]: ...

    def positions(self, wallet: str) -> list[dict[str, Any]]: ...


def _identity(entry: dict[str, Any]) -> tuple[str, int | None, str, str, str]:
    return (
        entry["wallet"],
        entry["chain_id"],
        entry["protocol_id"],
        entry["pool_id"],
        entry["position_index"],
    )


def _action_id(entry: dict[str, Any]) -> str:
    payload = {"identity": _identity(entry), "action": entry["action"]}
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    return hashlib.sha256(canonical.encode()).hexdigest()


def create_defi_plan(
    wallets: list[str] | tuple[str, ...],
    rabby: RabbyPortfolio,
    *,
    supported_chain_ids: set[int],
    now_seconds: int | None = None,
) -> dict[str, Any]:
    """Select a single simple withdraw action per position; explain every exclusion."""

    now_seconds = int(time.time()) if now_seconds is None else now_seconds
    chain_ids = rabby.chain_ids()
    entries: list[dict[str, Any]] = []
    for wallet in wallets:
        for protocol in rabby.positions(wallet):
            if not isinstance(protocol, dict):
                raise ValueError("Rabby returned an invalid protocol")
            protocol_id = protocol.get("id")
            chain = protocol.get("chain")
            if not isinstance(protocol_id, str) or not isinstance(chain, str):
                raise ValueError("Rabby protocol identity is missing")
            chain_id = chain_ids.get(chain)
            positions = protocol.get("portfolio_item_list")
            if not isinstance(positions, list):
                raise ValueError("Rabby protocol positions are missing")
            for item in positions:
                if not isinstance(item, dict):
                    raise ValueError("Rabby returned an invalid position")
                pool = item.get("pool") or {}
                if not isinstance(pool, dict):
                    pool = {}
                pool_id = pool.get("id") or pool.get("controller") or ""
                index = item.get("position_index") or ""
                entry: dict[str, Any] = {
                    "wallet": wallet.lower(),
                    "chain": chain,
                    "chain_id": chain_id,
                    "protocol_id": protocol_id,
                    "protocol_name": protocol.get("name") or protocol_id,
                    "pool_id": str(pool_id),
                    "position_index": str(index),
                    "position_name": item.get("name") or "",
                    "status": "manual_review",
                    "reason": "",
                    "action_id": None,
                    "action": None,
                    "net_usd_value": (item.get("stats") or {}).get("net_usd_value"),
                    "debt_usd_value": (item.get("stats") or {}).get("debt_usd_value"),
                    "output_token_ids": _output_tokens(item, None),
                }
                if chain_id not in supported_chain_ids or pool.get("chain", chain) != chain:
                    entry["reason"] = "chain_not_configured"
                elif not pool_id:
                    entry["reason"] = "position_identity_missing"
                elif (item.get("proxy_detail") or {}).get("proxy_contract_id"):
                    entry["reason"] = "proxy_position"
                elif _positive(item.get("stats", {}).get("debt_usd_value")):
                    entry["reason"] = "outstanding_debt"
                else:
                    actions = item.get("withdraw_actions") or []
                    withdrawals = [
                        a for a in actions if isinstance(a, dict) and a.get("type") == "withdraw"
                    ]
                    if len(withdrawals) != 1:
                        entry["reason"] = "no_single_direct_withdraw_action"
                    else:
                        try:
                            encode_action(withdrawals[0], wallet=wallet, now_seconds=now_seconds)
                        except (TypeError, ValueError):
                            entry["reason"] = "unsafe_or_unsupported_action"
                        else:
                            entry["output_token_ids"] = _output_tokens(item, withdrawals[0])
                            if not entry["output_token_ids"]:
                                entry["reason"] = "output_asset_unknown"
                                entries.append(entry)
                                continue
                            entry["status"] = "ready"
                            entry["action"] = withdrawals[0]
                            entry["action_id"] = _action_id(entry)
                entries.append(entry)
    counts = Counter(_identity(entry) for entry in entries)
    for entry in entries:
        if counts[_identity(entry)] > 1:
            entry.update(
                status="manual_review", reason="duplicate_position", action=None, action_id=None
            )
    summary = dict(Counter(entry["status"] for entry in entries))
    return {
        "schema": "rabby-defi-withdraw-v1",
        "created_at": now_seconds,
        "entries": entries,
        "summary": {
            "ready": summary.get("ready", 0),
            "manual_review": summary.get("manual_review", 0),
        },
    }


def _positive(value: object) -> bool:
    try:
        amount = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        return True
    return not amount.is_finite() or amount > 0


def _output_tokens(item: dict[str, Any], action: dict[str, Any] | None) -> list[str]:
    supplied = (item.get("detail") or {}).get("supply_token_list", [])
    if action and str(action.get("func", "")).startswith("removeLiquidity("):
        candidates = (action.get("str_params") or [])[:2]
    else:
        candidates = [
            token.get("id")
            for token in supplied
            if isinstance(token, dict)
        ]
    return sorted(
        {
            token.lower()
            for token in candidates
            if isinstance(token, str) and (token.lower() == "eth" or _ADDRESS.fullmatch(token))
        }
    )
