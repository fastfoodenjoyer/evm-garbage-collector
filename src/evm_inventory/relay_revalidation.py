"""Recheck legacy unsigned Relay refusals that used unverified provider gas hints."""

import time
from decimal import Decimal

from .bitget_catalog import deposit_targets, fetch_public_coins
from .executor import ExecutionRpc
from .journal import Journal
from .planner_gas import PlannerGasEstimator
from .relay import _search_error, quote_relay_entry, relay_gas_revalidation_needed
from .route_execution import _position_key
from .route_targets import resolve_targets
from .rpc import quantity
from .transport import Transport
from .wallet_lifi import WalletLifiClients


def revalidate_legacy_gas(entries, *, db_path, wallet, rpc_urls, target_policy, loss_limit,
                          quote_floor, rpc=None, clients=None, targets=None):
    candidates = [e for e in entries if relay_gas_revalidation_needed(e)
                  and e["wallet"].lower() == wallet.public_address.lower()
                  and isinstance(e.get("relay_probe", {}).get("request"), dict)
                  and e.get("source_balance_usd") is not None]
    with Journal(db_path) as journal:
        def untouched(entry):
            row = journal.connection.execute(
                "SELECT id,state FROM route_positions WHERE position_key=?", (_position_key(entry),)
            ).fetchone()
            return row is None or (row["state"] in {"planned", "manual_review"}
                and journal.connection.execute(
                    "SELECT 1 FROM route_steps WHERE position_id=?", (row["id"],)
                ).fetchone() is None)

        candidates = [e for e in candidates if untouched(e)]
        if not candidates:
            return 0
        transport = Transport(interval=0) if rpc is None else None
        rpc = rpc or ExecutionRpc(transport)
        owned_clients = clients is None
        changed = 0
        try:
            for entry in candidates:
                context = {k: entry[k] for k in ("group", "execution_key", "automatic_requote")
                           if k in entry}
                try:
                    probe = entry.get("relay_probe", {}).get("request", {})
                    if (probe.get("user", "").lower() != wallet.public_address.lower()
                            or probe.get("recipient", "").lower()
                            != wallet.bitget_deposit_address.lower()
                            or probe.get("originChainId") != entry["chain_id"]
                            or int(probe["amount"]) != int(entry["raw_balance"])):
                        raise ValueError("relay_legacy_gas_identity_changed")
                    url = rpc_urls[entry["chain_id"]]
                    if quantity(rpc.call(url, "eth_chainId", [])) != entry["chain_id"]:
                        raise ValueError("relay_source_rpc_chain_mismatch")
                    amount = min(int(entry["raw_balance"]), quantity(rpc.call(
                        url, "eth_getBalance", [wallet.public_address, "latest"])))
                    if amount <= 0:
                        raise ValueError("relay_source_balance_unavailable")
                    if clients is None:
                        clients = WalletLifiClients((wallet,), db_path=db_path, purpose="execution",
                                                   policy_digest=target_policy.digest)
                    if targets is None:
                        targets = resolve_targets(target_policy,
                                                  deposit_targets(fetch_public_coins())).enabled
                    base = {k: entry[k] for k in ("wallet", "chain_id", "asset_id", "symbol",
                                                  "decimals")}
                    base["raw_balance"] = str(amount)
                    fresh = quote_relay_entry(base, clients.relay_for_wallet(entry["wallet"]),
                        targets, loss_limit=Decimal(loss_limit), quote_floor=Decimal(quote_floor),
                        deposit_address=wallet.bitget_deposit_address,
                        gas_estimator=PlannerGasEstimator(rpc, rpc_urls, clients),
                        original_source_usd=entry["source_balance_usd"])
                except Exception as error:
                    detail = _search_error(error)
                    fresh = {**entry, "reason": "relay_source_gas_revalidation_failed",
                             "search": {"complete": False, "errors": [detail],
                                        "pending": [detail] if detail["retry_at"] else []}}
                if not untouched(entry):
                    continue
                fresh.update(context, quoted_at=int(time.time() * 1000))
                fresh["relay_gas_revalidation"] = dict(
                    previous_reason=entry["reason"], original_amount_raw=entry["raw_balance"],
                    original_source_usd=entry.get("source_balance_usd"))
                entry.clear()
                entry.update(fresh)
                changed += 1
        finally:
            if owned_clients and clients is not None:
                clients.close()
            if transport is not None:
                transport.close()
        return changed
