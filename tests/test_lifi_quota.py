import pytest

from evm_inventory.lifi_quota import LifiQuotaStore, LifiRequestLimit


def quota(tmp_path):
    clock = [1000.0]
    return LifiQuotaStore(tmp_path / "inventory.sqlite", "scope", clock=lambda: clock[0]), clock


def test_durable_window_reserve_and_execution(tmp_path):
    q, clock = quota(tmp_path)
    for n in range(70):
        q.reserve_request("routes", str(n))
    with pytest.raises(LifiRequestLimit) as caught:
        q.reserve_request("routes", "blocked")
    assert caught.value.retry_at == 8200
    assert caught.value.reason == "quota_wait"
    q.reserve_request("routes", "execution", purpose="execution")
    clock[0] = 8200
    q.reserve_request("routes", "fresh")


def test_public_no_reserve_and_expired_snapshot_uses_local_ledger(tmp_path):
    q, clock = quota(tmp_path)
    attempt = q.reserve_request("public", "a")
    q.observe("public", attempt, {"ratelimit-remaining": "5", "ratelimit-reset": "10"}, 200)
    for n in range(5):
        q.reserve_request("public", str(n))
    with pytest.raises(LifiRequestLimit):
        q.reserve_request("public", "blocked")
    clock[0] += 11
    q.reserve_request("public", "expired")


def test_out_of_order_remaining_does_not_double_decrement(tmp_path):
    q, clock = quota(tmp_path)
    first = q.reserve_request("routes", "first")
    clock[0] += 1
    second = q.reserve_request("routes", "second")
    q.observe("routes", second, {"ratelimit-remaining": "63", "ratelimit-reset": "7200"}, 200)
    q.observe("routes", first, {"ratelimit-remaining": "64", "ratelimit-reset": "7200"}, 200)
    assert q.snapshot()["routes"]["remaining"] == 63


def test_dispatch_shared_and_cooldown_retained(tmp_path):
    q, clock = quota(tmp_path)
    assert q.reserve_dispatch_time() == 1000
    other = LifiQuotaStore(q.db_path, "scope", clock=lambda: clock[0])
    assert other.reserve_dispatch_time() == 1001
    attempt = q.reserve_request("routes", "a")
    q.observe("routes", attempt, {"Retry-After": "90"}, 429, '{"token":"secret"}')
    with pytest.raises(LifiRequestLimit) as caught:
        other.reserve_request("routes", "b")
    assert caught.value.retry_at == 1090
    assert "secret" not in str(caught.value.diagnostic)


def test_retry_after_http_date_and_invalid_headers(tmp_path):
    from email.utils import formatdate

    q, clock = quota(tmp_path)
    first = q.reserve_request("routes", "first")
    q.observe("routes", first, {"ratelimit-remaining": "nan", "ratelimit-reset": "bad"}, 200)
    assert q.snapshot()["routes"]["remaining"] is None
    limited = q.observe("routes", first, {"Retry-After": formatdate(1100, usegmt=True)}, 429)
    assert limited.retry_at == 1100
    clock[0] = 1100
    q.reserve_request("routes", "after")


def test_legacy_schema_preserves_history_and_cooldown(tmp_path):
    import sqlite3

    path = tmp_path / "inventory.sqlite"
    with sqlite3.connect(path) as db:
        db.executescript("""
            CREATE TABLE lifi_requests(scope TEXT,bucket TEXT,sent_at REAL);
            CREATE TABLE lifi_limits(scope TEXT,bucket TEXT,blocked_until REAL,
                PRIMARY KEY(scope,bucket));
            CREATE TABLE lifi_quota_state(scope TEXT,bucket TEXT,remaining INTEGER,
                reset_at REAL,PRIMARY KEY(scope,bucket));
            INSERT INTO lifi_requests VALUES ('scope','routes',1000);
            INSERT INTO lifi_limits VALUES ('scope','routes',1100);
        """)
    q = LifiQuotaStore(path, "scope", clock=lambda: 1000)
    assert q.snapshot()["routes"]["local_used"] == 1
    with pytest.raises(LifiRequestLimit) as caught:
        q.reserve_request("routes", "blocked")
    assert caught.value.retry_at == 1100


def test_provider_limit_constrains_local_window(tmp_path):
    q, clock = quota(tmp_path)
    first = q.reserve_request("routes", "first")
    q.observe(
        "routes",
        first,
        {"ratelimit-limit": "8", "ratelimit-remaining": "7", "ratelimit-reset": "7200"},
        200,
    )
    q.reserve_request("routes", "second")
    q.reserve_request("routes", "third")
    with pytest.raises(LifiRequestLimit):
        q.reserve_request("routes", "fourth")


def test_late_dispatches_recheck_actual_start_spacing(tmp_path):
    from evm_inventory.lifi_quota import DispatchWait

    q, clock = quota(tmp_path)
    q.reserve_dispatch_time()
    q.reserve_dispatch_time()
    clock[0] = 1005
    q.reserve_request("routes", "first", paced=True)
    with pytest.raises(DispatchWait) as caught:
        q.reserve_request("routes", "second", paced=True)
    assert caught.value.retry_at == 1006
    assert q.snapshot()["routes"]["local_used"] == 1


def test_provider_limit_smaller_than_planning_reserve_returns_wait(tmp_path):
    q, clock = quota(tmp_path)
    first = q.reserve_request("routes", "first")
    q.observe(
        "routes",
        first,
        {"ratelimit-limit": "2", "ratelimit-remaining": "1", "ratelimit-reset": "10"},
        200,
    )
    clock[0] += 7200
    # Fresh provider ceiling with no local entries must still produce a structured wait.
    q.observe(
        "routes",
        first,
        {"ratelimit-limit": "2", "ratelimit-remaining": "1", "ratelimit-reset": "10"},
        200,
    )
    with pytest.raises(LifiRequestLimit) as caught:
        q.reserve_request("routes", "blocked")
    assert caught.value.retry_at == clock[0] + 10


def test_local_retry_expires_enough_rows_and_step_reserve(tmp_path):
    q, clock = quota(tmp_path)
    for n in range(48):
        q.reserve_request("steps", str(n), purpose="execution")
        clock[0] += 1
    with pytest.raises(LifiRequestLimit) as caught:
        q.reserve_request("steps", "planning")
    assert caught.value.retry_at == 8201
    clock[0] = 8201
    q.reserve_request("steps", "due")


def test_response_remaining_accounts_for_later_local_reservations(tmp_path):
    q, clock = quota(tmp_path)
    first = q.reserve_request("routes", "first")
    q.reserve_request("routes", "second")
    q.reserve_request("routes", "third")
    q.observe("routes", first, {"ratelimit-remaining": "64", "ratelimit-reset": "7200"}, 200)
    assert q.snapshot()["routes"]["remaining"] == 62


def test_provider_limit_without_remaining_still_constrains_window(tmp_path):
    q, clock = quota(tmp_path)
    first = q.reserve_request("routes", "first")
    q.observe("routes", first, {"ratelimit-limit": "6", "ratelimit-reset": "7200"}, 200)
    assert q.snapshot()["routes"]["limit"] == 6
    with pytest.raises(LifiRequestLimit):
        q.reserve_request("routes", "blocked")


def test_429_diagnostics_use_shared_sanitizer(tmp_path, monkeypatch):
    monkeypatch.setenv("TEST_API_KEY", "sensitive-abcdefgh")
    q, clock = quota(tmp_path)
    first = q.reserve_request("routes", "first")
    error = q.observe(
        "routes",
        first,
        {"Set-Cookie": "sensitive-cookie", "Retry-After": "10"},
        429,
        "gateway socks5://user:pass@proxy:1234 sensitive-abcdefgh",
    )
    evidence = str(error.diagnostic)
    assert "user:pass" not in evidence
    assert "sensitive-" not in evidence
    assert "gateway" in evidence


def test_provider_header_cannot_raise_published_local_ceiling(tmp_path):
    from evm_inventory.lifi_quota import LifiQuotaStore

    quota = LifiQuotaStore(tmp_path / "db.sqlite", "scope", clock=lambda: 1000.0)
    with quota.db() as db:
        db.execute(
            "INSERT INTO lifi_quota_state(scope,bucket,remaining,reset_at,provider_limit) "
            "VALUES (?,?,?,?,?)",
            ("scope", "routes", 100, 2000, 100),
        )
    for _ in range(70):
        quota.reserve_request("routes", "key", purpose="planning")
    with pytest.raises(LifiRequestLimit):
        quota.reserve_request("routes", "key", purpose="planning")


def test_actual_start_rechecks_spacing_after_reservation_stall(tmp_path):
    from evm_inventory.lifi_quota import DispatchWait, LifiQuotaStore

    now = [1000.0]
    q = LifiQuotaStore(tmp_path / "db.sqlite", "scope", clock=lambda: now[0])
    first = q.reserve_request("routes", "a", paced=True)
    now[0] += 1
    second = q.reserve_request("routes", "b", paced=True)
    now[0] += 10  # Simulate stalled mark_started; reservations no longer imply spaced starts.
    q.mark_started(first, paced=True)
    with pytest.raises(DispatchWait):
        q.mark_started(second, paced=True)
    now[0] += 1
    q.mark_started(second, paced=True)


def test_actual_start_rechecks_cooldown_after_reservation(tmp_path):
    from evm_inventory.lifi_quota import LifiQuotaStore, LifiRequestLimit

    now = [1000.0]
    q = LifiQuotaStore(tmp_path / "inventory.sqlite", "p", clock=lambda: now[0])
    first = q.reserve_request("routes", "a")
    second = q.reserve_request("routes", "b")
    q.observe("routes", first, {"retry-after": "60"}, 429, "limited")
    with pytest.raises(LifiRequestLimit) as caught:
        q.mark_started(second, paced=True)
    assert caught.value.reason == "provider_cooldown"
