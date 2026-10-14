"""LI.FI connections isolated by wallet and its workbook proxy."""

from __future__ import annotations

import os
from collections.abc import Iterable
from pathlib import Path
from threading import Lock

import httpx

from .lifi import LifiClient
from .lifi_http import LifiHttpClient
from .models import ConfigError
from .proxy_pool import ProxyPoolHttpClient
from .relay import BASE_URL as RELAY_BASE_URL
from .relay import RelayClient, RelayQuotaStore
from .workbook import WalletWorkbookRow, _rabby_proxy


class WalletLifiClients:
    def __init__(
        self,
        rows: Iterable[WalletWorkbookRow],
        *,
        db_path: Path,
        purpose: str = "planning",
        policy_digest: str = "",
        quota_wait_seconds: float = 0,
    ):
        self.purpose = purpose
        self.policy_digest = policy_digest
        self.db_path = db_path
        self.quota_wait_seconds = quota_wait_seconds
        self._proxies: dict[str, str] = {}
        self._pools: dict[str, tuple[str, ...]] = {}
        for row in rows:
            if not row.rabby_proxy:
                raise ConfigError(f"row {row.row_number}: LI.FI wallet proxy is required")
            self._proxies[row.public_address.lower()] = _rabby_proxy(
                row.rabby_proxy, row_number=row.row_number, required=True
            )
            self._pools[row.public_address.lower()] = (
                getattr(row, "proxy_pool", ()) or (row.rabby_proxy,)
            )
        self._clients: dict[str, LifiClient] = {}
        self._relay_clients: dict[str, RelayClient] = {}
        self._lock = Lock()

    def for_wallet(self, wallet: str) -> LifiClient:
        wallet = wallet.lower()
        with self._lock:
            if wallet not in self._proxies:
                raise ConfigError("LI.FI wallet proxy is missing from workbook")
            if wallet not in self._clients:
                transports = [LifiHttpClient(
                    httpx.Client(timeout=30, proxy=proxy, trust_env=False),
                    db_path=self.db_path, proxy=proxy, purpose=self.purpose,
                    api_key=os.environ.get("LIFI_API_KEY", ""),
                    quota_wait_seconds=self.quota_wait_seconds,
                ) for proxy in self._pools[wallet]]
                transport = ProxyPoolHttpClient(transports, wallet=wallet)
                self._clients[wallet] = LifiClient(transport, policy_digest=self.policy_digest)
            return self._clients[wallet]

    def relay_for_wallet(self, wallet: str) -> RelayClient:
        wallet = wallet.lower()
        with self._lock:
            if wallet not in self._proxies:
                raise ConfigError("Relay wallet proxy is missing from workbook")
            if wallet not in self._relay_clients:
                self._relay_clients[wallet] = RelayClient(
                    LifiHttpClient(
                        httpx.Client(timeout=20, proxy=self._proxies[wallet], trust_env=False),
                        db_path=self.db_path,
                        proxy=self._proxies[wallet],
                        purpose=self.purpose,
                        base_url=RELAY_BASE_URL,
                        provider="relay",
                        quota_store=RelayQuotaStore,
                        api_key=os.environ.get("RELAY_API_KEY", ""),
                        quota_wait_seconds=self.quota_wait_seconds,
                    ),
                    policy_digest=self.policy_digest,
                )
            return self._relay_clients[wallet]

    @property
    def relay_request_metrics(self) -> dict[str, dict]:
        with self._lock:
            return {
                wallet: {**client.http_client.metrics, "job_cache_hits": client.job_cache_hits}
                for wallet, client in self._relay_clients.items()
            }

    @property
    def request_metrics(self) -> dict[str, dict]:
        with self._lock:
            return {
                wallet: {**client.http_client.metrics, **client.quote_job_metrics}
                for wallet, client in self._clients.items()
            }

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
            for client in self._relay_clients.values():
                client.http_client.close()
