from dataclasses import replace
from datetime import timedelta
from decimal import Decimal
from types import SimpleNamespace

import httpx
import pytest
from test_live_plan import NOW, _quoted_route
from test_route_execution import ASSET_A, DEPOSIT, WALLET, _entry

from evm_inventory.bitget_catalog import BitgetDepositTarget
from evm_inventory.journal import Journal
from evm_inventory.lifi import LifiRouteRequest, TransactionRequest
from evm_inventory.live_plan import _route_data
from evm_inventory.models import AssetIdentity
from evm_inventory.route_execution import (
    _execution_reason,
    _position_key,
    _preflight_live_entry,
    _refresh_route_step,
    _retryable_provider_error,
    execute_group_entries,
)
from evm_inventory.valuation import FeeQuote, QuotePrice

STEP_URL = "https://li.quest/v1/advanced/stepTransaction"
LEGACY_REASON = (
    f"Client error '422 Unprocessable Entity' for url '{STEP_URL}'\n"
    "For more information check: https://developer.mozilla.org/en-US/docs/Web/HTTP/Status/422"
)


def _status_error(*, method="POST", url=STEP_URL, status=422):
    request = httpx.Request(method, url)
    response = httpx.Response(status, request=request, json={
        "code": "PREPARATION_REJECTED", "message": "cannot prepare current route",
        "detail": "Bearer abcdefgh-secret", "nested": {"provider": "https://secret.example/rpc"},
    })
    with pytest.raises(httpx.HTTPStatusError) as failure:
        response.raise_for_status()
    return failure.value


def test_exact_lifi_unsigned_422_yields_with_redacted_full_diagnostic(tmp_path):
    entry = {**_entry(ASSET_A, status="route_ready"), "automatic_requote": True}
    error = _status_error()

    def rejected(_):
        raise error

    with Journal(tmp_path / "inventory.sqlite") as journal:
        result = execute_group_entries(
            [entry], journal=journal, max_route_loss_pct=Decimal("15"), preflight=rejected,
            submit_step=lambda *_: pytest.fail("must not sign or send rejected preparation"),
        )
        assert result.get("deferred") == 1
        assert result["manual_review"] == 0
        assert journal.group(result["group_id"])["state"] == "active"
        position = journal.connection.execute(
            "SELECT * FROM route_positions WHERE position_key=?", (_position_key(entry),),
        ).fetchone()
        retry = journal.checkpoint(position["id"], "automatic_retry")
        assert retry["reason"] == "route_preparation_rejected"
        diagnostic = retry["diagnostic"]["exceptions"][0]
        assert diagnostic["http_status"] == 422
        assert diagnostic["http_method"] == "POST"
        assert "PREPARATION_REJECTED" in diagnostic["http_response_body"]
        assert "cannot prepare current route" in diagnostic["http_response_body"]
        assert "abcdefgh-secret" not in str(retry)
        assert "secret.example" not in str(retry)
        assert not journal.connection.execute("SELECT 1 FROM route_steps").fetchone()
    assert _retryable_provider_error(error)
    assert _execution_reason(error) == "route_preparation_rejected"


@pytest.mark.parametrize("method,url,status", [
    ("GET", STEP_URL, 422),
    ("POST", "https://li.quest/v1/advanced/routes", 422),
    ("POST", STEP_URL + "/", 422),
    ("POST", STEP_URL + "?other=1", 422),
    ("POST", "https://other.example/v1/advanced/stepTransaction", 422),
    ("POST", STEP_URL, 400),
    ("POST", STEP_URL, 403),
])
def test_unsigned_preparation_retry_requires_actual_exact_lifi_post_422(method, url, status):
    error = _status_error(method=method, url=url, status=status)
    assert not _retryable_provider_error(error)
    assert _execution_reason(error) != "route_preparation_rejected"
    assert not _retryable_provider_error(ValueError(LEGACY_REASON))


@pytest.mark.parametrize("reason", ["route_preparation_rejected", LEGACY_REASON])
@pytest.mark.parametrize("intent,tx_hash", [
    (None, None), ("approval:router", None), ("approval:router", "0x" + "c" * 64),
    ("bridge:0", None), ("bridge:0", "0x" + "c" * 64),
])
def test_preparation_recovery_requires_exact_unsigned_members_and_preserves_approvals(
    tmp_path, reason, intent, tx_hash,
):
    with Journal(tmp_path / "inventory.sqlite") as journal:
        group = journal.get_or_create_group(
            group_key="g", wallet=WALLET, source_chain_id=10,
            source_usd="100", loss_budget_pct="15",
        )
        positions = [journal.get_or_create_position(
            position_key=key, wallet=WALLET, group_id=group["id"],
        ) for key in ("p", "q")]
        saved_intent = None
        if intent:
            saved_intent = journal.record_step_intent(
                position_id=positions[0]["id"], step_key=intent, nonce=1,
                calldata_digest="a" * 64, signed_payload_digest="b" * 64, tx_hash=tx_hash,
            )
        journal.account_position_loss(positions[0]["id"], loss_usd="2")
        for position, state in zip(positions, ("manual_review", "manual_review_group_halted")):
            journal.record_position_state(position["id"], state, reason=reason)
        journal.record_group_state(group["id"], "manual_review_group_halted", reason=reason)
        assert not journal.reactivate_unsigned_retry_group(group["id"], {"p"})
        assert not journal.reactivate_unsigned_retry_group(group["id"], {"p", "q", "other"})
        recovered = journal.reactivate_unsigned_retry_group(group["id"], {"p", "q"})
        assert recovered is (intent != "bridge:0")
        assert journal.group(group["id"])["source_usd"] == "100"
        assert journal.group(group["id"])["loss_budget_pct"] == "15"
        assert journal.group(group["id"])["realized_loss_usd"] == "2"
        assert journal.position(positions[0]["id"])["accounted_loss_usd"] == "2"
        if saved_intent:
            assert journal.latest_step(
                position_id=positions[0]["id"], step_key=intent,
            ) == saved_intent
        if recovered:
            assert journal.group(group["id"])["state"] == "active"
            assert all(journal.position(p["id"])["state"] == "planned" for p in positions)
            assert all(journal.checkpoint(p["id"], "automatic_retry")["reason"]
                       == reason for p in positions)


@pytest.mark.parametrize("reason", [
    "execution_error:HTTPStatusError", "insufficient_source_gas",
    LEGACY_REASON.replace("li.quest", "wrong.example"),
    LEGACY_REASON.replace("stepTransaction", "routes"),
    LEGACY_REASON.replace("422 Unprocessable Entity", "403 Forbidden"),
])
def test_unsigned_preparation_recovery_keeps_other_reasons_locked(tmp_path, reason):
    with Journal(tmp_path / "inventory.sqlite") as journal:
        group = journal.get_or_create_group(group_key="g", wallet=WALLET, source_chain_id=10,
                                           source_usd="100", loss_budget_pct="15")
        position = journal.get_or_create_position(position_key="p", wallet=WALLET,
                                                  group_id=group["id"])
        journal.record_position_state(position["id"], "manual_review", reason=reason)
        journal.record_group_state(group["id"], "manual_review_group_halted", reason=reason)
        assert not journal.reactivate_unsigned_retry_group(group["id"], {"p"})


@pytest.mark.parametrize("member_state,member_reason", [
    ("planned", "route_preparation_rejected"),
    ("manual_review", "requote_required:stale_price"),
    ("manual_review", "unrelated_failure"),
])
def test_preparation_recovery_requires_matching_manual_state_and_reason(
    tmp_path, member_state, member_reason,
):
    with Journal(tmp_path / "inventory.sqlite") as journal:
        group = journal.get_or_create_group(group_key="g", wallet=WALLET, source_chain_id=10,
                                           source_usd="100", loss_budget_pct="15")
        position = journal.get_or_create_position(position_key="p", wallet=WALLET,
                                                  group_id=group["id"])
        journal.record_position_state(position["id"], member_state, reason=member_reason)
        journal.record_group_state(group["id"], "manual_review_group_halted",
                                   reason="route_preparation_rejected")
        assert not journal.reactivate_unsigned_retry_group(group["id"], {"p"})


@pytest.mark.parametrize("automatic", [False, True])
def test_collection_resumes_legacy_preparation_failure_only_for_automatic_entries(
    tmp_path, automatic,
):
    from evm_inventory.collection import _resume_unsigned_retry_groups

    entry = {**_entry(ASSET_A, status="route_ready"), "automatic_requote": automatic}
    path = tmp_path / "inventory.sqlite"
    with Journal(path) as journal:
        group = journal.get_or_create_group(group_key=entry["group"]["key"], wallet=WALLET,
            source_chain_id=10, source_usd="100", loss_budget_pct="15")
        position = journal.get_or_create_position(position_key=_position_key(entry), wallet=WALLET,
                                                  group_id=group["id"])
        journal.record_position_state(position["id"], "manual_review", reason=LEGACY_REASON)
        journal.record_group_state(group["id"], "manual_review_group_halted", reason=LEGACY_REASON)
    _resume_unsigned_retry_groups([entry], path)
    with Journal(path) as journal:
        assert journal.group(group["id"])["state"] == (
            "active" if automatic else "manual_review_group_halted"
        )


def _native_case(*, age_ms, live_hint=100, fee=20, minimum=1, evidence_age_ms=None):
    native = AssetIdentity(10, "native", 18)
    old = _quoted_route(
        LifiRouteRequest(10, 8453, "0x" + "0" * 40, "0x" + "e" * 40,
                         "1000", WALLET, DEPOSIT),
        route_id="old", output_amount=1000, gas_amount=1,
    )
    if evidence_age_ms is not None:
        old = replace(old, evidence=replace(
            old.evidence,
            quote_timestamp=(NOW - timedelta(milliseconds=evidence_age_ms)).isoformat(),
        ))
    step = {"kind": "bridge", "route": _route_data(old)}
    entry = {
        **_entry("native", status="route_ready", steps=[step]),
        "decimals": 18, "raw_balance": "1000", "automatic_requote": True,
        "quoted_at": int(NOW.timestamp() * 1000) - age_ms,
        "reservations": {"source_native_gas_cap_raw": "1", "future_leg": "preserved"},
    }
    entry["target"]["minimum_raw"] = str(minimum)
    quoted, prepared = [], []

    def routes(request):
        quoted.append(int(request.from_amount))
        return (_quoted_route(request, route_id="fresh", output_amount=int(request.from_amount),
                              gas_amount=live_hint),)

    def prepare(raw_step):
        prepared.append(int(raw_step["action"]["fromAmount"]))
        return TransactionRequest(10, DEPOSIT, "0x", prepared[-1], 21_000, 1)

    rpc = SimpleNamespace(call=lambda *_: hex(1000))
    gas = SimpleNamespace(
        rpc=rpc, rpc_urls={10: "rpc"},
        estimate_prepared_route=lambda **_: (FeeQuote(fee, QuotePrice(native, "2000", NOW)),),
    )
    kwargs = dict(
        wallet=SimpleNamespace(public_address=WALLET, bitget_deposit_address=DEPOSIT),
        jumper=SimpleNamespace(routes=routes, step_transaction=prepare), gas_estimator=gas,
        bitget=SimpleNamespace(revalidate_deposit_target=lambda *_a, **_kw:
            BitgetDepositTarget("USDC", 8453, "0x" + "e" * 40, minimum, "Base", 6)),
    )
    return entry, step, rpc, kwargs, quoted, prepared


@pytest.mark.parametrize("evidence_age_ms,age_ms", [(None, 900_001), (900_001, 0)])
def test_stale_native_cap_requires_fresh_hint_probe_before_preparation(evidence_age_ms, age_ms):
    entry, step, _, kwargs, quoted, prepared = _native_case(
        age_ms=age_ms, evidence_age_ms=evidence_age_ms,
    )
    candidate, fresh = _refresh_route_step(entry, step, now=NOW, **kwargs)
    assert candidate.is_valid
    assert len(quoted) <= 4
    assert prepared[0] <= 1000 - 100 * 3
    assert int(fresh["route"]["from_amount"]) >= 1000 - 20 * 4
    assert 1000 - int(fresh["route"]["from_amount"]) >= 20 * 3


def test_fresh_native_cap_keeps_current_rpc_repricing_behavior():
    entry, step, _, kwargs, quoted, prepared = _native_case(age_ms=900_000)
    candidate, fresh = _refresh_route_step(entry, step, now=NOW, **kwargs)
    assert candidate.is_valid
    assert prepared[0] == quoted[0] == 996
    assert int(fresh["route"]["from_amount"]) == 928
    assert len(quoted) == 2


@pytest.mark.parametrize("minimum", [1, 935])
def test_oversized_live_hint_uses_rpc_cap_and_preserves_exact_minimum_fallback(minimum):
    entry, step, _, kwargs, quoted, prepared = _native_case(
        age_ms=900_001, live_hint=10_000, minimum=minimum,
    )
    candidate, fresh = _refresh_route_step(entry, step, now=NOW, **kwargs)
    assert candidate.is_valid
    assert prepared[0] == 500
    assert int(fresh["route"]["from_amount"]) == (940 if minimum > 1 else 928)
    assert len(quoted) <= 4


def test_actual_rpc_fee_cap_decides_physical_native_shortage():
    entry, step, _, kwargs, _, prepared = _native_case(
        age_ms=900_001, live_hint=10_000, fee=334,
    )
    with pytest.raises(ValueError, match="^insufficient_source_gas$"):
        _refresh_route_step(entry, step, now=NOW, **kwargs)
    assert prepared == [500]


def test_fresh_native_preflight_updates_validated_cap_and_preserves_budget_and_future_legs():
    entry, _, rpc, kwargs, _, _ = _native_case(age_ms=900_001)
    original_group = dict(entry["group"])
    fresh = _preflight_live_entry(
        entry, rpc=rpc, rpc_urls={10: "rpc"}, now_ms=lambda: int(NOW.timestamp() * 1000), **kwargs,
    )
    assert fresh["reservations"]["source_native_gas_cap_raw"] == "20"
    assert fresh["reservations"]["future_leg"] == "preserved"
    assert fresh["group"] == original_group


@pytest.mark.parametrize("intent", [None, "approval:test", "bridge:test"])
def test_scheduled_provider_retry_preserves_intents_and_waits_until_due(tmp_path, intent):
    from evm_inventory.route_preparation import require_provider_retry_due
    from evm_inventory.stages import StageDeadlineExceeded

    with Journal(tmp_path / "inventory.sqlite") as journal:
        p = journal.get_or_create_position(position_key="retry", wallet=WALLET)
        retry = {"retry_at": 2000.0, "diagnostic": {"body": "unchanged"}}
        journal.save_checkpoint(p["id"], "automatic_retry", retry)
        if intent:
            journal.record_step_intent(position_id=p["id"], step_key=intent, nonce=1,
                calldata_digest="a" * 64, signed_payload_digest="b" * 64,
                tx_hash="0x" + "c" * 64)
        if intent == "bridge:test":
            require_provider_retry_due(journal, p["id"], now_seconds=1000.0)
        else:
            with pytest.raises(StageDeadlineExceeded, match="provider_retry_scheduled"):
                require_provider_retry_due(journal, p["id"], now_seconds=1000.0)
        require_provider_retry_due(journal, p["id"], now_seconds=2000.0)
        assert journal.checkpoint(p["id"], "automatic_retry") == retry
        if intent:
            assert journal.latest_step(position_id=p["id"], step_key=intent)["tx_hash"]


@pytest.mark.parametrize("retry_at", [None, True, "2000", float("nan"), float("inf"), 900.0])
def test_invalid_or_elapsed_provider_retry_does_not_wait(tmp_path, retry_at):
    from evm_inventory.route_preparation import require_provider_retry_due

    with Journal(tmp_path / "inventory.sqlite") as journal:
        p = journal.get_or_create_position(position_key="retry", wallet=WALLET)
        journal.save_checkpoint(p["id"], "automatic_retry", {"retry_at": retry_at})
        require_provider_retry_due(journal, p["id"], now_seconds=1000.0)


@pytest.mark.parametrize("now_seconds,automatic", [(1000.0, True), (2000.0, True), (1000.0, False)])
def test_live_preflight_waits_before_provider_and_rpc_calls(tmp_path, monkeypatch, now_seconds,
                                                          automatic):
    from contextlib import nullcontext

    import evm_inventory.route_execution as module
    from evm_inventory.stages import StageDeadlineExceeded

    e = {**_entry(ASSET_A, status="route_ready"), "automatic_requote": automatic}
    path = tmp_path / "inventory.sqlite"
    with Journal(path) as journal:
        p = journal.get_or_create_position(position_key=_position_key(e), wallet=WALLET)
        journal.save_checkpoint(p["id"], "automatic_retry", {"retry_at": 2000.0})
    calls = []
    monkeypatch.setattr(module.time, "time", lambda: now_seconds)
    monkeypatch.setattr(module, "Transport", lambda **_: SimpleNamespace(close=lambda: None))
    monkeypatch.setattr(module, "ExecutionRpc", lambda _: SimpleNamespace(
        call=lambda *_: pytest.fail("must not call RPC during the scheduled provider wait")))
    monkeypatch.setattr(module, "WalletLifiClients", lambda *_, **kw: SimpleNamespace(
        for_wallet=lambda _: object(), close=lambda: None))
    monkeypatch.setattr(module, "_bitget_client", lambda: object())
    monkeypatch.setattr(module, "_DepositBackground", lambda **_: nullcontext(
        SimpleNamespace(raise_if_failed=lambda: None, finish=lambda: 0)))
    monkeypatch.setattr(module, "_preflight_live_entry",
                        lambda entry, **_: calls.append(entry) or e)

    def execute(entries, *, preflight, **_):
        if automatic and now_seconds < 2000:
            with pytest.raises(StageDeadlineExceeded, match="provider_retry_scheduled"):
                preflight(entries[0])
        else:
            assert preflight(entries[0]) == e
        return {"deferred": 1}

    monkeypatch.setattr(module, "execute_group_entries", execute)
    module._execute_live_groups([e], wallets={WALLET: SimpleNamespace(public_address=WALLET)},
        rpc_urls={10: "rpc"}, journal_path=path, max_route_loss_pct=Decimal("15"),
        now_ms=lambda: 0, delay_min_seconds=0, delay_max_seconds=0, sleep=lambda _: None, rng=None)
    assert len(calls) == (0 if automatic and now_seconds < 2000 else 1)
