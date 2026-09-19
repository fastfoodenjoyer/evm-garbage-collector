"""Contract tests for the immutable input and snapshot helpers."""

from __future__ import annotations

import json
from dataclasses import asdict
from pathlib import Path

import pytest

from evm_inventory.config import (
    ConfigError,
    format_amount,
    load_catalog,
    load_wallets,
    snapshot,
    snapshot_hash,
    validate_delays,
)
from evm_inventory.models import Catalog, Network, Token


def _token(address: str = "0x" + "a" * 40, *, decimals: int | None = 6) -> Token:
    return Token(
        address=address,
        symbol="USD",
        decimals=decimals,
        source="https://issuer.example/tokens",
        checked_at="2026-09-19",
    )


def _network(
    chain_id: int = 1,
    *,
    tokens: tuple[Token, ...] = (),
    rpc_urls: tuple[str, ...] = ("https://rpc.example",),
    status: str = "verified",
) -> Network:
    return Network(
        chain_id=chain_id,
        name="Example",
        rpc_urls=rpc_urls,
        native_symbol="ETH",
        native_decimals=18,
        tokens=tokens,
        token_review_status=status,
    )


def _catalog(*networks: Network) -> Catalog:
    return Catalog(
        networks=tuple(networks),
        revision="abc123",
        checked_at="2026-09-19",
        source="https://registry.example/revision/abc123",
    )


def test_load_wallets_strips_blanks_and_normalizes_duplicate_addresses(tmp_path: Path) -> None:
    path = tmp_path / "wallets.txt"
    path.write_text(
        "\n  0x" + "A" * 40 + "  \n0x" + "a" * 40 + "\n0x" + "b" * 40 + "\n",
        encoding="utf-8",
    )

    assert load_wallets(path) == ("0x" + "a" * 40, "0x" + "b" * 40)


def test_load_wallets_reports_all_invalid_lines_with_line_numbers(tmp_path: Path) -> None:
    path = tmp_path / "wallets.txt"
    path.write_text("bad\n0x123\n0x" + "g" * 40 + "\n", encoding="utf-8")

    with pytest.raises(ConfigError) as raised:
        load_wallets(path)

    message = str(raised.value)
    assert "line 1" in message
    assert "line 2" in message
    assert "line 3" in message


def test_load_wallets_rejects_empty_input(tmp_path: Path) -> None:
    path = tmp_path / "wallets.txt"
    path.write_text("\n  \n", encoding="utf-8")

    with pytest.raises(ConfigError, match="at least one wallet"):
        load_wallets(path)


def test_catalog_rejects_duplicate_chain_ids_and_token_addresses() -> None:
    duplicate_chain = _catalog(_network(), _network())
    with pytest.raises(ConfigError, match="duplicate chain ID"):
        duplicate_chain.validate()

    duplicate_token = _catalog(_network(tokens=(_token(), _token("0x" + "A" * 40))))
    with pytest.raises(ConfigError, match="duplicate token"):
        duplicate_token.validate()


@pytest.mark.parametrize("decimals", [-1, 256, True, 1.5])
def test_catalog_rejects_invalid_decimals(decimals: object) -> None:
    with pytest.raises(ConfigError, match="decimals"):
        _token(decimals=decimals)  # type: ignore[arg-type]


def test_catalog_requires_token_provenance_and_status() -> None:
    with pytest.raises(ConfigError, match="source"):
        Token(
            address="0x" + "a" * 40,
            symbol="USD",
            decimals=6,
            source="",
            checked_at="2026-09-19",
        )

    with pytest.raises(ConfigError, match="token_review_status"):
        _catalog(_network(status="unknown")).validate()


def test_catalog_rejects_malformed_rpc_url_and_literal_credentials() -> None:
    with pytest.raises(ConfigError, match="RPC URL"):
        _catalog(_network(rpc_urls=("ftp://rpc.example",))).validate()
    with pytest.raises(ConfigError, match="credential"):
        _catalog(_network(rpc_urls=("https://user:secret@rpc.example",))).validate()


def test_unavailable_networks_may_have_no_rpc_urls() -> None:
    assert _network(rpc_urls=()).validate().rpc_urls == ()


def test_network_rejects_unknown_curated_token_decimals() -> None:
    with pytest.raises(ConfigError, match="token decimals"):
        _network(tokens=(_token(decimals=None),))


def test_token_provenance_requires_http_url_and_iso_date() -> None:
    with pytest.raises(ConfigError, match="source"):
        Token(
            address="0x" + "a" * 40,
            symbol="USD",
            decimals=6,
            source="issuer.example/token",
            checked_at="2026-09-19",
        )
    with pytest.raises(ConfigError, match="checked_at"):
        Token(
            address="0x" + "a" * 40,
            symbol="USD",
            decimals=6,
            source="https://issuer.example/token",
            checked_at="yesterday",
        )


def test_wallet_file_rejects_invalid_utf8(tmp_path: Path) -> None:
    path = tmp_path / "wallets.txt"
    path.write_bytes(b"\xff")
    with pytest.raises(ConfigError, match="UTF-8"):
        load_wallets(path)


@pytest.mark.parametrize(
    ("minimum", "maximum"),
    [(-1, 1), (1, -1), (2, 1), (float("inf"), 2), (0, float("nan"))],
)
def test_validate_delays_rejects_invalid_ranges(minimum: float, maximum: float) -> None:
    with pytest.raises(ConfigError):
        validate_delays(minimum, maximum)


def test_load_catalog_validates_json_schema(tmp_path: Path) -> None:
    path = tmp_path / "catalog.json"
    path.write_text(
        json.dumps(
            {
                "networks": [
                    {
                        "chain_id": 1,
                        "name": "Ethereum",
                        "rpc_urls": ["https://rpc.example/${RPC_KEY}"],
                        "native_symbol": "ETH",
                        "native_decimals": 18,
                        "tokens": [asdict(_token())],
                        "token_review_status": "verified",
                    }
                ],
                "revision": "abc123",
                "checked_at": "2026-09-19",
                "source": "https://registry.example/abc123",
            }
        ),
        encoding="utf-8",
    )

    catalog = load_catalog(path)
    assert catalog.networks[0].rpc_urls == ("https://rpc.example/${RPC_KEY}",)
    assert catalog.networks[0].tokens[0].address == "0x" + "a" * 40


def test_snapshot_is_json_friendly_and_hash_is_canonical() -> None:
    catalog = _catalog(_network(tokens=(_token(),)))
    value = snapshot(
        catalog,
        ("0x" + "B" * 40, "0x" + "b" * 40),
        {"delay_min": 1, "delay_max": 2, "interval": 0.5, "discovery_enabled": True},
    )

    assert value["wallets"] == ["0x" + "b" * 40]
    json.dumps(value)
    assert snapshot_hash(value) == snapshot_hash(
        {
            "settings": {
                "delay_min": 1,
                "delay_max": 2,
                "interval": 0.5,
                "discovery_enabled": True,
            },
            "wallets": value["wallets"],
            "catalog": value["catalog"],
        }
    )


def test_format_amount_is_exact_and_unknown_decimals_are_none() -> None:
    assert format_amount(123456789012345678901234567890, 18) == "123456789012.345678901234567890"
    assert format_amount(100, 0) == "100"
    assert format_amount(1, None) is None
