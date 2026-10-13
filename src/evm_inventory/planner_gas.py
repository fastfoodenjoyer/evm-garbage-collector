"""Read-only RPC estimates for direct deposits and required approvals."""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import replace
from datetime import UTC, datetime
from threading import Lock

from eth_abi import decode
from eth_abi.exceptions import DecodingError

from .diagnostics import exception_diagnostic, redact_text
from .executor import approve_transaction, token_allowance
from .fee_planner import FeePlanner, native_asset_identity
from .lifi import LifiClient, LifiRoute, TransactionRequest
from .lifi_quota import LifiRequestLimit
from .models import AssetIdentity
from .quote_jobs import JobInProgress
from .relay_protocol import verified_transaction
from .rpc import RpcReader, quantity
from .stages import StageDeadlineExceeded
from .transport import is_transient_error
from .valuation import FeeQuote, QuotePrice

_TRANSFER_SELECTOR = "a9059cbb"
_UINT256_MAX = 2**256 - 1


def _approval_dependent_revert(
    error: ValueError, *, expected_spender: str | None = None, expected_amount: int | None = None,
) -> bool:
    text = str(error).lower()
    prefix = "estimate_gas_rpc_error:"
    if not text.startswith(prefix):
        return any(marker in text for marker in ("insufficient allowance", "transfer_from_failed"))
    try:
        failure = json.loads(str(error)[len(prefix):])
        diagnostic = failure.get("diagnostic")
        provider = diagnostic.get("provider_error") if isinstance(diagnostic, dict) else None
        provider = provider if isinstance(provider, dict) else {}
        data = provider.get("data")
        # LI.FI/Solady TransferFromFailed(): only structured RPC revert data qualifies.
        if data == "0x7939f424":
            return True
        if isinstance(data, str) and data.lower().startswith("0xfb8f41b2"):
            if (len(data) != 202 or not isinstance(expected_spender, str)
                    or isinstance(expected_amount, bool) or not isinstance(expected_amount, int)):
                return False
            spender, allowance, needed = decode(
                ("address", "uint256", "uint256"), bytes.fromhex(data[10:]), strict=True)
            return (spender.lower() == expected_spender.lower()
                    and allowance < needed == expected_amount)
        message = str(provider.get("message", failure.get("message", ""))).lower()
        return any(marker in message for marker in (
            "insufficient allowance", "transfer_from_failed"))
    except (ValueError, AttributeError, TypeError, DecodingError):
        return False


class PlannerGasEstimator:
    """Estimate gas using read-only RPC calls; never signs or broadcasts."""

    def __init__(
        self,
        rpc: RpcReader,
        rpc_urls: Mapping[int, str],
        price_client: LifiClient,
        *,
        retry_transient_errors: bool = False,
    ):
        self.rpc = rpc
        self.rpc_urls = rpc_urls
        self.price_client = price_client
        self.retry_transient_errors = retry_transient_errors
        self.diagnostics: list[dict] = []
        self._diagnostic_lock = Lock()
        self.prepared_fee_caps: dict[str, tuple[TransactionRequest, ...]] = {}
        self.approval_fee_caps: dict[str, int] = {}

    def _failure(self, route, phase: str, error: Exception) -> None:
        with self._diagnostic_lock:
            self.diagnostics.append(
                {
                    "route_id": route.route_id if route else None,
                    "phase": phase,
                    "error": type(error).__name__,
                    "detail": redact_text(str(error)),
                    "diagnostic": exception_diagnostic(error),
                }
            )

    def relay_fee_available(self, chain_id: int) -> bool:
        """Whether exact Relay transaction preparation has an RPC capability."""
        url = self.rpc_urls.get(chain_id)
        return isinstance(url, str) and bool(url) and callable(getattr(self.rpc, "call", None))

    def prepare_relay_fee(
        self, *, quote: dict, chain_catalog: list[dict], wallet: str, source_chain_id: int,
    ) -> TransactionRequest:
        """Validate and price the entire unsigned native order on its own RPC chain."""
        try:
            request = quote["request"]
            if request["user"].lower() != wallet.lower():
                raise ValueError("Relay fee preparation wallet differs from source wallet")
            if request["originChainId"] != source_chain_id:
                raise ValueError("Relay fee preparation chain differs from source chain")
            transaction = verified_transaction(quote, chain_catalog)
            url = self._url(source_chain_id)
            actual_chain = quantity(self.rpc.call(url, "eth_chainId", []))
            if actual_chain != source_chain_id:
                raise ValueError(
                    f"relay_rpc_chain_mismatch:expected={source_chain_id};actual={actual_chain}"
                )
            planned = FeePlanner(self.rpc).plan(url, transaction, sender=wallet)
            return replace(planned, max_total_fee_cap_wei=planned.max_total_fee_wei)
        except Exception as error:
            self._failure(None, "relay_prepare_transaction", error)
            raise

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
            prepared = tuple(self.price_client.step_transaction(step) for step in route.raw_steps)
            return self.estimate_prepared_route(
                route=route, wallet=wallet, prepared_transactions=prepared
            )
        except (StageDeadlineExceeded, LifiRequestLimit, JobInProgress):
            raise
        except Exception as error:
            self._failure(route, "prepare_transactions", error)
            if self.retry_transient_errors and is_transient_error(error):
                raise
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
                self._failure(route, "estimate", ValueError("incomplete route gas metadata"))
                return None
            return (*transaction_quotes, *approval_quotes)
        except (StageDeadlineExceeded, LifiRequestLimit, JobInProgress):
            raise
        except Exception as error:
            self._failure(route, "estimate", error)
            if self.retry_transient_errors and is_transient_error(error):
                raise
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
        fee_caps = []
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
            try:
                planned = FeePlanner(self.rpc).plan(url, request, sender=wallet)
            except ValueError as error:
                spender = step.get("estimate", {}).get("approvalAddress")
                token = action.get("fromToken", {}).get("address", "").lower()
                if (not _approval_dependent_revert(error, expected_spender=spender,
                                                 expected_amount=int(action["fromAmount"]))
                        or token in {"", "0x" + "0" * 40} or not isinstance(spender, str)
                        or token_allowance(self.rpc, url=url, token=token, owner=wallet,
                                           spender=spender) >= int(action["fromAmount"])
                        or request.gas_limit <= 0):
                    raise
                # This is a cap for staged preparation, not a successful RPC simulation.
                # FeePlanner's buffered network ceiling and fee cap bound large provider hints.
                # Execution confirms approval, then must simulate under this fee cap before signing.
                planned = FeePlanner(_PendingApprovalRpc(self.rpc, request.gas_limit * 2)).plan(
                    url, request, sender=wallet
                )
                planned = replace(planned, fee_quote_fallback_reason="pending_approval")
            planned = replace(planned, max_total_fee_cap_wei=planned.max_total_fee_wei)
            fee_caps.append(planned)
            result.append(self._price_planned(planned))
        with self._diagnostic_lock:
            self.prepared_fee_caps[route.route_id] = tuple(fee_caps)
        return tuple(result)

    def _estimate_direct(
        self, asset: AssetIdentity, wallet: str, amount: int, recipient: str
    ) -> FeeQuote:
        url = self._url(asset.chain_id)
        if asset.is_native:
            transaction = {
                "from": wallet,
                "to": recipient,
                # Execution chooses the sendable value after reserving gas.
                # Estimating the full observed balance can fail for insufficient funds.
                "value": "0x0",
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

    def _estimate_approvals(self, route: LifiRoute, wallet: str) -> tuple[FeeQuote, ...] | None:
        if route.evidence is None:
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
            if token.is_native:
                # LI.FI also supplies the router as approvalAddress for native inputs.
                # Native value is sent with the transaction and has no ERC20 allowance.
                continue
            if not isinstance(spender, str):
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
            fee = self._priced_estimate(
                    token.chain_id,
                    url,
                    {
                        "from": wallet,
                        "to": transaction.to,
                        "value": "0x0",
                        "data": transaction.data,
                    },
                )
            result.append(fee)
            with self._diagnostic_lock:
                key = f"{token.chain_id}:{token.contract_address}:{spender.lower()}"
                self.approval_fee_caps[key] = (
                    fee.raw_amount
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
        return self._price_planned(planned)

    def _price_planned(self, planned):
        native = native_asset_identity(planned.chain_id)
        evidence = self.price_client.token_price(native)
        if evidence.asset != native or evidence.price_usd is None:
            raise ValueError("native gas-token price is unavailable")
        observed = _timestamp(evidence.timestamp)
        price = QuotePrice(
            native, evidence.price_usd, observed, evidence.source, evidence.reported_price_usd
        )
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


class _PendingApprovalRpc:
    def __init__(self, rpc, gas_cap):
        self.rpc, self.gas_cap = rpc, gas_cap

    def call(self, url, method, params):
        if method == "eth_estimateGas":
            return hex(self.gas_cap)
        return self.rpc.call(url, method, params)
