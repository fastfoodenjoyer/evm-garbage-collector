"""Signed read-only Bitget deposit-record queries."""

from __future__ import annotations

import base64
import hashlib
import hmac
import time
from decimal import Decimal, InvalidOperation
from urllib.parse import urlencode

import httpx

from .bitget_catalog import (
    BitgetDepositTarget,
    deposit_targets,
    fetch_public_coins,
    validate_live_target,
)

_COIN_DECIMALS = {"ETH": 18, "USDC": 6, "USDT": 6, "OP": 18, "ARB": 18, "CELO": 18}


def _is_credited_status(status: str | None) -> bool:
    return status is not None and status.lower() == "success"


class BitgetClient:
    endpoint = "https://api.bitget.com"

    def __init__(self, *, api_key: str, secret_key: str, passphrase: str, http=None):
        self.api_key, self.secret_key, self.passphrase = api_key, secret_key, passphrase
        self.http = http or httpx.Client(timeout=20)

    def deposit_records(
        self, *, start_ms: int, end_ms: int, coin: str | None = None
    ) -> tuple[dict, ...]:
        params = {"startTime": str(start_ms), "endTime": str(end_ms), "limit": "100"}
        if coin:
            params["coin"] = coin
        path = "/api/v2/spot/wallet/deposit-records?" + urlencode(params)
        timestamp = str(int(time.time() * 1000))
        signature = base64.b64encode(
            hmac.new(
                self.secret_key.encode(), f"{timestamp}GET{path}".encode(), hashlib.sha256
            ).digest()
        ).decode()
        response = self.http.get(self.endpoint + path, headers={
            "ACCESS-KEY": self.api_key, "ACCESS-SIGN": signature,
            "ACCESS-TIMESTAMP": timestamp, "ACCESS-PASSPHRASE": self.passphrase,
            "locale": "en-US", "Content-Type": "application/json",
        })
        response.raise_for_status()
        body = response.json()
        if body.get("code") != "00000" or not isinstance(body.get("data"), list):
            raise ValueError("Bitget deposit record request failed")
        return tuple(item for item in body["data"] if isinstance(item, dict))

    def deposit_address(self, *, coin: str, chain: str) -> str:
        """Read the current authenticated Bitget deposit address for an exact target."""

        path = "/api/v2/spot/wallet/deposit-address?" + urlencode({"coin": coin, "chain": chain})
        timestamp = str(int(time.time() * 1000))
        signature = base64.b64encode(
            hmac.new(
                self.secret_key.encode(), f"{timestamp}GET{path}".encode(), hashlib.sha256
            ).digest()
        ).decode()
        response = self.http.get(self.endpoint + path, headers={
            "ACCESS-KEY": self.api_key, "ACCESS-SIGN": signature,
            "ACCESS-TIMESTAMP": timestamp, "ACCESS-PASSPHRASE": self.passphrase,
            "locale": "en-US", "Content-Type": "application/json",
        })
        response.raise_for_status()
        body = response.json()
        data = body.get("data")
        address = data.get("address") if isinstance(data, dict) else None
        if body.get("code") != "00000" or not isinstance(address, str):
            raise ValueError("Bitget deposit address request failed")
        return address

    def revalidate_deposit_target(self, target: dict, *, recipient: str) -> BitgetDepositTarget:
        """Reload enabled metadata and address immediately before a final deposit."""

        try:
            planned = BitgetDepositTarget(
                str(target["coin"]), int(target["chain_id"]), str(target["asset_id"]).lower(),
                int(target["minimum_raw"]), str(target["chain"]),
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise ValueError("invalid planned Bitget target") from exc
        current_address = self.deposit_address(coin=planned.coin, chain=planned.chain)
        live = validate_live_target(
            planned,
            live_targets=deposit_targets(fetch_public_coins(self.http)),
            user_address=current_address,
        )
        if current_address.lower() != recipient.lower():
            raise ValueError("Bitget deposit address changed since planning")
        return live

    def deposit_status(
        self,
        *,
        tx_hash: str,
        start_ms: int,
        end_ms: int,
        coin: str,
        chain: str,
        recipient: str,
        minimum_raw: int,
    ) -> str | None:
        for record in self.deposit_records(start_ms=start_ms, end_ms=end_ms, coin=coin):
            hashes = (record.get("tradeId"), record.get("txId"), record.get("txHash"))
            if (
                any(str(value or "").lower() == tx_hash.lower() for value in hashes)
                and record.get("coin") == coin
                and record.get("chain") == chain
                and str(record.get("address", "")).lower() == recipient.lower()
                and _record_raw_amount(record, coin) >= minimum_raw
            ):
                return str(record.get("status", "")) or None
        return None

    def wait_for_deposit(
        self,
        *,
        tx_hash: str,
        started_ms: int,
        coin: str,
        chain: str,
        recipient: str,
        minimum_raw: int,
        timeout_seconds: int = 21_600,
        poll_seconds: int = 60,
    ) -> str | None:
        """Poll Bitget after a submitted deposit; returns the exchange status or None."""

        deadline = time.monotonic() + timeout_seconds
        while time.monotonic() < deadline:
            status = self.deposit_status(
                tx_hash=tx_hash,
                start_ms=started_ms,
                end_ms=int(time.time() * 1000),
                coin=coin,
                chain=chain,
                recipient=recipient,
                minimum_raw=minimum_raw,
            )
            if _is_credited_status(status):
                return status
            time.sleep(poll_seconds)
        return None


def _record_raw_amount(record: dict, coin: str) -> int:
    decimals = _COIN_DECIMALS.get(coin)
    if decimals is None:
        return -1
    try:
        raw = Decimal(str(record["size"])) * (10**decimals)
    except (InvalidOperation, KeyError, ValueError):
        return -1
    if not raw.is_finite():
        return -1
    if raw != raw.to_integral_value():
        return -1
    return int(raw)
