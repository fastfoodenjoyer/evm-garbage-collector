"""Load, validate, and serialize inventory inputs."""

from __future__ import annotations

import hashlib
import json
import math
import re
from collections.abc import Mapping
from dataclasses import asdict, is_dataclass
from decimal import Decimal, localcontext
from importlib.resources import files
from pathlib import Path
from typing import Any

from .models import Catalog, ConfigError, Network, Token

_WALLET_RE = re.compile(r"^0x[0-9a-fA-F]{40}$")
_SNAPSHOT_SETTINGS = frozenset({"delay_min", "delay_max", "interval", "discovery_enabled"})


def load_wallets(path: Path) -> tuple[str, ...]:
    """Read and normalize public EVM addresses, reporting every bad line."""

    path = Path(path)
    try:
        text = path.read_text(encoding="utf-8")
    except UnicodeError as exc:
        raise ConfigError("wallet file must be valid UTF-8") from exc
    except OSError as exc:
        raise ConfigError(f"cannot read wallet file {path}: {exc}") from exc

    addresses: list[str] = []
    seen: set[str] = set()
    issues: list[str] = []
    for line_number, raw_line in enumerate(text.splitlines(), start=1):
        value = raw_line.strip()
        if not value:
            continue
        normalized = value.lower()
        if (
            len(value) != 42
            or not normalized.startswith("0x")
            or any(character not in "0123456789abcdef" for character in normalized[2:])
        ):
            issues.append(f"line {line_number}: invalid EVM address {value!r}")
            continue
        if normalized not in seen:
            addresses.append(normalized)
            seen.add(normalized)

    if issues:
        raise ConfigError("invalid wallet file: " + "; ".join(issues), issues=tuple(issues))
    if not addresses:
        raise ConfigError("wallet file must contain at least one wallet address")
    return tuple(addresses)


def _as_mapping(value: Any, *, context: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise ConfigError(f"{context} must be an object")
    return value


def _token_from_dict(value: Any, index: int, chain_id: int) -> Token:
    data = _as_mapping(value, context=f"network {chain_id} token {index}")
    required = ("address", "symbol", "decimals", "source", "checked_at")
    missing = [field for field in required if field not in data]
    if missing:
        raise ConfigError(f"network {chain_id} token {index} missing fields: {', '.join(missing)}")
    return Token(
        address=data["address"],
        symbol=data["symbol"],
        decimals=data["decimals"],
        source=data["source"],
        checked_at=data["checked_at"],
        variant=data.get("variant", ""),
    )


def _network_from_dict(value: Any, index: int) -> Network:
    data = _as_mapping(value, context=f"network {index}")
    required = (
        "chain_id",
        "name",
        "rpc_urls",
        "native_symbol",
        "native_decimals",
        "tokens",
    )
    missing = [field for field in required if field not in data]
    if missing:
        raise ConfigError(f"network {index} missing fields: {', '.join(missing)}")
    try:
        chain_id = data["chain_id"]
        tokens = tuple(
            _token_from_dict(item, token_index, chain_id)
            for token_index, item in enumerate(data["tokens"], start=1)
        )
        network = Network(
            chain_id=chain_id,
            name=data["name"],
            rpc_urls=tuple(data["rpc_urls"]),
            native_symbol=data["native_symbol"],
            native_decimals=data["native_decimals"],
            tokens=tokens,
            token_review_status=data.get("token_review_status", "pending"),
            notes=data.get("notes", ""),
            alchemy_network=data.get("alchemy_network"),
        )
        return network
    except (TypeError, KeyError) as exc:
        raise ConfigError(f"network {index} has malformed fields: {exc}") from exc


def load_catalog(path: Path | None = None) -> Catalog:
    """Load a catalog JSON file, or the package's catalog resource by default."""

    if path is None:
        resource = files("evm_inventory").joinpath("data/catalog.json")
        try:
            raw = resource.read_text(encoding="utf-8")
        except (FileNotFoundError, ModuleNotFoundError, OSError) as exc:
            raise ConfigError("packaged catalog data/catalog.json is unavailable") from exc
        location = "packaged catalog"
    else:
        try:
            raw = path.read_text(encoding="utf-8")
        except OSError as exc:
            raise ConfigError(f"cannot read catalog {path}: {exc}") from exc
        location = str(path)
    try:
        value = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ConfigError(
            f"invalid catalog JSON in {location}: {exc.msg} at line {exc.lineno}"
        ) from exc
    data = _as_mapping(value, context="catalog")
    required = ("networks", "revision", "checked_at", "source")
    missing = [field for field in required if field not in data]
    if missing:
        raise ConfigError(f"catalog missing fields: {', '.join(missing)}")
    if not isinstance(data["networks"], list):
        raise ConfigError("catalog networks must be an array")
    networks = tuple(
        _network_from_dict(item, index) for index, item in enumerate(data["networks"], start=1)
    )
    catalog = Catalog(
        networks=networks,
        revision=data["revision"],
        checked_at=data["checked_at"],
        source=data["source"],
    )
    return catalog.validate()


def validate_catalog(catalog: Catalog) -> Catalog:
    """Validate and return a catalog, for callers that already parsed one."""

    if not isinstance(catalog, Catalog):
        raise ConfigError("catalog must be a Catalog instance")
    return catalog.validate()


def validate_delays(delay_min: float, delay_max: float) -> tuple[float, float]:
    """Validate a nonnegative finite inclusive delay interval."""

    values = (delay_min, delay_max)
    if any(isinstance(value, bool) or not isinstance(value, (int, float)) for value in values):
        raise ConfigError("delay bounds must be numbers")
    minimum, maximum = float(delay_min), float(delay_max)
    if not math.isfinite(minimum) or not math.isfinite(maximum):
        raise ConfigError("delay bounds must be finite")
    if minimum < 0 or maximum < 0:
        raise ConfigError("delay bounds must be nonnegative")
    if minimum > maximum:
        raise ConfigError("delay minimum must not exceed delay maximum")
    return minimum, maximum


def _jsonable(value: Any) -> Any:
    if is_dataclass(value):
        return _jsonable(asdict(value))
    if isinstance(value, Mapping):
        return {str(key): _jsonable(item) for key, item in value.items()}
    if isinstance(value, (tuple, list)):
        return [_jsonable(item) for item in value]
    if isinstance(value, set):
        return sorted(_jsonable(item) for item in value)
    return value


def snapshot(
    catalog: Catalog, wallets: tuple[str, ...] | list[str], settings: Any
) -> dict[str, Any]:
    """Create a JSON-friendly immutable run scope without reading environment values."""

    catalog.validate()
    normalized_wallets: list[str] = []
    seen: set[str] = set()
    issues: list[str] = []
    for index, wallet in enumerate(wallets, start=1):
        if not isinstance(wallet, str) or not _WALLET_RE.fullmatch(wallet):
            issues.append(f"wallet {index}: invalid EVM address")
            continue
        wallet = wallet.lower()
        if wallet not in seen:
            normalized_wallets.append(wallet)
            seen.add(wallet)
    if issues:
        raise ConfigError("invalid snapshot wallets: " + "; ".join(issues), issues=tuple(issues))
    if not normalized_wallets:
        raise ConfigError("snapshot requires at least one wallet")
    if settings is None:
        settings = {}
    if not isinstance(settings, Mapping):
        raise ConfigError("snapshot settings must be an object")
    unknown = set(settings) - _SNAPSHOT_SETTINGS
    if unknown:
        raise ConfigError("snapshot settings contain unsupported fields")
    clean_settings = dict(settings)
    for name in ("delay_min", "delay_max", "interval"):
        if name in clean_settings:
            value = clean_settings[name]
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                raise ConfigError(f"snapshot setting {name} must be a number")
            if not math.isfinite(float(value)) or float(value) < 0:
                raise ConfigError(f"snapshot setting {name} must be finite and nonnegative")
    if "delay_min" in clean_settings and "delay_max" in clean_settings:
        validate_delays(clean_settings["delay_min"], clean_settings["delay_max"])
    if "discovery_enabled" in clean_settings and not isinstance(
        clean_settings["discovery_enabled"], bool
    ):
        raise ConfigError("snapshot setting discovery_enabled must be boolean")
    return _jsonable(
        {
            "catalog": catalog,
            "wallets": tuple(normalized_wallets),
            "settings": clean_settings,
        }
    )


def add_allowlisted_tokens(scope: dict[str, Any], path: Path) -> dict[str, Any]:
    """Make every exact non-native execution asset a mandatory RPC check.

    Token decimals are deliberately omitted here: the RPC reader obtains them
    from the contract, avoiding guessed metadata in a safety boundary.
    """

    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ConfigError(f"cannot read execution allowlist {path}") from exc
    assets = data.get("assets") if isinstance(data, dict) else None
    if not isinstance(assets, list):
        raise ConfigError("execution allowlist assets must be an array")
    copied = _jsonable(scope)
    networks = {item["chain_id"]: item for item in copied["catalog"]["networks"]}
    for asset in assets:
        if not isinstance(asset, dict):
            raise ConfigError("execution allowlist asset must be an object")
        chain_id, address = asset.get("chain_id"), asset.get("asset_id")
        if address == "native":
            continue
        if not isinstance(chain_id, int) or not isinstance(address, str):
            raise ConfigError("execution allowlist asset has invalid chain or address")
        network = networks.get(chain_id)
        if network is None:
            raise ConfigError(f"execution allowlist chain {chain_id} is absent from catalog")
        address = address.lower()
        if any(token["address"].lower() == address for token in network["tokens"]):
            continue
        network["tokens"].append(
            {"address": address, "symbol": str(asset.get("symbol", address)), "source": str(path)}
        )
    return copied


def snapshot_hash(value: Mapping[str, Any], wallets: Any = None, settings: Any = None) -> str:
    """Hash a canonical snapshot; the optional arguments support the 3-value shorthand."""

    if wallets is not None or settings is not None:
        if not isinstance(value, Catalog):
            raise TypeError("snapshot_hash shorthand requires a Catalog as its first argument")
        value = snapshot(value, wallets, settings)
    canonical = json.dumps(
        _jsonable(value), ensure_ascii=False, sort_keys=True, separators=(",", ":")
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def format_amount(raw: int, decimals: int | None) -> str | None:
    """Format an integer balance exactly, preserving the configured decimal places."""

    if decimals is None:
        return None
    if isinstance(raw, bool) or not isinstance(raw, int):
        raise TypeError("raw balance must be an integer")
    if isinstance(decimals, bool) or not isinstance(decimals, int) or not 0 <= decimals <= 255:
        raise ValueError("decimals must be an integer from 0 through 255 or null")
    with localcontext() as context:
        context.prec = 400
        amount = Decimal(raw).scaleb(-decimals)
        return format(amount, "f")
