from dataclasses import replace

import pytest

from evm_inventory.fee_planner import FeePlanner, FeePolicy
from evm_inventory.lifi import TransactionRequest
from evm_inventory.rpc import RpcError


class StubRpc:
    def __init__(self, results):
        self.results = results
        self.calls = []

    def call(self, url, method, params):
        self.calls.append((url, method, params))
        result = self.results[method]
        if isinstance(result, Exception):
            raise result
        return result


def _request() -> TransactionRequest:
    return TransactionRequest(10, "0x" + "1" * 40, "0x12345678", 7, 0, 0)


def test_plans_eip1559_fee_and_buffered_gas_limit():
    rpc = StubRpc(
        {
            "eth_estimateGas": "0x186a0",
            "eth_getBlockByNumber": {"number": "0xa", "baseFeePerGas": "0x64"},
            "eth_feeHistory": {
                "baseFeePerGas": ["0x64", "0x70", "0x7e"],
                "reward": [["0x5"], ["0x7"], ["0x9"]],
            },
            "eth_call": "0x" + f"{1:064x}",
        }
    )

    planned = FeePlanner(rpc).plan("https://rpc", _request(), sender="0x" + "2" * 40)

    assert planned.gas_limit == 120_001
    assert planned.gas_price_wei is None
    assert planned.max_priority_fee_per_gas_wei == 7
    assert planned.max_fee_per_gas_wei == 135
    assert planned.max_total_fee_wei == 120_001 * 135 + 1
    assert planned.additional_fee_wei == 1
    assert planned.fee_quote_method == "eth_feeHistory+op_l1_fee_upper_bound"
    assert [call[1] for call in rpc.calls] == [
        "eth_estimateGas",
        "eth_getBlockByNumber",
        "eth_feeHistory",
        "eth_call",
    ]


def test_plans_legacy_price_when_block_has_no_base_fee():
    rpc = StubRpc(
        {
            "eth_estimateGas": "0x5208",
            "eth_getBlockByNumber": {"number": "0xa"},
            "eth_gasPrice": "0x14",
            "eth_call": "0x" + f"{1:064x}",
        }
    )

    planned = FeePlanner(rpc).plan("https://rpc", _request(), sender="0x" + "2" * 40)

    assert planned.gas_limit == 25_201
    assert planned.gas_price_wei == 20
    assert planned.max_fee_per_gas_wei is None
    assert planned.max_total_fee_wei == 25_201 * 20 + 1


def test_eip1559_rpc_gap_falls_back_to_legacy_fee_quote():
    rpc = StubRpc(
        {
            "eth_estimateGas": "0x5208",
            "eth_getBlockByNumber": {"number": "0xa", "baseFeePerGas": "0x64"},
            "eth_feeHistory": RuntimeError("unsupported method"),
            "eth_maxPriorityFeePerGas": RuntimeError("unsupported method"),
            "eth_gasPrice": "0x1e",
            "eth_call": "0x" + f"{1:064x}",
        }
    )

    planned = FeePlanner(rpc).plan("https://rpc", _request(), sender="0x" + "2" * 40)

    assert planned.gas_price_wei == 30
    assert planned.max_fee_per_gas_wei is None
    assert planned.max_priority_fee_per_gas_wei is None


def test_gas_limit_policy_is_a_ceiling_not_a_replacement_estimate():
    rpc = StubRpc(
        {
            "eth_estimateGas": "0x186a0",
            "eth_getBlockByNumber": {"number": "0xa"},
            "eth_gasPrice": "0x1",
        }
    )
    policy = replace(FeePolicy(), max_gas_limit=100_000)

    with pytest.raises(ValueError, match="gas_limit_exceeds_network_ceiling"):
        FeePlanner(rpc, policy=policy).plan("https://rpc", _request(), sender="0x" + "2" * 40)


def test_invalid_rpc_gas_estimate_fails_closed():
    rpc = StubRpc(
        {
            "eth_estimateGas": "0x0",
            "eth_getBlockByNumber": {"number": "0xa"},
            "eth_gasPrice": "0x1",
        }
    )

    with pytest.raises(ValueError, match="invalid_gas_estimate"):
        FeePlanner(rpc).plan("https://rpc", _request(), sender="0x" + "2" * 40)


def test_gas_estimation_error_retains_rpc_provider_diagnostic():
    rpc = StubRpc(
        {
            "eth_estimateGas": RpcError(
                "rpc_error",
                diagnostic={
                    "method": "eth_estimateGas",
                    "provider_error": {
                        "code": 3,
                        "message": "execution reverted: insufficient native balance",
                    },
                },
            ),
        }
    )

    with pytest.raises(ValueError, match="insufficient native balance"):
        FeePlanner(rpc).plan("https://rpc", _request(), sender="0x" + "2" * 40)


def test_op_stack_l1_data_fee_is_added_to_total_fee_upper_bound():
    rpc = StubRpc(
        {
            "eth_estimateGas": "0x5208",
            "eth_getBlockByNumber": {"number": "0xa", "baseFeePerGas": "0x64"},
            "eth_feeHistory": {"reward": [["0x5"], ["0x5"], ["0x5"]]},
            "eth_call": "0x" + f"{77:064x}",
        }
    )

    planned = FeePlanner(rpc).plan("https://rpc", _request(), sender="0x" + "2" * 40)

    assert planned.additional_fee_wei == 77
    assert planned.max_total_fee_wei == planned.gas_limit * planned.max_fee_per_gas_wei + 77
    assert planned.fee_quote_method == "eth_feeHistory+op_l1_fee_upper_bound"
    assert rpc.calls[-1][1] == "eth_call"


def test_fee_cap_checks_execution_and_op_l1_fee_components_together():
    rpc = StubRpc(
        {
            "eth_estimateGas": "0x5208",
            "eth_getBlockByNumber": {"number": "0xa"},
            "eth_gasPrice": "0x2",
            "eth_call": "0x" + f"{1000:064x}",
        }
    )
    execution_fee = 25_201 * 2

    with pytest.raises(ValueError, match="additional_fee_wei=1000") as exc_info:
        FeePlanner(rpc).plan(
            "https://rpc",
            _request(),
            sender="0x" + "2" * 40,
            max_total_fee_wei=execution_fee + 999,
        )

    assert f"estimated_total_fee_wei={execution_fee + 1000}" in str(exc_info.value)
