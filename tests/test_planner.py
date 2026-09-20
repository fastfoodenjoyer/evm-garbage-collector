from evm_inventory.gas import native_transfer_gas_quote
from evm_inventory.planner import direct_native_deposit_plan, native_spendable_amount


def test_native_spendable_amount_keeps_five_times_estimated_gas():
    balance = 1_000_000

    plan = native_spendable_amount(balance, gas_limit=21_000, gas_price_wei=4)

    assert plan.gas_reserve_wei == 420_000
    assert plan.spendable_wei == 580_000
    assert plan.gas_reserve_multiplier == 5


def test_native_spendable_amount_refuses_amount_below_gas_reserve():
    plan = native_spendable_amount(419_999, gas_limit=21_000, gas_price_wei=4)

    assert plan.spendable_wei == 0


def test_direct_native_deposit_requires_minimum_after_five_x_gas_reserve():
    plan = direct_native_deposit_plan(
        balance_wei=1_000_000,
        gas_limit=21_000,
        gas_price_wei=4,
        minimum_deposit_wei=600_000,
    )

    assert plan.status == "below_minimum_after_gas_reserve"
    assert plan.send_amount_wei == 0
    assert plan.gas_reserve_wei == 420_000


class Rpc:
    def __init__(self):
        self.calls = []

    def check_chain(self, url, chain_id):
        self.checked = (url, chain_id)

    def call(self, url, method, params):
        self.calls.append((url, method, params))
        return {"eth_estimateGas": "0x5208", "eth_gasPrice": "0x4"}[method]


def test_native_transfer_gas_quote_uses_estimation_and_current_gas_price():
    rpc = Rpc()
    quote = native_transfer_gas_quote(
        rpc,
        url="https://rpc.example",
        chain_id=10,
        sender="0x" + "1" * 40,
        recipient="0x" + "2" * 40,
        value_wei=1,
    )

    assert quote.gas_limit == 21_000
    assert quote.gas_price_wei == 4
    assert rpc.calls[0][1] == "eth_estimateGas"
