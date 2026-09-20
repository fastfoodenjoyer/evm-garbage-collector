"""Resumable wallet traversal with mandatory RPC checks and optional discovery."""

import os
import random
import time
from datetime import UTC, datetime
from urllib.parse import quote

from .config import format_amount
from .discovery import Discovery
from .rpc import RpcReader
from .transport import RequestError, Transport

DONE = {"success"}
PERMANENT_RPC_ERRORS = {"wrong_chain", "provider_access_denied", "missing_rpc_environment"}
TRANSIENT_RPC_ERRORS = {"network_error", "provider_unavailable", "rate_limited"}


def now():
    return datetime.now(UTC).isoformat()


def _ready(job):
    return job["status"] not in DONE and (job.get("retry_after") or 0) <= time.time()


def _failure(store, job, error, status="error"):
    store.record(
        job["id"],
        {"error": error.code, "observed_at": now()},
        status="deferred" if error.retry_after else status,
        retry_after=error.retry_after,
    )


def _ensure_jobs(store, run, wallet, network):
    store.ensure_job(
        run,
        wallet,
        network["chain_id"],
        "native",
        metadata={"symbol": network["native_symbol"], "decimals": network["native_decimals"]},
    )
    for token in network["tokens"]:
        store.ensure_job(run, wallet, network["chain_id"], token["address"], metadata=token)
    store.ensure_job(run, wallet, network["chain_id"], "discovery", kind="discovery")


def _rpc_urls(network):
    """Prefer an in-memory Alchemy RPC URL when the configured key supports the chain."""

    urls = list(network["rpc_urls"])
    key = os.environ.get("ALCHEMY_RPC_API_KEY") or os.environ.get("ALCHEMY_API_KEY")
    alchemy_network = network.get("alchemy_network")
    if key and alchemy_network:
        urls.insert(
            0,
            f"https://{alchemy_network}.g.alchemy.com/v2/{quote(key, safe='')}",
        )
    return tuple(dict.fromkeys(urls))


def _select_endpoint(rpc, network, pinned, blocked):
    error = RequestError("no_rpc_available")
    for url in _rpc_urls(network):
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
                error = RequestError(exc.code, exc.retry_after or time.time() + 300)
                blocked[url] = error
    raise error


def _balances(store, run, wallet, network, rpc, url, block, blocked):
    endpoint_error = None
    for job in store.jobs(run, wallet, network["chain_id"]):
        if job["kind"] == "discovery" or job["status"] == "provider_only" or not _ready(job):
            continue
        if endpoint_error is not None:
            _failure(store, job, endpoint_error)
            continue
        meta = job["metadata"]
        try:
            if job["asset_id"] == "native":
                raw = rpc.native(url, network["chain_id"], wallet, block["number"])
                decimals = network["native_decimals"]
            else:
                raw, decimals = rpc.token(
                    url,
                    network["chain_id"],
                    wallet,
                    job["asset_id"],
                    block["number"],
                    meta.get("decimals"),
                )
            store.record(
                job["id"],
                {
                    "raw_balance": str(raw),
                    "decimals": decimals,
                    "amount": format_amount(raw, decimals),
                    "symbol": meta.get("symbol", job["asset_id"]),
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
            if job["kind"] == "discovered" and (job.get("result") or {}).get("raw_balance"):
                continue
            if exc.code in TRANSIENT_RPC_ERRORS:
                exc = RequestError(exc.code, exc.retry_after or time.time() + 300)
                endpoint_error = exc
                blocked[url] = exc
            elif exc.code in PERMANENT_RPC_ERRORS:
                endpoint_error = exc
                blocked[url] = exc
            elif exc.code not in {"invalid_contract", "decimals_mismatch", "missing_contract_code"}:
                endpoint_error = RequestError(exc.code, exc.retry_after or time.time() + 300)
                blocked[url] = endpoint_error
            _failure(store, job, exc)


def _discover(store, job, network, wallet, discovery):
    if job["status"] == "success":
        return
    if not discovery.enabled:
        store.record(job["id"], {"reason": "no_api_key"}, status="disabled")
        return
    if not network.get("alchemy_network"):
        store.record(job["id"], {"reason": "unsupported_network"}, status="disabled")
        return
    if not _ready(job):
        return
    result = job.get("result") or {}
    cursor = result.get("cursor")
    seen = set(result.get("cursor_history", []))
    try:
        for _ in range(100):
            contracts, next_cursor = discovery.page(wallet, network["alchemy_network"], cursor)
            if next_cursor and (next_cursor in seen or next_cursor == cursor):
                raise RequestError("discovery_cursor_cycle")
            store.save_discovery_page(job["id"], contracts, next_cursor)
            for candidate in contracts:
                raw = candidate.get("reported_raw_balance")
                if not raw:
                    continue
                if candidate["address"] == "native":
                    candidate = {
                        **candidate,
                        "symbol": network["native_symbol"]
                        if candidate.get("symbol") in {None, "", "native"}
                        else candidate["symbol"],
                        "decimals": candidate.get("decimals") or network["native_decimals"],
                    }
                discovered = next(
                    item
                    for item in store.jobs(job["run_id"], wallet, network["chain_id"])
                    if item["asset_id"] == candidate["address"]
                )
                store.record(
                    discovered["id"],
                    {
                        "raw_balance": raw,
                        "decimals": candidate.get("decimals"),
                        "amount": format_amount(int(raw), candidate["decimals"])
                        if candidate.get("decimals") is not None
                        else None,
                        "name": candidate.get("name"),
                        "symbol": candidate.get("symbol", candidate["address"]),
                        "observed_at": candidate.get("reported_at", now()),
                        "source": "alchemy",
                        "verification": "provider_only",
                        "price_usd": candidate.get("price_usd"),
                        "price_source": candidate.get("price_source"),
                        "price_timestamp": candidate.get("price_timestamp"),
                    },
                    status="provider_only",
                )
            if next_cursor is None:
                return
            seen.add(next_cursor)
            cursor = next_cursor
        raise RequestError("discovery_page_limit")
    except RequestError as exc:
        # Keep the persisted cursor when marking a resumable page error.
        latest = next(
            j
            for j in store.jobs(job["run_id"], wallet, network["chain_id"])
            if j["id"] == job["id"]
        )
        saved = latest.get("result") or {}
        saved.update(error=exc.code, observed_at=now())
        store.record(
            job["id"],
            saved,
            status="deferred" if exc.retry_after else "error",
            retry_after=exc.retry_after,
        )


def scan(store, run_id, *, rpc=None, discovery=None, sleep=time.sleep, progress=None, resume=False):
    run = store.run(run_id)
    scope = run["snapshot"]
    settings = scope["settings"]
    transport = (
        Transport(interval=settings.get("interval", 1))
        if rpc is None or discovery is None
        else None
    )
    rpc = rpc or RpcReader(transport)
    discovery = discovery or Discovery(
        transport, key="" if not settings.get("discovery_enabled", True) else None
    )
    blocked = {}
    store.set_status(run_id, "running")
    try:
        for wi, wallet in enumerate(scope["wallets"]):
            if wi:
                sleep(random.uniform(settings.get("delay_min", 1), settings.get("delay_max", 3)))
            for network in scope["catalog"]["networks"]:
                cid = network["chain_id"]
                _ensure_jobs(store, run_id, wallet, network)
                jobs = store.jobs(run_id, wallet, cid)
                dj = next(j for j in jobs if j["kind"] == "discovery")
                # Discovery is independent of RPC availability; persist candidates first.
                _discover(store, dj, network, wallet, discovery)
                jobs = store.jobs(run_id, wallet, cid)
                dj = next(j for j in jobs if j["kind"] == "discovery")
                pending = [j for j in jobs if j["kind"] != "discovery" and _ready(j)]
                if network.get("alchemy_network") and dj.get("status") == "success":
                    # Alchemy supplied the indexed holdings for this network. Keep any
                    # curated contracts it did not return pending for a later RPC pass,
                    # while allowing the cheap provider-only inventory to complete.
                    pending = []
                    continue
                if progress:
                    progress(
                        f"Wallet {wi + 1}/{len(scope['wallets'])} | "
                        f"{network['name']} | {len(pending)} checks"
                    )
                if not pending:
                    continue
                pinned = store.get_pass(run_id, wallet, cid)
                try:
                    url, block = _select_endpoint(rpc, network, pinned, blocked)
                except RequestError as exc:
                    for job in pending:
                        _failure(store, job, exc, "unavailable")
                    continue
                if pinned is None:
                    store.save_pass(run_id, wallet, cid, block)
                _balances(store, run_id, wallet, network, rpc, url, block, blocked)
        jobs = store.jobs(run_id)
        mandatory = [j for j in jobs if j["kind"] == "mandatory"]
        failures = sum(j["status"] != "success" for j in mandatory)
        gaps = sum(n["token_review_status"] == "pending" for n in scope["catalog"]["networks"])
        status = "incomplete" if failures or gaps else "completed"
        store.set_status(run_id, status)
        return {
            "run_id": run_id,
            "status": status,
            "mandatory_total": len(mandatory),
            "mandatory_failed": failures,
            "catalog_gaps": gaps,
            "discovery_statuses": {
                status: sum(j["kind"] == "discovery" and j["status"] == status for j in jobs)
                for status in ("success", "disabled", "error", "deferred", "pending", "unavailable")
            },
            "discovered_checks": sum(j["kind"] == "discovered" for j in jobs),
            "discovered_failed": sum(
                j["kind"] == "discovered" and j["status"] != "success" for j in jobs
            ),
            "positive_balances": sum(
                int((j.get("result") or {}).get("raw_balance", "0")) > 0 for j in jobs
            ),
        }
    except KeyboardInterrupt:
        store.set_status(run_id, "interrupted")
        raise
    finally:
        if transport is not None:
            transport.close()
