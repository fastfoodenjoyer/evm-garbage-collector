"""Read-only Rabby portfolio API and strict encoding of supported exit actions."""

from __future__ import annotations

import json
import re
import sys
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from typing import Any

import httpx
from eth_abi import encode
from eth_utils import function_signature_to_4byte_selector

from .diagnostics import redact_data, redact_text

_ADDRESS = re.compile(r"^0x[0-9a-fA-F]{40}$")
_FUNCTION = re.compile(r"^(?:function )?([A-Za-z][A-Za-z0-9_]*)\(([^()]*)\)(?:\([^()]*\))?$")
FUEL_PREDEPOSITS = "0x19b5cc75846bf6286d599ec116536a333c4c2c14"
ZERO_ADDRESS = "0x" + "0" * 40
FUEL_NATIVE_WITHDRAW = "withdraw(address,address,uint240)"
_SUPPORTED = {
    "withdraw()": ((), ()),
    "withdraw(uint256)": ((), (0,)),
    "withdraw(uint256,address)": ((1,), (0,)),
    "withdraw(uint256,address,address)": ((1, 2), (0,)),
    "redeem(uint256)": ((), (0,)),
    "redeem(uint256,address,address)": ((1, 2), (0,)),
    "removeLiquidity(address,address,uint256,uint256,uint256,address,uint256)": ((5,), (2, 3, 4)),
    FUEL_NATIVE_WITHDRAW: ((1,), (2,)),
}


@dataclass(frozen=True, slots=True)
class EncodedAction:
    to: str
    data: str
    value: int
    approval_token: str | None = None
    approval_spender: str | None = None
    approval_amount: int = 0


class RabbyClient:
    """Public-address reads from the API used by Rabby's published SDK."""

    def __init__(
        self, client: httpx.Client, *, base_url: str = "https://api.rabby.io",
        sleep=time.sleep,
    ):
        self.client = client
        self.base_url = base_url.rstrip("/")
        self.sleep = sleep

    @staticmethod
    def _retry_after_seconds(header: str | None) -> float:
        if not header:
            return 0
        try:
            return max(0, float(header))
        except ValueError:
            try:
                instant = parsedate_to_datetime(header)
                if instant.tzinfo is None:
                    instant = instant.replace(tzinfo=UTC)
                return max(0, (instant - datetime.now(UTC)).total_seconds())
            except (ValueError, TypeError):
                return 0

    def _get(self, path: str, *, params: dict[str, str] | None = None) -> httpx.Response:
        attempts: list[dict[str, Any]] = []
        for attempt in range(4):
            response = self.client.get(
                self.base_url + path, params=params, timeout=30,
            )
            if response.status_code != 429:
                response.raise_for_status()
                return response
            detail = {
                "attempt": attempt + 1, "endpoint": path,
                "http_status": response.status_code,
                "response_headers": redact_data(dict(response.headers)),
                "response_body": redact_text(response.text),
            }
            attempts.append(detail)
            wait_seconds = (
                max(5, self._retry_after_seconds(response.headers.get("Retry-After")))
                * 2**attempt
                if attempt < 3 else None
            )
            print(json.dumps({
                "event": "rabby_rate_limited", **detail,
                "retry_in_seconds": wait_seconds, "max_attempts": 4,
            }, ensure_ascii=False), file=sys.stderr, flush=True)
            if wait_seconds is None:
                try:
                    response.raise_for_status()
                except httpx.HTTPStatusError as exc:
                    exc.diagnostic = {"attempts": attempts}
                    raise
            self.sleep(wait_seconds)
        raise AssertionError("unreachable")

    def positions(self, wallet: str) -> list[dict[str, Any]]:
        if not _ADDRESS.fullmatch(wallet):
            raise ValueError("invalid wallet address")
        response = self._get("/v1/user/complex_protocol_list", params={"id": wallet})
        data = response.json()
        if not isinstance(data, list) or any(not isinstance(item, dict) for item in data):
            raise ValueError("Rabby returned an invalid protocol list")
        return data

    def chain_ids(self) -> dict[str, int]:
        response = self._get("/v1/chain/list")
        data = response.json()
        if not isinstance(data, list):
            raise ValueError("Rabby returned an invalid chain list")
        result: dict[str, int] = {}
        for row in data:
            if not isinstance(row, dict):
                raise ValueError("Rabby returned an invalid chain list")
            chain, chain_id = row.get("id"), row.get("community_id")
            if (
                isinstance(chain, str)
                and isinstance(chain_id, int)
                and not isinstance(chain_id, bool)
            ):
                if chain_id > 0:
                    result[chain] = chain_id
        if not result:
            raise ValueError("Rabby returned no usable chains")
        return result


def encode_action(action: object, *, wallet: str, now_seconds: int | None = None) -> EncodedAction:
    """Encode a narrow Rabby action set; arbitrary protocol calls fail closed."""

    if not isinstance(action, dict) or not _ADDRESS.fullmatch(wallet):
        raise ValueError("invalid Rabby action or wallet")
    if action.get("type") != "withdraw":
        raise ValueError("unsupported Rabby action type")
    target = action.get("contract_id")
    if not isinstance(target, str) or not _ADDRESS.fullmatch(target):
        raise ValueError("invalid Rabby action contract")
    function = action.get("func")
    match = _FUNCTION.fullmatch(function.strip()) if isinstance(function, str) else None
    if match is None:
        raise ValueError("invalid Rabby action method")
    types = tuple(match.group(2).split(",")) if match.group(2) else ()
    signature = f"{match.group(1)}({','.join(types)})"
    if signature not in _SUPPORTED:
        raise ValueError("unsupported Rabby action method")
    if signature == FUEL_NATIVE_WITHDRAW and target.lower() != FUEL_PREDEPOSITS:
        raise ValueError("unsupported Fuel withdrawal contract")
    recipient_indices, positive_indices = _SUPPORTED[signature]
    raw_params = action.get("str_params")
    if not isinstance(raw_params, list) or len(raw_params) != len(types):
        raise ValueError("Rabby action parameters do not match method")
    values: list[Any] = []
    for abi_type, raw in zip(types, raw_params, strict=True):
        if not isinstance(raw, str):
            raise ValueError("Rabby action parameter is not a string")
        if abi_type == "address":
            if not _ADDRESS.fullmatch(raw):
                raise ValueError("invalid Rabby address parameter")
            values.append(raw)
        elif abi_type in {"uint256", "uint240"}:
            bits = 240 if abi_type == "uint240" else 256
            if not raw.isdecimal() or not 0 <= int(raw) < 2**bits:
                raise ValueError(f"invalid Rabby {abi_type} parameter")
            values.append(int(raw))
        else:
            raise ValueError("unsupported Rabby ABI parameter")
    for index in recipient_indices:
        if values[index].lower() != wallet.lower():
            raise ValueError("Rabby withdrawal recipient differs from wallet")
    for index in positive_indices:
        if values[index] <= 0:
            raise ValueError("Rabby withdrawal amount or minimum must be positive")
    if signature == FUEL_NATIVE_WITHDRAW and values[0].lower() != ZERO_ADDRESS:
        raise ValueError("Fuel withdrawal must use native ETH")
    if match.group(1) == "removeLiquidity":
        if values[3] <= 0 or values[4] <= 0:
            raise ValueError("Rabby liquidity minimum must be positive")
        if values[6] <= (now_seconds if now_seconds is not None else int(time.time())):
            raise ValueError("Rabby liquidity deadline expired")
    approval = action.get("need_approve") or {}
    if signature == FUEL_NATIVE_WITHDRAW and approval:
        raise ValueError("Fuel native withdrawal must not require approval")
    if not isinstance(approval, dict):
        raise ValueError("invalid Rabby approval")
    token = approval.get("token_id")
    spender = approval.get("to")
    raw_amount = approval.get("str_raw_amount")
    if any(value is not None for value in (token, spender, raw_amount)):
        if (
            not isinstance(token, str)
            or not _ADDRESS.fullmatch(token)
            or not isinstance(spender, str)
            or not _ADDRESS.fullmatch(spender)
            or spender.lower() != target.lower()
            or not isinstance(raw_amount, str)
            or not raw_amount.isdecimal()
            or not 0 < int(raw_amount) < 2**256
        ):
            raise ValueError("Rabby approval spender, token or amount is unsafe")
        approval_amount = int(raw_amount)
    else:
        token = spender = None
        approval_amount = 0
    calldata = (
        "0x" + (function_signature_to_4byte_selector(signature) + encode(types, values)).hex()
    )
    return EncodedAction(
        to=target.lower(),
        data=calldata,
        value=0,
        approval_token=token.lower() if token else None,
        approval_spender=spender.lower() if spender else None,
        approval_amount=approval_amount,
    )
