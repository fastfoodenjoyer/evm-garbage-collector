"""Explain exact asset policy decisions in both planner formats."""

from pathlib import Path


def denied_entry(base: dict, *, policy: dict, path: Path) -> dict:
    identity = (int(base["chain_id"]), str(base["asset_id"]).lower())
    explicit = identity in policy
    return {
        **base,
        "status": "denied",
        "reason": "asset_explicitly_denied" if explicit else "asset_not_allowlisted",
        "reason_detail": {
            "message": "Asset is explicitly denied by policy"
            if explicit
            else "Exact network and asset are absent from the execution allowlist",
            "policy_file": str(path),
            "chain_id": identity[0],
            "asset_id": identity[1],
            "matched_rule": explicit,
            "action": "deny",
        },
    }
