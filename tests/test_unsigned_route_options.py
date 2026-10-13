"""Unsigned simulation recovery with exact group accounting and alternative routes."""

from dataclasses import replace
from datetime import timedelta
from decimal import Decimal
from types import SimpleNamespace

import httpx
import pytest
from test_live_plan import NOW, _quoted_route
from test_route_execution import ASSET_A, DEPOSIT, WALLET, _entry
from test_unsigned_preparation_recovery import _status_error

from evm_inventory.bitget_catalog import BitgetDepositTarget
from evm_inventory.journal import Journal
from evm_inventory.lifi import LifiRouteRequest, TransactionRequest
from evm_inventory.live_plan import _route_data
from evm_inventory.models import AssetIdentity
from evm_inventory.route_execution import (
    _position_key,
    _refresh_route_step,
    _retryable_provider_error,
    execute_group_entries,
)
from evm_inventory.valuation import FeeQuote, QuotePrice


def _partial_retry_group(journal, *, credit_change=None, intent="approval:router"):
    reason = "requote_required:missing_gas_estimate"
    group = journal.get_or_create_group(
        group_key="partial",
        wallet=WALLET,
        source_chain_id=10,
        source_usd="100",
        loss_budget_pct="15",
    )
    pending = journal.get_or_create_position(
        position_key="ezeth", wallet=WALLET, group_id=group["id"]
    )
    completed = journal.get_or_create_position(
        position_key="ohno", wallet=WALLET, group_id=group["id"]
    )
    if intent:
        journal.record_step_intent(
            position_id=pending["id"],
            step_key=intent,
            nonce=1,
            calldata_digest="a" * 64,
            signed_payload_digest="b" * 64,
            tx_hash=None,
        )
    sent = journal.record_step_intent(
        position_id=completed["id"],
        step_key="bridge:ohno:0",
        nonce=2,
        calldata_digest="c" * 64,
        signed_payload_digest="d" * 64,
        tx_hash="0x" + "e" * 64,
    )
    context = {
        "target": {"coin": "USDC", "chain": "Base"},
        "recipient": DEPOSIT,
        "tx_hash": "0x" + "f" * 64,
        "source_hash": sent["tx_hash"],
        "step_key": sent["step_key"],
    }
    credit = {
        "status": "success",
        "tx_hash": context["tx_hash"],
        "recipient": DEPOSIT,
        "coin": "USDC",
        "chain": "Base",
    }
    journal.save_checkpoint(completed["id"], "route_deposit", context)
    if credit_change != "missing":
        journal.save_checkpoint(
            completed["id"], "bitget_credit", {**credit, **(credit_change or {})}
        )
    journal.account_position_loss(pending["id"], loss_usd="2")
    journal.account_position_loss(completed["id"], loss_usd="3")
    journal.record_position_state(completed["id"], "completed")
    journal.record_position_state(pending["id"], "manual_review", reason=reason)
    journal.record_group_state(group["id"], "manual_review_group_halted", reason=reason)
    return group, pending, completed


@pytest.mark.parametrize("keys", [{"ezeth"}, {"ezeth", "ohno", "foreign"}, {"ezeth", "foreign"}])
def test_partial_retry_requires_complete_exact_group_members(tmp_path, keys):
    with Journal(tmp_path / "inventory.sqlite") as journal:
        group, _, _ = _partial_retry_group(journal)
        assert not journal.reactivate_unsigned_retry_group(group["id"], keys)


@pytest.mark.parametrize(
    "credit",
    [
        "missing",
        {"status": "pending"},
        {"tx_hash": "wrong"},
        {"recipient": WALLET},
        {"coin": "ETH"},
        {"chain": "Optimism"},
    ],
)
def test_partial_retry_requires_context_bound_successful_credit(tmp_path, credit):
    with Journal(tmp_path / "inventory.sqlite") as journal:
        group, _, _ = _partial_retry_group(journal, credit_change=credit)
        assert not journal.reactivate_unsigned_retry_group(group["id"], {"ezeth", "ohno"})


@pytest.mark.parametrize("intent", ["bridge:ezeth:0", "swap:ezeth:0", "direct_deposit"])
def test_partial_retry_rejects_any_unsigned_asset_intent(tmp_path, intent):
    with Journal(tmp_path / "inventory.sqlite") as journal:
        group, _, _ = _partial_retry_group(journal, intent=intent)
        assert not journal.reactivate_unsigned_retry_group(group["id"], {"ezeth", "ohno"})


def test_partial_retry_preserves_terminal_sibling_approvals_and_original_accounting(tmp_path):
    with Journal(tmp_path / "inventory.sqlite") as journal:
        group, pending, completed = _partial_retry_group(journal)
        terminal_before = journal.position(completed["id"])
        approval = journal.latest_step(position_id=pending["id"], step_key="approval:router")
        checkpoints = journal.connection.execute(
            "SELECT * FROM route_checkpoints WHERE position_id=?", (completed["id"],)
        ).fetchall()
        assert journal.reactivate_unsigned_retry_group(group["id"], {"ezeth", "ohno"})
        assert journal.position(completed["id"]) == terminal_before
        assert (
            journal.latest_step(position_id=pending["id"], step_key="approval:router") == approval
        )
        assert journal.position(pending["id"])["state"] == "planned"
        assert journal.position(pending["id"])["accounted_loss_usd"] == "2"
        current = journal.group(group["id"])
        assert (
            current["source_usd"],
            current["loss_budget_pct"],
            current["realized_loss_usd"],
        ) == ("100", "15", "5")
        assert (
            journal.connection.execute(
                "SELECT * FROM route_checkpoints WHERE position_id=?", (completed["id"],)
            ).fetchall()
            == checkpoints
        )


@pytest.mark.parametrize("completed_first", [False, True])
def test_group_projection_excludes_completed_sibling_in_either_order(tmp_path, completed_first):
    completed = _entry("ohno", loss="10")
    remaining = _entry("ezeth", loss="5")
    entries = [completed, remaining] if completed_first else [remaining, completed]
    submitted = []
    with Journal(tmp_path / "inventory.sqlite") as journal:
        group = journal.get_or_create_group(
            group_key=remaining["group"]["key"],
            wallet=WALLET,
            source_chain_id=10,
            source_usd="100",
            loss_budget_pct="15",
        )
        terminal = journal.get_or_create_position(
            position_key=_position_key(completed),
            wallet=WALLET,
            group_id=group["id"],
            source_asset_id=completed["asset_id"],
            source_amount_raw=completed["raw_balance"],
        )
        journal.account_position_loss(terminal["id"], loss_usd="10")
        journal.record_position_state(terminal["id"], "completed")
        result = execute_group_entries(
            entries,
            journal=journal,
            max_route_loss_pct=Decimal("15"),
            preflight=lambda e: e,
            submit_step=lambda e, _: (
                submitted.append(e["asset_id"]) or {"state": "completed", "realized_loss_usd": "5"}
            ),
        )
        assert result["completed"] == 2
        assert submitted == ["ezeth"]
        assert journal.group(group["id"])["projected_loss_usd"] == "15"


def test_group_future_projection_subtracts_already_accounted_unfinished_cost(tmp_path):
    entries = [_entry("first", loss="5"), _entry("second", loss="10")]
    with Journal(tmp_path / "inventory.sqlite") as journal:
        group = journal.get_or_create_group(
            group_key=entries[0]["group"]["key"],
            wallet=WALLET,
            source_chain_id=10,
            source_usd="100",
            loss_budget_pct="15",
        )
        second = journal.get_or_create_position(
            position_key=_position_key(entries[1]),
            wallet=WALLET,
            group_id=group["id"],
            source_asset_id=entries[1]["asset_id"],
            source_amount_raw=entries[1]["raw_balance"],
        )
        journal.account_position_loss(second["id"], loss_usd="5")
        result = execute_group_entries(
            entries,
            journal=journal,
            max_route_loss_pct=Decimal("15"),
            preflight=lambda e: e,
            submit_step=lambda e, _: {
                "state": "completed",
                "realized_loss_usd": e["valuation"]["loss_usd"],
            },
        )
        assert result["completed"] == 2
        assert journal.group(group["id"])["projected_loss_usd"] == "15"


def _fallback_case(*, preparation_failure=None, simulation_failure=False, options=None):
    from evm_inventory.diagnostics import exception_diagnostic
    from evm_inventory.rpc import RpcError

    request = LifiRouteRequest(10, 8453, ASSET_A, "0x" + "e" * 40, "1000000", WALLET, DEPOSIT)
    first = _quoted_route(request, route_id="first", output_amount=999999, gas_amount=1)
    second = _quoted_route(request, route_id="second", output_amount=999999, gas_amount=1)
    routes = tuple(options(request, first, second)) if options else (first, second)
    step = {"kind": "bridge", "route": _route_data(first)}
    entry = {**_entry(ASSET_A, status="route_ready", steps=[step]), "automatic_requote": True}
    prepared = []
    gas = SimpleNamespace(
        rpc=SimpleNamespace(call=lambda *_: "0x" + f"{1000000:064x}"),
        rpc_urls={10: "rpc"},
        diagnostics=[],
    )

    def prepare(raw_step):
        route_id = raw_step["id"].removeprefix("step-")
        prepared.append(route_id)
        if preparation_failure:
            error = preparation_failure(route_id)
            if error:
                raise error
        return TransactionRequest(10, DEPOSIT, "0x", 0, 21000, 1)

    def estimate(*, route, **_):
        if simulation_failure and (
            simulation_failure is True or simulation_failure(route.route_id)
        ):
            error = RpcError(
                "rpc_error",
                diagnostic={
                    "method": "eth_estimateGas",
                    "provider_error": {
                        "code": 3,
                        "message": "execution reverted",
                        "data": "0xdeadbeef",
                    },
                },
            )
            gas.diagnostics.append(
                {
                    "route_id": route.route_id,
                    "phase": "estimate",
                    "diagnostic": exception_diagnostic(error),
                }
            )
            return None
        return (FeeQuote(1, QuotePrice(AssetIdentity(10, "native", 18), "2000", NOW)),)

    gas.estimate_prepared_route = estimate
    kwargs = dict(
        wallet=SimpleNamespace(public_address=WALLET, bitget_deposit_address=DEPOSIT),
        jumper=SimpleNamespace(routes=lambda _: routes, step_transaction=prepare),
        gas_estimator=gas,
        bitget=SimpleNamespace(
            revalidate_deposit_target=lambda *_a, **_kw: BitgetDepositTarget(
                "USDC", 8453, "0x" + "e" * 40, 1, "Base", 6
            )
        ),
    )
    return entry, step, kwargs, prepared


@pytest.mark.parametrize("phase", ["prepare", "simulate"])
def test_refresh_tries_next_route_after_exact_deterministic_failure(phase):
    entry, step, kwargs, prepared = _fallback_case(
        preparation_failure=(lambda route: _status_error() if route == "first" else None)
        if phase == "prepare"
        else None,
        simulation_failure=(lambda route: route == "first") if phase == "simulate" else False,
    )
    candidate, fresh = _refresh_route_step(entry, step, now=NOW, **kwargs)
    assert candidate.is_valid
    assert fresh["route"]["id"] == "second"
    assert prepared == ["first", "second"]
    assert kwargs["gas_estimator"].diagnostics


def test_refresh_skips_foreign_and_invalid_alternatives_without_option_limit():
    def options(request, first, second):
        foreign = _quoted_route(
            replace(request, to_address=WALLET),
            route_id="foreign",
            output_amount=999999,
            gas_amount=1,
        )
        invalid = _quoted_route(request, route_id="below-minimum", output_amount=1, gas_amount=1)
        failures = [
            _quoted_route(request, route_id=f"bad-{i}", output_amount=999999, gas_amount=1)
            for i in range(7)
        ]
        return (first, foreign, invalid, *failures, second)

    entry, step, kwargs, prepared = _fallback_case(
        options=options,
        preparation_failure=lambda route: (
            _status_error() if route not in {"second", "below-minimum"} else None
        ),
    )
    candidate, fresh = _refresh_route_step(entry, step, now=NOW, **kwargs)
    assert candidate.is_valid and fresh["route"]["id"] == "second"
    assert "foreign" not in prepared
    assert prepared == ["first", "below-minimum", *[f"bad-{i}" for i in range(7)], "second"]


@pytest.mark.parametrize("phase", ["prepare", "simulate"])
def test_refresh_exhaustion_is_scheduled_beyond_quote_ttl_with_sanitized_route_diagnostics(phase):
    entry, step, kwargs, prepared = _fallback_case(
        preparation_failure=(lambda _: _status_error()) if phase == "prepare" else None,
        simulation_failure=phase == "simulate",
    )
    with pytest.raises(ValueError) as failure:
        _refresh_route_step(
            entry, step, now=NOW, now_fn=lambda: NOW + timedelta(seconds=30), **kwargs
        )
    assert str(failure.value) == "unsigned_route_preparation_exhausted"
    assert _retryable_provider_error(failure.value)
    assert failure.value.retry_at == NOW.timestamp() + 270
    assert prepared == ["first", "second"]
    assert {d["route_id"] for d in failure.value.diagnostic["route_failures"]} == {
        "first",
        "second",
    }
    assert "abcdefgh-secret" not in str(failure.value.diagnostic)
    assert "secret.example" not in str(failure.value.diagnostic)


@pytest.mark.parametrize("failure_kind", ["transport", "rpc", "quota", "deadline", "other_422"])
@pytest.mark.parametrize("after_deterministic", [False, True])
def test_refresh_propagates_operational_failures_without_trying_more_routes(
    failure_kind, after_deterministic
):
    from evm_inventory.lifi_quota import LifiRequestLimit
    from evm_inventory.rpc import RpcError
    from evm_inventory.stages import StageDeadlineExceeded

    errors = {
        "transport": httpx.ConnectError("offline"),
        "rpc": RpcError("provider_access_denied"),
        "quota": LifiRequestLimit("route_quota_exhausted", retry_at=NOW.timestamp() + 60),
        "deadline": StageDeadlineExceeded("stage_deadline_exceeded"),
        "other_422": _status_error(url="https://li.quest/v1/advanced/routes"),
    }
    error = errors[failure_kind]
    entry, step, kwargs, prepared = _fallback_case(
        preparation_failure=lambda route: (
            _status_error() if after_deterministic and route == "first" else error
        )
    )
    with pytest.raises(type(error)) as failure:
        _refresh_route_step(entry, step, now=NOW, **kwargs)
    assert failure.value is error
    assert prepared == (["first", "second"] if after_deterministic else ["first"])


@pytest.mark.parametrize("frozen", ["disabled", "dependent", "asset-intent"])
def test_frozen_refresh_never_replaces_exact_route_on_deterministic_failure(frozen):
    entry, step, kwargs, prepared = _fallback_case(preparation_failure=lambda _: _status_error())
    if frozen == "disabled":
        entry["automatic_requote"] = False
    elif frozen == "asset-intent":
        entry["_journal_position_id"] = 1
    with pytest.raises(httpx.HTTPStatusError):
        _refresh_route_step(
            entry,
            step,
            now=NOW,
            **({"source_amount": 1000000} if frozen == "dependent" else {}),
            **kwargs,
        )
    assert prepared == ["first"]


@pytest.mark.parametrize("changed", ["source", "destination", "amount", "recipient"])
def test_refresh_skips_each_foreign_route_identity(changed):
    def options(request, first, second):
        changes = {
            "source": {"from_token_address": "0x" + "b" * 40},
            "destination": {"to_chain_id": 1},
            "amount": {"from_amount": "999999"},
            "recipient": {"to_address": WALLET},
        }
        foreign = _quoted_route(
            replace(request, **changes[changed]),
            route_id="foreign",
            output_amount=999999,
            gas_amount=1,
        )
        return (first, foreign, second)

    entry, step, kwargs, prepared = _fallback_case(
        options=options,
        preparation_failure=lambda route: _status_error() if route == "first" else None,
    )
    _, fresh = _refresh_route_step(entry, step, now=NOW, **kwargs)
    assert fresh["route"]["id"] == "second"
    assert prepared == ["first", "second"]


@pytest.mark.parametrize(
    "diagnostic_kind", ["unstructured", "previous", "foreign_route", "wrong_method", "non_revert"]
)
def test_missing_gas_without_this_attempts_structured_simulation_revert_remains_locked(
    diagnostic_kind,
):
    from evm_inventory.diagnostics import exception_diagnostic
    from evm_inventory.rpc import RpcError

    entry, step, kwargs, prepared = _fallback_case()
    gas = kwargs["gas_estimator"]
    diagnostic = {
        "route_id": "foreign" if diagnostic_kind == "foreign_route" else "first",
        "phase": "estimate",
        "diagnostic": exception_diagnostic(
            RpcError(
                "rpc_error",
                diagnostic={
                    "method": "eth_gasPrice"
                    if diagnostic_kind == "wrong_method"
                    else "eth_estimateGas",
                    "provider_error": {
                        "code": 3,
                        "message": "rate limited"
                        if diagnostic_kind == "non_revert"
                        else "execution reverted",
                    },
                },
            )
        ),
    }
    if diagnostic_kind == "previous":
        gas.diagnostics.append(diagnostic)

    def estimate(**_):
        if diagnostic_kind == "unstructured":
            gas.diagnostics.append({"route_id": "first", "detail": "execution reverted"})
        elif diagnostic_kind != "previous":
            gas.diagnostics.append(diagnostic)
        return None

    gas.estimate_prepared_route = estimate
    with pytest.raises(ValueError, match="requote_required:missing_gas_estimate") as failure:
        _refresh_route_step(entry, step, now=NOW, **kwargs)
    assert not _retryable_provider_error(failure.value)
    assert prepared == ["first"]


def test_exhausted_route_retry_checkpoint_defers_until_scheduled_quote_refresh(tmp_path):
    from evm_inventory.route_preparation import require_provider_retry_due
    from evm_inventory.stages import StageDeadlineExceeded

    entry, step, kwargs, _ = _fallback_case(preparation_failure=lambda _: _status_error())
    with Journal(tmp_path / "inventory.sqlite") as journal:
        result = execute_group_entries(
            [entry],
            journal=journal,
            max_route_loss_pct=Decimal("15"),
            preflight=lambda _: _refresh_route_step(entry, step, now=NOW, **kwargs),
            submit_step=lambda *_: pytest.fail("must not sign a failed route"),
        )
        assert result["deferred"] == 1 and result["manual_review"] == 0
        position = journal.connection.execute("SELECT id FROM route_positions").fetchone()
        retry = journal.checkpoint(position["id"], "automatic_retry")
        assert retry["reason"] == "unsigned_route_preparation_exhausted"
        assert retry["retry_at"] == NOW.timestamp() + 240
        with pytest.raises(StageDeadlineExceeded, match="provider_retry_scheduled"):
            require_provider_retry_due(journal, position["id"], now_seconds=NOW.timestamp() + 239)
        require_provider_retry_due(journal, position["id"], now_seconds=NOW.timestamp() + 240)


@pytest.mark.parametrize(
    "context_change",
    [{"source_hash": "0x" + "a" * 64}, {"step_key": "bridge:foreign:0"}, {"target": {}}],
)
def test_partial_retry_requires_valid_saved_route_source_context(tmp_path, context_change):
    with Journal(tmp_path / "inventory.sqlite") as journal:
        group, _, completed = _partial_retry_group(journal)
        context = journal.checkpoint(completed["id"], "route_deposit")
        journal.save_checkpoint(completed["id"], "route_deposit", {**context, **context_change})
        assert not journal.reactivate_unsigned_retry_group(group["id"], {"ezeth", "ohno"})


def test_metadata_eth_call_revert_does_not_trigger_route_simulation_fallback():
    from evm_inventory.diagnostics import exception_diagnostic
    from evm_inventory.rpc import RpcError

    entry, step, kwargs, prepared = _fallback_case()
    gas = kwargs["gas_estimator"]

    def estimate(**_):
        error = RpcError(
            "rpc_error",
            diagnostic={
                "method": "eth_call",
                "params": [{"to": ASSET_A, "data": "0xdd62ed3e"}, "latest"],
                "provider_error": {"code": 3, "message": "execution reverted"},
            },
        )
        gas.diagnostics.append(
            {"route_id": "first", "phase": "estimate", "diagnostic": exception_diagnostic(error)}
        )
        return None

    gas.estimate_prepared_route = estimate
    with pytest.raises(ValueError, match="requote_required:missing_gas_estimate"):
        _refresh_route_step(entry, step, now=NOW, **kwargs)
    assert prepared == ["first"]


def test_refresh_replaces_real_estimator_structured_transaction_revert():
    from test_planner_gas import Rpc

    from evm_inventory.lifi import LifiPriceEvidence
    from evm_inventory.planner_gas import PlannerGasEstimator
    from evm_inventory.rpc import RpcError

    entry, step, kwargs, prepared = _fallback_case()

    class RevertingFirstRpc(Rpc):
        estimates = 0

        def call(self, url, method, params):
            if method == "eth_estimateGas":
                self.estimates += 1
                if self.estimates == 1:
                    raise RpcError(
                        "rpc_error",
                        diagnostic={
                            "method": method,
                            "params": params,
                            "provider_error": {
                                "code": 3,
                                "message": "execution reverted",
                                "data": "0xdeadbeef",
                            },
                        },
                    )
            if method == "eth_call" and params[0]["data"].startswith("0x70a08231"):
                return "0x" + f"{1000000:064x}"
            return super().call(url, method, params)

    rpc = RevertingFirstRpc()
    kwargs["gas_estimator"] = PlannerGasEstimator(
        rpc,
        {10: "rpc"},
        SimpleNamespace(
            token_price=lambda asset: LifiPriceEvidence(asset, "2000", NOW.isoformat())
        ),
        retry_transient_errors=True,
    )
    candidate, fresh = _refresh_route_step(entry, step, now=NOW, **kwargs)
    assert candidate.is_valid and fresh["route"]["id"] == "second"
    assert prepared == ["first", "second"] and rpc.estimates == 2
    assert fresh["_prepared_request"].max_total_fee_cap_wei > 0
    assert any("estimate_gas_rpc_error:" in str(d) for d in kwargs["gas_estimator"].diagnostics)


@pytest.mark.parametrize("tx_hash", [None, "0x" + "c" * 64])
def test_execution_freezes_saved_preparation_for_every_asset_intent(tmp_path, tx_hash):
    from evm_inventory.stages import StageDeadlineExceeded

    entry, step, kwargs, _ = _fallback_case()
    _, fresh_step = _refresh_route_step(entry, step, now=NOW, **kwargs)
    saved = {**entry, "steps": [fresh_step], "retained_preparation": "original"}
    submitted = []
    with Journal(tmp_path / "inventory.sqlite") as journal:
        group = journal.get_or_create_group(
            group_key=entry["group"]["key"],
            wallet=WALLET,
            source_chain_id=10,
            source_usd="100",
            loss_budget_pct="15",
        )
        p = journal.get_or_create_position(
            position_key=_position_key(entry),
            wallet=WALLET,
            group_id=group["id"],
            source_asset_id=ASSET_A,
            source_amount_raw=entry["raw_balance"],
        )
        journal.save_checkpoint(p["id"], "prepared", saved)
        intent = journal.record_step_intent(
            position_id=p["id"],
            step_key="bridge:first:0",
            nonce=1,
            calldata_digest="a" * 64,
            signed_payload_digest="b" * 64,
            tx_hash=tx_hash,
        )

        def submit(e, _):
            submitted.append(e)
            raise StageDeadlineExceeded("replay_pending")

        result = execute_group_entries(
            [entry],
            journal=journal,
            max_route_loss_pct=Decimal("15"),
            preflight=lambda _: pytest.fail("must retain preparation after any asset intent"),
            submit_step=submit,
        )
        assert result.get("deferred", result.get("pending")) == 1
        assert submitted[0]["retained_preparation"] == "original"
        assert journal.checkpoint(p["id"], "prepared") == saved
        assert journal.latest_step(position_id=p["id"], step_key="bridge:first:0") == intent


def test_live_groups_never_requote_or_reprepare_a_null_hash_asset_intent(tmp_path, monkeypatch):
    from contextlib import nullcontext

    import evm_inventory.route_execution as module
    from evm_inventory.stages import StageDeadlineExceeded

    entry, step, kwargs, _ = _fallback_case()
    _, fresh_step = _refresh_route_step(entry, step, now=NOW, **kwargs)
    saved = {**entry, "steps": [fresh_step], "retained_preparation": "original"}
    db_path = tmp_path / "inventory.sqlite"
    with Journal(db_path) as journal:
        group = journal.get_or_create_group(
            group_key=entry["group"]["key"],
            wallet=WALLET,
            source_chain_id=10,
            source_usd="100",
            loss_budget_pct="15",
        )
        p = journal.get_or_create_position(
            position_key=_position_key(entry),
            wallet=WALLET,
            group_id=group["id"],
            source_asset_id=ASSET_A,
            source_amount_raw=entry["raw_balance"],
        )
        journal.save_checkpoint(p["id"], "prepared", saved)
        intent = journal.record_step_intent(
            position_id=p["id"],
            step_key="bridge:first:0",
            nonce=1,
            calldata_digest="a" * 64,
            signed_payload_digest="b" * 64,
            tx_hash=None,
        )
    calls = []
    kwargs["jumper"].routes = lambda _: pytest.fail("must not request routes for a durable intent")
    kwargs["jumper"].step_transaction = lambda _: pytest.fail("must not reprepare a durable intent")
    monkeypatch.setattr(module, "Transport", lambda **_: SimpleNamespace(close=lambda: None))
    monkeypatch.setattr(module, "ExecutionRpc", lambda _: kwargs["gas_estimator"].rpc)
    monkeypatch.setattr(
        module,
        "WalletLifiClients",
        lambda *_, **kw: SimpleNamespace(for_wallet=lambda _: kwargs["jumper"], close=lambda: None),
    )
    monkeypatch.setattr(module, "_bitget_client", lambda: kwargs["bitget"])
    monkeypatch.setattr(
        module,
        "_DepositBackground",
        lambda **_: nullcontext(SimpleNamespace(raise_if_failed=lambda: None, finish=lambda: 0)),
    )

    def replay(e, s, **_):
        calls.append(e["retained_preparation"])
        assert s["route"]["id"] == "first"
        raise StageDeadlineExceeded("replay_pending")

    monkeypatch.setattr(module, "_submit_live_step", replay)
    result = module._execute_live_groups(
        [entry],
        wallets={WALLET: kwargs["wallet"]},
        rpc_urls={10: "rpc"},
        journal_path=db_path,
        max_route_loss_pct=Decimal("15"),
        now_ms=lambda: int(NOW.timestamp() * 1000),
        delay_min_seconds=0,
        delay_max_seconds=0,
        sleep=lambda _: None,
        rng=None,
    )
    assert result["deferred"] == 1 and calls == ["original"]
    with Journal(db_path) as journal:
        assert journal.checkpoint(p["id"], "prepared") == saved
        assert journal.latest_step(position_id=p["id"], step_key="bridge:first:0") == intent


def test_submit_replays_null_hash_intent_without_final_route_refresh(tmp_path, monkeypatch):
    import evm_inventory.route_execution as module
    from evm_inventory.stages import Deadline, StageDeadlineExceeded, deadline_scope

    entry, step, kwargs, _ = _fallback_case()
    _, fresh_step = _refresh_route_step(entry, step, now=NOW, **kwargs)
    monkeypatch.setattr(
        module,
        "_refresh_route_step",
        lambda *_a, **_kw: pytest.fail("intent replay must not refresh exact evidence"),
    )

    def replay(**kw):
        assert kw["prepared_request"] == fresh_step["_prepared_request"]
        raise StageDeadlineExceeded("replay_pending")

    monkeypatch.setattr(module, "_execute_entry", replay)
    with Journal(tmp_path / "inventory.sqlite") as journal:
        p = journal.get_or_create_position(position_key="replay", wallet=WALLET)
        journal.record_step_intent(
            position_id=p["id"],
            step_key="bridge:first:0",
            nonce=1,
            calldata_digest="a" * 64,
            signed_payload_digest="b" * 64,
            tx_hash=None,
        )
        with (
            deadline_scope(Deadline(30)),
            pytest.raises(StageDeadlineExceeded, match="replay_pending"),
        ):
            module._submit_live_step(
                {**entry, "_journal_position_id": p["id"]},
                fresh_step,
                wallet=kwargs["wallet"],
                rpc=kwargs["gas_estimator"].rpc,
                rpc_urls={10: "rpc"},
                jumper=kwargs["jumper"],
                bitget=kwargs["bitget"],
                journal=journal,
                now_ms=lambda: int(NOW.timestamp() * 1000),
            )


@pytest.mark.parametrize("phase", ["final_refresh", "after_approval"])
def test_submit_unsigned_simulation_revert_schedules_full_fresh_preflight_retry(
    tmp_path,
    monkeypatch,
    phase,
):
    import evm_inventory.route_execution as module
    from evm_inventory.diagnostics import exception_diagnostic
    from evm_inventory.route_preparation import require_provider_retry_due
    from evm_inventory.rpc import RpcError
    from evm_inventory.stages import Deadline, StageDeadlineExceeded, deadline_scope

    entry, step, kwargs, _ = _fallback_case()
    _, fresh_step = _refresh_route_step(entry, step, now=NOW, **kwargs)
    pending = replace(
        fresh_step["_prepared_request"],
        fee_quote_fallback_reason="pending_approval",
        max_total_fee_cap_wei=50000,
    )
    fresh_step["_prepared_request"] = pending
    fresh_step["_prepared_requests"] = (pending,)
    phases = []
    gas = kwargs["gas_estimator"]
    gas.prepared_fee_caps = {"first": (pending,)}

    def reverted():
        return RpcError(
            "rpc_error",
            diagnostic={
                "method": "eth_estimateGas",
                "provider_error": {
                    "code": 3,
                    "message": "execution reverted",
                    "data": "0xdeadbeef",
                },
            },
        )

    if phase == "final_refresh":

        def estimate(*, route, **_):
            gas.diagnostics.append(
                {
                    "route_id": route.route_id,
                    "phase": "estimate",
                    "diagnostic": exception_diagnostic(reverted()),
                }
            )
            return None

        gas.estimate_prepared_route = estimate
    else:

        def call(url, method, params):
            if method == "eth_estimateGas":
                phases.append("exact_simulation")
                raise reverted()
            return "0x" + f"{1000000:064x}"

        gas.rpc.call = call
    monkeypatch.setattr(module, "PlannerGasEstimator", lambda *a, **kw: gas)
    monkeypatch.setattr(module, "recover_durable_broadcast", lambda *a, **kw: None)
    monkeypatch.setattr(module, "_native_balance", lambda *a, **kw: 1000000)
    monkeypatch.setattr(module, "sign_transaction", lambda *a, **kw: pytest.fail("must not sign"))
    approval_hash = "0x" + "c" * 64

    def approve(e, request, **kw):
        phases.append("approval_confirmed")
        kw["journal"].record_step_intent(
            position_id=kw["position_id"],
            step_key="approval:bridge:first:0",
            nonce=0,
            calldata_digest="a" * 64,
            signed_payload_digest="b" * 64,
            tx_hash=approval_hash,
        )
        return approval_hash

    monkeypatch.setattr(module, "_approve_if_needed", approve)
    fresh = {**entry, "steps": [fresh_step], "quoted_at": int(NOW.timestamp() * 1000)}
    with Journal(tmp_path / "inventory.sqlite") as journal, deadline_scope(Deadline(30)):

        def submit(e, s):
            return module._submit_live_step(
                e,
                s,
                wallet=kwargs["wallet"],
                rpc=gas.rpc,
                rpc_urls={10: "rpc"},
                jumper=kwargs["jumper"],
                bitget=kwargs["bitget"],
                journal=journal,
                now_ms=lambda: int(NOW.timestamp() * 1000),
            )

        result = execute_group_entries(
            [entry],
            journal=journal,
            max_route_loss_pct=Decimal("15"),
            preflight=lambda _: fresh,
            submit_step=submit,
        )
        p = journal.connection.execute("SELECT id FROM route_positions").fetchone()
        assert (
            result.get("deferred", result.get("pending")) == 1 and result["manual_review"] == 0
        ), (result, journal.checkpoint(p["id"], "execution_failure"))
        retry = journal.checkpoint(p["id"], "automatic_retry")
        assert retry["retry_at"] == NOW.timestamp() + 240
        assert retry["reason"] == "unsigned_route_preparation_exhausted"
        assert not journal.connection.execute(
            "SELECT 1 FROM route_steps WHERE step_key NOT LIKE 'approval:%'"
        ).fetchone()
        assert journal.checkpoint(p["id"], "execution_failure")["phase"] == "submit"
        if phase == "after_approval":
            assert phases == ["approval_confirmed", "exact_simulation"]
            assert (
                journal.latest_step(position_id=p["id"], step_key="approval:bridge:first:0")[
                    "tx_hash"
                ]
                == approval_hash
            )
        with pytest.raises(StageDeadlineExceeded, match="provider_retry_scheduled"):
            require_provider_retry_due(journal, p["id"], now_seconds=retry["retry_at"] - 1)
        require_provider_retry_due(journal, p["id"], now_seconds=retry["retry_at"])
        assert journal.group(result["group_id"])["state"] == "active"
        # The stored unsigned preparation must be regenerated after the provider window.
        fresh_calls = []
        result = execute_group_entries(
            [entry],
            journal=journal,
            max_route_loss_pct=Decimal("15"),
            preflight=lambda e: fresh_calls.append(e) or e,
            submit_step=lambda *_: {"state": "completed", "realized_loss_usd": "1"},
        )
        assert result["completed"] == 1 and len(fresh_calls) == 1


@pytest.mark.parametrize("prepared", [False, True])
def test_asset_intent_without_hash_never_reprices_or_signs_again(tmp_path, monkeypatch, prepared):
    import evm_inventory.route_execution as module

    entry = {
        **_entry(ASSET_A, status="route_ready"),
        "_journal_step_key": "bridge:first:0",
        "route": {"step": {"id": "first"}},
    }
    request = TransactionRequest(10, DEPOSIT, "0x", 0, 21000, 1)
    monkeypatch.setattr(
        module,
        "FeePlanner",
        lambda _: pytest.fail("a frozen NULL-hash intent must never be repriced"),
    )
    monkeypatch.setattr(
        module,
        "sign_transaction",
        lambda *_a, **_kw: pytest.fail("a frozen NULL-hash intent must never be signed again"),
    )
    with Journal(tmp_path / "inventory.sqlite") as journal:
        p = journal.get_or_create_position(position_key="missing-hash", wallet=WALLET)
        saved = journal.record_step_intent(
            position_id=p["id"],
            step_key="bridge:first:0",
            nonce=1,
            calldata_digest="a" * 64,
            signed_payload_digest="b" * 64,
            tx_hash=None,
        )
        with pytest.raises(ValueError, match="durable_asset_intent_hash_missing"):
            module._execute_entry(
                entry=entry,
                wallet=SimpleNamespace(public_address=WALLET),
                rpc=object(),
                jumper=SimpleNamespace(
                    step_transaction=lambda _: pytest.fail(
                        "a frozen intent must never be prepared again"
                    )
                ),
                rpc_urls={10: "rpc"},
                journal=journal,
                position_id=p["id"],
                prepared_request=request if prepared else None,
            )
        assert journal.latest_step(position_id=p["id"], step_key="bridge:first:0") == saved


def test_asset_intent_without_saved_preparation_cannot_requote(tmp_path):
    entry = {**_entry(ASSET_A, status="route_ready"), "automatic_requote": True}
    with Journal(tmp_path / "inventory.sqlite") as journal:
        group = journal.get_or_create_group(
            group_key=entry["group"]["key"],
            wallet=WALLET,
            source_chain_id=10,
            source_usd="100",
            loss_budget_pct="15",
        )
        p = journal.get_or_create_position(
            position_key=_position_key(entry),
            wallet=WALLET,
            group_id=group["id"],
            source_asset_id=ASSET_A,
            source_amount_raw=entry["raw_balance"],
        )
        intent = journal.record_step_intent(
            position_id=p["id"],
            step_key="bridge:first:0",
            nonce=1,
            calldata_digest="a" * 64,
            signed_payload_digest="b" * 64,
            tx_hash=None,
        )
        result = execute_group_entries(
            [entry],
            journal=journal,
            max_route_loss_pct=Decimal("15"),
            preflight=lambda _: pytest.fail("must not invent new preparation for an asset intent"),
            submit_step=lambda *_: pytest.fail("must not sign an asset intent without preparation"),
        )
        assert result["manual_review"] == 1
        assert (
            journal.position(p["id"])["reason"]
            == "requote_required:durable_intent_preparation_missing"
        )
        assert journal.latest_step(position_id=p["id"], step_key="bridge:first:0") == intent


@pytest.mark.parametrize("metadata_failure", ["rpc", "transport"])
def test_real_estimator_allowance_read_failure_does_not_retry_handled_simulation_context(
    metadata_failure,
):
    from evm_inventory.lifi import LifiPriceEvidence
    from evm_inventory.planner_gas import PlannerGasEstimator
    from evm_inventory.rpc import RpcError
    from evm_inventory.transport import RequestError

    spender = "0x" + "c" * 40

    def options(request, first, second):
        return tuple(
            _quoted_route(
                request,
                route_id=route.route_id,
                output_amount=999999,
                gas_amount=1,
                approval_address=spender,
            )
            for route in (first, second)
        )

    entry, step, kwargs, prepared = _fallback_case(options=options)
    estimates = []
    metadata_error = (
        RpcError(
            "rpc_error",
            diagnostic={
                "method": "eth_call",
                "provider_error": {"code": -32000, "message": "metadata access denied"},
            },
        )
        if metadata_failure == "rpc"
        else RequestError("network_error")
    )

    class AllowanceReadFailureRpc:
        def call(self, url, method, params):
            if method == "eth_estimateGas":
                estimates.append(params)
                raise RpcError(
                    "rpc_error",
                    diagnostic={
                        "method": method,
                        "params": params,
                        "provider_error": {
                            "code": 3,
                            "message": "execution reverted",
                            "data": "0x7939f424",
                        },
                    },
                )
            if method == "eth_call" and params[0]["data"].startswith("0xdd62ed3e"):
                raise metadata_error
            if method == "eth_call" and params[0]["data"].startswith("0x70a08231"):
                return "0x" + f"{1000000:064x}"
            pytest.fail(f"unexpected RPC operation: {method}")

    gas = PlannerGasEstimator(
        AllowanceReadFailureRpc(),
        {10: "rpc"},
        SimpleNamespace(
            token_price=lambda asset: LifiPriceEvidence(asset, "2000", NOW.isoformat())
        ),
        retry_transient_errors=True,
    )
    kwargs["gas_estimator"] = gas
    expected_type = ValueError if metadata_failure == "rpc" else RequestError
    with pytest.raises(expected_type) as failure:
        _refresh_route_step(entry, step, now=NOW, **kwargs)
    if metadata_failure == "rpc":
        assert str(failure.value) == "requote_required:missing_gas_estimate"
        assert not _retryable_provider_error(failure.value)
    else:
        assert failure.value is metadata_error and _retryable_provider_error(failure.value)
    assert prepared == ["first"] and len(estimates) == 1
    diagnostics = gas.diagnostics[0]["diagnostic"]["exceptions"]
    assert diagnostics[0]["chain_link"] == "root"
    assert diagnostics[1]["chain_link"] == "context"
    assert diagnostics[-1]["provider"]["method"] == "eth_estimateGas"
