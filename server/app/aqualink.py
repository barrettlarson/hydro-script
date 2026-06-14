"""Connection helper for Jandy iAquaLink.

Handles credentials, system discovery, and device lookup.
Raises typed exceptions — callers (CLI, API) decide how to present errors.
"""

import os
from typing import Any

from iaqualink.client import AqualinkClient


class AqualinkError(Exception):
    """Base exception for iAquaLink connection/setup problems."""


class MissingCredentials(AqualinkError):
    """IAQUALINK_USER or IAQUALINK_PASS not set."""


class NoSystemFound(AqualinkError):
    """Account has no iAquaLink systems."""


class DeviceNotFound(AqualinkError):
    """Required device key not present in system."""


def get_credentials() -> tuple[str, str]:
    """Read iAquaLink credentials from environment variables.

    Caller is responsible for loading .env if needed (load_dotenv).
    """
    user = os.environ.get("IAQUALINK_USER")
    pw = os.environ.get("IAQUALINK_PASS")
    if not user or not pw:
        raise MissingCredentials("Set IAQUALINK_USER and IAQUALINK_PASS environment variables.")
    return user, pw


async def open_devices(client: AqualinkClient) -> dict[str, Any]:
    """Get devices from the first system on the account."""
    systems = await client.get_systems()
    if not systems:
        raise NoSystemFound("No iAquaLink systems found on this account.")
    system = list(systems.values())[0]
    await system.update()
    return await system.get_devices()


def require(devices: dict[str, Any], key: str) -> Any:
    """Look up a device by key, raising DeviceNotFound if missing."""
    dev = devices.get(key)
    if dev is None:
        avail = ", ".join(sorted(devices.keys()))
        raise DeviceNotFound(f"Device '{key}' not found. Available: {avail}")
    return dev
