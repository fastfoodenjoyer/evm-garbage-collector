from evm_inventory.diagnostics import exception_diagnostic
from evm_inventory.rpc import RpcError


def test_http_json_diagnostic_masks_quoted_nested_secret_fields():
    import json

    import httpx

    request = httpx.Request("POST", "https://li.quest/v1/advanced/stepTransaction")
    response = httpx.Response(422, request=request, json={
        "code": "ROUTE_REJECTED", "message": "could not prepare",
        "secret": "plain-secret", "nested": {"apiKey": "plain-key",
        "passphrase": "plain-passphrase", "privateKey": "plain-private-key"},
    })
    error = httpx.HTTPStatusError("rejected", request=request, response=response)
    diagnostic = exception_diagnostic(error)
    body = json.loads(diagnostic["exceptions"][0]["http_response_body"])
    assert body["code"] == "ROUTE_REJECTED"
    assert body["message"] == "could not prepare"
    assert body["secret"] == "[REDACTED_SECRET]"
    assert all(value == "[REDACTED_SECRET]" for value in body["nested"].values())


def test_passphrase_environment_value_is_masked_in_plain_diagnostics(monkeypatch):
    from evm_inventory.diagnostics import redact_text

    monkeypatch.setenv("BITGET_PASSPHRASE", "plain-private-passphrase")
    assert "plain-private-passphrase" not in redact_text("failed: plain-private-passphrase")


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


def test_scan_keeps_provider_error_through_cooldown_and_export(tmp_path):
    import csv
    import json

    from test_scanner import RPC, scope

    from evm_inventory.report import export_current
    from evm_inventory.scanner import scan
    from evm_inventory.store import Store
    from evm_inventory.transport import RequestError

    class BrokenRPC(RPC):
        def block(self, *args):
            raise RequestError('network_error', diagnostic={
                'attempts': [{'exception_type': 'ConnectError', 'exception': 'DNS lookup failed'}]
            })

    with Store(tmp_path / 'db.sqlite') as store:
        result = scan(store, scope(), rpc=BrokenRPC())
        failure = store.assets()[0]['result']
        assert 'DNS lookup failed' in str(failure['diagnostic'])
        assert 'DNS lookup failed' in str(result['error_details'])
        export_current(store, tmp_path / 'report')
    rows = list(csv.DictReader((tmp_path / 'report/checks.csv').open()))
    assert 'DNS lookup failed' in str(json.loads(rows[0]['diagnostic']))


def test_provider_only_is_not_counted_as_failed(tmp_path):
    from test_scanner import RPC, D, Discover, scope

    from evm_inventory.report import _coverage
    from evm_inventory.scanner import scan
    from evm_inventory.store import Store

    config = scope()
    config['catalog']['networks'][0]['alchemy_network'] = 'opt-mainnet'
    discovery = Discover([([{'address': D, 'decimals': 6, 'reported_raw_balance': '10'}], None)])
    with Store(tmp_path / 'db.sqlite') as store:
        result = scan(store, config, rpc=RPC(), discovery=discovery)
        assert result['discovered_failed'] == 0
        assert result['discovered_provider_only'] == 1
        assets = store.assets()
        coverage = _coverage(store, [r for r in assets if r['kind'] == 'mandatory'], assets)
        assert coverage[0]['discovered_verification_failures'] == 0
        assert coverage[0]['discovered_provider_only'] == 1
