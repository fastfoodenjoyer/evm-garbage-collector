"""Per-wallet time budgets and bounded concurrency for read-only work.

Workers never own operational database connections or submit transactions.
The caller persists completed results; late readers cannot change that snapshot.
"""

from __future__ import annotations

import math
import threading
import time
from collections.abc import Callable, Iterable
from contextlib import contextmanager
from contextvars import ContextVar, copy_context
from dataclasses import dataclass
from queue import Empty, Queue
from typing import TypeVar

T = TypeVar("T")
R = TypeVar("R")


class StageDeadlineExceeded(TimeoutError):
    """The current wallet's stage must checkpoint and yield."""


class Deadline:
    def __init__(self, seconds: float = 30, label: str = "stage"):
        if isinstance(seconds, bool) or not math.isfinite(seconds) or not 0 < seconds <= 30:
            raise ValueError("stage budget must be greater than zero and at most 30 seconds")
        self.started_at = time.monotonic()
        # Include checkpointing and CLI shutdown in the requested stage budget.
        # Remote reads must yield early enough to leave room for local work.
        self.expires_at = self.started_at + seconds - min(1.0, seconds / 10)
        self.label = label
        self.network_slots = threading.BoundedSemaphore(32)

    def remaining(self) -> float:
        return max(0.0, self.expires_at - time.monotonic())

    @property
    def expired(self) -> bool:
        return self.remaining() <= 0

    def check(self) -> None:
        if self.expired:
            raise StageDeadlineExceeded(f"stage_deadline_exceeded:{self.label}")


_CURRENT: ContextVar[Deadline | None] = ContextVar("wallet_stage_deadline", default=None)


def current_deadline() -> Deadline | None:
    return _CURRENT.get()


@contextmanager
def deadline_scope(deadline: Deadline):
    token = _CURRENT.set(deadline)
    try:
        yield deadline
    finally:
        _CURRENT.reset(token)


def check_deadline() -> None:
    if deadline := current_deadline():
        deadline.check()


def request_timeout(default: float = 30) -> float:
    deadline = current_deadline()
    if deadline is None:
        return default
    deadline.check()
    # HTTPX applies timeouts independently to pool/connect/write/read. Reserve
    # room for each phase and favour another endpoint over a long stalled call.
    return min(default, 3.0, deadline.remaining() / 4)


def budget_sleep(seconds: float, *, sleep: Callable[[float], None] = time.sleep) -> None:
    deadline = current_deadline()
    if deadline is None:
        sleep(seconds)
        return
    deadline.check()
    remaining = deadline.remaining()
    sleep(min(seconds, remaining))
    if seconds >= remaining:
        raise StageDeadlineExceeded(f"stage_deadline_exceeded:{deadline.label}")
    deadline.check()


@contextmanager
def network_slot():
    deadline = current_deadline()
    if deadline is None:
        yield
        return
    deadline.check()
    if not deadline.network_slots.acquire(timeout=deadline.remaining()):
        raise StageDeadlineExceeded(f"stage_deadline_exceeded:{deadline.label}")
    try:
        deadline.check()
        yield
    finally:
        deadline.network_slots.release()


def http_request(client, method: str, url: str, **kwargs):
    """Apply the shared stage budget without changing unbudgeted client APIs."""
    if current_deadline() is not None:
        kwargs['timeout'] = request_timeout(kwargs.get('timeout', 20))
    with network_slot():
        return getattr(client, method)(url, **kwargs)


@dataclass(frozen=True)
class ParallelResults[R]:
    completed: dict[int, R]
    errors: dict[int, Exception]
    pending: tuple[int, ...]


def run_parallel[T, R](
    function: Callable[[T], R],
    items: Iterable[T],
    *,
    deadline: Deadline,
    workers: int = 16,
) -> ParallelResults[R]:
    """Collect independent reads within one budget, without joining late I/O.

    Daemon workers are intentional: ThreadPoolExecutor joins outstanding tasks
    on process exit, which would invalidate the stage deadline. Only isolated
    read-only computations may use this function.
    """
    if isinstance(workers, bool) or not isinstance(workers, int) or not 1 <= workers <= 32:
        raise ValueError("workers must be an integer from 1 to 32")
    inputs = list(items)
    jobs: Queue[tuple[int, T]] = Queue()
    output: Queue[tuple[int, R | None, Exception | None, float]] = Queue()
    stopped = threading.Event()
    for index, value in enumerate(inputs):
        jobs.put((index, value))

    def worker():
        with deadline_scope(deadline):
            while not stopped.is_set() and not deadline.expired:
                try:
                    index, value = jobs.get_nowait()
                except Empty:
                    return
                try:
                    deadline.check()
                    result = function(value)
                except Exception as exc:
                    output.put((index, None, exc, time.monotonic()))
                else:
                    output.put((index, result, None, time.monotonic()))

    for _ in range(min(workers, len(inputs))):
        context = copy_context()
        threading.Thread(target=context.run, args=(worker,), daemon=True).start()

    completed: dict[int, R] = {}
    errors: dict[int, Exception] = {}

    def collect(item):
        index, value, error, finished = item
        if finished > deadline.expires_at:
            return
        if error is None:
            completed[index] = value
        else:
            errors[index] = error

    try:
        while len(completed) + len(errors) < len(inputs) and not deadline.expired:
            try:
                collect(output.get(timeout=deadline.remaining()))
            except Empty:
                break
    finally:
        stopped.set()
    while True:
        try:
            collect(output.get_nowait())
        except Empty:
            break
    pending = tuple(i for i in range(len(inputs)) if i not in completed and i not in errors)
    return ParallelResults(completed, errors, pending)
