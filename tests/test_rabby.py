import httpx
import pytest

from evm_inventory.rabby import RabbyClient, encode_action

WALLET = "0x" + "1" * 40
ROUTER = "0x" + "2" * 40
TOKEN = "0x" + "3" * 40
FUEL = "0x19b5cc75846bf6286d599ec116536a333c4c2c14"


def test_client_fetches_positions_and_chain_ids():
    def respond(request):
        if request.url.path == "/v1/chain/list":
            return httpx.Response(200, json=[{"id": "eth", "community_id": 1}])
        assert request.url.params["id"] == WALLET
        return httpx.Response(200, json=[{"id": "aave3", "portfolio_item_list": []}])

    client = RabbyClient(httpx.Client(transport=httpx.MockTransport(respond)))
    assert client.chain_ids() == {"eth": 1}
    assert client.positions(WALLET)[0]["id"] == "aave3"


def test_client_rejects_malformed_payload():
    client = RabbyClient(
        httpx.Client(transport=httpx.MockTransport(lambda _: httpx.Response(200, json={})))
    )
    with pytest.raises(ValueError, match="Rabby"):
        client.positions(WALLET)


@pytest.mark.parametrize("endpoint", ["chain_ids", "positions"])
def test_rabby_retries_three_429_cycles_with_exponential_waits(endpoint, capsys):
    calls = []
    waits = []

    def respond(request):
        calls.append(request.url.path)
        if len(calls) <= 3:
            return httpx.Response(429, json={"message": "too many requests"})
        if request.url.path == "/v1/chain/list":
            return httpx.Response(200, json=[{"id": "eth", "community_id": 1}])
        return httpx.Response(200, json=[])

    client = RabbyClient(
        httpx.Client(transport=httpx.MockTransport(respond)), sleep=waits.append,
    )
    result = client.chain_ids() if endpoint == "chain_ids" else client.positions(WALLET)

    assert result == ({"eth": 1} if endpoint == "chain_ids" else [])
    assert len(calls) == 4
    assert waits == [5, 10, 20]
    assert capsys.readouterr().err.count('"event": "rabby_rate_limited"') == 3


def test_rabby_honors_retry_after_and_keeps_all_failed_attempts():
    calls = []
    waits = []

    def respond(request):
        calls.append(request)
        return httpx.Response(
            429, headers={"Retry-After": "30"}, json={"message": "too many requests"},
        )

    client = RabbyClient(
        httpx.Client(transport=httpx.MockTransport(respond)), sleep=waits.append,
    )
    with pytest.raises(httpx.HTTPStatusError) as error:
        client.chain_ids()

    assert len(calls) == 4
    assert waits == [30, 60, 120]
    assert len(error.value.diagnostic["attempts"]) == 4
    assert all(attempt["response_body"] == '{"message":"too many requests"}'
               for attempt in error.value.diagnostic["attempts"])


def test_rabby_does_not_retry_non_429_errors():
    waits = []
    calls = []

    def respond(request):
        calls.append(request)
        return httpx.Response(403, json={"message": "forbidden"})

    client = RabbyClient(
        httpx.Client(transport=httpx.MockTransport(respond)), sleep=waits.append,
    )
    with pytest.raises(httpx.HTTPStatusError):
        client.positions(WALLET)
    assert len(calls) == 1
    assert waits == []


def test_encode_withdraw_uses_first_abi_signature_and_wallet_recipient():
    action = {
        "type": "withdraw",
        "contract_id": ROUTER,
        "func": "withdraw(uint256,address)(uint256)",
        "str_params": ["123", WALLET],
        "need_approve": {},
    }
    call = encode_action(action, wallet=WALLET)
    assert call.to == ROUTER
    assert len(call.data) == 2 + 8 + 2 * 64
    assert call.data.endswith(WALLET[2:].lower())
    assert call.value == 0


def test_encode_remove_liquidity_requires_nonzero_minimums():
    action = {
        "type": "withdraw",
        "contract_id": ROUTER,
        "func": (
            "removeLiquidity(address,address,uint256,uint256,uint256,address,uint256)"
            "(uint256,uint256)"
        ),
        "str_params": [TOKEN, ROUTER, "10", "0", "1", WALLET, "9999999999"],
        "need_approve": {},
    }
    with pytest.raises(ValueError, match="minimum"):
        encode_action(action, wallet=WALLET)


def test_encode_rejects_other_recipient_and_arbitrary_method():
    base = {
        "type": "withdraw",
        "contract_id": ROUTER,
        "func": "withdraw(uint256,address)",
        "str_params": ["123", TOKEN],
        "need_approve": {},
    }
    with pytest.raises(ValueError, match="recipient"):
        encode_action(base, wallet=WALLET)
    with pytest.raises(ValueError, match="method"):
        encode_action({**base, "func": "transfer(address,uint256)"}, wallet=WALLET)


def test_encode_rejects_spender_different_from_call_target():
    action = {
        "type": "withdraw",
        "contract_id": ROUTER,
        "func": "redeem(uint256)",
        "str_params": ["10"],
        "need_approve": {"token_id": TOKEN, "to": WALLET, "str_raw_amount": "10"},
    }
    with pytest.raises(ValueError, match="spender"):
        encode_action(action, wallet=WALLET)


def test_fuel_native_withdraw_is_restricted_to_its_contract_token_and_owner():
    action = {
        "type": "withdraw", "contract_id": FUEL,
        "func": "withdraw(address,address,uint240)()",
        "str_params": ["0x" + "0" * 40, WALLET, "1023000000000000"],
    }
    encoded = encode_action(action, wallet=WALLET)
    assert encoded.to == FUEL
    assert encoded.data.startswith("0x7bdbd122")
    assert encoded.data.endswith(f"{1023000000000000:064x}")
    for unsafe in (
        {**action, "contract_id": ROUTER},
        {**action, "str_params": [TOKEN, WALLET, "1023000000000000"]},
        {**action, "str_params": ["0x" + "0" * 40, ROUTER, "1023000000000000"]},
    ):
        with pytest.raises(ValueError):
            encode_action(unsafe, wallet=WALLET)
