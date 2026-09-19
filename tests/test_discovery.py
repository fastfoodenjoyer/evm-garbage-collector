import json

import httpx
import pytest

from evm_inventory.discovery import Discovery
from evm_inventory.transport import RequestError, Transport


def test_page_and_address_validation():
    wallet = "0x" + "1" * 40
    token = "0x" + "2" * 40

    def handler(req):
        payload = json.loads(req.content)
        assert payload["addresses"][0]["networks"] == ["eth-mainnet"]
        assert payload["withMetadata"] is True
        assert payload["withPrices"] is True
        return httpx.Response(
            200,
            json={
                "data": {
                    "tokens": [
                        {
                            "address": wallet,
                            "network": "eth-mainnet",
                            "tokenAddress": token,
                            "tokenBalance": "0x2",
                            "tokenMetadata": {"name": "USD Coin", "symbol": "USDC", "decimals": 6},
                            "tokenPrices": [
                                {
                                    "currency": "usd",
                                    "value": "1",
                                    "lastUpdatedAt": "2026-09-19T00:00:00Z",
                                }
                            ],
                        }
                    ]
                },
                "pageKey": "next",
            },
        )

    d = Discovery(
        Transport(client=httpx.Client(transport=httpx.MockTransport(handler)), interval=0),
        key="test-key",
    )
    tokens, cursor = d.page(wallet, "eth-mainnet")
    assert tokens[0]["address"] == token and cursor == "next"
    assert tokens[0]["name"] == "USD Coin"
    assert tokens[0]["symbol"] == "USDC"
    assert tokens[0]["decimals"] == 6
    assert tokens[0]["price_usd"] == "1"


def test_metadata_errors_do_not_drop_a_valid_balance():
    wallet = "0x" + "1" * 40
    token = "0x" + "2" * 40

    def handler(_req):
        return httpx.Response(
            200,
            json={
                "data": {
                    "tokens": [
                        {
                            "address": wallet,
                            "network": "base-mainnet",
                            "tokenAddress": token,
                            "tokenBalance": "0x7",
                            "tokenMetadata": {"name": 7, "symbol": None, "decimals": "bad"},
                            "tokenPrices": [{"currency": "usd", "value": "not-a-number"}],
                        }
                    ]
                }
            },
        )

    t = Transport(client=httpx.Client(transport=httpx.MockTransport(handler)), interval=0)
    tokens, _ = Discovery(t, key="test").page(wallet, "base-mainnet")
    assert tokens[0]["reported_raw_balance"] == "7"
    assert tokens[0]["name"] is None
    assert tokens[0]["symbol"] == token.lower()
    assert tokens[0]["decimals"] is None
    assert tokens[0]["price_usd"] is None


def test_partial_errors_not_empty_success():
    def handler(req):
        return httpx.Response(
            200,
            json={"data": {"tokens": []}, "error": {"partialErrors": [{"network": "eth-mainnet"}]}},
        )

    d = Discovery(
        Transport(client=httpx.Client(transport=httpx.MockTransport(handler)), interval=0),
        key="key",
    )
    with pytest.raises(RequestError, match="discovery_partial"):
        d.page("0x" + "1" * 40, "eth-mainnet")


def test_no_key_does_not_request():
    d = Discovery(None, key="")
    assert not d.enabled


def test_nested_pagination_and_zero_filter():
    wallet = "0x" + "1" * 40
    contract = "0x" + "2" * 40

    def handler(req):
        payload = json.loads(req.content)
        assert payload.get("pageKey") == "previous"
        return httpx.Response(
            200,
            json={
                "data": {
                    "tokens": [
                        {
                            "address": wallet,
                            "network": "base-mainnet",
                            "tokenAddress": contract,
                            "tokenBalance": "0x0",
                        }
                    ],
                    "pageKey": "next-page",
                }
            },
        )

    t = Transport(client=httpx.Client(transport=httpx.MockTransport(handler)), interval=0)
    tokens, cursor = Discovery(t, key="test").page(wallet, "base-mainnet", "previous")
    assert tokens == []
    assert cursor == "next-page"


def test_invalid_balance_does_not_become_zero():
    wallet = "0x" + "1" * 40

    def handler(req):
        return httpx.Response(
            200,
            json={
                "data": {
                    "tokens": [
                        {
                            "address": wallet,
                            "network": "base-mainnet",
                            "tokenAddress": "0x" + "2" * 40,
                            "tokenBalance": None,
                        }
                    ]
                }
            },
        )

    t = Transport(client=httpx.Client(transport=httpx.MockTransport(handler)), interval=0)
    with pytest.raises(RequestError, match="discovery_invalid_balance"):
        Discovery(t, key="test").page(wallet, "base-mainnet")


def test_rate_limit_pauses_provider_for_following_wallets():
    calls = []

    def handler(req):
        calls.append(1)
        return httpx.Response(429)

    t = Transport(
        client=httpx.Client(transport=httpx.MockTransport(handler)),
        interval=0,
        sleep=lambda _: None,
    )
    discovery = Discovery(t, key="test")
    for _ in range(2):
        with pytest.raises(RequestError) as error:
            discovery.page("0x" + "1" * 40, "base-mainnet")
        assert error.value.retry_after is not None
    assert len(calls) == 3
