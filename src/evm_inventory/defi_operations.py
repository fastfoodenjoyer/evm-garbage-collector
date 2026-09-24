"""DeFi planning and execution shared by the CLI and the batch worker."""

from __future__ import annotations

import hashlib
import json
from contextlib import ExitStack
from pathlib import Path

import httpx

from .config import load_catalog, load_wallets
from .defi_execution import execute_defi_action
from .defi_plan import create_defi_plan
from .executor import ExecutionRpc
from .models import ConfigError
from .rabby import RabbyClient
from .store import Store
from .transport import Transport
from .workbook import load_wallet_workbook


class _WalletRabbyPortfolio:
    def __init__(self, proxies: dict[str, str], stack: ExitStack):
        self.proxies = proxies
        self.stack = stack
        self.clients: dict[str, RabbyClient] = {}

    def _client(self, wallet: str) -> RabbyClient:
        if wallet not in self.clients:
            http_client = self.stack.enter_context(
                httpx.Client(timeout=30, proxy=self.proxies[wallet], trust_env=False)
            )
            self.clients[wallet] = RabbyClient(http_client)
        return self.clients[wallet]

    def chain_ids(self) -> dict[str, int]:
        return self._client(next(iter(self.proxies))).chain_ids()

    def positions(self, wallet: str) -> list[dict]:
        return self._client(wallet).positions(wallet)


def quote_defi(
    *, wallets_path: Path, workbook_path: Path, db_path: Path,
    output_path: Path, catalog_path: Path | None = None,
) -> dict:
    wallets = load_wallets(wallets_path)
    rows = load_wallet_workbook(workbook_path, require_deposit_address=False)
    wallet_map = {row.public_address: row for row in rows}
    proxies: dict[str, str] = {}
    for wallet in wallets:
        row = wallet_map.get(wallet)
        if row is None:
            raise ConfigError("DeFi wallet is not in workbook")
        if not row.rabby_proxy:
            raise ConfigError(f"row {row.row_number}: Rabby proxy is required")
        proxies[wallet] = row.rabby_proxy
    catalog = load_catalog(catalog_path)
    with ExitStack() as stack:
        plan = create_defi_plan(
            wallets, _WalletRabbyPortfolio(proxies, stack),
            supported_chain_ids={network.chain_id for network in catalog.networks},
        )
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(plan, indent=2) + "\n", encoding="utf-8")
    plan_bytes = output_path.read_bytes()
    plan_sha256 = hashlib.sha256(plan_bytes).hexdigest()
    with Store(db_path) as store:
        store.save_defi_plan(plan_sha256, plan_bytes)
    return {
        "status": "planned", "output": str(output_path),
        "plan_sha256": plan_sha256, **plan["summary"],
    }


def execute_defi(
    *, plan_path: Path, plan_sha256: str, action_id: str,
    workbook_path: Path, db_path: Path, max_gas_wei: int,
    execute: bool, rpc_urls: dict[int, str],
) -> dict:
    plan_bytes = plan_path.read_bytes()
    if hashlib.sha256(plan_bytes).hexdigest() != plan_sha256:
        raise ValueError("reviewed plan digest does not match file")
    with Store(db_path, readonly=True) as store:
        if store.defi_plan(plan_sha256) != plan_bytes:
            raise ValueError("reviewed DeFi plan is not stored in the inventory database")
    plan = json.loads(plan_bytes)
    entries = [
        entry for entry in plan.get("entries", [])
        if isinstance(entry, dict) and entry.get("action_id") == action_id
    ]
    if len(entries) != 1:
        raise ValueError("DeFi action ID does not select one position")
    wallet_rows = load_wallet_workbook(workbook_path, require_deposit_address=False)
    wallet_map = {row.public_address: row for row in wallet_rows}
    wallet = str(entries[0].get("wallet", ""))
    if wallet not in wallet_map:
        raise ValueError("selected DeFi wallet is not in workbook")
    if not wallet_map[wallet].rabby_proxy:
        raise ConfigError(f"row {wallet_map[wallet].row_number}: Rabby proxy is required")
    chain_id = entries[0].get("chain_id")
    if chain_id not in rpc_urls:
        raise ValueError("selected DeFi chain is not configured")
    transport = Transport(interval=0.2)
    try:
        with httpx.Client(
            timeout=30, proxy=wallet_map[wallet].rabby_proxy, trust_env=False
        ) as http_client:
            result = execute_defi_action(
                plan_bytes, plan_sha256=plan_sha256,
                action_id=action_id, wallet_row=wallet_map[wallet],
                rabby=RabbyClient(http_client), rpc=ExecutionRpc(transport),
                rpc_url=rpc_urls[chain_id], journal_path=db_path,
                max_gas_wei=max_gas_wei, execute=execute,
            )
    finally:
        transport.close()
    with Store(db_path) as store:
        store.record_defi_execution(plan_sha256, action_id, result)
    return result
