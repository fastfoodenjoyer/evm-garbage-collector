from datetime import UTC, datetime

from evm_inventory.lifi import LifiPriceEvidence, TransactionRequest, _route_from_dict
from evm_inventory.models import AssetIdentity
from evm_inventory.planner_gas import PlannerGasEstimator

NOW = datetime(2026, 9, 22, 12, tzinfo=UTC)
WALLET = "0x" + "1" * 40
RECIPIENT = "0x" + "2" * 40
TOKEN = "0x" + "a" * 40
SPENDER = "0x" + "b" * 40


class Rpc:
    def __init__(self):
        self.calls = []

    def call(self, url, method, params):
        self.calls.append((url, method, params))
        if method == "eth_estimateGas":
            return "0x5208"
        if method == "eth_gasPrice":
            return "0x3b9aca00"
        if method == "eth_call":
            return "0x" + "0" * 64
        raise AssertionError(method)


class PriceClient:
    def token_price(self, asset):
        return LifiPriceEvidence(asset, "2000", NOW.isoformat())

    def step_transaction(self, _step):
        return TransactionRequest(10, "0x" + "d" * 40, "0x1234", 0, 50_000, 1)


def test_planner_gas_estimator_quotes_direct_erc20_transfer():
    rpc = Rpc()
    estimator = PlannerGasEstimator(rpc, {10: "https://rpc.example"}, PriceClient())

    estimates = estimator(
        purpose="direct_deposit",
        route=None,
        wallet=WALLET,
        source_asset=AssetIdentity(10, TOKEN, 6),
        target=None,
        amount=123456,
        recipient=RECIPIENT,
    )

    assert estimates is not None and len(estimates) == 1
    assert estimates[0].raw_amount == 21_000 * 1_000_000_000
    request = rpc.calls[0][2][0]
    assert request["from"] == WALLET
    assert request["to"] == TOKEN
    assert request["data"] == (
        "0xa9059cbb"
        + RECIPIENT[2:].rjust(64, "0")
        + hex(123456)[2:].rjust(64, "0")
    )


def test_planner_gas_estimator_prices_required_approval():
    rpc = Rpc()
    estimator = PlannerGasEstimator(rpc, {10: "https://rpc.example"}, PriceClient())
    route = _route_from_dict(
        {
            "id": "approval-route",
            "fromAmount": "1000",
            "toAmount": "900",
            "toAmountMin": "850",
            "gasCosts": [],
            "steps": [
                {
                    "id": "step",
                    "type": "swap",
                    "tool": "provider",
                    "action": {
                        "fromChainId": 10,
                        "toChainId": 10,
                        "fromToken": {"address": TOKEN, "decimals": 6},
                        "toToken": {"address": "0x" + "c" * 40, "decimals": 6},
                        "fromAmount": "1000",
                        "toAddress": WALLET,
                    },
                    "estimate": {"toAmount": "900", "approvalAddress": SPENDER},
                }
            ],
        }
    )

    estimates = estimator(
        purpose="swap_to_native",
        route=route,
        wallet=WALLET,
        source_asset=AssetIdentity(10, TOKEN, 6),
        target=None,
        amount=1000,
        recipient=WALLET,
    )

    assert estimates is not None and len(estimates) == 2
    assert all(item.raw_amount == 21_000 * 1_000_000_000 for item in estimates)
    assert [method for _url, method, _params in rpc.calls] == [
        "eth_estimateGas",
        "eth_gasPrice",
        "eth_call",
        "eth_estimateGas",
        "eth_gasPrice",
    ]
    route_transaction = rpc.calls[0][2][0]
    assert route_transaction["to"] == "0x" + "d" * 40
    approval = rpc.calls[3][2][0]
    assert approval["to"] == TOKEN
    assert approval["data"].startswith("0x095ea7b3")
    assert approval["data"][34:74] == SPENDER[2:]
