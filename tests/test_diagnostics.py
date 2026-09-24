from evm_inventory.diagnostics import exception_diagnostic
from evm_inventory.rpc import RpcError


def test_provider_diagnostic_preserves_cause_and_masks_credentials():
    proxy = "socks5://alice:password@proxy.example:1080"
    private_key = "0x" + "a" * 64
    try:
        raise RpcError("rpc_error", diagnostic={
            "method": "eth_estimateGas",
            "provider_error": {
                "code": -32000,
                "message": f"insufficient funds; proxy={proxy}",
                "private_key": private_key,
            },
            "response_headers": {"x-api-key": "api-secret-value"},
        })
    except RpcError as exc:
        diagnostic = exception_diagnostic(exc)

    provider = diagnostic["exceptions"][0]["provider"]
    assert provider["method"] == "eth_estimateGas"
    assert provider["provider_error"]["code"] == -32000
    assert "insufficient funds" in provider["provider_error"]["message"]
    assert "[REDACTED_URL]" in provider["provider_error"]["message"]
    assert provider["provider_error"]["private_key"] == "[REDACTED_SECRET]"
    assert provider["response_headers"]["x-api-key"] == "[REDACTED_SECRET]"
    assert proxy not in str(diagnostic)
    assert private_key not in str(diagnostic)
