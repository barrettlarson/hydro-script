#!/usr/bin/env python3
"""
Spa/pool automation for Jandy iAquaLink.

Commands:
    python controls.py spa-on    # enable spa mode, set temp, wait for valves, heater on
    python controls.py spa-off   # heater off -> purge heat exchanger -> spa mode off
    python controls.py pool-on   # set pool temp, turn on pool heater
    python controls.py pool-off  # turn off pool heater
    python controls.py status    # print current state of all devices
    python controls.py safety    # shut down everything on (for 2 AM cron)

Requires:  pip install iaqualink python-dotenv
Credentials in .env:
    IAQUALINK_USER=you@example.com
    IAQUALINK_PASS=yourpassword
"""

import asyncio
import os
import sys

from dotenv import load_dotenv
from iaqualink.client import AqualinkClient

load_dotenv()

# ---- Configuration -------------------------------------------------
VALVE_DELAY = 30 # seconds before heater fires on startup; may be reducible
COOLDOWN_DELAY = 5 # seconds to purge heat exchanger after heater-off before
SPA_SET_POINT = 102 # desired spa temperature (deg F); set None to skip
POOL_SET_POINT = 84 # desired pool temparature (deg F); set None to skip

# Device keys as exposed by iaqualink-py. These are the common defaults,
# but your system may differ -- run `status` first to confirm the names.
SPA_DEVICE = "spa_pump" # the "spa mode" toggle
SPA_HEATER = "spa_heater"
SPA_SETPOINT_DEV = "spa_set_point"

POOL_DEVICE = "pool_pump"
POOL_HEATER = "pool_heater"
POOL_SETPOINT_DEV = "pool_set_point"
# --------------------------------------------------------------------


def get_credentials():
    user = os.environ.get("IAQUALINK_USER")
    pw = os.environ.get("IAQUALINK_PASS")
    if not user or not pw:
        sys.exit("Set IAQUALINK_USER and IAQUALINK_PASS environment variables.")
    return user, pw


async def get_system(client):
    systems = await client.get_systems()
    if not systems:
        sys.exit("No iAquaLink systems found on this account.")
    system = list(systems.values())[0]
    await system.update()
    return system


def require(devices, key):
    dev = devices.get(key)
    if dev is None:
        avail = ", ".join(sorted(devices.keys()))
        sys.exit(f"Device '{key}' not found. Available devices: {avail}")
    return dev


async def cmd_status(devices):
    for key in (SPA_DEVICE, SPA_HEATER, SPA_SETPOINT_DEV, POOL_DEVICE, POOL_HEATER, POOL_SETPOINT_DEV):
        dev = devices.get(key)
        if dev is None:
            print(f"{key:18s} : (not present)")
            continue
        state = getattr(dev, "state", "?")
        on = getattr(dev, "is_on", None)
        if key in (SPA_SETPOINT_DEV, POOL_SETPOINT_DEV):
            label = f"{state}°F" if state and state != "?" else f"state={state}"
        elif on is not None:
            label = "ON" if on else "OFF"
        else:
            label = f"state={state}"
        print(f"{key:18s} : {label}")
    print("\nAll device keys:", ", ".join(sorted(devices.keys())))


async def cmd_spa_on(devices):
    spa = require(devices, SPA_DEVICE)
    heater = require(devices, SPA_HEATER)
    pool_heater = require(devices, POOL_HEATER)

    if pool_heater.is_on:
        print("Turning off pool heater first...")
        await cmd_pool_off(devices)

    print("Enabling spa mode...")
    if not spa.is_on:
        await spa.turn_on()

    if SPA_SET_POINT is not None:
        sp = devices.get(SPA_SETPOINT_DEV)
        if sp is not None:
            print(f"Setting spa set point to {SPA_SET_POINT}F...")
            await sp.set_temperature(SPA_SET_POINT)

    print(f"Waiting {VALVE_DELAY}s for valves to actuate and flow to establish...")
    await asyncio.sleep(VALVE_DELAY)

    print("Enabling spa heater...")
    if not heater.is_on:
        await heater.turn_on()
    print("Spa is heating.")


async def cmd_spa_off(devices):
    spa = require(devices, SPA_DEVICE)
    heater = require(devices, SPA_HEATER)

    print("Disabling spa heater...")
    if heater.is_on:
        await heater.turn_off()

    print(f"Waiting {COOLDOWN_DELAY}s to purge heat exchanger before stopping flow...")
    await asyncio.sleep(COOLDOWN_DELAY)

    print("Disabling spa mode...")
    if spa.is_on:
        await spa.turn_off()
    print("Spa is off.")


async def cmd_pool_on(devices):
    heater = require(devices, POOL_HEATER)
    spa = require(devices, SPA_DEVICE)
    spa_heater = require(devices, SPA_HEATER)

    if spa.is_on and spa_heater.is_on:
        print("Turning off spa heater and spa mode first...")
        await cmd_spa_off(devices)
    elif spa.is_on:
        print("Turning off spa mode first...")
        await spa.turn_off()

    if POOL_SET_POINT is not None:
        po = devices.get(POOL_SETPOINT_DEV)
        if po is not None:
            print(f"Setting pool set point to {POOL_SET_POINT}F...")
            await po.set_temperature(POOL_SET_POINT)

    print("Enabling pool heater...")
    if not heater.is_on:
        await heater.turn_on()
    print("Pool is heating.")


async def cmd_pool_off(devices):
    heater = require(devices, POOL_HEATER)

    print("Disabling pool heater...")
    if heater.is_on:
        await heater.turn_off()


async def cmd_safety(devices):
    """For the 2 AM cron job: only act if the spa or pool is actually on."""
    spa = devices.get(SPA_DEVICE)
    spa_heater = devices.get(SPA_HEATER)
    spa_on = spa is not None and spa.is_on
    spa_heater_on = spa_heater is not None and spa_heater.is_on

    pool_heater = devices.get(POOL_HEATER)
    pool_heater_on = pool_heater is not None and pool_heater.is_on

    if pool_heater_on:
        await cmd_pool_off(devices)
    if spa_on and spa_heater_on:
        await cmd_spa_off(devices)
    elif spa_on:
        await spa.turn_off()

    print("Any system previously on is now shut off (excluding pool pump).")


async def main():
    if len(sys.argv) < 2 or sys.argv[1] not in ("spa-on", "spa-off", "pool-on", "pool-off", "status", "safety"):
        sys.exit("Usage: controls.py [spa-on|spa-off|pool-on|pool-off|status|safety]")

    cmd = sys.argv[1]
    user, pw = get_credentials()

    async with AqualinkClient(user, pw) as client:
        system = await get_system(client)
        devices = await system.get_devices()

        if cmd == "status":
            await cmd_status(devices)
        elif cmd == "spa-on":
            await cmd_spa_on(devices)
        elif cmd == "spa-off":
            await cmd_spa_off(devices)
        elif cmd == "pool-on":
            await cmd_pool_on(devices)
        elif cmd == "pool-off":
            await cmd_pool_off(devices)
        elif cmd == "safety":
            await cmd_safety(devices)


if __name__ == "__main__":
    asyncio.run(main())