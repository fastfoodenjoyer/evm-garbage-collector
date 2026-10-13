"""Spec-review regressions for staged canonical source conversion."""

import json
from dataclasses import replace
from datetime import UTC, datetime
from decimal import Decimal
from hashlib import sha256
from types import SimpleNamespace

import pytest
from test_canonical_source_bootstrap import AMOUNT, DEPOSIT, WALLET, bootstrap_entry
from test_live_plan import _quoted_route

from evm_inventory.bitget_catalog import BitgetDepositTarget
from evm_inventory.journal import Journal
from evm_inventory.lifi import (
    LifiPriceEvidence,
    LifiRouteRequest,
    TransactionRequest,
    _route_from_dict,
)
from evm_inventory.live_plan import _route_data
from evm_inventory.planner_gas import PlannerGasEstimator
from evm_inventory.route_execution import _execute_live_groups, _position_key, _preflight_live_entry
from evm_inventory.rpc import RpcError
from evm_inventory.stages import Deadline, StageDeadlineExceeded, deadline_scope
from evm_inventory.unsigned_route_options import UnsignedRoutePreparationExhausted
from evm_inventory.weth import WETH_CONTRACTS


def preflight_fixture(monkeypatch):
    e, request, wallet = bootstrap_entry()
    e.pop("_journal_step_key")
    for key in (
        "source_fee_cap_raw",
        "forecast_proof",
        "future_balance_raw",
        "future_source_fee_reserve_raw",
    ):
        e["canonical_source_bootstrap"].pop(key)
    e["canonical_source_bootstrap"].update(paid_loss_usd="0", accounted_loss_usd="0")
    now = [int(datetime.now(UTC).timestamp() * 1000)]
    future_calls = []

    class Rpc:
        def call(self, url, method, params):
            if method == "eth_chainId":
                return "0xa"
            if method == "eth_getBalance":
                return hex(2000)
            if method == "eth_call":
                return "0x" + f"{AMOUNT:064x}"
            if method == "eth_estimateGas":
                return "0xa"
            raise AssertionError(method)

    rpc = Rpc()

    def fee_plan(self, url, req, sender, **kw):
        self.rpc.call(
            url,
            "eth_estimateGas",
            [dict(to=req.to, data=req.data, value=hex(req.value), **{"from": sender}), "latest"],
        )
        return replace(req, gas_limit=10, gas_price_wei=100, max_total_fee_cap_wei=1000)

    monkeypatch.setattr("evm_inventory.fee_planner.FeePlanner.plan", fee_plan)
    target = BitgetDepositTarget("USDC", 8453, e["target"]["asset_id"], 1, "BASE", 6)
    jumper = SimpleNamespace(
        token_price=lambda asset: LifiPriceEvidence(
            asset, "2500", datetime.fromtimestamp((now[0] - 100) / 1000, UTC).isoformat()
        ),
        step_transaction=lambda _: request,
    )

    def future(original, amount, **kw):
        future_calls.append(now[0])
        payload = json.loads(
            _quoted_route(
                LifiRouteRequest(
                    10, 8453, "0x" + "0" * 40, target.asset_id, str(amount - 3600), WALLET, DEPOSIT
                ),
                route_id="future",
                output_amount=260000,
            ).evidence.canonical_payload_json
        )
        payload["steps"][0]["action"]["fromAddress"] = WALLET
        payload["priceTimestamp"] = datetime.fromtimestamp((now[0] - 100) / 1000, UTC).isoformat()
        for token in ("fromToken", "toToken"):
            payload["steps"][0]["action"][token]["priceTimestamp"] = payload["priceTimestamp"]
        for cost in payload.get("gasCosts", []):
            cost["timestamp"] = payload["priceTimestamp"]
        route = _route_from_dict(payload, quote_timestamp=payload["priceTimestamp"])
        tx = TransactionRequest(10, DEPOSIT, "0x1234", amount - 3600, 10, 100)
        assert kw["gas_estimator"].estimate_prepared_route(
            route=route, wallet=WALLET, prepared_transactions=(tx,)
        )
        return dict(
            kind="bridge",
            route=_route_data(route),
            _live_route=route,
            _prepared_requests=(tx,),
            valuation={"loss_usd": "0.005", "loss_pct": "1"},
        )

    monkeypatch.setattr("evm_inventory.route_execution._requote_bridge_after_swap", future)
    bitget = SimpleNamespace(revalidate_deposit_target=lambda *a, **kw: target)

    def preflight(entry):
        return _preflight_live_entry(
            entry,
            wallet=wallet,
            rpc=rpc,
            rpc_urls={10: "rpc"},
            jumper=jumper,
            bitget=bitget,
            gas_estimator=PlannerGasEstimator(rpc, {10: "rpc"}, jumper),
            now_ms=lambda: now[0],
        )

    return e, now, future_calls, preflight


def test_forecast_revalidates_original_endpoint_prices_after_six_minutes(monkeypatch):
    e, now, calls, preflight = preflight_fixture(monkeypatch)
    first = preflight(e)
    original = first["steps"][1]["_live_route"].evidence
    now[0] += 6 * 60 * 1000
    second = preflight(first)
    assert len(calls) == 2
    assert second["steps"][1]["_live_route"].evidence.quote_timestamp != original.quote_timestamp
    assert first["steps"][1]["_live_route"].evidence == original


def test_source_ceiling_rejects_budget_that_allows_estimate_but_not_increase_within_cap(
    monkeypatch,
):
    e, _, _, preflight = preflight_fixture(monkeypatch)
    # 15% budget is ~0.0050000000027: current gas1000 costs2.5e-12 USD,
    # future0.005 fits; signing at1100 (within1200 cap) costs2.75e-12 and fails.
    source_usd = "0.033333333351333333333333333333333333333333333333333333333333333333"
    e["group"]["source_usd"] = source_usd
    e["canonical_source_bootstrap"]["source_usd"] = source_usd
    with pytest.raises(ValueError, match="loss_threshold_exceeded"):
        preflight(e)


def test_forecast_cache_checks_endpoint_timestamp_even_with_young_quote(monkeypatch):
    e, now, calls, preflight = preflight_fixture(monkeypatch)
    first = preflight(e)
    assert len(calls) == 1
    old = first["steps"][1]["_live_route"]
    payload = json.loads(old.evidence.canonical_payload_json)
    payload["steps"][0]["action"]["toToken"]["priceTimestamp"] = datetime.fromtimestamp(
        (now[0] - 360_000) / 1000, UTC
    ).isoformat()
    first["steps"][1]["_live_route"] = _route_from_dict(
        payload, quote_timestamp=old.evidence.quote_timestamp
    )
    preflight(first)
    assert len(calls) == 2


def test_young_forecast_reprices_fresh_gas_and_leaves_withdrawal_unsigned(tmp_path, monkeypatch):
    import evm_inventory.route_execution as execution
    from evm_inventory.valuation import FeeQuote

    e, _, _, preflight = preflight_fixture(monkeypatch)
    first = preflight(e)
    previous = first["steps"][1]["valuation"].copy()
    price = PlannerGasEstimator._price_planned

    def expensive_fee(self, request):
        fee = price(self, request)
        return FeeQuote(fee.raw_amount, replace(fee.price, usd_per_token="80000000000000"))

    monkeypatch.setattr(PlannerGasEstimator, "_price_planned", expensive_fee)
    attempts = []

    def no_affordable_route(*a, **kw):
        attempts.append(kw["remaining_loss_usd"])
        raise ValueError("requote_required:no_eligible_route_after_swap")

    monkeypatch.setattr(execution, "_requote_bridge_after_swap", no_affordable_route)
    with Journal(tmp_path / "fake.sqlite") as journal:
        result = execution.execute_group_entries([first], journal=journal,
            max_route_loss_pct=Decimal("15"), preflight=preflight,
            submit_step=lambda *a: pytest.fail("withdrawal must remain unsigned"),
            requote_after_swap=lambda *a: pytest.fail("source conversion did not occur"))
        assert result["manual_review"] == 1
        assert list(journal.connection.execute("SELECT * FROM route_steps")) == []
    assert len(attempts) == 1
    assert first["steps"][1]["valuation"] == previous


@pytest.mark.parametrize("live_minimum,expected", [(2, 2), (0, 1)])
def test_automatic_target_minimum_keeps_stricter_live_or_original_floor(live_minimum, expected):
    from evm_inventory.route_execution import _require_live_target

    e, _, wallet = bootstrap_entry()
    original = e["target"].copy()
    live = BitgetDepositTarget("USDC", 8453, original["asset_id"], live_minimum, "BASE", 6)
    target = _require_live_target(e, wallet=wallet,
        bitget=SimpleNamespace(revalidate_deposit_target=lambda *a, **kw: live))
    assert target.minimum_raw == expected
    assert e["target"] == original


def test_changed_live_minimum_blocks_forecast_before_source_signature(monkeypatch):
    e, _, _, preflight = preflight_fixture(monkeypatch)
    e["target"]["minimum_raw"] = "1000000"
    e["canonical_source_bootstrap"]["target"]["minimum_raw"] = "1000000"
    with pytest.raises(ValueError, match="below_target_minimum"):
        preflight(e)


def confirmed_source(journal):
    e, request, wallet = bootstrap_entry()
    e["canonical_source_bootstrap"].update(paid_loss_usd="0", accounted_loss_usd="0")
    source = e["steps"][0]
    source.update(route=_route_data(source["_live_route"]), _prepared_requests=(request,))
    e["steps"][1]["route"] = _route_data(e["steps"][1]["_live_route"])
    e.update(quoted_at=int(datetime.now(UTC).timestamp() * 1000))
    group = journal.get_or_create_group(
        group_key="g",
        wallet=WALLET,
        source_chain_id=10,
        source_usd=e["group"]["source_usd"],
        loss_budget_pct="15",
    )
    journal.record_group_state(group["id"], "active")
    p = journal.get_or_create_position(
        position_key=_position_key(e),
        wallet=WALLET,
        group_id=group["id"],
        source_asset_id=e["asset_id"],
        source_amount_raw=e["raw_balance"],
    )
    key = f"swap_to_native:{source['route']['id']}:0"
    source_hash = "0x" + "a" * 64
    e["_journal_step_key"] = key
    journal.save_checkpoint(p["id"], f"canonical_signing:{key}", dict(entry=e, request=request))
    row = journal.record_step_intent(
        position_id=p["id"],
        step_key=key,
        nonce=0,
        calldata_digest=sha256(request.data.encode()).hexdigest(),
        signed_payload_digest="b" * 64,
        tx_hash=source_hash,
    )
    journal.record_step_state(row["id"], "confirmed")
    result = dict(
        state="confirmed",
        actual_asset_id="native",
        actual_balance_raw=str(AMOUNT - 500),
        realized_loss_usd="0.00000000000125",
        protected_native_raw="2000",
        source_receipt_proof=dict(
            tx_hash=source_hash,
            chain_id=10,
            owner=WALLET,
            contract=WETH_CONTRACTS[10],
            amount_raw=str(AMOUNT),
            paid_gas_raw="500",
            status=1,
        ),
    )
    journal.checkpoint_source_conversion(p["id"], result)
    journal.save_checkpoint(
        p["id"],
        f"route:swap_to_native:{source['route']['id']}",
        dict(
            next_index=1,
            confirmed_hashes=[(10, source_hash)],
            initial_native=2000,
            paid_native_gas=500,
        ),
    )
    journal.save_checkpoint(p["id"], "prepared", e)
    journal.save_checkpoint(p["id"], "canonical_source_bootstrap", e["canonical_source_bootstrap"])
    return e, request, wallet, p["id"]


def test_confirmed_source_does_not_bypass_future_provider_retry_at(tmp_path, monkeypatch):
    path = tmp_path / "fake.sqlite"
    with Journal(path) as journal:
        e, _, wallet, pid = confirmed_source(journal)
        journal.save_checkpoint(pid, "automatic_retry", dict(reason="quota_wait", retry_at=2000))
    traffic = []
    monkeypatch.setattr("evm_inventory.route_execution.time.time", lambda: 1000)
    monkeypatch.setattr(
        "evm_inventory.route_execution.ExecutionRpc",
        lambda _: SimpleNamespace(call=lambda *a: pytest.fail("RPC before provider retry_at")),
    )
    monkeypatch.setattr(
        "evm_inventory.route_execution.WalletLifiClients",
        lambda *a, **kw: SimpleNamespace(for_wallet=lambda _: object(), close=lambda: None),
    )
    monkeypatch.setattr("evm_inventory.route_execution._bitget_client", lambda: object())

    def quote(*a, **kw):
        traffic.append("quote")
        raise StageDeadlineExceeded("unexpected_provider_attempt")

    monkeypatch.setattr("evm_inventory.route_execution._requote_bridge_after_swap", quote)
    result = _execute_live_groups(
        [e],
        wallets={WALLET: wallet},
        rpc_urls={10: "rpc"},
        journal_path=path,
        max_route_loss_pct=Decimal("15"),
        now_ms=lambda: e["quoted_at"],
        delay_min_seconds=0,
        delay_max_seconds=0,
        sleep=lambda _: None,
        rng=None,
    )
    assert result["pending"] == 1
    assert traffic == []
    with Journal(path) as journal:
        assert journal.position(pid)["reason"] == "provider_retry_scheduled"
        assert journal.checkpoint(pid, "automatic_retry")["retry_at"] == 2000
        assert len(journal.connection.execute("SELECT * FROM route_steps").fetchall()) == 1


def test_structured_actual_simulation_after_source_gets_240_second_retry_not_manual(tmp_path):
    from evm_inventory.route_execution import _submit_live_step

    path = tmp_path / "fake.sqlite"
    with Journal(path) as journal:
        e, _, wallet, pid = confirmed_source(journal)
        future = e["steps"][1]
        # Fresh actual route has current endpoint timestamps and no forecast marker.
        payload = json.loads(future["_live_route"].evidence.canonical_payload_json)
        now = datetime.now(UTC)
        payload["priceTimestamp"] = now.isoformat()
        payload["steps"][0]["action"]["fromAddress"] = WALLET
        for cost in payload.get("gasCosts", []):
            cost["timestamp"] = now.isoformat()
        route = _route_from_dict(payload, quote_timestamp=now.isoformat())
        tx = replace(future["_prepared_requests"][0], fee_quote_fallback_reason=None)
        future = dict(
            kind="bridge", route=_route_data(route), _live_route=route, _prepared_requests=(tx,)
        )
        e.update(
            _actual_asset_id="native",
            _actual_balance_raw=str(AMOUNT - 500),
            _journal_position_id=pid,
            _realized_loss_before_current_usd="0.00000000000125",
            steps=[e["steps"][0], future],
        )

        class Rpc:
            def call(self, url, method, params):
                if method == "eth_getBalance":
                    return hex(AMOUNT + 1500)
                if method == "eth_estimateGas":
                    raise RpcError(
                        "rpc_error",
                        diagnostic=dict(
                            method=method,
                            provider_error={"code": -32000, "message": "execution reverted"},
                        ),
                    )
                raise AssertionError(method)

        jumper = SimpleNamespace(step_transaction=lambda _: tx)
        target = BitgetDepositTarget("USDC", 8453, e["target"]["asset_id"], 1, "BASE", 6)
        with deadline_scope(Deadline(30)):
            with pytest.raises(UnsignedRoutePreparationExhausted) as failure:
                _submit_live_step(
                    e,
                    future,
                    wallet=wallet,
                    rpc=Rpc(),
                    rpc_urls={10: "rpc"},
                    jumper=jumper,
                    bitget=SimpleNamespace(revalidate_deposit_target=lambda *a, **k: target),
                    journal=journal,
                    now_ms=lambda: int(now.timestamp() * 1000),
                )
        assert failure.value.retry_at >= now.timestamp() + 240
        assert len(journal.connection.execute("SELECT * FROM route_steps").fetchall()) == 1


@pytest.mark.parametrize("failure_stage", ["initial", "adjusted"])
def test_first_actual_bridge_requote_defers_structured_reverts_after_source(
    tmp_path, monkeypatch, failure_stage,
):
    import evm_inventory.route_execution as execution

    path = tmp_path / "fake.sqlite"
    with Journal(path) as journal:
        e, _, wallet, pid = confirmed_source(journal)
    now = datetime.now(UTC)
    clock = [now.timestamp()]
    quotes = []
    estimates = []

    class Rpc:
        def call(self, url, method, params):
            if method == "eth_getBalance":
                return hex(AMOUNT + 1500)
            if method == "eth_estimateGas":
                estimates.append(params)
                if failure_stage == "initial" or len(estimates) > 1:
                    raise RpcError("rpc_error", diagnostic=dict(method=method,
                        provider_error={"code": -32000, "message": "execution reverted"}))
                return "0x10"
            raise AssertionError(method)

    def routes(request):
        quotes.append(request)
        raw = json.loads(_quoted_route(request, output_amount=260000,
            route_id=f"actual-{len(quotes)}").evidence.canonical_payload_json)
        raw["priceTimestamp"] = now.isoformat()
        for token in ("fromToken", "toToken"):
            raw["steps"][0]["action"][token]["priceTimestamp"] = now.isoformat()
        raw["steps"][0]["action"]["fromAddress"] = WALLET
        return (_route_from_dict(raw, quote_timestamp=now.isoformat()),)

    jumper = SimpleNamespace(routes=routes,
        step_transaction=lambda raw: TransactionRequest(10, DEPOSIT, "0x1234",
            int(raw["action"]["fromAmount"]), 10, 100),
        token_price=lambda asset: LifiPriceEvidence(asset, "2500", now.isoformat()))
    rpc = Rpc()

    def fee(self, url, request, *, sender, **kw):
        self.rpc.call(url, "eth_estimateGas", [{"from": sender, "to": request.to,
                      "data": request.data, "value": hex(request.value)}, "latest"])
        return replace(request, max_total_fee_cap_wei=1000)

    monkeypatch.setattr("evm_inventory.fee_planner.FeePlanner.plan", fee)
    monkeypatch.setattr(execution.time, "time", lambda: clock[0])
    monkeypatch.setattr(execution, "ExecutionRpc", lambda _: rpc)
    monkeypatch.setattr(execution, "WalletLifiClients",
        lambda *a, **kw: SimpleNamespace(for_wallet=lambda _: jumper, close=lambda: None))
    target = BitgetDepositTarget("USDC", 8453, e["target"]["asset_id"], 1, "BASE", 6)
    monkeypatch.setattr(execution, "_bitget_client", lambda: SimpleNamespace(
        revalidate_deposit_target=lambda *a, **kw: target))
    monkeypatch.setattr(execution, "sign_transaction",
                        lambda *a, **kw: pytest.fail("no repeated withdrawal or bridge signing"))

    def run():
        return _execute_live_groups([e], wallets={WALLET: wallet}, rpc_urls={10: "rpc"},
            journal_path=path, max_route_loss_pct=Decimal("15"),
            now_ms=lambda: int(clock[0] * 1000), delay_min_seconds=0, delay_max_seconds=0,
            sleep=lambda _: None, rng=None)

    assert run()["pending"] == 1
    expected = 1 if failure_stage == "initial" else 2
    assert len(quotes) == expected
    assert len(estimates) == expected
    with Journal(path) as journal:
        assert journal.position(pid)["state"] == "source_asset_converted_bridge_pending"
        retry = journal.checkpoint(pid, "automatic_retry")
        assert retry["retry_at"] >= now.timestamp() + 240
        assert "execution reverted" in json.dumps(
            journal.checkpoint(pid, "execution_failure"))
        assert len(journal.connection.execute("SELECT * FROM route_steps").fetchall()) == 1
    clock[0] += 1
    assert run()["pending"] == 1
    assert len(quotes) == len(estimates) == expected


@pytest.mark.parametrize("damage", ["hash", "owner", "amount", "pending", "null_hash",
                                   "unrelated_intent", "approval_intent"])
def test_unsigned_bridge_exception_requires_exact_confirmed_source(tmp_path, damage):
    from evm_inventory.canonical_source_bootstrap import (
        confirmed_unsigned_source_bridge,
        require_source_bridge_retry_due,
    )

    with Journal(tmp_path / "fake.sqlite") as journal:
        e, _, _, pid = confirmed_source(journal)
        assert confirmed_unsigned_source_bridge(journal, pid, e)
        result = journal.checkpoint(pid, "swap_result")
        if damage in {"hash", "owner", "amount"}:
            result["source_receipt_proof"][{"hash": "tx_hash", "owner": "owner",
                                            "amount": "amount_raw"}[damage]] = {
                "hash": "0x" + "f" * 64, "owner": DEPOSIT, "amount": str(AMOUNT + 1),
            }[damage]
            journal.save_checkpoint(pid, "swap_result", result)
        elif damage == "pending":
            journal.connection.execute(
                "UPDATE route_steps SET state='submitted' WHERE position_id=?", (pid,))
            journal.connection.commit()
        elif damage == "null_hash":
            journal.connection.execute("UPDATE route_steps SET tx_hash=NULL WHERE position_id=?",
                                       (pid,))
            journal.connection.commit()
        else:
            journal.record_step_intent(position_id=pid,
                step_key="approval:foreign:0" if damage == "approval_intent" else "foreign:0",
                nonce=1, calldata_digest="c" * 64, signed_payload_digest="d" * 64, tx_hash=None)
        assert not confirmed_unsigned_source_bridge(journal, pid, e)
        with pytest.raises(ValueError, match="confirmation_proof_invalid"):
            require_source_bridge_retry_due(journal, pid, e, now_seconds=1000)
