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


@pytest.mark.parametrize("coin,chain,chain_id", [
    ("BNB", "BEP20", 56), ("CORE", "CoreDAO", 1116),
    ("POL", "Polygon", 137), ("METIS", "MetisToken", 1088),
    ("AVAX", "AVAXC-Chain", 43114), ("ETH", "SCROLL", 534352),
    ("ETH", "zkSyncEra", 324),
])
def test_catalog_recognizes_enabled_native_deposits(coin, chain, chain_id):
    targets = deposit_targets([{"coin": coin, "chains": [{
        "chain": chain, "rechargeable": "true", "needTag": "false",
        "contractAddress": None, "minDepositAmount": "0.01",
    }]}])
    assert targets == (BitgetDepositTarget(coin, chain_id, "native", 10**16, chain, 18),)


def test_catalog_uses_contract_for_eth_on_bsc_and_network_specific_stablecoin_precision():
    contract = "0x" + "a" * 40
    data = [{"coin": coin, "chains": [{
        "chain": "BEP20", "rechargeable": "true", "contractAddress": contract,
        "minDepositAmount": "0.01",
    }]} for coin in ("ETH", "USDT", "USDC")]
    targets = deposit_targets(data)
    assert len(targets) == 3
    assert all(t.asset_id == contract and t.decimals == 18 and t.minimum_raw == 10**16
               for t in targets)


@pytest.mark.parametrize("chain,coin,contract,need_tag", [
    ("AVAXX-Chain", "AVAX", None, "false"),
    ("BEP20", "ETH", None, "false"),
    ("Polygon", "ETH", None, "false"),
    ("CoreDAO", "CORE", None, "true"),
    ("CoreDAO", "CORE", "invalid", "false"),
])
def test_catalog_rejects_non_evm_wrong_native_tag_and_bad_contract(chain, coin, contract, need_tag):
    assert not deposit_targets([{"coin": coin, "chains": [{
        "chain": chain, "rechargeable": "true", "contractAddress": contract,
        "needTag": need_tag, "minDepositAmount": "0.01",
    }]}])
