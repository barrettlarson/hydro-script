"""Tests for the Lambda entry points.

These are deliberately *synchronous* test functions: ``poll_handler`` calls
``asyncio.run`` internally, exactly as Lambda invokes it, and that would raise
inside an already-running loop.
"""

from __future__ import annotations

from typing import Any

import pytest

from app import handlers, main
from app.cache import StateCache
from app.poller import Poller
from app.store import MemoryStore
from app.watcher import HeatWatcher
from tests.conftest import FakeDevice
from tests.test_main import FakeAqualinkClient


class FakeFetch:
    """Injectable status fetcher, counting calls."""

    def __init__(self, result: dict[str, Any] | None = None) -> None:
        self.result = result if result is not None else {"devices": {}, "temps": {}}
        self.calls = 0

    async def __call__(self) -> dict[str, Any]:
        self.calls += 1
        return self.result


@pytest.fixture
def lambda_env(monkeypatch: pytest.MonkeyPatch, devices: dict[str, FakeDevice]) -> dict[str, Any]:
    """Wire main as a cold Lambda invocation would find it: empty, store-backed."""
    store = MemoryStore()
    cache = StateCache()
    watcher = HeatWatcher()
    fetch = FakeFetch()
    monkeypatch.setattr(main, "store", store)
    monkeypatch.setattr(main, "cache", cache)
    monkeypatch.setattr(main, "watcher", watcher)
    monkeypatch.setattr(main, "poller", Poller(cache, fetch, on_cycle=main.save_state))
    monkeypatch.setattr(main, "vapid", None)  # push off; delivery is tested elsewhere

    # the connection layer, for the safety path
    monkeypatch.setattr(main, "get_credentials", lambda: ("user", "pass"))
    monkeypatch.setattr(main, "AqualinkClient", FakeAqualinkClient)

    async def fake_open_devices(_client: Any) -> dict[str, FakeDevice]:
        return devices

    monkeypatch.setattr(main, "open_devices", fake_open_devices)
    return {"store": store, "cache": cache, "watcher": watcher, "fetch": fetch, "devices": devices}


class TestHttpHandler:
    def test_lifespan_is_disabled(self) -> None:
        # Load-bearing: with lifespan on, the HTTP function would start the
        # background poller, which cannot run in a frozen execution environment.
        assert handlers.http_handler.lifespan == "off"


class TestPollHandler:
    def test_idle_system_makes_no_upstream_call(self, lambda_env: dict[str, Any]) -> None:
        result = handlers.poll_handler({"action": "poll"})
        assert result["polled"] is False
        assert lambda_env["fetch"].calls == 0

    def test_defaults_to_poll_when_the_event_says_nothing(self, lambda_env: dict[str, Any]) -> None:
        assert handlers.poll_handler({})["action"] == "poll"
        assert handlers.poll_handler(None)["action"] == "poll"

    def test_polls_while_a_heat_watch_is_active(self, lambda_env: dict[str, Any]) -> None:
        lambda_env["watcher"].start("spa", "dev-1")
        main.save_state()

        result = handlers.poll_handler({"action": "poll"})

        assert result["polled"] is True
        assert lambda_env["fetch"].calls == 1

    def test_picks_up_a_watch_started_by_another_invocation(
        self, lambda_env: dict[str, Any]
    ) -> None:
        # The HTTP function starts the watch; this process never saw it.
        lambda_env["watcher"].start("pool", "dev-1")
        main.save_state()
        lambda_env["watcher"].cancel_all()  # simulate a cold execution environment

        assert handlers.poll_handler({"action": "poll"})["polled"] is True
        assert lambda_env["fetch"].calls == 1

    def test_poll_result_is_persisted(self, lambda_env: dict[str, Any]) -> None:
        lambda_env["watcher"].start("spa", "dev-1")
        main.save_state()

        handlers.poll_handler({"action": "poll"})

        stored = lambda_env["store"].get("cache") or {}
        assert stored["state"] == {"devices": {}, "temps": {}}
        assert stored["last_snapshot_at"] > 0

    def test_unknown_action_is_rejected(self, lambda_env: dict[str, Any]) -> None:
        # A typo in an EventBridge rule should fail loudly, not poll silently.
        with pytest.raises(ValueError, match="drain-the-pool"):
            handlers.poll_handler({"action": "drain-the-pool"})


class TestSafetyHandler:
    def test_turns_the_system_off(self, lambda_env: dict[str, Any]) -> None:
        devices = lambda_env["devices"]
        devices["spa_pump"]._is_on = True
        devices["spa_heater"]._is_on = True

        result = handlers.poll_handler({"action": "safety"})

        assert result["ok"] is True
        assert devices["spa_heater"].is_on is False
        assert devices["spa_pump"].is_on is False

    def test_leaves_the_filter_pump_alone(self, lambda_env: dict[str, Any]) -> None:
        # Circulation is not a heat risk; safety has never touched the pump.
        pump = lambda_env["devices"]["pool_pump"]
        handlers.poll_handler({"action": "safety"})
        assert pump.calls == []

    def test_cancels_watches_and_persists(self, lambda_env: dict[str, Any]) -> None:
        # Everything is off, so a pending "your spa is ready" would be a lie.
        lambda_env["watcher"].start("spa", "dev-1")
        main.save_state()

        handlers.poll_handler({"action": "safety"})

        assert lambda_env["watcher"].any_active() is False
        assert lambda_env["store"].get("watches") == {}

    def test_makes_no_poll(self, lambda_env: dict[str, Any]) -> None:
        handlers.poll_handler({"action": "safety"})
        assert lambda_env["fetch"].calls == 0
