"""Networks explicitly excluded by the operator from all operational processing."""

EXCLUDED_NETWORKS = {
    183: "Ethernity",
    185: "Mint Mainnet",
    624: "Binary Mainnet",
    690: "Redstone",
    6805: "RACE Mainnet",
    8008: "Polynomial",
    65536: "Automata Mainnet",
    380929: "Silent Data Mainnet",
    2702128: "Xterio Chain (ETH)",
}

_CHAIN_FIELDS = frozenset({
    "chain_id", "chainId", "source_chain_id", "destination_chain_id",
    "from_chain_id", "to_chain_id", "fromChainId", "toChainId",
})


def uses_excluded_network(value: object) -> bool:
    """Recognize excluded source, destination, and intermediate networks in saved plans."""
    if isinstance(value, dict):
        for key, item in value.items():
            if key in _CHAIN_FIELDS and str(item).isdigit() and int(item) in EXCLUDED_NETWORKS:
                return True
            if uses_excluded_network(item):
                return True
    elif isinstance(value, (list, tuple)):
        return any(uses_excluded_network(item) for item in value)
    return False
