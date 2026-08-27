"""Endpoint tests for the FastAPI app with the connection layer faked.

The TestClient is created without a `with` block, so lifespan never runs and
the background poller never starts — endpoints are exercised in isolation
against fake devices and a fresh StateCache.
"""

from typing import Any

import pytest
from fastapi.testclient import TestClient

from app import auth, main, push
from app.aqualink import MissingCredentials
from app.cache import StateCache
from app.errors import FailureCategory
from app.store import CACHE_KEY, WATCHES_KEY, MemoryStore
from app.watcher import HeatWatcher
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
def anon_client(monkeypatch: pytest.MonkeyPatch, devices: dict[str, FakeDevice]) -> TestClient:
    """TestClient with the connection layer faked but no session — for auth tests."""
    monkeypatch.setattr(main, "get_credentials", lambda: ("user", "pass"))
    # The login path resolves get_credentials inside app.auth, not app.main.
    monkeypatch.setattr(auth, "get_credentials", lambda: ("user", "pass"))
    monkeypatch.setattr(auth, "FAILED_LOGIN_DELAY", 0)
    monkeypatch.setattr(main, "AqualinkClient", FakeAqualinkClient)

    async def fake_open_devices(_client: Any) -> dict[str, FakeDevice]:
        return devices

    monkeypatch.setattr(main, "open_devices", fake_open_devices)
    monkeypatch.setattr(main, "cache", StateCache())
    # Memory-backed so the suite never touches the repo's .data/ directory.
    store = MemoryStore()
    monkeypatch.setattr(main, "store", store)
    monkeypatch.setattr(main, "subscriptions", push.SubscriptionStore(store))
    return TestClient(main.app)


@pytest.fixture
def client(anon_client: TestClient) -> TestClient:
    """Logged-in client: the session cookie persists on the TestClient."""
    r = anon_client.post("/api/login", json={"email": "user", "password": "pass"})
    assert r.status_code == 200
    return anon_client


class TestAuth:
    def test_unauth_status_401(self, anon_client: TestClient) -> None:
        r = anon_client.get("/api/status")
        assert r.status_code == 401
        assert r.json()["detail"] == "Not authenticated."

    def test_unauth_health_401(self, anon_client: TestClient) -> None:
        # Gated: health exposes raw exception text in recent_failures.
        assert anon_client.get("/api/health").status_code == 401

    def test_unauth_action_401_and_no_device_touched(
        self, anon_client: TestClient, devices: dict[str, FakeDevice]
    ) -> None:
        r = anon_client.post("/api/spa/on")
        assert r.status_code == 401
        # the dependency short-circuits before _run_action opens a connection
        assert all(dev.calls == [] for dev in devices.values())

    def test_bad_password_401_and_still_gated(self, anon_client: TestClient) -> None:
        r = anon_client.post("/api/login", json={"email": "user", "password": "nope"})
        assert r.status_code == 401
        assert r.json()["detail"] == "Invalid email or password."
        assert anon_client.get("/api/status").status_code == 401

    def test_wrong_email_401(self, anon_client: TestClient) -> None:
        r = anon_client.post("/api/login", json={"email": "intruder", "password": "pass"})
        assert r.status_code == 401

    def test_email_case_and_whitespace_insensitive(self, anon_client: TestClient) -> None:
        r = anon_client.post("/api/login", json={"email": " USER ", "password": "pass"})
        assert r.status_code == 200
        assert r.json() == {"ok": True}

    def test_login_grants_access(self, client: TestClient) -> None:
        main.cache.record_success({"devices": {}, "temps": {}})
        assert client.get("/api/status").status_code == 200
        assert client.get("/api/health").status_code == 200

    def test_logout_revokes_session(self, client: TestClient) -> None:
        assert client.post("/api/logout").status_code == 200
        assert client.get("/api/status").status_code == 401

    def test_login_without_server_creds_500(
        self, anon_client: TestClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def raise_missing() -> tuple[str, str]:
            raise MissingCredentials("IAQUALINK_USER not set")

        monkeypatch.setattr(auth, "get_credentials", raise_missing)
        r = anon_client.post("/api/login", json={"email": "user", "password": "pass"})
        assert r.status_code == 500
        assert r.json()["detail"] == "Server login is not configured."


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


SUBSCRIPTION = {"endpoint": "https://push.example/a", "keys": {"p256dh": "pk", "auth": "au"}}


@pytest.fixture
def push_enabled(monkeypatch: pytest.MonkeyPatch) -> None:
    """Configure push in main with a fake VAPID identity and a memory store."""
    monkeypatch.setattr(
        main,
        "vapid",
        push.VapidConfig(private_key="priv", public_key="pub-key", subject="mailto:t@e.st"),
    )
    store = MemoryStore()
    monkeypatch.setattr(main, "store", store)
    monkeypatch.setattr(main, "subscriptions", push.SubscriptionStore(store))
    monkeypatch.setattr(main, "watcher", HeatWatcher())


def subscribe(client: TestClient) -> None:
    r = client.post("/api/push/subscribe", json={"subscription": SUBSCRIPTION})
    assert r.status_code == 200


class TestPushEndpoints:
    def test_push_endpoints_are_auth_gated(self, anon_client: TestClient) -> None:
        assert anon_client.get("/api/push/config").status_code == 401
        assert anon_client.post("/api/push/subscribe", json={}).status_code == 401
        assert anon_client.post("/api/push/unsubscribe").status_code == 401

    def test_config_reports_disabled_without_vapid(
        self, client: TestClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(main, "vapid", None)
        body = client.get("/api/push/config").json()
        assert body == {"enabled": False, "public_key": None, "subscribed": False}

    @pytest.mark.usefixtures("push_enabled")
    def test_subscribe_then_config_shows_subscribed(self, client: TestClient) -> None:
        body = client.get("/api/push/config").json()
        assert body == {"enabled": True, "public_key": "pub-key", "subscribed": False}
        subscribe(client)
        body = client.get("/api/push/config").json()
        assert body["subscribed"] is True

    @pytest.mark.usefixtures("push_enabled")
    def test_unsubscribe_clears_subscription(self, client: TestClient) -> None:
        subscribe(client)
        assert client.post("/api/push/unsubscribe").status_code == 200
        assert client.get("/api/push/config").json()["subscribed"] is False

    @pytest.mark.usefixtures("push_enabled")
    def test_subscribe_without_endpoint_422(self, client: TestClient) -> None:
        r = client.post("/api/push/subscribe", json={"subscription": {"keys": {}}})
        assert r.status_code == 422

    def test_subscribe_when_disabled_503(
        self, client: TestClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(main, "vapid", None)
        r = client.post("/api/push/subscribe", json={"subscription": SUBSCRIPTION})
        assert r.status_code == 503


class TestHeatWatchLifecycle:
    @pytest.mark.usefixtures("push_enabled")
    def test_spa_on_from_subscribed_device_latches_watch(self, client: TestClient) -> None:
        subscribe(client)
        client.post("/api/spa/on")
        assert main.watcher.watching("spa")

    @pytest.mark.usefixtures("push_enabled")
    def test_spa_on_without_subscription_starts_no_watch(self, client: TestClient) -> None:
        # strictly per-device: no subscription, no notification, no watch
        client.post("/api/spa/on")
        assert not main.watcher.watching("spa")

    @pytest.mark.usefixtures("push_enabled")
    def test_spa_off_cancels_watch(self, client: TestClient) -> None:
        subscribe(client)
        client.post("/api/spa/on")
        client.post("/api/spa/off")
        assert not main.watcher.watching("spa")

    @pytest.mark.usefixtures("push_enabled")
    def test_pool_on_steals_the_heater_and_the_watch(self, client: TestClient) -> None:
        subscribe(client)
        client.post("/api/spa/on")
        client.post("/api/pool/on")
        assert not main.watcher.watching("spa")
        assert main.watcher.watching("pool")

    @pytest.mark.usefixtures("push_enabled")
    def test_safety_cancels_all_watches(self, client: TestClient) -> None:
        subscribe(client)
        client.post("/api/spa/on")
        client.post("/api/safety")
        assert not main.watcher.watching("spa")

    @pytest.mark.usefixtures("push_enabled")
    def test_failed_action_starts_no_watch(
        self, client: TestClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        subscribe(client)

        async def broken(_client: Any) -> dict[str, FakeDevice]:
            raise ConnectionError("no route to host")

        monkeypatch.setattr(main, "open_devices", broken)
        assert client.post("/api/spa/on").status_code == 503
        assert not main.watcher.watching("spa")


def ready_snapshot() -> dict[str, Any]:
    """A snapshot in which the spa has reached its target."""
    return {
        "devices": {
            "spa_heater": {"state": "1", "label": "ON"},
            "spa_set_point": {"state": "102", "label": "102°F"},
            "pool_heater": {"state": "0", "label": "OFF"},
            "pool_set_point": {"state": "84", "label": "84°F"},
        },
        "temps": {"air": 75.0, "spa": 102.0, "pool": None},
    }


class TestNotifyFromSnapshot:
    @pytest.mark.usefixtures("push_enabled")
    async def test_ready_push_is_delivered_to_the_acting_device(
        self, client: TestClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        subscribe(client)
        client.post("/api/spa/on")
        sent: list[tuple[dict[str, Any], dict[str, Any]]] = []

        async def fake_send(
            sub: dict[str, Any], payload: dict[str, Any], _vapid: Any, **_kw: Any
        ) -> bool:
            sent.append((sub, payload))
            return True

        monkeypatch.setattr(main.push, "send_push", fake_send)
        await main.notify_from_snapshot(ready_snapshot())
        assert len(sent) == 1
        sub, payload = sent[0]
        assert sub == SUBSCRIPTION
        assert payload["ready"] is True
        assert payload["tag"] == "heat-spa"
        assert "ready" in payload["title"].lower()
        # one-shot: the watch is gone, a second snapshot sends nothing
        await main.notify_from_snapshot(ready_snapshot())
        assert len(sent) == 1

    @pytest.mark.usefixtures("push_enabled")
    async def test_gone_subscription_is_pruned(
        self, client: TestClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        subscribe(client)
        client.post("/api/spa/on")

        async def gone(*_a: Any, **_kw: Any) -> bool:
            return False

        monkeypatch.setattr(main.push, "send_push", gone)
        await main.notify_from_snapshot(ready_snapshot())
        assert client.get("/api/push/config").json()["subscribed"] is False
        assert not main.watcher.watching("spa")

    @pytest.mark.usefixtures("push_enabled")
    async def test_delivery_failure_is_swallowed(
        self, client: TestClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        subscribe(client)
        client.post("/api/spa/on")

        async def boom(*_a: Any, **_kw: Any) -> bool:
            raise RuntimeError("push service down")

        monkeypatch.setattr(main.push, "send_push", boom)
        await main.notify_from_snapshot(ready_snapshot())  # must not raise

    async def test_noop_when_push_disabled(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(main, "vapid", None)
        await main.notify_from_snapshot(ready_snapshot())  # must not raise


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


class TestPersistence:
    """The store must reflect a request's changes once it returns.

    These pin the invariant that ``persist_changes`` exists to guarantee: an
    action endpoint added later that forgets to save would otherwise drop a
    heat watch with no error and no failing test.
    """

    @pytest.mark.usefixtures("push_enabled")
    def test_action_persists_the_watch_it_started(self, client: TestClient) -> None:
        subscribe(client)
        client.post("/api/spa/on")
        watches = main.store.get(WATCHES_KEY) or {}
        assert "spa" in watches

    @pytest.mark.usefixtures("push_enabled")
    def test_action_persists_a_cancellation(self, client: TestClient) -> None:
        subscribe(client)
        client.post("/api/spa/on")
        client.post("/api/spa/off")
        assert main.store.get(WATCHES_KEY) == {}

    def test_action_without_watcher_changes_still_persists_the_cache(
        self, client: TestClient
    ) -> None:
        # pump endpoints never touch the watcher; the cache still has to land
        client.post("/api/pump/off")
        cached = main.store.get(CACHE_KEY) or {}
        assert cached["last_success_at"] > 0

    def test_failed_action_persists_the_failure(
        self, client: TestClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # health is most useful while broken, so a raising endpoint must save
        async def broken(_client: Any) -> dict[str, FakeDevice]:
            raise ConnectionError("no route to host")

        monkeypatch.setattr(main, "open_devices", broken)
        assert client.post("/api/spa/on").status_code == 503
        cached = main.store.get(CACHE_KEY) or {}
        assert cached["consecutive_failures"] == 1
        assert cached["history"][-1]["category"] == FailureCategory.NETWORK.value

    def test_reads_do_not_write(self, client: TestClient) -> None:
        # clients poll /api/status every 15s; a read changes nothing worth saving
        main.cache.record_success({"devices": {}, "temps": {}})
        assert client.get("/api/status").status_code == 200
        assert client.get("/api/health").status_code == 200
        assert main.store.get(CACHE_KEY) is None
