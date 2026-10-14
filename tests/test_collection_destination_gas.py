"""Offline regressions for gas needed by routes arriving at the same wallet."""

from decimal import Decimal
from types import SimpleNamespace

import pytest

from evm_inventory.collection import drive_execution
from evm_inventory.journal import Journal
from evm_inventory.route_execution import _position_key, execute_group_entries

WALLET = "0x" + "a" * 40
EXCHANGE = "0x" + "d" * 40


def native(chain, *, wallet=WALLET, provider="lifi"):
    return {
        "wallet": wallet,
        "chain_id": chain,
        "asset_id": "native",
        "raw_balance": "1000",
        "status": "direct_deposit",
        "routing_provider": provider,
        "group": {"key": f"{wallet}:{chain}", "source_usd": "100", "max_route_loss_pct": "15"},
    }


def bridge(*, recipient=WALLET, wallet=WALLET, substep=False):
    return {
        **native(81457, wallet=wallet),
        "asset_id": "0xabc",
        "status": "route_ready",
        "steps": [{"kind": "bridge", "route": {"id": "bridge-route", "evidence": {
            "steps": [{"source": {"chain_id": 81457}, "destination": {"chain_id": 8453},
                       "recipient": recipient, "is_substep": substep}],
        }}}],
    }


def finish(journal, entry):
    p = journal.get_or_create_position(position_key=_position_key(entry), wallet=entry["wallet"])
    journal.record_position_state(p["id"], "completed")


def run(plan, path, *, dependency_plan=None, seconds=10):
    clock, rounds = [1000.0], []

    def execute(entries):
        rounds.append({(e["wallet"], e["chain_id"], e["asset_id"]):
                       bool(e.get("_destination_gas_held")) for e in entries})
        with Journal(path) as journal:
            for e in entries:
                if not e.get("_destination_gas_held"):
                    finish(journal, e)

    kwargs = {} if dependency_plan is None else {"dependency_plan": dependency_plan}
    result = drive_execution(plan, path, execute_round=execute, until=1000 + seconds,
                             clock=lambda: clock[0],
                             sleep=lambda s: clock.__setitem__(0, clock[0] + s), **kwargs)
    return result, rounds


@pytest.mark.parametrize("provider", ["lifi", "relay"])
def test_destination_native_waits_for_wallet_bridge_and_releases_after_completion(
    tmp_path, provider,
):
    n, b, unrelated = native(8453, provider=provider), bridge(), native(137)
    result, rounds = run({"entries": [n, b, unrelated]}, tmp_path / "inventory.sqlite")
    first = rounds[0]
    assert first.get((WALLET, 8453, "native"), True)
    assert first[(WALLET, 137, "native")] is False
    assert first[(WALLET, 81457, "0xabc")] is False
    assert rounds[1] == {(WALLET, 8453, "native"): False}
    assert result["status"] == "completed"
    assert n["group"] == {"key": f"{WALLET}:8453", "source_usd": "100", "max_route_loss_pct": "15"}


@pytest.mark.parametrize("case", ["exchange", "other_wallet", "substep", "completed"])
def test_destination_native_ignores_routes_without_an_unfinished_wallet_arrival(tmp_path, case):
    b = bridge(recipient=EXCHANGE) if case == "exchange" else bridge(substep=case == "substep")
    if case == "other_wallet":
        other = "0x" + "b" * 40
        b = bridge(wallet=other, recipient=other)
    path = tmp_path / "inventory.sqlite"
    if case == "completed":
        with Journal(path) as journal:
            finish(journal, b)
    result, rounds = run({"entries": [native(8453), b]}, path)
    assert rounds[0][(WALLET, 8453, "native")] is False
    assert result["status"] == "completed"


def test_destination_hold_observes_already_broadcast_native(tmp_path):
    n, b, path = native(8453), bridge(), tmp_path / "inventory.sqlite"
    with Journal(path) as journal:
        p = journal.get_or_create_position(position_key=_position_key(n), wallet=WALLET)
        journal.record_step_intent(position_id=p["id"], step_key="direct_deposit", nonce=9,
                                   calldata_digest="a" * 64, signed_payload_digest="b" * 64,
                                   tx_hash="0x" + "c" * 64)
    result, rounds = run({"entries": [n, b]}, path)
    assert rounds[0][(WALLET, 8453, "native")] is False
    assert result["status"] == "completed"


@pytest.mark.parametrize("checkpoint", [
    "prepared", "bridge_prepared", "route:bridge:bridge-route", "canonical_prepared",
])
@pytest.mark.parametrize("state", ["manual_review", "completed"])
def test_saved_wallet_continuation_holds_native_until_its_owner_completes(
    tmp_path, checkpoint, state,
):
    b, path = bridge(), tmp_path / "inventory.sqlite"
    advanced = {**b, "steps": []}
    with Journal(path) as journal:
        p = journal.get_or_create_position(position_key=_position_key(b), wallet=WALLET)
        journal.record_position_state(p["id"], state, reason="continuation_retry")
        child = {**native(8453), "asset_id": "0xdef", "steps": []}
        saved = b
        if checkpoint == "canonical_prepared":
            saved = child
        elif checkpoint.startswith("route:"):
            saved = {"continuation": child}
        journal.save_checkpoint(p["id"], checkpoint, saved)
    result, rounds = run({"entries": [native(8453), advanced]}, path, seconds=3)
    assert rounds == [{(WALLET, 8453, "native"): state != "completed"}]
    assert result["status"] == ("completed" if state == "completed" else "incomplete")


def test_saved_continuation_for_another_wallet_does_not_hold_native(tmp_path):
    b, path = {**bridge(), "steps": []}, tmp_path / "inventory.sqlite"
    with Journal(path) as journal:
        p = journal.get_or_create_position(position_key=_position_key(b), wallet=WALLET)
        journal.record_position_state(p["id"], "manual_review", reason="continuation_retry")
        journal.save_checkpoint(p["id"], "route:bridge:bridge-route", {
            "continuation": native(8453, wallet=EXCHANGE)})
    _, rounds = run({"entries": [native(8453), b]}, path)
    assert rounds == [{(WALLET, 8453, "native"): False}]


def test_known_route_excluded_from_ready_subset_still_holds_destination(tmp_path):
    n, b, path = native(8453), bridge(), tmp_path / "inventory.sqlite"
    _, rounds = run({"entries": [n]}, path, dependency_plan={"entries": [n, b]})
    assert rounds == [{(WALLET, 8453, "native"): True}]
    with Journal(path) as journal:
        finish(journal, b)
    result, rounds = run({"entries": [n]}, path, dependency_plan={"entries": [n, b]})
    assert rounds == [{(WALLET, 8453, "native"): False}]
    assert result["status"] == "completed"


def native_wallet_route(chain, destination):
    e = native(chain)
    return {**e, "status": "route_ready", "valuation": {"loss_usd": "1"}, "steps": [{
        "kind": "bridge", "route": {"id": f"native-{chain}", "evidence": {"steps": [{
            "source": {"chain_id": chain}, "destination": {"chain_id": destination},
            "recipient": WALLET, "is_substep": False,
        }]}}}]}


@pytest.mark.parametrize("known_hash", [None, "unrelated", "cycle_member"])
def test_reciprocal_native_routes_report_cycle_and_continue_unrelated_or_broadcast_work(
    tmp_path, known_hash,
):
    path, clock, observed = tmp_path / "inventory.sqlite", [1000.0], []
    a, b = native_wallet_route(8453, 10), native_wallet_route(10, 8453)
    unrelated = {**native(137), "valuation": {"loss_usd": "1"},
                 "steps": [{"kind": "direct_deposit"}]}
    broadcast = a if known_hash == "cycle_member" else unrelated
    if known_hash:
        with Journal(path) as journal:
            g = journal.get_or_create_group(
                group_key=broadcast["group"]["key"], wallet=WALLET,
                source_chain_id=broadcast["chain_id"], loss_budget_pct="15", source_usd="100")
            p = journal.get_or_create_position(
                position_key=_position_key(broadcast), wallet=WALLET, group_id=g["id"],
                source_asset_id="native", source_amount_raw=broadcast["raw_balance"])
            journal.record_position_state(p["id"], "direct_deposit_submitted")
            journal.record_step_intent(position_id=p["id"], step_key="direct_deposit", nonce=9,
                                       calldata_digest="a" * 64, signed_payload_digest="b" * 64,
                                       tx_hash="0x" + "c" * 64)

    def execute(entries):
        with Journal(path) as journal:
            for e in entries:
                def preflight(fresh):
                    assert not fresh.get("_destination_gas_held")
                    return fresh

                def submit(fresh, _step):
                    observed.append(fresh["chain_id"])
                    return {"state": "completed", "realized_loss_usd": "1"}

                execute_group_entries([e], journal=journal, max_route_loss_pct=Decimal("15"),
                                      preflight=preflight, submit_step=submit)
        return {}

    result = drive_execution(
        {"entries": [a, b, unrelated]}, path, execute_round=execute, until=1010,
        clock=lambda: clock[0], sleep=lambda s: clock.__setitem__(0, clock[0] + s),
    )
    assert 137 in observed
    if known_hash == "cycle_member":
        assert result["status"] == "completed" and result["reason"] is None
        assert sorted(observed) == [10, 137, 8453]
    else:
        assert observed == [137]
        assert result["status"] == "incomplete"
        assert result["reason"] == "native_gas_dependency_cycle"
        assert len(result["native_gas_dependency_cycles"]) == 1
        diagnostic = result["native_gas_dependency_cycles"][0]
        assert diagnostic["wallet"] == WALLET
        assert {m["position_key"]: (m["chain_id"], m["depends_on"])
                for m in diagnostic["members"]} == {
                    _position_key(a): (8453, [_position_key(b)]),
                    _position_key(b): (10, [_position_key(a)]),
                }
        with Journal(path) as journal:
            for e in (a, b):
                p = journal.connection.execute(
                    "SELECT * FROM route_positions WHERE position_key=?",
                    (_position_key(e),)).fetchone()
                assert p["state"] == "planned" and p["reason"] == "native_gas_dependency_cycle"
                assert journal.checkpoint(p["id"], "native_gas_dependency_cycle") == diagnostic
                g = journal.group(p["group_id"])
                assert g["source_usd"] == "100" and g["loss_budget_pct"] == "15"
                assert not journal.has_broadcast(p["id"])
    if known_hash:
        with Journal(path) as journal:
            p = journal.connection.execute("SELECT id FROM route_positions WHERE position_key=?",
                                           (_position_key(broadcast),)).fetchone()
            step = journal.latest_step(position_id=p["id"], step_key="direct_deposit")
            assert step["tx_hash"] == "0x" + "c" * 64
            assert journal.checkpoint(p["id"], "native_gas_dependency_cycle") is None


def test_cycle_diagnostic_correlates_owner_outside_execution_subset(tmp_path):
    path = tmp_path / "inventory.sqlite"
    a, b = native_wallet_route(8453, 10), native_wallet_route(10, 8453)

    def execute(entries):
        with Journal(path) as journal:
            return execute_group_entries(
                entries, journal=journal, max_route_loss_pct=Decimal("15"),
                preflight=lambda _: pytest.fail("cycle must preserve unsigned gas hold"),
                submit_step=lambda *_: pytest.fail("cycle must never sign"))

    result = drive_execution({"entries": [a]}, path, dependency_plan={"entries": [a, b]},
                             execute_round=execute, until=1010, clock=lambda: 1000,
                             sleep=lambda _: pytest.fail("blocked cycle must yield promptly"))
    assert result["reason"] == "native_gas_dependency_cycle"
    diagnostic = result["native_gas_dependency_cycles"][0]
    assert {m["position_key"] for m in diagnostic["members"]} == {
        _position_key(a), _position_key(b)}
    with Journal(path) as journal:
        p = journal.connection.execute("SELECT id FROM route_positions WHERE position_key=?",
                                       (_position_key(a),)).fetchone()
        assert journal.checkpoint(p["id"], "native_gas_dependency_cycle") == diagnostic
        assert journal.connection.execute("SELECT id FROM route_positions WHERE position_key=?",
                                          (_position_key(b),)).fetchone() is None


def test_reciprocal_networks_with_one_exchange_recipient_do_not_form_cycle(tmp_path):
    a, b = native_wallet_route(8453, 10), native_wallet_route(10, 8453)
    a["steps"][0]["route"]["evidence"]["steps"][0]["recipient"] = EXCHANGE
    result, rounds = run({"entries": [a, b]}, tmp_path / "inventory.sqlite")
    assert result["status"] == "completed"
    assert "native_gas_dependency_cycles" not in result
    assert rounds[0][(WALLET, 8453, "native")] is True
    assert rounds[0][(WALLET, 10, "native")] is False


@pytest.mark.parametrize("state", [
    "manual_review", "manual_review_after_swap", "manual_review_group_threshold_exceeded",
    "manual_review_group_halted",
])
def test_cycle_recorder_preserves_new_manual_failure_state_and_reason(tmp_path, state):
    from evm_inventory.collection import _record_native_gas_cycles

    path, a, b = tmp_path / "inventory.sqlite", native(8453), native(10)
    with Journal(path) as journal:
        p = journal.get_or_create_position(position_key=_position_key(a), wallet=WALLET)
        journal.record_position_state(p["id"], state, reason="real_group_failure")
    diagnostic = {"wallet": WALLET, "members": [
        {"position_key": _position_key(a), "chain_id": 8453, "depends_on": [_position_key(b)]},
        {"position_key": _position_key(b), "chain_id": 10, "depends_on": [_position_key(a)]},
    ]}
    _record_native_gas_cycles([diagnostic], {_position_key(a)}, path)
    with Journal(path) as journal:
        position = journal.position(p["id"])
        assert position["state"] == state and position["reason"] == "real_group_failure"
        assert journal.checkpoint(p["id"], "native_gas_dependency_cycle") is None


@pytest.mark.parametrize("hold", ["source", "destination"])
@pytest.mark.parametrize("existing_state", [None, "gas_deferred"])
def test_excluded_relay_hold_persists_exact_position_reason_without_changing_economics(
    tmp_path, hold, existing_state,
):
    n, path, clock = native(8453, provider="relay"), tmp_path / "inventory.sqlite", [1000.0]
    dependency = bridge() if hold == "destination" else {**native(8453), "asset_id": "0xdef"}
    if existing_state:
        with Journal(path) as journal:
            p = journal.get_or_create_position(position_key=_position_key(n), wallet=WALLET)
            journal.record_position_state(p["id"], existing_state, reason="previous_gas_wait")
    result = drive_execution(
        {"entries": [n]}, path, dependency_plan={"entries": [n, dependency]},
        execute_round=lambda _: pytest.fail("held Relay native must not reach its adapter"),
        until=1003, clock=lambda: clock[0],
        sleep=lambda s: clock.__setitem__(0, clock[0] + s),
    )
    reason = ("destination_gas_for_unsettled_routes" if hold == "destination"
              else "source_gas_for_unsettled_tokens")
    assert result["positions"][0]["reason"] == reason
    with Journal(path) as journal:
        p = journal.connection.execute(
            "SELECT * FROM route_positions WHERE position_key=?", (_position_key(n),)).fetchone()
        assert p and p["state"] == (existing_state or "planned") and p["reason"] == reason
        assert p["group_id"] is None and p["source_asset_id"] is None
        assert p["source_amount_raw"] is None and p["accounted_loss_usd"] is None
        assert not journal.has_broadcast(p["id"])
        assert journal.connection.execute("SELECT COUNT(*) FROM route_groups").fetchone()[0] == 0
    assert n["group"]["source_usd"] == "100" and n["group"]["max_route_loss_pct"] == "15"


@pytest.mark.parametrize("hold", ["source", "destination"])
def test_relay_hold_keeps_known_hash_and_existing_state_observable(tmp_path, hold):
    n, path = native(8453, provider="relay"), tmp_path / "inventory.sqlite"
    clock, observed = [1000], []
    dependency = bridge() if hold == "destination" else {**native(8453), "asset_id": "0xdef"}
    with Journal(path) as journal:
        p = journal.get_or_create_position(position_key=_position_key(n), wallet=WALLET)
        journal.record_position_state(p["id"], "bridge_submitted", reason="receipt_pending")
        journal.record_step_intent(position_id=p["id"], step_key="relay_deposit", nonce=9,
                                   calldata_digest="a" * 64, signed_payload_digest="b" * 64,
                                   tx_hash="0x" + "c" * 64)
    result = drive_execution(
        {"entries": [n]}, path, dependency_plan={"entries": [n, dependency]},
        execute_round=lambda entries: observed.extend(entries), until=1003,
        clock=lambda: clock[0], sleep=lambda s: clock.__setitem__(0, clock[0] + s),
    )
    assert len(observed) == 1 and not observed[0][f"_{hold}_gas_held"]
    assert result["positions"][0]["state"] == "bridge_submitted"
    assert result["positions"][0]["reason"] == "receipt_pending"
    with Journal(path) as journal:
        assert journal.latest_step(position_id=p["id"], step_key="relay_deposit")["tx_hash"] == (
            "0x" + "c" * 64)


@pytest.mark.parametrize("reason,search,held", [
    ("quota_wait", {"complete": False, "pending": [{"retry_at": 2000}]}, True),
    ("below_target_minimum", {"complete": False, "pending": [{"retry_at": 2000}]}, True),
    ("unsupported_by_lifi", {}, False),
    ("unsupported_by_relay", {}, False),
    ("policy_review", {"complete": False, "pending": [{"retry_at": 2000}]}, False),
    ("below_target_minimum", {"complete": True, "pending": []}, False),
    ("loss_threshold_exceeded", {}, False),
])
def test_retryable_search_holds_only_exact_enabled_targets_from_full_context(
    tmp_path, reason, search, held,
):
    ready = [native(8453), native(10), native(137), native(56),
             {**native(8453), "asset_id": "0xdef"}, native(8453, wallet=EXCHANGE)]
    unresolved = {**bridge(), "status": "manual_review", "reason": reason,
                  "search": search, "steps": []}
    full = {"entries": [*ready, unresolved], "search_scope": {
        "enabled_targets": [{"chain_id": 8453}, {"chain_id": 10}],
        "requested_targets": [{"chain_id": 56}], "unavailable_targets": [{"chain_id": 56}],
    }}
    result, rounds = run({"entries": ready}, tmp_path / "inventory.sqlite",
                         dependency_plan=full, seconds=3)
    assert rounds[0][(WALLET, 8453, "native")] is held
    assert rounds[0][(WALLET, 10, "native")] is held
    assert rounds[0][(WALLET, 137, "native")] is False
    assert rounds[0][(WALLET, 56, "native")] is False
    assert rounds[0][(WALLET, 8453, "0xdef")] is False
    assert rounds[0][(EXCHANGE, 8453, "native")] is False
    assert result["status"] == ("incomplete" if held else "completed")


def test_destination_hold_yields_before_preflight_and_keeps_full_group_budget(tmp_path):
    n = {**native(8453), "_destination_gas_held": True, "valuation": {"loss_usd": "1"}}
    token = {**n, "asset_id": "0xdef", "_destination_gas_held": False,
             "valuation": {"loss_usd": "1"}, "steps": [{"kind": "direct_deposit"}]}
    prepared = []

    def preflight(e):
        prepared.append(e["asset_id"])
        assert e["asset_id"] != "native"
        return e

    with Journal(tmp_path / "inventory.sqlite") as journal:
        result = execute_group_entries([token, n], journal=journal,
                                       max_route_loss_pct=Decimal("15"), preflight=preflight,
                                       submit_step=lambda *_: {"state": "completed"})
        assert result["deferred"] == 1
        assert prepared == ["0xdef"]
        group = journal.group(result["group_id"])
        assert group["state"] == "active"
        assert group["source_usd"] == "100" and group["loss_budget_pct"] == "15"
        position = journal.connection.execute(
            "SELECT * FROM route_positions WHERE position_key=?", (_position_key(n),)).fetchone()
        assert position["state"] == "planned"
        assert position["reason"] == "destination_gas_for_unsettled_routes"
        assert not journal.has_broadcast(position["id"])


def test_destination_held_group_observes_its_known_native_hash(tmp_path):
    n = {**native(8453), "_destination_gas_held": True, "valuation": {"loss_usd": "1"}}
    observed = []
    with Journal(tmp_path / "inventory.sqlite") as journal:
        g = journal.get_or_create_group(
            group_key=n["group"]["key"], wallet=WALLET, source_chain_id=8453,
            loss_budget_pct="15", source_usd="100")
        p = journal.get_or_create_position(position_key=_position_key(n), wallet=WALLET,
                                           group_id=g["id"], source_asset_id="native",
                                           source_amount_raw=n["raw_balance"])
        journal.record_position_state(p["id"], "direct_deposit_submitted")
        journal.record_step_intent(position_id=p["id"], step_key="direct_deposit", nonce=9,
                                   calldata_digest="a" * 64, signed_payload_digest="b" * 64,
                                   tx_hash="0x" + "c" * 64)

        def observe(e, step):
            observed.append(step["kind"])
            assert e["_journal_position_id"] == p["id"]
            return {"state": "completed", "realized_loss_usd": "1"}

        result = execute_group_entries(
            [n], journal=journal, max_route_loss_pct=Decimal("15"),
            preflight=lambda _: pytest.fail("known native hash must only be observed"),
            submit_step=observe,
        )
        assert result["completed"] == 1 and observed == ["direct_deposit"]
        assert journal.latest_step(position_id=p["id"], step_key="direct_deposit")["tx_hash"] == (
            "0x" + "c" * 64)


def test_collection_callback_keeps_full_search_context_and_allows_source_retry(tmp_path):
    from evm_inventory.collection import collect_wallets
    from evm_inventory.config import load_catalog

    path, clock, rounds, quote_times = tmp_path / "inventory.sqlite", [1000.0], [], []
    allowlist = tmp_path / "allowlist.json"
    allowlist.write_text('{"assets": []}')
    ready = [native(8453), native(10), native(137)]
    waiting = {**bridge(), "status": "manual_review", "reason": "quota_wait", "steps": [],
               "search": {"complete": False, "pending": [{"reason": "quota_wait"}]}}

    def scan(store, *_args, **_kwargs):
        p = store.upsert_asset(WALLET, 81457, "0xabc", metadata={"decimals": 18})
        store.record_asset(p, {"raw_balance": "1000", "decimals": 18,
                               "amount": "0.000000000000001"})
        return {"mandatory_failed": 0, "discovered_failed": 0, "timed_out": False}

    def quote(*_args, **_kwargs):
        quote_times.append(clock[0])
        entries = [*ready, waiting] if len(quote_times) == 1 else [bridge(recipient=EXCHANGE)]
        return {"entries": entries,
                "search_scope": {"enabled_targets": [{"chain_id": 8453}, {"chain_id": 10}]}}

    def execute(entries, **kwargs):
        assert kwargs["journal_path"] == path
        rounds.append({e["chain_id"]: bool(e.get("_destination_gas_held")) for e in entries})
        with Journal(path) as journal:
            for e in entries:
                if not e.get("_destination_gas_held"):
                    finish(journal, e)
        return {}

    result = collect_wallets(
        [SimpleNamespace(public_address=WALLET, bitget_deposit_address=EXCHANGE, ordinal=3)],
        db_path=path, output=tmp_path / "output", catalog=load_catalog(), rpc_urls={},
        allowlist=allowlist, target_policy=SimpleNamespace(digest="policy"),
        loss_limit=Decimal("15"), execute=True, trust_deposit_addresses=True,
        max_runtime_seconds=120, scan_fn=scan, quote_fn=quote, execute_fn=execute,
        clock=lambda: clock[0], sleep=lambda s: clock.__setitem__(0, clock[0] + s),
    )
    assert rounds[0] == {8453: True, 10: True, 137: False}
    assert len(quote_times) == 2 and quote_times[1] < 1010
    assert result["status"] == "completed"
    assert len(list(tmp_path.glob("*.sqlite"))) == 1


def test_terminal_upfront_fee_refusal_releases_destination_native_reserves(tmp_path):
    blocked = {**native(81457), 'status': 'manual_review',
               'reason': 'source_native_fee_exceeds_balance',
               'search': {'complete': True, 'errors': [], 'pending': []}}
    targets = [native(chain) for chain in (8453, 10, 42161)]
    plan = {'entries': [blocked, *targets], 'search_scope': {
        'enabled_targets': [{'chain_id': chain} for chain in (8453, 10, 42161)]}}
    result, rounds = run(plan, tmp_path / 'inventory.sqlite')
    assert all(rounds[0][(WALLET, chain, 'native')] is False for chain in (8453, 10, 42161))
    assert result['status'] == 'completed'


def test_fee_refusal_with_pending_alternative_keeps_destination_gas(tmp_path):
    blocked = {**native(81457), 'status': 'manual_review',
               'reason': 'source_native_fee_exceeds_balance',
               'search': {'complete': False, 'errors': ['quota_wait'],
                          'pending': [{'reason': 'quota_wait', 'retry_at': 2000}]}}
    plan = {'entries': [blocked, native(8453)], 'search_scope': {
        'enabled_targets': [{'chain_id': 8453}]}}
    result, rounds = run(plan, tmp_path / 'inventory.sqlite')
    assert rounds[0][(WALLET, 8453, 'native')] is True
    assert result['reason'] == 'planning_incomplete'


def test_fee_refusal_preserves_gas_for_already_broadcast_saved_continuation(tmp_path):
    b = {**bridge(), 'status': 'manual_review',
         'reason': 'source_native_fee_exceeds_balance', 'steps': [],
         'search': {'complete': True, 'errors': [], 'pending': []}}
    path = tmp_path / 'inventory.sqlite'
    with Journal(path) as journal:
        p = journal.get_or_create_position(position_key=_position_key(b), wallet=WALLET)
        journal.record_step_intent(position_id=p['id'], step_key='bridge', nonce=9,
                                   calldata_digest='a'*64, signed_payload_digest='b'*64,
                                   tx_hash='0x'+'c'*64)
        journal.save_checkpoint(p['id'], 'canonical_prepared', {
            'continuation': {**native(8453), 'steps': []}})
    _, rounds = run({'entries': [b, native(8453)]}, path)
    assert rounds[0][(WALLET, 8453, 'native')] is True
