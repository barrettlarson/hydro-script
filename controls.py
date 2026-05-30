#!/usr/bin/env python3
"""
Spa automation for Jandy iAquaLink.

Commands:
    python controls.py on        # spa mode -> wait -> heater on
    python controls.py off       # heater off -> wait -> spa mode off
    python controls.py status    # print current state
    python controls.py safety    # turn spa off ONLY if it is currently on (for 2 AM cron)

Requires:  pip install iaqualink
Credentials come from environment variables so they aren't hardcoded:
    export IAQUALINK_USER="you@example.com"
    export IAQUALINK_PASS="yourpassword"
"""

import asyncio
import os
import sys

from dotenv import load_dotenv
from iaqualink.client import AqualinkClient

load_dotenv()

# ---- Configuration -------------------------------------------------
VALVE_DELAY = 90          # seconds before heater fires on startup; may be reducible —
                          # verify on live hardware (AquaLink may stage this internally)
COOLDOWN_DELAY = 900      # seconds to purge heat exchanger after heater-off before
                          # stopping flow; not the same problem as VALVE_DELAY
SPA_SET_POINT = 102        # desired spa temperature (deg F); set None to skip

# Device keys as exposed by iaqualink-py. These are the common defaults,
# but your system may differ -- run `status` first to confirm the names.
SPA_DEVICE = "spa_pump"        # the "spa mode" toggle
SPA_HEATER = "spa_heater"
SPA_SETPOINT_DEV = "spa_set_point"
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


async def get_devices(system):
    return await system.get_devices()


def require(devices, key):
    dev = devices.get(key)
    if dev is None:
        avail = ", ".join(sorted(devices.keys()))
        sys.exit(f"Device '{key}' not found. Available devices: {avail}")
    return dev


async def cmd_status(devices):
    for key in (SPA_DEVICE, SPA_HEATER, SPA_SETPOINT_DEV):
        dev = devices.get(key)
        if dev is None:
            print(f"{key:18s} : (not present)")
            continue
        state = getattr(dev, "state", "?")
        on = getattr(dev, "is_on", None)
        if key == SPA_SETPOINT_DEV:
            label = f"{state}°F" if state and state != "?" else f"state={state}"
        elif on is not None:
            label = "ON" if on else "OFF"
        else:
            label = f"state={state}"
        print(f"{key:18s} : {label}")
    print("\nAll device keys:", ", ".join(sorted(devices.keys())))


async def cmd_on(devices):
    spa = require(devices, SPA_DEVICE)
    heater = require(devices, SPA_HEATER)

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


async def cmd_off(devices):
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


async def cmd_safety(devices):
    """For the 2 AM cron job: only act if the spa is actually on."""
    spa = devices.get(SPA_DEVICE)
    heater = devices.get(SPA_HEATER)
    spa_on = spa is not None and spa.is_on
    heater_on = heater is not None and heater.is_on

    if not spa_on and not heater_on:
        print("Spa already off. Nothing to do.")
        return

    print("Spa left on -- running safety shutdown.")
    await cmd_off(devices)


async def main():
    if len(sys.argv) < 2 or sys.argv[1] not in ("on", "off", "status", "safety"):
        sys.exit("Usage: controls.py [on|off|status|safety]")

    cmd = sys.argv[1]
    user, pw = get_credentials()

    async with AqualinkClient(user, pw) as client:
        system = await get_system(client)
        devices = await get_devices(system)

        if cmd == "status":
            await cmd_status(devices)
        elif cmd == "on":
            await cmd_on(devices)
        elif cmd == "off":
            await cmd_off(devices)
        elif cmd == "safety":
            await cmd_safety(devices)


if __name__ == "__main__":
    asyncio.run(main())