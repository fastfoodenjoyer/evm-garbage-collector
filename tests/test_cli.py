import json

import pytest

from evm_inventory.cli import main
from evm_inventory.models import ConfigError


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


def test_scan_progress_and_final_summary_are_json_and_stderr(tmp_path, capsys, monkeypatch):
    wallets = tmp_path / "wallets.txt"
    wallets.write_text("0x" + "1" * 40 + "\n")
    monkeypatch.setattr(
        "evm_inventory.cli.scan",
        lambda _st, _run, progress=None: (
            progress("checking"),
            {"run_id": "run", "status": "completed"},
        )[1],
    )
    assert main(["scan", "--wallets", str(wallets), "--db", str(tmp_path / "db")]) == 0
    captured = capsys.readouterr()
    assert "checking\n" in captured.err
    assert json.loads(captured.out)["status"] == "completed"


def test_keyboard_interrupt_returns_130(tmp_path, capsys, monkeypatch):
    wallets = tmp_path / "wallets.txt"
    wallets.write_text("0x" + "1" * 40 + "\n")

    def interrupted(*_args, **_kwargs):
        raise KeyboardInterrupt

    monkeypatch.setattr("evm_inventory.cli.scan", interrupted)
    assert main(["scan", "--wallets", str(wallets), "--db", str(tmp_path / "db")]) == 130
    assert "interrupted" in capsys.readouterr().err
