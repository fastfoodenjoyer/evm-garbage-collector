"""Read-only diagnosis of legacy unsigned transactions whose errors were truncated."""

from .diagnostics import exception_diagnostic
from .executor import ExecutionRpc
from .fee_planner import FeePlanner
from .lifi import TransactionRequest
from .route_preparation import is_fee_cap_changed
from .rpc import quantity
from .stages import StageDeadlineExceeded
from .transport import Transport


def diagnose_legacy_fee_cap(journal, group_id, entries, rpc_urls):
    """Establish a specific retry cause before reopening one legacy native route."""
    group = journal.group(group_id)
    if (group["state"] != "manual_review_group_halted"
            or group["reason"] != "execution_error:ValueError" or len(entries) != 1):
        return
    entry = entries[0]
    rows = journal.connection.execute(
        "SELECT * FROM route_positions WHERE group_id=?", (group_id,),
    ).fetchall()
    if (len(rows) != 1 or entry.get("asset_id") != "native"
            or entry.get("status") != "route_ready" or not entry.get("automatic_requote")):
        return
    position = rows[0]
    if (position["state"] != "manual_review" or position["reason"] != group["reason"]
            or position["source_asset_id"] != "native"
            or position["wallet"] != str(entry["wallet"]).lower()
            or position["source_amount_raw"] != str(entry["raw_balance"])
            or int(entry["chain_id"]) != group["source_chain_id"]
            or journal.connection.execute(
                "SELECT 1 FROM route_steps WHERE position_id=? LIMIT 1",
                (position["id"],),
            ).fetchone() is not None):
        return
    prepared = journal.checkpoint(position["id"], "prepared") or {}
    steps = prepared.get("steps", [])
    request = steps[0].get("_prepared_request") if len(steps) == 1 else None
    if (not isinstance(request, TransactionRequest)
            or request.chain_id != group["source_chain_id"]
            or not request.max_total_fee_cap_wei
            or request.max_total_fee_wei > request.max_total_fee_cap_wei
            or request.chain_id not in rpc_urls):
        return
    transport = Transport(interval=0)
    try:
        rpc = ExecutionRpc(transport)
        url = rpc_urls[request.chain_id]
        if quantity(rpc.call(url, "eth_chainId", [])) != request.chain_id:
            return
        FeePlanner(rpc).plan(url, request, sender=position["wallet"])
    except StageDeadlineExceeded:
        raise
    except Exception as error:
        if is_fee_cap_changed(error):
            journal.save_checkpoint(position["id"], "execution_failure", {
                "phase": "legacy_fee_cap_revalidation",
                "reason": "requote_required:fee_cap_changed",
                "diagnostic": exception_diagnostic(error),
            })
    finally:
        transport.close()
