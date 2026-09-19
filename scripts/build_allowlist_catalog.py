"""Create a narrow RPC catalog for the exact assets allowed by swap policy."""

import argparse
import json
from datetime import date
from pathlib import Path

RPC_FALLBACKS = {
    10: ["https://optimism.publicnode.com"],
    130: ["https://unichain.drpc.org", "https://unichain.publicnode.com"],
    8453: ["https://base-rpc.publicnode.com", "https://base.drpc.org"],
    57073: ["https://rpc-qnd.inkonchain.com", "https://rpc-ten.inkonchain.com"],
}


def build_catalog(base, allowlist, inventory, checked_at):
    if allowlist.get("default_action") != "deny":
        raise ValueError("allowlist default_action must be deny")
    networks = {network["chain_id"]: network for network in base["networks"]}
    balances = {
        (row["chain_id"], row["asset_id"]): row
        for row in inventory["balances"]
        if row.get("decimals") is not None
    }
    selected = {}
    for asset in allowlist.get("assets", []):
        chain_id, asset_id = asset.get("chain_id"), asset.get("asset_id")
        if not isinstance(chain_id, int) or chain_id not in networks:
            raise ValueError(f"allowlist chain is absent from base catalog: {chain_id}")
        if not isinstance(asset_id, str):
            raise ValueError("allowlist asset_id is required")
        selected.setdefault(chain_id, []).append(asset)

    result = []
    for chain_id, assets in sorted(selected.items()):
        base_network = networks[chain_id]
        tokens = []
        seen = set()
        for asset in assets:
            asset_id = asset["asset_id"]
            if asset_id == "native":
                continue
            if asset_id in seen:
                raise ValueError(f"duplicate allowlist asset {chain_id}:{asset_id}")
            seen.add(asset_id)
            balance = balances.get((chain_id, asset_id))
            if balance is None:
                raise ValueError(f"missing inventory metadata for {chain_id}:{asset_id}")
            tokens.append(
                {
                    "address": asset_id,
                    "symbol": asset["symbol"],
                    "decimals": balance["decimals"],
                    "source": "https://jumper.exchange/",
                    "checked_at": checked_at,
                    "variant": balance.get("name") or asset["symbol"],
                }
            )
        result.append(
            {
                "chain_id": chain_id,
                "name": base_network["name"],
                "rpc_urls": list(
                    dict.fromkeys(RPC_FALLBACKS.get(chain_id, []) + base_network["rpc_urls"])
                ),
                "native_symbol": base_network["native_symbol"],
                "native_decimals": base_network["native_decimals"],
                "tokens": tokens,
                "token_review_status": "verified",
                "notes": "Exact assets selected from config/swap-allowlist.json.",
                "alchemy_network": base_network.get("alchemy_network"),
            }
        )
    return {
        "revision": "allowlist-rpc-v1",
        "checked_at": checked_at,
        "source": "https://jumper.exchange/",
        "networks": result,
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base", type=Path, required=True)
    parser.add_argument("--allowlist", type=Path, required=True)
    parser.add_argument("--inventory", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if args.output.exists():
        parser.error("output exists; choose a new file")
    base = json.loads(args.base.read_text(encoding="utf-8"))
    allowlist = json.loads(args.allowlist.read_text(encoding="utf-8"))
    inventory = json.loads(args.inventory.read_text(encoding="utf-8"))
    catalog = build_catalog(base, allowlist, inventory, date.today().isoformat())
    args.output.parent.mkdir(parents=True, exist_ok=True)
    with args.output.open("x", encoding="utf-8") as stream:
        json.dump(catalog, stream, ensure_ascii=False, indent=2)
        stream.write("\n")
    print(f"Catalog: {args.output}; {len(catalog['networks'])} networks.")


if __name__ == "__main__":
    main()
