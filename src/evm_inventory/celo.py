"""CELO's native/ERC20 duality and its exact LI.FI token representation."""

CELO_CHAIN_ID = 42220
CELO_TOKEN_ADDRESS = "0x471ece3750da237f93b8e339c536989b8978a438"
ZERO_ADDRESS = "0x" + "0" * 40


def lifi_token_address(chain_id: int, address: str) -> str:
    address = address.lower()
    if chain_id == CELO_CHAIN_ID and address in ("native", ZERO_ADDRESS):
        return CELO_TOKEN_ADDRESS
    return ZERO_ADDRESS if address == "native" else address


def normalize_celo_balances(rows):
    """Collapse verified views of the same funds, rejecting inconsistent observations."""
    result = []
    seen = {}
    for row in rows:
        if not (
            int(row["chain_id"]) == CELO_CHAIN_ID
            and int(row["decimals"]) == 18
            and row["asset_id"].lower() in ("native", CELO_TOKEN_ADDRESS)
            and row.get("status") == "success"
        ):
            result.append(row)
            continue
        key = row["wallet"].lower()
        if key not in seen:
            canonical = {
                **row,
                "asset_id": CELO_TOKEN_ADDRESS,
                "balance_aliases": [row["asset_id"].lower()],
            }
            seen[key] = canonical
            result.append(canonical)
        else:
            canonical = seen[key]
            canonical["balance_aliases"].append(row["asset_id"].lower())
            if int(row["raw_balance"]) != int(canonical["raw_balance"]):
                canonical["balance_conflict"] = True
                canonical.setdefault("conflicting_amounts", [canonical["raw_balance"]]).append(
                    row["raw_balance"]
                )
    return result
