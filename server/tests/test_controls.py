"""Tests for pure control logic in controls.py."""

import pytest

from app import controls
from app.aqualink import DeviceNotFound
from tests.conftest import make_devices


# cmd_status


class TestStatus:
    async def test_all_devices_off(self, devices):
        result = await controls.cmd_status(devices)
        assert result["devices"]["spa_pump"]["label"] == "OFF"
        assert result["devices"]["spa_heater"]["label"] == "OFF"
        assert result["devices"]["pool_heater"]["label"] == "OFF"
        assert result["devices"]["spa_set_point"]["label"] == "102°F"
        assert result["devices"]["pool_set_point"]["label"] == "84°F"
        assert "spa_pump" in result["all_keys"]

    async def test_devices_on(self):
        devs = make_devices(
            spa_pump={"is_on": True},
            spa_heater={"is_on": True},
        )
        result = await controls.cmd_status(devs)
        assert result["devices"]["spa_pump"]["label"] == "ON"
        assert result["devices"]["spa_heater"]["label"] == "ON"

    async def test_missing_device(self):
        devs = make_devices()
        del devs["spa_set_point"]
        result = await controls.cmd_status(devs)
        assert result["devices"]["spa_set_point"]["label"] == "(not present)"
        assert result["devices"]["spa_set_point"]["state"] is None

    async def test_unknown_state(self):
        devs = make_devices(spa_set_point={"state": "?"})
        result = await controls.cmd_status(devs)
        assert result["devices"]["spa_set_point"]["label"] == "state=?"

    async def test_all_keys_sorted(self, devices):
        result = await controls.cmd_status(devices)
        assert result["all_keys"] == sorted(devices.keys())


# cmd_spa_on


class TestSpaOn:
    async def test_basic_startup_sequence(self, devices):
        messages = await controls.cmd_spa_on(devices)
        assert devices["spa_pump"].is_on
        assert devices["spa_heater"].is_on
        assert "Spa is heating." in messages

    async def test_sets_temperature(self, devices):
        await controls.cmd_spa_on(devices)
        assert devices["spa_set_point"].state == "102"
        assert "set_temperature(102)" in devices["spa_set_point"].calls

    async def test_turns_off_pool_heater_first(self):
        devs = make_devices(pool_heater={"is_on": True})
        messages = await controls.cmd_spa_on(devs)
        assert not devs["pool_heater"].is_on
        assert "Turning off pool heater first..." in messages

    async def test_already_on_spa_stays_on(self):
        devs = make_devices(spa_pump={"is_on": True})
        await controls.cmd_spa_on(devs)
        # should not call turn_on again
        assert "turn_on" not in devs["spa_pump"].calls
        assert devs["spa_pump"].is_on

    async def test_missing_device_raises(self):
        devs = make_devices()
        del devs["spa_pump"]
        with pytest.raises(DeviceNotFound, match="spa_pump"):
            await controls.cmd_spa_on(devs)


# cmd_spa_off


class TestSpaOff:
    async def test_basic_shutdown_sequence(self):
        devs = make_devices(spa_pump={"is_on": True}, spa_heater={"is_on": True})
        messages = await controls.cmd_spa_off(devs)
        assert not devs["spa_heater"].is_on
        assert not devs["spa_pump"].is_on
        assert "Spa is off." in messages

    async def test_heater_already_off(self):
        devs = make_devices(spa_pump={"is_on": True}, spa_heater={"is_on": False})
        await controls.cmd_spa_off(devs)
        # should not call turn_off on heater since it's already off
        assert "turn_off" not in devs["spa_heater"].calls
        assert not devs["spa_pump"].is_on

    async def test_shutdown_order(self):
        """Heater must turn off before spa pump (heat exchanger purge)."""
        devs = make_devices(spa_pump={"is_on": True}, spa_heater={"is_on": True})
        await controls.cmd_spa_off(devs)
        assert devs["spa_heater"].calls == ["turn_off"]
        assert devs["spa_pump"].calls == ["turn_off"]


# cmd_pool_on


class TestPoolOn:
    async def test_basic_startup(self, devices):
        messages = await controls.cmd_pool_on(devices)
        assert devices["pool_heater"].is_on
        assert "Pool is heating." in messages

    async def test_sets_temperature(self, devices):
        await controls.cmd_pool_on(devices)
        assert devices["pool_set_point"].state == "84"

    async def test_turns_off_spa_and_heater_first(self):
        devs = make_devices(spa_pump={"is_on": True}, spa_heater={"is_on": True})
        messages = await controls.cmd_pool_on(devs)
        assert not devs["spa_pump"].is_on
        assert not devs["spa_heater"].is_on
        assert devs["pool_heater"].is_on
        assert "Turning off spa heater and spa mode first..." in messages

    async def test_turns_off_spa_mode_only(self):
        """Spa pump on but heater off — just disable spa mode, no full spa_off."""
        devs = make_devices(spa_pump={"is_on": True}, spa_heater={"is_on": False})
        messages = await controls.cmd_pool_on(devs)
        assert not devs["spa_pump"].is_on
        assert devs["pool_heater"].is_on
        assert "Turning off spa mode first..." in messages

    async def test_already_on_pool_stays_on(self):
        devs = make_devices(pool_heater={"is_on": True})
        await controls.cmd_pool_on(devs)
        assert "turn_on" not in devs["pool_heater"].calls


# cmd_pool_off


class TestPoolOff:
    async def test_turns_off_heater(self):
        devs = make_devices(pool_heater={"is_on": True})
        messages = await controls.cmd_pool_off(devs)
        assert not devs["pool_heater"].is_on
        assert "Disabling pool heater..." in messages

    async def test_already_off(self, devices):
        messages = await controls.cmd_pool_off(devices)
        assert "turn_off" not in devices["pool_heater"].calls
        assert "Disabling pool heater..." in messages


# cmd_safety


class TestSafety:
    async def test_everything_off_is_noop(self, devices):
        messages = await controls.cmd_safety(devices)
        for dev in devices.values():
            assert dev.calls == []
        assert "Any system previously on is now shut off" in messages[-1]

    async def test_shuts_off_pool_heater(self):
        devs = make_devices(pool_heater={"is_on": True})
        await controls.cmd_safety(devs)
        assert not devs["pool_heater"].is_on

    async def test_shuts_off_spa_full(self):
        devs = make_devices(spa_pump={"is_on": True}, spa_heater={"is_on": True})
        await controls.cmd_safety(devs)
        assert not devs["spa_pump"].is_on
        assert not devs["spa_heater"].is_on

    async def test_spa_on_heater_off(self):
        """Spa pump on but heater already off — just disable spa mode."""
        devs = make_devices(spa_pump={"is_on": True}, spa_heater={"is_on": False})
        messages = await controls.cmd_safety(devs)
        assert not devs["spa_pump"].is_on
        assert "Spa mode disabled." in messages

    async def test_shuts_off_everything(self):
        devs = make_devices(
            spa_pump={"is_on": True},
            spa_heater={"is_on": True},
            pool_heater={"is_on": True},
        )
        await controls.cmd_safety(devs)
        assert not devs["spa_pump"].is_on
        assert not devs["spa_heater"].is_on
        assert not devs["pool_heater"].is_on

    async def test_tolerates_missing_devices(self):
        """Partial system — safety should not crash if devices are absent."""
        devs = make_devices()
        del devs["spa_pump"]
        del devs["spa_heater"]
        messages = await controls.cmd_safety(devs)
        assert "Any system previously on is now shut off" in messages[-1]
