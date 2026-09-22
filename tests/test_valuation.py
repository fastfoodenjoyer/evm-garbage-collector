from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from evm_inventory.models import AssetIdentity
from evm_inventory.valuation import FeeQuote, QuotePrice, quote_valuation, raw_to_decimal

NOW = datetime(2026, 9, 22, 12, tzinfo=UTC)
USDC = AssetIdentity(10, "0x" + "a" * 40, 6)
ETH = AssetIdentity(8453, "native", 18)


def test_raw_amount_normalizes_exactly_by_asset_decimals():
    assert raw_to_decimal(1_234_567, USDC) == Decimal("1.234567")
    assert raw_to_decimal(1_500_000_000_000_000_000, ETH) == Decimal("1.5")


def test_uint256_valuation_preserves_single_raw_unit_loss_exactly():
    raw_amount = 2**256 - 1
    expected_source = Decimal(
        (0, tuple(int(digit) for digit in str(raw_amount)), -USDC.decimals)
    )

    assert raw_to_decimal(raw_amount, USDC) == expected_source
    valuation = quote_valuation(
        source_amount=raw_amount,
        source_price=QuotePrice(USDC, "1", NOW),
        destination_amount=raw_amount - 1,
        destination_price=QuotePrice(USDC, "1", NOW),
        wallet_paid_gas=(),
        now=NOW,
        max_price_age=timedelta(minutes=5),
    )

    assert valuation.source_usd == expected_source
    assert valuation.loss_usd == Decimal("0.000001")


@pytest.mark.parametrize(
    "price", [None, 0.1, "NaN", "Infinity", "-1", "0", "not-a-price"]
)
def test_quote_price_rejects_missing_or_non_finite_values(price):
    with pytest.raises(ValueError, match="price"):
        QuotePrice(USDC, price, NOW)


def test_quote_price_rejects_stale_evidence():
    price = QuotePrice(USDC, "1", NOW - timedelta(minutes=6))

    with pytest.raises(ValueError, match="stale"):
        quote_valuation(
            source_amount=1_000_000,
            source_price=price,
            destination_amount=1_000_000,
            destination_price=QuotePrice(USDC, "1", NOW),
            fees=(),
            now=NOW,
            max_price_age=timedelta(minutes=5),
        )


def test_quote_valuation_calculates_source_destination_fee_and_usd_loss_conservatively():
    valuation = quote_valuation(
        source_amount=2_000_000,
        source_price=QuotePrice(USDC, "10", NOW),
        destination_amount=1_500_000_000_000_000_000,
        destination_price=QuotePrice(ETH, "12", NOW),
        wallet_paid_gas=(FeeQuote(1_000_000, QuotePrice(USDC, "1", NOW)),),
        now=NOW,
        max_price_age=timedelta(minutes=5),
    )

    assert valuation.source_usd == Decimal("20")
    assert valuation.destination_usd == Decimal("18.0")
    assert valuation.wallet_paid_gas_usd == Decimal("1")
    assert valuation.loss_usd == Decimal("3.0")


def test_quote_valuation_never_turns_destination_gain_into_negative_route_loss():
    valuation = quote_valuation(
        source_amount=1_000_000,
        source_price=QuotePrice(USDC, "1", NOW),
        destination_amount=2_000_000,
        destination_price=QuotePrice(USDC, "1", NOW),
        wallet_paid_gas=(),
        now=NOW,
        max_price_age=timedelta(minutes=5),
    )

    assert valuation.loss_usd == Decimal("0")


def test_quote_valuation_rejects_unpriced_wallet_paid_gas():
    with pytest.raises(ValueError, match="gas"):
        quote_valuation(
            source_amount=1_000_000,
            source_price=QuotePrice(USDC, "1", NOW),
            destination_amount=1_000_000,
            destination_price=QuotePrice(USDC, "1", NOW),
            wallet_paid_gas=(FeeQuote(1, None),),
            now=NOW,
            max_price_age=timedelta(minutes=5),
        )
