"""Exact, operator-configured destination choices for bounded LI.FI searches."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from hashlib import sha256
from pathlib import Path

from .bitget_catalog import BitgetDepositTarget
from .models import ConfigError
from .network_policy import EXCLUDED_NETWORKS

_ADDRESS = re.compile(r"0x[0-9a-fA-F]{40}")


@dataclass(frozen=True)
class TargetSpec:
    coin: str
    chain_id: int
    asset_id: str
    decimals: int | None = None

    def data(self):
        return {
            "coin": self.coin,
            "chain_id": self.chain_id,
            "asset_id": self.asset_id,
            **({"decimals": self.decimals} if self.decimals is not None else {}),
        }


@dataclass(frozen=True)
class TargetPolicy:
    targets: tuple[TargetSpec, ...]

    def __post_init__(self):
        if not 1 <= len(self.targets) <= 3:
            raise ConfigError("route targets must contain one to three exact destinations")
        identities = set()
        for spec in self.targets:
            if (
                isinstance(spec.chain_id, bool)
                or not isinstance(spec.chain_id, int)
                or spec.chain_id <= 0
                or spec.chain_id in EXCLUDED_NETWORKS
                or not spec.coin
                or spec.coin != spec.coin.upper()
                or (spec.asset_id != "native" and not _ADDRESS.fullmatch(spec.asset_id))
                or spec.asset_id != spec.asset_id.lower()
                or (
                    spec.decimals is not None
                    and (
                        isinstance(spec.decimals, bool)
                        or not isinstance(spec.decimals, int)
                        or not 0 <= spec.decimals <= 255
                    )
                )
            ):
                raise ConfigError("invalid route target identity")
            identity = (spec.chain_id, spec.asset_id)
            if identity in identities:
                raise ConfigError("duplicate route target identity")
            identities.add(identity)

    @property
    def digest(self) -> str:
        return sha256(
            json.dumps([s.data() for s in self.targets], sort_keys=True).encode()
        ).hexdigest()

    @classmethod
    def from_targets(cls, targets: tuple[BitgetDepositTarget, ...]):
        return cls(
            tuple(
                TargetSpec(t.coin.upper(), t.chain_id, t.asset_id.lower(), t.decimals)
                for t in targets
            )
        )


@dataclass(frozen=True)
class TargetResolution:
    enabled: tuple[BitgetDepositTarget, ...]
    unavailable: tuple[dict, ...]


def load_target_policy(path: Path) -> TargetPolicy:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise ConfigError(f"cannot read route targets policy {path}") from exc
    if (
        not isinstance(data, dict)
        or type(data.get("version")) is not int
        or data["version"] != 1
        or not isinstance(data.get("targets"), list)
    ):
        raise ConfigError("invalid route targets policy schema")
    specs = []
    for raw in data["targets"]:
        if (
            not isinstance(raw, dict)
            or not isinstance(raw.get("coin"), str)
            or not isinstance(raw.get("asset_id"), str)
        ):
            raise ConfigError("invalid route target identity")
        specs.append(
            TargetSpec(
                raw["coin"].upper(),
                raw.get("chain_id"),
                raw["asset_id"].lower(),
                raw.get("decimals"),
            )
        )
    return TargetPolicy(tuple(specs))


def resolve_targets(policy: TargetPolicy, live_targets: tuple[BitgetDepositTarget, ...]):
    enabled, unavailable = [], []
    for spec in policy.targets:
        matches = [
            t
            for t in live_targets
            if t.chain_id == spec.chain_id and t.asset_id.lower() == spec.asset_id
        ]
        exact = [t for t in matches if t.coin.upper() == spec.coin]
        if len(exact) > 1:
            raise ConfigError("ambiguous live deposit target identity")
        compatible = [t for t in exact if spec.decimals is None or spec.decimals == t.decimals]
        if compatible:
            if len(compatible) != 1:
                raise ConfigError("ambiguous live deposit target identity")
            enabled.append(compatible[0])
        else:
            reason = (
                "target_decimals_mismatch"
                if exact
                else "target_coin_mismatch"
                if matches
                else "target_unavailable"
            )
            unavailable.append({**spec.data(), "reason": reason})
    return TargetResolution(tuple(enabled), tuple(unavailable))
