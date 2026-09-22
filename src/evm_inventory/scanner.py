"""Resumable wallet traversal backed by the current inventory store."""

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


def _ready(asset):
    return asset["status"] not in DONE and (asset.get("retry_after") or 0) <= time.time()


def _failure(store, asset, error, status="error"):
    store.record_asset(
        asset["id"],
        {"error": error.code, "observed_at": now()},
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
    for asset_id, metadata in specs:
        row_id = store.upsert_asset(
            wallet,
            network["chain_id"],
            asset_id,
            "mandatory",
            _mandatory_metadata(network, asset_id, metadata),
        )
        existing = next(
            row for row in store.assets(wallet, network["chain_id"]) if row["id"] == row_id
        )
        # Store intentionally exposes updates through record_asset.  Re-recording
        # the prior payload clears an old cooldown while preserving the last result
        # until the authoritative RPC answer replaces it.
        store.record_asset(row_id, existing["result"] or {}, status="pending")
        ids.append(row_id)
    return ids


def _rpc_urls(network):
    """Return the preferred endpoints for one network."""
    urls = list(network["rpc_urls"])
    key = os.environ.get("ALCHEMY_RPC_API_KEY") or os.environ.get("ALCHEMY_API_KEY")
    alchemy_network = network.get("alchemy_network")
    if key and alchemy_network:
        return (f"https://{alchemy_network}.g.alchemy.com/v2/{quote(key, safe='')}",)
    return tuple(dict.fromkeys(urls))


def _select_endpoint(rpc, network, pinned, blocked, *, rpc_urls=None):
    error = RequestError("no_rpc_available")
    for url in _rpc_urls(network) if rpc_urls is None else rpc_urls:
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


def _balances(store, assets, wallet, network, rpc, url, block, blocked):
    endpoint_error = None
    for asset in assets:
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
                exc = RequestError(exc.code, exc.retry_after or time.time() + 300)
                endpoint_error = exc
                blocked[url] = exc
            elif exc.code in PERMANENT_RPC_ERRORS:
                endpoint_error = exc
                blocked[url] = exc
            elif exc.code not in {"invalid_contract", "decimals_mismatch", "missing_contract_code"}:
                endpoint_error = RequestError(exc.code, exc.retry_after or time.time() + 300)
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
            contracts, next_cursor = discovery.page(wallet, network["alchemy_network"], cursor)
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
            error={"code": exc.code, "observed_at": now()},
        )
        return discovered_ids


def scan(store, scope, *, rpc=None, discovery=None, sleep=time.sleep, progress=None):
    """Scan the supplied scope into the store's current inventory."""
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
    networks = scope["catalog"]["networks"]
    rpc_urls = {network["chain_id"]: _rpc_urls(network) for network in networks}
    blocked = {}
    mandatory_total = mandatory_failed = discovered_checks = discovered_failed = (
        positive_balances
    ) = 0
    discovery_statuses = {
        status: 0
        for status in ("success", "disabled", "error", "deferred", "pending", "unavailable")
    }
    try:
        for wi, wallet in enumerate(scope["wallets"]):
            if wi:
                sleep(random.uniform(settings.get("delay_min", 1), settings.get("delay_max", 3)))
            for network in networks:
                cid = network["chain_id"]
                mandatory_ids = _ensure_mandatory(store, wallet, network)
                mandatory = [row for row in store.assets(wallet, cid) if row["id"] in mandatory_ids]
                mandatory_total += len(mandatory)
                _reset_discovery(store, wallet, cid)
                discovered_ids = _discover(store, network, wallet, discovery, set(mandatory_ids))
                state = store.discovery_state(wallet, cid)
                discovery_statuses[state["status"]] = discovery_statuses.get(state["status"], 0) + 1
                discovered = [
                    row for row in store.assets(wallet, cid) if row["id"] in discovered_ids
                ]
                discovered_checks += len(discovered)
                discovered_failed += sum(row["status"] != "success" for row in discovered)
                if progress:
                    progress(
                        f"Wallet {wi + 1}/{len(scope['wallets'])} | "
                        f"{network['name']} | {len(mandatory)} checks"
                    )
                try:
                    # A pass is a current snapshot, not a run-scoped immutable pin.
                    url, block = _select_endpoint(
                        rpc, network, None, blocked, rpc_urls=rpc_urls[cid]
                    )
                except RequestError as exc:
                    for asset in mandatory:
                        _failure(store, asset, exc, "unavailable")
                else:
                    store.save_pass(wallet, cid, block)
                    _balances(store, mandatory, wallet, network, rpc, url, block, blocked)
                current = [
                    row
                    for row in store.assets(wallet, cid)
                    if row["id"] in {*mandatory_ids, *discovered_ids}
                ]
                mandatory_failed += sum(
                    row["status"] != "success" for row in current if row["id"] in mandatory_ids
                )
                positive_balances += sum(
                    int((row["result"] or {}).get("raw_balance", "0")) > 0 for row in current
                )
        return {
            "mandatory_total": mandatory_total,
            "mandatory_failed": mandatory_failed,
            "catalog_gaps": sum(n["token_review_status"] == "pending" for n in networks),
            "discovery_statuses": discovery_statuses,
            "discovered_checks": discovered_checks,
            "discovered_failed": discovered_failed,
            "positive_balances": positive_balances,
        }
    finally:
        if transport is not None:
            transport.close()
