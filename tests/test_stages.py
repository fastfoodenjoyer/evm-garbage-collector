import threading
import time

import httpx
import pytest

from evm_inventory.stages import (
    Deadline,
    StageDeadlineExceeded,
    budget_sleep,
    current_deadline,
    deadline_scope,
    run_parallel,
)


def test_independent_jobs_finish_in_parallel_under_one_stage_budget():
    barrier = threading.Barrier(4)
    deadline = Deadline(0.5)

    def job(value):
        assert current_deadline() is deadline
        barrier.wait(timeout=0.3)
        return value * 2

    result = run_parallel(job, [1, 2, 3, 4], deadline=deadline, workers=4)
    assert result.completed == {0: 2, 1: 4, 2: 6, 3: 8}
    assert result.pending == ()
    assert result.errors == {}


def test_deadline_returns_completed_work_without_waiting_for_slow_readers():
    release = threading.Event()
    started = time.monotonic()

    def job(value):
        if value:
            release.wait(timeout=1)
        return value

    try:
        result = run_parallel(job, [0, 1], deadline=Deadline(0.08), workers=2)
        assert time.monotonic() - started < 0.25
        assert result.completed == {0: 0}
        assert result.pending == (1,)
    finally:
        release.set()


def test_retry_sleep_cannot_outlive_stage_budget():
    sleeps = []
    with deadline_scope(Deadline(0.05)):
        with pytest.raises(StageDeadlineExceeded):
            budget_sleep(5, sleep=sleeps.append)
    assert len(sleeps) == 1
    assert 0 < sleeps[0] <= 0.05


def test_stage_budget_rejects_above_thirty_seconds():
    with pytest.raises(ValueError):
        Deadline(31)


def test_stage_reserves_time_for_local_checkpoint_and_shutdown():
    deadline = Deadline(30)
    assert deadline.expires_at - deadline.started_at == 29


def test_transport_rate_limit_does_not_start_retries_after_budget():
    from evm_inventory.transport import Transport

    calls = []
    sleeps = []

    def handler(request):
        calls.append(request.extensions['timeout']['read'])
        return httpx.Response(429, headers={'Retry-After': '1'})

    transport = Transport(
        client=httpx.Client(transport=httpx.MockTransport(handler)),
        interval=0, sleep=sleeps.append,
    )
    with deadline_scope(Deadline(0.05)):
        with pytest.raises(StageDeadlineExceeded):
            transport.post('https://rpc.test', {'method': 'eth_getBalance'})
    assert len(calls) == 1
    assert calls[0] <= 0.05
    assert len(sleeps) == 1 and sleeps[0] <= 0.05
    transport.close()
