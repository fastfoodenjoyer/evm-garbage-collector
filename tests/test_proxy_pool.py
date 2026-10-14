import concurrent.futures
import sqlite3

import httpx
import pytest

from evm_inventory.lifi_http import LifiHttpClient
from evm_inventory.lifi_quota import LifiRequestLimit

URL = "https://li.quest/v1/advanced/routes"


def make_pool(tmp_path, handlers, clock=None, key=""):
    from evm_inventory.proxy_pool import ProxyPoolHttpClient

    clients = [
        LifiHttpClient(
            httpx.Client(transport=httpx.MockTransport(handler)),
            db_path=tmp_path / "inventory.sqlite",
            proxy=f"http://secret:pass@host{i}:1234",
            api_key=key,
            clock=clock,
            max_attempts=1,
        )
        for i, handler in enumerate(handlers)
    ]
    return ProxyPoolHttpClient(clients, wallet="0xabc")


def test_rotates_429_and_persists_cursor_and_original_cache_age(tmp_path):
    calls = []
    now = [1000.0]

    def a(request):
        calls.append("a")
        return httpx.Response(429, json={}, headers={"ratelimit-reset": "7200"})

    def b(request):
        calls.append("b")
        return httpx.Response(200, json={"routes": []})

    p = make_pool(tmp_path, [a, b], lambda: now[0])
    r = p.post(URL, json={"n": 1})
    assert calls == ["a", "b"]
    assert r.headers["x-inventory-observed-at"] == "1000.0"
    p.close()
    now[0] += 10
    p = make_pool(tmp_path, [a, b], lambda: now[0])
    assert p.post(URL, json={"n": 1}).headers["x-inventory-observed-at"] == "1000.0"
    assert calls == ["a", "b"]
    assert p.metrics["transport_index"] == 2
    p.close()


def test_exhausted_pool_is_temporary_with_earliest_reset(tmp_path):
    def a(r):
        return httpx.Response(429, json={}, headers={"ratelimit-reset": "100"})

    def b(r):
        return httpx.Response(429, json={}, headers={"ratelimit-reset": "200"})

    p = make_pool(tmp_path, [a, b], lambda: 1000.0)
    with pytest.raises(LifiRequestLimit) as caught:
        p.post(URL, json={})
    assert caught.value.reason == "proxy_pool_exhausted"
    assert caught.value.retry_at == 1100.0
    p.close()


def test_network_failure_recovers_after_cooldown_same_pool(tmp_path):
    now = [1000.0]
    broken = [True]
    calls = []

    def handler(r):
        calls.append(1)
        if broken[0]:
            raise httpx.ProxyError("credentials must not leak")
        return httpx.Response(200, json={})

    p = make_pool(tmp_path, [handler, handler], lambda: now[0])
    with pytest.raises(LifiRequestLimit):
        p.get("https://li.quest/v1/chains")
    broken[0] = False
    now[0] += 61
    assert p.get("https://li.quest/v1/chains").status_code == 200
    assert len(calls) == 3
    with sqlite3.connect(tmp_path / "inventory.sqlite") as db:
        dumped = "\n".join(db.iterdump())
        assert "secret" not in dumped
        assert "credentials must not leak" not in dumped
    p.close()


def test_no_rotation_for_409_or_external_host(tmp_path):
    calls = []

    def a(r):
        calls.append("a")
        return httpx.Response(409, json={})

    def b(r):
        calls.append("b")
        return httpx.Response(200, json={})

    p = make_pool(tmp_path, [a, b])
    assert p.post(URL, json={}).status_code == 409
    assert p.get("https://api.bitget.com/ticker").status_code == 409
    assert calls == ["a", "a"]
    p.close()


def test_parallel_identical_requests_singleflight_across_rotation(tmp_path):
    calls = []

    def a(r):
        calls.append("a")
        return httpx.Response(429, json={}, headers={"ratelimit-reset": "100"})

    def b(r):
        calls.append("b")
        return httpx.Response(200, json={"routes": []})

    p = make_pool(tmp_path, [a, b])
    with concurrent.futures.ThreadPoolExecutor(max_workers=4) as workers:
        results = list(workers.map(lambda _: p.post(URL, json={"n": 1}), range(4)))
    assert all(r.status_code == 200 for r in results)
    assert calls == ["a", "b"]
    p.close()


def test_api_key_quota_is_shared_across_transports(tmp_path):
    calls = []

    def h(r):
        calls.append(1)
        return httpx.Response(429, json={}, headers={"ratelimit-reset": "100"})

    p = make_pool(tmp_path, [h, h], key="private-key")
    with pytest.raises(LifiRequestLimit):
        p.post(URL, json={})
    assert len(calls) == 1
    p.close()


def test_available_at_respects_bucket_and_execution_reserve(tmp_path):
    def h(r):
        return httpx.Response(200, json={})

    p = make_pool(tmp_path, [h, h], lambda: 1000.0)
    with p.clients[0].quota.db() as db:
        for c in p.clients:
            db.executemany(
                "INSERT INTO lifi_requests VALUES (?,?,?)", [(c.scope, "routes", 1000.0)] * 70
            )
    assert p.available_at("routes") == 8200.0
    assert p.available_at("public") == 1000.0
    for c in p.clients:
        c.purpose = "execution"
    assert p.available_at("routes") == 1000.0
    p.close()


def test_persisted_job_wait_rechecks_new_reserve_without_resetting_quota(tmp_path):
    from evm_inventory.quote_jobs import QuoteJobs, quote_job_key

    def limited(r):
        return httpx.Response(429, json={}, headers={"ratelimit-reset": "100"})

    def good(r):
        return httpx.Response(200, json={"routes": []})

    p = make_pool(tmp_path, [limited], lambda: 1000.0)
    jobs = QuoteJobs(p.db_path, p.scope, clock=p.clock)
    key = quote_job_key(p.scope, {}, "")
    token = jobs.claim(key, {}, "")
    with pytest.raises(LifiRequestLimit) as e:
        p.post(URL, json={})
    jobs.fail(token, e.value, state="quota_wait", retry_at=e.value.retry_at)
    p.close()
    p = make_pool(tmp_path, [limited, good], lambda: 1000.0)
    jobs = QuoteJobs(p.db_path, p.scope, clock=p.clock)
    jobs.wait_recheck = p.recheck_wait
    assert jobs.claim(key, {}, "")
    assert p.post(URL, json={}).status_code == 200
    p.close()


@pytest.mark.parametrize("error_type", [httpx.WriteTimeout, httpx.RemoteProtocolError])
def test_proxy_disconnect_during_request_rotates(tmp_path, error_type):
    calls = []

    def a(r):
        calls.append("a")
        raise error_type("disconnected")

    def b(r):
        calls.append("b")
        return httpx.Response(200, json={"routes": []})

    p = make_pool(tmp_path, [a, b])
    assert p.post(URL, json={}).status_code == 200
    assert calls == ["a", "b"]
    p.close()


def test_all_5xx_dispatches_once_per_transport_even_through_lifi(tmp_path):
    from evm_inventory.lifi import LifiClient, LifiRouteRequest

    calls = []

    def h(r):
        calls.append(1)
        return httpx.Response(503, json={})

    p = make_pool(tmp_path, [h, h])
    client = LifiClient(p)
    request = LifiRouteRequest(
        10, 8453, "0x" + "a" * 40, "0x" + "b" * 40, "1000", "0x" + "1" * 40, "0x" + "1" * 40
    )
    with pytest.raises(LifiRequestLimit):
        client.routes(request)
    assert len(calls) == 2
    p.close()


def test_combined_health_and_quota_wait_uses_true_earliest_time(tmp_path):
    p = make_pool(
        tmp_path, [lambda r: httpx.Response(200), lambda r: httpx.Response(200)], lambda: 1000.0
    )
    p._cooldown(0)
    with p.clients[0].quota.db() as db:
        for c, delay in zip(p.clients, [7200.0, 100.0], strict=True):
            db.execute(
                "INSERT INTO lifi_limits(scope,bucket,blocked_until) VALUES (?,?,?)",
                (c.scope, "routes", 1000.0 + delay),
            )
    with pytest.raises(LifiRequestLimit) as error:
        p.post(URL, json={})
    assert error.value.retry_at == 1100.0
    p.close()


def test_existing_legacy_inflight_claim_is_not_dispatched_again(tmp_path):
    from evm_inventory.lifi import LifiClient, LifiRouteRequest
    from evm_inventory.quote_jobs import JobInProgress, QuoteJobs, quote_job_key

    calls = []

    def handler(r):
        calls.append(1)
        return httpx.Response(200, json={"routes": []})

    child = LifiHttpClient(
        httpx.Client(transport=httpx.MockTransport(handler)),
        db_path=tmp_path / "inventory.sqlite",
        proxy="http://host0:1234",
    )
    request = LifiRouteRequest(
        10, 8453, "0x" + "a" * 40, "0x" + "b" * 40, "1000", "0x" + "1" * 40, "0x" + "1" * 40
    )
    legacy = LifiClient(child)
    complete = {
        "endpoint": legacy.endpoint,
        "payload": {},
        "from_token_decimals": request.from_token_decimals,
        "to_token_decimals": request.to_token_decimals,
        "catalog_digest": "",
    }
    jobs = QuoteJobs(child.db_path, child.scope)
    jobs.claim(quote_job_key(child.scope, complete, ""), complete, "")
    from evm_inventory.proxy_pool import ProxyPoolHttpClient

    pool = ProxyPoolHttpClient([child], wallet=request.from_address)
    client = LifiClient(pool)
    with pytest.raises(JobInProgress):
        client._read_routes(request, {})
    assert calls == []
    pool.close()


def test_same_lifi_client_resumes_routes_after_pool_cooldown(tmp_path):
    from evm_inventory.lifi import LifiClient, LifiRouteRequest

    now = [1000.0]
    broken = [True]
    calls = []

    def handler(r):
        calls.append(1)
        if broken[0]:
            raise httpx.RemoteProtocolError("disconnected")
        return httpx.Response(200, json={"routes": []})

    p = make_pool(tmp_path, [handler, handler], lambda: now[0])
    client = LifiClient(p)
    request = LifiRouteRequest(
        10, 8453, "0x" + "a" * 40, "0x" + "b" * 40, "1000", "0x" + "1" * 40, "0x" + "1" * 40
    )
    with pytest.raises(LifiRequestLimit):
        client.routes(request)
    broken[0] = False
    now[0] += 61
    assert client.routes(request) == ()
    assert len(calls) == 3
    p.close()


def test_wallet_clients_use_only_assigned_reserve_and_relay_primary(tmp_path, monkeypatch):
    from evm_inventory.wallet_lifi import WalletLifiClients
    from evm_inventory.workbook import WalletWorkbookRow

    primary = "http://host1:1234"
    reserve = "http://host2:1234"
    other = "http://host3:1234"
    calls = []
    real_client = httpx.Client

    def factory(*, proxy, **kwargs):
        def handler(request):
            calls.append(proxy)
            if proxy == primary:
                return httpx.Response(429, json={}, headers={"ratelimit-reset": "100"})
            return httpx.Response(200, json={"routes": []})

        return real_client(transport=httpx.MockTransport(handler))

    monkeypatch.setattr("evm_inventory.wallet_lifi.httpx.Client", factory)
    row = WalletWorkbookRow(
        row_number=2,
        ordinal=4,
        public_address="0x" + "1" * 40,
        private_key="0x" + "2" * 64,
        bitget_deposit_address="0x" + "3" * 40,
        rabby_proxy=primary,
        proxy_pool=(primary, reserve),
    )
    second = WalletWorkbookRow(
        row_number=3,
        ordinal=5,
        public_address="0x" + "4" * 40,
        private_key="0x" + "5" * 64,
        bitget_deposit_address="0x" + "6" * 40,
        rabby_proxy=other,
        proxy_pool=(other, "http://host4:1234"),
    )
    clients = WalletLifiClients([row, second], db_path=tmp_path / "inventory.sqlite")
    assert clients.for_wallet(row.public_address).http_client.post(URL, json={}).status_code == 200
    assert calls == [primary, reserve]
    assert clients.for_wallet(second.public_address).http_client.metrics["transport_index"] == 1
    clients.relay_for_wallet(row.public_address).http_client.get("https://outside.example")
    assert calls[-1] == primary
    clients.close()


def test_recover_planning_waits_pool_cooldown_in_same_invocation(tmp_path):
    import csv

    from evm_inventory.collection import recover_planning

    now = [1000.0]

    def handler(r):
        if now[0] < 1060.0:
            raise httpx.ProxyError("offline")
        return httpx.Response(200, json={"routes": []})

    pool = make_pool(tmp_path, [handler, handler], lambda: now[0])
    with pytest.raises(LifiRequestLimit) as caught:
        pool.post(URL, json={})
    pending = dict(
        wallet="0xabc",
        chain_id=10,
        asset_id="native",
        status="manual_review",
        reason=caught.value.reason,
        search={
            "complete": False,
            "errors": [caught.value.reason],
            "pending": [{"reason": caught.value.reason, "retry_at": caught.value.retry_at}],
        },
    )
    existing = dict(wallet="0xabc", chain_id=56, asset_id="native", status="direct_deposit")
    inventory = tmp_path / "wallet" / "inventory"
    inventory.mkdir(parents=True)
    balances = inventory / "balances.csv"
    with balances.open("w") as stream:
        writer = csv.DictWriter(stream, fieldnames=["wallet", "chain_id", "asset_id", "status"])
        writer.writeheader()
        writer.writerow({k: pending[k] for k in writer.fieldnames})
    quote_calls = []

    def quote(path):
        assert path.parent == inventory.parent
        assert pool.post(URL, json={}).status_code == 200
        quote_calls.append(1)
        return {
            "entries": [
                {**pending, "status": "route_ready", "reason": None, "search": {"complete": True}}
            ]
        }

    def sleep(seconds):
        now[0] += seconds

    plan = recover_planning(
        {"entries": [pending, existing]},
        balances,
        quote,
        until=1200.0,
        clock=lambda: now[0],
        wall_clock=lambda: now[0],
        sleep=sleep,
    )
    assert now[0] >= 1060.0
    assert quote_calls == [1]
    assert plan["summary"] == {"direct_deposit": 1, "route_ready": 1}
    assert existing in plan["entries"]
    pool.close()


def test_saved_plan_wait_is_rechecked_when_new_reserve_available(tmp_path):
    import csv

    from evm_inventory.collection import recover_planning

    now = [1000.0]
    inventory = tmp_path / "wallet" / "inventory"
    inventory.mkdir(parents=True)
    path = inventory / "balances.csv"
    pending = dict(
        wallet="0xabc",
        chain_id=10,
        asset_id="native",
        status="manual_review",
        reason="proxy_pool_exhausted",
        search={
            "complete": False,
            "errors": ["proxy_pool_exhausted"],
            "pending": [{"retry_at": 8200.0}],
        },
    )
    with path.open("w") as stream:
        writer = csv.DictWriter(stream, fieldnames=["wallet", "chain_id", "asset_id"])
        writer.writeheader()
        writer.writerow({k: pending[k] for k in writer.fieldnames})

    def sleep(seconds):
        now[0] += seconds

    plan = recover_planning(
        {"entries": [pending]},
        path,
        lambda _: {"entries": [{**pending, "status": "route_ready"}]},
        until=1100.0,
        clock=lambda: now[0],
        wall_clock=lambda: now[0],
        sleep=sleep,
        provider_retry_time=lambda _: 1000.0,
    )
    assert now[0] == 1001.0
    assert plan["summary"] == {"route_ready": 1}


def test_old_provider_429_wait_rechecks_bucket_from_safe_exception_message(tmp_path):
    from evm_inventory.diagnostics import exception_diagnostic

    p = make_pool(
        tmp_path, [lambda r: httpx.Response(200), lambda r: httpx.Response(200)], lambda: 1000.0
    )
    old_error = LifiRequestLimit(
        "routes",
        reason="provider_cooldown",
        retry_at=8200.0,
        diagnostic={"status": 429, "headers": {"retry-after": "7200"}},
    )
    pending = {
        "reason": "quota_wait",
        "retry_at": 8200.0,
        "diagnostic": exception_diagnostic(old_error),
    }
    assert p.retry_at_for_pending(pending) == 1000.0
    p.close()


def test_lifi_pool_refresh_does_not_override_relay_wait(tmp_path):
    import csv

    from evm_inventory.collection import recover_planning

    inventory = tmp_path / "wallet" / "inventory"
    inventory.mkdir(parents=True)
    path = inventory / "balances.csv"
    pending = dict(
        wallet="0xabc",
        chain_id=10,
        asset_id="native",
        status="manual_review",
        routing_provider="relay",
        reason="quota_wait",
        search={"complete": False, "errors": ["quota_wait"], "pending": [{"retry_at": 8200.0}]},
    )
    with path.open("w") as stream:
        writer = csv.DictWriter(stream, fieldnames=["wallet", "chain_id", "asset_id"])
        writer.writeheader()
        writer.writerow({k: pending[k] for k in writer.fieldnames})

    def forbidden(_):
        raise AssertionError("LI.FI must not re-evaluate Relay quota")

    result = recover_planning(
        {"entries": [pending]},
        path,
        forbidden,
        until=1100.0,
        clock=lambda: 1000.0,
        wall_clock=lambda: 1000.0,
        provider_retry_time=forbidden,
    )
    assert result["entries"] == [pending]
