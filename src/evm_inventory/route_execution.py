"""Sequential, explicit-only execution of prepared direct-deposit or Jumper routes."""

from __future__ import annotations

import os
import random
import time
from pathlib import Path

import httpx

from .bitget import BitgetClient
from .executor import (
    ExecutionRpc,
    approve_transaction,
    broadcast_signed_transaction,
    pending_nonce,
    require_native_reserve,
    sign_transaction,
    token_allowance,
    wait_for_receipt,
)
from .journal import Journal
from .lifi import LifiClient, TransactionRequest
from .transport import Transport
from .workbook import WalletWorkbookRow


def execute_entries(
    entries: list[dict],
    *,
    wallets: dict[str, WalletWorkbookRow],
    rpc_urls: dict[int, str],
    journal_path: Path,
    execute: bool,
    delay_min_seconds: int = 1800,
    delay_max_seconds: int = 10800,
    sleep=time.sleep,
    rng: random.Random | None = None,
) -> dict[str, int]:
    """Execute one wallet at a time; refuses unless the caller set ``execute=True``."""

    if not execute:
        raise ValueError("refusing to broadcast without --execute")
    if delay_min_seconds < 0 or delay_max_seconds < delay_min_seconds:
        raise ValueError("invalid execution delay range")
    rng = rng or random.Random()
    transport = Transport(interval=0.2)
    rpc = ExecutionRpc(transport)
    jumper = LifiClient(httpx.Client(timeout=30))
    bitget = _bitget_client()
    summary = {"submitted": 0, "skipped": 0, "failed": 0}
    by_wallet: dict[str, list[dict]] = {}
    for entry in entries:
        if entry.get("status") in {"route_ready", "direct_deposit"}:
            by_wallet.setdefault(str(entry["wallet"]).lower(), []).append(entry)
    try:
        with Journal(journal_path) as journal:
            for wallet, wallet_entries in sorted(by_wallet.items()):
                source = wallets.get(wallet)
                if source is None:
                    summary["skipped"] += len(wallet_entries)
                    continue
                for entry in wallet_entries:
                    operation_id = journal.create_operation(
                        wallet=wallet, action=str(entry["status"])
                    )
                    try:
                        started_ms = int(time.time() * 1000)
                        tx_hash = _execute_entry(
                            entry,
                            wallet=source,
                            rpc=rpc,
                            jumper=jumper,
                            rpc_urls=rpc_urls,
                        )
                        journal.record_transaction(operation_id, tx_hash)
                        status = bitget.wait_for_deposit(
                            tx_hash=tx_hash,
                            started_ms=started_ms,
                            coin=str(entry["target"]["coin"]),
                        )
                        journal.record_deposit_status(operation_id, (status or "timeout").lower())
                        summary["submitted"] += 1
                    except Exception:
                        journal.record_deposit_status(operation_id, "execution_failed")
                        summary["failed"] += 1
                if wallet != sorted(by_wallet)[-1]:
                    sleep(rng.randint(delay_min_seconds, delay_max_seconds))
    finally:
        transport.close()
    return summary


def _execute_entry(
    *,
    entry: dict,
    wallet: WalletWorkbookRow,
    rpc: ExecutionRpc,
    jumper: LifiClient,
    rpc_urls: dict[int, str],
) -> str:
    chain_id = int(entry["chain_id"])
    url = rpc_urls[chain_id]
    if entry["status"] == "route_ready":
        step = entry["route"]["step"]
        request = jumper.step_transaction(step)
    else:
        request = _direct_request(entry, wallet=wallet, rpc=rpc, url=url)
    if request.chain_id != chain_id:
        raise ValueError("route transaction chain does not match source balance")
    balance = _native_balance(rpc, url=url, wallet=wallet.public_address)
    require_native_reserve(
        balance=balance - request.value,
        gas_cost=request.gas_limit * request.gas_price_wei,
    )
    asset_id = str(entry["asset_id"])
    if entry["status"] == "route_ready" and asset_id != "native":
        _approve_if_needed(entry, request, wallet=wallet, rpc=rpc, url=url)
    nonce = pending_nonce(rpc, url=url, wallet=wallet.public_address)
    raw = sign_transaction(
        request,
        private_key=wallet.private_key,
        expected_sender=wallet.public_address,
        nonce=nonce,
    )
    tx_hash = broadcast_signed_transaction(rpc, url=url, raw_transaction=raw)
    wait_for_receipt(rpc, url=url, tx_hash=tx_hash)
    return tx_hash


def _direct_request(
    entry: dict, *, wallet: WalletWorkbookRow, rpc: ExecutionRpc, url: str
) -> TransactionRequest:
    target = entry["target"]
    chain_id = int(entry["chain_id"])
    gas_price = _quantity(rpc.call(url, "eth_gasPrice", []))
    asset_id = str(entry["asset_id"])
    if asset_id == "native":
        balance = _native_balance(rpc, url=url, wallet=wallet.public_address)
        gas_limit = 21_000
        amount = balance - 5 * gas_limit * gas_price
        if amount < int(target["minimum_raw"]):
            raise ValueError("native balance is below Bitget minimum after gas reserve")
        return TransactionRequest(
            chain_id, wallet.bitget_deposit_address, "0x", amount, gas_limit, gas_price
        )
    amount = int(entry["raw_balance"])
    recipient = wallet.bitget_deposit_address[2:].lower().rjust(64, "0")
    data = "0xa9059cbb" + recipient + hex(amount)[2:].rjust(64, "0")
    gas_limit = _quantity(
        rpc.call(
            url,
            "eth_estimateGas",
            [{"from": wallet.public_address, "to": asset_id, "data": data}],
        )
    )
    return TransactionRequest(chain_id, asset_id, data, 0, gas_limit, gas_price)


def _approve_if_needed(
    entry: dict,
    request: TransactionRequest,
    *,
    wallet: WalletWorkbookRow,
    rpc: ExecutionRpc,
    url: str,
) -> None:
    spender = entry["route"]["step"].get("estimate", {}).get("approvalAddress")
    asset_id = str(entry["asset_id"])
    amount = int(entry["raw_balance"])
    if not isinstance(spender, str):
        raise ValueError("Jumper route is missing an approval address")
    allowed = token_allowance(
        rpc,
        url=url,
        token=asset_id,
        owner=wallet.public_address,
        spender=spender,
    ) >= amount
    if allowed:
        return
    approval = approve_transaction(
        chain_id=request.chain_id,
        token=asset_id,
        spender=spender,
        amount=amount,
        gas_price_wei=request.gas_price_wei,
    )
    balance = _native_balance(rpc, url=url, wallet=wallet.public_address)
    require_native_reserve(balance=balance, gas_cost=approval.gas_limit * approval.gas_price_wei)
    nonce = pending_nonce(rpc, url=url, wallet=wallet.public_address)
    raw = sign_transaction(
        approval,
        private_key=wallet.private_key,
        expected_sender=wallet.public_address,
        nonce=nonce,
    )
    tx_hash = broadcast_signed_transaction(rpc, url=url, raw_transaction=raw)
    wait_for_receipt(rpc, url=url, tx_hash=tx_hash)


def _native_balance(rpc: ExecutionRpc, *, url: str, wallet: str) -> int:
    return _quantity(rpc.call(url, "eth_getBalance", [wallet, "latest"]))


def _quantity(value: object) -> int:
    from .rpc import quantity

    return quantity(value)


def _bitget_client() -> BitgetClient:
    keys = ("BITGET_API_KEY", "BITGET_SECRET_KEY", "BITGET_PASSPHRASE")
    missing = [key for key in keys if not os.environ.get(key)]
    if missing:
        raise ValueError("Bitget API credentials are required for execution")
    return BitgetClient(
        api_key=os.environ["BITGET_API_KEY"],
        secret_key=os.environ["BITGET_SECRET_KEY"],
        passphrase=os.environ["BITGET_PASSPHRASE"],
    )
