import csv
import json
from pathlib import Path

import pytest

from evm_inventory.report import export_current
from evm_inventory.store import Store

WALLET = "0x" + "a" * 40
TOKEN = "0x" + "b" * 40


def _mandatory(network="Mainnet", review="verified"):
    return {"network_name": network, "token_review_status": review}


def test_export_current_reads_retained_balance_assets_and_excludes_discovery_state(tmp_path: Path):
    with Store(tmp_path / "db") as store:
        native = store.upsert_asset(
            WALLET, 1, "native", metadata={**_mandatory(), "symbol": "ETH", "decimals": 18}
        )
        discovered = store.upsert_asset(WALLET, 1, TOKEN, "discovered", {"symbol": "USDC"})
        store.record_asset(native, {"raw_balance": "0", "decimals": 18})
        store.record_asset(
            discovered,
            {"raw_balance": "7", "decimals": 6, "symbol": "=USDC"},
            "provider_only",
        )
        store.write_discovery_state(WALLET, 1, status="success")
        export_current(store, tmp_path / "out")

    payload = json.loads((tmp_path / "out" / "inventory.json").read_text())
    assert [row["asset_id"] for row in payload["balances"]] == [TOKEN]
    assert "discovery_state" not in payload
    assert payload["coverage"] == [
        {
            "wallet": WALLET,
            "chain_id": 1,
            "network": "Mainnet",
            "mandatory_status": "complete",
            "token_review_status": "verified",
            "discovery_status": "success",
            "discovered_verification_failures": 1,
        }
    ]
    with (tmp_path / "out" / "balances.csv").open(newline="") as stream:
        assert next(csv.DictReader(stream))["symbol"] == "'=USDC"


def test_export_current_preserves_coverage_for_retained_mandatory_asset(tmp_path: Path):
    with Store(tmp_path / "db") as store:
        asset = store.upsert_asset(
            WALLET, 10, "native", metadata={**_mandatory("Optimism", "pending"), "symbol": "ETH"}
        )
        store.record_asset(asset, {"raw_balance": "1", "decimals": 18})
        export_current(store, tmp_path / "out")

    coverage = json.loads((tmp_path / "out" / "inventory.json").read_text())["coverage"]
    assert coverage[0]["network"] == "Optimism"
    assert coverage[0]["token_review_status"] == "pending"
    assert coverage[0]["mandatory_status"] == "incomplete"


def test_export_current_reuses_only_output_marked_by_same_database(tmp_path: Path):
    output = tmp_path / "out"
    with Store(tmp_path / "one.db") as first:
        asset = first.upsert_asset(WALLET, 1, "native", metadata=_mandatory())
        first.record_asset(asset, {"raw_balance": "1"})
        export_current(first, output)
        export_current(first, output)
        assert (output / ".inventory-database").read_text().strip() == first.database_uuid()

    with Store(tmp_path / "two.db") as second:
        with pytest.raises(ValueError, match="another database"):
            export_current(second, output)


def test_export_current_refuses_unrelated_existing_output(tmp_path: Path):
    output = tmp_path / "out"
    output.mkdir()
    (output / "keep.txt").write_text("do not overwrite")
    with Store(tmp_path / "db") as store:
        with pytest.raises(ValueError, match="unrelated"):
            export_current(store, output)
    assert (output / "keep.txt").read_text() == "do not overwrite"
