import json

import httpx
import pytest

from evm_inventory.executor import ExecutionRpc
from evm_inventory.rpc import RpcError, RpcReader, balance_of_data
from evm_inventory.transport import RequestError, Transport


def reader(handler):
    return RpcReader(
        Transport(
            client=httpx.Client(transport=httpx.MockTransport(handler)),
            interval=0,
            sleep=lambda _: None,
        )
    )


def test_native_zero_and_exact_token():
    def handle(req):
        p = json.loads(req.content)
        m = p["method"]
        result = {"eth_chainId": "0xa", "eth_getBalance": "0x0", "eth_getCode": "0x6000"}.get(m)
        if m == "eth_call":
            result = "0x" + format(
                6 if p["params"][0]["data"] == "0x313ce567" else 2**256 - 1, "064x"
            )
        return httpx.Response(200, json={"jsonrpc": "2.0", "id": p["id"], "result": result})

    r = reader(handle)
    assert r.native("https://rpc.test", 10, "0x" + "1" * 40, 123) == 0
    assert r.token("https://rpc.test", 10, "0x" + "1" * 40, "0x" + "2" * 40, 123, 6) == (
        2**256 - 1,
        6,
    )
    assert len(balance_of_data("0x" + "1" * 40)) == 74


def test_wrong_chain_and_absent_code_are_errors():
    def handle(req):
        p = json.loads(req.content)
        return httpx.Response(200, json={"jsonrpc": "2.0", "id": p["id"], "result": "0x0"})

    r = reader(handle)
    with pytest.raises(RpcError, match="wrong_chain"):
        r.native("https://rpc.test", 10, "0x" + "1" * 40, 2)


def test_execution_rpc_error_retains_provider_payload_and_method():
    def handler(req):
        payload = json.loads(req.content)
        return httpx.Response(200, json={
            "jsonrpc": "2.0", "id": payload["id"],
            "error": {"code": -32000, "message": "insufficient funds for gas"},
        })

    rpc = ExecutionRpc(Transport(
        client=httpx.Client(transport=httpx.MockTransport(handler)),
        interval=0, sleep=lambda _: None,
    ))
    with pytest.raises(RpcError, match="rpc_error") as err:
        rpc.call("https://rpc.test", "eth_estimateGas", [{"to": "0x" + "1" * 40}])
    assert err.value.diagnostic["method"] == "eth_estimateGas"
    assert err.value.diagnostic["provider_error"] == {
        "code": -32000, "message": "insufficient funds for gas",
    }


def test_malformed_abi_not_zero():
    def handle(req):
        p = json.loads(req.content)
        result = {"eth_chainId": "0xa", "eth_getCode": "0x6000", "eth_call": "0x"}.get(p["method"])
        return httpx.Response(200, json={"jsonrpc": "2.0", "id": p["id"], "result": result})

    with pytest.raises(RpcError):
        reader(handle).token("https://rpc.test", 10, "0x" + "1" * 40, "0x" + "2" * 40, 1, 6)


def test_retries_and_redacts_secrets():
    calls = []

    def handler(req):
        calls.append(1)
        return httpx.Response(429, headers={"Retry-After": "0"})

    t = Transport(
        client=httpx.Client(transport=httpx.MockTransport(handler)),
        interval=0,
        sleep=lambda _: None,
    )
    with pytest.raises(RequestError) as err:
        t.post("https://rpc.test/secret", {})
    assert len(calls) == 3
    assert [item["http_status"] for item in err.value.diagnostic["attempts"]] == [
        429, 429, 429,
    ]


def test_http_failure_retains_provider_response_for_diagnostics():
    def handler(req):
        return httpx.Response(
            403, headers={"X-Request-Id": "provider-123"},
            text='{"error":"quota exhausted","retry_after":3600}',
        )

    transport = Transport(
        client=httpx.Client(transport=httpx.MockTransport(handler)), interval=0,
    )
    with pytest.raises(RequestError, match="provider_access_denied") as err:
        transport.post("https://rpc.test", {"method": "eth_estimateGas"})
    assert err.value.diagnostic["rpc_method"] == "eth_estimateGas"
    attempt = err.value.diagnostic["attempts"][0]
    assert attempt["http_status"] == 403
    assert attempt["response_headers"]["x-request-id"] == "provider-123"
    assert attempt["response_body"] == '{"error":"quota exhausted","retry_after":3600}'
    assert "secret" not in str(err.value)


def test_http_timeout_retries_success():
    calls = []

    def handler(req):
        calls.append(1)
        if len(calls) == 1:
            return httpx.Response(408)
        return httpx.Response(200, json={"ok": True})

    t = Transport(
        client=httpx.Client(transport=httpx.MockTransport(handler)),
        interval=0,
        sleep=lambda _: None,
    )
    assert t.post("https://rpc.test", {}) == {"ok": True}
    assert len(calls) == 2


def test_bad_expanded_url_sanitised(monkeypatch):
    monkeypatch.setenv("RPC_URL", "https://[secret")
    t = Transport(interval=0)
    with pytest.raises(RequestError, match="invalid_rpc_url"):
        t.post("${RPC_URL}", {})
