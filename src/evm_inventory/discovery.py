"""Optional Alchemy ERC-20 discovery; balances are verified separately via RPC."""

import os
import re
import time
from datetime import UTC, datetime
from urllib.parse import quote

from .transport import RequestError

SOURCE = "https://www.alchemy.com/docs/data/portfolio-apis/portfolio-api-endpoints/portfolio-api-endpoints/get-token-balances-by-address"
ADDRESS = re.compile(r"0x[0-9a-fA-F]{40}")


class Discovery:
    def __init__(self, transport, key=None):
        self.transport = transport
        self.key = os.environ.get("ALCHEMY_API_KEY", "") if key is None else key
        self.enabled = bool(self.key)
        self.stopped = None
        self.paused = None

    def page(self, wallet, network, cursor=None):
        if not self.enabled:
            raise RequestError("discovery_disabled")
        if self.stopped:
            raise RequestError(self.stopped)
        if self.paused is not None and self.paused.retry_after > time.time():
            raise self.paused
        payload = {
            "addresses": [{"address": wallet, "networks": [network]}],
            "includeNativeTokens": True,
            "includeErc20Tokens": True,
            "includeBlockMetadata": False,
        }
        if cursor:
            payload["pageKey"] = cursor
        url = (
            "https://api.g.alchemy.com/data/v1/"
            + quote(self.key, safe="")
            + "/assets/tokens/balances/by-address"
        )
        try:
            data = self.transport.post(url, payload)
        except RequestError as exc:
            if exc.code == "provider_access_denied":
                self.stopped = exc.code
            elif exc.code in {"rate_limited", "network_error", "provider_unavailable"}:
                self.paused = RequestError(exc.code, exc.retry_after or time.time() + 300)
                raise self.paused from None
            raise
        # Network errors are independent of HTTP status and of per-token metadata errors.
        if data.get("error"):
            raise RequestError("discovery_partial")
        if not isinstance(data.get("data"), dict) or not isinstance(
            data["data"].get("tokens"), list
        ):
            raise RequestError("discovery_invalid_response")
        result = {}
        for token in data["data"]["tokens"]:
            if not isinstance(token, dict):
                raise RequestError("discovery_invalid_token")
            contract = token.get("tokenAddress")
            if contract is None:
                contract = "native"
            if contract != "native" and (
                not isinstance(contract, str) or not ADDRESS.fullmatch(contract)
            ):
                raise RequestError("discovery_invalid_contract")
            token_address = token.get("address")
            if not isinstance(token_address, str):
                raise RequestError("discovery_invalid_address")
            if token_address.lower() != wallet.lower() or token.get("network") != network:
                raise RequestError("discovery_wrong_scope")
            raw = token.get("tokenBalance")
            if not isinstance(raw, str) or not re.fullmatch(r"0x[0-9a-fA-F]{1,64}", raw):
                raise RequestError("discovery_invalid_balance")
            if int(raw, 16) == 0:
                continue
            address = contract.lower() if contract != "native" else "native"
            result[address] = {
                "address": address,
                "symbol": token.get("symbol") or address,
                "decimals": None,
                "source": SOURCE,
                "checked_at": datetime.now(UTC).date().isoformat(),
                "variant": "discovered ERC-20",
                "reported_raw_balance": str(int(raw, 16)),
                "reported_at": datetime.now(UTC).isoformat(),
                "reported_source": "alchemy",
            }
        next_cursor = data["data"].get("pageKey", data.get("pageKey"))
        if next_cursor is not None and (not isinstance(next_cursor, str) or not next_cursor):
            raise RequestError("discovery_invalid_cursor")
        return list(result.values()), next_cursor
