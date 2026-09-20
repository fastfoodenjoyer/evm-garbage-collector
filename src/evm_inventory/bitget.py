"""Signed read-only Bitget deposit-record queries."""

from __future__ import annotations

import base64
import hashlib
import hmac
import time
from urllib.parse import urlencode

import httpx


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

    def deposit_status(self, *, tx_hash: str, start_ms: int, end_ms: int, coin: str) -> str | None:
        for record in self.deposit_records(start_ms=start_ms, end_ms=end_ms, coin=coin):
            if str(record.get("tradeId", "")).lower() == tx_hash.lower():
                return str(record.get("status"))
        return None
