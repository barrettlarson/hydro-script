"""Fake devices and fixtures for testing controls without hardware."""

from __future__ import annotations

from typing import Any

import pytest

from app import controls


class FakeDevice:
    """Simulates an iAquaLink device with on/off state and optional set_temperature."""

    def __init__(self, key: str, *, is_on: bool = False, state: str = "0") -> None:
        self.key = key
        self._is_on = is_on
        self.state = state
        self.calls: list[str] = []

    @property
    def is_on(self) -> bool:
        return self._is_on

    async def turn_on(self) -> None:
        self._is_on = True
        self.calls.append("turn_on")

    async def turn_off(self) -> None:
        self._is_on = False
        self.calls.append("turn_off")

    async def set_temperature(self, temp: int) -> None:
        self.state = str(temp)
        self.calls.append(f"set_temperature({temp})")


def make_devices(**overrides: dict[str, Any]) -> dict[str, FakeDevice]:
    """Build a full device dict with sensible defaults. Override per-device with kwargs.

    Example: make_devices(spa_pump={"is_on": True})
    """
    defaults: dict[str, dict[str, Any]] = {
        "spa_pump": {"is_on": False, "state": "0"},
        "spa_heater": {"is_on": False, "state": "0"},
        "spa_set_point": {"is_on": False, "state": "102"},
        "pool_heater": {"is_on": False, "state": "0"},
        "pool_set_point": {"is_on": False, "state": "84"},
    }
    for key, vals in overrides.items():
        if key in defaults:
            defaults[key].update(vals)
        else:
            defaults[key] = vals
    return {key: FakeDevice(key, **vals) for key, vals in defaults.items()}


@pytest.fixture
def devices() -> dict[str, FakeDevice]:
    """All devices present, everything off."""
    return make_devices()


@pytest.fixture(autouse=True)
def _patch_delays(monkeypatch: pytest.MonkeyPatch) -> None:
    """Zero out asyncio.sleep so tests run instantly."""
    monkeypatch.setattr(controls, "VALVE_DELAY", 0)
    monkeypatch.setattr(controls, "COOLDOWN_DELAY", 0)
