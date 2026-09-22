import json
import re
from pathlib import Path

import pytest

from evm_inventory.live_plan import _actions
from evm_inventory.models import ConfigError

ALLOWLIST = Path(__file__).parents[1] / "config" / "swap-allowlist.json"
ADDRESS = re.compile(r"0x[0-9a-f]{40}$")


def test_swap_allowlist_is_strict_and_has_unique_asset_identities():
    data = json.loads(ALLOWLIST.read_text(encoding="utf-8"))
    assert not {"version", "default_action", "bridge_mappings"}.intersection(data)
    assert data["notes"]
    identities = set()
    for asset in data["assets"]:
        assert asset["action"] in {"swap", "unwrap", "review", "deny"}
        assert isinstance(asset["chain_id"], int) and asset["chain_id"] > 0
        assert asset["asset_id"] == "native" or ADDRESS.fullmatch(asset["asset_id"])
        identity = (asset["chain_id"], asset["asset_id"])
        assert identity not in identities
        identities.add(identity)


def test_swap_allowlist_admits_only_the_exact_chain_and_asset_identity():
    data = json.loads(ALLOWLIST.read_text(encoding="utf-8"))
    actions = _actions(ALLOWLIST)
    ohno = next(asset for asset in data["assets"] if asset["symbol"] == "OHNO")

    assert actions[(ohno["chain_id"], ohno["asset_id"])] == ohno["action"] == "swap"
    assert actions.get((10, ohno["asset_id"]), "deny") == "deny"
    assert actions.get((ohno["chain_id"], "OHNO"), "deny") == "deny"


def test_swap_allowlist_preserves_each_per_asset_action_by_exact_identity(tmp_path):
    identities = [
        (10, "0x" + "a" * 40, "swap"),
        (10, "0x" + "b" * 40, "unwrap"),
        (10, "0x" + "c" * 40, "review"),
        (10, "0x" + "d" * 40, "deny"),
    ]
    path = tmp_path / "allowlist.json"
    path.write_text(
        json.dumps(
            {
                "assets": [
                    {
                        "chain_id": chain_id,
                        "asset_id": asset_id,
                        "symbol": "SAME",
                        "action": action,
                    }
                    for chain_id, asset_id, action in identities
                ]
            }
        ),
        encoding="utf-8",
    )

    actions = _actions(path)

    assert [actions[identity[:2]] for identity in identities] == [
        "swap",
        "unwrap",
        "review",
        "deny",
    ]
    assert actions.get((42161, identities[0][1]), "deny") == "deny"
    assert actions.get((10, "SAME"), "deny") == "deny"


def test_swap_allowlist_rejects_an_unknown_per_asset_action(tmp_path):
    path = tmp_path / "allowlist.json"
    path.write_text(
        json.dumps(
            {
                "assets": [
                    {
                        "chain_id": 10,
                        "asset_id": "0x" + "a" * 40,
                        "symbol": "USDC",
                        "action": "swapp",
                    }
                ]
            }
        ),
        encoding="utf-8",
    )

    with pytest.raises(ConfigError, match="action"):
        _actions(path)


def test_swap_allowlist_rejects_duplicate_asset_identities(tmp_path):
    identity = {"chain_id": 10, "asset_id": "0x" + "a" * 40, "symbol": "USDC"}
    path = tmp_path / "allowlist.json"
    path.write_text(
        json.dumps(
            {
                "assets": [
                    {**identity, "action": "deny"},
                    {**identity, "action": "swap"},
                ]
            }
        ),
        encoding="utf-8",
    )

    with pytest.raises(ConfigError, match="duplicate"):
        _actions(path)
