#!/usr/bin/env python3
"""Thin CLI wrapper for pool/spa automation.

Handles print/exit/argv — all logic lives in controls.py.
"""

import asyncio
import sys

from dotenv import load_dotenv
from iaqualink.client import AqualinkClient

from app import controls
from app.aqualink import get_credentials, open_devices, AqualinkError

load_dotenv()

COMMANDS = {
    "spa-on": controls.cmd_spa_on,
    "spa-off": controls.cmd_spa_off,
    "pool-on": controls.cmd_pool_on,
    "pool-off": controls.cmd_pool_off,
    "status": controls.cmd_status,
    "safety": controls.cmd_safety,
}


def _format_status(data: dict) -> None:
    """Print status dict in the same columnar format as the original CLI."""
    for key, info in data["devices"].items():
        print(f"{key:18s} : {info['label']}")
    print(f"\nAll device keys: {', '.join(data['all_keys'])}")


async def main() -> None:
    if len(sys.argv) < 2 or sys.argv[1] not in COMMANDS:
        sys.exit(f"Usage: cli.py [{' | '.join(COMMANDS)}]")

    cmd = sys.argv[1]

    try:
        user, pw = get_credentials()
        async with AqualinkClient(user, pw) as client:
            devices = await open_devices(client)

            if cmd == "status":
                data = await controls.cmd_status(devices)
                _format_status(data)
            else:
                messages = await COMMANDS[cmd](devices)
                for msg in messages:
                    print(msg)
    except AqualinkError as e:
        sys.exit(str(e))


if __name__ == "__main__":
    asyncio.run(main())
