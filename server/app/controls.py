"""Pure control logic for spa/pool automation.

All functions take a devices dict and return values or raise exceptions.
No print, sys.exit, or argv — CLI and FastAPI both import this.
"""

import asyncio
from typing import Any, Optional

from app.aqualink import require

# ---- Configuration -------------------------------------------------
COOLDOWN_DELAY = 5  # seconds to purge heat exchanger after heater-off
SPA_SET_POINT = 102  # desired spa temperature (deg F); set None to skip
POOL_SET_POINT = 84  # desired pool temperature (deg F); set None to skip

# System-specific set-point bounds (deg F), narrower than what Jandy accepts.
# Requests outside these are rejected before any command is sent upstream, and
# the web client reads them from /api/status to bound its sliders.
SPA_SETPOINT_RANGE = (90, 104)
POOL_SETPOINT_RANGE = (76, 90)

# Device keys as exposed by iaqualink-py. These are the common defaults,
# but your system may differ -- run `status` first to confirm the names.
SPA_DEVICE = "spa_pump"  # the "spa mode" toggle
SPA_HEATER = "spa_heater"
SPA_SETPOINT_DEV = "spa_set_point"

POOL_HEATER = "pool_heater"
POOL_SETPOINT_DEV = "pool_set_point"
FILTER_PUMP = "pool_pump"  # main filtration pump; deliberately untouched by safety

# Temperature sensors (read-only). Verify key names against real `status` output.
TEMP_SENSORS = {"air": "air_temp", "pool": "pool_temp", "spa": "spa_temp"}
# --------------------------------------------------------------------


def _temp_value(dev: Any) -> Optional[float]:
    """Numeric reading from a temp sensor, or None if absent/unavailable.

    Sensors report state as a string and read empty when the relevant pump
    is off (no water flowing past the probe), so "" maps to None.
    """
    if dev is None:
        return None
    state = getattr(dev, "state", None)
    if state is None:
        return None
    try:
        return float(state)
    except ValueError:
        return None


async def cmd_status(devices: dict[str, Any]) -> dict[str, Any]:
    """Return structured status for display or API serialization."""
    result: dict[str, Any] = {}
    for key in (
        SPA_DEVICE,
        SPA_HEATER,
        SPA_SETPOINT_DEV,
        POOL_HEATER,
        POOL_SETPOINT_DEV,
        FILTER_PUMP,
    ):
        dev = devices.get(key)
        if dev is None:
            result[key] = {"state": None, "label": "(not present)"}
            continue
        state = getattr(dev, "state", "?")
        on = getattr(dev, "is_on", None)
        if key in (SPA_SETPOINT_DEV, POOL_SETPOINT_DEV):
            label = f"{state}°F" if state and state != "?" else f"state={state}"
        elif on is not None:
            label = "ON" if on else "OFF"
        else:
            label = f"state={state}"
        result[key] = {"state": state, "label": label}
    temps = {name: _temp_value(devices.get(key)) for name, key in TEMP_SENSORS.items()}
    return {
        "devices": result,
        "temps": temps,
        "setpoint_ranges": {"spa": SPA_SETPOINT_RANGE, "pool": POOL_SETPOINT_RANGE},
        "all_keys": sorted(devices.keys()),
    }


def _check_setpoint(temp: int, bounds: tuple[int, int], zone: str) -> None:
    lo, hi = bounds
    if not lo <= temp <= hi:
        raise ValueError(f"{zone} set point {temp}°F is outside {lo}-{hi}°F")


async def cmd_set_spa_temp(devices: dict[str, Any], temp: int) -> list[str]:
    """Set the spa heater set point. Does not change any on/off state."""
    _check_setpoint(temp, SPA_SETPOINT_RANGE, "Spa")
    sp = require(devices, SPA_SETPOINT_DEV)
    await sp.set_temperature(temp)
    return [f"Spa set point set to {temp}°F."]


async def cmd_set_pool_temp(devices: dict[str, Any], temp: int) -> list[str]:
    """Set the pool heater set point. Does not change any on/off state."""
    _check_setpoint(temp, POOL_SETPOINT_RANGE, "Pool")
    sp = require(devices, POOL_SETPOINT_DEV)
    await sp.set_temperature(temp)
    return [f"Pool set point set to {temp}°F."]


async def cmd_pump_on(devices: dict[str, Any]) -> list[str]:
    """Turn on the main filtration pump."""
    pump = require(devices, FILTER_PUMP)
    if not pump.is_on:
        await pump.turn_on()
    return ["Filter pump is on."]


async def cmd_pump_off(devices: dict[str, Any]) -> list[str]:
    """Turn off the main filtration pump."""
    pump = require(devices, FILTER_PUMP)
    if pump.is_on:
        await pump.turn_off()
    return ["Filter pump is off."]


async def cmd_spa_on(devices: dict[str, Any]) -> list[str]:
    """Enable spa mode and heater."""
    messages: list[str] = []
    spa = require(devices, SPA_DEVICE)
    heater = require(devices, SPA_HEATER)
    pool_heater = require(devices, POOL_HEATER)

    if pool_heater.is_on:
        messages.append("Turning off pool heater first...")
        messages.extend(await cmd_pool_off(devices))

    messages.append("Enabling spa mode...")
    if not spa.is_on:
        await spa.turn_on()

    if SPA_SET_POINT is not None:
        sp = devices.get(SPA_SETPOINT_DEV)
        if sp is not None:
            messages.append(f"Setting spa set point to {SPA_SET_POINT}°F...")
            await sp.set_temperature(SPA_SET_POINT)

    messages.append("Enabling spa heater...")
    if not heater.is_on:
        await heater.turn_on()
    messages.append("Spa is heating.")
    return messages


async def cmd_spa_off(devices: dict[str, Any]) -> list[str]:
    """Heater off, cooldown purge, then spa mode off."""
    messages: list[str] = []
    spa = require(devices, SPA_DEVICE)
    heater = require(devices, SPA_HEATER)

    messages.append("Disabling spa heater...")
    if heater.is_on:
        await heater.turn_off()

    messages.append(f"Waiting {COOLDOWN_DELAY}s to purge heat exchanger before stopping flow...")
    await asyncio.sleep(COOLDOWN_DELAY)

    messages.append("Disabling spa mode...")
    if spa.is_on:
        await spa.turn_off()
    messages.append("Spa is off.")
    return messages


async def cmd_pool_on(devices: dict[str, Any]) -> list[str]:
    """Enable pool heater with spa mutual exclusion."""
    messages: list[str] = []
    heater = require(devices, POOL_HEATER)
    spa = require(devices, SPA_DEVICE)
    spa_heater = require(devices, SPA_HEATER)

    if spa.is_on and spa_heater.is_on:
        messages.append("Turning off spa heater and spa mode first...")
        messages.extend(await cmd_spa_off(devices))
    elif spa.is_on:
        messages.append("Turning off spa mode first...")
        await spa.turn_off()

    if POOL_SET_POINT is not None:
        po = devices.get(POOL_SETPOINT_DEV)
        if po is not None:
            messages.append(f"Setting pool set point to {POOL_SET_POINT}°F...")
            await po.set_temperature(POOL_SET_POINT)

    messages.append("Enabling pool heater...")
    if not heater.is_on:
        await heater.turn_on()
    messages.append("Pool is heating.")
    return messages


async def cmd_pool_off(devices: dict[str, Any]) -> list[str]:
    """Turn off pool heater."""
    messages: list[str] = []
    heater = require(devices, POOL_HEATER)

    messages.append("Disabling pool heater...")
    if heater.is_on:
        await heater.turn_off()
    return messages


async def cmd_safety(devices: dict[str, Any]) -> list[str]:
    """Idempotent shutdown — only acts on things that are currently on."""
    messages: list[str] = []
    spa = devices.get(SPA_DEVICE)
    spa_heater = devices.get(SPA_HEATER)
    spa_on = spa is not None and spa.is_on
    spa_heater_on = spa_heater is not None and spa_heater.is_on

    pool_heater = devices.get(POOL_HEATER)
    pool_heater_on = pool_heater is not None and pool_heater.is_on

    if pool_heater_on:
        messages.extend(await cmd_pool_off(devices))
    if spa_on and spa_heater_on:
        messages.extend(await cmd_spa_off(devices))
    elif spa_on and spa is not None:
        await spa.turn_off()
        messages.append("Spa mode disabled.")

    messages.append("Any system previously on is now shut off (excluding pool pump).")
    return messages
