"""Sequential, explicit-only execution of prepared direct-deposit or Jumper routes."""

from __future__ import annotations

import os
import random
import time
from pathlib import Path

import httpx

from .bitget import BitgetClient
from .executor import (
    EthereumGasDeferred,
    ExecutionRpc,
    approve_transaction,
    broadcast_signed_transaction,
    pending_nonce,
    require_ethereum_gas_below_limit,
    require_ethereum_planned_gas_price_valid,
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
    wallet_batches: tuple[tuple[str, ...], ...] | None = None,
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
    summary = {"submitted": 0, "skipped": 0, "failed": 0, "deferred": 0}
    by_wallet: dict[str, list[dict]] = {}
    for entry in entries:
        if entry.get("status") in {"route_ready", "direct_deposit", "post_bridge_deposit"}:
            by_wallet.setdefault(str(entry["wallet"]).lower(), []).append(entry)
    try:
        with Journal(journal_path) as journal:
            batches = wallet_batches or (tuple(sorted(by_wallet)),)
            for batch_index, batch in enumerate(batches):
                wallet_order = list(batch)
                rng.shuffle(wallet_order)
                for wallet in wallet_order:
                    wallet_entries = by_wallet.get(wallet, [])
                    if not wallet_entries:
                        continue
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
                                entry=entry,
                                wallet=source,
                                rpc=rpc,
                                jumper=jumper,
                                rpc_urls=rpc_urls,
                            )
                            journal.record_transaction(operation_id, tx_hash)
                            if entry.get("settlement") == "wallet":
                                journal.record_deposit_status(operation_id, "staged")
                            else:
                                status = bitget.wait_for_deposit(
                                    tx_hash=tx_hash,
                                    started_ms=started_ms,
                                    coin=str(entry["target"]["coin"]),
                                )
                                journal.record_deposit_status(
                                    operation_id, (status or "timeout").lower()
                                )
                            summary["submitted"] += 1
                        except EthereumGasDeferred as exc:
                            if exc.approval_tx_hash is None:
                                journal.record_deferred(operation_id, exc.reason)
                            else:
                                journal.record_approval_completed_route_deferred(
                                    operation_id, exc.approval_tx_hash, exc.reason
                                )
                            summary["deferred"] += 1
                        except Exception:
                            journal.record_deposit_status(operation_id, "execution_failed")
                            summary["failed"] += 1
                    if wallet != wallet_order[-1]:
                        sleep(rng.randint(delay_min_seconds, delay_max_seconds))
                if batch_index != len(batches) - 1 and batch:
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
    require_ethereum_planned_gas_price_valid(request)
    balance = _native_balance(rpc, url=url, wallet=wallet.public_address)
    require_native_reserve(
        balance=balance - request.value,
        gas_cost=request.gas_limit * request.gas_price_wei,
    )
    asset_id = str(entry["asset_id"])
    approval_tx_hash = None
    if entry["status"] == "route_ready" and asset_id != "native":
        approval_tx_hash = _approve_if_needed(entry, request, wallet=wallet, rpc=rpc, url=url)
    nonce = pending_nonce(rpc, url=url, wallet=wallet.public_address)
    try:
        require_ethereum_gas_below_limit(rpc, url=url, request=request)
        raw = sign_transaction(
            request,
            private_key=wallet.private_key,
            expected_sender=wallet.public_address,
            nonce=nonce,
        )
    except EthereumGasDeferred as exc:
        exc.approval_tx_hash = approval_tx_hash
        raise
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
    if entry["status"] == "post_bridge_deposit":
        amount = _wait_for_staged_balance(
            rpc,
            url=url,
            token=asset_id,
            wallet=wallet.public_address,
            expected_minimum=amount,
        )
        if amount < int(target["minimum_raw"]):
            raise ValueError("staged USDC is below Bitget minimum")
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
) -> str | None:
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
        return None
    approval = approve_transaction(
        chain_id=request.chain_id,
        token=asset_id,
        spender=spender,
        amount=amount,
        gas_price_wei=request.gas_price_wei,
    )
    require_ethereum_planned_gas_price_valid(approval)
    balance = _native_balance(rpc, url=url, wallet=wallet.public_address)
    require_native_reserve(balance=balance, gas_cost=approval.gas_limit * approval.gas_price_wei)
    nonce = pending_nonce(rpc, url=url, wallet=wallet.public_address)
    require_ethereum_gas_below_limit(rpc, url=url, request=approval)
    raw = sign_transaction(
        approval,
        private_key=wallet.private_key,
        expected_sender=wallet.public_address,
        nonce=nonce,
    )
    tx_hash = broadcast_signed_transaction(rpc, url=url, raw_transaction=raw)
    wait_for_receipt(rpc, url=url, tx_hash=tx_hash)
    return tx_hash


def _native_balance(rpc: ExecutionRpc, *, url: str, wallet: str) -> int:
    return _quantity(rpc.call(url, "eth_getBalance", [wallet, "latest"]))


def _token_balance(rpc: ExecutionRpc, *, url: str, token: str, wallet: str) -> int:
    data = "0x70a08231" + wallet[2:].lower().rjust(64, "0")
    result = rpc.call(url, "eth_call", [{"to": token, "data": data}, "latest"])
    from .rpc import uint256

    return uint256(result)


def _wait_for_staged_balance(
    rpc: ExecutionRpc,
    *,
    url: str,
    token: str,
    wallet: str,
    expected_minimum: int,
    timeout_seconds: int = 21_600,
    poll_seconds: int = 60,
) -> int:
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        amount = _token_balance(rpc, url=url, token=token, wallet=wallet)
        if amount >= expected_minimum:
            return amount
        time.sleep(poll_seconds)
    raise TimeoutError("staged USDC did not arrive before timeout")


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
