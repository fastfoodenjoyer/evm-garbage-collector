import pytest

from evm_inventory.bitget_catalog import BitgetDepositTarget, deposit_targets, validate_live_target


def test_deposit_targets_only_include_enabled_supported_evm_chains():
    data = [
        {"coin": "USDC", "chains": [
            {"chain": "BASE", "rechargeable": "true", "contractAddress": "0x" + "1" * 40,
             "minDepositAmount": "0.009997"},
            {"chain": "SOL", "rechargeable": "true", "contractAddress": None,
             "minDepositAmount": "0.01"},
        ]}
    ]

    targets = deposit_targets(data)

    assert len(targets) == 1
    assert targets[0].chain_id == 8453
    assert targets[0].chain == "BASE"
    assert targets[0].minimum_raw == 9_997


def test_live_target_revalidation_requires_exact_enabled_target_and_user_address():
    planned = BitgetDepositTarget("USDC", 8453, "0x" + "1" * 40, 9_997, "BASE")
    live = (BitgetDepositTarget("USDC", 8453, "0x" + "1" * 40, 10_000, "BASE"),)

    refreshed = validate_live_target(
        planned, live_targets=live, user_address="0x" + "2" * 40
    )

    assert refreshed.minimum_raw == 10_000
    with pytest.raises(ValueError, match="deposit target is no longer enabled"):
        validate_live_target(
            planned, live_targets=(), user_address="0x" + "2" * 40
        )
    with pytest.raises(ValueError, match="invalid Bitget deposit address"):
        validate_live_target(planned, live_targets=live, user_address="not-an-address")
