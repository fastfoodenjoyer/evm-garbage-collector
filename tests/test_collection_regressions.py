import json
from hashlib import sha256
from pathlib import Path

import httpx
import pytest

from evm_inventory.lifi_http import LifiHttpClient
from evm_inventory.runbook import create_route_plan
from evm_inventory.stages import Deadline, deadline_scope, request_timeout


def test_celo_erc20_direct_transfer_reserves_its_shared_native_balance(monkeypatch):
    from types import SimpleNamespace

    import evm_inventory.route_execution as execution
    from evm_inventory.celo import CELO_TOKEN_ADDRESS
    from evm_inventory.lifi import TransactionRequest

    wallet = SimpleNamespace(public_address="0x" + "1" * 40,
                             bitget_deposit_address="0x" + "2" * 40)
    entry = {"chain_id": 42220, "asset_id": CELO_TOKEN_ADDRESS, "decimals": 18,
             "raw_balance": "1000", "status": "direct_deposit",
             "target": {"minimum_raw": "1"}}
    monkeypatch.setattr(execution, "_native_balance", lambda *_, **__: 1000)
    monkeypatch.setattr(execution, "FeePlanner", lambda _: SimpleNamespace(
        plan=lambda _, request, **__: TransactionRequest(
            request.chain_id, request.to, request.data, 0, 10, 1)))
    tx = execution._direct_request(entry, wallet=wallet, rpc=object(), url="rpc")
    assert tx.to == CELO_TOKEN_ADDRESS and tx.value == 0
    assert int(tx.data[-64:], 16) == 970
    assert tx.max_total_fee_wei == 10


def test_reverted_celo_recovery_uses_separate_step_and_observes_retry_on_resume(
    tmp_path, monkeypatch,
):
    from types import SimpleNamespace

    import evm_inventory.route_execution as execution
    from evm_inventory.celo import CELO_TOKEN_ADDRESS
    from evm_inventory.journal import Journal
    from evm_inventory.lifi import TransactionRequest

    wallet = SimpleNamespace(public_address="0x" + "1" * 40,
                             bitget_deposit_address="0x" + "2" * 40)
    old_hash, new_hash = "0x" + "a"*64, "0x" + "b"*64
    sends = []
    rpc = SimpleNamespace(call=lambda _, method, params: {
        "status": "0x0" if params[0] == old_hash else "0x1"})
    with Journal(tmp_path / "inventory.sqlite") as journal:
        p = journal.get_or_create_position(position_key="celo", wallet=wallet.public_address)
        old = journal.record_step_intent(position_id=p["id"], step_key="direct_deposit", nonce=15,
            calldata_digest="c"*64, signed_payload_digest="d"*64)
        journal.record_broadcast_attempt(old["id"], old_hash)
        entry = {"chain_id": 42220, "asset_id": CELO_TOKEN_ADDRESS, "status": "direct_deposit",
                 "automatic_requote": True, "decimals": 18,
                 "_journal_position_id": p["id"], "valuation": {"loss_usd": "1"},
                 "target": {"coin": "CELO", "chain": "CELO", "minimum_raw": "1"}}
        def recover(**kw):
            assert kw["saved"]["tx_hash"] == old_hash
            journal.record_receipt(old["id"], status="reverted", finality_block=10)
            fresh = {**entry, "_journal_step_key": "direct_deposit:recovery1",
                     "_direct_transaction": TransactionRequest(42220, CELO_TOKEN_ADDRESS,
                                                               "0x", 0, 10, 1)}
            journal.save_checkpoint(p["id"], "direct_recovery", {
                "step_key": "direct_deposit:recovery1", "failed_tx_hash": old_hash})
            return fresh
        def send(**kw):
            sends.append(kw["entry"]["_journal_step_key"])
            step = journal.record_step_intent(position_id=p["id"], step_key=sends[-1], nonce=16,
                calldata_digest="e"*64, signed_payload_digest="f"*64)
            journal.record_broadcast_attempt(step["id"], new_hash)
            return new_hash
        monkeypatch.setattr(execution, "_recover_reverted_celo_direct", recover)
        monkeypatch.setattr(execution, "_execute_entry", send)
        bitget = SimpleNamespace(wait_for_deposit_seen=lambda **kw: "success"
                                 if kw["tx_hash"] == new_hash else None)
        kwargs = dict(wallet=wallet, rpc=rpc, rpc_urls={42220: "rpc"}, jumper=object(),
                      bitget=bitget, journal=journal, now_ms=lambda: 1000)
        with deadline_scope(Deadline(30)):
            assert execution._submit_live_step(entry, {"kind": "direct_deposit"}, **kwargs)[
                "state"] == "completed"
            assert execution._submit_live_step(entry, {"kind": "direct_deposit"}, **kwargs)[
                "state"] == "completed"
        assert sends == ["direct_deposit:recovery1"]
        assert journal.step(old["id"])["state"] == "reverted"


@pytest.mark.parametrize("finalized,loss,allowed", [(9, "1", False), (10, "1", True),
                                                    (10, "16", False)])
def test_failed_celo_full_balance_transfer_retry_requires_finality_and_original_loss_budget(
    tmp_path, monkeypatch, finalized, loss, allowed
):
    from types import SimpleNamespace

    from test_route_execution import NOW

    import evm_inventory.route_execution as execution
    from evm_inventory.celo import CELO_TOKEN_ADDRESS
    from evm_inventory.journal import Journal
    from evm_inventory.lifi import LifiPriceEvidence, TransactionRequest
    from evm_inventory.stages import StageDeadlineExceeded

    wallet = SimpleNamespace(public_address="0x" + "1" * 40,
                             bitget_deposit_address="0x" + "2" * 40)
    data = "0xa9059cbb" + wallet.bitget_deposit_address[2:].rjust(64, "0") + f"{1000:064x}"
    original = TransactionRequest(42220, CELO_TOKEN_ADDRESS, data, 0, 10, 1)
    entry = {"wallet": wallet.public_address, "chain_id": 42220,
             "asset_id": CELO_TOKEN_ADDRESS, "decimals": 18, "raw_balance": "1000",
             "automatic_requote": True, "_direct_transaction": original}
    tx_hash = "0x" + "a" * 64
    receipt = {"transactionHash": tx_hash, "from": wallet.public_address,
               "to": CELO_TOKEN_ADDRESS, "status": "0x0", "blockNumber": "0xa",
               "gasUsed": "0xa", "effectiveGasPrice": "0x1"}
    def call(_, method, params):
        if method == "eth_chainId":
            return hex(42220)
        if method == "eth_getBlockByNumber":
            assert params == ["finalized", False]
            return {"number": hex(finalized)}
        assert method == "eth_getBalance" and params == [wallet.public_address, "0x9"]
        return hex(1000)
    rpc = SimpleNamespace(call=call)
    jumper = SimpleNamespace(
        token_price=lambda asset: LifiPriceEvidence(asset, "1", NOW.isoformat()))
    monkeypatch.setattr(execution, "_preflight_live_entry", lambda e, **_: {
        **e, "valuation": {"loss_usd": loss},
        "_direct_transaction": TransactionRequest(42220, CELO_TOKEN_ADDRESS,
            data[:-64] + f"{970:064x}", 0, 10, 1),
    })
    with Journal(tmp_path / "inventory.sqlite") as journal:
        group = journal.get_or_create_group(group_key="celo", wallet=wallet.public_address,
            source_chain_id=42220, loss_budget_pct="15", source_usd="100")
        position = journal.get_or_create_position(position_key="celo", wallet=wallet.public_address,
            group_id=group["id"], source_asset_id=CELO_TOKEN_ADDRESS, source_amount_raw="1000")
        step = journal.record_step_intent(position_id=position["id"], step_key="direct_deposit",
            nonce=15, calldata_digest=sha256(data.encode()).hexdigest(),
            signed_payload_digest="b"*64)
        journal.record_broadcast_attempt(step["id"], tx_hash)
        step = journal.step(step["id"])
        kwargs = dict(entry=entry, saved=step, receipt=receipt, position_id=position["id"],
                      wallet=wallet, rpc=rpc, rpc_urls={42220: "rpc"}, jumper=jumper,
                      bitget=object(), journal=journal, now_ms=lambda: int(NOW.timestamp()*1000))
        if allowed:
            fresh = execution._recover_reverted_celo_direct(**kwargs)
            assert fresh["_journal_step_key"] == "direct_deposit:recovery1"
            assert journal.step(step["id"])["state"] == "reverted"
            proof = journal.checkpoint(position["id"], "direct_recovery")
            assert proof["failed_tx_hash"] == tx_hash
            assert int(fresh["_direct_transaction"].data[-64:], 16) == 970
        else:
            with pytest.raises((StageDeadlineExceeded, execution.GroupLossLimitExceeded)):
                execution._recover_reverted_celo_direct(**kwargs)
            assert journal.step(step["id"])["state"] == "submitted"
            assert journal.checkpoint(position["id"], "direct_recovery") is None


@pytest.mark.parametrize(
    "configured,reason", [(False, "asset_not_allowlisted"), (True, "asset_explicitly_denied")]
)
def test_rejected_asset_explains_exact_policy_decision(tmp_path, configured, reason):
    balances = tmp_path / "balances.csv"
    balances.write_text(
        "wallet,chain_id,asset_id,raw_balance,symbol\n" + "0x" + "1" * 40 + ",10,0xdead,100,OP\n"
    )
    policy = tmp_path / "policy.json"
    policy.write_text(
        json.dumps(
            {"assets": [dict(chain_id=10, asset_id="0xdead", action="deny")] if configured else []}
        )
    )
    entry = create_route_plan(balances, allowlist_path=policy)["entries"][0]
    assert entry["reason"] == reason
    assert entry["reason_detail"]["policy_file"] == str(policy)
    assert entry["reason_detail"]["chain_id"] == 10


def test_soft_quote_target_preserves_real_http_timeout():
    with deadline_scope(Deadline(30, hard_limit=False)):
        assert request_timeout(30) == 30


def test_op_is_automatically_swappable_by_exact_contract():
    path = Path(__file__).parents[1] / "config" / "swap-allowlist.json"
    actions = {
        (a["chain_id"], a["asset_id"]): a["action"] for a in json.loads(path.read_text())["assets"]
    }
    assert actions[(10, "0x4200000000000000000000000000000000000042")] == "swap"


def test_transient_read_retries_inside_one_quote_job(tmp_path):
    clock = [1000.0]
    requests = []

    def handle(request):
        requests.append(request)
        if len(requests) == 1:
            raise httpx.ReadTimeout("temporary", request=request)
        return httpx.Response(200, json={"routes": []})

    client = LifiHttpClient(
        httpx.Client(transport=httpx.MockTransport(handle)),
        db_path=tmp_path / "inventory.sqlite",
        proxy="proxy",
        clock=lambda: clock[0],
        sleep=lambda seconds: clock.__setitem__(0, clock[0] + seconds),
    )
    response = client.post("https://li.quest/v1/advanced/routes", json={})
    assert response.json() == {"routes": []}
    assert len(requests) == 2
    assert client.metrics["retries"] == 1
    client.close()


def test_retry_after_is_obeyed_in_same_invocation(tmp_path):
    clock = [1000.0]
    requests = []

    def handle(request):
        requests.append(clock[0])
        if len(requests) == 1:
            return httpx.Response(429, headers={"retry-after": "2"}, json={"code": 1005})
        return httpx.Response(200, json={"routes": []})

    client = LifiHttpClient(
        httpx.Client(transport=httpx.MockTransport(handle)),
        db_path=tmp_path / "inventory.sqlite",
        proxy="proxy",
        clock=lambda: clock[0],
        quota_wait_seconds=5,
        sleep=lambda seconds: clock.__setitem__(0, clock[0] + seconds),
    )
    assert client.post("https://li.quest/v1/advanced/routes", json={}).status_code == 200
    assert requests == [1000.0, 1002.0]
    client.close()


def test_authenticated_lifi_capacity_and_ledger_are_shared_across_proxies(tmp_path):
    clients = [
        LifiHttpClient(
            httpx.Client(),
            db_path=tmp_path / "inventory.sqlite",
            proxy=proxy,
            api_key="test-secret",
        )
        for proxy in ("one", "two")
    ]
    assert clients[0].scope == clients[1].scope
    assert "test-secret" not in clients[0].scope
    for n in range(100):
        clients[n % 2].quota.reserve_request("routes", str(n))
    assert clients[0].metrics["quota"]["routes"]["local_used"] == 100
    for client in clients:
        client.close()


def test_authenticated_minute_headers_do_not_create_two_hour_local_block(tmp_path):
    from evm_inventory.lifi_quota import AuthenticatedLifiQuotaStore

    clock = [1000.0]
    quota = AuthenticatedLifiQuotaStore(
        tmp_path / "inventory.sqlite", "key", clock=lambda: clock[0]
    )
    for n in range(100):
        attempt = quota.reserve_request("routes", str(n))
    clock[0] += 61
    quota.observe(
        "routes",
        attempt,
        {"ratelimit-limit": "100", "ratelimit-remaining": "99", "ratelimit-reset": "60"},
        200,
    )
    quota.reserve_request("routes", "new-minute")


def test_available_search_prepares_valid_route_despite_unavailable_direction(tmp_path):
    from test_live_plan import DESTINATION, _quoted_route, _RecordingQuotes
    from test_route_search import gas_quote, plan_for

    from evm_inventory.bitget_catalog import BitgetDepositTarget

    def quote(request):
        if request.to_chain_id == 11:
            raise TimeoutError("second destination temporarily unavailable")
        return (_quoted_route(request, route_id="safe", output_amount=9_999_000, gas_amount=1),)

    targets = tuple(
        BitgetDepositTarget("USDC", chain, DESTINATION, 1, decimals=6) for chain in (10, 11)
    )
    plan = plan_for(
        tmp_path,
        targets,
        _RecordingQuotes(quote),
        lambda **kw: gas_quote(1),
        allow_partial_search=True,
    )
    entry = plan["entries"][0]
    assert entry["status"] == "route_ready"
    assert entry["route"]["id"] == "safe"
    assert entry["search"]["complete"] is False
    assert entry["search"]["selection_policy"] == "best_available"
    assert entry["search"]["prepared"] == 1


def test_available_search_never_executes_candidate_without_gas_evidence(tmp_path):
    from test_live_plan import DESTINATION, _quoted_route, _RecordingQuotes
    from test_route_search import plan_for

    from evm_inventory.bitget_catalog import BitgetDepositTarget

    quotes = _RecordingQuotes(
        lambda r: (_quoted_route(r, route_id="no-gas", output_amount=9_999_000, gas_amount=1),)
    )
    plan = plan_for(
        tmp_path,
        (BitgetDepositTarget("USDC", 10, DESTINATION, 1, decimals=6),),
        quotes,
        lambda **kw: None,
        allow_partial_search=True,
    )
    assert plan["entries"][0]["status"] != "route_ready"


@pytest.mark.parametrize("first_prepares", [True, False])
def test_automatic_search_stops_after_first_prepared_route_in_policy(tmp_path, first_prepares):
    from test_live_plan import DESTINATION, _quoted_route, _RecordingQuotes
    from test_route_search import gas_quote, plan_for

    from evm_inventory.bitget_catalog import BitgetDepositTarget

    quotes = _RecordingQuotes(
        lambda r: (
            _quoted_route(r, route_id=str(r.to_chain_id), output_amount=9_999_000, gas_amount=1),
        )
    )
    prepared = []

    def gas(**kwargs):
        route_id = kwargs["route"].route_id
        prepared.append(route_id)
        return None if route_id == "10" and not first_prepares else gas_quote(1)

    targets = tuple(
        BitgetDepositTarget("USDC", chain, DESTINATION, 1, decimals=6) for chain in (10, 11, 12)
    )
    entry = plan_for(tmp_path, targets, quotes, gas, allow_partial_search=True)["entries"][0]
    expected = ["10"] if first_prepares else ["10", "11"]
    assert [str(r.to_chain_id) for r in quotes.requests] == expected
    assert prepared == expected
    assert entry["status"] == "route_ready"
    assert entry["route"]["id"] == expected[-1]
    assert entry["search"]["complete"] is False
    assert "alternative_destination_not_requested" in entry["search"]["errors"]


@pytest.mark.parametrize("provider_gas", [10**12, 2 * 10**16])
@pytest.mark.parametrize("fee_multipliers", [(1,), (1, 2, 1)])
@pytest.mark.parametrize("deposit_minimum", [1, 19_000_000])
def test_native_search_requotes_spendable_amount_with_gas_reserve(
    tmp_path, provider_gas, fee_multipliers, deposit_minimum
):
    from test_live_plan import (
        DESTINATION,
        NOW,
        WALLET,
        _allowlist,
        _balance_rows,
        _quoted_route,
        _RecordingQuotes,
    )

    from evm_inventory.bitget_catalog import BitgetDepositTarget
    from evm_inventory.live_plan import create_live_plan
    from evm_inventory.models import AssetIdentity
    from evm_inventory.planner_gas import PlannerGasEstimator
    from evm_inventory.valuation import FeeQuote, QuotePrice

    balance = 10**16
    gas = 10**12
    quotes = _RecordingQuotes(
        lambda r: (
            _quoted_route(
                r,
                route_id=r.from_amount,
                output_amount=int(r.from_amount) * 2000 // 10**12 - 100_000,
                gas_amount=provider_gas,
            ),
        )
    )

    class ExactGas(PlannerGasEstimator):
        def __call__(self, **kwargs):
            index = min(self.calls, len(fee_multipliers) - 1)
            self.calls += 1
            return (
                FeeQuote(
                    gas * fee_multipliers[index],
                    QuotePrice(AssetIdentity(10, "native", 18), "2000", NOW),
                ),
            )

    estimator = ExactGas(None, {10: "rpc"}, quotes)
    estimator.calls = 0

    plan = create_live_plan(
        _balance_rows(
            tmp_path,
            [
                dict(
                    wallet=WALLET,
                    chain_id=10,
                    asset_id="native",
                    raw_balance=str(balance),
                    decimals=18,
                    status="success",
                )
            ],
        ),
        deposit_addresses={WALLET: "0x" + "d" * 40},
        allowlist_path=_allowlist(tmp_path, [(10, "native", "swap")]),
        targets=(BitgetDepositTarget("USDC", 8453, DESTINATION, deposit_minimum, decimals=6),),
        client=quotes,
        allow_partial_search=True,
        now_ms=lambda: int(NOW.timestamp() * 1000),
        gas_estimator=estimator,
    )
    assert balance - gas * 6 <= int(quotes.requests[-1].from_amount) <= balance - gas * 3
    assert plan["entries"][0]["status"] == "route_ready"


def test_native_reserve_uses_rpc_fee_cap_when_provider_understates_gas(tmp_path):
    from test_live_plan import (
        DESTINATION,
        NOW,
        WALLET,
        _allowlist,
        _balance_rows,
        _quoted_route,
        _RecordingQuotes,
    )

    from evm_inventory.bitget_catalog import BitgetDepositTarget
    from evm_inventory.live_plan import create_live_plan
    from evm_inventory.models import AssetIdentity
    from evm_inventory.valuation import FeeQuote, QuotePrice

    balance, hint, actual = 10**16, 10**12, 10**13
    quotes = _RecordingQuotes(
        lambda r: (
            _quoted_route(r, route_id=r.from_amount, output_amount=19_900_000, gas_amount=hint),
        )
    )
    plan = create_live_plan(
        _balance_rows(
            tmp_path,
            [
                dict(
                    wallet=WALLET,
                    chain_id=10,
                    asset_id="native",
                    raw_balance=str(balance),
                    decimals=18,
                    status="success",
                )
            ],
        ),
        deposit_addresses={WALLET: "0x" + "d" * 40},
        allowlist_path=_allowlist(tmp_path, [(10, "native", "swap")]),
        targets=(BitgetDepositTarget("USDC", 8453, DESTINATION, 1, decimals=6),),
        client=quotes,
        allow_partial_search=True,
        now_ms=lambda: int(NOW.timestamp() * 1000),
        gas_estimator=lambda **kw: (
            FeeQuote(actual, QuotePrice(AssetIdentity(10, "native", 18), "2000", NOW)),
        ),
    )
    e = plan["entries"][0]
    assert e["status"] == "route_ready"
    assert balance - int(e["route"]["evidence"]["input_amount"]) >= actual * 3


def test_missing_allowance_uses_bounded_fee_quote_until_approval_is_confirmed():
    from test_live_plan import _quoted_route
    from test_planner_gas import SPENDER, TOKEN, WALLET, PriceClient, Rpc

    from evm_inventory.lifi import LifiRouteRequest
    from evm_inventory.models import AssetIdentity
    from evm_inventory.planner_gas import PlannerGasEstimator

    class AllowanceRpc(Rpc):
        def call(self, url, method, params):
            if method == "eth_estimateGas" and params[0]["to"] == "0x" + "d" * 40:
                raise ValueError("execution reverted: ERC20: insufficient allowance")
            return super().call(url, method, params)

    rpc = AllowanceRpc()
    estimator = PlannerGasEstimator(rpc, {10: "rpc"}, PriceClient())
    route = _quoted_route(
        LifiRouteRequest(10, 8453, TOKEN, "0x" + "c" * 40, "1000", WALLET, WALLET),
        route_id="needs-approval",
        output_amount=900,
        approval_address=SPENDER,
    )
    fees = estimator(
        purpose="bridge",
        route=route,
        wallet=WALLET,
        source_asset=AssetIdentity(10, TOKEN, 6),
        target=None,
        amount=1000,
        recipient=WALLET,
    )
    assert fees is not None and len(fees) == 2
    assert fees[0].raw_amount > fees[1].raw_amount
    prepared = estimator.prepared_fee_caps[route.route_id][0]
    assert prepared.max_total_fee_cap_wei == fees[0].raw_amount
    assert prepared.fee_quote_fallback_reason == "pending_approval"


def test_automatic_preflight_timeout_keeps_original_group_retryable(tmp_path):
    from decimal import Decimal

    from test_route_execution import ASSET_A, _entry

    from evm_inventory.journal import Journal
    from evm_inventory.route_execution import execute_group_entries

    e = {**_entry(ASSET_A), "automatic_requote": True}

    def unavailable(_):
        try:
            raise httpx.ReadTimeout("temporary outage")
        except httpx.ReadTimeout as error:
            raise ValueError("estimate_gas_rpc_error") from error

    with Journal(tmp_path / "inventory.sqlite") as journal:
        first = execute_group_entries(
            [e],
            journal=journal,
            max_route_loss_pct=Decimal("15"),
            preflight=unavailable,
            submit_step=lambda *_: pytest.fail("must not submit"),
        )
        assert journal.group(first["group_id"])["state"] == "active"
        assert first["deferred"] == 1
        second = execute_group_entries(
            [e],
            journal=journal,
            max_route_loss_pct=Decimal("15"),
            preflight=lambda e: e,
            submit_step=lambda *_: {"state": "completed", "realized_loss_usd": "1"},
        )
        assert second["completed"] == 1


def test_automatic_execution_allows_full_request_time_with_bounded_run():
    from evm_inventory.route_execution import _execution_deadline

    with deadline_scope(_execution_deadline(180, [{"automatic_requote": True}])):
        assert request_timeout(30) == 30
    with pytest.raises(ValueError):
        _execution_deadline(180, [{}])
    with deadline_scope(Deadline(5)):
        assert _execution_deadline(180, [{"automatic_requote": True}]).remaining() <= 5


@pytest.mark.parametrize("simulation_ok", [True, False])
def test_pending_approval_must_be_confirmed_and_simulated_before_swap_signature(
    monkeypatch, simulation_ok
):
    from dataclasses import replace
    from types import SimpleNamespace

    from test_route_execution import ASSET_A, DEPOSIT, NOW, WALLET, _entry

    from evm_inventory.lifi import TransactionRequest
    from evm_inventory.route_execution import _execute_entry

    phases = []
    request = TransactionRequest(
        10,
        DEPOSIT,
        "0x",
        0,
        21_000,
        1,
        max_total_fee_cap_wei=50_000,
        fee_quote_fallback_reason="pending_approval",
    )

    class Planner:
        def __init__(self, _):
            pass

        def plan(self, *a, **kw):
            assert phases == ["approval_confirmed"]
            phases.append("exact_simulation")
            if not simulation_ok:
                raise ValueError("actual simulation rejected")
            return replace(request, fee_quote_fallback_reason=None)

    def approve(*a, **kw):
        phases.append("approval_confirmed")
        return "approval-hash"

    def sign(*a, **kw):
        assert phases == ["approval_confirmed", "exact_simulation"]
        phases.append("swap_signed")
        return "signed-payload"

    module = "evm_inventory.route_execution."
    monkeypatch.setattr(module + "FeePlanner", Planner)
    monkeypatch.setattr(module + "_approve_if_needed", approve)
    monkeypatch.setattr(module + "_native_balance", lambda *a, **kw: 1_000_000)
    monkeypatch.setattr(module + "pending_nonce", lambda *a, **kw: 1)
    monkeypatch.setattr(module + "sign_transaction", sign)
    monkeypatch.setattr(module + "broadcast_signed_transaction", lambda *a, **kw: "swap-hash")
    monkeypatch.setattr(module + "wait_for_receipt", lambda *a, **kw: None)
    entry = {**_entry(ASSET_A, status="route_ready"), "quoted_at": int(NOW.timestamp() * 1000)}
    kwargs = dict(
        entry=entry,
        wallet=SimpleNamespace(public_address=WALLET, private_key="secret"),
        rpc=object(),
        jumper=object(),
        rpc_urls={10: "rpc"},
        prepared_request=request,
        now_ms=lambda: entry["quoted_at"],
    )
    if simulation_ok:
        assert _execute_entry(**kwargs) == "swap-hash"
        assert phases[-1] == "swap_signed"
    else:
        with pytest.raises(ValueError, match="simulation rejected"):
            _execute_entry(**kwargs)
        assert "swap_signed" not in phases


def test_live_fee_estimator_preserves_transient_cause_for_automatic_retry():
    from test_live_plan import _quoted_route
    from test_planner_gas import TOKEN, WALLET, PriceClient, Rpc

    from evm_inventory.lifi import LifiRouteRequest
    from evm_inventory.planner_gas import PlannerGasEstimator
    from evm_inventory.route_execution import _retryable_provider_error

    class Unavailable(Rpc):
        def call(self, url, method, params):
            if method == "eth_estimateGas":
                raise httpx.ReadTimeout("temporary RPC failure")
            return super().call(url, method, params)

    estimator = PlannerGasEstimator(
        Unavailable(), {10: "rpc"}, PriceClient(), retry_transient_errors=True
    )
    route = _quoted_route(
        LifiRouteRequest(10, 8453, TOKEN, "0x" + "c" * 40, "1000", WALLET, WALLET),
        route_id="temporary",
        output_amount=900,
    )
    with pytest.raises(ValueError) as failure:
        estimator.estimate_prepared_route(
            route=route,
            wallet=WALLET,
            prepared_transactions=(estimator.price_client.step_transaction(route.first_step),),
        )
    assert _retryable_provider_error(failure.value)


def test_approval_only_broadcast_does_not_freeze_saved_preparation(tmp_path):
    from decimal import Decimal

    from test_route_execution import ASSET_A, _entry

    from evm_inventory.journal import Journal
    from evm_inventory.route_execution import _position_key, execute_group_entries

    e = {**_entry(ASSET_A), "automatic_requote": True}
    calls = []
    with Journal(tmp_path / "inventory.sqlite") as journal:
        group = journal.get_or_create_group(
            group_key=e["group"]["key"],
            wallet=e["wallet"],
            source_chain_id=10,
            loss_budget_pct="15",
            source_usd="100",
        )
        position = journal.get_or_create_position(
            position_key=_position_key(e),
            wallet=e["wallet"],
            group_id=group["id"],
            source_asset_id=ASSET_A,
            source_amount_raw=e["raw_balance"],
        )
        journal.save_checkpoint(position["id"], "prepared", {**e, "bad_old_quote": True})
        approval = journal.record_step_intent(
            position_id=position["id"],
            step_key="approval:bridge:old:0",
            nonce=0,
            calldata_digest="a",
            signed_payload_digest="b",
        )
        journal.record_broadcast_attempt(approval["id"], "0x" + "a" * 64)
        assert journal.has_broadcast(position["id"]) and not journal.has_asset_broadcast(
            position["id"]
        )
        result = execute_group_entries(
            [e],
            journal=journal,
            max_route_loss_pct=Decimal("15"),
            preflight=lambda e: calls.append(e) or e,
            submit_step=lambda e, _: {"state": "completed", "realized_loss_usd": "1"},
        )
        assert len(calls) == 1
        assert result["completed"] == 1


def test_automatic_requote_accepts_new_quote_id_after_identity_and_fee_validation():
    from types import SimpleNamespace

    from test_route_execution import ASSET_A, DEPOSIT, NOW, WALLET, _entry, _lifi_route

    from evm_inventory.bitget_catalog import BitgetDepositTarget
    from evm_inventory.lifi import TransactionRequest
    from evm_inventory.live_plan import _route_data
    from evm_inventory.models import AssetIdentity
    from evm_inventory.route_execution import _refresh_route_step
    from evm_inventory.valuation import FeeQuote, QuotePrice

    old = _lifi_route(route_id="expired-id", output_amount=900_000)
    current = _lifi_route(route_id="fresh-id", output_amount=910_000)
    step = {"kind": "bridge", "route": _route_data(old)}
    e = {**_entry(ASSET_A, status="route_ready", steps=[step]), "automatic_requote": True}
    gas = SimpleNamespace(
        rpc=SimpleNamespace(call=lambda *_: "0x" + f"{1_000_000:064x}"),
        rpc_urls={10: "rpc"},
        estimate_prepared_route=lambda **_: (
            FeeQuote(1, QuotePrice(AssetIdentity(10, "native", 18), "2000", NOW)),
        ),
    )
    client = SimpleNamespace(
        routes=lambda _: (current,),
        step_transaction=lambda _: TransactionRequest(10, DEPOSIT, "0x", 0, 21_000, 1),
    )
    bitget = SimpleNamespace(
        revalidate_deposit_target=lambda *_a, **_kw: BitgetDepositTarget(
            "USDC", 8453, "0x" + "e" * 40, 1, "Base", 6
        )
    )
    candidate, fresh = _refresh_route_step(
        e,
        step,
        wallet=SimpleNamespace(public_address=WALLET, bitget_deposit_address=DEPOSIT),
        jumper=client,
        gas_estimator=gas,
        now=NOW,
        bitget=bitget,
    )
    assert candidate.is_valid
    assert fresh["route"]["id"] == "fresh-id"


@pytest.mark.parametrize("provider_hint", [1, 100, 10_000])
@pytest.mark.parametrize("fee_values", [(10,), (10, 11, 10)])
def test_automatic_native_preflight_reduces_input_after_sibling_gas_spending(
    provider_hint, fee_values
):
    from types import SimpleNamespace

    from test_live_plan import NOW, _quoted_route
    from test_route_execution import DEPOSIT, WALLET, _entry

    from evm_inventory.bitget_catalog import BitgetDepositTarget
    from evm_inventory.lifi import LifiRouteRequest, TransactionRequest
    from evm_inventory.live_plan import _route_data
    from evm_inventory.models import AssetIdentity
    from evm_inventory.route_execution import _refresh_route_step
    from evm_inventory.valuation import FeeQuote, QuotePrice

    destination = "0x" + "e" * 40
    old = _quoted_route(
        LifiRouteRequest(10, 8453, "0x" + "0" * 40, destination, "3000", WALLET, DEPOSIT),
        route_id="old",
        output_amount=900,
        gas_amount=provider_hint,
    )
    step = {"kind": "bridge", "route": _route_data(old)}
    e = {**_entry("native", status="route_ready", steps=[step]), "automatic_requote": True}
    gas = SimpleNamespace(
        rpc=SimpleNamespace(call=lambda *_: hex(2000)),
        rpc_urls={10: "rpc"},
        estimate_prepared_route=lambda **_: (
            FeeQuote(
                fee_values[min(len(amounts) - 1, len(fee_values) - 1)],
                QuotePrice(AssetIdentity(10, "native", 18), "2000", NOW),
            ),
        ),
    )
    amounts = []

    def routes(request):
        amounts.append(int(request.from_amount))
        return (
            _quoted_route(request, route_id="fresh", output_amount=900, gas_amount=provider_hint),
        )

    client = SimpleNamespace(
        routes=routes,
        step_transaction=lambda _: TransactionRequest(10, DEPOSIT, "0x", 0, 21_000, 1),
    )
    bitget = SimpleNamespace(
        revalidate_deposit_target=lambda *_a, **_kw: BitgetDepositTarget(
            "USDC", 8453, destination, 1, "Base", 6
        )
    )
    candidate, fresh = _refresh_route_step(
        e,
        step,
        wallet=SimpleNamespace(public_address=WALLET, bitget_deposit_address=DEPOSIT),
        jumper=client,
        gas_estimator=gas,
        now=NOW,
        bitget=bitget,
    )
    assert candidate.is_valid
    expected = 1970 if len(fee_values) == 1 else 1967
    assert int(fresh["route"]["from_amount"]) == expected
    assert amounts[-1] == expected


@pytest.mark.parametrize(
    "fee_values,expected", [((20,), [970, 940]), ((20, 21, 22, 21), [970, 940, 937, 934])]
)
def test_automatic_relay_requotes_live_spendable_balance_after_token_gas(
    monkeypatch, fee_values, expected
):
    from datetime import UTC, datetime
    from decimal import Decimal
    from types import SimpleNamespace

    import evm_inventory.relay_execution as execution
    from evm_inventory.bitget_catalog import BitgetDepositTarget

    wallet = SimpleNamespace(public_address="0x" + "1" * 40, bitget_deposit_address="0x" + "2" * 40)
    entry = {
        "wallet": wallet.public_address,
        "chain_id": 10,
        "asset_id": "native",
        "decimals": 18,
        "deposit_address": wallet.bitget_deposit_address,
        "automatic_requote": True,
        "relay_selected": {"request": {"amount": "1800"}, "source_gas_raw": "10"},
    }
    amounts = []

    def quote(_entry, _target, amount, **kwargs):
        assert kwargs["fresh"] and kwargs["recipient"] == wallet.bitget_deposit_address
        amounts.append(amount)
        return {
            "amount": amount,
            "source_usd": str(amount),
            "minimum_output_usd": str(Decimal(amount) * Decimal("0.99")),
            "to_amount_min": str(amount),
            "observed_at": datetime.now(UTC).isoformat(),
        }

    client = SimpleNamespace(supported_chain_ids=lambda: {10}, quote=quote, chain_catalog=[])
    rpc = SimpleNamespace(
        call=lambda _, method, __: "0xa" if method == "eth_chainId" else hex(1000)
    )
    monkeypatch.setattr(execution, "verified_transaction", lambda q, _: q)
    monkeypatch.setattr(
        execution,
        "FeePlanner",
        lambda _: SimpleNamespace(
            plan=lambda _, q, **__: SimpleNamespace(
                value=q["amount"],
                max_total_fee_wei=fee_values[min(len(amounts) - 1, len(fee_values) - 1)],
            )
        ),
    )
    target = BitgetDepositTarget("USDC", 8453, "0x" + "3" * 40, 1, decimals=6)
    fresh, tx = execution._prepare(entry, wallet, client, rpc, "rpc", target, Decimal("15"))
    assert amounts == expected
    assert tx.value == int(fresh["amount"]) == expected[-1]
    assert tx.value + 3 * tx.max_total_fee_wei <= 1000
