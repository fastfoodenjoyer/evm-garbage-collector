import copy
from datetime import UTC, datetime
from decimal import Decimal
from types import SimpleNamespace

import pytest
from eth_abi import encode
from eth_account._utils.encode_typed_data.encoding_and_hashing import hash_struct
from eth_utils import keccak
from test_relay_protocol import CHAINS, DEPOSITORY, NOW, RECIPIENT, USDC, WALLET, _quote

from evm_inventory.bitget_catalog import BitgetDepositTarget
from evm_inventory.lifi_quota import LifiRequestLimit
from evm_inventory.planner_gas import PlannerGasEstimator
from evm_inventory.relay import quote_relay_entry
from evm_inventory.relay_protocol import ORDER_TYPES

BASE = {"wallet": WALLET, "chain_id": 1868, "asset_id": "native", "decimals": 18,
        "symbol": "ETH", "raw_balance": "10000"}
TARGET = BitgetDepositTarget("USDC", 8453, USDC, 100, "BASE")
OTHER = BitgetDepositTarget("USDC", 42161, USDC, 100, "ARBITRUM")


class FakeRpc:
    def __init__(self, estimates=(100,), *, price=1, l1_fee=5, chain_id=1868, error=None):
        self.estimates = iter(estimates)
        self.estimate = estimates[-1]
        self.price = price
        self.l1_fee = l1_fee
        self.chain_id = chain_id
        self.error = error
        self.calls = []
        self.balance = 10000

    def call(self, url, method, params):
        assert url == "fake-rpc"
        self.calls.append((method, copy.deepcopy(params)))
        if self.error is not None:
            raise self.error
        if method == "eth_chainId":
            return hex(self.chain_id)
        if method == "eth_getBalance":
            return hex(self.balance)
        if method == "eth_estimateGas":
            self.estimate = next(self.estimates, self.estimate)
            return hex(self.estimate)
        if method == "eth_getBlockByNumber":
            return {"number": "0x1"}
        if method == "eth_gasPrice":
            return hex(self.price)
        if method == "eth_call":
            return "0x" + f"{self.l1_fee:064x}"
        pytest.fail("Unexpected RPC operation: " + method)


def estimator(rpc=None, *, urls=None):
    return PlannerGasEstimator(rpc or FakeRpc(), urls if urls is not None else {1868: "fake-rpc"},
                               SimpleNamespace())


class Quotes:
    chain_catalog = CHAINS

    def __init__(self, *, hint=1, received_pct="98", minimum_raw=1000, failures=None):
        self.hint = hint
        self.received_pct = Decimal(received_pct)
        self.minimum_raw = minimum_raw
        self.failures = failures or {}
        self.calls = []

    def supported_chain_ids(self):
        return {1868, 8453, 42161}

    def quote(self, base, target, amount, **kwargs):
        self.calls.append((target.chain_id, amount))
        failure = self.failures.get((target.chain_id, amount), self.failures.get(target.chain_id))
        if failure:
            raise failure
        quote = _quote(amount=amount)
        quote["observed_at"] = datetime.fromtimestamp(NOW, UTC).isoformat()
        quote.update(source_usd=str(Decimal(amount) / 1000),
                     minimum_output_usd=str(Decimal(amount) / 1000 * self.received_pct / 100),
                     source_gas_raw=str(self.hint), source_gas_usd=str(self.hint / 1000),
                     to_amount=str(self.minimum_raw + 10), to_amount_min=str(self.minimum_raw))
        request = quote["request"]
        request.update(destinationChainId=target.chain_id, recipient=kwargs.get("recipient"))
        protocol = quote["response"]["protocol"]["v2"]
        order = protocol["orderData"]
        order["output"]["chainId"] = "base" if target.chain_id == 8453 else "arbitrum"
        payment = order["output"]["payments"][0]
        payment.update(minimumAmount=quote["to_amount_min"], expectedAmount=quote["to_amount"])
        order["inputs"][0]["refunds"] = order["inputs"][0]["refunds"][:1]
        order_id = "0x" + hash_struct("Order", ORDER_TYPES, order).hex()
        protocol["orderId"] = order_id
        transaction = quote["response"]["steps"][0]["items"][0]["data"]
        transaction["data"] = "0x" + (keccak(text="depositNative(address,bytes32)")[:4] + encode(
            ["address", "bytes32"], [WALLET, bytes.fromhex(order_id[2:])])).hex()
        return quote


def plan(client=None, gas=None, *, targets=(TARGET,), balance=10000):
    return quote_relay_entry(
        {**BASE, "raw_balance": str(balance)}, client or Quotes(), targets,
        loss_limit=Decimal("15"), quote_floor=Decimal("0.01"), deposit_address=RECIPIENT,
        gas_estimator=gas,
    )


@pytest.fixture(autouse=True)
def clock(monkeypatch):
    monkeypatch.setattr("evm_inventory.relay_protocol.time.time", lambda: NOW)


def test_provider_high_gas_hint_does_not_hide_exact_rpc_loss_refusal():
    rpc = FakeRpc()
    quotes = Quotes(hint=9000, received_pct="74")
    entry = plan(quotes, estimator(rpc))
    assert entry["reason"] == "max_route_loss_exceeded"
    candidate = entry["relay_candidates"][0]
    assert candidate["source_gas_raw"] == "126"
    assert candidate["source_gas_provider_estimate_raw"] == "9000"
    assert candidate["source_gas_estimate_verified"] is True
    assert Decimal(candidate["loss_pct"]) > 26
    assert candidate["source_balance_usd"] == "10"
    assert [amount for _, amount in quotes.calls] == [10000, 9848]
    estimates = [params for method, params in rpc.calls if method == "eth_estimateGas"]
    assert [int(params[0]["value"], 16) for params in estimates] == [10000, 9848]
    assert all(params[0]["from"] == WALLET and params[0]["to"] == DEPOSITORY
               and params[0]["data"].startswith("0x") and len(params) == 2 for params in estimates)


def test_provider_low_hint_is_replaced_by_exact_unaffordable_fee_diagnostics():
    entry = plan(Quotes(hint=1), estimator(FakeRpc(price=100)), balance=10000)
    assert entry["reason"] == "insufficient_source_gas"
    candidate = entry["relay_candidates"][0]
    assert candidate["reason"] == "insufficient_source_gas"
    assert candidate["gas_budget"]["balance_raw"] == "10000"
    assert candidate["gas_budget"]["required_fee_raw"] == "12105"
    assert candidate["gas_budget"]["shortfall_raw"] == "2105"
    assert candidate["gas_budget"]["additional_fee_raw"] == "5"
    assert entry["execution_supported"] is False


@pytest.mark.parametrize("gas", [None, SimpleNamespace(), estimator(urls={})])
def test_missing_exact_rpc_marks_hint_unverified_and_never_proves_insufficient_gas(gas):
    entry = plan(Quotes(hint=20000), gas)
    assert entry["reason"] == "relay_source_gas_unverified"
    assert entry["relay_candidates"][0]["source_gas_estimate_verified"] is False
    assert entry["execution_supported"] is False


def test_wrong_rpc_chain_does_not_estimate_or_admit_relay_candidate():
    rpc = FakeRpc(chain_id=8453)
    entry = plan(gas=estimator(rpc))
    assert entry["reason"] == "relay_route_search_incomplete"
    assert "chain" in entry["search"]["errors"][0]["diagnostic"]["exceptions"][0]["message"]
    assert not any(method == "eth_estimateGas" for method, _ in rpc.calls)
    assert entry["execution_supported"] is False


def test_transient_requote_retains_other_target_evidence_and_exact_retry_time():
    quota = LifiRequestLimit("routes", retry_at=NOW + 300)
    quotes = Quotes(failures={(42161, 9848): quota})
    entry = plan(quotes, estimator(), targets=(TARGET, OTHER))
    assert entry["reason"] == "relay_route_search_incomplete"
    assert {chain for chain, _ in quotes.calls} == {8453, 42161}
    assert entry["relay_candidates"][0]["gas_reserve_sufficient"] is True
    error = entry["search"]["errors"][0]
    assert error["destination_chain_id"] == 42161 and error["retry_at"] == NOW + 300
    assert entry["search"]["pending"][0]["retry_at"] == NOW + 300
    assert entry["execution_supported"] is False


def test_each_target_quote_failure_is_preserved_in_complete_search():
    quotas = {8453: LifiRequestLimit("routes", retry_at=NOW + 200),
              42161: LifiRequestLimit("routes", retry_at=NOW + 300)}
    entry = plan(Quotes(failures=quotas), estimator(), targets=(TARGET, OTHER))
    assert [error["destination_chain_id"] for error in entry["search"]["errors"]] == [8453, 42161]
    assert [error["retry_at"] for error in entry["search"]["pending"]] == [NOW + 200, NOW + 300]


def test_exact_spendable_quote_is_rechecked_for_deposit_minimum():
    quotes = Quotes()
    original_quote = quotes.quote

    def reduced(*args, **kwargs):
        quote = original_quote(*args, **kwargs)
        if args[2] < 10000:
            # Return a valid order for a different conservative output, not mutate its hash.
            quotes.minimum_raw = 99
            quote = original_quote(*args, **kwargs)
        return quote

    quotes.quote = reduced
    entry = plan(quotes, estimator())
    assert entry["reason"] == "relay_deposit_minimum_or_gas_reserve"
    assert entry["relay_candidates"][0]["reason"] == "bitget_deposit_minimum_not_met"
    assert entry["relay_candidates"][0]["deposit_minimum_met"] is False


def test_fee_padding_is_fixed_and_small_fee_change_needs_only_one_requote():
    quotes = Quotes()
    entry = plan(quotes, estimator(FakeRpc(estimates=(100, 101))))
    assert entry["status"] == "route_ready"
    candidate = entry["relay_candidates"][0]
    assert candidate["source_gas_reserve_raw"] == "152"
    assert candidate["source_gas_raw"] == "127"
    assert len(quotes.calls) == 2


def test_when_padding_cannot_fit_use_exact_cap_without_one_wei_churn():
    quotes = Quotes()
    entry = plan(quotes, estimator(), balance=140)
    assert entry["reason"] == "max_route_loss_exceeded"
    assert [amount for _, amount in quotes.calls] == [140, 14]
    candidate = entry["relay_candidates"][0]
    assert candidate["source_gas_reserve_raw"] == "126"
    assert candidate["gas_budget"]["reserve_method"] == "exact_cap"
    assert candidate["gas_budget"]["reserve_multiplier"] == "1"


def test_fee_growth_outside_bounded_reserve_schedules_retry_and_never_reuses_unsafe_quote():
    quotes = Quotes()
    entry = plan(quotes, estimator(FakeRpc(estimates=(100, 200, 400))))
    assert entry["reason"] == "relay_route_search_incomplete"
    assert len(quotes.calls) == 3
    pending = entry["search"]["pending"][0]
    assert pending["reason"] == "relay_gas_reserve_changed"
    assert isinstance(pending["retry_at"], (int, float))
    assert entry["execution_supported"] is False


def test_unsupported_source_chain_never_requests_a_quote():
    quotes = Quotes()
    quotes.supported_chain_ids = lambda: {8453}
    entry = plan(quotes, estimator())
    assert entry["reason"] == "unsupported_by_relay"
    assert quotes.calls == []


def test_preparation_rejects_wallet_or_source_chain_mismatch_before_rpc():
    rpc = FakeRpc()
    gas = estimator(rpc)
    quote = Quotes().quote(BASE, TARGET, 10000, recipient=RECIPIENT)
    with pytest.raises(ValueError, match="wallet"):
        gas.prepare_relay_fee(quote=quote, chain_catalog=CHAINS, wallet=RECIPIENT,
                              source_chain_id=1868)
    with pytest.raises(ValueError, match="chain"):
        gas.prepare_relay_fee(quote=quote, chain_catalog=CHAINS, wallet=WALLET,
                              source_chain_id=8453)
    assert rpc.calls == []


def test_rpc_quota_wrapped_by_fee_planner_preserves_real_retry_at():
    rpc = FakeRpc()
    original_call = rpc.call

    def call(url, method, params):
        if method == "eth_estimateGas":
            raise LifiRequestLimit("routes", retry_at=NOW + 500)
        return original_call(url, method, params)

    rpc.call = call
    entry = plan(gas=estimator(rpc))
    assert entry["reason"] == "relay_route_search_incomplete"
    assert entry["search"]["pending"][0]["retry_at"] == NOW + 500
    assert entry["relay_candidates"] == []


def test_rpc_timeout_has_scheduled_retry_and_no_provider_fee_fallback():
    entry = plan(gas=estimator(FakeRpc(error=TimeoutError("RPC timeout"))))
    assert entry["reason"] == "relay_route_search_incomplete"
    assert entry["search"]["pending"][0]["retry_at"] > NOW
    assert entry["relay_candidates"] == []


def test_all_target_comparisons_retain_original_balance_valuation():
    quotes = Quotes()
    original = quotes.quote

    def quote(base, target, amount, **kwargs):
        result = original(base, target, amount, **kwargs)
        if target == OTHER:
            result["source_usd"] = str(Decimal(result["source_usd"]) / 2)
        return result

    quotes.quote = quote
    entry = plan(quotes, estimator(), targets=(TARGET, OTHER))
    assert entry["status"] == "route_ready"
    assert [candidate["source_balance_usd"] for candidate in entry["relay_candidates"]] == [
        "10", "10"]
    assert len({candidate["loss_pct"] for candidate in entry["relay_candidates"]}) == 1


def test_legacy_revalidation_preserves_original_usd_budget_with_current_retained_price():
    entry = quote_relay_entry(
        {**BASE, "raw_balance": "8000"}, Quotes(), (TARGET,), gas_estimator=estimator(),
        loss_limit=Decimal("15"), quote_floor=Decimal("0.01"), deposit_address=RECIPIENT,
        original_source_usd="10",
    )
    candidate = entry["relay_candidates"][0]
    assert entry["reason"] == "max_route_loss_exceeded"
    assert candidate["source_balance_usd"] == "10"
    assert candidate["current_source_balance_usd"] == "8"
    assert candidate["unspent_source_reserve_usd"] == "0.026"
    assert Decimal(candidate["loss_pct"]) == Decimal("22.8296")


@pytest.mark.parametrize("original_usd", ["0", "-1", "NaN", "Infinity"])
def test_legacy_revalidation_rejects_invalid_original_budget_without_quote(original_usd):
    quotes = Quotes()
    entry = quote_relay_entry(BASE, quotes, (TARGET,), gas_estimator=estimator(),
                              loss_limit=Decimal("15"), quote_floor=Decimal("0.01"),
                              deposit_address=RECIPIENT, original_source_usd=original_usd)
    assert entry["execution_supported"] is False
    assert entry["diagnostic"]
    assert quotes.calls == []


def test_legacy_gas_revalidation_predicate_excludes_verified_proof_and_other_manual_reasons():
    from evm_inventory.relay import relay_gas_revalidation_needed

    entry = {**BASE, "routing_provider": "relay", "status": "manual_review",
             "reason": "insufficient_source_gas", "relay_probe": {"source_gas_raw": "9000"}}
    assert relay_gas_revalidation_needed(entry) is True
    for changed in ({"status": "route_ready"}, {"reason": "max_route_loss_exceeded"},
                    {"routing_provider": "lifi"}, {"asset_id": USDC}, {"decimals": 6},
                    {"source_gas_estimate_verified": True},
                    {"relay_candidates": [{"source_gas_estimate_verified": True}]},
                    {"relay_selected": {"source_gas_estimate_verified": True}},
                    {"relay_probe": {"gas_budget": {"required_fee_raw": "200",
                         "gas_limit": 100, "fee_quote_method": "eth_gasPrice"}}}):
        assert relay_gas_revalidation_needed({**entry, **changed}) is False, changed
