"""Explicit-only signing and broadcast primitives for approved route plans."""

from __future__ import annotations

import re
import time
from typing import Protocol

from eth_account import Account

from .lifi import TransactionRequest
from .rpc import RpcError, quantity, uint256
from .transport import Transport

_ADDRESS_RE = re.compile(r"^0x[0-9a-fA-F]{40}$")
ETHEREUM_GAS_LIMIT_WEI = 500_000_000


class Broadcaster(Protocol):
    def call(self, url: str, method: str, params: list[object]) -> object: ...


class EthereumGasDeferred(ValueError):
    """Fail closed before an Ethereum signature when current gas is too expensive."""

    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason
        self.approval_tx_hash: str | None = None


class ExecutionRpc:
    """Small RPC client whose write capability is used only by the execute command."""

    _METHODS = {
        "eth_call",
        "eth_chainId",
        "eth_estimateGas",
        "eth_gasPrice",
        "eth_getBalance",
        "eth_getTransactionCount",
        "eth_getTransactionReceipt",
        "eth_sendRawTransaction",
    }

    def __init__(self, transport: Transport):
        self.transport = transport
        self._next_id = 1

    def call(self, url: str, method: str, params: list[object]) -> object:
        if method not in self._METHODS:
            raise RpcError("execution_method_not_allowed")
        request_id = self._next_id
        self._next_id += 1
        response = self.transport.post(
            url,
            {"jsonrpc": "2.0", "id": request_id, "method": method, "params": params},
        )
        if response.get("id") != request_id or response.get("jsonrpc") != "2.0":
            raise RpcError("invalid_rpc_envelope")
        if response.get("error") or "result" not in response:
            raise RpcError("rpc_error")
        return response["result"]


def require_native_reserve(*, balance: int, gas_cost: int, multiplier: int = 5) -> int:
    """Return retained native balance, or refuse a transaction that breaks the reserve."""

    if balance < 0 or gas_cost < 0 or multiplier < 1:
        raise ValueError("invalid gas reserve inputs")
    retained = gas_cost * multiplier
    if balance < retained:
        raise ValueError("insufficient native balance for gas reserve")
    return retained


def require_ethereum_gas_below_limit(
    broadcaster: Broadcaster, *, url: str, request: TransactionRequest
) -> None:
    """Freshly verify Ethereum endpoint and gas immediately before signing.

    Other chains retain their existing execution policy.  This deliberately does
    not modify the request: a route is deferred rather than silently repriced.
    """

    if request.chain_id != 1:
        return
    try:
        endpoint_chain_id = quantity(broadcaster.call(url, "eth_chainId", []))
    except Exception as exc:
        raise EthereumGasDeferred(
            "ethereum_gas_deferred:source=chain_probe_error;threshold_wei=500000000"
        ) from exc
    if endpoint_chain_id != 1:
        raise EthereumGasDeferred(
            "ethereum_gas_deferred:source=chain_id;"
            f"value_wei={endpoint_chain_id};threshold_wei=500000000"
        )
    try:
        rpc_gas_price = quantity(broadcaster.call(url, "eth_gasPrice", []))
    except Exception as exc:
        raise EthereumGasDeferred(
            "ethereum_gas_deferred:source=rpc_probe_error;threshold_wei=500000000"
        ) from exc
    if rpc_gas_price >= ETHEREUM_GAS_LIMIT_WEI:
        raise EthereumGasDeferred(
            "ethereum_gas_deferred:source=rpc;"
            f"value_wei={rpc_gas_price};threshold_wei={ETHEREUM_GAS_LIMIT_WEI}"
        )
    if (
        isinstance(request.gas_price_wei, bool)
        or not isinstance(request.gas_price_wei, int)
        or request.gas_price_wei < 0
    ):
        raise EthereumGasDeferred(
            "ethereum_gas_deferred:source=plan_error;threshold_wei=500000000"
        )
    if request.gas_price_wei >= ETHEREUM_GAS_LIMIT_WEI:
        raise EthereumGasDeferred(
            "ethereum_gas_deferred:source=plan;"
            f"value_wei={request.gas_price_wei};threshold_wei={ETHEREUM_GAS_LIMIT_WEI}"
        )


def sign_transaction(
    request: TransactionRequest,
    *,
    private_key: str,
    expected_sender: str,
    nonce: int,
) -> str:
    """Sign exactly one preflighted transaction; this function never broadcasts it."""

    if not _ADDRESS_RE.fullmatch(expected_sender):
        raise ValueError("invalid expected sender")
    if nonce < 0:
        raise ValueError("invalid nonce")
    account = Account.from_key(private_key)
    if account.address.lower() != expected_sender.lower():
        raise ValueError("private key does not match expected sender")
    signed = account.sign_transaction(
        {
            "chainId": request.chain_id,
            "nonce": nonce,
            "to": request.to,
            "value": request.value,
            "data": request.data,
            "gas": request.gas_limit,
            "gasPrice": request.gas_price_wei,
        }
    )
    return "0x" + signed.raw_transaction.hex()


def broadcast_signed_transaction(
    broadcaster: Broadcaster, *, url: str, raw_transaction: str
) -> str:
    """Broadcast a signed payload through an RPC client dedicated to execution."""

    if not isinstance(raw_transaction, str) or not re.fullmatch(r"0x[0-9a-fA-F]+", raw_transaction):
        raise ValueError("invalid signed transaction")
    result = broadcaster.call(url, "eth_sendRawTransaction", [raw_transaction])
    if not isinstance(result, str) or not re.fullmatch(r"0x[0-9a-fA-F]{64}", result):
        raise ValueError("RPC did not return a transaction hash")
    return result.lower()


def pending_nonce(broadcaster: Broadcaster, *, url: str, wallet: str) -> int:
    if not _ADDRESS_RE.fullmatch(wallet):
        raise ValueError("invalid wallet")
    return quantity(broadcaster.call(url, "eth_getTransactionCount", [wallet, "pending"]))


def token_allowance(
    broadcaster: Broadcaster, *, url: str, token: str, owner: str, spender: str
) -> int:
    data = "0xdd62ed3e" + owner[2:].lower().rjust(64, "0") + spender[2:].lower().rjust(64, "0")
    result = broadcaster.call(url, "eth_call", [{"to": token, "data": data}, "latest"])
    return uint256(result)


def approve_transaction(
    *, chain_id: int, token: str, spender: str, amount: int, gas_price_wei: int
) -> TransactionRequest:
    if amount < 0 or not _ADDRESS_RE.fullmatch(token) or not _ADDRESS_RE.fullmatch(spender):
        raise ValueError("invalid approval")
    data = "0x095ea7b3" + spender[2:].lower().rjust(64, "0") + hex(amount)[2:].rjust(64, "0")
    return TransactionRequest(chain_id, token.lower(), data, 0, 100_000, gas_price_wei)


def wait_for_receipt(
    broadcaster: Broadcaster,
    *,
    url: str,
    tx_hash: str,
    timeout_seconds: int = 600,
    poll_seconds: int = 10,
) -> dict:
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        receipt = broadcaster.call(url, "eth_getTransactionReceipt", [tx_hash])
        if receipt is not None:
            if not isinstance(receipt, dict):
                raise ValueError("invalid transaction receipt")
            if quantity(receipt.get("status")) != 1:
                raise ValueError("transaction reverted")
            return receipt
        time.sleep(poll_seconds)
    raise TimeoutError("transaction receipt timeout")
