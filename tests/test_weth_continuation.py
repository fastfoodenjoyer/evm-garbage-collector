from copy import deepcopy
from dataclasses import replace
from types import SimpleNamespace

import pytest

from evm_inventory.lifi import LifiRouteRequest, TransactionRequest
from evm_inventory.weth import NATIVE, WETH_CONTRACTS, unwrap_route_payload
from evm_inventory.weth_continuation import withdrawal_reserve

WALLET = "0x" + "1" * 40
DEPOSIT = "0x" + "2" * 40
AMOUNT = 367154271454740


def fixture():
    payload = unwrap_route_payload(LifiRouteRequest(
        8453, 8453, WETH_CONTRACTS[8453], NATIVE, str(AMOUNT), WALLET, WALLET,
    ), "2500", "2500")
    target = dict(coin="ETH", chain_id=8453, chain="BASE", asset_id="native",
                  decimals=18, minimum_raw="197500000000000")
    entry = dict(automatic_requote=True, local_unwrap=True, chain_id=8453,
                 asset_id=WETH_CONTRACTS[8453], decimals=18, raw_balance=str(AMOUNT),
                 target=target, _check_group_loss=lambda _loss: None,
                 steps=[dict(kind="unwrap_to_native", _live_route=SimpleNamespace(
                     raw_steps=tuple(payload["steps"])))],
                 reservations=dict(final_deposit_native_gas_cap_raw="100000000000"),
                 canonical_continuation=dict(wallet=WALLET, recipient=DEPOSIT,
                     amount_raw=str(AMOUNT), chain_id=8453, target=target))
    request = TransactionRequest(8453, WETH_CONTRACTS[8453],
        "0x2e1a7d4d" + f"{AMOUNT:064x}", 0, 43333, 1000000,
        additional_fee_wei=275460290647, max_total_fee_cap_wei=318793290647)
    wallet = SimpleNamespace(public_address=WALLET, bitget_deposit_address=DEPOSIT)
    return entry, request, wallet


def test_exact_capped_canonical_withdrawal_uses_twenty_percent_padding():
    entry, request, wallet = fixture()
    assert withdrawal_reserve(entry, request, wallet) == 382551948777


def test_ordinary_routes_keep_existing_reserve_policy():
    entry, request, wallet = fixture()
    entry.pop("canonical_continuation")
    assert withdrawal_reserve(entry, request, wallet) is None


@pytest.mark.parametrize("canonical", (True, False))
def test_executor_uses_narrow_reserve_only_for_proved_withdraw(monkeypatch, canonical):
    from evm_inventory.route_execution import _execute_entry
    entry, request, wallet = fixture()
    entry.update(status="route_ready", quoted_at=1800000000000,
                 route={"step": entry["steps"][0]["_live_route"].raw_steps[0]})
    wallet.private_key = "fake"
    if not canonical:
        entry.pop("canonical_continuation")
    signed = []
    for name, replacement in {
        "FeePlanner.plan": lambda *a, **k: request,
        "_native_balance": lambda *a, **k: 429312408000,
        "pending_nonce": lambda *a, **k: 1,
        "sign_transaction": lambda *a, **k: signed.append(request) or "0x01",
        "broadcast_signed_transaction": lambda *a, **k: "0x" + "a" * 64,
        "wait_for_receipt": lambda *a, **k: {},
    }.items():
        monkeypatch.setattr("evm_inventory.route_execution." + name, replacement)
    kwargs = dict(entry=entry, prepared_request=request, wallet=wallet, rpc=object(),
                  jumper=object(), rpc_urls={8453: "rpc"}, now_ms=lambda: 1800000000000)
    if canonical:
        assert _execute_entry(**kwargs) == "0x" + "a" * 64
        assert signed == [request]
    else:
        with pytest.raises(ValueError, match="insufficient native balance"):
            _execute_entry(**kwargs)
        assert signed == []


def test_canonical_intent_recovers_crash_before_rpc_send_with_identical_payload(
    tmp_path, monkeypatch,
):
    from hashlib import sha256

    from evm_inventory.executor import signed_transaction_hash
    from evm_inventory.journal import Journal
    from evm_inventory.route_execution import _execute_entry
    entry, request, wallet = fixture()
    wallet.private_key = "fake"
    entry.update(status="route_ready", _journal_step_key="unwrap_to_native:canonical:0")
    raw = "0x01"
    tx_hash = signed_transaction_hash(raw)
    sent = []
    signed = []

    class Rpc:
        def call(self, url, method, params):
            if method in {"eth_getTransactionReceipt", "eth_getTransactionByHash"}:
                return None
            if method == "eth_getTransactionCount":
                return "0x0"
            if method == "eth_sendRawTransaction":
                sent.append(params[0])
                return tx_hash
            raise AssertionError(method)

    monkeypatch.setattr("evm_inventory.route_execution.sign_transaction",
        lambda req, **kwargs: signed.append((req, kwargs["nonce"])) or raw)
    monkeypatch.setattr("evm_inventory.route_execution.wait_for_receipt", lambda *a, **k: {})
    with Journal(tmp_path / "fixture.sqlite") as journal:
        position = journal.get_or_create_position(position_key="p", wallet=WALLET)
        key = entry["_journal_step_key"]
        journal.save_checkpoint(position["id"], f"canonical_signing:{key}",
            dict(entry=entry, request=request))
        journal.record_step_intent(position_id=position["id"], step_key=key, nonce=0,
            calldata_digest=sha256(request.data.encode()).hexdigest(),
            signed_payload_digest=sha256(raw.encode()).hexdigest(), tx_hash=tx_hash)
        assert _execute_entry(entry=entry, wallet=wallet, rpc=Rpc(), jumper=object(),
            rpc_urls={8453: "rpc"}, journal=journal, position_id=position["id"],
            prepared_request=replace(request, gas_price_wei=1)) == tx_hash
        assert len(journal.connection.execute("SELECT * FROM route_steps").fetchall()) == 1
    assert sent == [raw] and signed == [(request, 0)]


@pytest.mark.parametrize("field,value", [
    ("to", DEPOSIT), ("data", "0xa9059cbb"), ("chain_id", 10),
    ("value", 1), ("max_total_fee_cap_wei", None),
    ("max_total_fee_cap_wei", 1),
])
def test_short_reserve_rejects_changed_transaction(field, value):
    entry, request, wallet = fixture()
    with pytest.raises(ValueError, match="canonical_continuation"):
        withdrawal_reserve(entry, replace(request, **{field: value}), wallet)


@pytest.mark.parametrize("change", ["wallet", "recipient", "amount", "target", "approval",
                                    "budget_callback", "deposit_fee", "minimum"])
def test_short_reserve_rejects_missing_or_foreign_proofs(change):
    entry, request, wallet = fixture()
    entry = deepcopy(entry)
    if change in {"wallet", "recipient"}:
        entry["canonical_continuation"][change] = "0x" + "3" * 40
    elif change == "amount":
        entry["canonical_continuation"]["amount_raw"] = "1"
    elif change == "target":
        entry["target"]["coin"] = "USDC"
    elif change == "approval":
        entry["steps"][0]["_live_route"].raw_steps[0]["estimate"]["approvalAddress"] = DEPOSIT
    elif change == "budget_callback":
        entry.pop("_check_group_loss")
    elif change == "deposit_fee":
        entry["reservations"].clear()
    elif change == "minimum":
        entry["target"]["minimum_raw"] = str(AMOUNT)
    with pytest.raises(ValueError, match="canonical_continuation"):
        withdrawal_reserve(entry, request, wallet)


def test_intermediate_canonical_fallback_prepares_native_target_without_route_api(monkeypatch):
    from datetime import UTC, datetime

    from evm_inventory.bitget_catalog import BitgetDepositTarget
    from evm_inventory.lifi import LifiPriceEvidence
    from evm_inventory.models import AssetIdentity
    from evm_inventory.route_execution import _requote_from_intermediate

    target = BitgetDepositTarget("ETH", 8453, "native", 197500000000000, "BASE", 18)
    old = dict(coin="USDC", chain_id=8453, asset_id="0x" + "3" * 40,
               minimum_raw="1", chain="BASE", decimals=6)
    entry = dict(target=old, automatic_requote=True, _journal_position_id=58,
                 _actual_asset_id="native", _actual_balance_raw="123", group={"key": "g"})
    wallet = SimpleNamespace(public_address=WALLET, bitget_deposit_address=DEPOSIT)
    bitget = SimpleNamespace(native_deposit_target=lambda chain: target,
        revalidate_deposit_target=lambda data, **kw: BitgetDepositTarget(
            data["coin"], data["chain_id"], data["asset_id"], int(data["minimum_raw"]),
            data["chain"], data["decimals"]))
    client = SimpleNamespace(token_price=lambda asset: LifiPriceEvidence(
        asset, "2500", datetime.now(UTC).isoformat()))
    instant = int(datetime.now(UTC).timestamp() * 1000) + 100
    prepared = []

    def preflight(dynamic, **kwargs):
        prepared.append(dynamic)
        return dynamic

    monkeypatch.setattr("evm_inventory.route_execution._preflight_live_entry", preflight)
    result = _requote_from_intermediate(entry,
        AssetIdentity(8453, WETH_CONTRACTS[8453], 18), AMOUNT, wallet=wallet,
        rpc=object(), rpc_urls={}, jumper=client, bitget=bitget, gas_estimator=object(),
        now_ms=lambda: instant)
    assert result["target"]["coin"] == "ETH"
    assert result["group"] == entry["group"]
    assert result["canonical_continuation"]["previous_target"] == old
    assert result["steps"][0]["kind"] == "unwrap_to_native"
    assert "_actual_balance_raw" not in result and "_actual_asset_id" not in result
    assert len(prepared) == 1


@pytest.mark.parametrize("age", (-1, 301))
def test_synthetic_unwrap_rejects_future_or_stale_source_price(age):
    from datetime import UTC, datetime, timedelta

    from evm_inventory.lifi import LifiPriceEvidence
    from evm_inventory.models import AssetIdentity
    from evm_inventory.route_execution import _canonical_unwrap_route
    now = datetime.now(UTC)
    asset = AssetIdentity(8453, WETH_CONTRACTS[8453], 18)
    client = SimpleNamespace(token_price=lambda token: LifiPriceEvidence(token, "2500",
        (now - timedelta(seconds=age if token == asset else 0)).isoformat()))
    with pytest.raises(ValueError, match="stale_price"):
        _canonical_unwrap_route(client, asset, AMOUNT,
            SimpleNamespace(public_address=WALLET), lambda: now)


def test_synthetic_unwrap_reads_clock_after_both_price_responses():
    from datetime import UTC, datetime, timedelta

    from evm_inventory.lifi import LifiPriceEvidence
    from evm_inventory.models import AssetIdentity
    from evm_inventory.route_execution import _canonical_unwrap_route
    instant = datetime.now(UTC)
    responses = []

    def price(asset):
        responses.append(asset)
        return LifiPriceEvidence(asset, "2500",
            (instant + timedelta(seconds=len(responses))).isoformat())

    route = _canonical_unwrap_route(SimpleNamespace(token_price=price),
        AssetIdentity(8453, WETH_CONTRACTS[8453], 18), AMOUNT,
        SimpleNamespace(public_address=WALLET),
        lambda: instant + timedelta(seconds=len(responses)))
    assert len(responses) == 2
    assert route.evidence.quote_timestamp == (instant + timedelta(seconds=1)).isoformat()


def seed_delivered(journal, position_key="p"):
    from evm_inventory.lifi import _route_from_dict
    from evm_inventory.live_plan import _route_data
    group = journal.get_or_create_group(group_key="g", wallet=WALLET,
        source_chain_id=130, loss_budget_pct="15", source_usd="1")
    position = journal.get_or_create_position(position_key=position_key, wallet=WALLET,
        group_id=group["id"], source_asset_id="native", source_amount_raw=str(AMOUNT))
    source = dict(chainId=130, address=NATIVE, decimals=18, priceUSD="2500")
    weth = dict(chainId=8453, address=WETH_CONTRACTS[8453], decimals=18, priceUSD="2500")
    usdc = dict(chainId=8453, address="0x" + "3" * 40, decimals=6, priceUSD="1")
    steps = []
    for index, (src, dest, output, recipient) in enumerate((
        (source, weth, AMOUNT, WALLET), (weth, usdc, 1000000, DEPOSIT),
    )):
        steps.append(dict(id=f"s{index}", type="cross" if index == 0 else "swap", tool="test",
            action=dict(fromChainId=src["chainId"], toChainId=dest["chainId"],
                fromToken=src, toToken=dest, fromAmount=str(AMOUNT),
                fromAddress=WALLET, toAddress=recipient),
            estimate=dict(fromAmount=str(AMOUNT), toAmount=str(output),
                toAmountMin=str(output), gasCosts=[], feeCosts=[])))
    route = _route_from_dict(dict(id="bridge-original", fromAmount=str(AMOUNT),
        toAmount="1000000", toAmountMin="1000000", steps=steps),
        quote_timestamp="2026-10-12T12:00:00+00:00")
    target = dict(coin="USDC", chain_id=8453, chain="BASE", asset_id=usdc["address"],
                  decimals=6, minimum_raw="1")
    group_data = dict(key="g", source_usd="1", max_route_loss_pct="15")
    prepared = dict(automatic_requote=True, wallet=WALLET, deposit_address=DEPOSIT,
        chain_id=130, asset_id="native", target=target, group=group_data,
        raw_balance=str(AMOUNT), decimals=18, status="route_ready",
        valuation={"loss_usd": "0.01"},
        steps=[dict(kind="bridge", route=_route_data(route), _live_route=route)])
    prepared["steps"][0]["_prepared_requests"] = (
        TransactionRequest(130, DEPOSIT, "0x", AMOUNT, 100, 1),
        TransactionRequest(8453, DEPOSIT, "0x", 0, 100, 1),
    )
    child = dict(automatic_requote=True, wallet=WALLET, deposit_address=DEPOSIT,
        chain_id=8453, asset_id=WETH_CONTRACTS[8453], decimals=18, raw_balance=str(AMOUNT),
        _actual_asset_id=WETH_CONTRACTS[8453], _actual_balance_raw=str(AMOUNT),
        group=group_data, target=target, _realized_loss_before_current_usd="0.01", steps=[])
    key = "bridge:bridge-original:0"
    saved = journal.record_step_intent(position_id=position["id"], step_key=key, nonce=1,
        calldata_digest="a" * 64, signed_payload_digest="b" * 64)
    tx = "0x" + "4" * 64
    journal.record_broadcast_attempt(saved["id"], tx)
    journal.record_step_state(saved["id"], "confirmed")
    journal.record_step_state(saved["id"], "credited")
    journal.save_checkpoint(position["id"], "prepared", prepared)
    journal.save_checkpoint(position["id"], "route:bridge:bridge-original",
                            dict(continuation=child))
    journal.save_checkpoint(position["id"], f"bridge_delivery:{key}", dict(
        chain_id=8453, asset_id=WETH_CONTRACTS[8453], decimals=18, amount_raw=str(AMOUNT),
        recipient=WALLET, source_hash=tx, tx_hash="0x" + "5" * 64))
    reason = "insufficient native balance for gas reserve: fixture"
    journal.record_position_state(position["id"], "manual_review", reason=reason,
        actual_asset_id=WETH_CONTRACTS[8453], actual_balance_raw=AMOUNT)
    journal.record_group_state(group["id"], "manual_review_group_halted", reason=reason)
    return position["id"], group["id"]


def test_legacy_recovery_preserves_credited_source_and_budget(tmp_path):
    from evm_inventory.journal import Journal
    with Journal(tmp_path / "fixture.sqlite") as journal:
        position, group = seed_delivered(journal)
        before = journal.group(group)
        assert journal.reactivate_unsigned_weth_continuation(group, {"p"})
        after = journal.group(group)
        assert after["state"] == "active"
        assert journal.position(position)["state"] == "bridge_submitted"
        for field in ("source_usd", "loss_budget_pct", "realized_loss_usd"):
            assert after[field] == before[field]
        assert journal.latest_step(position_id=position,
            step_key="bridge:bridge-original:0")["state"] == "credited"
        assert journal.checkpoint(position, "canonical_recovery")["reason"] == before["reason"]


@pytest.mark.parametrize("change", ["source_hash", "recipient", "asset_id", "chain_id",
    "amount_raw", "child_budget", "child_recipient", "child_intent", "unknown_reason",
    "members", "invalid_accrued", "over_budget"])
def test_legacy_recovery_rejects_missing_proofs(tmp_path, change):
    from evm_inventory.journal import Journal
    with Journal(tmp_path / "fixture.sqlite") as journal:
        position, group = seed_delivered(journal)
        delivery_key = "bridge_delivery:bridge:bridge-original:0"
        if change in {"source_hash", "recipient", "asset_id", "chain_id", "amount_raw"}:
            delivery = journal.checkpoint(position, delivery_key)
            delivery[change] = 10 if change == "chain_id" else "1"
            journal.save_checkpoint(position, delivery_key, delivery)
        elif change in {"child_budget", "child_recipient", "invalid_accrued", "over_budget"}:
            progress = journal.checkpoint(position, "route:bridge:bridge-original")
            if change == "child_budget":
                progress["continuation"]["group"]["max_route_loss_pct"] = "16"
            elif change == "child_recipient":
                progress["continuation"]["deposit_address"] = WALLET
            else:
                progress["continuation"]["_realized_loss_before_current_usd"] = (
                    "bad" if change == "invalid_accrued" else "0.151")
            journal.save_checkpoint(position, "route:bridge:bridge-original", progress)
        elif change == "child_intent":
            journal.record_step_intent(position_id=position, step_key="approval:child", nonce=2,
                calldata_digest="c" * 64, signed_payload_digest="d" * 64)
        elif change == "unknown_reason":
            journal.record_position_state(position, "manual_review", reason="unknown")
        keys = {"other"} if change == "members" else {"p"}
        assert not journal.reactivate_unsigned_weth_continuation(group, keys)
        assert journal.position(position)["state"] == "manual_review"
        assert journal.group(group)["state"] == "manual_review_group_halted"


@pytest.mark.parametrize("completed", (True, False))
def test_recovery_requires_exact_completed_siblings(tmp_path, completed):
    from evm_inventory.journal import Journal
    with Journal(tmp_path / "fixture.sqlite") as journal:
        position, group = seed_delivered(journal)
        sibling = journal.get_or_create_position(position_key="sibling", wallet=WALLET,
            group_id=group, source_asset_id="0x" + "9" * 40, source_amount_raw="1")
        if completed:
            journal.record_position_state(sibling["id"], "completed")
        assert journal.reactivate_unsigned_weth_continuation(group, {"p", "sibling"}) is completed
        assert journal.position(position)["state"] == (
            "bridge_submitted" if completed else "manual_review")


def test_recovery_includes_already_paid_sibling_cost_in_original_budget(tmp_path):
    from evm_inventory.journal import Journal
    with Journal(tmp_path / "fixture.sqlite") as journal:
        _, group = seed_delivered(journal)
        sibling = journal.get_or_create_position(position_key="sibling", wallet=WALLET,
            group_id=group, source_asset_id="0x" + "9" * 40, source_amount_raw="1")
        # Fixture setup before the halt: emulate a paid, completed sibling.
        journal.connection.execute("UPDATE route_groups SET realized_loss_usd='0.145' WHERE id=?",
                                   (group,))
        journal.connection.commit()
        journal.record_position_state(sibling["id"], "completed")
        assert not journal.reactivate_unsigned_weth_continuation(group, {"p", "sibling"})


def test_group_instant_credit_accounting_survives_crash_before_completion(tmp_path, monkeypatch):
    from decimal import Decimal

    from evm_inventory.journal import Journal
    from evm_inventory.route_execution import execute_group_entries
    from evm_inventory.stages import StageDeadlineExceeded
    with Journal(tmp_path / "fixture.sqlite") as journal:
        position, group = seed_delivered(journal, "g:native")
        assert journal.reactivate_unsigned_weth_continuation(group, {"g:native"})
        entry = journal.checkpoint(position, "prepared")
        journal.save_checkpoint(position, "canonical_prepared", {"valuation": {"loss_usd": "0.02"}})
        original = journal.record_position_state
        crash = [True]

        def record(pid, state, **kwargs):
            if state == "completed" and crash[0]:
                crash[0] = False
                raise StageDeadlineExceeded("crash_after_accounting")
            return original(pid, state, **kwargs)

        monkeypatch.setattr(journal, "record_position_state", record)
        def submit(dynamic, step):
            dynamic["_check_group_loss"](Decimal("0.02"))
            return {"state": "completed", "realized_loss_usd": "0.02"}

        kwargs = dict(journal=journal, max_route_loss_pct=Decimal("15"),
                      preflight=lambda e: e, submit_step=submit)
        with pytest.raises(StageDeadlineExceeded, match="crash_after_accounting"):
            execute_group_entries([entry], **kwargs)
        assert journal.group(group)["realized_loss_usd"] == "0.02"
        result = execute_group_entries([entry], **kwargs)
        assert result["completed"] == 1
        assert journal.group(group)["realized_loss_usd"] == "0.02"
        assert journal.position(position)["accounted_loss_usd"] == "0.02"


def test_nested_unwrap_budget_rejection_retains_paid_loss(tmp_path):
    from decimal import Decimal

    from evm_inventory.journal import Journal
    from evm_inventory.route_execution import execute_group_entries
    with Journal(tmp_path / "fixture.sqlite") as journal:
        position, group = seed_delivered(journal, "g:native")
        assert journal.reactivate_unsigned_weth_continuation(group, {"g:native"})
        entry = journal.checkpoint(position, "prepared")
        canonical, _, _ = fixture()
        canonical["steps"][0]["route"] = {"id": "unwrap"}
        journal.save_checkpoint(position, "canonical_prepared", canonical)
        journal.save_checkpoint(position, "route:unwrap_to_native:unwrap", dict(
            unwrap_result=dict(realized_loss_usd="0.02")))

        def submit(dynamic, step):
            dynamic["_check_group_loss"](Decimal("0.2"))
            pytest.fail("over-budget deposit must not be sent")

        result = execute_group_entries([entry], journal=journal,
            max_route_loss_pct=Decimal("15"), preflight=lambda e: e, submit_step=submit)
        assert result["manual_review_group_threshold_exceeded"] == 1
        assert journal.group(group)["realized_loss_usd"] == "0.02"
        assert journal.position(position)["accounted_loss_usd"] == "0.02"


@pytest.mark.parametrize("later_segment", (False, True))
def test_native_observer_uses_exact_segment_of_saved_bridge_replacement(tmp_path, later_segment):
    from evm_inventory.journal import Journal
    from evm_inventory.lifi import _route_from_dict
    from evm_inventory.live_plan import _route_data
    from evm_inventory.route_execution import _saved_native_settlement_entry
    with Journal(tmp_path / "fixture.sqlite") as journal:
        position, _ = seed_delivered(journal)
        parent = journal.checkpoint(position, "prepared")
        route = parent["steps"][0]["_live_route"]
        source_key = "bridge:bridge-original:0"
        delivery = journal.checkpoint(position, f"bridge_delivery:{source_key}")
        if later_segment:
            payload = dict(id=route.route_id, fromAmount=str(AMOUNT), toAmount="1000000",
                           toAmountMin="1000000", steps=deepcopy(list(route.raw_steps)))
            prefix = deepcopy(payload["steps"][0])
            wrapped = dict(chainId=130, address="0x" + "8" * 40, decimals=18, priceUSD="2500")
            prefix["type"] = "swap"
            prefix["action"].update(toChainId=130, toToken=wrapped)
            payload["steps"][0]["action"]["fromToken"] = wrapped
            payload["steps"].insert(0, prefix)
            route = _route_from_dict(payload, quote_timestamp=route.evidence.quote_timestamp)
            parent["steps"][0].update(route=_route_data(route), _live_route=route)
            source_key = "bridge:bridge-original:1"
            saved = journal.record_step_intent(position_id=position, step_key=source_key, nonce=2,
                calldata_digest="e" * 64, signed_payload_digest="f" * 64)
            delivery["source_hash"] = "0x" + "8" * 64
            journal.record_broadcast_attempt(saved["id"], delivery["source_hash"])
            journal.record_step_state(saved["id"], "confirmed")
            journal.record_step_state(saved["id"], "credited")
            journal.save_checkpoint(position, f"bridge_delivery:{source_key}", delivery)
        child, _, wallet = fixture()
        child["steps"][0]["route"] = {"id": "unwrap"}
        marker = child["canonical_continuation"]
        marker.update(previous_target=parent["target"], source_step_key=source_key)
        final = {**child, "chain_id": 8453, "asset_id": "native", "decimals": 18,
                 "steps": [{"kind": "direct_deposit"}]}
        journal.save_checkpoint(position, "route:bridge:bridge-original", {"continuation": child})
        journal.save_checkpoint(position, "route:unwrap_to_native:unwrap", {
            "unwrap_result": {"actual_balance_raw": str(AMOUNT - 100)}, "continuation": final})
        saved = journal.record_step_intent(
            position_id=position, step_key="unwrap_to_native:unwrap:0",
            nonce=3, calldata_digest="e" * 64, signed_payload_digest="f" * 64)
        journal.record_broadcast_attempt(saved["id"], "0x" + "9" * 64)
        journal.save_checkpoint(position, "direct_prepared", final)
        journal.save_checkpoint(position, "bridge_prepared", parent)
        journal.save_checkpoint(position, "prepared",
                                {"steps": [{"kind": "swap_to_native"}]})
        observed = _saved_native_settlement_entry(parent, wallet, journal, position)
        assert observed["target"]["coin"] == "ETH"


@pytest.mark.parametrize("foreign_marker", (False, True))
@pytest.mark.parametrize("crash_after_hash", (False, True))
def test_delivered_bridge_refreshes_unsigned_child_and_recovers_after_unwrap(
    tmp_path, monkeypatch, foreign_marker, crash_after_hash,
):
    from decimal import Decimal

    from evm_inventory.journal import Journal
    from evm_inventory.lifi import _route_from_dict
    from evm_inventory.live_plan import _route_data
    from evm_inventory.route_execution import _observe_route_deposit, _submit_live_step
    from evm_inventory.stages import StageDeadlineExceeded

    native_balance = 429312408000
    submitted = []
    direct_quotes = []
    projections = []
    status = ["pending", "success"]
    wallet = SimpleNamespace(public_address=WALLET, bitget_deposit_address=DEPOSIT)
    canonical, withdraw, _ = fixture()
    route = _route_from_dict(unwrap_route_payload(LifiRouteRequest(8453, 8453,
        WETH_CONTRACTS[8453], NATIVE, str(AMOUNT), WALLET, WALLET), "2500", "2500"),
        quote_timestamp="2026-10-12T12:00:00+00:00")
    canonical.update(wallet=WALLET, deposit_address=DEPOSIT, status="route_ready",
        valuation={"loss_usd": "0.002"},
        _step_candidates={route.route_id: SimpleNamespace(loss_usd=Decimal("0.001"))},
        steps=[dict(kind="unwrap_to_native", route=_route_data(route), _live_route=route,
                    _prepared_requests=(withdraw,))])

    def requote(original, asset, amount):
        if asset.contract_address != "native":
            result = {**canonical, "group": original["group"]}
            result["canonical_continuation"] = {**canonical["canonical_continuation"],
                                               "previous_target": original["target"]}
            return result
        direct_quotes.append(amount)
        if len(direct_quotes) == 1:
            raise StageDeadlineExceeded("pause_after_unwrap")
        return {**original, "status": "direct_deposit", "asset_id": "native",
            "_actual_asset_id": "native", "_actual_balance_raw": str(amount),
            "steps": [dict(kind="direct_deposit")], "valuation": {"loss_usd": "0.001"},
            "_direct_transaction": TransactionRequest(8453, DEPOSIT, "0x", amount - 300, 100, 1)}

    class Rpc:
        def call(self, url, method, params):
            if method == "eth_getBalance":
                return hex(native_balance)
            if method == "eth_call":
                return "0x" + f"{AMOUNT:064x}"
            if method == "eth_getTransactionReceipt":
                return dict(gasUsed="0x64", effectiveGasPrice="0x1")
            raise AssertionError(method)

    def execute(*, entry, journal, position_id, **kwargs):
        nonlocal native_balance
        key = entry.get("_journal_step_key", "direct_deposit")
        known = journal.latest_step(position_id=position_id, step_key=key)
        if known and known["tx_hash"]:
            return known["tx_hash"]
        submitted.append(entry["asset_id"])
        tx = "0x" + ("6" if entry["asset_id"] != "native" else "7") * 64
        saved = journal.record_step_intent(position_id=position_id, step_key=key,
            nonce=len(submitted), calldata_digest="c" * 64, signed_payload_digest="d" * 64)
        journal.record_broadcast_attempt(saved["id"], tx)
        if entry["asset_id"] != "native":
            native_balance += AMOUNT - 100
            if crash_after_hash:
                raise StageDeadlineExceeded("crash_after_hash")
        return tx

    monkeypatch.setattr("evm_inventory.route_execution._execute_entry", execute)
    if crash_after_hash:
        fresh = {**canonical, "valuation": {"loss_usd": "0.005"},
            "_candidate": SimpleNamespace(loss_usd=Decimal("0.005")),
            "_step_candidates": {route.route_id: SimpleNamespace(loss_usd=Decimal("0.004"))},
            "quoted_at": 1800000000000}
        monkeypatch.setattr("evm_inventory.route_execution.current_deadline", lambda: object())
        monkeypatch.setattr("evm_inventory.route_execution._refresh_route_step",
            lambda *a, **k: (fresh["_candidate"], fresh["steps"][0]))
        monkeypatch.setattr("evm_inventory.route_execution._preflight_live_entry",
            lambda dynamic, **k: {**dynamic, **{key: fresh[key] for key in (
                "valuation", "_candidate", "_step_candidates", "steps", "quoted_at")}})
    bitget = SimpleNamespace(revalidate_deposit_target=lambda *a, **k: __import__(
        "evm_inventory.bitget_catalog", fromlist=["BitgetDepositTarget"]).BitgetDepositTarget(
            "ETH", 8453, "native", 197500000000000, "BASE", 18),
        wait_for_deposit_seen=lambda **kwargs: status.pop(0))
    with Journal(tmp_path / "fixture.sqlite") as journal:
        position, group = seed_delivered(journal)
        journal.reactivate_unsigned_weth_continuation(group, {"p"})
        entry = journal.checkpoint(position, "prepared")
        entry.update(_journal_position_id=position, _requote_after_intermediate=requote,
                     _check_group_loss=projections.append)
        kwargs = dict(wallet=wallet, rpc=Rpc(), rpc_urls={8453: "rpc"}, jumper=object(),
                      bitget=bitget, journal=journal, now_ms=lambda: 1800000000000)
        if crash_after_hash:
            with pytest.raises(StageDeadlineExceeded, match="crash_after_hash"):
                _submit_live_step(entry, entry["steps"][0], **kwargs)
            saved = journal.checkpoint(position, "route:bridge:bridge-original")["continuation"]
            assert saved["_step_candidates"][route.route_id].loss_usd == Decimal("0.004")
            assert saved["valuation"]["loss_usd"] == "0.005"
        with pytest.raises(StageDeadlineExceeded, match="pause_after_unwrap"):
            _submit_live_step(entry, entry["steps"][0], **kwargs)
        assert journal.checkpoint(position, "canonical_prepared")["local_unwrap"] is True
        assert _submit_live_step(entry, entry["steps"][0], **kwargs)["state"] == "deposit_pending"
        if foreign_marker:
            key = "route:bridge:bridge-original"
            progress = journal.checkpoint(position, key)
            progress["continuation"]["canonical_continuation"]["recipient"] = WALLET
            journal.save_checkpoint(position, key, progress)
            with pytest.raises(ValueError, match="native_continuation_identity"):
                _observe_route_deposit(entry, position_id=position, **kwargs)
            return
        assert _observe_route_deposit(entry, position_id=position, **kwargs)["state"] == "completed"
        assert len(journal.connection.execute(
            "SELECT * FROM route_steps WHERE position_id=?", (position,)).fetchall()) == 3
        source = journal.latest_step(position_id=position, step_key="bridge:bridge-original:0")
        assert (source["state"], source["tx_hash"]) == ("credited", "0x" + "4" * 64)
    assert submitted == [WETH_CONTRACTS[8453], "native"]
    assert direct_quotes == [AMOUNT - 100, AMOUNT - 100]
    assert projections == ([Decimal("0.012"), Decimal("0.015"), Decimal("0.015")]
                           if crash_after_hash else [Decimal("0.012"), Decimal("0.012")])
