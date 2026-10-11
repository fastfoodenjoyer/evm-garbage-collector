"""Scan, plan, submit and settle a wallet in one bounded, resumable process."""

from __future__ import annotations

import csv
import json
import math
import os
import time
from collections import Counter
from hashlib import sha256
from pathlib import Path
from uuid import uuid4

from .config import add_allowlisted_tokens, snapshot
from .journal import Journal
from .live_plan import create_live_plan
from .planner_gas import PlannerGasEstimator
from .report import export_current
from .route_execution import _position_key, execute_entries
from .rpc import RpcReader
from .scanner import scan
from .stages import Deadline, StageDeadlineExceeded, deadline_scope
from .store import Store
from .transport import Transport
from .wallet_lifi import WalletLifiClients

_READY = {"direct_deposit", "route_ready"}
_SKIP_REASONS = {
    "insufficient_source_gas",
    "below_quote_floor",
    "below_target_minimum",
    "max_route_loss_exceeded",
    "loss_threshold_exceeded",
    "relay_deposit_minimum_or_gas_reserve",
}
_PERMANENT_REASONS = {
    "unsupported_by_relay",
    "unsupported_by_lifi",
    "policy_review",
    "balance_not_rpc_verified",
    "asset_decimals_unavailable",
    "conflicting_native_balance",
    "missing_deposit_address",
}


def _unresolved(entry):
    return (
        entry.get("status") not in _READY
        and entry.get("status") not in {"denied", "dust", "below_quote_floor"}
        and entry.get("reason") not in _SKIP_REASONS
    )


def _assign_collection_identities(plan, run_id, *, locked_chains=()):
    for entry in plan["entries"]:
        if (
            entry.get("routing_provider") == "relay"
            and not entry.get("group")
            and entry["chain_id"] not in locked_chains
        ):
            entry["group"] = {"key": f"{run_id}:{entry['wallet']}:{entry['chain_id']}:relay"}


def recover_planning(
    plan,
    balances_path,
    quote,
    *,
    until,
    clock=time.monotonic,
    wall_clock=time.time,
    sleep=time.sleep,
    progress=None,
    locked_chains=(),
):
    """Retry unfinished source groups in this invocation without requerying completed groups."""
    attempt = 0
    while clock() < until:
        unresolved = [
            e
            for e in plan["entries"]
            if _unresolved(e)
            and e["chain_id"] not in locked_chains
            and e.get("reason") not in _PERMANENT_REASONS
        ]
        if not unresolved or clock() >= until:
            break
        times = [
            p["retry_at"]
            for e in unresolved
            for p in e.get("search", {}).get("pending", [])
            if isinstance(p.get("retry_at"), (int, float))
        ]
        backoff = min(60, 2 ** min(attempt, 6))
        delay = max(backoff, min(times) - wall_clock()) if times else backoff
        if delay >= until - clock():
            if progress:
                progress("Provider retry is outside this run's deadline; preserving exact reasons")
            break
        if progress:
            progress(f"Retrying {len(unresolved)} unresolved positions in {delay:.0f} seconds")
        end = clock() + delay
        while clock() < end:
            sleep(min(1, end - clock()))
            if progress and end - clock() > 60 and int(end - clock()) % 60 == 0:
                progress(f"Provider cooldown: {end - clock():.0f} seconds remaining")
        chains = {e["chain_id"] for e in unresolved}
        with Path(balances_path).open(newline="") as stream:
            reader = csv.DictReader(stream)
            rows = [r for r in reader if int(r["chain_id"]) in chains]
            fields = reader.fieldnames
        retry_path = Path(balances_path).parent.parent / "retry-balances.csv"
        with retry_path.open("w", newline="") as stream:
            writer = csv.DictWriter(stream, fieldnames=fields)
            writer.writeheader()
            writer.writerows(rows)
        refreshed = quote(retry_path)
        plan = {
            **plan,
            "entries": [e for e in plan["entries"] if e["chain_id"] not in chains]
            + refreshed["entries"],
        }
        plan["summary"] = dict(Counter(e["status"] for e in plan["entries"]))
        plan.setdefault("recovery_attempts", []).append(
            {
                "attempt": attempt + 1,
                "chains": sorted(chains),
                "diagnostics": {
                    k: refreshed[k]
                    for k in (
                        "gas_diagnostics",
                        "route_diagnostics",
                        "lifi_requests",
                        "relay_requests",
                    )
                    if k in refreshed
                },
            }
        )
        attempt += 1
    return plan


def trust_recipient(store, wallet, recipient, *, explicit):
    """Remember operator verification for this exact wallet/address, never a wildcard."""
    store.db.execute(
        "CREATE TABLE IF NOT EXISTS collection_recipients "
        "(wallet TEXT PRIMARY KEY, recipient TEXT NOT NULL)"
    )
    row = store.db.execute(
        "SELECT recipient FROM collection_recipients WHERE wallet=?", (wallet.lower(),)
    ).fetchone()
    if explicit:
        with store.db:
            store.db.execute(
                "INSERT OR REPLACE INTO collection_recipients VALUES (?,?)",
                (wallet.lower(), recipient.lower()),
            )
    elif not row or row[0] != recipient.lower():
        raise ValueError("Use --trust-deposit-addresses once after verifying this exact recipient")


def execution_positions(entries, db_path):
    result = []
    with Journal(db_path) as journal:
        for entry in entries:
            if entry.get("status") not in _READY:
                continue
            key = _position_key(entry)
            row = journal.connection.execute(
                "SELECT p.state,p.reason,g.state AS group_state,g.reason AS group_reason "
                "FROM route_positions p "
                "LEFT JOIN route_groups g ON p.group_id=g.id WHERE p.position_key=?",
                (key,),
            ).fetchone()
            state, reason = (row["state"], row["reason"]) if row else ("planned", None)
            if row and str(row["group_state"]).startswith("manual_review"):
                state = row["group_state"]
                reason = reason or row["group_reason"]
            result.append(
                {
                    "position_key": key,
                    "wallet": entry["wallet"],
                    "chain_id": entry["chain_id"],
                    "asset_id": entry["asset_id"],
                    "state": state,
                    "reason": reason,
                }
            )
    return result


def drive_execution(
    plan, db_path, *, execute_round, until, clock=time.monotonic, sleep=time.sleep, progress=None
):
    """Continue the same immutable plan; known broadcasts are observed by its journal."""
    rounds = 0
    last_progress = None
    last_result = {}
    while True:
        positions = execution_positions(plan["entries"], db_path)
        completed = sum(p["state"] == "completed" for p in positions)
        pending = {
            p["position_key"]
            for p in positions
            if p["state"] != "completed" and not p["state"].startswith("manual_review")
        }
        failure = any(p["state"].startswith("manual_review") for p in positions)
        if not pending or clock() >= until:
            return {
                "status": "incomplete" if pending or failure else "completed",
                "reason": "collection_deadline_exceeded"
                if pending
                else "execution_failed"
                if failure
                else None,
                "completed": completed,
                "rounds": rounds,
                "positions": positions,
                "last_round": last_result,
            }
        active = [
            e for e in plan["entries"] if e.get("status") in _READY and _position_key(e) in pending
        ]
        blocked_chains = {
            p["chain_id"]
            for p in positions
            if p["asset_id"] != "native" and p["state"] != "completed"
        }
        active = [
            e
            for e in active
            if not (e.get("routing_provider") == "relay" and e["chain_id"] in blocked_chains)
        ]
        if not active and failure:
            return {
                "status": "incomplete",
                "reason": "execution_failed",
                "completed": completed,
                "rounds": rounds,
                "positions": positions,
                "last_round": last_result,
            }
        # Keep every member of a partially settled group for its original loss budget.
        groups = {e.get("group", {}).get("key") for e in active if e.get("group")}
        active_keys = {_position_key(e) for e in active}
        active = [
            e
            for e in plan["entries"]
            if e.get("status") in _READY
            and (_position_key(e) in active_keys or e.get("group", {}).get("key") in groups)
        ]
        state = tuple((p["position_key"], p["state"], p["reason"]) for p in positions)
        if progress and state != last_progress:
            progress(f"Settlement: {completed}/{len(positions)} completed; {len(pending)} pending")
            last_progress = state
        try:
            with deadline_scope(
                Deadline(min(180, until - clock()), "collection_execution", max_seconds=86_400)
            ):
                last_result = execute_round(active) if active else {}
        except StageDeadlineExceeded:
            last_result = {"deferred": len(active)}
        rounds += 1
        if clock() < until:
            sleep(min(3, until - clock()))


def _quote_wallet(
    balances_path,
    row,
    *,
    db_path,
    catalog,
    rpc_urls,
    allowlist,
    target_policy,
    loss_limit,
    quote_floor,
    seconds,
):
    clients = WalletLifiClients(
        (row,),
        db_path=db_path,
        policy_digest=target_policy.digest,
        quota_wait_seconds=min(10, seconds),
    )
    transport = Transport(interval=0)
    try:
        return create_live_plan(
            balances_path,
            deposit_addresses={row.public_address: row.bitget_deposit_address},
            allowlist_path=allowlist,
            target_policy=target_policy,
            wallet_addresses={row.public_address},
            max_route_loss_pct=loss_limit,
            client=clients,
            gas_estimator=PlannerGasEstimator(RpcReader(transport), rpc_urls, clients),
            quote_floor=quote_floor,
            allow_partial_search=True,
            runtime_seconds=seconds,
        )
    finally:
        clients.close()
        transport.close()


def _save_run(store, run_id, *, status, plan=None, report=None):
    with store.db:
        store.db.execute(
            "UPDATE collection_runs SET status=?, plan_json=COALESCE(?,plan_json), "
            "report_json=COALESCE(?,report_json),updated_at=? WHERE id=?",
            (
                status,
                json.dumps(plan) if plan is not None else None,
                json.dumps(report) if report is not None else None,
                time.time(),
                run_id,
            ),
        )


def collect_wallets(
    rows,
    *,
    db_path,
    output,
    catalog,
    rpc_urls,
    allowlist,
    target_policy,
    loss_limit,
    execute=False,
    trust_deposit_addresses=False,
    max_runtime_seconds=10800,
    quote_floor="0.01",
    progress=None,
    scan_fn=scan,
    quote_fn=_quote_wallet,
    execute_fn=execute_entries,
    clock=time.monotonic,
    sleep=time.sleep,
):
    """One CLI invocation owns the inventory writer and all wallet continuation state."""
    if not math.isfinite(max_runtime_seconds) or not 1 <= max_runtime_seconds <= 86_400:
        raise ValueError("collection runtime must be finite and between 1 and 86400 seconds")
    db_path, output, allowlist = Path(db_path), Path(output), Path(allowlist)
    output.mkdir(parents=True, exist_ok=True)
    reports = []
    with Store(db_path) as store:
        store.db.execute(
            "CREATE TABLE IF NOT EXISTS collection_runs "
            "(id TEXT PRIMARY KEY,wallet TEXT,request_digest TEXT,status TEXT,"
            "plan_json TEXT,report_json TEXT,updated_at REAL)"
        )
        store.db.commit()
        for row in rows:
            if execute:
                trust_recipient(
                    store,
                    row.public_address,
                    row.bitget_deposit_address,
                    explicit=trust_deposit_addresses,
                )
            until = clock() + max_runtime_seconds
            planning_until = until - min(600, max_runtime_seconds / 3) if execute else until
            request = {
                "wallet": row.public_address,
                "recipient": row.bitget_deposit_address,
                "targets": target_policy.digest,
                "loss_limit": str(loss_limit),
                "allowlist": sha256(allowlist.read_bytes()).hexdigest(),
                "quote_floor": quote_floor,
            }
            digest = sha256(json.dumps(request, sort_keys=True).encode()).hexdigest()
            # A nonterminal plan is never replaced on restart: it may already own funds in flight.
            previous = store.db.execute(
                "SELECT * FROM collection_runs WHERE wallet=? AND status IN "
                "('executing','incomplete','planned') AND plan_json IS NOT NULL "
                "ORDER BY updated_at DESC "
                "LIMIT 1",
                (row.public_address,),
            ).fetchone()
            if previous:
                old_plan = json.loads(previous["plan_json"])
                pending_broadcast = False
                with Journal(db_path) as journal:
                    for entry in old_plan["entries"]:
                        position = journal.connection.execute(
                            "SELECT id,state FROM route_positions WHERE position_key=?",
                            (_position_key(entry),),
                        ).fetchone()
                        if (
                            position
                            and position["state"] != "completed"
                            and journal.has_broadcast(position["id"])
                        ):
                            pending_broadcast = True
                if not pending_broadcast:
                    _save_run(store, previous["id"], status="superseded")
                    previous = None
            if previous and previous["request_digest"] != digest:
                raise ValueError(
                    "Unfinished collection exists with a different recipient or policy"
                )
            known_keys = (
                {_position_key(e) for e in json.loads(previous["plan_json"])["entries"]}
                if previous
                else set()
            )
            with Journal(db_path) as journal:
                outstanding = journal.connection.execute(
                    "SELECT DISTINCT p.position_key FROM route_positions p JOIN route_steps s "
                    "ON s.position_id=p.id WHERE p.wallet=? AND p.state<>'completed' "
                    "AND s.tx_hash IS NOT NULL",
                    (row.public_address.lower(),),
                ).fetchall()
            foreign = [p[0] for p in outstanding if p[0] not in known_keys]
            if foreign:
                raise ValueError(
                    "unresolved_wallet_execution: resume the existing saved plan "
                    "before creating a new collection: " + ", ".join(foreign)
                )
            run_id = previous["id"] if previous else uuid4().hex
            wallet_output = output / f"wallet-{row.ordinal}"
            wallet_output.mkdir(exist_ok=True)
            scan_result = None
            if previous:
                plan = json.loads(previous["plan_json"])
                export_current(store, wallet_output / "inventory")
                if progress:
                    progress(f"Wallet {row.ordinal}: resuming saved collection {run_id}")
            else:
                with store.db:
                    store.db.execute(
                        "INSERT INTO collection_runs VALUES (?,?,?,'scanning',NULL,NULL,?)",
                        (run_id, row.public_address, digest, time.time()),
                    )
                if progress:
                    progress(f"Wallet {row.ordinal}: scanning all configured networks")
                for attempt in range(3):
                    scope = snapshot(
                        catalog,
                        (row.public_address,),
                        {
                            "discovery_enabled": bool(os.environ.get("ALCHEMY_API_KEY")),
                            "stage_seconds": max(0.1, until - clock()),
                            "workers": 16,
                            "complete_scan": True,
                            "delay_min": 0,
                            "delay_max": 0,
                        },
                    )
                    scope = add_allowlisted_tokens(scope, allowlist)
                    scan_result = scan_fn(store, scope, progress=progress)
                    failed = (
                        scan_result.get("mandatory_failed", 0)
                        + scan_result.get("discovered_failed", 0)
                        + sum(
                            scan_result.get("discovery_statuses", {}).get(s, 0)
                            for s in ("error", "deferred", "pending", "unavailable")
                        )
                    )
                    if not failed and not scan_result.get("timed_out"):
                        break
                    if attempt == 2 or clock() >= until:
                        break
                    sleep(min(2**attempt, max(0, until - clock())))
                if failed or scan_result.get("timed_out"):
                    report = {
                        "status": "incomplete",
                        "wallet": row.public_address,
                        "reason": "inventory_incomplete",
                        "scan": scan_result,
                    }
                    _save_run(store, run_id, status="scan_failed", report=report)
                    reports.append(report)
                    continue
                export_current(store, wallet_output / "inventory")
                if progress:
                    progress(f"Wallet {row.ordinal}: selecting verified routes and reserving gas")
                plan = quote_fn(
                    wallet_output / "inventory" / "balances.csv",
                    row,
                    db_path=db_path,
                    catalog=catalog,
                    rpc_urls=rpc_urls,
                    allowlist=allowlist,
                    target_policy=target_policy,
                    loss_limit=loss_limit,
                    quote_floor=quote_floor,
                    seconds=max(0.1, planning_until - clock()),
                )
            locked_chains = set()
            if not previous:
                _assign_collection_identities(plan, run_id)
            with Journal(db_path) as journal:
                for entry in plan["entries"]:
                    position = journal.connection.execute(
                        "SELECT id FROM route_positions WHERE position_key=?",
                        (_position_key(entry),),
                    ).fetchone()
                    if position and journal.has_broadcast(position[0]):
                        locked_chains.add(entry["chain_id"])
            balances_path = wallet_output / "inventory" / "balances.csv"
            if balances_path.exists():
                plan = recover_planning(
                    plan,
                    balances_path,
                    lambda path: quote_fn(
                        path,
                        row,
                        db_path=db_path,
                        catalog=catalog,
                        rpc_urls=rpc_urls,
                        allowlist=allowlist,
                        target_policy=target_policy,
                        loss_limit=loss_limit,
                        quote_floor=quote_floor,
                        seconds=max(0.1, planning_until - clock()),
                    ),
                    until=planning_until,
                    clock=clock,
                    sleep=sleep,
                    progress=progress,
                    locked_chains=locked_chains,
                )
            _assign_collection_identities(plan, run_id, locked_chains=locked_chains)
            for entry in plan["entries"]:
                entry["automatic_requote"] = True
            preserved_status = previous["status"] if previous and not execute else None
            _save_run(
                store,
                run_id,
                status=preserved_status or ("executing" if execute else "planned"),
                plan=plan,
            )
            store.save_run_artifact("route_quote", json.dumps(plan).encode())
            (wallet_output / "plan.json").write_text(json.dumps(plan, indent=2) + "\n")
            unresolved = [e for e in plan["entries"] if _unresolved(e)]
            report = {
                "wallet": row.public_address,
                "ordinal": row.ordinal,
                "run_id": run_id,
                "scan": scan_result,
                "plan_summary": plan.get("summary", {}),
                "unresolved": [
                    {
                        "chain_id": e["chain_id"],
                        "asset_id": e["asset_id"],
                        "symbol": e.get("symbol"),
                        "reason": e.get("reason"),
                        "search": e.get("search"),
                        "diagnostic": e.get("diagnostic"),
                    }
                    for e in unresolved
                ],
                "skipped": [
                    {
                        "chain_id": e["chain_id"],
                        "asset_id": e["asset_id"],
                        "symbol": e.get("symbol"),
                        "reason": e.get("reason"),
                        "reason_detail": e.get("reason_detail"),
                    }
                    for e in plan["entries"]
                    if e.get("status") not in _READY and e not in unresolved
                ],
            }
            if execute:
                _save_run(store, run_id, status="executing", plan=plan)

                def execute_round(entries):
                    return execute_fn(
                        entries,
                        wallets={row.public_address: row},
                        rpc_urls=rpc_urls,
                        journal_path=db_path,
                        execute=True,
                        trust_deposit_addresses=True,
                        max_route_loss_pct=loss_limit,
                        stage_seconds=min(180, max(0.1, until - clock())),
                    )

                report["execution"] = drive_execution(
                    plan,
                    db_path,
                    execute_round=execute_round,
                    until=until,
                    clock=clock,
                    sleep=sleep,
                    progress=progress,
                )
                report["status"] = "incomplete" if unresolved else report["execution"]["status"]
            else:
                report["status"] = "incomplete" if unresolved or previous else "planned"
                if previous:
                    report["reason"] = "execution_in_progress"
                    report["positions"] = execution_positions(plan["entries"], db_path)
            if report["status"] == "completed" and report["skipped"]:
                report["status"] = "completed_with_skips"
            _save_run(store, run_id, status=preserved_status or report["status"], report=report)
            (wallet_output / "result.json").write_text(json.dumps(report, indent=2) + "\n")
            reports.append(report)
    counts = Counter(report["status"] for report in reports)
    result = {
        "status": "incomplete"
        if counts["incomplete"]
        else "completed_with_skips"
        if execute and counts["completed_with_skips"]
        else "completed"
        if execute
        else "planned",
        "wallets": reports,
    }
    (output / "result.json").write_text(json.dumps(result, indent=2) + "\n")
    return result
