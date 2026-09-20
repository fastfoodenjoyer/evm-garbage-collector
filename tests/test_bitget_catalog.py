from evm_inventory.bitget_catalog import deposit_targets


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
    assert targets[0].minimum_raw == 9_997
