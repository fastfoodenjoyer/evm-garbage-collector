import concurrent.futures
import json

import httpx
import pytest

from evm_inventory.lifi_http import LifiHttpClient, LifiRequestLimit

URL = "https://li.quest/v1/advanced/routes"
PROXY = "http://user:pass@localhost:1234"


def client(db, handler):
    return LifiHttpClient(
        httpx.Client(transport=httpx.MockTransport(handler)), db_path=db, proxy=PROXY
    )


def test_identical_requests_singleflight_and_resume_with_original_age(tmp_path, monkeypatch):
    clock = [1000.0]
    monkeypatch.setattr("evm_inventory.lifi_http.time.time", lambda: clock[0])
    calls = []

    def handler(request):
        calls.append(request)
        return httpx.Response(200, json={"routes": []})

    db = tmp_path / "inventory.sqlite"
    c = client(db, handler)
    with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
        responses = list(pool.map(lambda _: c.post(URL, json={"fromAmount": "10"}), range(8)))
    assert len(calls) == 1
    assert all(r.headers["x-inventory-observed-at"] == "1000.0" for r in responses)
    c.close()
    clock[0] += 60
    resumed = client(db, handler)
    response = resumed.post(URL, json={"fromAmount": "10"})
    assert len(calls) == 1
    assert response.headers["x-inventory-observed-at"] == "1000.0"
    clock[0] += 300
    resumed.post(URL, json={"fromAmount": "10"})
    assert len(calls) == 2
    resumed.close()


def test_429_stops_requests_across_new_clients(tmp_path):
    calls = []

    def handler(request):
        calls.append(request)
        return httpx.Response(429, json={"code": 1005}, headers={"ratelimit-reset": "7200"})

    db = tmp_path / "inventory.sqlite"
    c = client(db, handler)
    with pytest.raises(LifiRequestLimit) as caught:
        c.post(URL, json={"n": 1})
    assert caught.value.reason == "provider_cooldown"
    c.close()
    resumed = client(db, handler)
    with pytest.raises(LifiRequestLimit):
        resumed.post(URL, json={"n": 2})
    assert len(calls) == 1
    resumed.close()


def test_provider_remaining_and_local_quota_survive_restart(tmp_path):
    calls = []

    def handler(request):
        calls.append(request)
        return httpx.Response(
            200,
            json={"routes": []},
            headers={"ratelimit-remaining": "5", "ratelimit-reset": "7200"},
        )

    db = tmp_path / "inventory.sqlite"
    c = client(db, handler)
    c.post(URL, json={"n": 1})
    c.close()
    resumed = client(db, handler)
    with pytest.raises(LifiRequestLimit):
        resumed.post(URL, json={"n": 2})
    assert len(calls) == 1
    resumed.close()


def test_dispatch_clock_crossing_deadline_never_sleeps_negative_duration(tmp_path):
    armed = [False]
    samples = iter([999.0, 1001.0])
    delays = []

    def clock():
        return next(samples, 1001.0) if armed[0] else 0.0

    def sleep(seconds):
        assert seconds >= 0
        delays.append(seconds)

    c = LifiHttpClient(httpx.Client(transport=httpx.MockTransport(lambda _: httpx.Response(
        200, json={"routes": []}))), db_path=tmp_path / "inventory.sqlite", proxy=PROXY,
        clock=clock, sleep=sleep)

    def dispatch():
        armed[0] = True
        return 1000.0

    c.quota.reserve_dispatch_time = dispatch
    assert c.post(URL, json={"n": 1}).status_code == 200
    assert all(delay >= 0 for delay in delays)
    c.close()


def test_distinct_requests_have_bounded_parallelism(tmp_path):
    import threading
    import time

    lock = threading.Lock()
    active = 0
    peak = 0

    def handler(request):
        nonlocal active, peak
        with lock:
            active += 1
            peak = max(peak, active)
        time.sleep(1.05)
        with lock:
            active -= 1
        return httpx.Response(200, json={"routes": []})

    c = client(tmp_path / "inventory.sqlite", handler)
    with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
        list(pool.map(lambda n: c.post(URL, json={"n": n}), range(8)))
    assert peak == 2
    c.close()


def test_pacing_rechecks_cooldown_before_reserving(tmp_path):
    clock = [1000.0]
    calls = []
    c = None

    def sleep(seconds):
        clock[0] += seconds
        with c.quota.db() as db:
            db.execute(
                "INSERT OR REPLACE INTO lifi_limits(scope,bucket,blocked_until) VALUES (?,?,?)",
                (c.scope, "routes", clock[0] + 100),
            )

    c = LifiHttpClient(
        httpx.Client(
            transport=httpx.MockTransport(
                lambda request: calls.append(request) or httpx.Response(200, json={})
            )
        ),
        db_path=tmp_path / "inventory.sqlite",
        proxy=PROXY,
        clock=lambda: clock[0],
        sleep=sleep,
    )
    c.post(URL, json={"n": 1})
    with pytest.raises(LifiRequestLimit):
        c.post(URL, json={"n": 2})
    assert len(calls) == 1
    assert c.metrics["reservations"] == 1


def test_more_than_24_requests_and_execution_reserve(tmp_path):
    clock = [1000.0]
    c = LifiHttpClient(
        httpx.Client(transport=httpx.MockTransport(lambda request: httpx.Response(200, json={}))),
        db_path=tmp_path / "inventory.sqlite",
        proxy=PROXY,
        clock=lambda: clock[0],
        sleep=lambda seconds: clock.__setitem__(0, clock[0] + seconds),
    )
    for n in range(70):
        c.post(URL, json={"n": n})
    with pytest.raises(LifiRequestLimit):
        c.post(URL, json={"n": 71})
    execution = LifiHttpClient(
        httpx.Client(transport=httpx.MockTransport(lambda request: httpx.Response(200, json={}))),
        db_path=c.db_path,
        proxy=PROXY,
        purpose="execution",
        clock=lambda: clock[0],
        sleep=lambda seconds: clock.__setitem__(0, clock[0] + seconds),
    )
    execution.post(URL, json={"n": 72})
    assert c.metrics["requests"] == 70
    assert execution.metrics["requests"] == 1


def test_internal_cache_namespace_changes_cache_identity_without_wire_argument(tmp_path):
    calls = []
    now = [1000.0]

    def handler(request):
        calls.append(request)
        assert json.loads(request.content) == {"fromAmount": "10"}
        return httpx.Response(200, json={"routes": []})

    def sleep(seconds):
        now[0] += seconds

    c = LifiHttpClient(
        httpx.Client(transport=httpx.MockTransport(handler)),
        db_path=tmp_path / "inventory.sqlite",
        proxy=PROXY,
        clock=lambda: now[0],
        sleep=sleep,
    )
    try:
        c.post(URL, json={"fromAmount": "10"}, cache_namespace="catalog-v1")
        c.post(URL, json={"fromAmount": "10"}, cache_namespace="catalog-v1")
        c.post(URL, json={"fromAmount": "10"}, cache_namespace="catalog-v2")
        assert len(calls) == 2
    finally:
        c.close()
