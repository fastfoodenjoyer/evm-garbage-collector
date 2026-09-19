import json
import re
from pathlib import Path

ALLOWLIST = Path(__file__).parents[1] / "config" / "swap-allowlist.json"
ADDRESS = re.compile(r"0x[0-9a-f]{40}$")


def test_swap_allowlist_is_strict_and_has_unique_asset_identities():
    data = json.loads(ALLOWLIST.read_text(encoding="utf-8"))
    assert data["default_action"] == "deny"
    assert data["version"] == 1
    identities = set()
    for asset in data["assets"]:
        assert asset["action"] in {"swap", "unwrap", "review"}
        assert isinstance(asset["chain_id"], int) and asset["chain_id"] > 0
        assert asset["asset_id"] == "native" or ADDRESS.fullmatch(asset["asset_id"])
        identity = (asset["chain_id"], asset["asset_id"])
        assert identity not in identities
        identities.add(identity)
