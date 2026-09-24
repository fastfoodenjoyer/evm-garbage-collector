"""Explicit, single-action execution of reviewed Rabby DeFi withdrawals."""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
import re
import time
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

from eth_abi import encode
from eth_utils import function_signature_to_4byte_selector, keccak

from .defi_plan import create_defi_plan
from .executor import (
    AmbiguousBroadcast,
    broadcast_durable_transaction,
    pending_nonce,
    recover_durable_broadcast,
    require_ethereum_gas_below_limit,
    require_native_reserve,
    sign_transaction,
    signed_transaction_hash,
    token_allowance,
    wait_for_receipt,
)
from .fee_planner import FeePlanner
from .journal import Journal
from .lifi import TransactionRequest
from .rabby import (
    FUEL_NATIVE_WITHDRAW,
    FUEL_PREDEPOSITS,
    ZERO_ADDRESS,
    EncodedAction,
    RabbyClient,
    encode_action,
)
from .rpc import RpcError, quantity, uint256
from .workbook import WalletWorkbookRow

_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_TRANSFER_TOPIC = "0x" + keccak(text="Transfer(address,address,uint256)").hex()
_WITHDRAW_TOPIC = "0x" + keccak(text="Withdraw(address,uint256,uint256)").hex()
_STARGATE_BSC_ESCROW = "0xd4888870c8686c748232719051b677791dbda26d"
_STARGATE_BSC_TOKEN = "0xb0d502e938ed5f4df2e681fe6e419ff29631d62b"
_FUEL_WITHDRAW_TOPIC = "0x" + keccak(text="Withdraw(address,address,address,uint240,uint16)").hex()


@contextmanager
def _action_lock(journal_path: Path):
    """Serialise signing with every writer of the shared inventory database."""

    resolved = journal_path.resolve()
    lock_path = resolved.with_name(resolved.name + ".lock")
    fd = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o600)
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as error:
            raise ValueError("inventory database is in use by another writer") from error
        yield
    finally:
        os.close(fd)


class Rpc(Protocol):
    def call(self, url: str, method: str, params: list[object]) -> object: ...


@dataclass(frozen=True, slots=True)
class PreparedExit:
    entry: dict[str, Any]
    request: TransactionRequest
    action: EncodedAction
    gas_cost_wei: int


def select_reviewed_action(
    plan_bytes: bytes,
    *,
    plan_sha256: str,
    action_id: str,
    wallet: str,
    now_seconds: int,
    allow_stale_for_recovery: bool = False,
) -> dict[str, Any]:
    if not _SHA256.fullmatch(plan_sha256) or hashlib.sha256(plan_bytes).hexdigest() != plan_sha256:
        raise ValueError("reviewed plan digest does not match file")
    if not _SHA256.fullmatch(action_id):
        raise ValueError("invalid DeFi action ID")
    plan = json.loads(plan_bytes)
    if not isinstance(plan, dict) or plan.get("schema") != "rabby-defi-withdraw-v1":
        raise ValueError("invalid DeFi plan schema")
    created_at = plan.get("created_at")
    if isinstance(created_at, bool) or not isinstance(created_at, int):
        raise ValueError("invalid DeFi plan timestamp")
    if not allow_stale_for_recovery and not 0 <= now_seconds - created_at <= 900:
        raise ValueError("DeFi plan is stale or from the future")
    entries = plan.get("entries")
    if not isinstance(entries, list):
        raise ValueError("invalid DeFi plan entries")
    selected = [
        entry
        for entry in entries
        if isinstance(entry, dict) and entry.get("action_id") == action_id
    ]
    if len(selected) != 1 or selected[0].get("status") != "ready":
        raise ValueError("DeFi action ID does not select one ready position")
    entry = selected[0]
    if entry.get("wallet") != wallet.lower():
        raise ValueError("selected DeFi action belongs to another wallet")
    return entry


def prepare_defi_action(
    plan_bytes: bytes,
    *,
    plan_sha256: str,
    action_id: str,
    wallet: str,
    rabby: RabbyClient,
    rpc: Rpc,
    rpc_url: str,
    max_gas_wei: int,
    now_seconds: int | None = None,
) -> PreparedExit:
    """Refresh and preflight one selected action without signing or broadcasting."""

    now_seconds = int(time.time()) if now_seconds is None else now_seconds
    entry = select_reviewed_action(
        plan_bytes,
        plan_sha256=plan_sha256,
        action_id=action_id,
        wallet=wallet,
        now_seconds=now_seconds,
    )
    if (
        isinstance(max_gas_wei, bool)
        or not isinstance(max_gas_wei, int)
        or max_gas_wei <= 0
    ):
        raise ValueError("invalid gas cap")
    chain_id = entry.get("chain_id")
    if isinstance(chain_id, bool) or not isinstance(chain_id, int) or chain_id <= 0:
        raise ValueError("invalid planned chain")
    fresh = create_defi_plan(
        [wallet], rabby, supported_chain_ids={chain_id}, now_seconds=now_seconds
    )
    matching = [row for row in fresh["entries"] if row.get("action_id") == action_id]
    if len(matching) != 1 or matching[0]["status"] != "ready":
        raise ValueError("Rabby withdrawal action changed or disappeared")
    current = matching[0]
    if current["chain_id"] != chain_id or current["action"] != entry.get("action"):
        raise ValueError("Rabby withdrawal action changed")
    action = encode_action(current["action"], wallet=wallet, now_seconds=now_seconds)
    if quantity(rpc.call(rpc_url, "eth_chainId", [])) != chain_id:
        raise ValueError("RPC chain does not match DeFi action chain")
    if _is_fuel_native_exit(current):
        call_data = "0x" + (
            function_signature_to_4byte_selector("getBalance(address,address)")
            + encode(["address", "address"], [wallet, ZERO_ADDRESS])
        ).hex()
        deposit = uint256(rpc.call(rpc_url, "eth_call", [
            {"to": FUEL_PREDEPOSITS, "data": call_data}, "latest"
        ]))
        if deposit != int(current["action"]["str_params"][2]):
            raise ValueError("Fuel deposit balance differs from Rabby withdrawal amount")
    if action.approval_token:
        allowance = token_allowance(
            rpc,
            url=rpc_url,
            token=action.approval_token,
            owner=wallet,
            spender=action.approval_spender or "",
        )
        if allowance < action.approval_amount:
            raise ValueError("DeFi action needs a separate approval before withdrawal")
    request = FeePlanner(rpc).plan(
        rpc_url,
        TransactionRequest(chain_id, action.to, action.data, 0, 0, 0),
        sender=wallet,
        max_total_fee_wei=max_gas_wei,
    )
    rpc.call(
        rpc_url,
        "eth_call",
        [
            {"from": wallet, "to": request.to, "data": request.data, "value": "0x0"},
            "latest",
        ],
    )
    gas_cost = request.max_total_fee_wei
    balance = quantity(rpc.call(rpc_url, "eth_getBalance", [wallet, "latest"]))
    require_native_reserve(balance=balance, gas_cost=gas_cost)
    tokens = tuple(current.get("output_token_ids") or ())
    if not tokens:
        raise ValueError("no verifiable withdrawal output token")
    return PreparedExit(current, request, action, gas_cost)


def execute_defi_action(
    plan_bytes: bytes,
    *,
    plan_sha256: str,
    action_id: str,
    wallet_row: WalletWorkbookRow,
    rabby: RabbyClient,
    rpc: Rpc,
    rpc_url: str,
    journal_path: Path,
    max_gas_wei: int,
    execute: bool,
    now_seconds: int | None = None,
) -> dict[str, Any]:
    """Preview or execute one reviewed withdrawal, never a batch of positions."""

    now_seconds = int(time.time()) if now_seconds is None else now_seconds
    wallet = wallet_row.public_address.lower()
    selected = select_reviewed_action(
        plan_bytes,
        plan_sha256=plan_sha256,
        action_id=action_id,
        wallet=wallet,
        now_seconds=now_seconds,
        allow_stale_for_recovery=execute,
    )
    if not execute:
        prepared = prepare_defi_action(
            plan_bytes,
            plan_sha256=plan_sha256,
            action_id=action_id,
            wallet=wallet,
            rabby=rabby,
            rpc=rpc,
            rpc_url=rpc_url,
            max_gas_wei=max_gas_wei,
            now_seconds=now_seconds,
        )
        return {
            "status": "preview",
            "action_id": action_id,
            "approval_required": False,
            "estimated_gas_wei": prepared.gas_cost_wei,
            "fee_quote": _fee_quote_detail(prepared.request),
            "contract": prepared.action.to,
        }
    journal_path.parent.mkdir(parents=True, exist_ok=True)
    with _action_lock(journal_path), Journal(journal_path) as journal:
        position = journal.get_or_create_position(
            position_key=f"defi:{wallet}:{action_id}", wallet=wallet
        )
        position_id = position["id"]
        # A recorded broadcast is always observed before consulting a fresh action.
        # This prevents a completed exit from being signed again after it disappears
        # from Rabby's portfolio response.
        existing = journal.latest_step(position_id=position_id, step_key="defi_withdraw")
        if existing and not existing.get("tx_hash"):
            raise ValueError("unresolved prior DeFi intent; inspect journal before retrying")
        if existing and existing.get("tx_hash"):
            tx_hash = recover_durable_broadcast(
                rpc,
                url=rpc_url,
                journal=journal,
                position_id=position_id,
                step_key="defi_withdraw",
            )
            assert tx_hash is not None
            receipt = wait_for_receipt(rpc, url=rpc_url, tx_hash=tx_hash)
            _record_defi_receipt(journal, existing["id"], receipt)
            result = _withdrawal_result(selected, action_id, tx_hash, receipt)
            result["fee_quote"] = _fee_quote_detail(None, receipt)
            return result
        prepared = prepare_defi_action(
            plan_bytes,
            plan_sha256=plan_sha256,
            action_id=action_id,
            wallet=wallet,
            rabby=rabby,
            rpc=rpc,
            rpc_url=rpc_url,
            max_gas_wei=max_gas_wei,
            now_seconds=now_seconds,
        )
        if selected["chain_id"] != prepared.entry["chain_id"]:
            raise ValueError("DeFi chain changed")
        tx_hash, submitted_request = _submit_durable(
            prepared.request,
            wallet_row=wallet_row,
            rpc=rpc,
            rpc_url=rpc_url,
            journal=journal,
            position_id=position_id,
            step_key="defi_withdraw",
        )
        receipt = wait_for_receipt(rpc, url=rpc_url, tx_hash=tx_hash)
        step = journal.latest_step(position_id=position_id, step_key="defi_withdraw")
        assert step is not None
        _record_defi_receipt(journal, step["id"], receipt)
        result = _withdrawal_result(selected, action_id, tx_hash, receipt)
        result["fee_quote"] = _fee_quote_detail(submitted_request, receipt)
        return result


def _fee_quote_detail(
    request: TransactionRequest | None, receipt: dict[str, Any] | None = None
) -> dict[str, str | int | None]:
    actual_fee = None
    gas_used = None
    effective_gas_price = None
    actual_l1_fee = None
    if receipt is not None and receipt.get("gasUsed") is not None:
        gas_used = quantity(receipt["gasUsed"])
        if receipt.get("effectiveGasPrice") is not None:
            effective_gas_price = quantity(receipt["effectiveGasPrice"])
            actual_l1_fee = (
                quantity(receipt["l1Fee"]) if receipt.get("l1Fee") is not None else 0
            )
            actual_fee = str(gas_used * effective_gas_price + actual_l1_fee)
    return {
        "gas_estimate": request.gas_estimate if request else None,
        "gas_limit": request.gas_limit if request else None,
        "gas_price_wei": str(request.gas_price_wei) if request and request.gas_price_wei else None,
        "max_fee_per_gas_wei": (
            str(request.max_fee_per_gas_wei)
            if request and request.max_fee_per_gas_wei is not None else None
        ),
        "max_priority_fee_per_gas_wei": (
            str(request.max_priority_fee_per_gas_wei)
            if request and request.max_priority_fee_per_gas_wei is not None else None
        ),
        "base_fee_per_gas_wei": (
            str(request.base_fee_per_gas_wei)
            if request and request.base_fee_per_gas_wei is not None else None
        ),
        "additional_fee_wei": str(request.additional_fee_wei) if request else None,
        "estimated_max_total_fee_wei": str(request.max_total_fee_wei) if request else None,
        "fee_cap_wei": (
            str(request.max_total_fee_cap_wei)
            if request and request.max_total_fee_cap_wei
            else None
        ),
        "quote_method": request.fee_quote_method if request else None,
        "quote_fallback_reason": request.fee_quote_fallback_reason if request else None,
        "quote_block_number": request.fee_quote_block_number if request else None,
        "actual_gas_used": gas_used,
        "actual_effective_gas_price_wei": (
            str(effective_gas_price) if effective_gas_price is not None else None
        ),
        "actual_l1_fee_wei": str(actual_l1_fee) if actual_l1_fee is not None else None,
        "actual_total_fee_wei": actual_fee,
    }


def _record_defi_receipt(journal: Journal, step_id: int, receipt: dict[str, Any]) -> None:
    block = receipt.get("blockNumber")
    journal.record_receipt(
        step_id, status="confirmed", finality_block=quantity(block) if block is not None else None
    )


def _stargate_withdrawn_raw(entry: dict[str, Any], receipt: dict[str, Any]) -> int | None:
    """Match the escrow's Withdraw event to the STG transfer in this transaction."""

    action = entry["action"]
    if not (
        entry.get("chain_id") == 56
        and entry.get("protocol_id") == "bsc_stargate"
        and str(entry.get("pool_id", "")).lower() == _STARGATE_BSC_ESCROW
        and str(action.get("contract_id", "")).lower() == _STARGATE_BSC_ESCROW
        and action.get("func") in {"withdraw()", "withdraw()()"}
        and entry.get("output_token_ids") == [_STARGATE_BSC_TOKEN]
    ):
        return None
    logs = receipt.get("logs")
    if not isinstance(logs, list):
        return None
    wallet_topic = "0x" + entry["wallet"][2:].lower().rjust(64, "0")
    escrow_topic = "0x" + _STARGATE_BSC_ESCROW[2:].rjust(64, "0")
    withdrawn: list[int] = []
    received: list[int] = []
    for log in logs:
        if not isinstance(log, dict):
            continue
        topics = log.get("topics")
        data = log.get("data")
        if not isinstance(topics, list) or not isinstance(data, str) or not topics:
            continue
        token = str(log.get("address", "")).lower()
        if (
            token == _STARGATE_BSC_ESCROW
            and len(topics) == 2
            and str(topics[0]).lower() == _WITHDRAW_TOPIC
            and str(topics[1]).lower() == wallet_topic
            and re.fullmatch(r"0x[0-9a-fA-F]{128}", data)
        ):
            withdrawn.append(uint256(data[:66]))
        if (
            token == _STARGATE_BSC_TOKEN
            and len(topics) == 3
            and str(topics[0]).lower() == _TRANSFER_TOPIC
            and str(topics[1]).lower() == escrow_topic
            and str(topics[2]).lower() == wallet_topic
        ):
            try:
                received.append(uint256(data))
            except (TypeError, ValueError, RpcError):
                continue
    if len(withdrawn) == len(received) == 1 and withdrawn[0] > 0 and withdrawn[0] == received[0]:
        return received[0]
    return None


def _is_fuel_native_exit(entry: dict[str, Any]) -> bool:
    action = entry.get("action") or {}
    params = action.get("str_params") or []
    return (
        entry.get("chain_id") == 1
        and entry.get("protocol_id") == "fuel"
        and str(entry.get("pool_id", "")).lower() == FUEL_PREDEPOSITS
        and entry.get("output_token_ids") == ["eth"]
        and str(action.get("contract_id", "")).lower() == FUEL_PREDEPOSITS
        and str(action.get("func", "")).startswith(FUEL_NATIVE_WITHDRAW)
        and len(params) == 3
        and str(params[0]).lower() == ZERO_ADDRESS
        and str(params[1]).lower() == entry.get("wallet")
        and str(params[2]).isdecimal()
        and int(params[2]) > 0
    )


def _fuel_withdrawn_raw(entry: dict[str, Any], receipt: dict[str, Any]) -> int | None:
    logs = receipt.get("logs")
    if not isinstance(logs, list):
        return None
    wallet_topic = "0x" + entry["wallet"][2:].lower().rjust(64, "0")
    zero_topic = "0x" + "0" * 64
    expected = int(entry["action"]["str_params"][2])
    matching = []
    for log in logs:
        if not isinstance(log, dict) or str(log.get("address", "")).lower() != FUEL_PREDEPOSITS:
            continue
        topics, data = log.get("topics"), log.get("data")
        if (
            isinstance(topics, list)
            and len(topics) == 4
            and [str(topic).lower() for topic in topics]
            == [_FUEL_WITHDRAW_TOPIC, wallet_topic, wallet_topic, zero_topic]
            and isinstance(data, str)
            and re.fullmatch(r"0x[0-9a-fA-F]{128}", data)
            and int(data[66:], 16) == 0
        ):
            matching.append(int(data[2:66], 16))
    return expected if matching == [expected] else None


def _withdrawal_result(
    entry: dict[str, Any], action_id: str, tx_hash: str, receipt: dict[str, Any]
) -> dict[str, Any]:
    """Only claim delivered assets when the receipt proves action-specific minima."""

    action = entry["action"]
    if _is_fuel_native_exit(entry):
        fuel_amount = _fuel_withdrawn_raw(entry, receipt)
        if fuel_amount is not None:
            return {
                "status": "withdrawn", "action_id": action_id, "tx_hash": tx_hash,
                "received_token_id": "eth", "received_raw": str(fuel_amount),
            }
        return {
            "status": "manual_review", "action_id": action_id, "tx_hash": tx_hash,
            "reason": "output_below_minimum_or_unverified",
        }
    stargate_amount = _stargate_withdrawn_raw(entry, receipt)
    if stargate_amount is not None:
        return {
            "status": "withdrawn", "action_id": action_id, "tx_hash": tx_hash,
            "received_token_id": _STARGATE_BSC_TOKEN, "received_raw": str(stargate_amount),
        }
    if not str(action.get("func", "")).startswith("removeLiquidity("):
        reason = "output_not_verifiable_from_action"
    else:
        params = action["str_params"]
        minimums = {params[0].lower(): int(params[3]), params[1].lower(): int(params[4])}
        if len(minimums) != 2:
            reason = "output_not_verifiable_from_action"
        else:
            received = dict.fromkeys(minimums, 0)
            logs = receipt.get("logs")
            if isinstance(logs, list):
                recipient = "0x" + entry["wallet"][2:].lower().rjust(64, "0")
                for log in logs:
                    if not isinstance(log, dict):
                        continue
                    topics = log.get("topics")
                    token = str(log.get("address", "")).lower()
                    if (
                        token in received
                        and isinstance(topics, list)
                        and len(topics) >= 3
                        and str(topics[0]).lower() == _TRANSFER_TOPIC
                        and str(topics[2]).lower() == recipient
                    ):
                        try:
                            received[token] += uint256(log.get("data"))
                        except (TypeError, ValueError, RpcError):
                            continue
            reason = (
                None
                if all(received[token] >= minimums[token] for token in minimums)
                else "output_below_minimum_or_unverified"
            )
    result = {"action_id": action_id, "tx_hash": tx_hash}
    if reason is None:
        result["status"] = "withdrawn"
    else:
        result.update(status="manual_review", reason=reason)
    return result


def _submit_durable(
    request: TransactionRequest,
    *,
    wallet_row: WalletWorkbookRow,
    rpc: Rpc,
    rpc_url: str,
    journal: Journal,
    position_id: int,
    step_key: str,
) -> tuple[str, TransactionRequest]:
    recovered = recover_durable_broadcast(
        rpc, url=rpc_url, journal=journal, position_id=position_id, step_key=step_key
    )
    if recovered is not None:
        wait_for_receipt(rpc, url=rpc_url, tx_hash=recovered)
        return recovered, request
    if journal.latest_step(position_id=position_id, step_key=step_key) is not None:
        raise ValueError("unresolved prior DeFi intent; inspect journal before retrying")
    request = FeePlanner(rpc).plan(
        rpc_url, request, sender=wallet_row.public_address
    )
    balance = quantity(rpc.call(rpc_url, "eth_getBalance", [wallet_row.public_address, "latest"]))
    require_native_reserve(balance=balance, gas_cost=request.max_total_fee_wei)
    require_ethereum_gas_below_limit(rpc, url=rpc_url, request=request)
    nonce = pending_nonce(rpc, url=rpc_url, wallet=wallet_row.public_address)
    raw = sign_transaction(
        request,
        private_key=wallet_row.private_key,
        expected_sender=wallet_row.public_address,
        nonce=nonce,
    )
    tx_hash = signed_transaction_hash(raw)
    observed = broadcast_durable_transaction(
        rpc,
        url=rpc_url,
        journal=journal,
        position_id=position_id,
        step_key=step_key,
        nonce=nonce,
        calldata=request.data,
        signed_transaction=raw,
        tx_hash=tx_hash,
    )
    if observed is None:
        raise AmbiguousBroadcast(tx_hash)
    wait_for_receipt(rpc, url=rpc_url, tx_hash=observed)
    return observed, request
