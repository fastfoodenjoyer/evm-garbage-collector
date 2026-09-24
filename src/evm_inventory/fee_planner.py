"""Dynamic EVM transaction gas and fee planning."""

from __future__ import annotations

import json
from dataclasses import dataclass, replace
from functools import lru_cache
from statistics import median
from typing import Protocol

import rlp
from eth_abi import encode
from eth_utils import function_signature_to_4byte_selector

from .config import load_catalog
from .lifi import TransactionRequest
from .models import AssetIdentity
from .rpc import quantity, uint256

_OP_STACK_CHAIN_IDS = frozenset(
    {
        10,
        130,
        204,
        252,
        254,
        288,
        291,
        360,
        480,
        690,
        957,
        1135,
        1750,
        1868,
        5330,
        5371,
        6805,
        7560,
        8008,
        8453,
        33979,
        34443,
        57073,
        60808,
        81457,
        380929,
        2702128,
        7777777,
    }
)


@lru_cache(maxsize=1)
def _native_decimals_by_chain() -> dict[int, int]:
    catalog = load_catalog()
    return {network.chain_id: network.native_decimals for network in catalog.networks}


def native_asset_identity(chain_id: int) -> AssetIdentity:
    decimals = _native_decimals_by_chain().get(chain_id)
    if decimals is None:
        raise ValueError(f"native_asset_metadata_unavailable:chain_id={chain_id}")
    return AssetIdentity(chain_id, "native", decimals)


class FeeRpc(Protocol):
    def call(self, url: str, method: str, params: list[object]) -> object: ...


@dataclass(frozen=True, slots=True)
class FeePolicy:
    gas_buffer_numerator: int = 12
    gas_buffer_denominator: int = 10
    max_gas_limit: int = 30_000_000
    base_fee_horizon_blocks: int = 2
    fee_history_blocks: int = 5
    priority_percentile: int = 50
    minimum_priority_fee_wei: int = 0

    def __post_init__(self) -> None:
        if (
            self.gas_buffer_numerator < self.gas_buffer_denominator
            or self.gas_buffer_denominator <= 0
        ):
            raise ValueError("invalid_gas_buffer")
        if self.max_gas_limit <= 0 or self.base_fee_horizon_blocks < 0:
            raise ValueError("invalid_fee_policy")
        if self.fee_history_blocks <= 0 or not 0 <= self.priority_percentile <= 100:
            raise ValueError("invalid_fee_history_policy")
        if self.minimum_priority_fee_wei < 0:
            raise ValueError("invalid_minimum_priority_fee")


class FeePlanner:
    """Estimate exact transaction gas and choose a chain-compatible fee model."""

    def __init__(self, rpc: FeeRpc, *, policy: FeePolicy | None = None):
        self.rpc = rpc
        self.policy = policy or FeePolicy()

    def plan(
        self,
        url: str,
        request: TransactionRequest,
        *,
        sender: str,
        max_total_fee_wei: int | None = None,
    ) -> TransactionRequest:
        if not sender.startswith("0x") or len(sender) != 42:
            raise ValueError("invalid_fee_sender")
        transaction = {
            "from": sender,
            "to": request.to,
            "data": request.data,
            "value": hex(request.value),
        }
        fee_cap = (
            max_total_fee_wei if max_total_fee_wei is not None else request.max_total_fee_cap_wei
        )
        if fee_cap is not None and fee_cap <= 0:
            raise ValueError("invalid_total_fee_cap")
        try:
            estimate = quantity(self.rpc.call(url, "eth_estimateGas", [transaction, "latest"]))
        except Exception as exc:
            raise ValueError(f"estimate_gas_rpc_error:{_rpc_failure_detail(exc)}") from exc
        if estimate <= 0:
            raise ValueError("invalid_gas_estimate")
        gas_limit = (
            estimate * self.policy.gas_buffer_numerator // self.policy.gas_buffer_denominator + 1
        )
        if gas_limit > self.policy.max_gas_limit:
            raise ValueError(
                f"gas_limit_exceeds_network_ceiling:estimate={estimate};"
                f"buffered={gas_limit};ceiling={self.policy.max_gas_limit}"
            )

        header = self.rpc.call(url, "eth_getBlockByNumber", ["latest", False])
        if not isinstance(header, dict):
            raise ValueError("latest_block_unavailable_for_fee_quote")
        try:
            quote_block_number = quantity(header.get("number"))
        except Exception as exc:
            raise ValueError("invalid_latest_block_number_for_fee_quote") from exc
        base_fee_value = header.get("baseFeePerGas")
        if base_fee_value is None:
            gas_price = self._legacy_price(url)
            additional_fee = self._op_stack_l1_fee(
                url,
                request,
                gas_limit=gas_limit,
                max_fee_per_gas=gas_price,
                priority_fee_per_gas=0,
                eip1559=False,
            )
            planned = replace(
                request,
                gas_limit=gas_limit,
                gas_price_wei=gas_price,
                max_fee_per_gas_wei=None,
                max_priority_fee_per_gas_wei=None,
                additional_fee_wei=additional_fee,
                gas_estimate=estimate,
                fee_quote_block_number=quote_block_number,
                fee_quote_method=(
                    "eth_gasPrice+op_l1_fee_upper_bound" if additional_fee else "eth_gasPrice"
                ),
                max_total_fee_cap_wei=fee_cap,
            )
            return self._check_fee_cap(planned)
        try:
            base_fee = quantity(base_fee_value)
        except Exception as exc:
            raise ValueError("invalid_latest_base_fee") from exc
        if base_fee <= 0:
            raise ValueError("invalid_latest_base_fee")

        priority_fee, source, fallback_reason = self._priority_fee(url)
        if source == "eth_gasPrice_fallback":
            max_fee = priority_fee
            eip1559 = False
        else:
            projected_base_fee = base_fee
            for _ in range(self.policy.base_fee_horizon_blocks):
                projected_base_fee = (projected_base_fee * 9 + 7) // 8
            max_fee = projected_base_fee + priority_fee
            eip1559 = True
        additional_fee = self._op_stack_l1_fee(
            url,
            request,
            gas_limit=gas_limit,
            max_fee_per_gas=max_fee,
            priority_fee_per_gas=priority_fee,
            eip1559=eip1559,
        )
        if additional_fee:
            source = f"{source}+op_l1_fee_upper_bound"
        planned = replace(
            request,
            gas_limit=gas_limit,
            gas_price_wei=None if eip1559 else max_fee,
            max_fee_per_gas_wei=max_fee if eip1559 else None,
            max_priority_fee_per_gas_wei=priority_fee if eip1559 else None,
            additional_fee_wei=additional_fee,
            gas_estimate=estimate,
            fee_quote_block_number=quote_block_number,
            base_fee_per_gas_wei=base_fee,
            fee_quote_method=source,
            fee_quote_fallback_reason=fallback_reason,
            max_total_fee_cap_wei=fee_cap,
        )
        return self._check_fee_cap(planned)

    def _op_stack_l1_fee(
        self,
        url: str,
        request: TransactionRequest,
        *,
        gas_limit: int,
        max_fee_per_gas: int,
        priority_fee_per_gas: int,
        eip1559: bool,
    ) -> int:
        if request.chain_id not in _OP_STACK_CHAIN_IDS:
            return 0
        unsigned = _unsigned_transaction_payload(
            request,
            gas_limit=gas_limit,
            eip1559=eip1559,
            max_fee_per_gas=max_fee_per_gas,
            priority_fee_per_gas=priority_fee_per_gas,
        )
        oracle = "0x420000000000000000000000000000000000000f"
        upper_bound_selector = function_signature_to_4byte_selector("getL1FeeUpperBound(uint256)")
        try:
            result = self.rpc.call(
                url,
                "eth_call",
                [
                    {
                        "to": oracle,
                        "data": "0x"
                        + (upper_bound_selector + encode(["uint256"], [len(unsigned)])).hex(),
                    },
                    "latest",
                ],
            )
            fee = uint256(result)
            if fee > 0:
                return fee
        except Exception:
            pass

        # Pre-Fjord OP Stack networks expose getL1Fee(bytes) instead of the
        # size-only upper-bound helper. The predeploy accepts unsigned RLP.
        legacy_selector = function_signature_to_4byte_selector("getL1Fee(bytes)")
        try:
            result = self.rpc.call(
                url,
                "eth_call",
                [
                    {
                        "to": oracle,
                        "data": "0x" + (legacy_selector + encode(["bytes"], [unsigned])).hex(),
                    },
                    "latest",
                ],
            )
            fee = uint256(result)
            if fee > 0:
                return fee
        except Exception as exc:
            raise ValueError(f"op_stack_l1_fee_estimation_failed:{type(exc).__name__}") from exc
        raise ValueError("op_stack_l1_fee_estimation_failed:empty_result")

    @staticmethod
    def _check_fee_cap(request: TransactionRequest) -> TransactionRequest:
        if (
            request.max_total_fee_cap_wei is not None
            and request.max_total_fee_wei > request.max_total_fee_cap_wei
        ):
            raise ValueError(
                "estimated DeFi transaction exceeds gas cap:"
                f"estimate_gas={request.gas_estimate};gas_limit={request.gas_limit};"
                f"gas_price_wei={request.gas_price_wei};"
                f"max_fee_per_gas_wei={request.max_fee_per_gas_wei};"
                f"max_priority_fee_per_gas_wei={request.max_priority_fee_per_gas_wei};"
                f"additional_fee_wei={request.additional_fee_wei};"
                f"estimated_total_fee_wei={request.max_total_fee_wei};"
                f"cap_wei={request.max_total_fee_cap_wei};"
                f"quote={request.fee_quote_method};"
                f"fallback_reason={request.fee_quote_fallback_reason}"
            )
        return request

    def _priority_fee(self, url: str) -> tuple[int, str, str | None]:
        failures: list[str] = []
        try:
            history = self.rpc.call(
                url,
                "eth_feeHistory",
                [hex(self.policy.fee_history_blocks), "latest", [self.policy.priority_percentile]],
            )
            if isinstance(history, dict) and isinstance(history.get("reward"), list):
                rewards = [
                    quantity(row[0])
                    for row in history["reward"]
                    if isinstance(row, list) and row and row[0] is not None
                ]
                if rewards:
                    return (
                        max(self.policy.minimum_priority_fee_wei, int(median(rewards))),
                        "eth_feeHistory",
                        None,
                    )
            failures.append("eth_feeHistory returned no usable reward percentiles")
        except Exception as exc:
            failures.append(f"eth_feeHistory:{_rpc_failure_detail(exc)}")
        try:
            tip = quantity(self.rpc.call(url, "eth_maxPriorityFeePerGas", []))
            if tip >= 0:
                return (
                    max(self.policy.minimum_priority_fee_wei, tip),
                    "eth_maxPriorityFeePerGas",
                    ";".join(failures),
                )
            failures.append("eth_maxPriorityFeePerGas returned a negative value")
        except Exception as exc:
            failures.append(f"eth_maxPriorityFeePerGas:{_rpc_failure_detail(exc)}")
        # Legacy transactions remain valid on EIP-1559 chains and are a safe
        # interoperability fallback when a provider omits fee-market methods.
        return self._legacy_price(url), "eth_gasPrice_fallback", ";".join(failures)

    def _legacy_price(self, url: str) -> int:
        try:
            gas_price = quantity(self.rpc.call(url, "eth_gasPrice", []))
        except Exception as exc:
            raise ValueError(f"gas_price_rpc_error:{_rpc_failure_detail(exc)}") from exc
        if gas_price <= 0:
            raise ValueError("invalid_rpc_gas_price")
        return gas_price


def _rpc_failure_detail(exc: Exception) -> str:
    detail = {
        "type": type(exc).__name__,
        "code": getattr(exc, "code", None),
        "message": str(exc),
        "diagnostic": getattr(exc, "diagnostic", None),
    }
    return json.dumps(detail, sort_keys=True, separators=(",", ":"), default=str)


def _unsigned_transaction_payload(
    request: TransactionRequest,
    *,
    gas_limit: int,
    eip1559: bool,
    max_fee_per_gas: int,
    priority_fee_per_gas: int,
) -> bytes:
    """Encode a conservative unsigned transaction size for the OP fee oracle."""

    def integer(value: int) -> bytes:
        return value.to_bytes((value.bit_length() + 7) // 8, "big")

    to = bytes.fromhex(request.to[2:])
    data = bytes.fromhex(request.data[2:])
    # A wide nonce makes the size quote safe before the actual pending nonce is
    # read by the signer. The upper-bound predeploy accounts for signature bytes.
    nonce = integer(2**64 - 1)
    if eip1559:
        fields = [
            integer(request.chain_id),
            nonce,
            integer(priority_fee_per_gas),
            integer(max_fee_per_gas),
            integer(gas_limit),
            to,
            integer(request.value),
            data,
            [],
        ]
        return b"\x02" + rlp.encode(fields)
    fields = [
        nonce,
        integer(max_fee_per_gas),
        integer(gas_limit),
        to,
        integer(request.value),
        data,
        integer(request.chain_id),
        b"",
        b"",
    ]
    return rlp.encode(fields)
