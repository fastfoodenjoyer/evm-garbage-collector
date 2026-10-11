"""Live Bitget deposit capability catalog used when preparing a route runbook."""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation

import httpx

from .stages import http_request

_EVM_CHAINS = {
    "ETH": 1,
    "ETHEREUM": 1,
    "ERC20": 1,
    "OPTIMISM": 10,
    "ARBITRUMONE": 42161,
    "ARBITRUM": 42161,
    "BASE": 8453,
    "CELO": 42220,
    "BEP20": 56,
    "POLYGON": 137,
    "ZKSYNCERA": 324,
    "METISTOKEN": 1088,
    "COREDAO": 1116,
    "AVAXC-CHAIN": 43114,
    "BLAST": 81457,
    "SCROLL": 534352,
    "MORPH": 2818,
}
_COIN_DECIMALS = {
    "ETH": 18, "USDC": 6, "USDT": 6, "OP": 18, "ARB": 18, "CELO": 18,
    "BNB": 18, "POL": 18, "METIS": 18, "CORE": 18, "AVAX": 18,
}
_NATIVE_COINS = {
    1: "ETH", 10: "ETH", 56: "BNB", 137: "POL", 324: "ETH", 1088: "METIS",
    1116: "CORE", 2818: "ETH", 8453: "ETH", 42161: "ETH", 42220: "CELO",
    43114: "AVAX", 81457: "ETH", 534352: "ETH",
}


def deposit_decimals(coin: str, chain: str) -> int | None:
    """Known network-specific units, shared by catalog and credit matching."""
    if coin.upper() in {"USDC", "USDT"} and _EVM_CHAINS.get(chain.upper()) == 56:
        return 18
    return _COIN_DECIMALS.get(coin.upper())


@dataclass(frozen=True, slots=True)
class BitgetDepositTarget:
    coin: str
    chain_id: int
    asset_id: str
    minimum_raw: int
    chain: str = ""
    decimals: int | None = None

    def __post_init__(self) -> None:
        decimals = self.decimals
        if decimals is None:
            decimals = 18 if self.asset_id == "native" else _COIN_DECIMALS.get(self.coin.upper(), 6)
            object.__setattr__(self, "decimals", decimals)
        if isinstance(decimals, bool) or not isinstance(decimals, int) or not 0 <= decimals <= 255:
            raise ValueError("Bitget target decimals must be an integer from 0 through 255")


def fetch_public_coins(http_client: httpx.Client | None = None) -> tuple[dict, ...]:
    client = http_client or httpx.Client(timeout=30)
    try:
        response = http_request(client, 'get', "https://api.bitget.com/api/v2/spot/public/coins")
        response.raise_for_status()
        body = response.json()
    finally:
        if http_client is None:
            client.close()
    if body.get("code") != "00000" or not isinstance(body.get("data"), list):
        raise ValueError("Bitget public coin catalog is unavailable")
    return tuple(item for item in body["data"] if isinstance(item, dict))


def deposit_targets(coins: Iterable[dict]) -> tuple[BitgetDepositTarget, ...]:
    """Convert Bitget's public catalog into exact EVM asset/minimum pairs."""

    targets: list[BitgetDepositTarget] = []
    for coin_data in coins:
        coin = str(coin_data.get("coin", "")).upper()
        chains = coin_data.get("chains")
        if coin not in _COIN_DECIMALS or not isinstance(chains, list):
            continue
        for chain in chains:
            if not isinstance(chain, dict) or str(chain.get("rechargeable")).lower() != "true":
                continue
            if str(chain.get("needTag", "false")).lower() != "false":
                continue
            chain_id = _EVM_CHAINS.get(str(chain.get("chain", "")).upper())
            if chain_id is None:
                continue
            contract = chain.get("contractAddress")
            asset_id = str(contract or "").lower()
            if not contract and _NATIVE_COINS.get(chain_id) == coin:
                asset_id = "native"
            if asset_id != "native" and not _valid_address(asset_id):
                continue
            decimals = deposit_decimals(coin, str(chain["chain"]))
            try:
                minimum = Decimal(str(chain["minDepositAmount"])) * 10**decimals
                if not minimum.is_finite() or minimum != minimum.to_integral_value():
                    continue
                minimum_raw = int(minimum)
            except (InvalidOperation, KeyError, ValueError):
                continue
            if minimum_raw > 0:
                targets.append(
                    BitgetDepositTarget(
                        coin,
                        chain_id,
                        asset_id,
                        minimum_raw,
                        str(chain["chain"]),
                        decimals,
                    )
                )
    return tuple(targets)


def validate_live_target(
    planned: BitgetDepositTarget,
    *,
    live_targets: Iterable[BitgetDepositTarget],
    user_address: str,
) -> BitgetDepositTarget:
    """Re-admit a final deposit only against current enabled exchange metadata."""

    if not _valid_address(user_address):
        raise ValueError("invalid Bitget deposit address")
    for target in live_targets:
        if (
            target.coin == planned.coin
            and target.chain_id == planned.chain_id
            and target.asset_id == planned.asset_id
            and target.chain == planned.chain
            and target.decimals == planned.decimals
        ):
            return target
    raise ValueError("Bitget deposit target is no longer enabled")


def _valid_address(value: str) -> bool:
    if len(value) != 42 or not value.startswith("0x"):
        return False
    try:
        int(value[2:], 16)
    except ValueError:
        return False
    return True
