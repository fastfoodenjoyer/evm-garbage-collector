import csv

from evm_inventory.bitget_catalog import BitgetDepositTarget
from evm_inventory.lifi import LifiGasCost, LifiRoute
from evm_inventory.live_plan import create_live_plan


class Quotes:
    def routes(self, request):
        assert request.to_address == "0x" + "2" * 40
        return (
            LifiRoute(
                "r1",
                50_000,
                20_000,
                10_000,
                (LifiGasCost(1, None),),
                ("across",),
                {"tool": "across"},
            ),
        )


def test_live_plan_keeps_only_route_meeting_bitget_minimum(tmp_path):
    balances = tmp_path / "balances.csv"
    with balances.open("w", newline="") as stream:
        writer = csv.DictWriter(
            stream,
            fieldnames=[
                "wallet", "chain_id", "asset_id", "raw_balance", "decimals", "symbol", "status"
            ],
        )
        writer.writeheader()
        writer.writerow({
            "wallet": "0x" + "1" * 40, "chain_id": 10,
            "asset_id": "0x0b2c639c533813f4aa9d7837caf62653d097ff85",
            "raw_balance": 50_000, "decimals": 6, "symbol": "USDC", "status": "success",
        })
    allowlist = tmp_path / "allowlist.json"
    allowlist.write_text(
        '{"assets":[{"chain_id":10,"asset_id":"0x0b2c639c533813f4aa9d7837caf62653d097ff85","action":"swap"}]}'
    )
    targets = (
        BitgetDepositTarget("USDC", 8453, "0x833589fcd6edb6e08f4c7c32d4f71b54bda02913", 9_997),
    )

    plan = create_live_plan(
        balances,
        deposit_addresses={"0x" + "1" * 40: "0x" + "2" * 40},
        allowlist_path=allowlist,
        quote_floor="0.01",
        client=Quotes(),
        targets=targets,
    )

    assert plan["summary"] == {"route_ready": 1}
    assert plan["entries"][0]["route"]["id"] == "r1"
