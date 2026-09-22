"""Validated settings shared by live route planning and execution."""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation

from .models import ConfigError

_MAX_ROUTE_LOSS_PCT = "MAX_ROUTE_LOSS_PCT"
_DEFAULT_MAX_ROUTE_LOSS_PCT = Decimal("15")


@dataclass(frozen=True)
class RoutingSettings:
    max_route_loss_pct: Decimal


def load_routing_settings(
    environ: Mapping[str, str] | None = None,
) -> RoutingSettings:
    """Load routing settings from an environment mapping or ``os.environ``."""

    source = os.environ if environ is None else environ
    raw = source.get(_MAX_ROUTE_LOSS_PCT)
    if raw is None:
        return RoutingSettings(max_route_loss_pct=_DEFAULT_MAX_ROUTE_LOSS_PCT)

    try:
        value = Decimal(raw.strip())
    except (InvalidOperation, ValueError):
        raise ConfigError(
            f"{_MAX_ROUTE_LOSS_PCT} must be a finite decimal in (0, 100]"
        ) from None

    if not value.is_finite() or not Decimal("0") < value <= Decimal("100"):
        raise ConfigError(
            f"{_MAX_ROUTE_LOSS_PCT} must be a finite decimal in (0, 100]"
        )
    return RoutingSettings(max_route_loss_pct=value)
