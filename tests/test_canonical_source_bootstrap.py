from copy import deepcopy
from types import SimpleNamespace

import pytest

from evm_inventory.journal import Journal
from evm_inventory.lifi import TransactionRequest
from evm_inventory.route_execution import _position_key
from evm_inventory.weth import WETH_CONTRACTS

WALLET = "0x" + "1" * 40
DEPOSIT = "0x" + "2" * 40
AMOUNT = 105277904533522
REASON = "insufficient native balance for gas reserve: balance=2; required=3"


def entry():
    return dict(
        wallet=WALLET,
        deposit_address=DEPOSIT,
        chain_id=10,
        asset_id=WETH_CONTRACTS[10],
        decimals=18,
        raw_balance=str(AMOUNT),
        status="route_ready",
        automatic_requote=True,
        steps=[{"kind": "bridge"}],
        target=dict(
            coin="USDC",
            chain_id=8453,
            chain="BASE",
            asset_id="0x" + "3" * 40,
            decimals=6,
            minimum_raw="1",
        ),
        group=dict(
            key="g",
            wallet=WALLET,
            source_chain_id=10,
            source_usd="0.2692061296826691062",
            max_route_loss_pct="15",
        ),
    )


def halted(journal, original):
    g = journal.get_or_create_group(
        group_key="g",
        wallet=WALLET,
        source_chain_id=10,
        source_usd=original["group"]["source_usd"],
        loss_budget_pct="15",
    )
    p = journal.get_or_create_position(
        position_key=_position_key(original),
        wallet=WALLET,
        group_id=g["id"],
        source_asset_id=original["asset_id"],
        source_amount_raw=original["raw_balance"],
    )
    journal.save_checkpoint(p["id"], "prepared", original)
    journal.record_position_state(p["id"], "manual_review", reason=REASON)
    journal.record_group_state(g["id"], "active")
    journal.record_group_state(g["id"], "manual_review_group_halted", reason=REASON)
    return g["id"], p["id"]


def test_bootstrap_migration_retains_original_identity_and_paid_budget(tmp_path):
    from evm_inventory.canonical_source_bootstrap import migrate_source_bootstraps

    original = entry()
    with Journal(tmp_path / "fake.sqlite") as journal:
        gid, pid = halted(journal, original)
        journal.connection.execute(
            "UPDATE route_groups SET realized_loss_usd=? WHERE id=?", ("0.001", gid)
        )
        journal.connection.execute(
            "UPDATE route_positions SET accounted_loss_usd=? WHERE id=?", ("0.001", pid)
        )
        journal.connection.commit()
        changed = migrate_source_bootstraps(
            [original],
            journal=journal,
            wallet=SimpleNamespace(public_address=WALLET, bitget_deposit_address=DEPOSIT),
            balance=lambda _: AMOUNT,
        )
        assert changed == 1
        assert original["canonical_source_bootstrap"]["amount_raw"] == str(AMOUNT)
        assert original["target"] == entry()["target"]
        assert _position_key(original) == _position_key(entry())
        assert journal.group(gid)["source_usd"] == entry()["group"]["source_usd"]
        assert journal.group(gid)["realized_loss_usd"] == "0.001"
        assert journal.position(pid)["accounted_loss_usd"] == "0.001"
        assert journal.position(pid)["state"] == "planned"
        assert journal.group(gid)["state"] == "active"


@pytest.mark.parametrize(
    "change",
    [
        "contract",
        "chain",
        "owner",
        "decimals",
        "amount",
        "target",
        "recipient",
        "membership",
        "intent",
        "approval",
        "null_hash",
        "balance",
        "budget",
    ],
)
def test_migration_rejects_nonexact_or_durable_sources(tmp_path, change):
    from evm_inventory.canonical_source_bootstrap import migrate_source_bootstraps

    original = entry()
    with Journal(tmp_path / "fake.sqlite") as journal:
        gid, pid = halted(journal, original)
        proposed = deepcopy(original)
        if change == "contract":
            proposed["asset_id"] = "0x" + "4" * 40
        if change == "chain":
            proposed["chain_id"] = 8453
        if change == "owner":
            proposed["wallet"] = DEPOSIT
        if change == "decimals":
            proposed["decimals"] = 6
        if change == "amount":
            proposed["raw_balance"] = str(AMOUNT - 1)
        if change == "target":
            proposed["target"]["coin"] = "ETH"
        if change == "recipient":
            proposed["deposit_address"] = WALLET
        if change == "membership":
            journal.get_or_create_position(position_key="g:native", wallet=WALLET, group_id=gid)
        if change in {"intent", "approval", "null_hash"}:
            journal.record_step_intent(
                position_id=pid,
                step_key="approval:x" if change == "approval" else "bridge:x:0",
                nonce=0,
                calldata_digest="a" * 64,
                signed_payload_digest="b" * 64,
                tx_hash=None if change == "null_hash" else "0x" + "a" * 64,
            )
        if change == "budget":
            journal.connection.execute(
                "UPDATE route_groups SET realized_loss_usd=? WHERE id=?", ("1", gid)
            )
            journal.connection.commit()
        assert (
            migrate_source_bootstraps(
                [proposed],
                journal=journal,
                wallet=SimpleNamespace(public_address=WALLET, bitget_deposit_address=DEPOSIT),
                balance=lambda _: AMOUNT - 1 if change == "balance" else AMOUNT,
            )
            == 0
        )
        assert not proposed.get("canonical_source_bootstrap")
        assert journal.group(gid)["state"] == "manual_review_group_halted"


def test_forecast_rpc_adds_only_exact_owner_balance_and_delegates_real_reads():
    from evm_inventory.canonical_source_bootstrap import ForecastRpc

    calls = []
    rpc = SimpleNamespace(
        call=lambda url, method, params: (
            calls.append((method, params)) or ("0xa" if method == "eth_chainId" else "0x123")
        )
    )
    request = TransactionRequest(10, DEPOSIT, "0x1234", 100, 0, 0)
    proxy = ForecastRpc(rpc, url="rpc", chain_id=10, owner=WALLET, projected_balance=1000)
    proxy.allow(request)
    tx = dict(from_=WALLET, to=DEPOSIT, data="0x1234", value="0x64")
    tx["from"] = tx.pop("from_")
    assert proxy.call("rpc", "eth_estimateGas", [tx, "latest"]) == "0x123"
    assert calls[-1] == ("eth_estimateGas", [tx, "latest", {WALLET: {"balance": "0x3e8"}}])
    assert proxy.call("rpc", "eth_getBalance", [WALLET, "latest"]) == "0x123"
    assert len(calls[-1][1]) == 2


@pytest.mark.parametrize(
    "change", ["owner", "to", "data", "value", "chain", "url", "override", "nonce", "block"]
)
def test_forecast_rpc_rejects_changed_identity_or_any_extra_override(change):
    from evm_inventory.canonical_source_bootstrap import ForecastRpc

    rpc = SimpleNamespace(call=lambda u, m, p: "0xa" if m == "eth_chainId" else "0x1")
    proxy = ForecastRpc(rpc, url="rpc", chain_id=10, owner=WALLET, projected_balance=1000)
    request = TransactionRequest(10, DEPOSIT, "0x1234", 100, 0, 0)
    proxy.allow(request)
    tx = {"from": WALLET, "to": DEPOSIT, "data": "0x1234", "value": "0x64"}
    params = [tx, "latest"]
    url = "rpc"
    if change == "owner":
        tx["from"] = DEPOSIT
    if change == "to":
        tx["to"] = WALLET
    if change == "data":
        tx["data"] = "0x5678"
    if change == "value":
        tx["value"] = "0x3e9"
    if change == "chain":
        with pytest.raises(ValueError, match="forecast"):
            proxy.allow(TransactionRequest(8453, DEPOSIT, "0x1234", 100, 0, 0))
        return
    if change == "url":
        url = "other"
    if change == "override":
        params.append({WALLET: {"code": "0x"}})
    if change == "nonce":
        tx["nonce"] = "0x0"
    if change == "block":
        params[1] = "pending"
    with pytest.raises(ValueError, match="forecast"):
        proxy.call(url, "eth_estimateGas", params)


def test_forecast_marker_rejected_before_any_fee_or_signing(monkeypatch):
    from evm_inventory.route_execution import _execute_entry

    monkeypatch.setattr(
        "evm_inventory.route_execution.FeePlanner.plan",
        lambda *a, **k: pytest.fail("forecast reached fee planner"),
    )
    monkeypatch.setattr(
        "evm_inventory.route_execution.sign_transaction",
        lambda *a, **k: pytest.fail("forecast reached signer"),
    )
    with pytest.raises(ValueError, match="pending_source_conversion"):
        _execute_entry(
            entry={**entry(), "pending_source_conversion": True},
            wallet=object(),
            rpc=object(),
            jumper=object(),
            rpc_urls={},
        )


def test_atomic_source_checkpoint_and_cumulative_cost_survive_restart(tmp_path):
    result = dict(
        state="confirmed",
        actual_asset_id="native",
        actual_balance_raw="100",
        realized_loss_usd="0.001",
    )
    path = tmp_path / "fake.sqlite"
    with Journal(path) as journal:
        original = entry()
        gid, pid = halted(journal, original)
        journal.connection.execute("UPDATE route_groups SET state='active' WHERE id=?", (gid,))
        journal.connection.execute(
            "UPDATE route_positions SET state='swap_submitted' WHERE id=?", (pid,)
        )
        journal.connection.commit()
        journal.checkpoint_source_conversion(pid, result)
    with Journal(path) as journal:
        assert journal.checkpoint(pid, "swap_result") == result
        assert journal.position(pid)["actual_balance_raw"] == "100"
        assert journal.position(pid)["accounted_loss_usd"] == "0.001"
        assert journal.group(gid)["realized_loss_usd"] == "0.001"
        journal.checkpoint_source_conversion(pid, result)
        assert journal.account_position_loss(pid, loss_usd="0.004") == "0.004"
        assert journal.account_position_loss(pid, loss_usd="0.004") == "0.004"
        assert journal.account_position_loss(pid, loss_usd="0.003") == "0.004"


def bootstrap_entry(cap=1000):
    from evm_inventory.lifi import LifiRouteRequest, _route_from_dict
    from evm_inventory.weth import NATIVE, unwrap_route_payload

    e = entry()
    e["canonical_source_bootstrap"] = dict(
        wallet=WALLET,
        recipient=DEPOSIT,
        chain_id=10,
        asset_id=WETH_CONTRACTS[10],
        decimals=18,
        amount_raw=str(AMOUNT),
        target=e["target"],
        group_key="g",
        source_usd=e["group"]["source_usd"],
        loss_budget_pct="15",
        paid_loss_usd="0",
        accounted_loss_usd="0",
        source_fee_cap_raw=str(cap),
    )
    route = _route_from_dict(
        unwrap_route_payload(
            LifiRouteRequest(10, 10, WETH_CONTRACTS[10], NATIVE, str(AMOUNT), WALLET, WALLET),
            "2500",
            "2500",
        ),
        quote_timestamp="2026-10-13T00:00:00+00:00",
    )
    from test_live_plan import _quoted_route

    from evm_inventory.canonical_source_bootstrap import future_bridge_proof

    future = _quoted_route(
        LifiRouteRequest(
            10, 8453, NATIVE, e["target"]["asset_id"], str(AMOUNT - cap - 3000), WALLET, DEPOSIT
        ),
        route_id="future",
        output_amount=260000,
    )
    future_tx = TransactionRequest(
        10,
        DEPOSIT,
        "0x1234",
        AMOUNT - cap - 3000,
        10,
        100,
        max_total_fee_cap_wei=1000,
        fee_quote_fallback_reason="pending_source_conversion",
    )
    future_step = dict(
        kind="bridge",
        _live_route=future,
        pending_source_conversion=True,
        _prepared_requests=(future_tx,),
    )
    e["canonical_source_bootstrap"].update(
        future_balance_raw=str(AMOUNT - cap), future_source_fee_reserve_raw="3000"
    )
    e["canonical_source_bootstrap"]["forecast_proof"] = future_bridge_proof(
        future_step, e["canonical_source_bootstrap"]
    )
    e["valuation"] = {"loss_usd": "0.005"}
    e.update(
        _journal_step_key="swap_to_native:canonical:0",
        _check_group_loss=lambda _: None,
        steps=[dict(kind="swap_to_native", _live_route=route), future_step],
    )
    request = TransactionRequest(
        10,
        WETH_CONTRACTS[10],
        "0x2e1a7d4d" + f"{AMOUNT:064x}",
        0,
        10,
        100,
        max_total_fee_cap_wei=cap,
    )
    return e, request, SimpleNamespace(public_address=WALLET, bitget_deposit_address=DEPOSIT)


def test_source_withdrawal_proof_is_separate_from_postbridge_proof():
    from evm_inventory.canonical_source_bootstrap import source_withdrawal_reserve
    from evm_inventory.weth_continuation import withdrawal_reserve

    e, request, wallet = bootstrap_entry()
    assert source_withdrawal_reserve(e, request, wallet) == 1200
    assert withdrawal_reserve(e, request, wallet) is None
    e["_journal_step_key"] = "bridge:x:0"
    assert source_withdrawal_reserve(e, request, wallet) is None


@pytest.mark.parametrize(
    "change",
    [
        "owner",
        "recipient",
        "target",
        "amount",
        "contract",
        "decimals",
        "cap",
        "callback",
        "approval",
        "shape",
    ],
)
def test_source_withdrawal_proof_rejects_changed_fields(change):
    from dataclasses import replace

    from evm_inventory.canonical_source_bootstrap import source_withdrawal_reserve

    e, request, wallet = bootstrap_entry()
    if change == "owner":
        e["canonical_source_bootstrap"]["wallet"] = DEPOSIT
    if change == "recipient":
        e["canonical_source_bootstrap"]["recipient"] = WALLET
    if change == "target":
        e["canonical_source_bootstrap"]["target"] = {}
    if change == "amount":
        e["canonical_source_bootstrap"]["amount_raw"] = "1"
    if change == "contract":
        request = replace(request, to=DEPOSIT)
    if change == "decimals":
        e["decimals"] = 6
    if change == "cap":
        request = replace(request, max_total_fee_cap_wei=999)
    if change == "callback":
        e.pop("_check_group_loss")
    if change == "approval":
        e["steps"][0]["_live_route"].raw_steps[0]["estimate"]["approvalAddress"] = DEPOSIT
    if change == "shape":
        e["steps"].pop()
    with pytest.raises(ValueError, match="canonical_source_bootstrap"):
        source_withdrawal_reserve(e, request, wallet)


def test_source_output_excludes_unrelated_inbound_and_protects_initial_reserve():
    from evm_inventory.canonical_source_bootstrap import source_output

    assert source_output(amount=1000, paid_gas=100, initial_native=200, current_native=5000) == 900
    assert source_output(amount=1000, paid_gas=100, initial_native=200, current_native=600) == 400
    with pytest.raises(ValueError, match="output"):
        source_output(amount=1000, paid_gas=100, initial_native=200, current_native=100)


@pytest.mark.parametrize("production_wrapper", [False, True])
def test_source_bootstrap_preflight_models_future_without_fabricating_actual_output(
    monkeypatch,
    tmp_path,
    production_wrapper,
):
    from datetime import UTC, datetime

    from test_live_plan import _quoted_route

    from evm_inventory.bitget_catalog import BitgetDepositTarget
    from evm_inventory.lifi import LifiPriceEvidence, LifiRouteRequest
    from evm_inventory.live_plan import _route_data
    from evm_inventory.planner_gas import PlannerGasEstimator
    from evm_inventory.route_execution import _preflight_live_entry

    e, request, wallet = bootstrap_entry()
    e.pop("_journal_step_key")
    for key in (
        "source_fee_cap_raw",
        "future_balance_raw",
        "future_source_fee_reserve_raw",
        "forecast_proof",
    ):
        e["canonical_source_bootstrap"].pop(key)
    e["canonical_source_bootstrap"].update(paid_loss_usd="0", accounted_loss_usd="0")
    now = int(datetime.now(UTC).timestamp() * 1000)
    estimates = []
    source_fees = [1000, 1001]

    class Rpc:
        def call(self, url, method, params):
            if method == "eth_chainId":
                return "0xa"
            if method == "eth_getBalance":
                return hex(2000)
            if method == "eth_call":
                return "0x" + f"{AMOUNT:064x}"
            if method == "eth_estimateGas":
                estimates.append(params)
                return hex(10)
            raise AssertionError(method)

    # An actual estimator consumes the wrapper; all fee repricing stays deterministic here.
    def fee_plan(self, url, req, sender, **kw):
        self.rpc.call(
            url,
            "eth_estimateGas",
            [{"from": sender, "to": req.to, "data": req.data, "value": hex(req.value)}, "latest"],
        )
        from dataclasses import replace

        cost = (
            source_fees.pop(0)
            if req.to == WETH_CONTRACTS[10]
            else 1001
            if req.max_total_fee_cap_wei
            else 1000
        )
        if req.max_total_fee_cap_wei is not None and cost > req.max_total_fee_cap_wei:
            raise ValueError("fee_cap_changed")
        return replace(
            req,
            gas_limit=10,
            gas_price_wei=100,
            additional_fee_wei=cost - 1000,
            max_total_fee_cap_wei=cost,
        )

    monkeypatch.setattr("evm_inventory.fee_planner.FeePlanner.plan", fee_plan)
    target = BitgetDepositTarget("USDC", 8453, e["target"]["asset_id"], 1, "BASE", 6)
    future_calls = []

    def future(original, amount, **kw):
        future_calls.append(amount)
        route = _quoted_route(
            LifiRouteRequest(
                10, 8453, "0x" + "0" * 40, target.asset_id, str(amount - 3600), WALLET, DEPOSIT
            ),
            output_amount=260000,
            route_id="future",
        )
        import json

        from evm_inventory.lifi import _route_from_dict

        payload = json.loads(route.evidence.canonical_payload_json)
        payload["steps"][0]["action"]["fromAddress"] = WALLET
        payload["priceTimestamp"] = datetime.fromtimestamp((now - 100) / 1000, UTC).isoformat()
        for token in ("fromToken", "toToken"):
            payload["steps"][0]["action"][token]["priceTimestamp"] = payload["priceTimestamp"]
        for cost in payload.get("gasCosts", []):
            cost["timestamp"] = payload["priceTimestamp"]
        route = _route_from_dict(
            payload, quote_timestamp=datetime.fromtimestamp((now - 100) / 1000, UTC).isoformat()
        )
        tx = TransactionRequest(10, DEPOSIT, "0x1234", amount - 3600, 10, 100)
        caps = kw["gas_estimator"].estimate_prepared_route(
            route=route, wallet=WALLET, prepared_transactions=(tx,)
        )
        assert caps
        return dict(
            kind="bridge",
            route=_route_data(route),
            _live_route=route,
            _prepared_requests=(tx,),
            valuation=dict(loss_usd="0.005", loss_pct="1"),
        )

    monkeypatch.setattr("evm_inventory.route_execution._requote_bridge_after_swap", future)
    rpc = Rpc()
    jumper = SimpleNamespace(
        token_price=lambda asset: LifiPriceEvidence(
            asset, "2500", datetime.fromtimestamp((now - 100) / 1000, UTC).isoformat()
        ),
        step_transaction=lambda _: request,
    )
    bitget = SimpleNamespace(revalidate_deposit_target=lambda *a, **k: target)
    if production_wrapper:
        from decimal import Decimal

        from evm_inventory.route_execution import _execute_live_groups
        from evm_inventory.stages import StageDeadlineExceeded

        path = tmp_path / "fake.sqlite"
        with Journal(path) as journal:
            group = journal.get_or_create_group(
                group_key="g",
                wallet=WALLET,
                source_chain_id=10,
                source_usd=e["group"]["source_usd"],
                loss_budget_pct="15",
            )
            journal.save_checkpoint(
                journal.get_or_create_position(
                    position_key=_position_key(e),
                    wallet=WALLET,
                    group_id=group["id"],
                    source_asset_id=e["asset_id"],
                    source_amount_raw=e["raw_balance"],
                )["id"],
                "canonical_source_bootstrap",
                e["canonical_source_bootstrap"],
            )
        monkeypatch.setattr("evm_inventory.route_execution.ExecutionRpc", lambda _: rpc)
        monkeypatch.setattr(
            "evm_inventory.route_execution.WalletLifiClients",
            lambda *a, **k: SimpleNamespace(for_wallet=lambda _: jumper, close=lambda: None),
        )
        monkeypatch.setattr("evm_inventory.route_execution._bitget_client", lambda: bitget)

        def defer(*a, **k):
            raise StageDeadlineExceeded("fee_wait")

        monkeypatch.setattr("evm_inventory.route_execution._submit_live_step", defer)

        def production_round():
            result = _execute_live_groups(
                [e],
                wallets={WALLET: wallet},
                rpc_urls={10: "rpc"},
                journal_path=path,
                max_route_loss_pct=Decimal("15"),
                now_ms=lambda: now,
                delay_min_seconds=0,
                delay_max_seconds=0,
                sleep=lambda _: None,
                rng=None,
            )
            assert result["deferred"] == 1
            with Journal(path) as journal:
                p = journal.connection.execute("SELECT id FROM route_positions").fetchone()
                return journal.checkpoint(p["id"], "source_bootstrap_preflight")

        fresh = production_round()
    else:
        fresh = _preflight_live_entry(
            e,
            wallet=wallet,
            rpc=rpc,
            rpc_urls={10: "rpc"},
            jumper=jumper,
            bitget=SimpleNamespace(revalidate_deposit_target=lambda *a, **k: target),
            gas_estimator=PlannerGasEstimator(rpc, {10: "rpc"}, jumper),
            now_ms=lambda: now,
        )
    assert [s["kind"] for s in fresh["steps"]] == ["swap_to_native", "bridge"]
    assert not any(k.startswith("_actual") for k in fresh)
    assert fresh["steps"][1]["pending_source_conversion"]
    assert fresh["canonical_source_bootstrap"]["future_balance_raw"] == str(AMOUNT - 1200)
    assert estimates[0][-1] == "latest"
    assert estimates[-1][2] == {WALLET: {"balance": hex(AMOUNT - 1200)}}
    # A renewed withdrawal estimate that remains within the saved cap reuses the future quote.
    if production_wrapper:
        again = production_round()
    else:
        again = _preflight_live_entry(
            fresh,
            wallet=wallet,
            rpc=rpc,
            rpc_urls={10: "rpc"},
            jumper=jumper,
            bitget=SimpleNamespace(revalidate_deposit_target=lambda *a, **k: target),
            gas_estimator=PlannerGasEstimator(rpc, {10: "rpc"}, jumper),
            now_ms=lambda: now + 3000,
        )
    assert again["steps"][1]["route"]["id"] == "future"
    assert len(future_calls) == 1


@pytest.mark.parametrize("canonical", [True, False])
def test_executor_accepts_source_reserve_only_with_separate_proof(monkeypatch, canonical):
    from evm_inventory.route_execution import _execute_entry

    e, request, wallet = bootstrap_entry()
    e.update(
        status="route_ready",
        quoted_at=1800000000000,
        route={"step": e["steps"][0]["_live_route"].raw_steps[0]},
    )
    wallet.private_key = "fake"
    if not canonical:
        e.pop("canonical_source_bootstrap")
    signed = []
    for name, replacement in {
        "FeePlanner.plan": lambda *a, **k: request,
        "_native_balance": lambda *a, **k: 1300,
        "pending_nonce": lambda *a, **k: 1,
        "sign_transaction": lambda *a, **k: signed.append(request) or "0x01",
        "broadcast_signed_transaction": lambda *a, **k: "0x" + "a" * 64,
        "wait_for_receipt": lambda *a, **k: {},
    }.items():
        monkeypatch.setattr("evm_inventory.route_execution." + name, replacement)
    kw = dict(
        entry=e,
        prepared_request=request,
        wallet=wallet,
        rpc=object(),
        jumper=object(),
        rpc_urls={10: "rpc"},
        now_ms=lambda: 1800000000000,
    )
    if canonical:
        assert _execute_entry(**kw) == "0x" + "a" * 64
        assert signed == [request]
    else:
        with pytest.raises(ValueError, match="insufficient native balance"):
            _execute_entry(**kw)
        assert signed == []


def test_source_intent_replay_keeps_saved_nonce_hash_payload_and_fee_cap(tmp_path, monkeypatch):
    from dataclasses import replace
    from hashlib import sha256

    from evm_inventory.executor import signed_transaction_hash
    from evm_inventory.route_execution import _execute_entry

    e, request, wallet = bootstrap_entry()
    wallet.private_key = "fake"
    raw = "0x01"
    tx_hash = signed_transaction_hash(raw)
    signed, sent = [], []

    class Rpc:
        def call(self, u, method, params):
            if method in {"eth_getTransactionReceipt", "eth_getTransactionByHash"}:
                return None
            if method == "eth_getTransactionCount":
                return "0x0"
            if method == "eth_sendRawTransaction":
                sent.append(params[0])
                return tx_hash
            raise AssertionError(method)

    monkeypatch.setattr(
        "evm_inventory.route_execution.sign_transaction",
        lambda req, **kw: signed.append((req, kw["nonce"])) or raw,
    )
    monkeypatch.setattr("evm_inventory.route_execution.wait_for_receipt", lambda *a, **k: {})
    with Journal(tmp_path / "fake.sqlite") as journal:
        pid = journal.get_or_create_position(position_key="p", wallet=WALLET)["id"]
        key = e["_journal_step_key"]
        journal.save_checkpoint(pid, f"canonical_signing:{key}", dict(entry=e, request=request))
        journal.record_step_intent(
            position_id=pid,
            step_key=key,
            nonce=0,
            calldata_digest=sha256(request.data.encode()).hexdigest(),
            signed_payload_digest=sha256(raw.encode()).hexdigest(),
            tx_hash=tx_hash,
        )
        assert (
            _execute_entry(
                entry=e,
                prepared_request=replace(request, gas_price_wei=1),
                wallet=wallet,
                rpc=Rpc(),
                jumper=object(),
                rpc_urls={10: "rpc"},
                journal=journal,
                position_id=pid,
            )
            == tx_hash
        )
        assert journal.latest_step(position_id=pid, step_key=key)["nonce"] == 0
    assert sent == [raw] and signed == [(request, 0)]


def test_request_forecast_marker_rejected_even_when_entry_marker_absent():
    from dataclasses import replace

    from evm_inventory.route_execution import _execute_entry

    e, request, wallet = bootstrap_entry()
    with pytest.raises(ValueError, match="pending_source_conversion"):
        _execute_entry(
            entry=e,
            prepared_request=replace(
                request, fee_quote_fallback_reason="pending_source_conversion"
            ),
            wallet=wallet,
            rpc=object(),
            jumper=object(),
            rpc_urls={10: "rpc"},
        )


@pytest.mark.parametrize("change", ["", "owner", "contract", "amount", "status", "hash", "value"])
def test_source_receipt_requires_exact_successful_withdrawal_effect(change):
    from eth_utils import keccak

    from evm_inventory.canonical_source_bootstrap import require_source_receipt

    e, request, wallet = bootstrap_entry()
    tx_hash = "0x" + "a" * 64
    tx = dict(hash=tx_hash, to=request.to, input=request.data, value="0x0", **{"from": WALLET})
    log = dict(
        address=request.to,
        topics=[
            "0x" + keccak(text="Withdrawal(address,uint256)").hex(),
            "0x" + WALLET[2:].rjust(64, "0"),
        ],
        data="0x" + f"{AMOUNT:064x}",
    )
    receipt = dict(
        transactionHash=tx_hash, status="0x1", logs=[log], gasUsed="0x5", effectiveGasPrice="0x64"
    )
    if change == "owner":
        tx["from"] = DEPOSIT
    if change == "contract":
        log["address"] = DEPOSIT
    if change == "amount":
        log["data"] = "0x" + f"{AMOUNT - 1:064x}"
    if change == "status":
        receipt["status"] = "0x0"
    if change == "hash":
        receipt["transactionHash"] = "0x" + "b" * 64
    if change == "value":
        tx["value"] = "0x1"
    rpc = SimpleNamespace(call=lambda u, m, p: receipt if m == "eth_getTransactionReceipt" else tx)
    if change:
        with pytest.raises(ValueError, match="source_receipt"):
            require_source_receipt(e, tx_hash=tx_hash, rpc=rpc, url="rpc", wallet=wallet)
    else:
        proof = require_source_receipt(e, tx_hash=tx_hash, rpc=rpc, url="rpc", wallet=wallet)
        assert proof["amount_raw"] == str(AMOUNT) and proof["tx_hash"] == tx_hash


def test_source_checkpoint_rolls_back_output_and_cost_on_crash(tmp_path):
    original = entry()
    with Journal(tmp_path / "fake.sqlite") as journal:
        gid, pid = halted(journal, original)
        journal.connection.execute("UPDATE route_groups SET state='active' WHERE id=?", (gid,))
        journal.connection.execute(
            "UPDATE route_positions SET state='swap_submitted' WHERE id=?", (pid,)
        )
        journal.connection.execute(
            "CREATE TEMP TRIGGER fail_cost BEFORE UPDATE OF realized_loss_usd "
            "ON route_groups BEGIN SELECT RAISE(ABORT,'simulated crash'); END"
        )
        journal.connection.commit()
        result = dict(
            state="confirmed",
            actual_asset_id="native",
            actual_balance_raw="100",
            realized_loss_usd="0.001",
        )
        with pytest.raises(Exception, match="simulated crash"):
            journal.checkpoint_source_conversion(pid, result)
        assert journal.checkpoint(pid, "swap_result") is None
        assert journal.position(pid)["actual_balance_raw"] is None
        assert journal.group(gid)["realized_loss_usd"] == "0"
        journal.connection.execute("DROP TRIGGER fail_cost")
        journal.checkpoint_source_conversion(pid, result)
        assert journal.group(gid)["realized_loss_usd"] == "0.001"


@pytest.mark.parametrize("pending", [True, False])
def test_swap_source_and_final_costs_are_cumulative_for_fast_and_pending_credit(tmp_path, pending):
    from decimal import Decimal

    from test_route_execution import ASSET_A, _entry

    from evm_inventory.route_execution import execute_group_entries

    swap = dict(
        kind="swap_to_native", route=dict(id="source", evidence={"payload_sha256": "a" * 64})
    )
    old = dict(kind="bridge", route=dict(id="old", evidence={"payload_sha256": "b" * 64}))
    new = dict(
        kind="bridge",
        route=dict(id="new", from_amount="70", evidence={"payload_sha256": "c" * 64}),
        valuation={"loss_usd": "3"},
    )
    e = {**_entry(ASSET_A, status="route_ready", steps=[swap, old]), "automatic_requote": True}
    e["canonical_source_bootstrap"] = {"accounted_loss_usd": "0"}
    with Journal(tmp_path / "fake.sqlite") as journal:

        def submit(fresh, step):
            if step["kind"] == "swap_to_native":
                return dict(
                    state="confirmed",
                    actual_asset_id="native",
                    actual_balance_raw="100",
                    realized_loss_usd="2",
                    protected_native_raw="0",
                )
            assert fresh["_realized_loss_before_current_usd"] == "2"
            assert not step.get("pending_source_conversion")
            return dict(state="deposit_pending" if pending else "completed", realized_loss_usd="5")

        result = execute_group_entries(
            [e],
            journal=journal,
            max_route_loss_pct=Decimal("15"),
            preflight=lambda x: {
                **x,
                "valuation": {"loss_usd": "5" if x.get("_actual_balance_raw") else "5"},
            },
            submit_step=submit,
            requote_after_swap=lambda *a: new,
        )
        assert result["deposit_pending" if pending else "completed"] == 1
        p = journal.connection.execute("SELECT * FROM route_positions").fetchone()
        assert journal.group(p["group_id"])["realized_loss_usd"] == "5"
        assert journal.position(p["id"])["accounted_loss_usd"] == "5"
        if pending:
            assert journal.position(p["id"])["state"] == "deposit_pending"
        assert journal.checkpoint(p["id"], "swap_result")["realized_loss_usd"] == "2"


def test_source_conversion_retry_preserves_checkpoint_and_does_not_withdraw_again(tmp_path):
    from decimal import Decimal

    from test_route_execution import ASSET_A, _entry

    from evm_inventory.route_execution import execute_group_entries
    from evm_inventory.stages import StageDeadlineExceeded

    swap = dict(
        kind="swap_to_native", route=dict(id="source", evidence={"payload_sha256": "a" * 64})
    )
    bridge = dict(kind="bridge", route=dict(id="bridge", evidence={"payload_sha256": "b" * 64}))
    e = {**_entry(ASSET_A, status="route_ready", steps=[swap, bridge]), "automatic_requote": True}
    calls = []
    with Journal(tmp_path / "fake.sqlite") as journal:

        def submit(fresh, step):
            calls.append(step["kind"])
            return dict(
                state="confirmed",
                actual_asset_id="native",
                actual_balance_raw="100",
                realized_loss_usd="2",
            )

        def quote(*a):
            raise StageDeadlineExceeded("quota_wait")

        for _ in range(2):
            result = execute_group_entries(
                [e],
                journal=journal,
                max_route_loss_pct=Decimal("15"),
                preflight=lambda x: x,
                submit_step=submit,
                requote_after_swap=quote,
            )
            assert result.get("deferred", result.get("pending")) == 1
        p = journal.connection.execute("SELECT * FROM route_positions").fetchone()
        assert journal.group(p["group_id"])["realized_loss_usd"] == "2"
        assert p["actual_balance_raw"] == "100"
    assert calls == ["swap_to_native"]


def test_collection_migration_uses_same_database_and_rechecks_live_exact_balance(tmp_path):
    from evm_inventory.collection import _migrate_canonical_sources

    e = entry()
    db = tmp_path / "fake.sqlite"
    with Journal(db) as journal:
        gid, pid = halted(journal, e)
    calls = []

    class Rpc:
        def call(self, url, method, params):
            calls.append(method)
            if method == "eth_chainId":
                return "0xa"
            if method == "eth_call":
                return "0x" + f"{AMOUNT:064x}"
            raise AssertionError(method)

    wallet = SimpleNamespace(public_address=WALLET, bitget_deposit_address=DEPOSIT)
    assert _migrate_canonical_sources([e], db, wallet=wallet, rpc_urls={10: "rpc"}, rpc=Rpc()) == 1
    assert calls == ["eth_chainId", "eth_call"]
    with Journal(db) as journal:
        assert journal.position(pid)["state"] == "planned"
        assert journal.group(gid)["state"] == "active"
    # A crash between migration commit and collection plan save restores the committed marker.
    old = entry()
    assert (
        _migrate_canonical_sources([old], db, wallet=wallet, rpc_urls={10: "rpc"}, rpc=Rpc()) == 1
    )
    assert old["canonical_source_bootstrap"] == e["canonical_source_bootstrap"]
    assert calls == ["eth_chainId", "eth_call"]


def test_driver_reports_incomplete_when_no_executable_positions_but_unresolved_remain(tmp_path):
    from evm_inventory.collection import drive_execution

    plan = {
        "entries": [
            dict(
                wallet=WALLET,
                chain_id=10,
                asset_id=WETH_CONTRACTS[10],
                status="unavailable",
                reason="quota_wait",
            )
        ]
    }
    result = drive_execution(
        plan,
        tmp_path / "fake.sqlite",
        execute_round=lambda _: pytest.fail(),
        until=10,
        clock=lambda: 0,
    )
    assert result["status"] == "incomplete"
    assert result["reason"] == "planning_incomplete"


def test_full_bootstrap_uses_actual_withdrawal_and_normal_rpc_for_future_bridge(
    tmp_path, monkeypatch
):
    import json
    from dataclasses import replace
    from datetime import UTC, datetime
    from decimal import Decimal

    from eth_utils import keccak
    from test_live_plan import _quoted_route

    from evm_inventory.bitget_catalog import BitgetDepositTarget
    from evm_inventory.lifi import LifiPriceEvidence, _route_from_dict
    from evm_inventory.planner_gas import PlannerGasEstimator
    from evm_inventory.route_execution import (
        _preflight_live_entry,
        _requote_bridge_after_swap,
        _submit_live_step,
        execute_group_entries,
    )

    original = entry()
    now = int(datetime.now(UTC).timestamp() * 1000)
    target = BitgetDepositTarget("USDC", 8453, original["target"]["asset_id"], 1, "BASE", 6)
    wallet = SimpleNamespace(public_address=WALLET, bitget_deposit_address=DEPOSIT)
    calls, sends, receipts = [], [], {}
    state = {"native": 2000, "weth": AMOUNT, "converted": False}

    class Rpc:
        def call(self, url, method, params):
            calls.append((state["converted"], method, params))
            if method == "eth_chainId":
                return "0xa" if url == "op" else hex(8453)
            if method == "eth_getBalance":
                return hex(state["native"])
            if method == "eth_call":
                return "0x" + f"{state['weth']:064x}"
            if method == "eth_estimateGas":
                return hex(10)
            if method == "eth_getTransactionReceipt":
                return receipts[params[0]]
            if method == "eth_getTransactionByHash":
                return receipts[params[0]]["transaction"]
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

    def routes(request):
        payload = json.loads(
            _quoted_route(
                request,
                route_id="native-bridge-" + request.from_amount,
                output_amount=260000,
                gas_amount=1000,
            ).evidence.canonical_payload_json
        )
        payload["steps"][0]["action"]["fromAddress"] = WALLET
        payload["priceTimestamp"] = datetime.fromtimestamp((now - 100) / 1000, UTC).isoformat()
        for cost in payload.get("gasCosts", []):
            cost["timestamp"] = payload["priceTimestamp"]
        return [_route_from_dict(payload, quote_timestamp=payload["priceTimestamp"])]

    def step_tx(raw):
        from evm_inventory.weth import WETH_WITHDRAW_TOOL, withdraw_transaction_fields

        if raw["tool"] == WETH_WITHDRAW_TOOL:
            chain, contract, data = withdraw_transaction_fields(raw)
            return TransactionRequest(chain, contract, data, 0, 0, 0)
        return TransactionRequest(10, DEPOSIT, "0x1234", int(raw["action"]["fromAmount"]), 0, 0)

    jumper = SimpleNamespace(
        routes=routes,
        step_transaction=step_tx,
        token_price=lambda asset: LifiPriceEvidence(
            asset, "2500", datetime.fromtimestamp((now - 100) / 1000, UTC).isoformat()
        ),
        transaction_status=lambda **kw: dict(
            sending={"txHash": kw["tx_hash"]},
            receiving={"txHash": "0x" + "f" * 64, "amount": "260000"},
            fromAddress=WALLET,
            toAddress=DEPOSIT,
            status="DONE",
            substatus="COMPLETED",
        ),
    )
    bitget = SimpleNamespace(
        revalidate_deposit_target=lambda *a, **kw: target,
        wait_for_deposit_seen=lambda **kw: "success",
    )
    with Journal(tmp_path / "fake.sqlite") as journal:
        halted(journal, original)
        from evm_inventory.canonical_source_bootstrap import migrate_source_bootstraps

        assert (
            migrate_source_bootstraps(
                [original], journal=journal, wallet=wallet, balance=lambda _: AMOUNT
            )
            == 1
        )

        def execute(*, entry, prepared_request, journal, position_id, **kw):
            key = entry["_journal_step_key"]
            sends.append(key)
            tx_hash = "0x" + str(len(sends)) * 64
            journal.record_step_intent(
                position_id=position_id,
                step_key=key,
                nonce=len(sends),
                calldata_digest="a" * 64,
                signed_payload_digest="b" * 64,
                tx_hash=tx_hash,
            )
            log = []
            if key.startswith("swap_to_native:"):
                state.update(converted=True, weth=0, native=2000 + AMOUNT - 500)
                log = [
                    dict(
                        address=WETH_CONTRACTS[10],
                        topics=[
                            "0x" + keccak(text="Withdrawal(address,uint256)").hex(),
                            "0x" + WALLET[2:].rjust(64, "0"),
                        ],
                        data="0x" + f"{AMOUNT:064x}",
                    )
                ]
            receipts[tx_hash] = dict(
                transactionHash=tx_hash,
                status="0x1",
                gasUsed=hex(5),
                effectiveGasPrice=hex(100),
                logs=log,
                transaction=dict(
                    hash=tx_hash,
                    to=prepared_request.to,
                    input=prepared_request.data,
                    value=hex(prepared_request.value),
                    **{"from": WALLET},
                ),
            )
            return tx_hash

        monkeypatch.setattr("evm_inventory.route_execution._execute_entry", execute)
        estimator = PlannerGasEstimator(rpc, {10: "op", 8453: "base"}, jumper)
        kw = dict(
            wallet=wallet,
            rpc=rpc,
            rpc_urls={10: "op", 8453: "base"},
            jumper=jumper,
            bitget=bitget,
            now_ms=lambda: now,
        )
        result = execute_group_entries(
            [original],
            journal=journal,
            max_route_loss_pct=Decimal("15"),
            preflight=lambda e: _preflight_live_entry(e, gas_estimator=estimator, **kw),
            submit_step=lambda e, s: _submit_live_step(e, s, journal=journal, **kw),
            requote_after_swap=lambda e, a: _requote_bridge_after_swap(
                e,
                a,
                wallet=wallet,
                jumper=jumper,
                bitget=bitget,
                gas_estimator=estimator,
                now_ms=lambda: now,
            ),
        )
        assert result["completed"] == 1
        p = journal.connection.execute("SELECT * FROM route_positions").fetchone()
        assert journal.checkpoint(p["id"], "swap_result")["actual_balance_raw"] == str(AMOUNT - 500)
        assert Decimal(p["accounted_loss_usd"]) == Decimal(
            journal.group(p["group_id"])["realized_loss_usd"]
        )
        assert Decimal(p["accounted_loss_usd"]) > 0
    assert len(sends) == 2 and sends[0].startswith("swap_to_native:")
    assert all(
        len(params) == 2
        for converted, method, params in calls
        if converted and method == "eth_estimateGas"
    )
    assert any(
        len(params) == 3
        for converted, method, params in calls
        if not converted and method == "eth_estimateGas"
    )


@pytest.mark.parametrize(
    "method",
    [
        "eth_sendRawTransaction",
        "eth_sendTransaction",
        "eth_sign",
        "personal_sign",
        "wallet_sendCalls",
    ],
)
def test_forecast_rpc_cannot_be_used_to_submit_or_sign(method):
    from evm_inventory.canonical_source_bootstrap import ForecastRpc

    calls = []
    rpc = SimpleNamespace(call=lambda u, m, p: calls.append(m) or "0xa")
    proxy = ForecastRpc(rpc, url="rpc", chain_id=10, owner=WALLET, projected_balance=1000)
    with pytest.raises(ValueError, match="forecast_read_only"):
        proxy.call("rpc", method, [])
    assert calls == ["eth_chainId"]


def test_source_reserve_refuses_missing_or_changed_future_proof():
    from evm_inventory.canonical_source_bootstrap import source_withdrawal_reserve

    e, request, wallet = bootstrap_entry()
    e["canonical_source_bootstrap"]["forecast_proof"] = {"forged": True}
    with pytest.raises(ValueError, match="canonical_source_bootstrap"):
        source_withdrawal_reserve(e, request, wallet)


def test_migration_rechecks_any_intent_created_during_balance_verification(tmp_path):
    from evm_inventory.canonical_source_bootstrap import migrate_source_bootstraps

    original = entry()
    with Journal(tmp_path / "fake.sqlite") as journal:
        gid, pid = halted(journal, original)

        def balance(_):
            journal.record_step_intent(
                position_id=pid,
                step_key="approval:race",
                nonce=0,
                calldata_digest="a" * 64,
                signed_payload_digest="b" * 64,
                tx_hash=None,
            )
            return AMOUNT

        assert (
            migrate_source_bootstraps(
                [original],
                journal=journal,
                wallet=SimpleNamespace(public_address=WALLET, bitget_deposit_address=DEPOSIT),
                balance=balance,
            )
            == 0
        )
        assert journal.group(gid)["state"] == "manual_review_group_halted"
        assert not original.get("canonical_source_bootstrap")


def test_forecast_rejects_wrong_provider_chain_before_estimation():
    from evm_inventory.canonical_source_bootstrap import ForecastRpc

    calls = []
    rpc = SimpleNamespace(call=lambda u, m, p: calls.append(m) or "0x2105")
    with pytest.raises(ValueError, match="forecast_chain_mismatch"):
        ForecastRpc(rpc, url="rpc", chain_id=10, owner=WALLET, projected_balance=1000)
    assert calls == ["eth_chainId"]


def test_unsupported_forecast_rpc_returns_no_inferred_gas():
    from evm_inventory.canonical_source_bootstrap import ForecastRpc
    from evm_inventory.fee_planner import FeePlanner

    request = TransactionRequest(10, DEPOSIT, "0x1234", 100, 0, 0)

    def call(u, m, p):
        if m == "eth_chainId":
            return "0xa"
        raise ValueError("state override unsupported")

    proxy = ForecastRpc(
        SimpleNamespace(call=call), url="rpc", chain_id=10, owner=WALLET, projected_balance=1000
    )
    proxy.allow(request)
    with pytest.raises(ValueError, match="estimate_gas_rpc_error"):
        FeePlanner(proxy).plan("rpc", request, sender=WALLET)


def test_continuation_signature_cannot_spend_protected_source_native_after_balance_reduction(
    monkeypatch,
):
    from evm_inventory.route_execution import _execute_entry

    e, _, wallet = bootstrap_entry()
    e.update(
        asset_id="native",
        _actual_asset_id="native",
        _actual_balance_raw="900",
        _journal_step_key="bridge:future:0",
        quoted_at=1800000000000,
    )
    e["canonical_source_bootstrap"]["protected_native_raw"] = "200"
    request = TransactionRequest(10, DEPOSIT, "0x1234", 250, 10, 10, max_total_fee_cap_wei=100)
    monkeypatch.setattr("evm_inventory.route_execution.FeePlanner.plan", lambda *a, **k: request)
    monkeypatch.setattr("evm_inventory.route_execution._native_balance", lambda *a, **k: 600)
    monkeypatch.setattr(
        "evm_inventory.route_execution.sign_transaction",
        lambda *a, **k: pytest.fail("spent another source gas reserve"),
    )
    with pytest.raises(ValueError, match="insufficient native balance"):
        _execute_entry(
            entry=e,
            prepared_request=request,
            wallet=wallet,
            rpc=object(),
            jumper=object(),
            rpc_urls={10: "rpc"},
            now_ms=lambda: 1800000000000,
        )


def test_confirmed_source_intent_does_not_freeze_unsigned_future_preflight(tmp_path):
    from decimal import Decimal

    from test_route_execution import ASSET_A, _entry

    from evm_inventory.route_execution import execute_group_entries
    from evm_inventory.stages import StageDeadlineExceeded

    swap = dict(
        kind="swap_to_native", route=dict(id="source", evidence={"payload_sha256": "a" * 64})
    )
    bridge = dict(kind="bridge", route=dict(id="old", evidence={"payload_sha256": "b" * 64}))
    replacement = dict(
        kind="bridge",
        route=dict(id="new", from_amount="70", evidence={"payload_sha256": "c" * 64}),
        valuation={"loss_usd": "2"},
    )
    e = {
        **_entry(ASSET_A, status="route_ready", steps=[swap, bridge]),
        "automatic_requote": True,
        "canonical_source_bootstrap": {},
    }
    # Nonempty marker activates the source-specific continuation policy.
    e["canonical_source_bootstrap"] = {"chain_id": 10}
    preflights, sends = [], []
    with Journal(tmp_path / "fake.sqlite") as journal:

        def preflight(fresh):
            preflights.append(fresh.get("_actual_balance_raw"))
            return {**fresh, "valuation": {"loss_usd": "3"}}

        def submit(fresh, step):
            sends.append(step["kind"])
            if step["kind"] == "swap_to_native":
                journal.record_step_intent(
                    position_id=fresh["_journal_position_id"],
                    step_key="swap_to_native:source:0",
                    nonce=1,
                    calldata_digest="a" * 64,
                    signed_payload_digest="b" * 64,
                    tx_hash="0x" + "a" * 64,
                )
                return dict(
                    state="confirmed",
                    actual_asset_id="native",
                    actual_balance_raw="100",
                    realized_loss_usd="1",
                    protected_native_raw="0",
                )
            if len(sends) == 2:
                raise StageDeadlineExceeded("future_fee_wait")
            return dict(state="completed", realized_loss_usd="3")

        for _ in range(2):
            result = execute_group_entries(
                [e],
                journal=journal,
                max_route_loss_pct=Decimal("15"),
                preflight=preflight,
                submit_step=submit,
                requote_after_swap=lambda *a: replacement,
            )
        assert result["completed"] == 1
        p = journal.connection.execute("SELECT * FROM route_positions").fetchone()
        assert p["accounted_loss_usd"] == "3"
        assert journal.group(p["group_id"])["realized_loss_usd"] == "3"
    assert sends == ["swap_to_native", "bridge", "bridge"]
    assert preflights == [None, "100", "100"]
