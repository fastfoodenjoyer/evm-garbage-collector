"""Read-only routing and gas-planning primitives.

This module never accepts a private key and never constructs signed transactions.
"""

from __future__ import annotations

from dataclasses import dataclass

GAS_RESERVE_MULTIPLIER = 5


@dataclass(frozen=True, slots=True)
class NativeSpendableAmount:
    balance_wei: int
    gas_reserve_wei: int
    spendable_wei: int
    gas_reserve_multiplier: int


@dataclass(frozen=True, slots=True)
class DirectNativeDepositPlan:
    status: str
    send_amount_wei: int
    gas_reserve_wei: int


def native_spendable_amount(
    balance_wei: int,
    *,
    gas_limit: int,
    gas_price_wei: int,
    gas_reserve_multiplier: int = GAS_RESERVE_MULTIPLIER,
) -> NativeSpendableAmount:
    """Reserve a multiple of the estimated gas cost before spending a native balance."""

    if min(balance_wei, gas_limit, gas_price_wei) < 0:
        raise ValueError("balance, gas limit, and gas price must be nonnegative")
    if gas_reserve_multiplier < 1:
        raise ValueError("gas reserve multiplier must be at least one")
    reserve = gas_limit * gas_price_wei * gas_reserve_multiplier
    return NativeSpendableAmount(
        balance_wei=balance_wei,
        gas_reserve_wei=reserve,
        spendable_wei=max(0, balance_wei - reserve),
        gas_reserve_multiplier=gas_reserve_multiplier,
    )


def direct_native_deposit_plan(
    *,
    balance_wei: int,
    gas_limit: int,
    gas_price_wei: int,
    minimum_deposit_wei: int,
    gas_reserve_multiplier: int = GAS_RESERVE_MULTIPLIER,
) -> DirectNativeDepositPlan:
    """Return a direct-deposit decision after retaining the native gas reserve."""

    if minimum_deposit_wei < 0:
        raise ValueError("minimum deposit must be nonnegative")
    available = native_spendable_amount(
        balance_wei,
        gas_limit=gas_limit,
        gas_price_wei=gas_price_wei,
        gas_reserve_multiplier=gas_reserve_multiplier,
    )
    if available.spendable_wei < minimum_deposit_wei:
        return DirectNativeDepositPlan(
            status="below_minimum_after_gas_reserve",
            send_amount_wei=0,
            gas_reserve_wei=available.gas_reserve_wei,
        )
    return DirectNativeDepositPlan(
        status="ready",
        send_amount_wei=available.spendable_wei,
        gas_reserve_wei=available.gas_reserve_wei,
    )
