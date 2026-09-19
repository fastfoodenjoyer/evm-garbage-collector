import multiprocessing
import sqlite3
from pathlib import Path

import pytest

from evm_inventory.store import Store


def test_snapshot_round_trip_and_status(tmp_path: Path):
    db = tmp_path / "inventory.db"
    with Store(db) as store:
        run_id = store.create_run({"wallets": ["0x1"], "n": 1})
        assert store.run(run_id) == {"run_id": run_id, "snapshot": {"wallets": ["0x1"], "n": 1}, "status": "pending"}
        store.set_status(run_id, "success")
        assert store.run(run_id)["status"] == "success"


def test_jobs_dedup_success_zero_and_huge_balance(tmp_path: Path):
    with Store(tmp_path / "db") as store:
        rid = store.create_run({})
        jid = store.ensure_job(rid, "0xabc", 1, "native")
        assert store.ensure_job(rid, "0xabc", 1, "native") == jid
        assert store.jobs(rid)[0]["status"] == "pending"
        store.record(jid, {"raw_balance": "100000000000000000000000000000000000000", "decimals": 18}, "success")
        row = store.jobs(rid)[0]
        assert row["result"]["raw_balance"].startswith("1000")
        assert row["attempts"] == 1 and row["status"] == "success"


def test_pass_is_immutable(tmp_path: Path):
    with Store(tmp_path / "db") as store:
        rid = store.create_run({})
        block = {"number": 5, "hash": "0x5", "timestamp": 10}
        store.save_pass(rid, "0xabc", 1, block)
        store.save_pass(rid, "0xabc", 1, {"number": 6, "hash": "0x6", "timestamp": 11})
        assert store.get_pass(rid, "0xabc", 1) == block
        with pytest.raises(ValueError):
            store.save_pass(rid, "0xabc", 1, {"number": 6, "hash": "0x6", "timestamp": 11})


def test_deferred_retry_after_is_numeric(tmp_path: Path):
    with Store(tmp_path / "db") as store:
        rid = store.create_run({})
        jid = store.ensure_job(rid, "0xabc", 1, "native")
        store.record(jid, {"error": "busy"}, "deferred", retry_after="123.5")
        assert store.jobs(rid)[0]["retry_after"] == 123.5


def test_discovery_page_atomically_creates_jobs_and_advances_cursor(tmp_path: Path):
    with Store(tmp_path / "db") as store:
        rid = store.create_run({})
        did = store.ensure_job(rid, "0xabc", 1, "discovery")
        store.save_discovery_page(did, [{"address": "0xtoken", "symbol": "T"}], "next")
        discovery = store.jobs(rid)[0]
        assert discovery["result"]["cursor"] == "next" and discovery["status"] == "pending"
        token = store.jobs(rid, wallet="0xabc", chain_id=1)
        assert len(token) == 2
        assert token[1]["metadata"]["address"] == "0xtoken"
        store.save_discovery_page(did, [], None)
        assert store.jobs(rid)[0]["status"] == "success"


def test_unknown_run_and_readonly_reject_writes(tmp_path: Path):
    db = tmp_path / "db"
    with Store(db) as store:
        rid = store.create_run({})
    with Store(db, readonly=True) as store:
        with pytest.raises(ValueError):
            store.run("missing")
        with pytest.raises(sqlite3.OperationalError):
            store.create_run({})


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


def test_future_schema_is_rejected_without_overwrite(tmp_path: Path):
    db = tmp_path / "db"
    with sqlite3.connect(db) as conn:
        conn.execute("PRAGMA user_version=2")
        conn.commit()
    with pytest.raises(ValueError):
        Store(db)
    with sqlite3.connect(db) as conn:
        assert conn.execute("PRAGMA user_version").fetchone()[0] == 2


def test_initialization_error_releases_writer_lock(tmp_path: Path):
    db = tmp_path / "db"
    with sqlite3.connect(db) as conn:
        conn.execute("PRAGMA user_version=2")
        conn.commit()
    with pytest.raises(ValueError):
        Store(db)
    with pytest.raises(ValueError):
        Store(db)
