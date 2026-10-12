"""Narrow gas-reserve proof for a canonical withdrawal after bridge delivery."""

from .weth import WETH_CONTRACTS, withdraw_transaction_fields


def withdrawal_reserve(entry, request, wallet) -> int | None:
    """Return a padded immutable cap; ordinary transactions use their usual policy."""
    marker = entry.get("canonical_continuation")
    if marker is None or entry.get("status") == "direct_deposit":
        return None
    try:
        chain = int(entry["chain_id"])
        amount = int(entry["raw_balance"])
        target = entry["target"]
        steps = entry["steps"]
        raw_steps = steps[0]["_live_route"].raw_steps
        cap = request.max_total_fee_cap_wei
        direct_cap = int(entry["reservations"]["final_deposit_native_gas_cap_raw"])
        minimum = int(target["minimum_raw"])
        if (
            not entry.get("automatic_requote") or not entry.get("local_unwrap")
            or not callable(entry.get("_check_group_loss"))
            or entry["asset_id"] != WETH_CONTRACTS.get(chain) or entry["decimals"] != 18
            or len(steps) != 1 or steps[0]["kind"] != "unwrap_to_native"
            or len(raw_steps) != 1
            or marker["wallet"].lower() != wallet.public_address.lower()
            or marker["recipient"].lower() != wallet.bitget_deposit_address.lower()
            or marker["chain_id"] != chain or int(marker["amount_raw"]) != amount
            or marker["target"] != target
            or (target["coin"], target["chain_id"], target["asset_id"], target["decimals"])
            != ("ETH", chain, "native", 18)
            or not target["chain"] or minimum <= 0 or direct_cap <= 0
            or cap is None or cap <= 0 or request.max_total_fee_wei > cap
            or amount - cap - 3 * direct_cap < minimum
        ):
            raise ValueError("invalid proof")
        expected = withdraw_transaction_fields(raw_steps[0])
        if ((request.chain_id, request.to.lower(), request.data.lower()) != expected
                or request.value != 0
                or raw_steps[0]["action"]["fromAddress"].lower()
                != wallet.public_address.lower()):
            raise ValueError("changed canonical transaction")
    except (KeyError, TypeError, ValueError, AttributeError, IndexError) as exc:
        raise ValueError("canonical_continuation_reserve_proof_invalid") from exc
    return (cap * 12 + 9) // 10


def unsigned_delivery(journal, position_id):
    """Correlate the saved source intent, exact arrival and still unsigned child."""
    position = journal.position(position_id)
    prepared = journal.checkpoint(position_id, "prepared")
    if not prepared or not prepared.get("automatic_requote"):
        return None
    for step in prepared.get("steps", []):
        route = step.get("_live_route")
        if step.get("kind") != "bridge" or route is None:
            continue
        normalized = tuple(s for s in route.evidence.steps if not s.is_substep)
        for index, source in enumerate(normalized[:-1]):
            key = f"bridge:{route.route_id}:{index}"
            delivery = journal.checkpoint(position_id, f"bridge_delivery:{key}")
            progress_key = f"route:bridge:{route.route_id}"
            progress = journal.checkpoint(position_id, progress_key) or {}
            child = progress.get("continuation")
            saved = journal.latest_step(position_id=position_id, step_key=key)
            if not delivery or not isinstance(child, dict) or not saved:
                continue
            try:
                chain = delivery["chain_id"]
                amount = int(delivery["amount_raw"])
                asset = WETH_CONTRACTS.get(chain)
                group = journal.group(position["group_id"])
                child_group = child["group"]
                if (
                    asset is None or delivery["asset_id"] != asset or delivery["decimals"] != 18
                    or amount <= 0 or saved["state"] != "credited" or not saved["tx_hash"]
                    or delivery["source_hash"] != saved["tx_hash"] or not delivery["tx_hash"]
                    or source.source.chain_id != group["source_chain_id"]
                    or source.source.contract_address != position["source_asset_id"]
                    or (source.destination.chain_id, source.destination.contract_address,
                        source.destination.decimals) != (chain, asset, 18)
                    or str(source.recipient).lower() != position["wallet"].lower()
                    or delivery["recipient"].lower() != position["wallet"].lower()
                    or child["wallet"].lower() != position["wallet"].lower()
                    or child.get("deposit_address") != prepared.get("deposit_address")
                    or (child["chain_id"], child["asset_id"], child["decimals"],
                        int(child["raw_balance"])) != (chain, asset, 18, amount)
                    or child.get("_actual_asset_id") != asset
                    or int(child.get("_actual_balance_raw", "0")) != amount
                    or position["actual_asset_id"] != asset
                    or int(position["actual_balance_raw"]) != amount
                    or child_group["key"] != group["group_key"]
                    or child_group["source_usd"] != group["source_usd"]
                    or child_group["max_route_loss_pct"] != group["loss_budget_pct"]
                    or child.get("target") != prepared.get("target")
                    or child.get("canonical_continuation")
                ):
                    continue
                others = journal.connection.execute(
                    "SELECT 1 FROM route_steps WHERE position_id=? AND step_key<>? LIMIT 1",
                    (position_id, key),
                ).fetchone()
                if others is not None:
                    continue
                return {"progress_key": progress_key, "source_step_key": key,
                        "source_hash": saved["tx_hash"], "delivery": delivery,
                        "continuation": child}
            except (KeyError, TypeError, ValueError, AttributeError):
                continue
    return None
