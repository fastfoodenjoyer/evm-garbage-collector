"""Recover deterministic unsigned route failures using the same exact quote request."""

from __future__ import annotations

import re
from collections.abc import Callable
from datetime import datetime

from .diagnostics import exception_diagnostic, redact_data
from .route_preparation import is_route_preparation_rejected
from .rpc import RpcError

_QUOTE_TTL_SECONDS = 240


class UnsignedRoutePreparationExhausted(ValueError):
    """Every usable alternative failed; wait until a fresh quote can be obtained."""

    def __init__(self, *, retry_at: float, failures: list[dict]):
        super().__init__("unsigned_route_preparation_exhausted")
        self.retry_at = retry_at
        self.diagnostic = {"route_failures": redact_data(failures)}


class InvalidUnsignedRouteCandidate(ValueError):
    """A prepared alternative failed ordinary conservative admission checks."""


def _structured_revert(provider: object) -> bool:
    if not isinstance(provider, dict) or provider.get("method") != "eth_estimateGas":
        return False
    error = provider.get("provider_error")
    if (not isinstance(error, dict) or not isinstance(error.get("code"), int)
            or isinstance(error.get("code"), bool)):
        return False
    message = str(error.get("message", "")).lower()
    return "execution reverted" in message or (
        "vm exception" in message and re.search(r"\brevert(?:ed)?\b", message) is not None
    )


def _deterministic_failure(error: Exception, diagnostics: list[dict]) -> bool:
    if is_route_preparation_rejected(error):
        return True
    seen = set()
    current = error
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        if isinstance(current, RpcError) and _structured_revert(current.diagnostic):
            return True
        current = current.__cause__
    if not isinstance(error, ValueError) or str(error) != "requote_required:missing_gas_estimate":
        return False
    # A handled simulation error in __context__ cannot explain a later metadata failure.
    for item in diagnostics:
        for index, exception in enumerate(item.get("diagnostic", {}).get("exceptions", [])):
            if exception.get("chain_link") != ("root" if index == 0 else "cause"):
                break
            if (exception.get("type") == "RpcError"
                    and _structured_revert(exception.get("provider"))):
                return True
    return False


def prepare_unsigned_route_options(
    chosen,
    *,
    alternatives: Callable,
    validate: Callable,
    prepare: Callable,
    diagnostics: list[dict],
    now: Callable[[], datetime],
):
    """Keep operational errors intact and stop at the first fully admitted route."""
    failures = []
    attempted = []

    def attempt(route):
        attempted.append(route)
        start = len(diagnostics)
        try:
            return prepare(route)
        except Exception as error:
            deterministic = _deterministic_failure(
                error, [d for d in diagnostics[start:] if d.get("route_id") == route.route_id])
            if not deterministic and not (
                failures and isinstance(error, InvalidUnsignedRouteCandidate)
            ):
                raise
            failure = {"route_id": route.route_id, "phase": "unsigned_route_preparation",
                       "diagnostic": exception_diagnostic(error)}
            failures.append(failure)
            diagnostics.append(failure)
            return None

    result = attempt(chosen)
    if result is not None:
        return result
    for route in alternatives():
        if route.route_id == chosen.route_id:
            continue
        try:
            validate(route)
        except ValueError as error:
            failure = {"route_id": route.route_id, "phase": "unsigned_route_identity",
                       "diagnostic": exception_diagnostic(error)}
            failures.append(failure)
            diagnostics.append(failure)
            continue
        result = attempt(route)
        if result is not None:
            return result
    retry_at = now().timestamp() + _QUOTE_TTL_SECONDS
    for route in attempted:
        try:
            expiry = datetime.fromisoformat(route.evidence.quote_timestamp.replace("Z", "+00:00"))
            retry_at = max(retry_at, expiry.timestamp() + _QUOTE_TTL_SECONDS)
        except (AttributeError, TypeError, ValueError):
            pass
    raise UnsignedRoutePreparationExhausted(retry_at=retry_at, failures=failures)
