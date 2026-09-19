import json

import httpx
import pytest

from evm_inventory.discovery import Discovery
from evm_inventory.transport import RequestError, Transport


def test_page_and_address_validation():
    wallet = "0x" + "1" * 40
    token = "0x" + "2" * 40

    def handler(req):
        assert json.loads(req.content)["addresses"][0]["networks"] == ["eth-mainnet"]
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
