import csv
import json
from pathlib import Path

import pytest

from evm_inventory.report import export_run
from evm_inventory.store import Store

WALLET = "0x" + "a" * 40


def _snapshot(review="verified"):
    return {
        "wallets": [WALLET],
        "settings": {},
        "catalog": {
            "networks": [
                {
                    "chain_id": 1,
                    "name": "Mainnet",
                    "native_symbol": "ETH",
                    "native_decimals": 18,
                    "token_review_status": review,
                    "tokens": [
                        {
                            "address": "0x" + "b" * 40,
                            "symbol": "=FORMULA",
                            "decimals": 6,
                            "source": "test",
                            "checked_at": "now",
                        }
                    ],
                }
            ]
        },
    }


def test_export_synthesizes_pending_checks_after_interruption(tmp_path: Path):
    with Store(tmp_path / "db") as store:
        run_id = store.create_run(_snapshot())
        export_run(store, run_id, tmp_path / "out")
    payload = json.loads((tmp_path / "out" / "inventory.json").read_text())
    assert len(payload["checks"]) == 2
    assert {row["status"] for row in payload["checks"]} == {"pending"}
    assert payload["coverage"][0]["mandatory_status"] == "incomplete"


def test_pending_catalog_review_prevents_complete_coverage(tmp_path: Path):
    with Store(tmp_path / "db") as store:
        run_id = store.create_run(_snapshot(review="pending"))
        native = store.ensure_job(run_id, WALLET, 1, "native")
        token = store.ensure_job(run_id, WALLET, 1, "0x" + "b" * 40)
        discovery = store.ensure_job(run_id, WALLET, 1, "discovery", kind="discovery")
        store.record(native, {"raw_balance": "0", "decimals": 18}, "success")
        store.record(token, {"raw_balance": "0", "decimals": 6}, "success")
        store.record(discovery, {}, "success")
        export_run(store, run_id, tmp_path / "out")
    coverage = json.loads((tmp_path / "out" / "inventory.json").read_text())["coverage"][0]
    assert coverage["mandatory_status"] == "incomplete"
    assert coverage["discovery_status"] == "success"


def test_unverified_discovered_positive_is_excluded_and_missing_price_counted(tmp_path: Path):
    with Store(tmp_path / "db") as store:
        run_id = store.create_run(_snapshot())
        native = store.ensure_job(run_id, WALLET, 1, "native")
        token = store.ensure_job(run_id, WALLET, 1, "0x" + "b" * 40)
        discovery = store.ensure_job(run_id, WALLET, 1, "discovery", kind="discovery")
        extra = store.ensure_job(
            run_id, WALLET, 1, "0x" + "c" * 40, kind="discovered", metadata={"symbol": "X"}
        )
        store.record(
            native,
            {
                "raw_balance": "1" + "0" * 100,
                "decimals": 18,
                "amount": "100000000000000000000000000000",
                "price_usd": "2.5",
            },
        )
        store.record(token, {"raw_balance": "7", "decimals": 0, "amount": "7"})
        store.record(discovery, {}, "success")
        store.record(extra, {"reported_raw_balance": "99", "price_usd": "4"}, "error")
        export_run(store, run_id, tmp_path / "out")
    payload = json.loads((tmp_path / "out" / "inventory.json").read_text())
    assert len(payload["balances"]) == 2
    assert payload["summary"]["unpriced_balance_count"] == 1
    assert payload["summary"]["priced_total_usd"] == "250000000000000000000000000000.0"


def test_export_refuses_unrelated_existing_output(tmp_path: Path):
    output = tmp_path / "out"
    output.mkdir()
    (output / "keep.txt").write_text("do not overwrite")
    with Store(tmp_path / "db") as store:
        run_id = store.create_run(_snapshot())
        with pytest.raises(ValueError, match="unrelated"):
            export_run(store, run_id, output)
    assert (output / "keep.txt").read_text() == "do not overwrite"


def test_checks_csv_escapes_formula_symbol(tmp_path: Path):
    with Store(tmp_path / "db") as store:
        run_id = store.create_run(_snapshot())
        export_run(store, run_id, tmp_path / "out")
    with (tmp_path / "out" / "checks.csv").open(newline="") as stream:
        rows = list(csv.DictReader(stream))
    assert rows[1]["symbol"] == "'=FORMULA"


def test_export_keeps_token_name_and_provider_metadata(tmp_path: Path):
    with Store(tmp_path / "db") as store:
        run_id = store.create_run(_snapshot())
        token = store.ensure_job(run_id, WALLET, 1, "0x" + "b" * 40)
        store.record(
            token,
            {
                "raw_balance": "1230000",
                "decimals": 6,
                "amount": "1.23",
                "name": "USD Coin",
                "symbol": "USDC",
                "source": "alchemy",
                "verification": "provider_only",
                "price_usd": "1",
                "price_source": "alchemy",
                "price_timestamp": "2026-09-19T00:00:00Z",
            },
            "provider_only",
        )
        export_run(store, run_id, tmp_path / "out")
    payload = json.loads((tmp_path / "out" / "inventory.json").read_text())
    row = next(row for row in payload["balances"] if row["asset_id"] != "native")
    assert row["name"] == "USD Coin"
    assert row["source"] == "alchemy"
    assert row["verification"] == "provider_only"
    with (tmp_path / "out" / "balances.csv").open(newline="") as stream:
        csv_row = next(csv.DictReader(stream))
    assert csv_row["name"] == "USD Coin"
