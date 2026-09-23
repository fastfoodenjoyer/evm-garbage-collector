from evm_inventory.defi_plan import create_defi_plan

WALLET = "0x" + "1" * 40
ROUTER = "0x" + "2" * 40


class Rabby:
    def chain_ids(self):
        return {"eth": 1, "base": 8453}

    def positions(self, wallet):
        assert wallet == WALLET
        return self.data


def protocol(item, *, chain="eth", protocol_id="aave3"):
    return {"id": protocol_id, "chain": chain, "name": "Aave", "portfolio_item_list": [item]}


def position(*, debt=0, proxy=None, action=None, pool="pool-a"):
    return {
        "name": "Lending",
        "pool": {"id": pool, "chain": "eth"},
        "position_index": "",
        "stats": {"net_usd_value": 12, "debt_usd_value": debt},
        "proxy_detail": proxy or {},
        "detail": {"supply_token_list": [{"id": "0x" + "3" * 40, "amount": 12}]},
        "withdraw_actions": [action] if action else [],
    }


def withdraw():
    return {
        "type": "withdraw",
        "contract_id": ROUTER,
        "func": "withdraw(uint256,address)",
        "str_params": ["12", WALLET],
        "need_approve": {},
    }


def test_ready_position_has_stable_action_id_and_no_keys():
    rabby = Rabby()
    rabby.data = [protocol(position(action=withdraw()))]
    plan = create_defi_plan([WALLET], rabby, supported_chain_ids={1}, now_seconds=100)
    assert plan["summary"] == {"ready": 1, "manual_review": 0}
    entry = plan["entries"][0]
    assert entry["status"] == "ready"
    assert len(entry["action_id"]) == 64
    assert entry["wallet"] == WALLET
    assert entry["chain_id"] == 1
    assert entry["action"] == withdraw()
    assert entry["output_token_ids"] == ["0x" + "3" * 40]


def test_debt_and_proxy_positions_are_manual_review():
    rabby = Rabby()
    rabby.data = [
        protocol(position(debt=1, action=withdraw(), pool="one")),
        protocol(position(proxy={"proxy_contract_id": ROUTER}, action=withdraw(), pool="two")),
    ]
    plan = create_defi_plan([WALLET], rabby, supported_chain_ids={1}, now_seconds=100)
    assert [row["reason"] for row in plan["entries"]] == ["outstanding_debt", "proxy_position"]


def test_queue_and_missing_actions_are_manual_review():
    rabby = Rabby()
    queue = {**withdraw(), "type": "queue"}
    rabby.data = [
        protocol(position(action=queue, pool="one")),
        protocol(position(pool="two")),
    ]
    plan = create_defi_plan([WALLET], rabby, supported_chain_ids={1}, now_seconds=100)
    assert [row["status"] for row in plan["entries"]] == ["manual_review"] * 2


def test_duplicate_position_identity_is_not_executable():
    rabby = Rabby()
    item = position(action=withdraw())
    rabby.data = [{"id": "aave3", "chain": "eth", "portfolio_item_list": [item, item]}]
    plan = create_defi_plan([WALLET], rabby, supported_chain_ids={1}, now_seconds=100)
    assert all(row["reason"] == "duplicate_position" for row in plan["entries"])


def test_unsupported_chain_and_unsafe_action_are_manual_review():
    rabby = Rabby()
    bad = {**withdraw(), "str_params": ["12", ROUTER]}
    rabby.data = [
        protocol(position(action=withdraw(), pool="one"), chain="base"),
        protocol(position(action=bad, pool="two")),
    ]
    plan = create_defi_plan([WALLET], rabby, supported_chain_ids={1}, now_seconds=100)
    assert [row["reason"] for row in plan["entries"]] == [
        "chain_not_configured",
        "unsafe_or_unsupported_action",
    ]


def test_fuel_native_eth_position_can_be_planned():
    fuel = "0x19b5cc75846bf6286d599ec116536a333c4c2c14"
    rabby = Rabby()
    item = position(pool=fuel, action={
        "type": "withdraw", "contract_id": fuel,
        "func": "withdraw(address,address,uint240)()",
        "str_params": ["0x" + "0" * 40, WALLET, "1023000000000000"],
    })
    item["detail"]["supply_token_list"] = [{"id": "eth", "amount": 0.001023}]
    rabby.data = [protocol(item, protocol_id="fuel")]
    plan = create_defi_plan([WALLET], rabby, supported_chain_ids={1}, now_seconds=100)
    row = plan["entries"][0]
    assert row["status"] == "ready"
    assert row["output_token_ids"] == ["eth"]
