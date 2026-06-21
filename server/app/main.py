"""FastAPI application for pool/spa automation.

Reads (`/api/status`, `/api/health`) are served from an in-memory
:class:`StateCache` kept fresh by a single background :class:`Poller`, so
client count never multiplies upstream load. Actions still go live to Jandy
(commands aren't cached) and trigger a refresh poll so the cache reflects the
change quickly.
"""

from contextlib import asynccontextmanager
from datetime import datetime, timezone
from typing import Any, AsyncIterator, Optional

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException
from iaqualink.client import AqualinkClient
from pydantic import BaseModel

from app import controls
from app.aqualink import get_credentials, open_devices
from app.cache import StateCache
from app.errors import classify, http_response
from app.poller import Poller

load_dotenv()


async def fetch_status() -> dict[str, Any]:
    """Open a connection and return a fresh status snapshot (no cache write).

    This is the single upstream read the poller drives; it deliberately does
    not touch the cache so the poller owns recording success/failure.
    """
    user, pw = get_credentials()
    async with AqualinkClient(user, pw) as client:
        devices = await open_devices(client)
        return await controls.cmd_status(devices)


# Shared state cache + the single poller that feeds it.
cache = StateCache()
poller = Poller(cache, fetch_status)


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    """Start the background poller for the app's lifetime, stop it on shutdown."""
    poller.start()
    try:
        yield
    finally:
        await poller.stop()


app = FastAPI(lifespan=lifespan)


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
    """Open a connection, run the named action, return status messages.

    Actions go live to Jandy (commands aren't cached). On success we confirm
    connectivity in the cache and ask the poller to refresh so the cached
    snapshot reflects the change quickly.
    """
    try:
        user, pw = get_credentials()
        async with AqualinkClient(user, pw) as client:
            devices = await open_devices(client)
            messages = await ACTIONS[name](devices)
        cache.record_success()  # connectivity confirmed; no fresh snapshot
        poller.request_refresh()
        return messages
    except Exception as e:
        raise _handle_failure(e)


# Endpoints


@app.get("/")
def read_root() -> dict[str, str]:
    return {"status": "ok", "message": "hydro-script API"}


@app.get("/api/status")
async def status() -> dict[str, Any]:
    """Return the latest cached snapshot, served without an upstream call.

    The background poller keeps this fresh; staleness/health is at /api/health.
    Returns 503 only before the first successful poll (warming up or upstream
    unreachable) — clients should consult /api/health for the reason.
    """
    if cache.state is None:
        raise HTTPException(
            status_code=503,
            detail="Status is warming up — no snapshot yet. See /api/health.",
        )
    return cache.state


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
