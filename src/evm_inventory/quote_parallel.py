"""Bounded quote fanout and per-wallet singleflight price evidence."""

from __future__ import annotations

import threading
from collections.abc import Callable, Iterable

from .models import AssetIdentity
from .stages import StageDeadlineExceeded, check_deadline, current_deadline, run_parallel


class CachedQuoteClient:
    def __init__(self, client, *, workers: int):
        self.client = client
        self.workers = workers
        self._lock = threading.Lock()
        self._events: dict[AssetIdentity, threading.Event] = {}
        self._prices: dict[AssetIdentity, object] = {}
        self._errors: dict[AssetIdentity, Exception] = {}

    def __getattr__(self, name):
        return getattr(self.client, name)

    def routes(self, request):
        check_deadline()
        result = self.client.routes(request)
        check_deadline()
        return result

    def token_price(self, asset):
        check_deadline()
        with self._lock:
            event = self._events.get(asset)
            owner = event is None
            if owner:
                event = threading.Event()
                self._events[asset] = event
        assert event is not None
        if owner:
            try:
                method = getattr(self.client, "token_price", None)
                value = method(asset) if method is not None else None
                check_deadline()
                with self._lock:
                    self._prices[asset] = value
            except Exception as error:
                with self._lock:
                    self._errors[asset] = error
            finally:
                event.set()
        else:
            deadline = current_deadline()
            if not event.wait(deadline.remaining() if deadline is not None else None):
                raise StageDeadlineExceeded("quote price deadline exceeded")
        check_deadline()
        with self._lock:
            if asset in self._errors:
                raise self._errors[asset]
            return self._prices[asset]


def complete_parallel(function: Callable, items: Iterable, *, workers: int):
    """An incomplete fanout invalidates its entire wallet/network group."""
    deadline = current_deadline()
    assert deadline is not None
    results = run_parallel(function, items, deadline=deadline, workers=workers)
    if results.pending:
        raise StageDeadlineExceeded("quote candidate deadline exceeded")
    if results.errors:
        raise results.errors[min(results.errors)]
    return [results.completed[index] for index in sorted(results.completed)]
