"""Heat watches: notify the device that started heating when the water is ready.

A watch latches when a spa-on / pool-on action comes from a session whose
device has a push subscription (strictly per-device: no subscription, no
watch). The poller feeds every snapshot through :meth:`HeatWatcher.evaluate`,
which emits:

- *progress* messages — at most one per whole degree gained, all sharing one
  notification tag so the client replaces the notification in place ("96° now
  → 102° target" ticking upward) instead of stacking a pile of alerts. The
  per-degree throttle also keeps us inside iOS's push budget (~30s polls
  would otherwise mean ~120 pushes/hour).
- one *ready* message when the temperature reaches the target, after which the
  watch is dropped (one-shot latch — hovering at target never re-fires).

The target is read from the snapshot's set point on every evaluation, so
nudging the slider mid-heat moves the goalpost instead of being ignored.

Watches persist through the shared document store (see ``app.store``), so a
restart — or the next Lambda invocation, which is a different process
entirely — picks up a heat-up already in progress.

Pure logic, no I/O — delivery lives in ``main.py``; tests drive
``evaluate()`` with snapshot dicts.
"""

from __future__ import annotations

import math
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any, Optional

from app.controls import POOL_HEATER, POOL_SETPOINT_DEV, SPA_HEATER, SPA_SETPOINT_DEV

#: zone -> (heater device key, set point device key) in the status snapshot.
ZONES: dict[str, tuple[str, str]] = {
    "spa": (SPA_HEATER, SPA_SETPOINT_DEV),
    "pool": (POOL_HEATER, POOL_SETPOINT_DEV),
}

#: Seconds a fresh watch tolerates the heater reading OFF before the watch is
#: dropped. Right after an "on" action the cache may briefly lag the command;
#: past the grace period an OFF heater means someone turned it off elsewhere
#: (this app on another device, the Jandy app, or the nightly safety).
HEATER_OFF_GRACE = 90.0


@dataclass(frozen=True)
class PushMessage:
    """One notification to deliver to one device (transport-agnostic)."""

    device_id: str
    zone: str
    title: str
    body: str
    tag: str  # same tag per zone so progress updates replace in place
    ready: bool  # True for the final alert (client re-notifies audibly)


@dataclass
class _Watch:
    device_id: str
    started_at: float
    last_degree: Optional[int] = field(default=None)


def _setpoint(devices: dict[str, Any], key: str) -> Optional[int]:
    """Parse a set point device's state ("102") to an int, or None."""
    state = devices.get(key, {}).get("state")
    try:
        return int(float(state)) if state not in (None, "") else None
    except (TypeError, ValueError):
        return None


class HeatWatcher:
    """Tracks at most one active heat watch per zone."""

    def __init__(self, *, clock: Callable[[], float] = time.time) -> None:
        self._clock = clock
        self._watches: dict[str, _Watch] = {}

    def start(self, zone: str, device_id: str) -> None:
        """Latch a watch for `zone`, replacing any existing one.

        Replacement means the most recent "turn on" wins — if a second device
        re-triggers spa-on mid-heat, the notification follows the newer press.
        """
        if zone not in ZONES:
            raise ValueError(f"unknown zone: {zone!r}")
        self._watches[zone] = _Watch(device_id=device_id, started_at=self._clock())

    def cancel(self, zone: str) -> None:
        self._watches.pop(zone, None)

    def cancel_all(self) -> None:
        self._watches.clear()

    def watching(self, zone: str) -> bool:
        return zone in self._watches

    def evaluate(self, snapshot: dict[str, Any]) -> list[PushMessage]:
        """Turn one status snapshot into the notifications it warrants."""
        devices = snapshot.get("devices", {})
        temps = snapshot.get("temps", {})
        out: list[PushMessage] = []
        for zone, watch in list(self._watches.items()):
            heater_key, setpoint_key = ZONES[zone]
            heater_on = devices.get(heater_key, {}).get("label") == "ON"
            if not heater_on:
                if self._clock() - watch.started_at > HEATER_OFF_GRACE:
                    del self._watches[zone]
                continue
            temp = temps.get(zone)
            target = _setpoint(devices, setpoint_key)
            if temp is None or target is None:
                continue  # sensor not reading yet (e.g. water just started flowing)
            name = zone.capitalize()
            tag = f"heat-{zone}"
            if temp >= target:
                out.append(
                    PushMessage(
                        device_id=watch.device_id,
                        zone=zone,
                        title=f"{name} is ready",
                        body=f"Water is {round(temp)}°F (target {target}°F).",
                        tag=tag,
                        ready=True,
                    )
                )
                del self._watches[zone]
                continue
            degree = math.floor(temp)
            if watch.last_degree is None or degree > watch.last_degree:
                out.append(
                    PushMessage(
                        device_id=watch.device_id,
                        zone=zone,
                        title=f"{name} is heating",
                        body=f"{degree}°F now → {target}°F target.",
                        tag=tag,
                        ready=False,
                    )
                )
                watch.last_degree = degree
        return out

    # persistence
    #
    # Watches were in-memory because the server was long-lived; under Lambda
    # the process that starts a watch is never the process that would fire it,
    # so they have to be written down. This also closes the backlog item where
    # a restart mid-heat-up silently dropped the pending notification.

    def to_doc(self) -> dict[str, Any]:
        """Serialize active watches to a JSON-safe document."""
        return {
            zone: {
                "device_id": w.device_id,
                "started_at": w.started_at,
                "last_degree": w.last_degree,
            }
            for zone, w in self._watches.items()
        }

    def load_doc(self, doc: Optional[dict[str, Any]]) -> None:
        """Restore watches from a :meth:`to_doc` document, replacing current ones.

        Unknown zones and malformed entries are dropped rather than raising —
        a bad stored watch should cost one notification, not the poll loop.
        """
        self._watches.clear()
        if not doc:
            return
        for zone, entry in doc.items():
            if zone not in ZONES or not isinstance(entry, dict):
                continue
            device_id = entry.get("device_id")
            if not isinstance(device_id, str):
                continue
            last_degree = entry.get("last_degree")
            self._watches[zone] = _Watch(
                device_id=device_id,
                started_at=float(entry.get("started_at") or 0.0),
                last_degree=int(last_degree) if last_degree is not None else None,
            )
