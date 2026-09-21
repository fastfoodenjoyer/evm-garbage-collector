"""Persistable, transaction-free consolidation runbooks."""

from __future__ import annotations

import csv
import json
from collections import Counter
from pathlib import Path

from .execution_policy import SequentialSchedule
from .route_plan import DepositTarget, Position, classify_position

_TARGETS = (
    DepositTarget(8453, "0x833589fcd6edb6e08f4c7c32d4f71b54bda02913", 9_997),
    DepositTarget(10, "0x0b2c639c533813f4aa9d7837caf62653d097ff85", 9_997),
    DepositTarget(42161, "0xaf88d065e77c8cc2239327c5edb3a432268e5831", 9_997),
)


def create_route_plan(
    balances_path: Path, *, quote_floor_raw: int = 100, allowlist_path: Path | None = None
) -> dict:
    """Classify current balances into a JSON-safe, no-transaction runbook."""

    entries = []
    counts: Counter[str] = Counter()
    if allowlist_path is None:
        allowlist_path = Path("config/swap-allowlist.json")
    policy = _allowlist_policy(allowlist_path)
    with Path(balances_path).open(newline="") as stream:
        for row in csv.DictReader(stream):
            raw_balance = int(row["raw_balance"])
            if raw_balance <= 0:
                continue
            position = Position(int(row["chain_id"]), row["asset_id"], raw_balance)
            asset_policy = policy.get((position.chain_id, position.asset_id.lower()), "deny")
            if asset_policy == "deny":
                counts["denied"] += 1
                entries.append(_entry(row, position, "denied"))
                continue
            if asset_policy == "review":
                counts["manual_review"] += 1
                entries.append(_entry(row, position, "manual_review"))
                continue
            decision = classify_position(
                position, targets=_TARGETS, quote_floor_raw=quote_floor_raw
            )
            counts[decision.status] += 1
            entries.append(_entry(row, position, decision.status))
    schedule = SequentialSchedule()
    return {
        "version": 1,
        "execution": {
            "sequential": True,
            "delay_min_seconds": schedule.delay_min_seconds,
            "delay_max_seconds": schedule.delay_max_seconds,
            "requires_explicit_execute": True,
        },
        "summary": {key: counts[key] for key in (
            "direct_deposit", "dust", "quote_required", "manual_review", "denied"
        )},
        "entries": entries,
    }


def _allowlist_policy(path: Path) -> dict[tuple[int, str], str]:
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    return {
        (int(item["chain_id"]), item["asset_id"].lower()): item["action"]
        for item in data["assets"]
    }


def _entry(row: dict[str, str], position: Position, status: str) -> dict[str, object]:
    return {"wallet": row["wallet"].lower(), "chain_id": position.chain_id,
            "asset_id": position.asset_id.lower(), "symbol": row.get("symbol", ""),
            "raw_balance": str(position.raw_balance), "status": status}
