import csv
from pathlib import Path

from evm_inventory.bitget_catalog import BitgetDepositTarget
from evm_inventory.lifi import LifiGasCost, LifiRoute
from evm_inventory.live_plan import create_live_plan


class Quotes:
    def routes(self, request):
        assert request.to_address == "0x" + "1" * 40
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
        now_ms=lambda: 1_700_000_000_000,
    )

    assert plan["summary"] == {"post_bridge_deposit": 1, "route_ready": 1}
    assert plan["quoted_at"] == 1_700_000_000_000
    assert plan["entries"][0]["quoted_at"] == 1_700_000_000_000
    assert plan["entries"][0]["route"]["id"] == "r1"
    assert plan["entries"][1]["status"] == "post_bridge_deposit"


def test_live_plan_stages_existing_base_usdc_for_one_final_deposit(tmp_path):
    balances = tmp_path / "balances.csv"
    balances.write_text(
        "wallet,chain_id,asset_id,raw_balance,decimals,symbol,status\n"
        + "0x" + "1" * 40
        + ",8453,0x833589fcd6edb6e08f4c7c32d4f71b54bda02913,10000,6,USDC,success\n"
    )
    allowlist = tmp_path / "allowlist.json"
    allowlist.write_text(
        '{"assets":[{"chain_id":8453,"asset_id":"0x833589fcd6edb6e08f4c7c32d4f71b54bda02913","action":"swap"}]}'
    )
    targets = (
        BitgetDepositTarget("USDC", 8453, "0x833589fcd6edb6e08f4c7c32d4f71b54bda02913", 9_997),
    )

    plan = create_live_plan(
        balances,
        deposit_addresses={"0x" + "1" * 40: "0x" + "2" * 40},
        allowlist_path=allowlist,
        client=Quotes(),
        targets=targets,
    )

    assert [entry["status"] for entry in plan["entries"]] == [
        "stage_existing", "post_bridge_deposit"
    ]


def test_live_plan_quotes_full_native_balance_without_gas_preflight_fields(tmp_path):
    balances = tmp_path / "balances.csv"
    balances.write_text(
        "wallet,chain_id,asset_id,raw_balance,decimals,symbol,status\n"
        + "0x" + "1" * 40 + ",10,native,10000,6,ETH,success\n"
    )
    allowlist = tmp_path / "allowlist.json"
    allowlist.write_text('{"assets":[{"chain_id":10,"asset_id":"native","action":"swap"}]}')
    target = BitgetDepositTarget("USDC", 8453, "0x833589fcd6edb6e08f4c7c32d4f71b54bda02913", 1)

    class NativeQuotes:
        def __init__(self):
            self.requests = []

        def routes(self, request):
            self.requests.append(request)
            return (
                LifiRoute(
                    "native-route",
                    10_000,
                    10_000,
                    10_000,
                    (),
                    ("across",),
                    {
                        "estimate": {
                            "gasCosts": [{"amount": "999999", "token": {"chainId": 10}}]
                        }
                    },
                ),
            )

    quotes = NativeQuotes()
    plan = create_live_plan(
        balances,
        deposit_addresses={"0x" + "1" * 40: "0x" + "2" * 40},
        allowlist_path=allowlist,
        client=quotes,
        targets=(target,),
    )

    assert quotes.requests[0].from_amount == "10000"
    assert len(quotes.requests) == 1
    assert plan["entries"][0]["status"] == "route_ready"
    assert "requires_gas_preflight" not in plan["entries"][0]
    assert "requires_gas_preflight" not in plan["entries"][1]
    assert "gas_reserve_multiplier" not in plan["execution"]


def test_live_plan_quotes_ohno_only_on_blast_with_the_configured_allowlist(tmp_path):
    contract = "0x000000daa580e54635a043d2773f2c698593836a"
    balances = tmp_path / "balances.csv"
    balances.write_text(
        "wallet,chain_id,asset_id,raw_balance,decimals,symbol,status\n"
        + "0x" + "1" * 40 + f",81457,{contract},10000000000000000,18,OHNO,success\n"
        + "0x" + "1" * 40 + f",10,{contract},10000000000000000,18,OHNO,success\n"
    )
    target = BitgetDepositTarget("USDC", 8453, "0x833589fcd6edb6e08f4c7c32d4f71b54bda02913", 1)

    plan = create_live_plan(
        balances,
        deposit_addresses={"0x" + "1" * 40: "0x" + "2" * 40},
        allowlist_path=Path(__file__).parents[1] / "config" / "swap-allowlist.json",
        client=Quotes(),
        targets=(target,),
    )

    assert [entry["status"] for entry in plan["entries"][:2]] == ["route_ready", "denied"]
