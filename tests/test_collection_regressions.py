import json
from hashlib import sha256
from pathlib import Path

import httpx
import pytest

from evm_inventory.lifi_http import LifiHttpClient
from evm_inventory.runbook import create_route_plan
from evm_inventory.stages import Deadline, deadline_scope, request_timeout


@pytest.mark.parametrize("legacy_completed", [False, True])
def test_halted_sibling_does_not_hide_or_block_known_route_credit(tmp_path, legacy_completed):
    from decimal import Decimal

    from test_route_execution import ASSET_A, ASSET_B, WALLET, _entry

    from evm_inventory.collection import execution_positions
    from evm_inventory.journal import Journal
    from evm_inventory.route_execution import _position_key, execute_group_entries

    entries = [{**_entry(ASSET_A, status="route_ready", steps=[{"kind": "bridge"}]),
                "automatic_requote": True}, _entry(ASSET_B)]
    path = tmp_path / "inventory.sqlite"
    calls = []
    with Journal(path) as journal:
        group = journal.get_or_create_group(group_key=entries[0]["group"]["key"], wallet=WALLET,
            source_chain_id=10, source_usd="100", loss_budget_pct="15")
        positions = [journal.get_or_create_position(position_key=_position_key(e), wallet=WALLET,
                     group_id=group["id"]) for e in entries]
        p = positions[0]
        journal.record_step_intent(position_id=p["id"], step_key="bridge:saved:0", nonce=1,
            calldata_digest="a" * 64, signed_payload_digest="b" * 64, tx_hash="0x" + "c" * 64)
        journal.save_checkpoint(p["id"], "route_deposit", {"saved": "observation context"})
        journal.record_position_state(p["id"], "bridge_submitted")
        journal.record_position_state(p["id"], "deposit_pending")
        if legacy_completed:
            journal.record_position_state(p["id"], "completed")
            journal.record_group_realized_loss(group["id"], realized_loss_usd="3")
        else:
            journal.account_position_loss(p["id"], loss_usd="3")
        journal.record_position_state(positions[1]["id"], "manual_review", reason="missing gas")
        journal.record_group_state(group["id"], "active")
        journal.record_group_state(group["id"], "manual_review_group_halted", reason="missing gas")
        assert execution_positions(entries, path)[0]["state"] == (
            "route_credit_pending" if legacy_completed else "deposit_pending")
        outcomes = iter([{"state": "deposit_pending"}, RuntimeError("provider unavailable"),
                         {"state": "completed", "realized_loss_usd": "3"}])
        def submit(entry, step):
            calls.append((entry["_journal_position_id"], step["kind"]))
            result = next(outcomes)
            if isinstance(result, Exception):
                raise result
            if result["state"] == "completed":
                journal.save_checkpoint(p["id"], "bitget_credit", {"status": "success"})
            return result
        for expected in ("deposit_pending", "deposit_pending", "completed"):
            result = execute_group_entries(entries, journal=journal,
                max_route_loss_pct=Decimal("15"),
                preflight=lambda _: pytest.fail("halted group must not prepare another send"),
                submit_step=submit)
            assert result[expected] == 1
        assert result["completed"] == 1
        assert journal.group(group["id"])["state"] == "manual_review_group_halted"
        assert journal.group(group["id"])["realized_loss_usd"] == "3"
        assert journal.position(positions[1]["id"])["state"] == "manual_review"
        assert execution_positions(entries, path)[0]["state"] == "completed"
    assert calls == [(p["id"], "observe_route_deposit")] * 3


@pytest.mark.parametrize("cross_chain", [False, True])
@pytest.mark.parametrize("legacy_completed", [False, True])
def test_route_waits_for_exchange_credit_without_sending_again(
    tmp_path, monkeypatch, cross_chain, legacy_completed,
):
    from decimal import Decimal
    from types import SimpleNamespace

    from test_live_plan import _quoted_route
    from test_route_execution import ASSET_A, DEPOSIT, WALLET, _entry

    from evm_inventory.collection import execution_positions
    from evm_inventory.journal import Journal
    from evm_inventory.lifi import LifiRouteRequest, TransactionRequest
    from evm_inventory.live_plan import _route_data
    from evm_inventory.route_execution import (
        _position_key,
        _submit_live_step,
        execute_group_entries,
    )

    target_chain = 8453 if cross_chain else 10
    route = _quoted_route(LifiRouteRequest(10, target_chain, ASSET_A, "0x" + "e" * 40,
                         "1000", WALLET, DEPOSIT), route_id="deposit-swap", output_amount=900)
    step = {"kind": "bridge", "route": _route_data(route), "_live_route": route,
            "_prepared_requests": (TransactionRequest(10, DEPOSIT, "0x", 0, 21000, 1),)}
    e = {**_entry(ASSET_A, status="route_ready", steps=[step]),
         "_candidate": SimpleNamespace(loss_usd=Decimal("1")), "automatic_requote": True}
    chain = "Base" if cross_chain else "Optimism"
    e["target"] = {**e["target"], "chain_id": target_chain, "chain": chain}
    tx_hash = "0x" + "a" * 64
    deposit_hash = "0x" + "f" * 64 if cross_chain else tx_hash
    sends, polls, bridge_polls = [], [], []
    statuses = iter(["pending", None, "success"])

    def execute(*, entry, journal, position_id, **_):
        sends.append(entry["_journal_step_key"])
        journal.record_step_intent(position_id=position_id, step_key=sends[-1], nonce=1,
            calldata_digest="b" * 64, signed_payload_digest="c" * 64, tx_hash=tx_hash)
        return tx_hash

    def poll(**kw):
        polls.append(kw)
        return next(statuses)

    def bridge_status(**kw):
        bridge_polls.append(kw)
        return {"sending": {"txHash": tx_hash}, "receiving": {
            "txHash": deposit_hash, "amount": "900"}, "fromAddress": WALLET,
            "toAddress": DEPOSIT, "status": "DONE", "substatus": "COMPLETED"}

    monkeypatch.setattr("evm_inventory.route_execution._execute_entry", execute)
    path = tmp_path / "inventory.sqlite"
    with Journal(path) as journal:
        kw = dict(wallet=SimpleNamespace(public_address=WALLET, bitget_deposit_address=DEPOSIT),
                  rpc=SimpleNamespace(call=lambda *_: "0x" + f"{1000:064x}"),
                  rpc_urls={10: "rpc", 8453: "rpc"},
                  jumper=SimpleNamespace(transaction_status=bridge_status), journal=journal,
                  bitget=SimpleNamespace(wait_for_deposit_seen=poll), now_ms=lambda: 100000)
        def round():
            return execute_group_entries([e], journal=journal, max_route_loss_pct=Decimal("15"),
                preflight=lambda e: e, submit_step=lambda e, step: _submit_live_step(e, step, **kw))

        assert round()["deposit_pending"] == 1
        p = journal.connection.execute("SELECT * FROM route_positions WHERE position_key=?",
                                       (_position_key(e),)).fetchone()
        if legacy_completed:
            journal.record_position_state(p["id"], "completed")
            journal.record_group_state(p["group_id"], "completed")
            journal.connection.execute("DELETE FROM route_checkpoints WHERE position_id=? "
                                       "AND checkpoint_key='route_deposit'", (p["id"],))
            journal.connection.commit()
        assert execution_positions([e], path)[0]["state"] != "completed"
        assert round()["deposit_pending"] == 1
        assert round()["completed"] == 1
        assert execution_positions([e], path)[0]["state"] == "completed"
        assert journal.group(p["group_id"])["realized_loss_usd"] == "1"
        assert journal.checkpoint(p["id"], "bitget_credit")["tx_hash"] == deposit_hash
    assert len(sends) == 1 and len(polls) == 3
    assert len(bridge_polls) == int(cross_chain)
    assert all(p["tx_hash"] == deposit_hash and p["chain"] == chain for p in polls)


def test_legacy_direct_continuation_observes_saved_hash_without_send(tmp_path, monkeypatch):
    from decimal import Decimal
    from types import SimpleNamespace

    from test_live_plan import _quoted_route
    from test_route_execution import ASSET_A, DEPOSIT, WALLET, _entry

    from evm_inventory.collection import execution_positions
    from evm_inventory.journal import Journal
    from evm_inventory.lifi import LifiRouteRequest
    from evm_inventory.live_plan import _route_data
    from evm_inventory.route_execution import (
        _position_key,
        _submit_live_step,
        execute_group_entries,
    )

    route = _quoted_route(LifiRouteRequest(10, 8453, ASSET_A, "0x" + "e" * 40,
                         "1000", WALLET, WALLET), route_id="intermediate", output_amount=900)
    step = {"kind": "bridge", "route": _route_data(route), "_live_route": route}
    e = {**_entry(ASSET_A, status="route_ready", steps=[step]), "automatic_requote": True}
    direct = {**e, "status": "direct_deposit", "chain_id": 8453,
              "asset_id": "0x" + "e" * 40, "steps": [{"kind": "direct_deposit"}]}
    tx_hash = "0x" + "d" * 64
    polls = []
    monkeypatch.setattr("evm_inventory.route_execution._execute_entry",
                        lambda **_: pytest.fail("observation must not send"))
    path = tmp_path / "inventory.sqlite"
    with Journal(path) as journal:
        g = journal.get_or_create_group(group_key=e["group"]["key"], wallet=WALLET,
                                       source_chain_id=10, source_usd="100", loss_budget_pct="15")
        p = journal.get_or_create_position(position_key=_position_key(e), wallet=WALLET,
                                           group_id=g["id"])
        journal.record_step_intent(position_id=p["id"], step_key="direct_deposit", nonce=1,
            calldata_digest="a" * 64, signed_payload_digest="b" * 64, tx_hash=tx_hash)
        journal.save_checkpoint(p["id"], "prepared", e)
        journal.save_checkpoint(p["id"], "direct_prepared", direct)
        journal.save_checkpoint(p["id"], "route:bridge:intermediate", {"continuation": direct})
        journal.record_position_state(p["id"], "bridge_submitted")
        journal.record_position_state(p["id"], "completed")
        journal.record_group_state(g["id"], "active")
        journal.record_group_state(g["id"], "completed")
        assert execution_positions([e], path)[0]["state"] == "route_credit_pending"
        wallet = SimpleNamespace(public_address=WALLET, bitget_deposit_address=DEPOSIT)
        result = execute_group_entries([e], journal=journal, max_route_loss_pct=Decimal("15"),
            preflight=lambda _: pytest.fail("observation must not quote"),
            submit_step=lambda e, step: _submit_live_step(e, step, wallet=wallet, rpc=object(),
                rpc_urls={}, jumper=object(), bitget=SimpleNamespace(wait_for_deposit_seen=lambda
                    **kw: polls.append(kw) or "success"), journal=journal, now_ms=lambda: 100000))
        assert result["completed"] == 1
        assert execution_positions([e], path)[0]["state"] == "completed"
    assert len(polls) == 1 and polls[0]["tx_hash"] == tx_hash


def test_unauthenticated_quota_uses_published_limits_with_execution_reserves():
    from evm_inventory.lifi_quota import LifiQuotaStore

    assert LifiQuotaStore.limits == {
        "routes": (75, 7200, 5), "steps": (50, 7200, 3), "public": (100, 60, 0)
    }


def test_settlement_sleep_cannot_be_negative_when_clock_crosses_runtime(tmp_path):
    from test_route_execution import _entry

    from evm_inventory.collection import drive_execution

    armed = [False]
    samples = iter([1009.0, 1011.0])

    def clock():
        return next(samples, 1011.0) if armed[0] else 1000.0

    def execute(_):
        armed[0] = True
        return {}

    def sleep(seconds):
        assert seconds >= 0

    result = drive_execution({"entries": [_entry("native")]}, tmp_path / "inventory.sqlite",
                             execute_round=execute, until=1010.0, clock=clock, sleep=sleep)
    assert result["status"] == "incomplete"


def test_live_route_valuation_reads_clock_after_fetching_fresh_gas_prices():
    from datetime import timedelta
    from types import SimpleNamespace

    from test_route_execution import ASSET_A, DEPOSIT, NOW, WALLET, _entry, _lifi_route

    from evm_inventory.bitget_catalog import BitgetDepositTarget
    from evm_inventory.lifi import TransactionRequest
    from evm_inventory.live_plan import _route_data
    from evm_inventory.models import AssetIdentity
    from evm_inventory.route_execution import _refresh_route_step
    from evm_inventory.valuation import FeeQuote, QuotePrice

    route = _lifi_route(route_id="fresh", output_amount=910_000)
    step = {"kind": "bridge", "route": _route_data(route)}
    clock = [NOW]

    def estimate(**_):
        clock[0] += timedelta(seconds=3)
        return (FeeQuote(1, QuotePrice(AssetIdentity(10, "native", 18), "2000", clock[0])),)

    gas = SimpleNamespace(
        rpc=SimpleNamespace(call=lambda *_: "0x" + f"{1_000_000:064x}"),
        rpc_urls={10: "rpc"}, estimate_prepared_route=estimate,
    )
    candidate, _ = _refresh_route_step(
        _entry(ASSET_A, status="route_ready", steps=[step]), step,
        wallet=SimpleNamespace(public_address=WALLET, bitget_deposit_address=DEPOSIT),
        jumper=SimpleNamespace(routes=lambda _: (route,), step_transaction=lambda _:
                              TransactionRequest(10, DEPOSIT, "0x", 0, 21_000, 1)),
        gas_estimator=gas, now=NOW, now_fn=lambda: clock[0],
        bitget=SimpleNamespace(revalidate_deposit_target=lambda *_a, **_kw:
            BitgetDepositTarget("USDC", 8453, "0x" + "e" * 40, 1, "Base", 6)),
    )
    assert candidate.is_valid


@pytest.mark.parametrize("reason", ["requote_required:stale_price", "native_gas_reserve_unstable"])
@pytest.mark.parametrize("broadcast", [None, "approval:router", "bridge:0"])
def test_stale_price_group_recovery_preserves_approvals_and_never_unlocks_spent_assets(
    tmp_path, broadcast, reason,
):
    from evm_inventory.journal import Journal

    with Journal(tmp_path / "inventory.sqlite") as journal:
        g = journal.get_or_create_group(group_key="g", wallet="wallet", source_chain_id=10,
                                       source_usd="100", loss_budget_pct="15")
        p = journal.get_or_create_position(position_key="p", wallet="wallet", group_id=g["id"])
        if broadcast:
            journal.record_step_intent(position_id=p["id"], step_key=broadcast, nonce=1,
                calldata_digest="a" * 64, signed_payload_digest="b" * 64,
                tx_hash="0x" + "c" * 64)
        journal.record_position_state(p["id"], "manual_review", reason=reason)
        journal.record_group_state(g["id"], "manual_review_group_halted", reason=reason)
        assert not journal.reactivate_unsigned_retry_group(g["id"], {"another-position"})
        assert journal.reactivate_unsigned_retry_group(g["id"], {"p"}) is (broadcast != "bridge:0")
        if broadcast != "bridge:0":
            assert journal.position(p["id"])["state"] == "planned"
            assert journal.group(g["id"])["state"] == "active"
            assert journal.checkpoint(p["id"], "automatic_retry")["reason"] == reason
            if broadcast:
                assert journal.latest_step(position_id=p["id"], step_key=broadcast)["tx_hash"]


@pytest.mark.parametrize("reason", ["requote_required:stale_price", "native_gas_reserve_unstable"])
def test_automatic_stale_price_failure_yields_and_retries_instead_of_halting(tmp_path, reason):
    from decimal import Decimal

    from test_route_execution import ASSET_A, _entry

    from evm_inventory.journal import Journal
    from evm_inventory.route_execution import execute_group_entries

    e = {**_entry(ASSET_A), "automatic_requote": True}
    with Journal(tmp_path / "inventory.sqlite") as journal:
        def stale(_):
            raise ValueError(reason)

        result = execute_group_entries([e], journal=journal, max_route_loss_pct=Decimal("15"),
                                      preflight=stale, submit_step=lambda *_: pytest.fail("send"))
        assert result["deferred"] == 1
        assert journal.group(result["group_id"])["state"] == "active"


@pytest.mark.parametrize("automatic", [False, True])
@pytest.mark.parametrize("skipped_sibling", [False, True])
def test_collection_resumes_only_explicit_automatic_stale_price_groups(
    tmp_path, automatic, skipped_sibling,
):
    from test_route_execution import ASSET_A, ASSET_B, _entry

    from evm_inventory.collection import _resume_unsigned_retry_groups
    from evm_inventory.journal import Journal
    from evm_inventory.route_execution import _position_key

    path = tmp_path / "inventory.sqlite"
    e = {**_entry(ASSET_A), "automatic_requote": automatic}
    with Journal(path) as journal:
        g = journal.get_or_create_group(group_key=e["group"]["key"], wallet=e["wallet"],
            source_chain_id=10, source_usd="100", loss_budget_pct="15")
        p = journal.get_or_create_position(position_key=_position_key(e), wallet=e["wallet"],
                                          group_id=g["id"])
        reason = "requote_required:stale_price"
        journal.record_position_state(p["id"], "manual_review", reason=reason)
        journal.record_group_state(g["id"], "manual_review_group_halted", reason=reason)
    entries = [e]
    if skipped_sibling:
        entries.append({**_entry(ASSET_B), "status": "manual_review",
                        "reason": "below_target_minimum", "automatic_requote": automatic})
    _resume_unsigned_retry_groups(entries, path)
    with Journal(path) as journal:
        assert journal.group(g["id"])["state"] == (
            "active" if automatic else "manual_review_group_halted")
        assert journal.position(p["id"])["state"] == ("planned" if automatic else "manual_review")
        assert journal.group(g["id"])["source_usd"] == "100"
        assert journal.group(g["id"])["loss_budget_pct"] == "15"


@pytest.mark.parametrize("phase", ["intermediate", "after_swap"])
@pytest.mark.parametrize("reason", ["stale_price", "missing_destination_price"])
def test_dependent_requote_preserves_stale_price_failure(monkeypatch, phase, reason):
    from types import SimpleNamespace

    from test_live_plan import _quoted_route
    from test_route_execution import ASSET_A, DEPOSIT, NOW, WALLET, _entry, _lifi_route

    import evm_inventory.route_execution as execution
    from evm_inventory.bitget_catalog import BitgetDepositTarget
    from evm_inventory.lifi import TransactionRequest
    from evm_inventory.live_plan import _route_data
    from evm_inventory.models import AssetIdentity
    from evm_inventory.valuation import FeeQuote, QuotePrice

    planned = _lifi_route(route_id="planned", output_amount=910_000)
    step = {"kind": "bridge", "route": _route_data(planned)}
    e = {**_entry(ASSET_A, status="route_ready", steps=[step]), "automatic_requote": True}
    monkeypatch.setattr(execution, "candidate_from_route", lambda *_a, **_kw:
                        SimpleNamespace(is_valid=False, reason=reason))
    client = SimpleNamespace(
        routes=lambda request: (_quoted_route(request, route_id="live", output_amount=910_000,
                                              gas_amount=10),),
        step_transaction=lambda _: TransactionRequest(10, DEPOSIT, "0x", 0, 21_000, 1),
    )
    kwargs = dict(
        wallet=SimpleNamespace(public_address=WALLET, bitget_deposit_address=DEPOSIT),
        jumper=client, gas_estimator=SimpleNamespace(estimate_prepared_route=lambda **_:
            (FeeQuote(10, QuotePrice(AssetIdentity(10, "native", 18), "2000", NOW)),)),
        now_ms=lambda: int(NOW.timestamp() * 1000),
        bitget=SimpleNamespace(revalidate_deposit_target=lambda *_a, **_kw:
            BitgetDepositTarget("USDC", 8453, "0x" + "e" * 40, 1, "Base", 6)),
    )
    match = "requote_required:stale_price" if reason == "stale_price" else "eligible.*route"
    with pytest.raises(ValueError, match=match):
        if phase == "intermediate":
            execution._requote_from_intermediate(e, AssetIdentity(10, ASSET_A, 6), 1_000_000,
                                                rpc=object(), rpc_urls={}, **kwargs)
        else:
            execution._requote_bridge_after_swap(e, 1_000_000, **kwargs)


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


def test_best_available_sorts_missing_cost_candidate_without_aborting_group(tmp_path):
    from dataclasses import replace

    from test_live_plan import DESTINATION, _quoted_route, _RecordingQuotes
    from test_route_search import gas_quote, plan_for

    from evm_inventory.bitget_catalog import BitgetDepositTarget
    from evm_inventory.lifi import LifiFeeCost
    from evm_inventory.models import AssetIdentity

    quotes = _RecordingQuotes(lambda r: (
        replace(_quoted_route(r, route_id="unknown-cost", output_amount=9_999_000, gas_amount=1),
                fee_costs=(LifiFeeCost(1, AssetIdentity(8453, "0x" + "f" * 40, 18),
                                       None, None, None),)),
        _quoted_route(r, route_id="valid-cost", output_amount=9_999_000, gas_amount=1),
    ))
    entry = plan_for(tmp_path, (BitgetDepositTarget("USDC", 10, DESTINATION, 1, decimals=6),),
                     quotes, lambda **_: gas_quote(1), allow_partial_search=True)["entries"][0]
    assert entry["status"] == "route_ready"
    assert entry["route"]["id"] == "valid-cost"


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
@pytest.mark.parametrize("fee_multipliers", [(10,), (10, 20, 10), (10, 11, 12)])
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

    calls = []

    class ExactGas(PlannerGasEstimator):
        def __call__(self, **kwargs):
            index = min(len(calls), len(fee_multipliers) - 1)
            calls.append(kwargs["route"].route_id)
            return (
                FeeQuote(
                    gas * fee_multipliers[index] // 10,
                    QuotePrice(AssetIdentity(10, "native", 18), "2000", NOW),
                ),
            )

    estimator = ExactGas(None, {10: "rpc"}, quotes)

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
    assert balance - gas * 8 <= int(quotes.requests[-1].from_amount) <= balance - gas * 3
    assert plan["entries"][0]["status"] == "route_ready"
    if fee_multipliers == (10, 11, 12):
        assert len(calls) == 2


def test_native_plan_padding_does_not_create_false_deposit_minimum_rejection(tmp_path):
    test_native_search_requotes_spendable_amount_with_gas_reserve(
        tmp_path, 10**12, (10,), 19_893_500,
    )


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


@pytest.mark.parametrize("revert", ["allowance_text", "lifi_transfer_from_custom", "erc6093"])
@pytest.mark.parametrize("allowance,native", [(0, False), (1000, False), (0, True)])
@pytest.mark.parametrize("gas_hint", [50_000, 1_422_200, 0, 20_000_000])
def test_missing_allowance_uses_bounded_fee_quote_until_approval_is_confirmed(
    revert, allowance, native, gas_hint,
):
    from dataclasses import replace

    from test_live_plan import _quoted_route
    from test_planner_gas import SPENDER, TOKEN, WALLET, PriceClient, Rpc

    from evm_inventory.lifi import LifiRouteRequest
    from evm_inventory.models import AssetIdentity
    from evm_inventory.planner_gas import PlannerGasEstimator

    class AllowanceRpc(Rpc):
        def call(self, url, method, params):
            if method == "eth_call" and params[0]["data"].startswith("0xdd62ed3e"):
                return "0x" + f"{allowance:064x}"
            if method == "eth_estimateGas" and params[0]["to"] == "0x" + "d" * 40:
                error = ValueError("execution reverted: ERC20: insufficient allowance"
                                   if revert == "allowance_text" else "execution reverted")
                if revert == "lifi_transfer_from_custom":
                    error.diagnostic = {"provider_error": {"data": "0x7939f424"}}
                elif revert == "erc6093":
                    error.diagnostic = {"provider_error": {"data": (
                        "0xfb8f41b2" + SPENDER[2:].rjust(64, "0")
                        + f"{allowance:064x}" + f"{1000:064x}")}}
                raise error
            return super().call(url, method, params)

    rpc = AllowanceRpc()
    class HintClient(PriceClient):
        def step_transaction(self, step):
            return replace(super().step_transaction(step), gas_limit=gas_hint)
    estimator = PlannerGasEstimator(rpc, {10: "rpc"}, HintClient())
    route = _quoted_route(
        LifiRouteRequest(10, 8453, "0x" + "0" * 40 if native else TOKEN,
                         "0x" + "c" * 40, "1000", WALLET, WALLET),
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
    if native or allowance >= 1000 or gas_hint in {0, 20_000_000}:
        assert fees is None
        assert route.route_id not in estimator.prepared_fee_caps
        return
    assert fees is not None and len(fees) == 2
    assert fees[0].raw_amount > fees[1].raw_amount
    prepared = estimator.prepared_fee_caps[route.route_id][0]
    assert prepared.max_total_fee_cap_wei == fees[0].raw_amount
    assert prepared.fee_quote_fallback_reason == "pending_approval"
    assert prepared.gas_limit <= 30_000_000


@pytest.mark.parametrize("change", [None, "spender", "needed", "allowance", "padding", "length",
                                    "calldata", "message", "malformed", "named_error_message"])
def test_erc6093_allowance_revert_requires_exact_expected_abi_fields(change):
    from test_planner_gas import SPENDER

    from evm_inventory.planner_gas import _approval_dependent_revert
    data = "0xfb8f41b2" + SPENDER[2:].rjust(64, "0") + f"{0:064x}" + f"{1000:064x}"
    if change in {"spender", "named_error_message"}:
        data = "0xfb8f41b2" + ("1" * 40).rjust(64, "0") + f"{0:064x}" + f"{1000:064x}"
    elif change == "needed":
        data = data[:-64] + f"{1001:064x}"
    elif change == "allowance":
        data = data[:74] + f"{1000:064x}" + f"{1000:064x}"
    elif change == "padding":
        data = data[:10] + "f" + data[11:]
    elif change == "length":
        data += "00"
    elif change == "malformed":
        data = data[:-1] + "g"
    diagnostic = {"provider_error": {"data": data}}
    if change == "named_error_message":
        diagnostic["provider_error"]["message"] = "execution reverted: ERC20InsufficientAllowance"
    if change == "calldata":
        diagnostic = {"params": [{"data": data}]}
    elif change == "message":
        diagnostic = {"provider_error": {"message": data}}
    error = ValueError("estimate_gas_rpc_error:" + json.dumps({"diagnostic": diagnostic}))
    assert _approval_dependent_revert(error, expected_spender=SPENDER,
                                     expected_amount=1000) is (change is None)


@pytest.mark.parametrize("detail", [
    {"diagnostic": {"provider_error": {"data": "0xdeadbeef"}}},
    {"diagnostic": {"params": [{"data": "0x7939f424"}]}},
    {"message": "0x7939f424"},
    {"diagnostic": None},
])
def test_custom_approval_revert_requires_exact_rpc_error_data(detail):
    from evm_inventory.planner_gas import _approval_dependent_revert

    assert not _approval_dependent_revert(
        ValueError("estimate_gas_rpc_error:" + json.dumps(detail)))


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


@pytest.mark.parametrize("confirmed_allowance", [0, 1_000_000])
def test_fresh_approval_checks_confirmed_allowance_and_retains_intent(
    tmp_path, monkeypatch, confirmed_allowance,
):
    from dataclasses import replace
    from types import SimpleNamespace

    from test_route_execution import ASSET_A, DEPOSIT, WALLET, _entry

    from evm_inventory.journal import Journal
    from evm_inventory.lifi import TransactionRequest
    from evm_inventory.route_execution import _approve_if_needed

    phases = []
    spender = "0x" + "c" * 40
    approval_hash = "0x" + "a" * 64
    request = TransactionRequest(10, DEPOSIT, "0x", 0, 21_000, 1)
    entry = {**_entry(ASSET_A, status="route_ready"),
             "route": {"step": {"estimate": {"approvalAddress": spender}}}}

    def allowance(*_, **kw):
        assert (kw["token"], kw["owner"], kw["spender"]) == (ASSET_A, WALLET, spender)
        phases.append("allowance")
        return confirmed_allowance if "receipt" in phases else 0

    def broadcast(*_, journal, position_id, step_key, **kw):
        phases.append("approval_sent")
        journal.record_step_intent(position_id=position_id, step_key=step_key,
            nonce=kw["nonce"], calldata_digest="b" * 64, signed_payload_digest="c" * 64,
            tx_hash=kw["tx_hash"])
        return approval_hash

    module = "evm_inventory.route_execution."
    monkeypatch.setattr(module + "token_allowance", allowance)
    monkeypatch.setattr(module + "recover_durable_broadcast", lambda *a, **kw: None)
    monkeypatch.setattr(module + "FeePlanner", lambda _: SimpleNamespace(
        plan=lambda url, tx, **kw: replace(tx, gas_limit=21_000, gas_price_wei=1)))
    monkeypatch.setattr(module + "_native_balance", lambda *a, **kw: 1_000_000)
    monkeypatch.setattr(module + "pending_nonce", lambda *a, **kw: 1)
    monkeypatch.setattr(module + "sign_transaction", lambda *a, **kw: "approval-payload")
    monkeypatch.setattr(module + "signed_transaction_hash", lambda _: approval_hash)
    monkeypatch.setattr(module + "broadcast_durable_transaction", broadcast)
    monkeypatch.setattr(module + "wait_for_receipt", lambda *a, **kw: phases.append("receipt"))
    with Journal(tmp_path / "inventory.sqlite") as journal:
        position = journal.get_or_create_position(position_key="approval", wallet=WALLET)
        kwargs = dict(entry=entry, request=request,
            wallet=SimpleNamespace(public_address=WALLET, private_key="secret"),
            rpc=object(), url="rpc", journal=journal, position_id=position["id"],
            route_step_key="bridge:test:0")
        if confirmed_allowance:
            assert _approve_if_needed(**kwargs) == approval_hash
        else:
            with pytest.raises(ValueError, match="approval confirmed without sufficient"):
                _approve_if_needed(**kwargs)
        assert journal.latest_step(position_id=position["id"],
            step_key="approval:bridge:test:0")["tx_hash"] == approval_hash
    assert phases == ["allowance", "approval_sent", "receipt", "allowance"]


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
@pytest.mark.parametrize("native_balance,fee_values,minimum", [
    (2000, (10,), 1), (2000, (10, 11, 10), 1), (2000, (10, 11, 12), 1),
    (2000, (20, 21, 22), 1), (100, (30,), 1), (100, (10,), 68),
])
@pytest.mark.parametrize("saved_cap", [None, 10])
def test_automatic_native_preflight_reduces_input_after_sibling_gas_spending(
    provider_hint, fee_values, saved_cap, native_balance, minimum,
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
    e["target"]["minimum_raw"] = str(minimum)
    if saved_cap is not None:
        e["reservations"] = {"source_native_gas_cap_raw": str(saved_cap)}
    gas = SimpleNamespace(
        rpc=SimpleNamespace(call=lambda *_: hex(native_balance)),
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
            _quoted_route(request, route_id="fresh", gas_amount=provider_hint,
                          output_amount=int(request.from_amount) if minimum > 1 else 900),
        )

    client = SimpleNamespace(
        routes=routes,
        step_transaction=lambda _: TransactionRequest(10, DEPOSIT, "0x", 0, 21_000, 1),
    )
    bitget = SimpleNamespace(
        revalidate_deposit_target=lambda *_a, **_kw: BitgetDepositTarget(
            "USDC", 8453, destination, minimum, "Base", 6
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
    expected = native_balance - (fee_values[0] * 3 * 12 + 9) // 10
    if expected <= 0:
        expected = native_balance - fee_values[0] * 3
    if minimum > 1:
        expected = native_balance - fee_values[0] * 3
    assert int(fresh["route"]["from_amount"]) == expected
    assert amounts[-1] == expected
    expected_requests = 2 if saved_cap is None or fee_values[0] > saved_cap else 1
    assert len(amounts) == expected_requests + (minimum > 1)


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
