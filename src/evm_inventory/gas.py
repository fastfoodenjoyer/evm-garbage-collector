"""Read-only EVM gas probes used by the route planner."""

from __future__ import annotations

import re
from dataclasses import dataclass

from .rpc import RpcReader, quantity

_ADDRESS_RE = re.compile(r"^0x[0-9a-fA-F]{40}$")


@dataclass(frozen=True, slots=True)
class GasQuote:
    gas_limit: int
    gas_price_wei: int

    @property
    def cost_wei(self) -> int:
        return self.gas_limit * self.gas_price_wei


def native_transfer_gas_quote(
    rpc: RpcReader,
    *,
    url: str,
    chain_id: int,
    sender: str,
    recipient: str,
    value_wei: int,
) -> GasQuote:
    """Estimate a native transfer with RPC calls only; no transaction is signed."""

    if not _ADDRESS_RE.fullmatch(sender) or not _ADDRESS_RE.fullmatch(recipient):
        raise ValueError("sender and recipient must be EVM addresses")
    if value_wei < 0:
        raise ValueError("transfer value must be nonnegative")
    rpc.check_chain(url, chain_id)
    gas_limit = quantity(
        rpc.call(
            url,
            "eth_estimateGas",
            [{"from": sender, "to": recipient, "value": hex(value_wei)}],
        )
    )
    gas_price_wei = quantity(rpc.call(url, "eth_gasPrice", []))
    return GasQuote(gas_limit=gas_limit, gas_price_wei=gas_price_wei)
