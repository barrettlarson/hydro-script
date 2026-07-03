"""Endpoint tests for the FastAPI app with the connection layer faked.

The TestClient is created without a `with` block, so lifespan never runs and
the background poller never starts — endpoints are exercised in isolation
against fake devices and a fresh StateCache.
"""

from typing import Any

import pytest
from fastapi.testclient import TestClient

from app import main
from app.cache import StateCache
from app.errors import FailureCategory
from tests.conftest import FakeDevice


class FakeAqualinkClient:
    """Stands in for AqualinkClient as an async context manager."""

    def __init__(self, user: str, password: str) -> None:
        pass

    async def __aenter__(self) -> "FakeAqualinkClient":
        return self

    async def __aexit__(self, *exc_info: object) -> bool:
        return False


@pytest.fixture
def client(monkeypatch: pytest.MonkeyPatch, devices: dict[str, FakeDevice]) -> TestClient:
    monkeypatch.setattr(main, "get_credentials", lambda: ("user", "pass"))
    monkeypatch.setattr(main, "AqualinkClient", FakeAqualinkClient)

    async def fake_open_devices(_client: Any) -> dict[str, FakeDevice]:
        return devices

    monkeypatch.setattr(main, "open_devices", fake_open_devices)
    monkeypatch.setattr(main, "cache", StateCache())
    return TestClient(main.app)


class TestStatusEndpoint:
    def test_warming_up_503(self, client: TestClient) -> None:
        r = client.get("/api/status")
        assert r.status_code == 503
        assert "warming up" in r.json()["detail"].lower()

    def test_served_from_cache_without_upstream_call(
        self, client: TestClient, devices: dict[str, FakeDevice]
    ) -> None:
        snapshot = {"devices": {}, "temps": {"air": 75.0}}
        main.cache.record_success(snapshot)
        r = client.get("/api/status")
        assert r.status_code == 200
        assert r.json() == snapshot
        # no device was touched: the read came from the cache
        assert all(dev.calls == [] for dev in devices.values())


class TestHealthEndpoint:
    def test_down_before_first_success(self, client: TestClient) -> None:
        body = client.get("/api/health").json()
        assert body["status"] == "down"
        assert body["last_success_at"] is None

    def test_iso_timestamps_and_failure_records(self, client: TestClient) -> None:
        main.cache.record_failure(FailureCategory.NETWORK, "boom")
        body = client.get("/api/health").json()
        assert body["consecutive_failures"] == 1
        record = body["recent_failures"][0]
        assert record["category"] == "network"
        assert record["ts"].endswith("+00:00")  # ISO at the edge


class TestActionEndpoints:
    def test_spa_on(self, client: TestClient, devices: dict[str, FakeDevice]) -> None:
        r = client.post("/api/spa/on")
        assert r.status_code == 200
        body = r.json()
        assert body["ok"] is True
        assert body["action"] == "spa-on"
        assert "Spa is heating." in body["messages"]
        assert devices["spa_pump"].is_on
        assert devices["spa_heater"].is_on

    def test_pump_on_and_off(self, client: TestClient, devices: dict[str, FakeDevice]) -> None:
        assert client.post("/api/pump/off").status_code == 200
        assert not devices["pool_pump"].is_on
        assert client.post("/api/pump/on").status_code == 200
        assert devices["pool_pump"].is_on

    def test_action_confirms_connectivity_in_cache(self, client: TestClient) -> None:
        client.post("/api/pool/on")
        assert main.cache.last_success_at > 0
        assert main.cache.state is None  # actions don't fabricate a snapshot


class TestTempEndpoints:
    def test_set_spa_temp(self, client: TestClient, devices: dict[str, FakeDevice]) -> None:
        r = client.post("/api/spa/temp", json={"temp": 100})
        assert r.status_code == 200
        assert r.json()["action"] == "spa-temp"
        assert devices["spa_set_point"].state == "100"

    def test_set_pool_temp(self, client: TestClient, devices: dict[str, FakeDevice]) -> None:
        r = client.post("/api/pool/temp", json={"temp": 80})
        assert r.status_code == 200
        assert devices["pool_set_point"].state == "80"

    @pytest.mark.parametrize("temp", [89, 105])
    def test_spa_temp_out_of_range_422(
        self, client: TestClient, devices: dict[str, FakeDevice], temp: int
    ) -> None:
        r = client.post("/api/spa/temp", json={"temp": temp})
        assert r.status_code == 422
        assert "90-104" in r.json()["detail"]
        assert devices["spa_set_point"].calls == []

    @pytest.mark.parametrize("temp", [71, 91])
    def test_pool_temp_out_of_range_422(
        self, client: TestClient, devices: dict[str, FakeDevice], temp: int
    ) -> None:
        r = client.post("/api/pool/temp", json={"temp": temp})
        assert r.status_code == 422
        assert "76-90" in r.json()["detail"]
        assert devices["pool_set_point"].calls == []

    def test_non_integer_temp_rejected(self, client: TestClient) -> None:
        r = client.post("/api/spa/temp", json={"temp": "hot"})
        assert r.status_code == 422


class TestFailurePath:
    def test_upstream_failure_is_classified_and_masked(
        self, client: TestClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        async def broken(_client: Any) -> dict[str, FakeDevice]:
            raise ConnectionError("no route to host")

        monkeypatch.setattr(main, "open_devices", broken)
        r = client.post("/api/spa/on")
        assert r.status_code == 503  # NETWORK maps to 503
        assert main.cache.consecutive_failures == 1
        assert main.cache.recent_failures[-1].category == FailureCategory.NETWORK
        # the caller sees the public message, not the raw exception text
        assert "no route to host" not in r.json()["detail"]
        assert main.cache.recent_failures[-1].detail == "no route to host"
