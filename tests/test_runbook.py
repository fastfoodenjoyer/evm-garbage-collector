import csv

from evm_inventory.runbook import create_route_plan


def test_create_route_plan_marks_direct_deposit_and_dust(tmp_path):
    balances = tmp_path / "balances.csv"
    with balances.open("w", newline="") as stream:
        writer = csv.DictWriter(
            stream, fieldnames=["wallet", "chain_id", "asset_id", "raw_balance", "symbol"]
        )
        writer.writeheader()
        writer.writerows([
            {"wallet": "0x" + "1" * 40, "chain_id": "8453", "asset_id": "native",
             "raw_balance": "1", "symbol": "ETH"},
            {"wallet": "0x" + "1" * 40, "chain_id": "8453",
             "asset_id": "0x833589fcd6edb6e08f4c7c32d4f71b54bda02913",
             "raw_balance": "10000", "symbol": "USDC"},
        ])

    plan = create_route_plan(balances, quote_floor_raw=100)

    assert plan["summary"] == {"direct_deposit": 1, "dust": 1, "quote_required": 0}
    assert plan["execution"]["delay_min_seconds"] == 1800
