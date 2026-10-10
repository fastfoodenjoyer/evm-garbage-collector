"""Per-wallet time budgets and bounded concurrency for read-only work.

Workers never own operational database connections or submit transactions.
The caller persists completed results; late readers cannot change that snapshot.
"""

from __future__ import annotations

import logging
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
    def __init__(self, seconds: float = 30, label: str = "stage", *, hard_limit: bool = True):
        if isinstance(seconds, bool) or not math.isfinite(seconds) or seconds <= 0:
            raise ValueError("stage budget must be finite and greater than zero")
        if hard_limit and seconds > 30:
            raise ValueError("hard stage budget must be at most 30 seconds")
        self.started_at = time.monotonic()
        # Include checkpointing and CLI shutdown in the requested stage budget.
        # Remote reads must yield early enough to leave room for local work.
        self.expires_at = (
            self.started_at + seconds - min(1.0, seconds / 10) if hard_limit else None
        )
        self.target_seconds = seconds
        self._warning_lock = threading.Lock()
        self._warned = False
        self.label = label
        self.network_slots = threading.BoundedSemaphore(32)

    def remaining(self) -> float | None:
        if self.expires_at is None:
            return None
        return max(0.0, self.expires_at - time.monotonic())

    @property
    def expired(self) -> bool:
        remaining = self.remaining()
        return remaining is not None and remaining <= 0

    @property
    def elapsed(self) -> float:
        return time.monotonic() - self.started_at

    @property
    def budget_exceeded(self) -> bool:
        return self.elapsed > self.target_seconds

    def check(self) -> None:
        if self.expires_at is None and self.budget_exceeded:
            with self._warning_lock:
                if not self._warned:
                    self._warned = True
                    logging.getLogger(__name__).warning(
                        "%s: превышен ориентир %.2f с; полное сравнение продолжается",
                        self.label, self.target_seconds,
                    )
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
    remaining = deadline.remaining()
    return min(default, 3.0) if remaining is None else min(default, 3.0, remaining / 4)


def budget_sleep(seconds: float, *, sleep: Callable[[float], None] = time.sleep) -> None:
    deadline = current_deadline()
    if deadline is None:
        sleep(seconds)
        return
    deadline.check()
    remaining = deadline.remaining()
    sleep(seconds if remaining is None else min(seconds, remaining))
    if remaining is not None and seconds >= remaining:
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
    """Collect independent reads; only a hard budget discards late results.

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
        if deadline.expires_at is not None and finished > deadline.expires_at:
            return
        if error is None:
            completed[index] = value
        else:
            errors[index] = error

    try:
        while len(completed) + len(errors) < len(inputs) and not deadline.expired:
            deadline.check()
            remaining = deadline.remaining()
            try:
                collect(output.get(timeout=0.1 if remaining is None else remaining))
            except Empty:
                if remaining is not None:
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
