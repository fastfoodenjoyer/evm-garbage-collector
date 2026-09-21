"""Live Bitget deposit capability catalog used when preparing a route runbook."""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation

import httpx

_EVM_CHAINS = {
    "ETH": 1,
    "ETHEREUM": 1,
    "OPTIMISM": 10,
    "ARBITRUMONE": 42161,
    "ARBITRUM": 42161,
    "BASE": 8453,
    "CELO": 42220,
}
_COIN_DECIMALS = {"ETH": 18, "USDC": 6, "USDT": 6, "OP": 18, "ARB": 18, "CELO": 18}


@dataclass(frozen=True, slots=True)
class BitgetDepositTarget:
    coin: str
    chain_id: int
    asset_id: str
    minimum_raw: int
    chain: str = ""


def fetch_public_coins(http_client: httpx.Client | None = None) -> tuple[dict, ...]:
    client = http_client or httpx.Client(timeout=30)
    response = client.get("https://api.bitget.com/api/v2/spot/public/coins")
    response.raise_for_status()
    body = response.json()
    if body.get("code") != "00000" or not isinstance(body.get("data"), list):
        raise ValueError("Bitget public coin catalog is unavailable")
    return tuple(item for item in body["data"] if isinstance(item, dict))


def deposit_targets(coins: Iterable[dict]) -> tuple[BitgetDepositTarget, ...]:
    """Convert Bitget's public catalog into exact EVM asset/minimum pairs."""

    targets: list[BitgetDepositTarget] = []
    for coin_data in coins:
        coin = str(coin_data.get("coin", "")).upper()
        decimals = _COIN_DECIMALS.get(coin)
        chains = coin_data.get("chains")
        if decimals is None or not isinstance(chains, list):
            continue
        for chain in chains:
            if not isinstance(chain, dict) or str(chain.get("rechargeable")).lower() != "true":
                continue
            chain_id = _EVM_CHAINS.get(str(chain.get("chain", "")).upper())
            if chain_id is None:
                continue
            contract = chain.get("contractAddress")
            asset_id = "native" if coin == "ETH" else str(contract or "").lower()
            if asset_id != "native" and not _valid_address(asset_id):
                continue
            try:
                minimum_raw = int(Decimal(str(chain["minDepositAmount"])) * 10**decimals)
            except (InvalidOperation, KeyError, ValueError):
                continue
            if minimum_raw > 0:
                targets.append(
                    BitgetDepositTarget(coin, chain_id, asset_id, minimum_raw, str(chain["chain"]))
                )
    return tuple(targets)


def _valid_address(value: str) -> bool:
    if len(value) != 42 or not value.startswith("0x"):
        return False
    try:
        int(value[2:], 16)
    except ValueError:
        return False
    return True
