"""Classify balances before requesting live swap or bridge quotes."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class Position:
    chain_id: int
    asset_id: str
    raw_balance: int


@dataclass(frozen=True, slots=True)
class DepositTarget:
    chain_id: int
    asset_id: str
    minimum_raw: int


@dataclass(frozen=True, slots=True)
class PositionDecision:
    status: str
    quote_required: bool


def classify_position(
    position: Position,
    *,
    targets: tuple[DepositTarget, ...],
    quote_floor_raw: int,
) -> PositionDecision:
    """Avoid live route requests for dust and direct-deposit positions."""

    if position.raw_balance < 0 or quote_floor_raw < 0:
        raise ValueError("balances and quote floors must be nonnegative")
    target = next(
        (
            item
            for item in targets
            if item.chain_id == position.chain_id
            and item.asset_id.lower() == position.asset_id.lower()
        ),
        None,
    )
    if target and position.raw_balance >= target.minimum_raw:
        return PositionDecision(status="direct_deposit", quote_required=False)
    if position.raw_balance < quote_floor_raw:
        return PositionDecision(status="dust", quote_required=False)
    return PositionDecision(status="quote_required", quote_required=True)
