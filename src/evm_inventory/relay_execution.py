"""Explicit native Relay sends and observation using the shared execution journal."""

from __future__ import annotations

import fcntl
import re
import time
from datetime import datetime
from decimal import Decimal

from eth_utils import keccak

from .diagnostics import exception_diagnostic
from .executor import (
    ExecutionRpc,
    broadcast_durable_transaction,
    pending_nonce,
    sign_transaction,
    signed_transaction_hash,
)
from .fee_planner import FeePlanner
from .journal import Journal
from .relay import SOURCE_CHAINS
from .relay_protocol import verified_transaction
from .rpc import quantity
from .stages import Deadline, StageDeadlineExceeded, check_deadline, deadline_scope
from .transport import Transport
from .wallet_lifi import WalletLifiClients

_HASH = re.compile(r"0x[0-9a-fA-F]{64}")
_TRANSFER = "0x" + keccak(text="Transfer(address,address,uint256)").hex()


class RelayTerminalFailure(ValueError):
    """A refund or failed settlement requires human review, never another deposit."""


def _confirmed(rpc, url, receipt):
    latest = rpc.call(url, "eth_getBlockByNumber", ["latest", False])
    return quantity(latest["number"]) >= quantity(receipt["blockNumber"]) + 1


def _prepare(entry, wallet, client, rpc, url, target, limit):
    if (
        entry["chain_id"] not in SOURCE_CHAINS
        or entry["asset_id"] != "native"
        or entry["decimals"] != 18
    ):
        raise ValueError("Relay execution supports native ETH on Zora, Shape and Superseed only")
    if target.coin != "USDC" or target.asset_id == "native":
        raise ValueError("Relay execution requires an approved USDC Bitget target")
    if quantity(rpc.call(url, "eth_chainId", [])) != entry["chain_id"]:
        raise ValueError("Relay source RPC chain mismatch")
    if entry.get("deposit_address", "").lower() != wallet.bitget_deposit_address.lower():
        raise ValueError("Relay recipient differs from the trusted workbook address")
    balance = quantity(rpc.call(url, "eth_getBalance", [wallet.public_address, "latest"]))
    amount = int(entry["relay_selected"]["request"]["amount"])
    if not 0 < amount < balance:
        raise ValueError("Relay planned amount exceeds live native balance or leaves no gas")
    client.supported_chain_ids()
    quote = client.quote(entry, target, amount, recipient=wallet.bitget_deposit_address, fresh=True)
    tx = FeePlanner(rpc).plan(
        url, verified_transaction(quote, client.chain_catalog), sender=wallet.public_address
    )
    fee = tx.max_total_fee_wei
    if balance < amount + fee:
        raise ValueError("Relay live balance cannot cover input and maximum RPC gas fee")
    if int(quote["to_amount_min"]) < target.minimum_raw:
        raise ValueError("Relay minimum output is below the current Bitget deposit minimum")
    # Input plus the maximum transaction fee is the amount at risk; retained ETH is excluded.
    input_usd = Decimal(quote["source_usd"])
    unit = input_usd / amount
    cost_usd = input_usd + Decimal(fee) * unit
    loss = (cost_usd - Decimal(quote["minimum_output_usd"])) / cost_usd * 100
    if not loss.is_finite() or loss > limit:
        raise ValueError(f"Relay loss {loss:.2f}% exceeds {limit}% before signing")
    if time.time() - datetime.fromisoformat(quote["observed_at"]).timestamp() > 60:
        raise ValueError("Relay execution quote expired during preflight")
    # Re-check balance and protocol deadline immediately before signing.
    verified_transaction(quote, client.chain_catalog)
    if quantity(rpc.call(url, "eth_getBalance", [wallet.public_address, "pending"])) < amount + fee:
        raise ValueError("Relay pending balance cannot cover the prepared transaction")
    return quote, tx


def _destination_transfer(rpc, rpc_urls, checkpoint, hashes):
    target = checkpoint["target"]
    url = rpc_urls[target["chain_id"]]
    if quantity(rpc.call(url, "eth_chainId", [])) != target["chain_id"]:
        raise ValueError("Relay destination RPC chain mismatch")
    recipient_topic = "0x" + checkpoint["recipient"][2:].lower().rjust(64, "0")
    minimum = int(checkpoint["quote"]["to_amount_min"])
    matched = []
    total = 0
    for tx_hash in dict.fromkeys(hashes):
        if not isinstance(tx_hash, str) or not _HASH.fullmatch(tx_hash):
            raise ValueError("Relay returned an invalid destination transaction hash")
        receipt = rpc.call(url, "eth_getTransactionReceipt", [tx_hash])
        if receipt is None:
            return None
        if (
            str(receipt.get("transactionHash", "")).lower() != tx_hash.lower()
            or quantity(receipt["status"]) != 1
        ):
            raise ValueError("Relay destination transaction receipt is invalid or reverted")
        if not _confirmed(rpc, url, receipt):
            return None
        received = 0
        for log in receipt.get("logs", []):
            topics = log.get("topics", [])
            if (
                not log.get("removed")
                and log.get("address", "").lower() == target["asset_id"].lower()
                and len(topics) == 3
                and topics[0].lower() == _TRANSFER
                and topics[2].lower() == recipient_topic
            ):
                received += quantity(log["data"])
        if received:
            total += received
            matched.append(tx_hash)
    if total < minimum or len(matched) != 1:
        raise ValueError(
            "Relay destination receipt does not prove the minimum USDC payment in one deposit"
        )
    return matched[0]


def _observe(journal, position, checkpoint, client, rpc, rpc_urls, bitget):
    step = journal.latest_step(position_id=position["id"], step_key="relay_deposit")
    if not step or not step.get("tx_hash"):
        raise ValueError("Relay checkpoint has no durable broadcast hash")
    source_hash = step["tx_hash"]
    receipt = rpc.call(
        rpc_urls[checkpoint["source_chain_id"]], "eth_getTransactionReceipt", [source_hash]
    )
    if receipt is None:
        return "pending"
    if str(receipt.get("transactionHash", "")).lower() != source_hash.lower():
        raise ValueError("Relay source receipt hash mismatch")
    if quantity(receipt["status"]) != 1:
        if step["state"] == "submitted":
            journal.record_receipt(step["id"], status="reverted")
        raise RelayTerminalFailure("Relay source deposit reverted")
    if not _confirmed(rpc, rpc_urls[checkpoint["source_chain_id"]], receipt):
        return "pending"
    if step["state"] == "submitted":
        journal.record_receipt(step["id"], status="confirmed")
    status = client.status(checkpoint["request_id"])
    journal.save_checkpoint(position["id"], "relay_status", status)
    state = status.get("status")
    if state in {"failure", "refund"}:
        raise RelayTerminalFailure(
            f"Relay {state}: {status.get('failReason') or status.get('details') or status}"
        )
    if state in {"waiting", "depositing", "pending", "submitted", "delayed"}:
        return "pending"
    if state != "success":
        raise ValueError(f"Unknown Relay status: {state}")
    if (
        int(status["originChainId"]) != checkpoint["source_chain_id"]
        or int(status["destinationChainId"]) != checkpoint["target"]["chain_id"]
        or source_hash.lower() not in [str(h).lower() for h in status["inTxHashes"]]
    ):
        raise ValueError("Relay status does not match the submitted transfer")
    destination_hash = _destination_transfer(rpc, rpc_urls, checkpoint, status["txHashes"])
    if destination_hash is None:
        return "pending"
    journal.save_checkpoint(position["id"], "relay_destination", {"tx_hash": destination_hash})
    if journal.position(position["id"])["state"] == "bridge_submitted":
        journal.record_position_state(position["id"], "deposit_pending")
    target = checkpoint["target"]
    credit = bitget.deposit_status(
        tx_hash=destination_hash,
        start_ms=checkpoint["started_ms"] - 60000,
        end_ms=int(time.time() * 1000),
        coin=target["coin"],
        chain=target["chain"],
        recipient=checkpoint["recipient"],
        minimum_raw=int(checkpoint["quote"]["to_amount_min"]),
    )
    journal.save_checkpoint(position["id"], "relay_bitget_credit", {"status": credit})
    if str(credit).lower() in {"fail", "failed"}:
        raise RelayTerminalFailure(f"Bitget Relay deposit failed: {credit}")
    if str(credit).lower() != "success":
        return "pending"
    journal.record_step_state(step["id"], "credited")
    journal.record_position_state(position["id"], "completed")
    return "completed"


def execute_relay_entries(
    entries,
    *,
    wallets,
    rpc_urls,
    journal_path,
    bitget,
    target_validator,
    position_key,
    max_route_loss_pct,
    stage_seconds,
    execute=False,
    trust_deposit_addresses=False,
):
    """Called only after execute_entries has enforced explicit execution/trust flags."""
    if not execute or not trust_deposit_addresses:
        raise ValueError("Relay sending requires --execute and trusted deposit addresses")
    limit = Decimal(max_route_loss_pct)
    if not limit.is_finite() or not 0 <= limit <= 15:
        raise ValueError("Relay maximum loss must be between 0% and 15%")
    summary = dict(submitted=0, skipped=0, failed=0, deferred=0, pending=0, completed=0)
    summary["relay_diagnostics"] = []
    # Serialize Relay preparation through broadcast across processes, using no additional journal.
    with open(str(journal_path) + ".relay-execution.lock", "a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        transport = Transport(interval=0.2)
        clients = WalletLifiClients(wallets.values(), db_path=journal_path, purpose="execution")
        rpc = ExecutionRpc(transport)
        try:
            with Journal(journal_path) as journal:
                for entry in entries:
                    wallet = wallets.get(entry["wallet"].lower())
                    if wallet is None:
                        summary["skipped"] += 1
                        continue
                    position = journal.get_or_create_position(
                        position_key=position_key(entry), wallet=entry["wallet"]
                    )
                    if position["state"] in {"completed", "manual_review"}:
                        summary["skipped"] += 1
                        continue
                    try:
                        with deadline_scope(Deadline(stage_seconds, "relay_execution")):
                            client = clients.relay_for_wallet(entry["wallet"])
                            checkpoint = journal.checkpoint(position["id"], "relay_execution")
                            if journal.has_broadcast(position["id"]):
                                if (
                                    checkpoint
                                    and checkpoint["recipient"].lower()
                                    != wallet.bitget_deposit_address.lower()
                                ):
                                    raise ValueError(
                                        "Workbook recipient changed since Relay submission"
                                    )
                                if not checkpoint:
                                    raise ValueError(
                                        "Existing broadcast belongs to another executor; "
                                        "Relay will not resend"
                                    )
                            else:
                                if entry.get("status") != "route_ready" or not entry.get(
                                    "execution_supported"
                                ):
                                    summary["skipped"] += 1
                                    continue
                                target = target_validator(entry, wallet=wallet, bitget=bitget)
                                if target.chain_id not in rpc_urls:
                                    raise ValueError("Missing Relay destination RPC")
                                quote, tx = _prepare(
                                    entry,
                                    wallet,
                                    client,
                                    rpc,
                                    rpc_urls[entry["chain_id"]],
                                    target,
                                    limit,
                                )
                                checkpoint = dict(
                                    quote=quote,
                                    source_chain_id=entry["chain_id"],
                                    target=entry["target"],
                                    recipient=wallet.bitget_deposit_address,
                                    request_id=quote["response"]["steps"][0]["requestId"],
                                    started_ms=int(time.time() * 1000),
                                )
                                journal.save_checkpoint(
                                    position["id"], "relay_execution", checkpoint
                                )
                                nonce = pending_nonce(
                                    rpc,
                                    url=rpc_urls[entry["chain_id"]],
                                    wallet=wallet.public_address,
                                )
                                check_deadline()
                                signed = sign_transaction(
                                    tx,
                                    private_key=wallet.private_key,
                                    expected_sender=wallet.public_address,
                                    nonce=nonce,
                                )
                                broadcast_durable_transaction(
                                    rpc,
                                    url=rpc_urls[entry["chain_id"]],
                                    journal=journal,
                                    position_id=position["id"],
                                    step_key="relay_deposit",
                                    nonce=nonce,
                                    calldata=tx.data,
                                    signed_transaction=signed,
                                    tx_hash=signed_transaction_hash(signed),
                                )
                                summary["submitted"] += 1
                            if journal.position(position["id"])["state"] == "planned":
                                journal.record_position_state(position["id"], "bridge_submitted")
                            summary[
                                _observe(
                                    journal, position, checkpoint, client, rpc, rpc_urls, bitget
                                )
                            ] += 1
                    except RelayTerminalFailure as error:
                        diagnostic = exception_diagnostic(error)
                        journal.save_checkpoint(position["id"], "relay_diagnostic", diagnostic)
                        journal.record_position_state(
                            position["id"], "manual_review", reason=str(error)
                        )
                        summary["relay_diagnostics"].append(
                            {
                                "wallet": entry["wallet"],
                                "chain_id": entry["chain_id"],
                                "diagnostic": diagnostic,
                            }
                        )
                        summary["failed"] += 1
                    except StageDeadlineExceeded as error:
                        summary["relay_diagnostics"].append(
                            {
                                "wallet": entry["wallet"],
                                "chain_id": entry["chain_id"],
                                "diagnostic": exception_diagnostic(error),
                            }
                        )
                        journal.save_checkpoint(
                            position["id"], "relay_diagnostic", exception_diagnostic(error)
                        )
                        summary[
                            "pending" if journal.has_broadcast(position["id"]) else "deferred"
                        ] += 1
                    except Exception as error:
                        summary["relay_diagnostics"].append(
                            {
                                "wallet": entry["wallet"],
                                "chain_id": entry["chain_id"],
                                "diagnostic": exception_diagnostic(error),
                            }
                        )
                        journal.save_checkpoint(
                            position["id"], "relay_diagnostic", exception_diagnostic(error)
                        )
                        # Provider failures remain observable; never authorize a second send.
                        summary["failed"] += 1
        finally:
            clients.close()
            transport.close()
    return summary
