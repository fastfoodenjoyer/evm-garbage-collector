from evm_inventory.planner import direct_native_deposit_plan


def test_direct_native_deposit_planning_does_not_subtract_gas_reserve():
    plan = direct_native_deposit_plan(
        balance_wei=1_000_000,
        minimum_deposit_wei=600_000,
    )

    assert plan.status == "ready"
    assert plan.send_amount_wei == 1_000_000
    assert not hasattr(plan, "gas_reserve_wei")
