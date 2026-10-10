import httpx
from test_live_plan import NOW, WALLET, _allowlist, _balance_rows, _RecordingQuotes

from evm_inventory.bitget_catalog import BitgetDepositTarget
from evm_inventory.lifi import LifiClient, LifiRouteRequest, _identity_from_token
from evm_inventory.live_plan import create_live_plan
from evm_inventory.models import AssetIdentity

CELO = "0x471ece3750da237f93b8e339c536989b8978a438"
ZERO = "0x" + "0" * 40


def test_native_celo_price_uses_contract_and_preserves_requested_gas_identity():
    calls = []

    def handler(req):
        calls.append(req)
        return httpx.Response(
            200, json={"address": CELO, "chainId": 42220, "decimals": 18, "priceUSD": "0.1"}
        )

    with httpx.Client(transport=httpx.MockTransport(handler)) as http:
        native = AssetIdentity(42220, "native", 18)
        price = LifiClient(http).token_price(native)
    assert calls[0].url.params["token"] == CELO
    assert price.asset == native


def test_celo_routes_translate_zero_address_to_contract():
    calls = []

    def handler(req):
        import json

        calls.append(json.loads(req.content))
        return httpx.Response(200, json={"routes": []})

    with httpx.Client(transport=httpx.MockTransport(handler)) as http:
        LifiClient(http).routes(LifiRouteRequest(42220, 42220, ZERO, ZERO, "10", WALLET, WALLET))
    assert calls[0]["fromTokenAddress"] == CELO
    assert calls[0]["toTokenAddress"] == CELO
    assert _identity_from_token(42220, {"address": ZERO, "decimals": 18}) == AssetIdentity(
        42220, CELO, 18
    )
    assert _identity_from_token(10, {"address": CELO, "decimals": 18}) == AssetIdentity(
        10, CELO, 18
    )


def celo_plan(tmp_path, values, quotes):
    rows = [
        dict(
            wallet=WALLET,
            chain_id=42220,
            asset_id=a,
            raw_balance=str(v),
            decimals=18,
            symbol="CELO",
            status="success",
        )
        for a, v in values
    ]
    return create_live_plan(
        _balance_rows(tmp_path, rows),
        deposit_addresses={WALLET: "0x" + "d" * 40},
        allowlist_path=_allowlist(tmp_path, [(42220, "native", "swap"), (42220, CELO, "swap")]),
        targets=(BitgetDepositTarget("USDC", 10, "0x" + "b" * 40, 1, decimals=6),),
        client=quotes,
        now_ms=lambda: int(NOW.timestamp() * 1000),
    )


def test_celo_dual_balances_only_quote_once(tmp_path):
    quotes = _RecordingQuotes(lambda req: ())
    p = celo_plan(tmp_path, [("native", 10**18), (CELO, 10**18)], quotes)
    assert len(p["entries"]) == 1
    assert len(quotes.requests) == 1
    assert quotes.requests[0].from_token_address == CELO
    assert p["entries"][0]["asset_id"] == CELO


def test_celo_conflicting_dual_balances_block_quote(tmp_path):
    quotes = _RecordingQuotes(lambda req: ())
    p = celo_plan(tmp_path, [("native", 10**18), (CELO, 2 * 10**18)], quotes)
    assert len(p["entries"]) == 1
    assert p["entries"][0]["reason"] == "conflicting_native_balance"
    assert quotes.requests == []


def test_unsupported_source_preserved_without_price_or_route_requests(tmp_path):
    class Quotes(_RecordingQuotes):
        def supported_chain_ids(self):
            return frozenset({10})

        def token_price(self, asset):
            raise AssertionError("unsupported source must not request prices")

    quotes = Quotes(lambda req: ())
    p = celo_plan(tmp_path, [("native", 10**18)], quotes)
    assert p["entries"][0]["reason"] == "unsupported_by_lifi"
    assert p["entries"][0]["alternative_provider_required"] is True
    assert quotes.requests == []


def test_supported_chains_parses_live_catalog_once():
    calls = []

    def handler(req):
        calls.append(req)
        return httpx.Response(
            200,
            json={"chains": [{"id": 42220, "chainType": "EVM"}, {"id": 10, "chainType": "EVM"}]},
        )

    with httpx.Client(transport=httpx.MockTransport(handler)) as http:
        client = LifiClient(http)
        assert client.supported_chain_ids() == frozenset({42220, 10})
        assert client.supported_chain_ids() == frozenset({42220, 10})
    assert len(calls) == 1


def test_zero_native_balance_conflicting_with_contract_blocks_quote(tmp_path):
    quotes = _RecordingQuotes(lambda req: ())
    plan = celo_plan(tmp_path, [("native", 0), (CELO, 10**18)], quotes)
    assert plan["entries"][0]["reason"] == "conflicting_native_balance"
    assert quotes.requests == []


def test_celo_price_rejects_wrong_chain_response():
    import pytest

    def handler(req):
        return httpx.Response(
            200, json={"address": CELO, "chainId": 10, "decimals": 18, "priceUSD": "0.1"}
        )

    with httpx.Client(transport=httpx.MockTransport(handler)) as http:
        with pytest.raises(ValueError, match="identity does not match"):
            LifiClient(http).token_price(AssetIdentity(42220, "native", 18))


def test_celo_alias_policy_review_remains_blocking(tmp_path):
    quotes = _RecordingQuotes(lambda req: ())
    balances = _balance_rows(
        tmp_path,
        [
            dict(
                wallet=WALLET,
                chain_id=42220,
                asset_id="native",
                raw_balance=str(10**18),
                decimals=18,
                symbol="CELO",
                status="success",
            )
        ],
    )
    plan = create_live_plan(
        balances,
        deposit_addresses={WALLET: "0x" + "d" * 40},
        allowlist_path=_allowlist(tmp_path, [(42220, "native", "swap"), (42220, CELO, "review")]),
        targets=(BitgetDepositTarget("USDC", 10, "0x" + "b" * 40, 1, decimals=6),),
        client=quotes,
    )
    assert plan["entries"][0]["reason"] == "policy_review"
    assert quotes.requests == []


def test_unsupported_destination_is_reported_and_not_queried(tmp_path):
    class Quotes(_RecordingQuotes):
        def supported_chain_ids(self):
            return frozenset({42220})

    quotes = Quotes(lambda req: ())
    plan = celo_plan(tmp_path, [("native", 10**18)], quotes)
    assert plan["search_scope"]["enabled_targets"] == []
    assert plan["search_scope"]["unavailable_targets"][0]["reason"] == "unsupported_by_lifi"
    assert quotes.requests == []


def test_chain_catalog_error_preserves_position_and_diagnostic(tmp_path):
    class Quotes(_RecordingQuotes):
        def supported_chain_ids(self):
            raise ValueError("malformed chain catalog")

        def token_price(self, asset):
            raise AssertionError("must not quote without known support")

    quotes = Quotes(lambda req: ())
    plan = celo_plan(tmp_path, [("native", 10**18)], quotes)
    assert plan["entries"][0]["reason"] == "lifi_chain_catalog_unavailable"
    assert plan["entries"][0]["diagnostic"]["exceptions"][0]["message"] == "malformed chain catalog"
    assert quotes.requests == []
