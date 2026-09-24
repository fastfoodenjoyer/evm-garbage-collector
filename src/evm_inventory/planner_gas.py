"""Read-only RPC estimates for direct deposits and required approvals."""

from __future__ import annotations

from collections.abc import Mapping
from datetime import UTC, datetime

from .executor import approve_transaction, token_allowance
from .fee_planner import FeePlanner, native_asset_identity
from .lifi import LifiClient, LifiRoute, TransactionRequest
from .models import AssetIdentity
from .rpc import RpcReader, quantity
from .valuation import FeeQuote, QuotePrice

_TRANSFER_SELECTOR = "a9059cbb"
_UINT256_MAX = 2**256 - 1


class PlannerGasEstimator:
    """Estimate gas using read-only RPC calls; never signs or broadcasts."""

    def __init__(
        self,
        rpc: RpcReader,
        rpc_urls: Mapping[int, str],
        price_client: LifiClient,
    ):
        self.rpc = rpc
        self.rpc_urls = rpc_urls
        self.price_client = price_client

    def __call__(
        self,
        *,
        purpose: str,
        route: LifiRoute | None,
        wallet: str,
        source_asset: AssetIdentity,
        target,
        amount: int,
        recipient: str,
    ) -> tuple[FeeQuote, ...] | None:
        try:
            if route is None:
                return (self._estimate_direct(source_asset, wallet, amount, recipient),)
            prepared = tuple(
                self.price_client.step_transaction(step) for step in route.raw_steps
            )
            return self.estimate_prepared_route(
                route=route, wallet=wallet, prepared_transactions=prepared
            )
        except Exception:
            # Gas evidence is an admission requirement; an RPC/API failure
            # rejects this candidate instead of stopping the whole plan.
            return None

    def estimate_prepared_route(
        self,
        *,
        route: LifiRoute,
        wallet: str,
        prepared_transactions: tuple[TransactionRequest, ...],
    ) -> tuple[FeeQuote, ...] | None:
        """Price the exact unsigned route transactions that execution will use."""

        try:
            transaction_quotes = self._estimate_route_transactions(
                route, wallet, prepared_transactions
            )
            approval_quotes = self._estimate_approvals(route, wallet)
            if transaction_quotes is None or approval_quotes is None:
                return None
            return (*transaction_quotes, *approval_quotes)
        except Exception:
            return None

    def _estimate_route_transactions(
        self,
        route: LifiRoute,
        wallet: str,
        prepared_transactions: tuple[TransactionRequest, ...],
    ) -> tuple[FeeQuote, ...] | None:
        if (
            route.evidence is None
            or not route.raw_steps
            or len(prepared_transactions) != len(route.raw_steps)
        ):
            return None
        result = []
        for step, request in zip(route.raw_steps, prepared_transactions, strict=True):
            action = step.get("action")
            if not isinstance(action, dict):
                return None
            chain_id = action.get("fromChainId")
            if isinstance(chain_id, bool) or not isinstance(chain_id, int):
                return None
            if request.chain_id != chain_id:
                return None
            url = self._url(chain_id)
            result.append(
                self._priced_estimate(
                    chain_id,
                    url,
                    {
                        "from": wallet,
                        "to": request.to,
                        "value": hex(request.value),
                        "data": request.data,
                    },
                )
            )
        return tuple(result)

    def _estimate_direct(
        self, asset: AssetIdentity, wallet: str, amount: int, recipient: str
    ) -> FeeQuote:
        url = self._url(asset.chain_id)
        if asset.is_native:
            transaction = {
                "from": wallet,
                "to": recipient,
                "value": hex(amount),
                "data": "0x",
            }
        else:
            transaction = {
                "from": wallet,
                "to": asset.contract_address,
                "value": "0x0",
                "data": _transfer_data(recipient, amount),
            }
        return self._priced_estimate(asset.chain_id, url, transaction)

    def _estimate_approvals(
        self, route: LifiRoute, wallet: str
    ) -> tuple[FeeQuote, ...] | None:
        if route.evidence is None:
            return None
        if any(_nested_approval(step) for step in route.raw_steps):
            return None
        result: list[FeeQuote] = []
        top_level = tuple(step for step in route.evidence.steps if not step.is_substep)
        if len(top_level) != len(route.raw_steps):
            return None
        for step, evidence in zip(route.raw_steps, top_level, strict=True):
            estimate = step.get("estimate")
            action = step.get("action")
            if not isinstance(estimate, dict) or not estimate.get("approvalAddress"):
                continue
            if not isinstance(action, dict) or evidence.source is None:
                return None
            spender = estimate["approvalAddress"]
            token = evidence.source
            if token.is_native or not isinstance(spender, str):
                return None
            amount = evidence.from_amount
            if amount is None:
                return None
            url = self._url(token.chain_id)
            allowance = token_allowance(
                self.rpc,
                url=url,
                token=token.contract_address,
                owner=wallet,
                spender=spender,
            )
            if allowance >= amount:
                continue
            transaction = approve_transaction(
                chain_id=token.chain_id,
                token=token.contract_address,
                spender=spender,
                amount=_UINT256_MAX,
                gas_price_wei=0,
            )
            result.append(
                self._priced_estimate(
                    token.chain_id,
                    url,
                    {
                        "from": wallet,
                        "to": transaction.to,
                        "value": "0x0",
                        "data": transaction.data,
                    },
                )
            )
        return tuple(result)

    def _priced_estimate(self, chain_id: int, url: str, transaction: dict) -> FeeQuote:
        sender = transaction.get("from")
        recipient = transaction.get("to")
        data = transaction.get("data", "0x")
        if (
            not isinstance(sender, str)
            or not isinstance(recipient, str)
            or not isinstance(data, str)
        ):
            raise ValueError("gas estimate transaction is incomplete")
        value = transaction.get("value", "0x0")
        if isinstance(value, str):
            value = quantity(value)
        if not isinstance(value, int) or isinstance(value, bool) or value < 0:
            raise ValueError("gas estimate transaction value is invalid")
        planned = FeePlanner(self.rpc).plan(
            url,
            TransactionRequest(chain_id, recipient, data, value, 0, 0),
            sender=sender,
        )
        native = native_asset_identity(chain_id)
        evidence = self.price_client.token_price(native)
        if evidence.asset != native or evidence.price_usd is None:
            raise ValueError("native gas-token price is unavailable")
        observed = _timestamp(evidence.timestamp)
        price = QuotePrice(native, evidence.price_usd, observed)
        return FeeQuote(planned.max_total_fee_wei, price)

    def _url(self, chain_id: int) -> str:
        url = self.rpc_urls.get(chain_id)
        if not isinstance(url, str) or not url:
            raise ValueError("RPC URL is unavailable for gas estimation")
        return url


def _transfer_data(recipient: str, amount: int) -> str:
    if len(recipient) != 42 or not recipient.startswith("0x"):
        raise ValueError("deposit recipient is invalid")
    int(recipient[2:], 16)
    if amount < 0 or amount >= 2**256:
        raise ValueError("transfer amount is invalid")
    return (
        "0x"
        + _TRANSFER_SELECTOR
        + recipient[2:].lower().rjust(64, "0")
        + hex(amount)[2:].rjust(64, "0")
    )


def _nested_approval(step: dict, *, nested: bool = False) -> bool:
    estimate = step.get("estimate")
    if nested and isinstance(estimate, dict) and estimate.get("approvalAddress"):
        return True
    for key in ("includedSteps", "steps", "substeps"):
        children = step.get(key)
        if not isinstance(children, list):
            continue
        for child in children:
            if isinstance(child, dict) and _nested_approval(child, nested=True):
                return True
    return False


def _timestamp(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError("gas-token price timestamp must be timezone-aware")
    return parsed.astimezone(UTC)
