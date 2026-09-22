import json
from decimal import Decimal
from types import SimpleNamespace

import pytest

from evm_inventory.cli import _execution_rpc_urls, main
from evm_inventory.models import ConfigError


def test_workbook_commands_create_template_and_write_dry_run(tmp_path, capsys):
    workbook = tmp_path / "wallets.xlsx"

    assert main(["workbook-template", "--output", str(workbook)]) == 0
    template_summary = json.loads(capsys.readouterr().out)
    assert template_summary == {"status": "created", "workbook": str(workbook)}

    from openpyxl import load_workbook

    document = load_workbook(workbook)
    document["Wallets"].append([1, "0x" + "1" * 40, "0x" + "a" * 64, "0x" + "2" * 40])
    document.save(workbook)
    assert main(["workbook-dry-run", "--workbook", str(workbook)]) == 0
    assert json.loads(capsys.readouterr().out) == {"wallets": 1, "actions_written": 1}


def test_route_plan_writes_transaction_free_runbook(tmp_path, capsys):
    balances = tmp_path / "balances.csv"
    balances.write_text(
        "wallet,chain_id,asset_id,raw_balance,symbol\n"
        + "0x" + "1" * 40 + ",8453,native,1,ETH\n"
    )
    output = tmp_path / "runbook.json"

    assert main(["route-plan", "--balances", str(balances), "--output", str(output)]) == 0

    assert json.loads(capsys.readouterr().out)["status"] == "planned"
    assert json.loads(output.read_text())["summary"]["dust"] == 1


def test_dry_run_no_db(tmp_path, capsys):
    wallets = tmp_path / "wallets.txt"
    wallets.write_text("0x" + "1" * 40 + "\n")
    db = tmp_path / "db.sqlite"
    assert main(["scan", "--wallets", str(wallets), "--db", str(db), "--dry-run"]) == 0
    assert not db.exists()
    assert "mandatory_checks" in capsys.readouterr().out


def test_invalid_wallet_fails_before_db(tmp_path):
    wallets = tmp_path / "wallets.txt"
    wallets.write_text("not an address")
    db = tmp_path / "db.sqlite"
    assert main(["scan", "--wallets", str(wallets), "--db", str(db)]) == 2
    assert not db.exists()


def test_dry_run_counts_native_and_each_token_per_wallet(tmp_path, capsys):
    wallets = tmp_path / "wallets.txt"
    wallets.write_text("0x" + "1" * 40 + "\n0x" + "2" * 40 + "\n")
    catalog = tmp_path / "catalog.json"
    token = {
        "address": "0x" + "a" * 40,
        "symbol": "USDC",
        "decimals": 6,
        "source": "https://example.test/token",
        "checked_at": "2026-01-01",
    }
    catalog.write_text(
        json.dumps(
            {
                "revision": "test",
                "checked_at": "2026-01-01",
                "source": "https://example.test",
                "networks": [
                    {
                        "chain_id": 1,
                        "name": "one",
                        "rpc_urls": ["https://rpc.test/1"],
                        "native_symbol": "ETH",
                        "native_decimals": 18,
                        "tokens": [token],
                        "token_review_status": "verified",
                        "alchemy_network": "eth-mainnet",
                    },
                    {
                        "chain_id": 2,
                        "name": "two",
                        "rpc_urls": ["https://rpc.test/2"],
                        "native_symbol": "ETH",
                        "native_decimals": 18,
                        "tokens": [],
                        "token_review_status": "verified",
                        "alchemy_network": None,
                    },
                ],
            }
        )
    )
    allowlist = tmp_path / "allowlist.json"
    allowlist.write_text(json.dumps({"assets": []}))
    assert (
        main(
            [
                "scan",
                "--wallets",
                str(wallets),
                "--db",
                str(tmp_path / "db"),
                "--catalog",
                str(catalog),
                "--allowlist",
                str(allowlist),
                "--dry-run",
            ]
        )
        == 0
    )
    summary = json.loads(capsys.readouterr().out)
    assert summary["mandatory_checks"] == 2 * ((1 + 1) + (1 + 0))


def test_dry_run_summary_reports_catalog_and_key_without_value(tmp_path, capsys, monkeypatch):
    wallets = tmp_path / "wallets.txt"
    wallets.write_text("0x" + "1" * 40 + "\n0x" + "2" * 40 + "\n")
    monkeypatch.setenv("ALCHEMY_API_KEY", "secret-value")
    assert main(["scan", "--wallets", str(wallets), "--db", str(tmp_path / "db"), "--dry-run"]) == 0
    summary = json.loads(capsys.readouterr().out)
    assert summary["mandatory_checks"] > 0
    assert summary["catalog_gaps"] >= 0
    assert summary["alchemy_mapped_networks"] >= 0
    assert summary["alchemy_key_present"] is True
    assert "secret-value" not in json.dumps(summary)


def test_dotenv_quotes_comments_and_environment_precedence(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / ".env").write_text(
        "# comment\nexport ALCHEMY_API_KEY='from-file' # trailing comment\n"
        'OTHER="quoted value"\nEXPAND=${OTHER}\n'
    )
    monkeypatch.setenv("ALCHEMY_API_KEY", "from-environment")
    from evm_inventory.cli import _load_dotenv

    _load_dotenv()
    assert __import__("os").environ["ALCHEMY_API_KEY"] == "from-environment"
    assert __import__("os").environ["OTHER"] == "quoted value"
    assert __import__("os").environ["EXPAND"] == "${OTHER}"


def test_malformed_dotenv_is_sanitized_config_error(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / ".env").write_text("BAD LINE\n")
    from evm_inventory.cli import _load_dotenv

    with pytest.raises(ConfigError, match="invalid .env entry"):
        _load_dotenv()


def test_scan_progress_and_final_summary_are_json_and_stderr_without_run_id(
    tmp_path, capsys, monkeypatch
):
    wallets = tmp_path / "wallets.txt"
    wallets.write_text("0x" + "1" * 40 + "\n")
    monkeypatch.delenv("ALCHEMY_API_KEY", raising=False)
    monkeypatch.setattr("evm_inventory.cli._load_dotenv", lambda: None)
    monkeypatch.setattr(
        "evm_inventory.cli.scan",
        lambda _st, _scope, progress=None: (
            progress("checking"),
            {"status": "completed"},
        )[1],
    )
    assert main(["scan", "--wallets", str(wallets), "--db", str(tmp_path / "db")]) == 0
    captured = capsys.readouterr()
    assert "checking\n" in captured.err
    summary = json.loads(captured.out)
    assert summary["status"] == "completed"
    assert summary["alchemy_key_present"] is False
    assert "run_id" not in captured.out


def test_export_accepts_current_database_without_run_argument(tmp_path):
    from evm_inventory.store import Store

    db = tmp_path / "inventory.db"
    with Store(db) as store:
        asset = store.upsert_asset(
            "0x" + "1" * 40,
            1,
            "native",
            metadata={"network_name": "Mainnet", "token_review_status": "verified"},
        )
        store.record_asset(asset, {"raw_balance": "1"})
    assert main(["export", "--db", str(db), "--output", str(tmp_path / "out")]) == 0


def test_resume_routes_prints_key_free_durable_status(tmp_path, capsys):
    from evm_inventory.journal import Journal

    journal_path = tmp_path / "journal.sqlite"
    with Journal(journal_path) as journal:
        group = journal.get_or_create_group(
            group_key="wallet:10:plan", wallet="0x" + "1" * 40,
            source_chain_id=10, loss_budget_pct="15", source_usd="100",
        )
        journal.record_group_state(
            group["id"], "manual_review_group_threshold_exceeded",
            reason="loss_threshold_exceeded",
        )
        position = journal.get_or_create_position(
            position_key="wallet:10:native", wallet="0x" + "1" * 40,
            group_id=group["id"],
        )
        journal.record_position_state(
            position["id"], "manual_review_after_swap",
            actual_asset_id="native", actual_balance_raw="123",
            reason="bridge requote is unavailable",
        )
        step = journal.record_step_intent(
            position_id=position["id"], step_key="bridge", nonce=3,
            calldata_digest="a" * 64, signed_payload_digest="b" * 64,
        )
        journal.record_broadcast_attempt(step["id"], "0x" + "c" * 64)
        journal.record_receipt(step["id"], status="confirmed", finality_block=123)
        journal.record_timeout_report(step["id"], "no_correlated_arrival")

    assert main(["resume-routes", "--journal", str(journal_path)]) == 0

    captured = capsys.readouterr()
    result = json.loads(captured.out)
    assert result == {
        "status": "read_only",
        "groups": {"manual_review_group_threshold_exceeded": 1},
        "positions": {"manual_review_after_swap": 1},
        "steps": {"bridge_timeout": 1},
        "reasons": [{"reason": "bridge requote is unavailable", "count": 1}],
        "bridge_timeout": 1,
        "manual_review": 0,
        "pending": 0,
    }
    assert "private" not in captured.out.lower()


@pytest.mark.parametrize(
    "command, required_options",
    (
        ("scan", ("--wallets", "wallets.txt", "--db", "inventory.db")),
        ("export", ("--db", "inventory.db", "--output", "out")),
    ),
)
def test_scan_and_export_reject_run_option(command, required_options, capsys):
    with pytest.raises(SystemExit):
        main([command, *required_options, "--run", "obsolete"])
    assert "unrecognized arguments: --run obsolete" in capsys.readouterr().err


def test_two_cli_scans_update_one_current_asset_with_stable_created_at(
    tmp_path, capsys, monkeypatch
):
    wallets = tmp_path / "wallets.txt"
    wallets.write_text("0x" + "1" * 40 + "\n")
    db = tmp_path / "inventory.db"
    ticks = iter((10.0, 11.0, 20.0, 21.0))
    monkeypatch.setattr("evm_inventory.store.time.time", lambda: next(ticks))
    calls = 0

    def deterministic_scan(store, scope, progress=None):
        nonlocal calls
        calls += 1
        wallet = scope["wallets"][0]
        asset = store.upsert_asset(
            wallet,
            1,
            "native",
            metadata={"network_name": "Mainnet", "token_review_status": "verified"},
        )
        store.record_asset(asset, {"raw_balance": str(calls), "decimals": 18})
        return {"status": "completed"}

    monkeypatch.setattr("evm_inventory.cli.scan", deterministic_scan)
    assert main(["scan", "--wallets", str(wallets), "--db", str(db)]) == 0
    capsys.readouterr()
    from evm_inventory.store import Store

    with Store(db, readonly=True) as store:
        first = store.assets()[0]
    assert main(["scan", "--wallets", str(wallets), "--db", str(db)]) == 0
    with Store(db, readonly=True) as store:
        assets = store.assets()
    assert len(assets) == 1
    second = assets[0]
    assert second["created_at"] == first["created_at"] == 10.0
    assert second["modified_at"] == 21.0 > first["modified_at"]
    assert second["result"]["raw_balance"] == "2"


def test_keyboard_interrupt_returns_130(tmp_path, capsys, monkeypatch):
    wallets = tmp_path / "wallets.txt"
    wallets.write_text("0x" + "1" * 40 + "\n")

    def interrupted(*_args, **_kwargs):
        raise KeyboardInterrupt

    monkeypatch.setattr("evm_inventory.cli.scan", interrupted)
    assert main(["scan", "--wallets", str(wallets), "--db", str(tmp_path / "db")]) == 130
    assert "interrupted" in capsys.readouterr().err


def test_execution_rpc_urls_prefers_alchemy(monkeypatch):
    monkeypatch.setenv("ALCHEMY_API_KEY", "key")

    class Network:
        chain_id = 10
        alchemy_network = "opt-mainnet"
        rpc_urls = ("https://public.example",)

    class Catalog:
        networks = (Network(),)

    assert _execution_rpc_urls(Catalog()) == {10: "https://opt-mainnet.g.alchemy.com/v2/key"}


def test_quote_routes_passes_loaded_route_loss_limit(tmp_path, capsys, monkeypatch):
    monkeypatch.setattr("evm_inventory.cli._load_dotenv", lambda: None)
    monkeypatch.setenv("MAX_ROUTE_LOSS_PCT", "7.25")
    wallet = SimpleNamespace(
        public_address="0x" + "1" * 40,
        bitget_deposit_address="0x" + "2" * 40,
    )
    monkeypatch.setattr("evm_inventory.cli.parse_ordinal_ranges", lambda _ranges: None)
    monkeypatch.setattr(
        "evm_inventory.cli.load_wallet_workbook",
        lambda *_args, **_kwargs: [wallet],
    )
    monkeypatch.setattr(
        "evm_inventory.cli.wallet_range_batches",
        lambda *_args, **_kwargs: ((wallet,),),
    )
    calls = {}

    def create_live_plan(*_args, **kwargs):
        calls.update(kwargs)
        return {"summary": {}, "entries": []}

    monkeypatch.setattr("evm_inventory.cli.create_live_plan", create_live_plan)

    assert main(
        [
            "quote-routes",
            "--balances",
            str(tmp_path / "balances.csv"),
            "--workbook",
            str(tmp_path / "wallets.xlsx"),
            "--output",
            str(tmp_path / "plan.json"),
        ]
    ) == 0

    assert calls["max_route_loss_pct"] == Decimal("7.25")
    capsys.readouterr()


def test_execute_routes_passes_loaded_route_loss_limit(tmp_path, capsys, monkeypatch):
    monkeypatch.setattr("evm_inventory.cli._load_dotenv", lambda: None)
    monkeypatch.setenv("MAX_ROUTE_LOSS_PCT", "8.5")
    wallet = SimpleNamespace(public_address="0x" + "1" * 40)
    plan = tmp_path / "plan.json"
    plan.write_text(json.dumps({"entries": [], "execution": {}}))
    monkeypatch.setattr("evm_inventory.cli.parse_ordinal_ranges", lambda _ranges: None)
    monkeypatch.setattr(
        "evm_inventory.cli.load_catalog",
        lambda _path: SimpleNamespace(networks=()),
    )
    monkeypatch.setattr(
        "evm_inventory.cli.load_wallet_workbook",
        lambda *_args, **_kwargs: [wallet],
    )
    monkeypatch.setattr(
        "evm_inventory.cli.wallet_range_batches",
        lambda *_args, **_kwargs: ((wallet,),),
    )
    calls = {}

    def execute_entries(_entries, **kwargs):
        calls.update(kwargs)
        return {"submitted": 0, "skipped": 0, "failed": 0, "deferred": 0}

    monkeypatch.setattr("evm_inventory.cli.execute_entries", execute_entries)

    assert main(
        [
            "execute-routes",
            "--plan",
            str(plan),
            "--workbook",
            str(tmp_path / "wallets.xlsx"),
            "--catalog",
            str(tmp_path / "catalog.json"),
            "--journal",
            str(tmp_path / "journal.sqlite"),
        ]
    ) == 0

    assert calls["max_route_loss_pct"] == Decimal("8.5")
    capsys.readouterr()
