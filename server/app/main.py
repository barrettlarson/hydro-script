"""FastAPI application for pool/spa automation.

Action endpoints call controls.py logic via a fresh iAquaLink connection
per request. Background polling / StateCache is scaffolded for future work.
"""

from datetime import datetime, timezone
from typing import Any, Optional

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException
from iaqualink.client import AqualinkClient
from pydantic import BaseModel

from app import controls
from app.aqualink import get_credentials, open_devices
from app.cache import StateCache
from app.errors import classify, http_response

load_dotenv()

app = FastAPI()


# State cache: populated by status/action requests today; the background
# poller (future) will write to this same instance.
cache = StateCache()


# Helpers

ACTIONS: dict[str, Any] = {
    "spa-on": controls.cmd_spa_on,
    "spa-off": controls.cmd_spa_off,
    "pool-on": controls.cmd_pool_on,
    "pool-off": controls.cmd_pool_off,
    "safety": controls.cmd_safety,
}


class ActionResult(BaseModel):
    ok: bool
    action: str
    messages: list[str] = []
    error: Optional[str] = None


def _handle_failure(exc: Exception) -> HTTPException:
    """Classify, record into the cache, and map to a user-facing HTTP error.

    The real exception text is recorded for development; the caller only
    sees the category-appropriate public message.
    """
    category = classify(exc)
    cache.record_failure(category, str(exc))
    status_code, message = http_response(category)
    return HTTPException(status_code=status_code, detail=message)


async def _run_action(name: str) -> list[str]:
    """Open a connection, run the named action, return status messages."""
    try:
        user, pw = get_credentials()
        async with AqualinkClient(user, pw) as client:
            devices = await open_devices(client)
            messages = await ACTIONS[name](devices)
        cache.record_success()  # connectivity confirmed; no fresh snapshot
        return messages
    except Exception as e:
        raise _handle_failure(e)


async def _get_status() -> dict[str, Any]:
    """Open a connection and return structured status."""
    try:
        user, pw = get_credentials()
        async with AqualinkClient(user, pw) as client:
            devices = await open_devices(client)
            result = await controls.cmd_status(devices)
        cache.record_success(result)
        return result
    except Exception as e:
        raise _handle_failure(e)


# Endpoints


@app.get("/")
def read_root() -> dict[str, str]:
    return {"status": "ok", "message": "hydro-script API"}


@app.get("/api/status")
async def status() -> dict[str, Any]:
    return await _get_status()


def _iso(ts: Optional[float]) -> Optional[str]:
    """Epoch float -> ISO 8601 UTC string (None passes through)."""
    if ts is None:
        return None
    return datetime.fromtimestamp(ts, tz=timezone.utc).isoformat()


@app.get("/api/health")
async def health() -> dict[str, Any]:
    """Observability surface: cache status, staleness, failure history.

    Timestamps are converted from internal epoch floats to ISO strings here,
    at the API edge.
    """
    h = cache.health()
    h["last_success_at"] = _iso(h["last_success_at"])
    h["last_attempt_at"] = _iso(h["last_attempt_at"])
    for record in h["recent_failures"]:
        record["ts"] = _iso(record["ts"])
    return h


@app.post("/api/spa/on", response_model=ActionResult)
async def spa_on() -> ActionResult:
    messages = await _run_action("spa-on")
    return ActionResult(ok=True, action="spa-on", messages=messages)


@app.post("/api/spa/off", response_model=ActionResult)
async def spa_off() -> ActionResult:
    messages = await _run_action("spa-off")
    return ActionResult(ok=True, action="spa-off", messages=messages)


@app.post("/api/pool/on", response_model=ActionResult)
async def pool_on() -> ActionResult:
    messages = await _run_action("pool-on")
    return ActionResult(ok=True, action="pool-on", messages=messages)


@app.post("/api/pool/off", response_model=ActionResult)
async def pool_off() -> ActionResult:
    messages = await _run_action("pool-off")
    return ActionResult(ok=True, action="pool-off", messages=messages)


@app.post("/api/safety", response_model=ActionResult)
async def safety() -> ActionResult:
    messages = await _run_action("safety")
    return ActionResult(ok=True, action="safety", messages=messages)
