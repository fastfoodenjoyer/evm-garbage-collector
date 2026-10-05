"""LI.FI connections isolated by wallet and its workbook proxy."""

from __future__ import annotations

from collections.abc import Iterable
from threading import Lock

import httpx

from .lifi import LifiClient
from .models import ConfigError
from .workbook import WalletWorkbookRow, _rabby_proxy


class WalletLifiClients:
    def __init__(self, rows: Iterable[WalletWorkbookRow]):
        self._proxies: dict[str, str] = {}
        for row in rows:
            if not row.rabby_proxy:
                raise ConfigError(f"row {row.row_number}: LI.FI wallet proxy is required")
            self._proxies[row.public_address.lower()] = _rabby_proxy(
                row.rabby_proxy, row_number=row.row_number, required=True
            )
        self._clients: dict[str, LifiClient] = {}
        self._lock = Lock()

    def for_wallet(self, wallet: str) -> LifiClient:
        wallet = wallet.lower()
        with self._lock:
            if wallet not in self._proxies:
                raise ConfigError("LI.FI wallet proxy is missing from workbook")
            if wallet not in self._clients:
                self._clients[wallet] = LifiClient(httpx.Client(
                    timeout=30, proxy=self._proxies[wallet], trust_env=False
                ))
            return self._clients[wallet]

    @property
    def route_diagnostics(self) -> list[dict]:
        with self._lock:
            return [
                {"wallet": wallet, **item}
                for wallet, client in self._clients.items()
                for item in list(client.route_diagnostics)
            ]

    def close(self) -> None:
        with self._lock:
            for client in self._clients.values():
                client.http_client.close()

