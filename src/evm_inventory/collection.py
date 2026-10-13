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

from .celo import CELO_CHAIN_ID, CELO_TOKEN_ADDRESS
from .config import add_allowlisted_tokens, snapshot
from .executor import ExecutionRpc
from .journal import Journal
from .live_plan import create_live_plan
from .planner_gas import PlannerGasEstimator
from .report import export_current
from .route_execution import _position_key, execute_entries, route_credit_unverified
from .rpc import RpcReader, quantity
from .scanner import scan
from .stages import Deadline, StageDeadlineExceeded, deadline_scope
from .store import Store
from .transport import Transport
from .unsigned_recovery import diagnose_legacy_fee_cap
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


def verify_prior_defi_exit(journal, position_id, rpc_urls, *, rpc=None):
    """Exclude only local exits whose exact receipts are finalized on their source chain."""
    if not journal.has_confirmed_defi_exit(position_id):
        return False
    position = journal.position(position_id)
    action_id = position["position_key"].rsplit(":", 1)[1]
    chains = journal.connection.execute(
        "SELECT DISTINCT chain_id FROM defi_positions WHERE wallet=? AND action_id=?",
        (position["wallet"], action_id),
    ).fetchall()
    if len(chains) != 1 or chains[0][0] not in rpc_urls:
        return False
    chain_id = chains[0][0]
    url = rpc_urls[chain_id]
    transport = Transport(interval=0) if rpc is None else None
    rpc = rpc or ExecutionRpc(transport)
    try:
        if quantity(rpc.call(url, "eth_chainId", [])) != chain_id:
            return False
        finalized = rpc.call(url, "eth_getBlockByNumber", ["finalized", False])
        if not isinstance(finalized, dict):
            return False
        height = quantity(finalized["number"])
        for step in journal.connection.execute(
            "SELECT tx_hash FROM route_steps WHERE position_id=?", (position_id,)
        ):
            receipt = rpc.call(url, "eth_getTransactionReceipt", [step["tx_hash"]])
            if (
                not isinstance(receipt, dict)
                or str(receipt.get("transactionHash", "")).lower() != step["tx_hash"].lower()
                or quantity(receipt.get("status")) != 1
                or quantity(receipt.get("blockNumber")) > height
            ):
                return False
        return True
    finally:
        if transport is not None:
            transport.close()


def _unresolved(entry):
    search = entry.get("search", {})
    operational = {"quota_wait", "provider_cooldown", "route_search_incomplete", "stale_quote",
                   "quote_timeout", "quote_request_failed"}
    pending_alternatives = (bool(operational.intersection(_search_error_reasons(search))) or (
        not search.get("complete") and bool(search.get("pending"))
    ))
    return (
        entry.get("status") not in _READY
        and entry.get("status") not in {"denied", "dust", "below_quote_floor"}
        and (entry.get("reason") not in _SKIP_REASONS or pending_alternatives)
    )


def _asset_key(entry):
    return (str(entry["wallet"]).lower(), int(entry["chain_id"]), str(entry["asset_id"]).lower())


def _spends_native_balance(entry):
    return entry["asset_id"] == "native" or (
        entry["chain_id"] == CELO_CHAIN_ID and entry["asset_id"] == CELO_TOKEN_ADDRESS)


def _retry_row_key(row):
    key = _asset_key(row)
    if (key[1:] == (CELO_CHAIN_ID, "native") and row.get("status") == "success"
            and str(row.get("decimals")) == "18"):
        return (key[0], CELO_CHAIN_ID, CELO_TOKEN_ADDRESS)
    return key


def _group_key(entry):
    return entry.get("group", {}).get("key") or _position_key(entry)


def _search_error_reasons(search):
    return {e if isinstance(e, str) else e.get("reason", (
        "quota_wait" if isinstance(e.get("retry_at"), (int, float)) else "quote_request_failed"
    )) for e in search.get("errors", [])}


def _assign_collection_identities(plan, run_id, *, locked_chains=(), frozen_keys=None):
    for entry in plan["entries"]:
        if (
            entry.get("routing_provider") == "relay"
            and not entry.get("group")
            and (_asset_key(entry) not in frozen_keys if frozen_keys is not None
                 else entry["chain_id"] not in locked_chains)
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
    frozen_keys=None,
    on_plan=None,
):
    """Retry unfinished source groups in this invocation without requerying completed groups."""
    attempt = 0
    while clock() < until:
        if on_plan is not None:
            on_plan(plan)
        unresolved = [
            e
            for e in plan["entries"]
            if _unresolved(e)
            and (_asset_key(e) not in frozen_keys if frozen_keys is not None
                 else e["chain_id"] not in locked_chains)
            and e.get("reason") not in _PERMANENT_REASONS
        ]
        if not unresolved or clock() >= until:
            break
        backoff = min(60, 2 ** min(attempt, 6))
        due = {}
        for entry in unresolved:
            search = entry.get("search", {})
            errors = _search_error_reasons(search)
            times = [p["retry_at"] for p in search.get("pending", [])
                     if isinstance(p.get("retry_at"), (int, float))]
            # A provider cooldown gates only provider work. Local RPC/fee failures
            # on another network can be retried while its route quota is waiting.
            wait = (max(backoff, min(times) - wall_clock()) if times and
                    errors <= {"quota_wait", "provider_cooldown"} else backoff)
            key = _asset_key(entry)
            due[key] = min(due.get(key, math.inf), clock() + wait)
        delay = min(due.values()) - clock()
        if delay >= until - clock():
            if progress:
                progress("Provider retry is outside this run's deadline; preserving exact reasons")
            break
        if progress:
            progress(f"Retrying {len(unresolved)} unresolved positions in {delay:.0f} seconds")
        end = clock() + delay
        tick_at = clock() + 60
        while clock() < end:
            sleep(min(1, max(0, end - clock())))
            if on_plan is not None and clock() >= tick_at:
                on_plan(plan)
                tick_at = clock() + 60
            if progress and end - clock() > 60 and int(end - clock()) % 60 == 0:
                progress(f"Provider cooldown: {end - clock():.0f} seconds remaining")
        retry_keys = {key for key, when in due.items() if when <= clock()}
        if not retry_keys:
            continue
        chains = {key[1] for key in retry_keys}
        with Path(balances_path).open(newline="") as stream:
            reader = csv.DictReader(stream)
            rows = [r for r in reader if _retry_row_key(r) in retry_keys]
            fields = reader.fieldnames
        retry_path = Path(balances_path).parent.parent / "retry-balances.csv"
        with retry_path.open("w", newline="") as stream:
            writer = csv.DictWriter(stream, fieldnames=fields)
            writer.writeheader()
            writer.writerows(rows)
        refreshed = quote(retry_path)
        replacements = [e for e in refreshed["entries"] if _asset_key(e) in retry_keys]
        replaced_keys = {_asset_key(e) for e in replacements}
        plan = {
            **plan,
            "entries": [e for e in plan["entries"] if _asset_key(e) not in replaced_keys]
            + replacements,
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
                "SELECT p.id,p.state,p.reason,g.state AS group_state,g.reason AS group_reason "
                "FROM route_positions p "
                "LEFT JOIN route_groups g ON p.group_id=g.id WHERE p.position_key=?",
                (key,),
            ).fetchone()
            state, reason = (row["state"], row["reason"]) if row else ("planned", None)
            if row and state == "completed" and route_credit_unverified(entry, journal, row["id"]):
                state, reason = "route_credit_pending", "bitget_credit_unverified"
            if (row and str(row["group_state"]).startswith("manual_review")
                    and state not in {"completed", "route_credit_pending"}
                    and not (state == "deposit_pending" and journal.checkpoint(
                        row["id"], "route_deposit"))):
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
                    "has_broadcast": bool(row and journal.has_broadcast(row["id"])),
                }
            )
    return result


def _resume_unsigned_retry_groups(entries, db_path, *, rpc_urls=None):
    groups = {}
    for entry in entries:
        if entry.get("status") in _READY and entry.get("group"):
            groups.setdefault(entry["group"]["key"], []).append(entry)
    with Journal(db_path) as journal:
        for key, members in groups.items():
            if not all(e.get("automatic_requote") for e in members):
                continue
            group = journal.connection.execute(
                "SELECT id FROM route_groups WHERE group_key=?", (key,),
            ).fetchone()
            if group:
                if rpc_urls is not None:
                    diagnose_legacy_fee_cap(journal, group["id"], members, rpc_urls)
                journal.reactivate_unsigned_retry_group(group["id"],
                                                       {_position_key(e) for e in members})
                journal.reactivate_unsigned_weth_continuation(group["id"],
                                                             {_position_key(e) for e in members})


def _wallet_arrival_chains(entry, wallet):
    """Use top-level route evidence to identify a wallet-executed destination leg."""
    if str(entry.get("wallet", "")).lower() != wallet:
        return set()
    chains = set()
    for step in entry.get("steps", []):
        for leg in step.get("route", {}).get("evidence", {}).get("steps", []):
            destination = leg.get("destination")
            if (not leg.get("is_substep") and isinstance(destination, dict)
                    and str(leg.get("recipient", "")).lower() == wallet):
                chains.add(int(destination["chain_id"]))
    return chains


def _destination_gas_dependencies(plan, db_path):
    """Keep gas for unfinished wallet arrivals and recoverable saved continuations."""
    dependencies = {}

    def reserve(wallet, chain, owner):
        dependencies.setdefault((wallet, int(chain)), set()).add(owner)

    enabled = {int(t["chain_id"]) for t in plan.get("search_scope", {}).get("enabled_targets", [])}
    with Journal(db_path) as journal:
        for entry in plan["entries"]:
            owner, wallet = _position_key(entry), str(entry["wallet"]).lower()
            position = journal.connection.execute(
                "SELECT id,state,wallet FROM route_positions WHERE position_key=?", (owner,)
            ).fetchone()
            if position and position["wallet"].lower() != wallet:
                continue
            if (position and position["state"] == "completed"
                    and not route_credit_unverified(entry, journal, position["id"])):
                continue
            if _unresolved(entry) and entry.get("reason") not in _PERMANENT_REASONS:
                for chain in enabled:
                    reserve(wallet, chain, owner)
            if entry.get("status") not in _READY and position is None:
                continue
            chains = _wallet_arrival_chains(entry, wallet)
            if position:
                saved_keys = journal.connection.execute(
                    "SELECT checkpoint_key FROM route_checkpoints WHERE position_id=? AND "
                    "(checkpoint_key IN ('prepared','bridge_prepared','canonical_prepared') "
                    "OR checkpoint_key LIKE 'route:%')", (position["id"],)
                ).fetchall()
                for row in saved_keys:
                    saved = journal.checkpoint(position["id"], row["checkpoint_key"])
                    continuation = saved.get("continuation")
                    context = continuation if isinstance(continuation, dict) else saved
                    if str(context.get("wallet", "")).lower() != wallet:
                        continue
                    chains.update(_wallet_arrival_chains(context, wallet))
                    # An advanced child may only contain a local withdrawal/deposit.
                    if (isinstance(continuation, dict)
                            or int(context.get("chain_id", entry["chain_id"]))
                            != int(entry["chain_id"])):
                        chains.add(int(context["chain_id"]))
            for chain in chains:
                reserve(wallet, chain, owner)
    return dependencies


def _native_gas_dependency_cycles(entries, positions, destination_gas):
    """Identify mutually blocked unsigned native owners without releasing their gas."""
    unsigned = {p["position_key"] for p in positions if not p["has_broadcast"]
                and p["state"] != "completed" and not p["state"].startswith("manual_review")}
    owners = {_position_key(e): e for e in entries if e.get("status") in _READY
              and _spends_native_balance(e) and _position_key(e) in unsigned}
    graph = {key: (destination_gas.get(_asset_key(e)[:2], set()) & owners.keys()) - {key}
             for key, e in owners.items()}

    def reachable(start):
        found, pending_nodes = set(), [start]
        while pending_nodes:
            current = pending_nodes.pop()
            if current not in found:
                found.add(current)
                pending_nodes.extend(graph[current] - found)
        return found

    reach = {key: reachable(key) for key in graph}
    remaining, cycles = set(graph), []
    while remaining:
        start = min(remaining)
        component = {key for key in reach[start] if start in reach[key]}
        remaining -= component
        if len(component) > 1:
            cycles.append({
                "wallet": str(owners[start]["wallet"]).lower(),
                "members": [{"position_key": key, "chain_id": int(owners[key]["chain_id"]),
                             "depends_on": sorted(graph[key] & component)}
                            for key in sorted(component)],
            })
    return cycles


def _record_native_gas_cycles(cycles, active_keys, db_path):
    """Attach cycle evidence only after the normal executor created an exact position."""
    with Journal(db_path) as journal:
        for cycle in cycles:
            for member in cycle["members"]:
                if member["position_key"] not in active_keys:
                    continue
                position = journal.connection.execute(
                    "SELECT id,state FROM route_positions WHERE position_key=?",
                    (member["position_key"],),
                ).fetchone()
                if (not position or position["state"] == "completed"
                        or position["state"].startswith("manual_review")
                        or journal.has_broadcast(position["id"])):
                    continue
                journal.save_checkpoint(position["id"], "native_gas_dependency_cycle", cycle)
                journal.record_position_state(position["id"], position["state"],
                                              reason="native_gas_dependency_cycle")


def drive_execution(
    plan, db_path, *, execute_round, until, clock=time.monotonic, sleep=time.sleep, progress=None,
    dependency_plan=None,
):
    """Continue the same immutable plan; known broadcasts are observed by its journal."""
    dependencies = plan if dependency_plan is None else dependency_plan
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
        unresolved_chains = {e["chain_id"] for e in plan["entries"] if _unresolved(e)}
        observed_keys = {p["position_key"] for p in positions if p["has_broadcast"]}
        def eligible(e):
            return e["chain_id"] not in unresolved_chains or _position_key(e) in observed_keys
        active = [e for e in active if eligible(e)]
        if not active and (failure or unresolved_chains):
            return {
                "status": "incomplete",
                "reason": "execution_failed" if failure else "planning_incomplete",
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
            and eligible(e)
        ]
        dependency_positions = (positions if dependencies is plan else
                                execution_positions(dependencies["entries"], db_path))
        unfinished_tokens = [e for e in dependencies["entries"] if e.get("status") in _READY
                             and not _spends_native_balance(e)
                             and any(p["position_key"] == _position_key(e)
                                     and p["state"] != "completed" for p in dependency_positions)]
        destination_gas = _destination_gas_dependencies(dependencies, db_path)
        native_cycles = _native_gas_dependency_cycles(
            dependencies["entries"], dependency_positions, destination_gas)
        active = [{**e, "_source_gas_held": (
            _spends_native_balance(e) and _position_key(e) not in observed_keys
            and any(_asset_key(e)[:2] == _asset_key(token)[:2]
                    and (_group_key(e) != _group_key(token)
                         or e.get("routing_provider") == "relay")
                    for token in unfinished_tokens)), "_destination_gas_held": (
            _spends_native_balance(e) and _position_key(e) in pending
            and _position_key(e) not in observed_keys
            and bool(destination_gas.get(_asset_key(e)[:2], set()) - {_position_key(e)}))}
            for e in active]
        source_held = any(e.get("_source_gas_held") for e in active)
        destination_held = any(e.get("_destination_gas_held") for e in active)
        held_only = (source_held or destination_held) and all(
            e.get("_source_gas_held") or e.get("_destination_gas_held")
            for e in active if _position_key(e) in pending
        )
        held_cycle_keys = {_position_key(e) for e in active if e.get("_destination_gas_held")}
        # Relay native sends have their own group/adapter; ordinary groups retain
        # all members and defer their unsigned native member inside the executor.
        held_relay = [e for e in active if e.get("routing_provider") == "relay"
                      and (e.get("_source_gas_held") or e.get("_destination_gas_held"))]
        if held_relay:
            with Journal(db_path) as journal:
                for entry in held_relay:
                    position = journal.get_or_create_position(
                        position_key=_position_key(entry), wallet=entry["wallet"])
                    if position["state"] == "completed" or journal.has_broadcast(position["id"]):
                        continue
                    reason = ("source_gas_for_unsettled_tokens" if entry.get("_source_gas_held")
                              else "destination_gas_for_unsettled_routes")
                    journal.record_position_state(position["id"], position["state"], reason=reason)
        active = [e for e in active if not (
            e.get("routing_provider") == "relay"
            and (e.get("_source_gas_held") or e.get("_destination_gas_held")))]
        # Keep complete group membership/budgets; inside each group tokens go first.
        active.sort(key=_spends_native_balance)
        state = tuple((p["position_key"], p["state"], p["reason"]) for p in positions)
        if progress and state != last_progress:
            progress(f"Settlement: {completed}/{len(positions)} completed; {len(pending)} pending")
            last_progress = state
        remaining = until - clock()
        if remaining <= 0:
            continue
        try:
            with deadline_scope(
                Deadline(min(180, remaining), "collection_execution", max_seconds=86_400)
            ):
                last_result = execute_round(active) if active else {}
        except StageDeadlineExceeded:
            last_result = {"deferred": len(active)}
        rounds += 1
        if native_cycles:
            _record_native_gas_cycles(native_cycles, held_cycle_keys, db_path)
        if held_only:
            blocked_cycles = [cycle for cycle in native_cycles if any(
                member["position_key"] in held_cycle_keys for member in cycle["members"])]
            return {
                "status": "incomplete", "reason": (
                    "native_gas_dependency_cycle" if blocked_cycles else
                    "planning_incomplete" if destination_held else
                    "source_gas_for_unsettled_tokens"),
                "completed": completed, "rounds": rounds,
                "positions": execution_positions(plan["entries"], db_path),
                "last_round": last_result,
                **({"native_gas_dependency_cycles": blocked_cycles} if blocked_cycles else {}),
            }
        if clock() < until:
            sleep(min(3, max(0, until - clock())))


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
                "SELECT * FROM collection_runs WHERE wallet=? AND (status IN "
                "('executing','incomplete','planned') OR "
                "(status='superseded' AND request_digest=? AND NOT EXISTS "
                "(SELECT 1 FROM collection_runs newer WHERE newer.wallet=collection_runs.wallet "
                "AND newer.updated_at>collection_runs.updated_at "
                "AND newer.plan_json IS NOT NULL))) AND plan_json IS NOT NULL "
                "ORDER BY updated_at DESC "
                "LIMIT 1",
                (row.public_address, digest),
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
                            and (position["state"] != "completed" or route_credit_unverified(
                                entry, journal, position["id"]))
                            and journal.has_broadcast(position["id"])
                        ):
                            pending_broadcast = True
                if not pending_broadcast and previous["request_digest"] != digest:
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
                    "SELECT DISTINCT p.id,p.position_key FROM route_positions p JOIN route_steps s "
                    "ON s.position_id=p.id WHERE p.wallet=? AND p.state<>'completed' "
                    "AND s.tx_hash IS NOT NULL",
                    (row.public_address.lower(),),
                ).fetchall()
                foreign = [
                    p["position_key"] for p in outstanding
                    if p["position_key"] not in known_keys
                    and not verify_prior_defi_exit(journal, p["id"], rpc_urls)
                ]
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
            if execute:
                _resume_unsigned_retry_groups(plan["entries"], db_path, rpc_urls=rpc_urls)
            locked_chains = set()
            frozen_keys = set()
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
                        frozen_keys.add(_asset_key(entry))
            balances_path = wallet_output / "inventory" / "balances.csv"

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

            def advance_ready_groups(current):
                blocked = {e["chain_id"] for e in current["entries"] if _unresolved(e)}
                ready = [e for e in current["entries"] if e.get("status") in _READY and (
                    e["chain_id"] not in blocked or _asset_key(e) in frozen_keys
                )]
                if not ready or clock() >= until:
                    return
                _assign_collection_identities(current, run_id, locked_chains=locked_chains,
                                              frozen_keys=frozen_keys)
                for e in current["entries"]:
                    e["automatic_requote"] = True
                # A crash after the first broadcast must resume this exact plan.
                _save_run(store, run_id, status="executing", plan=current)
                (wallet_output / "plan.json").write_text(json.dumps(current, indent=2) + "\n")
                drive_execution(
                    {"entries": ready}, db_path, execute_round=execute_round,
                    until=min(until, clock() + 90), clock=clock, sleep=sleep, progress=progress,
                    dependency_plan=current,
                )
                with Journal(db_path) as journal:
                    for e in ready:
                        position = journal.connection.execute(
                            "SELECT id FROM route_positions WHERE position_key=?",
                            (_position_key(e),),
                        ).fetchone()
                        if position and journal.has_broadcast(position[0]):
                            locked_chains.add(e["chain_id"])
                            frozen_keys.add(_asset_key(e))

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
                    frozen_keys=frozen_keys,
                    on_plan=advance_ready_groups if execute else None,
                )
            _assign_collection_identities(plan, run_id, locked_chains=locked_chains,
                                          frozen_keys=frozen_keys)
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
