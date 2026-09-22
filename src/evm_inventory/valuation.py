"""Conservative USD valuation derived only from timestamped quote evidence."""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal, InvalidOperation, localcontext

from .models import AssetIdentity


@dataclass(frozen=True, slots=True)
class QuotePrice:
    """A USD unit price supplied by a quote, with its observation time."""

    asset: AssetIdentity
    usd_per_token: Decimal | str | None
    observed_at: datetime

    def __post_init__(self) -> None:
        if not isinstance(self.asset, AssetIdentity):
            raise ValueError("price asset is invalid")
        if not isinstance(self.observed_at, datetime) or self.observed_at.tzinfo is None:
            raise ValueError("price timestamp must be timezone-aware")
        if isinstance(self.usd_per_token, float):
            raise ValueError("price must be supplied as an exact decimal")
        try:
            price = Decimal(self.usd_per_token)  # type: ignore[arg-type]
        except (InvalidOperation, TypeError, ValueError) as exc:
            raise ValueError("price must be a finite positive Decimal") from exc
        if not price.is_finite() or price <= 0:
            raise ValueError("price must be a finite positive Decimal")
        object.__setattr__(self, "usd_per_token", price)
        object.__setattr__(self, "observed_at", self.observed_at.astimezone(UTC))


@dataclass(frozen=True, slots=True)
class FeeQuote:
    raw_amount: int
    price: QuotePrice | None

    def __post_init__(self) -> None:
        _validate_raw_amount(self.raw_amount, "fee amount")


@dataclass(frozen=True, slots=True)
class RouteValuation:
    source_usd: Decimal
    destination_usd: Decimal
    wallet_paid_gas_usd: Decimal
    loss_usd: Decimal

    @property
    def fees_usd(self) -> Decimal:
        """Backward-compatible name for wallet-paid gas only."""

        return self.wallet_paid_gas_usd


def raw_to_decimal(raw_amount: int, asset: AssetIdentity) -> Decimal:
    """Convert a raw on-chain amount without losing precision."""

    _validate_raw_amount(raw_amount, "raw amount")
    if not isinstance(asset, AssetIdentity):
        raise ValueError("asset is invalid")
    digits = tuple(int(digit) for digit in str(raw_amount))
    return Decimal((0, digits, -asset.decimals))


def quote_valuation(
    *,
    source_amount: int,
    source_price: QuotePrice,
    destination_amount: int,
    destination_price: QuotePrice,
    fees: Iterable[FeeQuote] | None = None,
    wallet_paid_gas: Iterable[FeeQuote] | None = None,
    now: datetime,
    max_price_age: timedelta,
) -> RouteValuation:
    """Value route output conservatively and add only wallet-paid gas.

    ``fees`` remains as a compatibility alias for older callers. It must contain
    wallet-paid gas estimates only; provider fee items already reflected in the
    quoted output must not be passed here.
    """

    _validate_now_and_max_age(now, max_price_age)
    if fees is not None and wallet_paid_gas is not None:
        raise ValueError("supply wallet_paid_gas, not both gas and legacy fees")
    _require_fresh(source_price, now, max_price_age)
    _require_fresh(destination_price, now, max_price_age)
    source_usd = _usd_value(source_amount, source_price)
    destination_usd = _usd_value(destination_amount, destination_price)

    gas_values: list[Decimal] = []
    gas_quotes = wallet_paid_gas if wallet_paid_gas is not None else fees
    for gas in gas_quotes or ():
        if not isinstance(gas, FeeQuote) or gas.price is None:
            raise ValueError("wallet-paid gas must include a quoted price")
        _require_fresh(gas.price, now, max_price_age)
        gas_values.append(_usd_value(gas.raw_amount, gas.price))
    gas_usd = exact_decimal_sum(gas_values)
    slippage_loss = max(
        Decimal(0), exact_decimal_difference(source_usd, destination_usd)
    )
    loss_usd = exact_decimal_sum((slippage_loss, gas_usd))
    return RouteValuation(
        source_usd=source_usd,
        destination_usd=destination_usd,
        wallet_paid_gas_usd=gas_usd,
        loss_usd=loss_usd,
    )


def _usd_value(raw_amount: int, price: QuotePrice) -> Decimal:
    amount = raw_to_decimal(raw_amount, price.asset)
    unit_price = price.usd_per_token
    assert isinstance(unit_price, Decimal)
    with localcontext() as context:
        context.prec = max(
            context.prec,
            len(amount.as_tuple().digits) + len(unit_price.as_tuple().digits) + 2,
        )
        return amount * unit_price


def exact_decimal_sum(values: Iterable[Decimal]) -> Decimal:
    """Add finite Decimal values without rounding away low-order digits."""

    terms = tuple(values)
    nonzero = tuple(value for value in terms if value)
    if not nonzero:
        return Decimal(0)
    max_adjusted = max(value.adjusted() for value in nonzero)
    min_exponent = min(value.as_tuple().exponent for value in nonzero)
    precision = max_adjusted - min_exponent + len(str(len(terms))) + 2
    with localcontext() as context:
        context.prec = max(context.prec, precision)
        return sum(terms, start=Decimal(0))


def exact_decimal_difference(left: Decimal, right: Decimal) -> Decimal:
    """Subtract finite Decimal values without applying unary context rounding."""

    return exact_decimal_sum((left, right.copy_negate()))


def exact_percentage(numerator: Decimal, denominator: Decimal) -> Decimal:
    """Calculate a percentage with ample precision for deterministic ranking."""

    precision = max(
        len(numerator.as_tuple().digits), len(denominator.as_tuple().digits)
    ) + 50
    with localcontext() as context:
        context.prec = max(context.prec, precision)
        return numerator / denominator * Decimal(100)


def _validate_raw_amount(value: object, field: str) -> None:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError(f"{field} must be a non-negative integer")


def _validate_now_and_max_age(now: datetime, max_price_age: timedelta) -> None:
    if not isinstance(now, datetime) or now.tzinfo is None:
        raise ValueError("now must be timezone-aware")
    if not isinstance(max_price_age, timedelta) or max_price_age < timedelta(0):
        raise ValueError("max_price_age must be non-negative")


def _require_fresh(price: QuotePrice, now: datetime, max_price_age: timedelta) -> None:
    observed_at = price.observed_at
    current = now.astimezone(UTC)
    if observed_at > current or current - observed_at > max_price_age:
        raise ValueError("quote price is stale")
