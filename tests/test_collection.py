import json
from functools import partial

import pytest

from evm_inventory.journal import Journal
from evm_inventory.route_execution import _position_key


@pytest.mark.parametrize("finalized", [99, 100])
def test_confirmed_defi_exit_requires_live_finalized_receipt(tmp_path, finalized):
    from types import SimpleNamespace

    from evm_inventory.collection import verify_prior_defi_exit
    from evm_inventory.store import Store

    path = tmp_path / "inventory.sqlite"
    wallet, action = entry()["wallet"], "old-action"
    with Store(path) as store:
        with store.db:
            store.db.execute("INSERT INTO defi_plans VALUES ('old-plan',1,'{}',1)")
            store.db.execute(
                "INSERT INTO defi_positions VALUES ('old-plan',1,?,10,'p','pool',"
                "'ready',NULL,?,'{}')",
                (wallet, action),
            )
    with Journal(path) as journal:
        p = journal.get_or_create_position(position_key=f"defi:{wallet}:{action}", wallet=wallet)
        step = journal.record_step_intent(
            position_id=p["id"],
            step_key="defi_withdraw",
            nonce=48,
            calldata_digest="a" * 64,
            signed_payload_digest="b" * 64,
        )
        tx_hash = "0x" + "c" * 64
        journal.record_broadcast_attempt(step["id"], tx_hash)
        journal.record_receipt(step["id"], status="confirmed", finality_block=100)

        def call(_, method, params):
            if method == "eth_chainId":
                return "0xa"
            if method == "eth_getBlockByNumber":
                assert params == ["finalized", False]
                return {"number": hex(finalized)}
            assert method == "eth_getTransactionReceipt" and params == [tx_hash]
            return {"status": "0x1", "blockNumber": "0x64", "transactionHash": tx_hash}

        assert verify_prior_defi_exit(
            journal, p["id"], {10: "rpc"}, rpc=SimpleNamespace(call=call)
        ) is (finalized >= 100)


def entry(chain=10):
    return dict(
        wallet="0x" + "a" * 40,
        chain_id=chain,
        asset_id="native",
        raw_balance="1000",
        status="direct_deposit",
        group=dict(key=f"test:{chain}", source_usd="100", max_route_loss_pct="15"),
    )


def test_execution_driver_observes_pending_in_same_call_and_never_repeats_completed_group(tmp_path):
    from evm_inventory.collection import drive_execution

    path = tmp_path / "inventory.sqlite"
    plan = {"entries": [entry(10), entry(8453)]}
    clock = [1000.0]
    rounds = []

    def execute_round(entries):
        rounds.append([e["chain_id"] for e in entries])
        with Journal(path) as journal:
            for e in entries:
                p = journal.get_or_create_position(
                    position_key=_position_key(e), wallet=e["wallet"]
                )
                if e["chain_id"] == 10 or len(rounds) == 2:
                    journal.record_position_state(p["id"], "completed")
                else:
                    journal.record_position_state(p["id"], "planned", reason="receipt_pending")
        return {}

    result = drive_execution(
        plan,
        path,
        execute_round=execute_round,
        until=1010,
        clock=lambda: clock[0],
        sleep=lambda s: clock.__setitem__(0, clock[0] + s),
    )
    assert rounds == [[10, 8453], [8453]]
    assert result["status"] == "completed"
    assert result["completed"] == 2


def test_driver_stops_on_terminal_failure_and_reports_it(tmp_path):
    from evm_inventory.collection import drive_execution

    path = tmp_path / "inventory.sqlite"
    e = entry()
    calls = []

    def fail(entries):
        calls.append(entries)
        with Journal(path) as journal:
            p = journal.get_or_create_position(position_key=_position_key(e), wallet=e["wallet"])
            journal.record_position_state(p["id"], "manual_review", reason="recipient_mismatch")
        return {}

    result = drive_execution(
        {"entries": [e]},
        path,
        execute_round=fail,
        until=2000,
        clock=lambda: 1000,
        sleep=lambda _: None,
    )
    assert len(calls) == 1
    assert result["status"] == "incomplete"
    assert result["positions"][0]["reason"] == "recipient_mismatch"


def test_pending_driver_is_bounded_and_checkpoint_can_be_resumed(tmp_path):
    from evm_inventory.collection import drive_execution

    path = tmp_path / "inventory.sqlite"
    clock = [1000.0]
    plan = {"entries": [entry()]}
    result = drive_execution(
        plan,
        path,
        execute_round=lambda _: {},
        until=1002,
        clock=lambda: clock[0],
        sleep=lambda s: clock.__setitem__(0, clock[0] + s),
    )
    assert result["status"] == "incomplete"
    assert result["reason"] == "collection_deadline_exceeded"


def test_changed_recipient_invalidates_persisted_trust(tmp_path):
    import pytest

    from evm_inventory.collection import trust_recipient
    from evm_inventory.store import Store

    with Store(tmp_path / "inventory.sqlite") as store:
        trust_recipient(store, "wallet", "address-one", explicit=True)
        trust_recipient(store, "wallet", "address-one", explicit=False)
        with pytest.raises(ValueError, match="trust-deposit-addresses"):
            trust_recipient(store, "wallet", "address-two", explicit=False)


def test_planning_waits_for_quota_and_only_retries_unresolved_network(tmp_path):
    import csv

    from evm_inventory.collection import recover_planning

    path = tmp_path / "balances.csv"
    wallet = entry()["wallet"]
    path.write_text(
        "wallet,chain_id,asset_id,raw_balance,decimals,status\n"
        f"{wallet},10,native,100,18,success\n{wallet},8453,native,200,18,success\n"
    )
    clock = [1000.0]
    waiting = {
        **entry(8453),
        "status": "manual_review",
        "reason": "quota_wait",
        "search": {"pending": [{"retry_at": 1002.0, "reason": "quota_wait"}]},
    }
    plan = {"entries": [entry(10), waiting]}
    calls = []

    def quote(source):
        with source.open() as stream:
            calls.append(list(csv.DictReader(stream)))
        assert clock[0] >= 1002.0
        return {"entries": [entry(8453)]}

    result = recover_planning(
        plan,
        path,
        quote,
        until=1010,
        clock=lambda: clock[0],
        wall_clock=lambda: clock[0],
        sleep=lambda s: clock.__setitem__(0, clock[0] + s),
    )
    assert len(calls) == 1
    assert [r["chain_id"] for r in calls[0]] == ["8453"]
    assert all(e["status"] == "direct_deposit" for e in result["entries"])


@pytest.mark.parametrize("reason", ["quota_wait", "quote_timeout", "quote_request_failed"])
def test_rejected_one_target_does_not_hide_pending_alternative_routes(reason):
    from evm_inventory.collection import _unresolved

    waiting = {"status": "manual_review", "reason": "below_target_minimum",
               "search": {"complete": False, "pending": [{"reason": reason}]}}
    assert _unresolved(waiting)
    assert not _unresolved({**waiting, "search": {"complete": True, "pending": []}})


@pytest.mark.parametrize("broadcast", [False, True])
def test_final_execution_holds_native_for_unresolved_token_but_observes_known_hash(
    tmp_path, broadcast,
):
    from evm_inventory.collection import drive_execution

    native = entry(10)
    token = {**native, "asset_id": "0xabc", "status": "manual_review",
             "reason": "below_target_minimum", "search": {
                 "complete": False, "pending": [{"reason": "quota_wait"}]}}
    path, calls, clock = tmp_path / "inventory.sqlite", [], [1000.0]
    if broadcast:
        with Journal(path) as journal:
            p = journal.get_or_create_position(position_key=_position_key(native),
                                              wallet=native["wallet"])
            step = journal.record_step_intent(position_id=p["id"], step_key="direct_deposit",
                nonce=9, calldata_digest="a"*64, signed_payload_digest="b"*64, tx_hash="0x"+"c"*64)
            assert step["tx_hash"]
    def execute(entries):
        calls.extend(entries)
        with Journal(path) as journal:
            journal.record_position_state(p["id"], "completed")
    result = drive_execution({"entries": [native, token]}, path, execute_round=execute,
        until=1010, clock=lambda: clock[0], sleep=lambda s: clock.__setitem__(0, clock[0]+s))
    assert len(calls) == int(broadcast)
    assert result["status"] == ("completed" if broadcast else "incomplete")


def test_pending_relay_diagnostics_remain_retryable_without_unhashable_dict_error():
    from evm_inventory.collection import _search_error_reasons, _unresolved

    search = {"complete": False, "errors": [{"destination_chain_id": 10, "retry_at": 1002}],
              "pending": [{"retry_at": 1002}]}
    assert _unresolved({"status": "manual_review", "reason": "below_target_minimum",
                        "search": search})
    assert _search_error_reasons(search) == {"quota_wait"}


def test_recovery_on_partially_sent_network_preserves_sent_entries_and_requotes_unsent_assets(
    tmp_path,
):
    import csv

    from evm_inventory.collection import recover_planning

    source = tmp_path / "balances.csv"
    source.write_text("wallet,chain_id,asset_id,raw_balance,decimals,status\n"
                      "wallet,10,native,100,18,success\n"
                      "wallet,10,0xabc,200,18,success\n")
    sent = {**entry(10), "wallet": "wallet"}
    waiting = {**sent, "asset_id": "0xabc", "status": "manual_review", "reason": "quota_wait",
               "search": {"pending": [{"reason": "quota_wait", "retry_at": 1002}]}}
    clock = [1000.0]
    calls = []
    def quote(path):
        calls.extend(csv.DictReader(path.open()))
        return {"entries": [{**waiting, "status": "route_ready", "reason": None}]}
    result = recover_planning({"entries": [sent, waiting]}, source, quote,
        until=1010, clock=lambda: clock[0], wall_clock=lambda: clock[0],
        sleep=lambda s: clock.__setitem__(0, clock[0]+s), locked_chains={10},
        frozen_keys={("wallet", 10, "native")})
    assert [r["asset_id"] for r in calls] == ["0xabc"]
    assert result["entries"][0] == sent
    assert result["entries"][1]["status"] == "route_ready"


def test_planning_progress_hook_runs_before_quota_wait_and_after_refresh(tmp_path):
    from evm_inventory.collection import recover_planning

    source = tmp_path / "balances.csv"
    source.write_text("wallet,chain_id,asset_id,raw_balance,decimals,status\n"
                      "wallet,8453,native,100,18,success\n")
    clock, events = [1000.0], []
    waiting = {**entry(8453), "status": "manual_review", "reason": "quota_wait",
               "search": {"pending": [{"retry_at": 1002.0}]}}
    def tick(plan):
        events.append((clock[0], sum(e["status"] == "direct_deposit" for e in plan["entries"])))
    recover_planning(
        {"entries": [entry(10), waiting]}, source,
        lambda _: {"entries": [entry(8453)]}, until=1010,
        clock=lambda: clock[0], wall_clock=lambda: clock[0],
        sleep=lambda s: clock.__setitem__(0, clock[0] + s), on_plan=tick,
    )
    assert events == [(1000, 1), (1002, 2)]


def test_planning_retry_preserves_ready_unsent_sibling_on_same_network(tmp_path):
    import csv

    from evm_inventory.collection import recover_planning
    native = {**entry(10), "wallet": "wallet"}
    waiting = {**native, "asset_id": "0xabc", "status": "manual_review", "reason": "quota_wait",
               "search": {"pending": [{"retry_at": 1002}]}}
    source = tmp_path / "balances.csv"
    source.write_text("wallet,chain_id,asset_id,raw_balance,decimals,status\n"
                      "wallet,10,native,100,18,success\n"
                      "wallet,10,0xabc,200,18,success\n")
    clock, calls = [1000.0], []
    def quote(path):
        calls.extend(csv.DictReader(path.open()))
        return {"entries": [{**waiting, "status": "route_ready", "reason": None}]}
    result = recover_planning({"entries": [native, waiting]}, source, quote, until=1010,
        clock=lambda: clock[0], wall_clock=lambda: clock[0],
        sleep=lambda s: clock.__setitem__(0, clock[0]+s), frozen_keys=set())
    assert [r["asset_id"] for r in calls] == ["0xabc"]
    assert next(e for e in result["entries"] if e["asset_id"] == "native") == native


def test_planning_retry_does_not_query_future_job_on_same_network(tmp_path):
    import csv

    from evm_inventory.collection import recover_planning
    waiting = {**entry(10), "wallet": "wallet", "asset_id": "0xaaa", "status": "manual_review",
               "reason": "quota_wait", "search": {"pending": [{"retry_at": 1002}]}}
    later = {**waiting, "asset_id": "0xbbb", "search": {"pending": [{"retry_at": 2000}]}}
    source = tmp_path / "balances.csv"
    source.write_text("wallet,chain_id,asset_id,raw_balance,decimals,status\n"
                      "wallet,10,0xaaa,100,18,success\n"
                      "wallet,10,0xbbb,200,18,success\n")
    clock, calls = [1000.0], []
    def quote(path):
        calls.extend(csv.DictReader(path.open()))
        return {"entries": [{**waiting, "status": "route_ready", "reason": None}]}
    result = recover_planning({"entries": [waiting, later]}, source, quote, until=1010,
        clock=lambda: clock[0], wall_clock=lambda: clock[0],
        sleep=lambda s: clock.__setitem__(0, clock[0]+s))
    assert [r["asset_id"] for r in calls] == ["0xaaa"]
    assert next(e for e in result["entries"] if e["asset_id"] == "0xbbb") == later


def test_missing_retry_result_does_not_remove_owned_balance(tmp_path):
    from evm_inventory.collection import recover_planning
    waiting = {**entry(10), "wallet": "wallet", "status": "manual_review", "reason": "quota_wait"}
    source = tmp_path / "balances.csv"
    source.write_text("wallet,chain_id,asset_id,raw_balance,decimals,status\n"
                      "wallet,10,native,100,18,success\n")
    clock = [1000.0]
    result = recover_planning({"entries": [waiting]}, source, lambda _: {"entries": []}, until=1003,
        clock=lambda: clock[0], sleep=lambda s: clock.__setitem__(0, clock[0]+s))
    assert result["entries"] == [waiting]


@pytest.mark.parametrize("same_group", (False, True))
def test_native_waits_for_retried_token_with_real_group_identity(tmp_path, same_group):
    from evm_inventory.collection import drive_execution
    native = {**entry(10), "status": "route_ready", "routing_provider": "lifi"}
    token = {**native, "asset_id": "0xabc", "group": {
        **native["group"], "key": native["group"]["key"] if same_group else "new-token-group"}}
    path, calls, clock = tmp_path / "fixture.sqlite", [], [1000.0]
    def execute(entries):
        calls.append([e["asset_id"] for e in entries if not e.get("_source_gas_held")])
        with Journal(path) as journal:
            for e in entries:
                if e.get("_source_gas_held"):
                    continue
                p = journal.get_or_create_position(
                    position_key=_position_key(e), wallet=e["wallet"])
                journal.record_position_state(p["id"], "completed")
    result = drive_execution({"entries": [native, token]}, path, execute_round=execute,
        until=1010, clock=lambda: clock[0], sleep=lambda s: clock.__setitem__(0, clock[0]+s))
    assert calls == ([["0xabc", "native"]] if same_group else [["0xabc"], ["native"]])
    assert result["status"] == "completed"
    assert native["group"]["source_usd"] == "100"


def test_group_keeps_native_gas_hold_before_any_preparation_or_signature(tmp_path):
    from decimal import Decimal

    from evm_inventory.route_execution import execute_group_entries
    native = {**entry(10), "_source_gas_held": True}
    with Journal(tmp_path / "fixture.sqlite") as journal:
        result = execute_group_entries([native], journal=journal,
            max_route_loss_pct=Decimal("15"),
            preflight=lambda _: pytest.fail("held native must not prepare"),
            submit_step=lambda *_: pytest.fail("held native must not send"))
        assert result["deferred"] == 1
        group = journal.group(result["group_id"])
        assert group["state"] == "active" and group["source_usd"] == "100"
        position = journal.connection.execute(
            "SELECT * FROM route_positions WHERE position_key=?",
            (_position_key(native),)).fetchone()
        assert position["state"] == "planned"
        assert position["reason"] == "source_gas_for_unsettled_tokens"
        assert not journal.has_broadcast(position["id"])


def test_failed_token_source_hold_returns_without_empty_runtime_loop(tmp_path):
    from evm_inventory.collection import drive_execution

    native = {**entry(81457), "status": "route_ready", "routing_provider": "relay"}
    token = {**entry(81457), "status": "route_ready", "asset_id": "0xabc",
             "group": {**entry(81457)["group"], "key": "token-group"}}
    path = tmp_path / "inventory.sqlite"
    with Journal(path) as journal:
        p = journal.get_or_create_position(position_key=_position_key(token),
                                           wallet=token["wallet"])
        journal.record_position_state(p["id"], "manual_review", reason="route_simulation_failed")
    clock, sleeps = [1000.0], []
    def sleep(seconds):
        sleeps.append(seconds)
        clock[0] += seconds
    result = drive_execution({"entries": [native, token]}, path,
        execute_round=lambda _: pytest.fail("held native cannot send"), until=1010,
        clock=lambda: clock[0], sleep=sleep)
    assert result["rounds"] == 1
    assert result["reason"] == "source_gas_for_unsettled_tokens"
    assert not sleeps


@pytest.mark.parametrize("conflict", (False, True))
def test_celo_retry_selects_native_alias_and_retains_conflicting_views(tmp_path, conflict):
    import csv

    from evm_inventory.celo import CELO_TOKEN_ADDRESS, normalize_celo_balances
    from evm_inventory.collection import recover_planning
    waiting = {**entry(42220), "wallet": "wallet", "asset_id": CELO_TOKEN_ADDRESS,
               "status": "manual_review", "reason": "quota_wait"}
    source = tmp_path / "balances.csv"
    source.write_text("wallet,chain_id,asset_id,raw_balance,decimals,status\n"
        "wallet,42220,native,100,18,success\n" +
        (f"wallet,42220,{CELO_TOKEN_ADDRESS},200,18,success\n" if conflict else ""))
    calls, clock = [], [1000.0]
    def quote(path):
        rows = list(csv.DictReader(path.open()))
        calls.extend(rows)
        normalized = normalize_celo_balances(rows)
        assert normalized[0].get("balance_conflict", False) == conflict
        return {"entries": [{**waiting, "status": "route_ready", "reason": None}]}
    recover_planning({"entries": [waiting]}, source, quote, until=1010,
        clock=lambda: clock[0], sleep=lambda s: clock.__setitem__(0, clock[0]+s))
    assert len(calls) == (2 if conflict else 1)
    assert calls[0]["asset_id"] == "native"


def test_quota_wait_does_not_delay_unrelated_rpc_retry(tmp_path):
    import csv

    from evm_inventory.collection import recover_planning

    source = tmp_path / "balances.csv"
    wallet = entry()["wallet"]
    source.write_text("wallet,chain_id,asset_id,raw_balance,decimals,status\n"
                      f"{wallet},1116,native,100,18,success\n"
                      f"{wallet},8453,native,100,18,success\n")
    clock, calls = [1000.0], []
    waiting = {**entry(8453), "status": "manual_review", "reason": "quota_wait",
               "search": {"errors": ["quota_wait"], "pending": [{"retry_at": 1008.0}]}}
    rpc_retry = {**entry(1116), "status": "manual_review", "reason": "route_search_incomplete",
                 "search": {"errors": ["missing_gas_estimate"]}}
    def quote(path):
        with path.open() as stream:
            chains = [int(row["chain_id"]) for row in csv.DictReader(stream)]
        calls.append((clock[0], chains))
        return {"entries": [entry(c) for c in chains]}
    recover_planning(
        {"entries": [waiting, rpc_retry]}, source, quote, until=1020,
        clock=lambda: clock[0], wall_clock=lambda: clock[0],
        sleep=lambda s: clock.__setitem__(0, clock[0] + s),
    )
    assert calls == [(1001, [1116]), (1008, [8453])]


@pytest.mark.parametrize("resume_after_crash", [False, "pending", "completed", "superseded"])
@pytest.mark.parametrize("include_skip", [False, True])
@pytest.mark.parametrize("prior_defi", [None, "confirmed", "pending", "confirmed_route"])
@pytest.mark.parametrize("early_ready", [False, True])
def test_single_cli_invocation_scans_recovers_plans_and_settles_without_second_send(
    tmp_path, monkeypatch, capsys, resume_after_crash, include_skip, prior_defi, early_ready
):
    from openpyxl import load_workbook

    from evm_inventory.cli import main
    from evm_inventory.collection import collect_wallets
    from evm_inventory.store import Store
    from evm_inventory.workbook import create_wallet_template

    workbook = tmp_path / "wallets.xlsx"
    create_wallet_template(workbook)
    book = load_workbook(workbook)
    book["Wallets"].append([3, entry()["wallet"], "0x" + "1" * 64, "0x" + "d" * 40])
    book.save(workbook)
    path, output = tmp_path / "inventory.sqlite", tmp_path / "result"
    if prior_defi:
        with Store(path) as store:
            with store.db:
                store.db.execute("INSERT INTO defi_plans VALUES ('old-plan',1,'{}',1)")
                store.db.execute(
                    "INSERT INTO defi_positions VALUES ('old-plan',1,?,10,'p','pool',"
                    "'ready',NULL,'old-action','{}')",
                    (entry()["wallet"],),
                )
        with Journal(path) as journal:
            p = journal.get_or_create_position(
                position_key=f"{'external' if prior_defi == 'confirmed_route' else 'defi'}:"
                f"{entry()['wallet']}:old-action",
                wallet=entry()["wallet"],
            )
            step = journal.record_step_intent(
                position_id=p["id"],
                step_key="bridge" if prior_defi == "confirmed_route" else "defi_withdraw",
                nonce=48,
                calldata_digest="a" * 64,
                signed_payload_digest="b" * 64,
            )
            journal.record_broadcast_attempt(step["id"], "0x" + "c" * 64)
            if prior_defi.startswith("confirmed"):
                journal.record_receipt(step["id"], status="confirmed", finality_block=100)
        from types import SimpleNamespace

        def prior_rpc(_, method, params):
            if method == "eth_chainId":
                return "0xa"
            if method == "eth_getBlockByNumber":
                assert params == ["finalized", False]
                return {"number": "0x100"}
            assert method == "eth_getTransactionReceipt"
            return {"status": "0x1", "blockNumber": "0x64", "transactionHash": params[0]}

        monkeypatch.setattr(
            "evm_inventory.collection.ExecutionRpc", lambda _: SimpleNamespace(call=prior_rpc)
        )
    clock = [1000.0]
    calls = dict(scan=0, quote=0, execute=0, send=0)
    events = []

    def scan(store, scope, **kw):
        calls["scan"] += 1
        assert scope["settings"]["complete_scan"] is True
        assert scope["settings"]["stage_seconds"] > 30
        p = store.upsert_asset(entry()["wallet"], 10, "native", metadata=dict(decimals=18))
        store.record_asset(p, dict(raw_balance="1000", decimals=18, amount="0.000000000000001"))
        return dict(mandatory_failed=0, discovered_failed=0, timed_out=False)

    def quote(source, row, **kw):
        events.append("quote")
        calls["quote"] += 1
        assert source.exists() and kw["db_path"] == path
        ready = entry()
        if calls["quote"] == 1:
            ready = {**ready, "status": "manual_review", "reason": "route_search_incomplete"}
        skipped = {
            **entry(),
            "asset_id": "0x" + "b" * 40,
            "status": "denied",
            "reason": "asset_not_allowlisted",
        }
        entries = [ready, skipped] if include_skip else [ready]
        if early_ready and calls["quote"] == 1:
            entries.append(entry(56))
        return {
            "entries": entries,
            "summary": {ready["status"]: 1, **({"denied": 1} if include_skip else {})},
        }

    def execute(entries, **kw):
        events.append("execute")
        calls["execute"] += 1
        assert kw["journal_path"] == path
        assert kw["execute"] and kw["trust_deposit_addresses"]
        with Journal(path) as journal:
            saved = journal.connection.execute("SELECT plan_json FROM collection_runs "
                                               "WHERE status='executing'").fetchone()
            assert saved and all(_position_key(e) in {
                _position_key(s) for s in json.loads(saved[0])["entries"]
            } for e in entries)
            for e in entries:
                p = journal.get_or_create_position(
                    position_key=_position_key(e), wallet=e["wallet"]
                )
                if not journal.checkpoint(p["id"], "sent"):
                    calls["send"] += 1
                    journal.save_checkpoint(p["id"], "sent", {"hash": "already-sent"})
                    journal.record_position_state(p["id"], "direct_deposit_submitted")
                    step = journal.record_step_intent(
                        position_id=p["id"],
                        step_key="direct_deposit",
                        nonce=0,
                        calldata_digest="a" * 64,
                        signed_payload_digest="b" * 64,
                    )
                    journal.record_broadcast_attempt(step["id"], "0x" + "a" * 64)
                state = "deposit_pending" if calls["execute"] == 1 else "completed"
                if resume_after_crash in {"completed", "superseded"}:
                    state = "completed"
                journal.record_position_state(p["id"], state)
        if resume_after_crash and calls["execute"] == 1:
            import httpx

            raise httpx.ReadTimeout("simulated process interruption")
        return {}

    monkeypatch.setattr(
        "evm_inventory.cli.collect_wallets",
        partial(
            collect_wallets,
            scan_fn=scan,
            quote_fn=quote,
            execute_fn=execute,
            clock=lambda: clock[0],
            sleep=lambda s: clock.__setitem__(0, clock[0] + s),
        ),
    )
    args = [
        "collect-wallet",
        "--workbook",
        str(workbook),
        "--wallet-ranges",
        "3",
        "--db",
        str(path),
        "--output",
        str(output),
        "--max-runtime-seconds",
        "120",
        "--execute",
    ]
    first = main([*args, "--trust-deposit-addresses"])
    if prior_defi in {"pending", "confirmed_route"}:
        assert first == 2
        assert calls == dict(scan=0, quote=0, execute=0, send=0)
        assert "unresolved_wallet_execution" in capsys.readouterr().err
        return
    if resume_after_crash:
        assert first == 4
        capsys.readouterr()
        if resume_after_crash == "superseded":
            with Store(path) as store:
                with store.db:
                    store.db.execute("UPDATE collection_runs SET status='superseded'")
        dry_args = [a for a in args if a != "--execute"]
        assert main(dry_args) == 3
        capsys.readouterr()
        with Store(path) as store:
            assert (
                store.db.execute("SELECT status FROM collection_runs").fetchone()[0]
                == ("superseded" if resume_after_crash == "superseded" else "executing")
            )
        args[args.index("--output") + 1] = str(tmp_path / "resumed-output")
        assert main(args) == 0
    else:
        assert first == 0
    result = json.loads(capsys.readouterr().out)
    expected_status = "completed_with_skips" if include_skip else "completed"
    assert result["status"] == expected_status
    assert result["wallets"][0]["execution"]["completed"] == (2 if early_ready else 1)
    assert calls == dict(
        scan=1, quote=2, execute=(
            1 if resume_after_crash in {"completed", "superseded"} and not early_ready
            else 3 if early_ready and not resume_after_crash else 2),
        send=2 if early_ready else 1,
    )
    if early_ready:
        assert events.index("execute") < events.index("quote", 1)
    assert len(list(tmp_path.glob("*.sqlite"))) == 1
    with Store(path) as store:
        saved = store.db.execute("SELECT status,plan_json FROM collection_runs").fetchone()
        assert saved["status"] == expected_status
        assert json.loads(saved["plan_json"])["entries"][0]["automatic_requote"]
        assert (
            store.db.execute("SELECT recipient FROM collection_recipients").fetchone()[0]
            == "0x" + "d" * 40
        )
    if resume_after_crash == "superseded":
        with Store(path) as store:
            current = store.db.execute("SELECT * FROM collection_runs").fetchone()
            with store.db:
                store.db.execute(
                    "INSERT INTO collection_runs "
                    "(id,wallet,request_digest,status,plan_json,updated_at) "
                    "VALUES ('historical-plan',?,?,'superseded',?,0)",
                    (current["wallet"], current["request_digest"], current["plan_json"]),
                )
        assert main(args) == 0
        assert calls["scan"] == 2  # New completed run prevents resurrecting historical plans.


def test_long_planning_does_not_destroy_route_needed_for_automatic_preflight():
    from datetime import UTC, datetime

    from evm_inventory.live_plan import _invalidate_expired_comparisons

    e = {
        **entry(),
        "search": {"complete": True, "evidence_expires_at": "2026-01-01T00:00:00+00:00"},
    }
    _invalidate_expired_comparisons([e], datetime(2026, 10, 11, tzinfo=UTC), allow_requote=True)
    assert e["status"] == "direct_deposit"
    assert e["execution_requote_required"]
    assert not e["search"]["complete"]


def test_new_relay_collection_has_new_identity_but_resume_keeps_same_identity():
    from evm_inventory.collection import _assign_collection_identities

    first = {"entries": [{**entry(), "routing_provider": "relay"}]}
    first["entries"][0].pop("group")
    second = json.loads(json.dumps(first))
    _assign_collection_identities(first, "run-one")
    _assign_collection_identities(second, "run-two")
    assert _position_key(first["entries"][0]) != _position_key(second["entries"][0])
    key = _position_key(first["entries"][0])
    _assign_collection_identities(first, "run-one")
    assert _position_key(first["entries"][0]) == key


def test_unsent_relay_on_partially_sent_chain_gets_collection_identity():
    from evm_inventory.collection import _assign_collection_identities

    native = {**entry(), "routing_provider": "relay"}
    native.pop("group")
    _assign_collection_identities({"entries": [native]}, "run-one", locked_chains={10},
        frozen_keys={(native["wallet"], 10, "0xabc")})
    assert native["group"]["key"].startswith("run-one:")


def test_relay_native_drain_waits_for_sibling_token_execution(tmp_path):
    from evm_inventory.collection import drive_execution

    relay = {**entry(), "routing_provider": "relay"}
    token = {
        **entry(),
        "asset_id": "0x" + "b" * 40,
        "group": {**entry()["group"], "key": "token-group"},
    }
    rounds, clock = [], [1000.0]

    def execute(entries):
        rounds.append([e["asset_id"] for e in entries])
        with Journal(tmp_path / "inventory.sqlite") as journal:
            for e in entries:
                p = journal.get_or_create_position(
                    position_key=_position_key(e), wallet=e["wallet"]
                )
                journal.record_position_state(p["id"], "completed")

    result = drive_execution(
        {"entries": [relay, token]},
        tmp_path / "inventory.sqlite",
        execute_round=execute,
        until=1010,
        clock=lambda: clock[0],
        sleep=lambda s: clock.__setitem__(0, clock[0] + s),
    )
    assert rounds == [[token["asset_id"]], ["native"]]
    assert result["status"] == "completed"


@pytest.mark.parametrize("token_state", ["planned", "manual_review"])
def test_already_sent_relay_is_observed_even_when_sibling_token_is_unfinished(
    tmp_path, token_state,
):
    from evm_inventory.collection import drive_execution

    relay = {**entry(), "routing_provider": "relay"}
    token = {**entry(), "asset_id": "0xabc",
             "group": {**entry()["group"], "key": "token-group"}}
    path, clock, calls = tmp_path / "inventory.sqlite", [1000.0], []
    with Journal(path) as journal:
        rp = journal.get_or_create_position(
            position_key=_position_key(relay), wallet=relay["wallet"])
        journal.record_step_intent(position_id=rp["id"], step_key="relay", nonce=9,
            calldata_digest="a"*64, signed_payload_digest="b"*64, tx_hash="0x"+"c"*64)
        tp = journal.get_or_create_position(
            position_key=_position_key(token), wallet=token["wallet"])
        journal.record_position_state(tp["id"], token_state)
    def execute(entries):
        calls.extend(e["asset_id"] for e in entries)
        with Journal(path) as journal:
            for e in entries:
                pid = rp["id"] if e["asset_id"] == "native" else tp["id"]
                journal.record_position_state(pid, "completed")
    drive_execution({"entries": [relay, token]}, path, execute_round=execute,
        until=1010, clock=lambda: clock[0], sleep=lambda s: clock.__setitem__(0, clock[0]+s))
    assert "native" in calls


def test_provider_retry_can_continue_after_more_than_two_failures(tmp_path):
    from evm_inventory.collection import recover_planning

    source = tmp_path / "balances.csv"
    source.write_text(
        "wallet,chain_id,asset_id,raw_balance,decimals,status\nwallet,10,native,100,18,success\n"
    )
    clock, calls = [1000.0], []
    unresolved = {**entry(), "status": "manual_review", "reason": "route_search_incomplete"}

    def quote(_):
        calls.append(1)
        return {"entries": [unresolved if len(calls) < 4 else entry()]}

    result = recover_planning(
        {"entries": [unresolved]},
        source,
        quote,
        until=1100,
        clock=lambda: clock[0],
        sleep=lambda s: clock.__setitem__(0, clock[0] + s),
    )
    assert len(calls) == 4
    assert result["entries"][0]["status"] == "direct_deposit"
