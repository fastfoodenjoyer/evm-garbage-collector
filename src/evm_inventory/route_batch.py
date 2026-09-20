"""Batch quote orchestration that never requests routes for dust."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from .route_plan import DepositTarget, Position, classify_position


@dataclass(frozen=True, slots=True)
class BatchQuoteResult:
    position: Position
    status: str
    quote: object | None = None


def quote_required_positions(
    positions: tuple[Position, ...],
    *,
    targets: tuple[DepositTarget, ...],
    quote_floor_raw: int,
    quote: Callable[[Position], object],
) -> tuple[BatchQuoteResult, ...]:
    """Classify positions and quote only the economically eligible remainder."""

    results: list[BatchQuoteResult] = []
    for position in positions:
        decision = classify_position(
            position,
            targets=targets,
            quote_floor_raw=quote_floor_raw,
        )
        if decision.quote_required:
            results.append(BatchQuoteResult(position, "quoted", quote(position)))
        else:
            results.append(BatchQuoteResult(position, decision.status))
    return tuple(results)
