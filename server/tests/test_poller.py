"""Tests for the background poller and cache-served status endpoint.

No hardware: the poller's status fetcher is injected with a fake, and the
status endpoint is driven directly against a fresh cache.
"""

from __future__ import annotations

import asyncio
from typing import Any, Optional

import pytest
from fastapi import HTTPException
from iaqualink.exception import AqualinkSystemOfflineException

from app import main
from app.cache import StateCache
from app.poller import Poller


class FakeFetch:
    """Injectable status fetcher; returns a snapshot or raises a set error."""

    def __init__(
        self,
        result: Optional[dict[str, Any]] = None,
        *,
        error: Optional[BaseException] = None,
    ) -> None:
        self.result = result if result is not None else {"devices": {}}
        self.error = error
        self.calls = 0

    async def __call__(self) -> dict[str, Any]:
        self.calls += 1
        if self.error is not None:
            raise self.error
        return self.result


async def _until(predicate, *, tries: int = 2000) -> None:
    """Yield to the loop until `predicate()` is true (no real-time sleeping)."""
    for _ in range(tries):
        if predicate():
            return
        await asyncio.sleep(0)
    raise AssertionError("condition not met before timeout")


class TestPollOnce:
    async def test_success_records_snapshot(self):
        cache = StateCache()
        fetch = FakeFetch({"devices": {"spa_pump": "on"}})
        poller = Poller(cache, fetch)

        await poller.poll_once()

        assert fetch.calls == 1
        assert cache.state == {"devices": {"spa_pump": "on"}}
        assert cache.status() == "ok"

    async def test_failure_is_classified_and_recorded_not_raised(self):
        cache = StateCache()
        fetch = FakeFetch(error=AqualinkSystemOfflineException("system offline"))
        poller = Poller(cache, fetch)

        await poller.poll_once()  # must not raise

        assert cache.state is None
        assert cache.consecutive_failures == 1
        assert cache.recent_failures[0].category.value == "upstream_offline"

    async def test_network_error_classified(self):
        cache = StateCache()
        fetch = FakeFetch(error=ConnectionError("no route"))
        poller = Poller(cache, fetch)

        await poller.poll_once()

        assert cache.recent_failures[0].category.value == "network"


class TestRunLoop:
    async def test_start_polls_immediately_then_stops(self):
        cache = StateCache()
        fetch = FakeFetch({"devices": {}})
        poller = Poller(cache, fetch, interval=1000)  # long: won't tick again

        poller.start()
        await _until(lambda: cache.state is not None)
        await poller.stop()

        assert fetch.calls == 1
        assert cache.state == {"devices": {}}

    async def test_start_is_idempotent(self):
        cache = StateCache()
        fetch = FakeFetch()
        poller = Poller(cache, fetch, interval=1000)

        poller.start()
        poller.start()  # second call is a no-op
        await _until(lambda: fetch.calls >= 1)
        await poller.stop()

        assert fetch.calls == 1

    async def test_request_refresh_triggers_extra_poll(self):
        cache = StateCache()
        fetch = FakeFetch()
        poller = Poller(cache, fetch, interval=1000)  # only immediate + refresh

        poller.start()
        await _until(lambda: fetch.calls == 1)  # immediate startup poll
        poller.request_refresh()
        await _until(lambda: fetch.calls == 2)  # refresh-driven poll
        await poller.stop()

        assert fetch.calls == 2

    async def test_stop_is_safe_without_start(self):
        poller = Poller(StateCache(), FakeFetch())
        await poller.stop()  # must not raise


class TestStatusEndpoint:
    async def test_serves_cached_snapshot(self, monkeypatch):
        cache = StateCache()
        cache.record_success({"devices": {"pool_heater": "on"}})
        monkeypatch.setattr(main, "cache", cache)

        result = await main.status()

        assert result == {"devices": {"pool_heater": "on"}}

    async def test_warming_up_returns_503(self, monkeypatch):
        monkeypatch.setattr(main, "cache", StateCache())  # no successful poll yet

        with pytest.raises(HTTPException) as exc_info:
            await main.status()

        assert exc_info.value.status_code == 503
