"""Identify the one unsigned LI.FI preparation failure eligible for a fresh quote."""

import math

import httpx

from .stages import StageDeadlineExceeded

_STEP_TRANSACTION_URL = "https://li.quest/v1/advanced/stepTransaction"
_LEGACY_PREPARATION_REJECTION = (
    f"Client error '422 Unprocessable Entity' for url '{_STEP_TRANSACTION_URL}'\n"
    "For more information check: https://developer.mozilla.org/en-US/docs/Web/HTTP/Status/422"
)


def is_route_preparation_rejected(error: Exception) -> bool:
    return (
        isinstance(error, httpx.HTTPStatusError)
        and error.response.status_code == 422
        and error.request.method == "POST"
        and str(error.request.url) == _STEP_TRANSACTION_URL
    )


def is_route_preparation_retry_reason(reason: str | None) -> bool:
    return reason in {"route_preparation_rejected", _LEGACY_PREPARATION_REJECTION,
                      "requote_required:missing_gas_estimate"}


def is_fee_cap_changed(error: Exception) -> bool:
    """Reprice an unsigned request instead of raising its immutable monetary cap."""
    return isinstance(error, ValueError) and str(error).startswith(
        "estimated DeFi transaction exceeds gas cap:")


def require_provider_retry_due(journal, position_id: int, *, now_seconds: float) -> None:
    """A known provider window must not trigger fresh unsigned quote requests."""
    if journal.connection.execute(
        "SELECT 1 FROM route_steps WHERE position_id=? AND step_key NOT LIKE 'approval:%' LIMIT 1",
        (position_id,),
    ).fetchone():
        return
    retry = journal.checkpoint(position_id, "automatic_retry") or {}
    retry_at = retry.get("retry_at")
    if (isinstance(retry_at, (int, float)) and not isinstance(retry_at, bool)
            and math.isfinite(retry_at) and now_seconds < retry_at):
        raise StageDeadlineExceeded("provider_retry_scheduled")
