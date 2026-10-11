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
    path.write_text(
        "wallet,chain_id,asset_id,raw_balance,decimals,status\n"
        "wallet,10,native,100,18,success\nwallet,8453,native,200,18,success\n"
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


def test_quota_wait_does_not_delay_unrelated_rpc_retry(tmp_path):
    import csv

    from evm_inventory.collection import recover_planning

    source = tmp_path / "balances.csv"
    source.write_text("wallet,chain_id,asset_id,raw_balance,decimals,status\n"
                      "wallet,1116,native,100,18,success\nwallet,8453,native,100,18,success\n")
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


@pytest.mark.parametrize("resume_after_crash", [False, True])
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
        dry_args = [a for a in args if a != "--execute"]
        assert main(dry_args) == 3
        capsys.readouterr()
        with Store(path) as store:
            assert (
                store.db.execute("SELECT status FROM collection_runs").fetchone()[0] == "executing"
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
        scan=1, quote=2, execute=3 if early_ready and not resume_after_crash else 2,
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
