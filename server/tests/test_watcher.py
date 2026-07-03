"""HeatWatcher tests: crossing detection, per-degree throttle, one-shot latch."""

from typing import Any, Optional

import pytest

from app.watcher import HEATER_OFF_GRACE, HeatWatcher, PushMessage


class FakeClock:
    def __init__(self, now: float = 1000.0) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


def snapshot(
    *,
    spa_heater: str = "ON",
    spa_temp: Optional[float] = 95.0,
    spa_target: Optional[str] = "102",
    pool_heater: str = "OFF",
    pool_temp: Optional[float] = 80.0,
    pool_target: Optional[str] = "84",
) -> dict[str, Any]:
    """A status snapshot in the shape cmd_status produces."""
    return {
        "devices": {
            "spa_heater": {"state": "1", "label": spa_heater},
            "spa_set_point": {"state": spa_target, "label": f"{spa_target}°F"},
            "pool_heater": {"state": "1", "label": pool_heater},
            "pool_set_point": {"state": pool_target, "label": f"{pool_target}°F"},
        },
        "temps": {"air": 75.0, "spa": spa_temp, "pool": pool_temp},
    }


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock()


@pytest.fixture
def watcher(clock: FakeClock) -> HeatWatcher:
    w = HeatWatcher(clock=clock)
    w.start("spa", "device-1")
    return w


class TestProgress:
    def test_first_evaluate_reports_current_degree(self, watcher: HeatWatcher) -> None:
        msgs = watcher.evaluate(snapshot(spa_temp=95.4))
        assert len(msgs) == 1
        msg = msgs[0]
        assert msg == PushMessage(
            device_id="device-1",
            zone="spa",
            title="Spa is heating",
            body="95°F now → 102°F target.",
            tag="heat-spa",
            ready=False,
        )

    def test_same_degree_is_throttled(self, watcher: HeatWatcher) -> None:
        watcher.evaluate(snapshot(spa_temp=95.0))
        assert watcher.evaluate(snapshot(spa_temp=95.9)) == []

    def test_next_degree_fires_once(self, watcher: HeatWatcher) -> None:
        watcher.evaluate(snapshot(spa_temp=95.0))
        msgs = watcher.evaluate(snapshot(spa_temp=96.1))
        assert [m.body for m in msgs] == ["96°F now → 102°F target."]

    def test_temperature_dip_stays_quiet(self, watcher: HeatWatcher) -> None:
        watcher.evaluate(snapshot(spa_temp=96.0))
        assert watcher.evaluate(snapshot(spa_temp=95.2)) == []

    def test_target_moves_with_the_slider(self, watcher: HeatWatcher) -> None:
        watcher.evaluate(snapshot(spa_temp=95.0, spa_target="102"))
        msgs = watcher.evaluate(snapshot(spa_temp=96.0, spa_target="104"))
        assert msgs[0].body == "96°F now → 104°F target."


class TestReady:
    def test_reaching_target_fires_ready_and_unlatches(self, watcher: HeatWatcher) -> None:
        msgs = watcher.evaluate(snapshot(spa_temp=102.0))
        assert len(msgs) == 1
        assert msgs[0].ready is True
        assert msgs[0].title == "Spa is ready"
        assert msgs[0].body == "Water is 102°F (target 102°F)."
        assert not watcher.watching("spa")

    def test_hovering_at_target_never_refires(self, watcher: HeatWatcher) -> None:
        watcher.evaluate(snapshot(spa_temp=102.0))
        assert watcher.evaluate(snapshot(spa_temp=101.8)) == []
        assert watcher.evaluate(snapshot(spa_temp=102.2)) == []

    def test_lowering_target_below_current_fires_ready(self, watcher: HeatWatcher) -> None:
        watcher.evaluate(snapshot(spa_temp=95.0, spa_target="102"))
        msgs = watcher.evaluate(snapshot(spa_temp=95.0, spa_target="94"))
        assert [m.ready for m in msgs] == [True]

    def test_already_at_target_on_first_evaluate(self, watcher: HeatWatcher) -> None:
        # e.g. spa turned on when the water is still warm from earlier
        msgs = watcher.evaluate(snapshot(spa_temp=103.0))
        assert [m.ready for m in msgs] == [True]


class TestCancellation:
    def test_heater_off_within_grace_keeps_watch(
        self, watcher: HeatWatcher, clock: FakeClock
    ) -> None:
        # right after spa-on the cached snapshot may still lag the command
        clock.advance(HEATER_OFF_GRACE / 2)
        assert watcher.evaluate(snapshot(spa_heater="OFF")) == []
        assert watcher.watching("spa")

    def test_heater_off_after_grace_drops_watch(
        self, watcher: HeatWatcher, clock: FakeClock
    ) -> None:
        # turned off elsewhere: another device, the Jandy app, or safety
        clock.advance(HEATER_OFF_GRACE + 1)
        assert watcher.evaluate(snapshot(spa_heater="OFF")) == []
        assert not watcher.watching("spa")

    def test_explicit_cancel(self, watcher: HeatWatcher) -> None:
        watcher.cancel("spa")
        assert watcher.evaluate(snapshot(spa_temp=102.0)) == []

    def test_cancel_all(self, watcher: HeatWatcher) -> None:
        watcher.start("pool", "device-2")
        watcher.cancel_all()
        assert not watcher.watching("spa")
        assert not watcher.watching("pool")


class TestRobustness:
    def test_missing_temp_is_skipped(self, watcher: HeatWatcher) -> None:
        # spa_temp reads empty until water flows past the probe
        assert watcher.evaluate(snapshot(spa_temp=None)) == []
        assert watcher.watching("spa")

    def test_missing_setpoint_is_skipped(self, watcher: HeatWatcher) -> None:
        assert watcher.evaluate(snapshot(spa_target="")) == []
        assert watcher.watching("spa")

    def test_empty_snapshot_is_harmless(self, watcher: HeatWatcher) -> None:
        assert watcher.evaluate({}) == []

    def test_unknown_zone_rejected(self, watcher: HeatWatcher) -> None:
        with pytest.raises(ValueError):
            watcher.start("jacuzzi", "device-1")

    def test_restart_replaces_watch_owner(self, watcher: HeatWatcher) -> None:
        # the most recent "turn on" wins: notify the newer device
        watcher.start("spa", "device-2")
        msgs = watcher.evaluate(snapshot(spa_temp=102.0))
        assert [m.device_id for m in msgs] == ["device-2"]


class TestZonesAreIndependent:
    def test_spa_and_pool_watches_coexist(self, watcher: HeatWatcher) -> None:
        watcher.start("pool", "device-2")
        msgs = watcher.evaluate(
            snapshot(spa_temp=95.0, pool_heater="ON", pool_temp=84.0, pool_target="84")
        )
        by_zone = {m.zone: m for m in msgs}
        assert by_zone["spa"].ready is False
        assert by_zone["spa"].device_id == "device-1"
        assert by_zone["pool"].ready is True
        assert by_zone["pool"].device_id == "device-2"
