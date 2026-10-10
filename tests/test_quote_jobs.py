import json
import sqlite3
import threading

import httpx
import pytest

from evm_inventory.quote_jobs import JobInProgress, QuoteJobs, quote_job_key


@pytest.fixture
def stores(tmp_path):
    now = [1000.0]
    path = tmp_path / "operations.sqlite"
    return path, now, QuoteJobs(path, "scope", clock=lambda: now[0])


def claim(store, request=None):
    request = request or {"endpoint": "/routes", "payload": {"amount": "100"}}
    key = quote_job_key("scope", request, "catalog-v1")
    return key, store.claim(key, request, "catalog-v1", search_key="search")


def test_full_canonical_request_and_policy_determine_key():
    request = {"payload": {"amount": "100", "slippage": 0.01}, "endpoint": "/routes"}
    assert quote_job_key("s", request, "p") == quote_job_key(
        "s", dict(reversed(list(request.items()))), "p"
    )
    for modified in ({**request, "endpoint": "/quote"}, {**request, "payload": {"amount": "101"}}):
        assert quote_job_key("s", request, "p") != quote_job_key("s", modified, "p")
    assert quote_job_key("s", request, "p") != quote_job_key("s", request, "p2")
    assert quote_job_key("s", request, "p") != quote_job_key("s2", request, "p")


def test_resume_reuses_raw_response_and_original_observation(stores):
    path, now, store = stores
    key, token = claim(store)
    response = {"routes": [{"id": "route1"}], "providerMetadata": "preserved"}
    assert store.finish(token, response, 995)
    resumed = QuoteJobs(path, "scope", clock=lambda: now[0])
    row = resumed.read(key)
    assert row["result_json"] == response
    assert row["observed_at"] == 995
    assert row["expires_at"] == 1235
    assert row["search_key"] == "search"
    assert resumed.read("missing") is None


def test_no_route_is_reused_until_observation_ttl(stores):
    _, now, store = stores
    key, token = claim(store)
    store.finish(token, {"routes": []}, 1000, ttl=240)
    assert store.read(key)["state"] == "no_route"
    now[0] = 1240
    assert store.read(key) is None
    with sqlite3.connect(store.db_path) as db:
        assert db.execute("SELECT state FROM quote_jobs").fetchone()[0] == "expired"
    assert store.claim(key, {"amount": "100"}, "catalog-v1")


def test_atomic_claim_excludes_second_connection_until_crash_lease_expires(stores):
    path, now, store = stores
    key, old = claim(store)
    other = QuoteJobs(path, "scope", clock=lambda: now[0])
    with pytest.raises(JobInProgress) as exc:
        other.claim(key, {}, "catalog-v1")
    assert exc.value.reason == "job_in_progress"
    assert exc.value.retry_at == 1060
    now[0] = 1060
    new = other.claim(key, {}, "catalog-v1")
    assert new.attempt_id != old.attempt_id
    assert not store.finish(old, {"routes": [{"id": "stale"}]}, 1060)
    assert not store.fail(old, ValueError("stale"))
    assert not store.renew(old)
    assert other.finish(new, {"routes": [{"id": "fresh"}]}, 1060)
    assert other.read(key)["result_json"]["routes"][0]["id"] == "fresh"


def test_renew_uses_owner_and_attempt_cas(stores):
    path, now, store = stores
    key, token = claim(store)
    now[0] = 1050
    assert store.renew(token)
    other = QuoteJobs(path, "scope", clock=lambda: now[0])
    now[0] = 1070
    with pytest.raises(JobInProgress) as exc:
        other.claim(key, {}, "")
    assert exc.value.retry_at == 1110


def test_errors_are_visible_and_retry_only_in_next_invocation(stores):
    path, now, store = stores
    key, token = claim(store)
    store.fail(token, httpx.ReadTimeout("timed out"))
    assert store.read(key)["diagnostic_json"]["exceptions"][0]["type"] == "ReadTimeout"
    with pytest.raises(JobInProgress) as exc:
        store.claim(key, {}, "")
    assert exc.value.reason == "error"
    assert QuoteJobs(path, "scope", clock=lambda: now[0]).claim(key, {}, "")


def test_non429_client_error_is_persistent_until_key_changes(stores):
    path, now, store = stores
    key, token = claim(store)
    request = httpx.Request("POST", "https://example.com/routes")
    error = httpx.HTTPStatusError(
        "bad request", request=request, response=httpx.Response(400, request=request)
    )
    store.fail(token, error)
    other = QuoteJobs(path, "scope", clock=lambda: now[0])
    with pytest.raises(JobInProgress) as exc:
        other.claim(key, {}, "")
    assert exc.value.reason == "bad_request"
    assert other.claim(quote_job_key("scope", {}, "catalog-v2"), {}, "catalog-v2")


def test_quota_wait_is_resumable_after_retry_time(stores):
    path, now, store = stores
    key, token = claim(store)
    store.fail(token, ValueError("quota"), state="quota_wait", retry_at=1100)
    other = QuoteJobs(path, "scope", clock=lambda: now[0])
    with pytest.raises(JobInProgress) as exc:
        other.claim(key, {}, "")
    assert exc.value.retry_at == 1100
    now[0] = 1100
    assert other.claim(key, {}, "")


def test_diagnostics_remove_credentials(stores):
    _, _, store = stores
    key, token = claim(store)
    store.fail(token, ValueError("api_key=supersecret https://user:password@example.com"))
    serialized = json.dumps(store.read(key)["diagnostic_json"])
    assert "supersecret" not in serialized
    assert "password@example.com" not in serialized


def test_heartbeat_renews_and_stops_thread(stores):
    _, now, store = stores
    _, token = claim(store)
    event = threading.Event()
    original = store.renew

    def renew(value):
        event.set()
        return original(value)

    store.renew = renew
    now[0] = 1010
    with store.heartbeat(token, interval=0.01):
        assert event.wait(1)
    assert not any(t.name == f"quote-lease-{token.attempt_id}" for t in threading.enumerate())


def test_claim_is_owned_by_store_that_acquired_it(stores):
    path, now, store = stores
    _, token = claim(store)
    other = QuoteJobs(path, "scope", clock=lambda: now[0])
    assert not other.renew(token)
    assert not other.finish(token, {"routes": []}, 1000)
    assert not other.fail(token, ValueError("foreign"))


def test_run_cannot_retry_after_another_run_overwrites_its_failure(stores):
    path, now, store = stores
    key, token = claim(store)
    store.fail(token, TimeoutError("first failure"))
    other = QuoteJobs(path, "scope", clock=lambda: now[0])
    other_token = other.claim(key, {}, "")
    other.fail(other_token, TimeoutError("second failure"))
    with pytest.raises(JobInProgress):
        store.claim(key, {}, "")


def test_concurrent_claims_only_one_wins(stores):
    path, now, _ = stores
    first = QuoteJobs(path, "scope", clock=lambda: now[0])
    second = QuoteJobs(path, "scope", clock=lambda: now[0])
    barrier = threading.Barrier(2)
    outcomes = []
    key = quote_job_key("scope", {}, "")

    def attempt(store):
        barrier.wait()
        try:
            outcomes.append(store.claim(key, {}, ""))
        except JobInProgress:
            outcomes.append(None)

    threads = [threading.Thread(target=attempt, args=(store,)) for store in (first, second)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert sum(value is not None for value in outcomes) == 1


@pytest.mark.parametrize("later_state", ["complete", "expired", "abandoned"])
def test_failed_run_cannot_retry_after_other_run_changes_job_state(stores, later_state):
    path, now, store = stores
    key, token = claim(store)
    store.fail(token, TimeoutError("original timeout"))
    other = QuoteJobs(path, "scope", clock=lambda: now[0])
    other_token = other.claim(key, {}, "")
    if later_state in ("complete", "expired"):
        other.finish(other_token, {"routes": [{"id": "fresh"}]}, now[0])
        if later_state == "expired":
            now[0] += 240
            assert store.read(key) is None
    else:
        now[0] += 60
    with pytest.raises(JobInProgress) as exc:
        store.claim(key, {}, "")
    assert exc.value.reason == "error"
    assert "original timeout" in json.dumps(exc.value.diagnostic)


def test_lost_lease_timeout_blocks_retry_even_when_failure_cas_is_rejected(stores):
    _, now, store = stores
    key, token = claim(store)
    now[0] += 60
    assert not store.fail(token, TimeoutError('expired timeout'))
    with pytest.raises(JobInProgress) as exc:
        store.claim(key, {}, '')
    assert exc.value.reason == 'error'
    assert 'expired timeout' in json.dumps(exc.value.diagnostic)
