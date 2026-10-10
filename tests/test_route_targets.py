import json

import pytest

from evm_inventory.bitget_catalog import BitgetDepositTarget
from evm_inventory.models import ConfigError
from evm_inventory.route_targets import load_target_policy, resolve_targets

A = "0x" + "a" * 40
B = "0x" + "b" * 40


def policy(tmp_path, targets):
    path = tmp_path / "targets.json"
    path.write_text(json.dumps({"version": 1, "targets": targets}))
    return load_target_policy(path)


def test_policy_intersects_exact_live_identity_and_preserves_order(tmp_path):
    p = policy(
        tmp_path,
        [
            {"coin": "usdc", "chain_id": 10, "asset_id": A},
            {"coin": "USDC", "chain_id": 8453, "asset_id": B},
        ],
    )
    live = (
        BitgetDepositTarget("USDC", 8453, B, 100),
        BitgetDepositTarget("USDC", 10, A, 200),
        BitgetDepositTarget("USDT", 10, B, 1),
    )
    r = resolve_targets(p, live)
    assert r.enabled == (live[1], live[0])
    assert r.unavailable == ()
    assert len(p.digest) == 64


@pytest.mark.parametrize(
    "targets",
    [
        [],
        [{"coin": "USDC", "chain_id": 10, "asset_id": A}] * 2,
        [{"coin": "USDC", "chain_id": i, "asset_id": A} for i in (1, 10, 8453, 137)],
        [{"coin": "USDC", "chain_id": True, "asset_id": A}],
        [{"coin": "USDC", "chain_id": 183, "asset_id": A}],
        [{"coin": "USDC", "chain_id": 10, "asset_id": "USDC"}],
    ],
)
def test_bad_policy_is_rejected(tmp_path, targets):
    with pytest.raises(ConfigError):
        policy(tmp_path, targets)


def test_closed_or_conflicting_target_has_explicit_reason(tmp_path):
    p = policy(tmp_path, [{"coin": "USDC", "chain_id": 10, "asset_id": A, "decimals": 6}])
    assert resolve_targets(p, ()).unavailable[0]["reason"] == "target_unavailable"
    bad = BitgetDepositTarget("USDC", 10, A, 1, decimals=18)
    assert resolve_targets(p, (bad,)).unavailable[0]["reason"] == "target_decimals_mismatch"
    other = BitgetDepositTarget("USDT", 10, A, 1)
    assert resolve_targets(p, (other,)).unavailable[0]["reason"] == "target_coin_mismatch"


def test_conflicting_live_identity_is_rejected_before_decimals_filter(tmp_path):
    p = policy(tmp_path, [{"coin": "USDC", "chain_id": 10, "asset_id": A, "decimals": 6}])
    with pytest.raises(ConfigError, match="ambiguous"):
        resolve_targets(
            p,
            (
                BitgetDepositTarget("USDC", 10, A, 1, decimals=6),
                BitgetDepositTarget("USDC", 10, A, 1, decimals=18),
            ),
        )


def test_case_insensitive_duplicates_and_digest_stability(tmp_path):
    first = policy(
        tmp_path, [{"coin": "usdc", "chain_id": 10, "asset_id": A.upper().replace("0X", "0x")}]
    )
    second = policy(tmp_path, [{"coin": "USDC", "chain_id": 10, "asset_id": A}])
    assert first.digest == second.digest
    with pytest.raises(ConfigError, match="duplicate"):
        policy(
            tmp_path,
            [
                {"coin": "usdc", "chain_id": 10, "asset_id": A.upper()},
                {"coin": "USDC", "chain_id": 10, "asset_id": A},
            ],
        )


def test_native_target_preserves_live_decimals(tmp_path):
    p = policy(tmp_path, [{"coin": "ETH", "chain_id": 1, "asset_id": "native"}])
    target = BitgetDepositTarget("ETH", 1, "native", 123, decimals=18)
    assert resolve_targets(p, (target,)).enabled == (target,)
