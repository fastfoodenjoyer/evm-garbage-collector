from scripts.build_allowlist_catalog import build_catalog


def test_builds_exact_rpc_catalog_from_allowlist_and_inventory():
    base = {
        "networks": [
            {
                "chain_id": 10,
                "name": "OP Mainnet",
                "rpc_urls": ["https://rpc.example"],
                "native_symbol": "ETH",
                "native_decimals": 18,
            }
        ]
    }
    allowlist = {
        "assets": [
            {"chain_id": 10, "asset_id": "native", "symbol": "ETH"},
            {"chain_id": 10, "asset_id": "0x" + "a" * 40, "symbol": "USDC"},
        ],
    }
    inventory = {
        "balances": [
            {"chain_id": 10, "asset_id": "0x" + "a" * 40, "decimals": 6, "name": "USD Coin"}
        ]
    }

    catalog = build_catalog(base, allowlist, inventory, "2026-09-19")

    assert catalog["networks"][0]["token_review_status"] == "verified"
    assert catalog["networks"][0]["tokens"] == [
        {
            "address": "0x" + "a" * 40,
            "symbol": "USDC",
            "decimals": 6,
            "source": "https://jumper.exchange/",
            "checked_at": "2026-09-19",
            "variant": "USD Coin",
        }
    ]
