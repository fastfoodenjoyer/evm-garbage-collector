"""Read-only routing eligibility primitives.

This module never accepts a private key and never constructs signed transactions.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class DirectNativeDepositPlan:
    status: str
    send_amount_wei: int


def direct_native_deposit_plan(
    *,
    balance_wei: int,
    minimum_deposit_wei: int,
) -> DirectNativeDepositPlan:
    """Return a gas-independent direct-deposit eligibility decision."""

    if balance_wei < 0 or minimum_deposit_wei < 0:
        raise ValueError("balance and minimum deposit must be nonnegative")
    if balance_wei < minimum_deposit_wei:
        return DirectNativeDepositPlan(
            status="below_minimum",
            send_amount_wei=0,
        )
    return DirectNativeDepositPlan(
        status="ready",
        send_amount_wei=balance_wei,
    )
