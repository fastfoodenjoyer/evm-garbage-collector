"""Immutable input models used by the inventory pipeline."""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date
from typing import ClassVar
from urllib.parse import urlsplit


class ConfigError(ValueError):
    """Raised when user supplied inventory configuration is invalid."""

    def __init__(self, message: str, *, issues: tuple[str, ...] = ()) -> None:
        super().__init__(message)
        self.issues = issues or (message,)


_ADDRESS_RE = re.compile(r"^0x[0-9a-fA-F]{40}$")
_ENV_RE = re.compile(r"^\$\{[A-Za-z_][A-Za-z0-9_]*\}$")
_RPC_SCHEMES = frozenset({"http", "https"})
_TOKEN_REVIEW_STATUSES = frozenset({"verified", "pending", "none"})


def validate_rpc_url(url: str) -> None:
    """Validate an HTTP(S) RPC endpoint without expanding environment values."""

    if not isinstance(url, str) or not url.strip():
        raise ConfigError("RPC URL must be a non-empty string")
    value = url.strip()
    if _ENV_RE.fullmatch(value):
        return
    try:
        parsed = urlsplit(value)
    except ValueError as exc:
        raise ConfigError("invalid RPC URL") from exc
    if parsed.scheme.lower() not in _RPC_SCHEMES or not parsed.netloc:
        raise ConfigError("RPC URL must use http(s)")
    try:
        hostname = parsed.hostname
        _ = parsed.port
    except ValueError as exc:
        raise ConfigError("invalid RPC URL") from exc
    if not hostname:
        raise ConfigError("RPC URL must include a host")
    if parsed.username is not None and not _ENV_RE.fullmatch(parsed.username):
        raise ConfigError("RPC URL credentials must be environment placeholders")
    if parsed.password is not None and not _ENV_RE.fullmatch(parsed.password):
        raise ConfigError("RPC URL credentials must be environment placeholders")


def _validate_decimals(value: object, field: str = "decimals") -> None:
    if value is not None and (
        isinstance(value, bool) or not isinstance(value, int) or not 0 <= value <= 255
    ):
        raise ConfigError(f"{field} must be an integer from 0 through 255 or null")


def _validate_provenance(source: str, checked_at: str) -> None:
    try:
        parsed = urlsplit(source)
    except ValueError as exc:
        raise ConfigError("token source must be a valid HTTP(S) URL") from exc
    if parsed.scheme.lower() not in _RPC_SCHEMES or not parsed.netloc:
        raise ConfigError("token source must be a valid HTTP(S) URL")
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", checked_at):
        raise ConfigError("token checked_at must be an ISO date (YYYY-MM-DD)")
    try:
        date.fromisoformat(checked_at)
    except ValueError as exc:
        raise ConfigError("token checked_at must be an ISO date (YYYY-MM-DD)") from exc


@dataclass(frozen=True, slots=True)
class Token:
    address: str
    symbol: str
    decimals: int | None
    source: str
    checked_at: str
    variant: str = ""

    _address_re: ClassVar[re.Pattern[str]] = _ADDRESS_RE

    def __post_init__(self) -> None:
        if not isinstance(self.address, str) or not _ADDRESS_RE.fullmatch(self.address):
            raise ConfigError(
                f"token address must be 0x followed by 40 hex characters: {self.address!r}"
            )
        object.__setattr__(self, "address", self.address.lower())
        if not isinstance(self.symbol, str) or not self.symbol.strip():
            raise ConfigError("token symbol is required")
        if not isinstance(self.source, str) or not self.source.strip():
            raise ConfigError("token source provenance is required")
        if not isinstance(self.checked_at, str) or not self.checked_at.strip():
            raise ConfigError("token checked_at is required")
        if not isinstance(self.variant, str):
            raise ConfigError("token variant must be a string")
        _validate_provenance(self.source, self.checked_at)
        _validate_decimals(self.decimals)


@dataclass(frozen=True, slots=True)
class Network:
    chain_id: int
    name: str
    rpc_urls: tuple[str, ...]
    native_symbol: str
    native_decimals: int
    tokens: tuple[Token, ...]
    token_review_status: str = "pending"
    notes: str = ""
    alchemy_network: str | None = None

    def __post_init__(self) -> None:
        if (
            isinstance(self.chain_id, bool)
            or not isinstance(self.chain_id, int)
            or self.chain_id <= 0
        ):
            raise ConfigError("chain_id must be a positive integer")
        for field, value in (("name", self.name), ("native_symbol", self.native_symbol)):
            if not isinstance(value, str) or not value.strip():
                raise ConfigError(f"network {field} is required")
        _validate_decimals(self.native_decimals, "native_decimals")
        if self.native_decimals is None:
            raise ConfigError("native_decimals must be an integer from 0 through 255")
        if not isinstance(self.rpc_urls, tuple):
            object.__setattr__(self, "rpc_urls", tuple(self.rpc_urls))
        if not isinstance(self.tokens, tuple):
            object.__setattr__(self, "tokens", tuple(self.tokens))
        if any(not isinstance(token, Token) for token in self.tokens):
            raise ConfigError("network tokens must contain Token instances")
        if any(token.decimals is None for token in self.tokens):
            raise ConfigError("token decimals are required for curated network tokens")
        if self.token_review_status not in _TOKEN_REVIEW_STATUSES:
            raise ConfigError("token_review_status must be one of: verified, pending, none")
        if not isinstance(self.notes, str):
            raise ConfigError("network notes must be a string")
        if self.token_review_status == "none" and self.tokens:
            raise ConfigError("token_review_status none requires an empty token list")
        if self.alchemy_network is not None and not isinstance(self.alchemy_network, str):
            raise ConfigError("alchemy_network must be a string or null")

    def validate(self) -> Network:
        issues: list[str] = []
        for index, url in enumerate(self.rpc_urls, start=1):
            try:
                validate_rpc_url(url)
            except ConfigError as exc:
                issues.append(f"network {self.chain_id} RPC URL {index}: {exc}")
        seen: set[str] = set()
        for token in self.tokens:
            normalized = token.address.lower()
            if normalized in seen:
                issues.append(f"network {self.chain_id} duplicate token {normalized}")
            seen.add(normalized)
        if issues:
            raise ConfigError("; ".join(issues), issues=tuple(issues))
        return self


@dataclass(frozen=True, slots=True)
class Catalog:
    networks: tuple[Network, ...]
    revision: str
    checked_at: str
    source: str

    def __post_init__(self) -> None:
        if not isinstance(self.networks, tuple):
            object.__setattr__(self, "networks", tuple(self.networks))
        if any(not isinstance(network, Network) for network in self.networks):
            raise ConfigError("catalog networks must contain Network instances")
        for field, value in (
            ("revision", self.revision),
            ("checked_at", self.checked_at),
            ("source", self.source),
        ):
            if not isinstance(value, str) or not value.strip():
                raise ConfigError(f"catalog {field} is required")

    def validate(self) -> Catalog:
        issues: list[str] = []
        seen_chains: set[int] = set()
        for network in self.networks:
            if network.chain_id in seen_chains:
                issues.append(f"duplicate chain ID {network.chain_id}")
            seen_chains.add(network.chain_id)
            try:
                network.validate()
            except ConfigError as exc:
                issues.extend(exc.issues)
        if issues:
            raise ConfigError("; ".join(issues), issues=tuple(issues))
        return self
