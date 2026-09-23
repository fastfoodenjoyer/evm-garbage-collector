import hashlib
import json
import multiprocessing
import sqlite3
from pathlib import Path

import pytest

from evm_inventory.store import Store


def test_new_and_zero_byte_databases_initialize(tmp_path: Path):
    for name in ("new.db", "zero.db"):
        db = tmp_path / name
        if name == "zero.db":
            db.touch()
        with Store(db) as store:
            assert store.assets() == []
            assert store.database_uuid()
        with sqlite3.connect(db) as conn:
            assert conn.execute("PRAGMA user_version").fetchone()[0] == Store.SCHEMA_VERSION


def test_asset_upsert_normalizes_identity_and_preserves_creation_time(tmp_path, monkeypatch):
    ticks = iter((100.0, 101.0, 102.0))
    monkeypatch.setattr("evm_inventory.store.time.time", lambda: next(ticks))
    with Store(tmp_path / "db") as store:
        first = store.upsert_asset("0xAbC", 1, "0xToKeN", kind="catalog", metadata={"v": 1})
        second = store.upsert_asset("0xabc", 1, "0xtoken", kind="discovered", metadata={"v": 2})
        assert first == second
        asset = store.assets()[0]
        assert asset["wallet"] == "0xabc"
        assert asset["asset_id"] == "0xtoken"
        assert asset["kind"] == "discovered" and asset["metadata"] == {"v": 2}
        assert asset["created_at"] == 100.0 and asset["modified_at"] == 101.0


def test_assets_are_separate_by_wallet_chain_and_asset_and_have_stable_order(tmp_path):
    with Store(tmp_path / "db") as store:
        store.upsert_asset("0xB", 10, "0xF")
        store.upsert_asset("0xA", 10, "native")
        store.upsert_asset("0xA", 1, "0xE")
        assert [(a["wallet"], a["chain_id"], a["asset_id"]) for a in store.assets()] == [
            ("0xa", 1, "0xe"),
            ("0xa", 10, "native"),
            ("0xb", 10, "0xf"),
        ]


def test_record_asset_updates_result_attempts_retry_and_modified_time(tmp_path, monkeypatch):
    ticks = iter((100.0, 101.0))
    monkeypatch.setattr("evm_inventory.store.time.time", lambda: next(ticks))
    with Store(tmp_path / "db") as store:
        asset_id = store.upsert_asset("0xabc", 1, "native")
        store.record_asset(asset_id, {"raw_balance": "0"}, "deferred", retry_after="123.5")
        asset = store.assets()[0]
        assert asset["result"] == {"raw_balance": "0"}
        assert asset["status"] == "deferred" and asset["attempts"] == 1
        assert asset["retry_after"] == 123.5 and asset["modified_at"] == 101.0


def test_discovery_state_is_one_row_per_normalized_wallet_and_network(tmp_path, monkeypatch):
    ticks = iter((100.0, 101.0, 102.0))
    monkeypatch.setattr("evm_inventory.store.time.time", lambda: next(ticks))
    with Store(tmp_path / "db") as store:
        store.write_discovery_state(
            "0xAbC", 1, cursor="page-1", cursor_history=[None, "page-1"], status="pending",
            retry_after=11, result={"seen": 3}, error={"reason": "busy"},
        )
        store.write_discovery_state(
            "0xabc",
            1,
            cursor="page-2",
            cursor_history=[None, "page-1", "page-2"],
            status="deferred",
            retry_after="12.5", result={"seen": 4}, error={"reason": "rate_limited"},
        )
        store.write_discovery_state("0xabc", 10, status="pending")
        state = store.discovery_state("0xABC", 1)
        assert state == {
            "wallet": "0xabc", "chain_id": 1, "cursor": "page-2",
            "cursor_history": [None, "page-1", "page-2"], "status": "deferred",
            "retry_after": 12.5, "result": {"seen": 4}, "error": {"reason": "rate_limited"},
            "created_at": 100.0, "modified_at": 101.0,
        }
        assert len(store.discovery_states()) == 2


def test_passes_are_replaceable_and_keep_timestamps(tmp_path, monkeypatch):
    ticks = iter((100.0, 101.0))
    monkeypatch.setattr("evm_inventory.store.time.time", lambda: next(ticks))
    with Store(tmp_path / "db") as store:
        store.save_pass("0xABC", 1, {"number": 5})
        store.save_pass("0xabc", 1, {"number": 6})
        assert store.get_pass("0xAbC", 1) == {"number": 6}
        row = store.passes()[0]
        assert row["created_at"] == 100.0 and row["modified_at"] == 101.0


def test_database_uuid_is_stable_across_reopen(tmp_path):
    db = tmp_path / "db"
    with Store(db) as store:
        database_uuid = store.database_uuid()
    with Store(db) as store:
        assert store.database_uuid() == database_uuid


def test_defi_plan_and_execution_events_are_persisted_without_keys(tmp_path):
    db = tmp_path / "db"
    action_id = "a" * 64
    plan = {
        "schema": "rabby-defi-withdraw-v1", "created_at": 100,
        "entries": [{
            "wallet": "0xabc", "chain_id": 1, "protocol_id": "vault",
            "pool_id": "pool", "position_index": "", "status": "ready",
            "reason": "", "action_id": action_id, "action": {"func": "withdraw()"},
        }],
        "summary": {"ready": 1, "manual_review": 0},
    }
    plan_bytes = (json.dumps(plan) + "\n").encode()
    digest = hashlib.sha256(plan_bytes).hexdigest()
    with Store(db) as store:
        store.save_defi_plan(digest, plan_bytes)
        store.record_defi_execution(
            digest, action_id, {
                "status": "manual_review", "action_id": action_id,
                "tx_hash": "0x" + "b" * 64,
            }
        )
    with Store(db, readonly=True) as store:
        assert store.defi_plan(digest) == plan_bytes
        assert store.defi_positions(digest)[0]["action_id"] == action_id
        assert store.defi_execution_events(digest)[0]["result"]["tx_hash"] == "0x" + "b" * 64
    assert "private_key" not in db.read_bytes().decode(errors="ignore")


def test_v2_inventory_is_migrated_without_losing_assets(tmp_path):
    db = tmp_path / "db"
    with Store(db) as store:
        original_uuid = store.database_uuid()
        store.upsert_asset("0xabc", 1, "native")
    with sqlite3.connect(db) as conn:
        conn.execute("DROP TABLE run_artifacts")
        conn.execute("DROP TABLE defi_execution_events")
        conn.execute("DROP TABLE defi_positions")
        conn.execute("DROP TABLE defi_plans")
        conn.execute("PRAGMA user_version=2")
        conn.commit()
    with Store(db) as store:
        assert store.database_uuid() == original_uuid
        assert len(store.assets()) == 1
        assert store.defi_positions("0" * 64) == []
    with sqlite3.connect(db) as conn:
        assert conn.execute("PRAGMA user_version").fetchone()[0] == Store.SCHEMA_VERSION


def test_route_artifact_is_kept_in_the_inventory_database(tmp_path):
    db = tmp_path / "inventory.sqlite"
    payload = b'{"entries":[]}\n'
    with Store(db) as store:
        digest = store.save_run_artifact("route_plan", payload)
        assert store.run_artifact("route_plan", payload) == digest
    with Store(db, readonly=True) as store:
        assert store.run_artifact_bytes("route_plan", digest) == payload


def test_v3_database_gains_run_artifacts_without_losing_defi(tmp_path):
    db = tmp_path / "inventory.sqlite"
    plan = {"schema": "rabby-defi-withdraw-v1", "created_at": 100, "entries": []}
    payload = json.dumps(plan).encode()
    digest = hashlib.sha256(payload).hexdigest()
    with Store(db) as store:
        store.save_defi_plan(digest, payload)
    with sqlite3.connect(db) as conn:
        conn.execute("DROP TABLE run_artifacts")
        conn.execute("PRAGMA user_version=3")
        conn.commit()
    with Store(db) as store:
        assert store.defi_plan(digest) == payload
        assert store.save_run_artifact("route_plan", b'{"entries":[]}')


def test_inventory_and_transaction_journal_share_one_sqlite_file(tmp_path):
    from evm_inventory.journal import Journal

    db = tmp_path / "inventory.sqlite"
    with Store(db) as store:
        store.upsert_asset("0xabc", 1, "native")
        store.save_run_artifact("route_plan", b'{"entries":[]}')
    with Journal(db) as journal:
        position = journal.get_or_create_position(position_key="route:abc", wallet="0xabc")
        journal.record_step_intent(
            position_id=position["id"], step_key="swap", nonce=1,
            calldata_digest="a" * 64, signed_payload_digest="b" * 64,
        )
    with Store(db, readonly=True) as store:
        assert len(store.assets()) == 1
        assert store.run_artifact("route_plan", b'{"entries":[]}')
    with Journal(db, readonly=True) as journal:
        assert journal.route_step_state_counts() == {"planned": 1}
    assert list(tmp_path.glob("*.sqlite")) == [db]


def test_populated_zero_version_database_is_rejected_without_modification(tmp_path):
    db = tmp_path / "db"
    with sqlite3.connect(db) as conn:
        conn.execute("CREATE TABLE existing (value TEXT)")
        conn.execute("INSERT INTO existing VALUES ('keep')")
        conn.commit()
    with pytest.raises(ValueError, match="unsupported schema"):
        Store(db)
    with sqlite3.connect(db) as conn:
        assert conn.execute("PRAGMA user_version").fetchone()[0] == 0
        assert conn.execute("SELECT value FROM existing").fetchone()[0] == "keep"


def test_empty_nonzero_version_database_is_rejected_without_modification(tmp_path):
    db = tmp_path / "db"
    with sqlite3.connect(db) as conn:
        conn.execute("PRAGMA user_version=99")
        conn.commit()
    with pytest.raises(ValueError, match="unsupported schema"):
        Store(db)
    with sqlite3.connect(db) as conn:
        assert conn.execute("PRAGMA user_version").fetchone()[0] == 99
        assert conn.execute("SELECT count(*) FROM sqlite_master").fetchone()[0] == 0


def test_v2_database_without_schema_is_rejected(tmp_path):
    db = tmp_path / "db"
    with sqlite3.connect(db) as conn:
        conn.execute("PRAGMA user_version=2")
        conn.commit()
    with pytest.raises(ValueError, match="missing required tables"):
        Store(db)


def test_v2_database_without_metadata_uuid_is_rejected(tmp_path):
    db = tmp_path / "db"
    with Store(db):
        pass
    with sqlite3.connect(db) as conn:
        conn.execute("DELETE FROM metadata WHERE key='database_uuid'")
        conn.commit()
    with pytest.raises(ValueError, match="database UUID"):
        Store(db)


def test_v2_database_without_asset_identity_constraint_is_rejected(tmp_path):
    db = tmp_path / "db"
    with Store(db):
        pass
    with sqlite3.connect(db) as conn:
        conn.execute(
            """
            CREATE TABLE replacement_assets (
                id INTEGER PRIMARY KEY,
                wallet_id INTEGER NOT NULL,
                chain_id INTEGER NOT NULL,
                asset_id TEXT NOT NULL,
                kind TEXT NOT NULL,
                metadata TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'pending',
                attempts INTEGER NOT NULL DEFAULT 0,
                retry_after REAL,
                result TEXT,
                created_at REAL NOT NULL,
                modified_at REAL NOT NULL
            )
            """
        )
        conn.execute("DROP TABLE assets")
        conn.execute("ALTER TABLE replacement_assets RENAME TO assets")
        conn.commit()
    with pytest.raises(ValueError, match="assets identity constraint"):
        Store(db)


def test_prior_v1_run_schema_is_rejected_without_modification(tmp_path):
    db = tmp_path / "db"
    with sqlite3.connect(db) as conn:
        conn.execute("CREATE TABLE runs (run_id TEXT PRIMARY KEY)")
        conn.execute("PRAGMA user_version=1")
        conn.commit()
    with pytest.raises(ValueError, match="unsupported schema"):
        Store(db)
    with sqlite3.connect(db) as conn:
        assert conn.execute("PRAGMA user_version").fetchone()[0] == 1
        row = conn.execute(
            "SELECT name FROM sqlite_master WHERE name='runs'"
        ).fetchone()
        assert row[0] == "runs"


def test_readonly_rejects_writes(tmp_path: Path):
    db = tmp_path / "db"
    with Store(db):
        pass
    with Store(db, readonly=True) as store:
        with pytest.raises(sqlite3.OperationalError):
            store.upsert_asset("0xabc", 1, "native")


def _open_writer(path: str, queue):
    try:
        Store(path)
    except Exception as exc:  # pragma: no cover - process boundary
        queue.put(type(exc).__name__)
    else:
        queue.put("ok")


def test_concurrent_writer_refusal(tmp_path: Path):
    db = tmp_path / "db"
    first = Store(db)
    queue = multiprocessing.Queue()
    process = multiprocessing.Process(target=_open_writer, args=(str(db), queue))
    process.start()
    process.join(5)
    assert queue.get(timeout=2) in {"OperationalError", "RuntimeError"}
    first.close()
