import pytest

from evm_inventory.config import load_catalog
from evm_inventory.models import Token
from scripts.update_registry import build_candidate


def test_packaged_catalog_has_working_core_portfolio_mappings():
    mappings = {n.chain_id: n.alchemy_network for n in load_catalog().networks}
    assert mappings[1] == "eth-mainnet"
    assert mappings[10] == "opt-mainnet"
    assert mappings[42161] == "arb-mainnet"
    assert mappings[8453] == "base-mainnet"
    assert mappings[81457] == "blast-mainnet"
    assert mappings[60808] is None  # Provider rejects BOB despite generic feature matrix.


def test_packaged_catalog_has_verified_bnb_and_polygon_stablecoin_metadata():
    networks = {network.chain_id: network for network in load_catalog().networks}
    expected = {
        56: {
            "name": "BNB Chain",
            "rpc_urls": ("https://bsc-dataseed.bnbchain.org",),
            "native_symbol": "BNB",
            "tokens": (
                (
                    "0x8ac76a51cc950d9822d68b83fe1ad97b32cd580d",
                    "USDC",
                    18,
                    "Binance-Peg USD Coin",
                    "https://bscscan.com/token/0x8ac76a51cc950d9822d68b83fe1ad97b32cd580d",
                ),
                (
                    "0x55d398326f99059ff775485246999027b3197955",
                    "USDT",
                    18,
                    "Binance-Peg BSC-USD",
                    "https://bscscan.com/token/0x55d398326f99059ff775485246999027b3197955",
                ),
                (
                    "0x1af3f329e8be154074d8769d1ffa4ee058b1dbc3",
                    "DAI",
                    18,
                    "Binance-Peg Dai Token",
                    "https://bscscan.com/token/0x1af3f329e8be154074d8769d1ffa4ee058b1dbc3",
                ),
            ),
        },
        137: {
            "name": "Polygon PoS",
            "rpc_urls": ("https://polygon-rpc.com",),
            "native_symbol": "POL",
            "tokens": (
                (
                    "0x3c499c542cef5e3811e1192ce70d8cc03d5c3359",
                    "USDC",
                    6,
                    "Circle native USDC",
                    "https://developers.circle.com/stablecoins/usdc-contract-addresses",
                ),
                (
                    "0x2791bca1f2de4661ed88a30c99a7a9449aa84174",
                    "USDC.e",
                    6,
                    "Polygon PoS bridged USDC",
                    "https://polygonscan.com/token/0x2791bca1f2de4661ed88a30c99a7a9449aa84174",
                ),
                (
                    "0xc2132d05d31c914a87c6611c10748aeb04b58e8f",
                    "USDT0",
                    6,
                    "Tether USD (USDT0)",
                    "https://tether.to/en/tether-token-usdt-launches-on-polygon/",
                ),
                (
                    "0x8f3cf7ad23cd3cadbd9735aff958023239c6a063",
                    "DAI",
                    18,
                    "PoS bridged Dai Stablecoin",
                    "https://polygonscan.com/token/0x8f3cf7ad23cd3cadbd9735aff958023239c6a063",
                ),
            ),
        },
    }

    for chain_id, metadata in expected.items():
        network = networks[chain_id]
        assert network.name == metadata["name"]
        assert network.rpc_urls == metadata["rpc_urls"]
        assert network.native_symbol == metadata["native_symbol"]
        assert network.native_decimals == 18
        assert network.alchemy_network is None
        assert network.token_review_status == "verified"
        assert (
            tuple(
                (token.address, token.symbol, token.decimals, token.variant, token.source)
                for token in network.tokens
            )
            == metadata["tokens"]
        )
        assert all(
            isinstance(token, Token) and token.checked_at == "2026-09-21"
            for token in network.tokens
        )


def test_mainnets_dedup_and_preserve_tokens():
    rows = [
        {
            "identifier": "mainnet/op",
            "chainId": 10,
            "name": "OP",
            "rpc": ["https://mainnet.optimism.io"],
        },
        {"identifier": "sepolia/op", "chainId": 11155420, "name": "OP test", "rpc": []},
        {
            "identifier": "mainnet/base",
            "chainId": 8453,
            "name": "Base",
            "rpc": ["https://mainnet.base.org"],
        },
    ]
    old = {
        "networks": [
            {"chain_id": 10, "tokens": [{"address": "a"}], "token_review_status": "verified"}
        ]
    }
    got = build_candidate(rows, "a" * 40, "2026-09-19", old)
    ids = [n["chain_id"] for n in got["networks"]]
    assert sorted(ids) == [1, 10, 8453, 42161, 81457]
    assert next(n for n in got["networks"] if n["chain_id"] == 10)["tokens"] == [{"address": "a"}]
    assert (
        next(n for n in got["networks"] if n["chain_id"] == 8453)["token_review_status"]
        == "pending"
    )


def test_registry_rejects_bad_rows():
    with pytest.raises(ValueError):
        build_candidate([{"name": "bad"}], "a" * 40, "2026-09-19", {})


def test_no_arbitrary_native_eth_for_custom_gas():
    row = {
        "identifier": "mainnet/custom",
        "chainId": 55,
        "name": "Custom",
        "rpc": [],
        "gasPayingToken": "0x" + "1" * 40,
    }
    got = build_candidate([row], "a" * 40, "2026-09-19", {})
    assert next(n for n in got["networks"] if n["chain_id"] == 55)["native_symbol"] == "UNKNOWN"
