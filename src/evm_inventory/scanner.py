"""Resumable wallet traversal backed by the current inventory store."""

import os
import random
import time
from datetime import UTC, datetime
from urllib.parse import quote

from .config import format_amount
from .diagnostics import exception_diagnostic
from .discovery import Discovery
from .network_policy import EXCLUDED_NETWORKS
from .rpc import RpcReader
from .scan_buffer import ScanBuffer
from .stages import Deadline, StageDeadlineExceeded, check_deadline, deadline_scope, run_parallel
from .transport import RequestError, Transport

DONE = {"success"}
PERMANENT_RPC_ERRORS = {"wrong_chain", "provider_access_denied", "missing_rpc_environment"}
TRANSIENT_RPC_ERRORS = {"network_error", "provider_unavailable", "rate_limited"}


def now():
    return datetime.now(UTC).isoformat()


def _ready(asset):
    return asset["status"] not in DONE and (asset.get("retry_after") or 0) <= time.time()


def _failure(store, asset, error, status="error"):
    store.record_asset(
        asset["id"],
        {"error": error.code, "observed_at": now(), "diagnostic": exception_diagnostic(error)},
        status="deferred" if error.retry_after else status,
        retry_after=error.retry_after,
    )


def _mandatory_metadata(network, asset_id, metadata):
    return {
        **metadata,
        "network_name": network["name"],
        "token_review_status": network["token_review_status"],
    }


def _ensure_mandatory(store, wallet, network):
    """Refresh the catalog-owned assets and make them retryable for this traversal."""
    specs = [
        (
            "native",
            {
                "symbol": network["native_symbol"],
                "decimals": network["native_decimals"],
            },
        )
    ]
    specs.extend((token["address"], token) for token in network["tokens"])
    ids = []
    previous = {row["asset_id"]: row for row in store.assets(wallet, network["chain_id"])}
    for asset_id, metadata in specs:
        row_id = store.upsert_asset(
            wallet,
            network["chain_id"],
            asset_id,
            "mandatory",
            _mandatory_metadata(network, asset_id, metadata),
        )
        existing = previous.get(asset_id.lower(), {})
        # Store intentionally exposes updates through record_asset.  Re-recording
        # the prior payload clears an old cooldown while preserving the last result
        # until the authoritative RPC answer replaces it.
        store.record_asset(row_id, existing.get("result") or {}, status="pending")
        ids.append(row_id)
    return ids


def _rpc_urls(network):
    """Return the preferred endpoints for one network."""
    urls = list(network["rpc_urls"])
    key = os.environ.get("ALCHEMY_RPC_API_KEY") or os.environ.get("ALCHEMY_API_KEY")
    alchemy_network = network.get("alchemy_rpc_network") or network.get("alchemy_network")
    if key and alchemy_network:
        return (f"https://{alchemy_network}.g.alchemy.com/v2/{quote(key, safe='')}",)
    return tuple(dict.fromkeys(urls))


def _select_endpoint(rpc, network, pinned, blocked, *, rpc_urls=None):
    error = RequestError("no_rpc_available")
    for url in _rpc_urls(network) if rpc_urls is None else rpc_urls:
        check_deadline()
        if url in blocked:
            saved = blocked[url]
            if saved.retry_after is None or saved.retry_after > time.time():
                error = saved
                continue
            del blocked[url]
        try:
            block = rpc.block(url, network["chain_id"], pinned["number"] if pinned else None)
            if pinned and pinned["hash"] != block["hash"]:
                raise RequestError("block_hash_changed")
            return url, block
        except RequestError as exc:
            error = exc
            if exc.code in PERMANENT_RPC_ERRORS:
                blocked[url] = exc
            elif exc.code in TRANSIENT_RPC_ERRORS:
                error = RequestError(
                    exc.code, exc.retry_after or time.time() + 300,
                    diagnostic=exception_diagnostic(exc),
                )
                blocked[url] = error
    raise error


def _balances(store, assets, wallet, network, rpc, url, block, blocked):
    endpoint_error = None
    for asset in assets:
        check_deadline()
        if not _ready(asset):
            continue
        if endpoint_error is not None:
            _failure(store, asset, endpoint_error)
            continue
        metadata = asset["metadata"]
        try:
            if asset["asset_id"] == "native":
                raw = rpc.native(url, network["chain_id"], wallet, block["number"])
                decimals = network["native_decimals"]
            else:
                raw, decimals = rpc.token(
                    url,
                    network["chain_id"],
                    wallet,
                    asset["asset_id"],
                    block["number"],
                    metadata.get("decimals"),
                )
            check_deadline()
            store.record_asset(
                asset["id"],
                {
                    "raw_balance": str(raw),
                    "decimals": decimals,
                    "amount": format_amount(raw, decimals),
                    "symbol": metadata.get("symbol", asset["asset_id"]),
                    "block_number": block["number"],
                    "block_hash": block["hash"],
                    "observed_at": now(),
                    "source": "rpc",
                    "price_usd": None,
                    "price_source": None,
                    "price_timestamp": None,
                },
            )
        except RequestError as exc:
            if exc.code in TRANSIENT_RPC_ERRORS:
                exc = RequestError(
                    exc.code, exc.retry_after or time.time() + 300,
                    diagnostic=exception_diagnostic(exc),
                )
                endpoint_error = exc
                blocked[url] = exc
            elif exc.code in PERMANENT_RPC_ERRORS:
                endpoint_error = exc
                blocked[url] = exc
            elif exc.code not in {"invalid_contract", "decimals_mismatch", "missing_contract_code"}:
                endpoint_error = RequestError(
                    exc.code, exc.retry_after or time.time() + 300,
                    diagnostic=exception_diagnostic(exc),
                )
                blocked[url] = endpoint_error
            _failure(store, asset, exc)


def _reset_discovery(store, wallet, chain_id):
    """Begin a fresh provider traversal without replacing the state row."""
    store.write_discovery_state(wallet, chain_id, cursor=None, cursor_history=[], status="pending")


def _discover(store, network, wallet, discovery, mandatory_ids):
    chain_id = network["chain_id"]
    if not discovery.enabled:
        store.write_discovery_state(
            wallet,
            chain_id,
            status="disabled",
            result={"reason": "no_api_key"},
        )
        return []
    if not network.get("alchemy_network"):
        store.write_discovery_state(
            wallet,
            chain_id,
            status="disabled",
            result={"reason": "unsupported_network"},
        )
        return []

    state = store.discovery_state(wallet, chain_id)
    cursor = state["cursor"]
    history = state["cursor_history"]
    seen = set(history)
    discovered_ids = []
    try:
        for _ in range(100):
            check_deadline()
            contracts, next_cursor = discovery.page(wallet, network["alchemy_network"], cursor)
            check_deadline()
            if next_cursor and (next_cursor in seen or next_cursor == cursor):
                raise RequestError("discovery_cursor_cycle")
            current = {row["asset_id"]: row for row in store.assets(wallet, chain_id)}
            for candidate in contracts:
                raw = candidate.get("reported_raw_balance")
                if not raw:
                    continue
                address = (
                    candidate["address"].lower() if candidate["address"] != "native" else "native"
                )
                existing = current.get(address)
                # A current catalog entry remains authoritative even when the
                # discovery provider reports it with conflicting metadata.
                if existing is not None and existing["id"] in mandatory_ids:
                    continue
                row_id = store.upsert_asset(wallet, chain_id, address, "discovered", candidate)
                decimals = candidate.get("decimals")
                store.record_asset(
                    row_id,
                    {
                        "raw_balance": str(raw),
                        "decimals": decimals,
                        "amount": format_amount(int(raw), decimals)
                        if decimals is not None
                        else None,
                        "name": candidate.get("name"),
                        "symbol": candidate.get("symbol", address),
                        "observed_at": candidate.get("reported_at", now()),
                        "source": "alchemy",
                        "verification": "provider_only",
                        "price_usd": candidate.get("price_usd"),
                        "price_source": candidate.get("price_source"),
                        "price_timestamp": candidate.get("price_timestamp"),
                    },
                    status="provider_only",
                )
                discovered_ids.append(row_id)
            if next_cursor is not None:
                seen.add(next_cursor)
                history.append(next_cursor)
            store.write_discovery_state(
                wallet,
                chain_id,
                cursor=next_cursor,
                cursor_history=history,
                status="success" if next_cursor is None else "pending",
                result={"observed_at": now()},
            )
            if next_cursor is None:
                return discovered_ids
            cursor = next_cursor
        raise RequestError("discovery_page_limit")
    except RequestError as exc:
        latest = store.discovery_state(wallet, chain_id)
        store.write_discovery_state(
            wallet,
            chain_id,
            cursor=latest["cursor"],
            cursor_history=latest["cursor_history"],
            status="deferred" if exc.retry_after else "error",
            retry_after=exc.retry_after,
            result=latest["result"],
            error={"code": exc.code, "observed_at": now(),
                   "diagnostic": exception_diagnostic(exc)},
        )
        return discovered_ids


def _scan_network(job, *, rpc, discovery, settings):
    network, wallet, buffer, mandatory_ids, blocked = job
    transport = None
    try:
        check_deadline()
        if rpc is None or discovery is None:
            transport = Transport(interval=settings.get("interval", 0))
        reader = rpc if rpc is not None else RpcReader(transport)
        provider = (
            discovery
            if discovery is not None
            else Discovery(
                transport, key="" if not settings.get("discovery_enabled", True) else None
            )
        )
        discovery_disabled = not provider.enabled or not network.get("alchemy_network")
        if discovery_disabled:
            _discover(buffer, network, wallet, provider, set(mandatory_ids))
        check_deadline()
        mandatory = [row for row in buffer.assets() if row["id"] in mandatory_ids]
        mandatory.sort(
            key=lambda row: (
                int((row["result"] or {}).get("raw_balance") or "0") <= 0,
                row["asset_id"] != "native",
            )
        )
        try:
            url, block = _select_endpoint(reader, network, None, blocked)
        except RequestError as exc:
            for asset in mandatory:
                _failure(buffer, asset, exc, "unavailable")
        else:
            buffer.save_pass(wallet, network["chain_id"], block)
            _balances(buffer, mandatory, wallet, network, reader, url, block, blocked)
        if not discovery_disabled:
            _discover(buffer, network, wallet, provider, set(mandatory_ids))
    except StageDeadlineExceeded:
        pass
    finally:
        if transport is not None:
            transport.close()
    return buffer


def scan(store, scope, *, rpc=None, discovery=None, sleep=time.sleep, progress=None):
    """Run each wallet's networks concurrently and save read results on the caller thread."""
    settings = scope["settings"]
    networks = [n for n in scope["catalog"]["networks"]
                if n["chain_id"] not in EXCLUDED_NETWORKS]
    started = time.monotonic()
    timed_out = False
    blocked = {}
    mandatory_total = mandatory_failed = discovered_checks = discovered_failed = 0
    positive_balances = 0
    discovered_provider_only = 0
    error_details = []
    discovery_statuses = dict.fromkeys(
        ("success", "disabled", "error", "deferred", "pending", "unavailable"), 0
    )
    major = {1, 10, 56, 137, 42161, 8453, 43114}
    for wi, wallet in enumerate(scope["wallets"]):
        if wi:
            sleep(random.uniform(settings.get("delay_min", 1), settings.get("delay_max", 3)))
        seconds = settings.get("stage_seconds", 30)
        deadline = Deadline(seconds=seconds, label="scan",
                            max_seconds=max(30, seconds) if settings.get("complete_scan") else 30)
        prior = store.assets(wallet)
        useful = {
            row["chain_id"]
            for row in prior
            if int((row["result"] or {}).get("raw_balance") or "0") > 0
        }
        ordered = sorted(
            networks, key=lambda n: (n["chain_id"] not in useful, n["chain_id"] not in major)
        )
        jobs = []
        for network in ordered:
            cid = network["chain_id"]
            mandatory_ids = _ensure_mandatory(store, wallet, network)
            mandatory_total += len(mandatory_ids)
            _reset_discovery(store, wallet, cid)
            buffer = ScanBuffer(
                wallet, cid, store.assets(wallet, cid), store.discovery_state(wallet, cid), deadline
            )
            # Each worker owns its endpoint cooldown snapshot. Completed snapshots
            # are merged between wallets, preserving the existing retry behavior.
            jobs.append((network, wallet, buffer, mandatory_ids, dict(blocked)))
        with deadline_scope(deadline):
            outcome = run_parallel(
                lambda job: _scan_network(job, rpc=rpc, discovery=discovery, settings=settings),
                jobs,
                deadline=deadline,
                workers=settings.get("workers", 16),
            )
        timed_out = timed_out or deadline.expired or bool(outcome.pending)
        for index, (network, _, buffer, mandatory_ids, network_blocked) in enumerate(jobs):
            cid = network["chain_id"]
            discovered_ids = buffer.persist(store)
            worker_failed = index in outcome.errors
            failure_code = "scan_worker_error" if worker_failed else "stage_deadline_exceeded"
            failure_status = "error" if worker_failed else "deferred"
            if index in outcome.completed:
                blocked.update(network_blocked)
            for row in store.assets(wallet, cid):
                if row["id"] in {*mandatory_ids, *discovered_ids} and row["status"] == "pending":
                    store.record_asset(
                        row["id"],
                        {"error": failure_code, "observed_at": now(),
                         "diagnostic": exception_diagnostic(outcome.errors[index])
                         if worker_failed else {"message": "Scan stage deadline exceeded"}},
                        status=failure_status,
                    )
            state = store.discovery_state(wallet, cid)
            if state["status"] == "pending":
                store.write_discovery_state(
                    wallet,
                    cid,
                    cursor=state["cursor"],
                    cursor_history=state["cursor_history"],
                    status=failure_status,
                    result=state["result"],
                    error={"code": failure_code, "observed_at": now(),
                           "diagnostic": exception_diagnostic(outcome.errors[index])
                           if worker_failed else {"message": "Scan stage deadline exceeded"}},
                )
                state = store.discovery_state(wallet, cid)
            discovery_statuses[state["status"]] = discovery_statuses.get(state["status"], 0) + 1
            current = [
                row
                for row in store.assets(wallet, cid)
                if row["id"] in {*mandatory_ids, *discovered_ids}
            ]
            mandatory_failed += sum(
                row["status"] != "success" for row in current if row["id"] in mandatory_ids
            )
            discovered = [row for row in current if row["id"] in discovered_ids]
            discovered_checks += len(discovered)
            discovered_failed += sum(
                row["status"] not in {"success", "provider_only"} for row in discovered
            )
            discovered_provider_only += sum(row["status"] == "provider_only" for row in discovered)
            error_details.extend(
                {"wallet": wallet, "chain_id": cid, "network": network["name"],
                 "asset_id": row["asset_id"], "status": row["status"],
                 "error": (row["result"] or {}).get("error"),
                 "diagnostic": (row["result"] or {}).get("diagnostic")}
                for row in current if row["status"] not in {"success", "provider_only"}
            )
            if state["status"] not in {"success", "disabled"}:
                error_details.append({"wallet": wallet, "chain_id": cid,
                                      "network": network["name"], "phase": "discovery",
                                      "status": state["status"], "error": state.get("error")})
            positive_balances += sum(
                int((row["result"] or {}).get("raw_balance") or "0") > 0 for row in current
            )
            if progress:
                progress(
                    f"Wallet {wi + 1}/{len(scope['wallets'])} | "
                    f"{network['name']} | {len(mandatory_ids)} checks"
                )
    return {
        "mandatory_total": mandatory_total,
        "mandatory_failed": mandatory_failed,
        "catalog_gaps": sum(n["token_review_status"] == "pending" for n in networks),
        "discovery_statuses": discovery_statuses,
        "discovered_checks": discovered_checks,
        "discovered_failed": discovered_failed,
        "discovered_provider_only": discovered_provider_only,
        "error_details": error_details,
        "positive_balances": positive_balances,
        "elapsed_seconds": round(time.monotonic() - started, 3),
        "timed_out": timed_out,
    }
