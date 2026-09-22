from decimal import Decimal

import pytest

from evm_inventory.models import ConfigError
from evm_inventory.routing_settings import load_routing_settings


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        (None, Decimal("15")),
        ("15", Decimal("15")),
        ("7.5", Decimal("7.5")),
        ("100", Decimal("100")),
    ],
)
def test_max_route_loss_pct_uses_default_or_valid_decimal(monkeypatch, raw, expected):
    monkeypatch.delenv("MAX_ROUTE_LOSS_PCT", raising=False)
    if raw is not None:
        monkeypatch.setenv("MAX_ROUTE_LOSS_PCT", raw)

    assert load_routing_settings().max_route_loss_pct == expected


@pytest.mark.parametrize(
    "raw",
    ["", "0", "-0.1", "100.01", "NaN", "Infinity", "-Infinity", "x"],
)
def test_max_route_loss_pct_rejects_invalid_values_with_stable_message(monkeypatch, raw):
    monkeypatch.setenv("MAX_ROUTE_LOSS_PCT", raw)
    with pytest.raises(ConfigError, match="MAX_ROUTE_LOSS_PCT") as exc_info:
        load_routing_settings()

    assert str(exc_info.value) == "MAX_ROUTE_LOSS_PCT must be a finite decimal in (0, 100]"


def test_routing_settings_is_frozen():
    settings = load_routing_settings({})

    with pytest.raises(AttributeError):
        settings.max_route_loss_pct = Decimal("20")
