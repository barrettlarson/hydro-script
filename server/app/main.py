"""FastAPI application for pool/spa automation.

Reads (`/api/status`, `/api/health`) are served from an in-memory
:class:`StateCache` kept fresh by a single background :class:`Poller`, so
client count never multiplies upstream load. Actions still go live to Jandy
(commands aren't cached) and trigger a refresh poll so the cache reflects the
change quickly.
"""

import asyncio
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, AsyncIterator, Optional

from dotenv import load_dotenv
from fastapi import APIRouter, Depends, FastAPI, HTTPException, Request
from fastapi.staticfiles import StaticFiles
from iaqualink.client import AqualinkClient
from pydantic import BaseModel
from starlette.middleware.sessions import SessionMiddleware

from app import auth, controls
from app.aqualink import MissingCredentials, get_credentials, open_devices
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

# Signed-cookie sessions: no server-side session store, so sessions survive
# restarts as long as SESSION_SECRET is stable (see auth.get_session_secret).
app.add_middleware(
    SessionMiddleware,
    secret_key=auth.get_session_secret(),
    max_age=auth.SESSION_MAX_AGE,
    same_site="lax",
    https_only=False,  # dev is plain http; flip in Phase 4 behind TLS
)

# All state + action endpoints live on the protected router, so any future
# route is gated by default; only login/logout are public.
public = APIRouter(prefix="/api")
protected = APIRouter(prefix="/api", dependencies=[Depends(auth.require_auth)])


# Helpers

ACTIONS: dict[str, Any] = {
    "spa-on": controls.cmd_spa_on,
    "spa-off": controls.cmd_spa_off,
    "pool-on": controls.cmd_pool_on,
    "pool-off": controls.cmd_pool_off,
    "pump-on": controls.cmd_pump_on,
    "pump-off": controls.cmd_pump_off,
    "safety": controls.cmd_safety,
}


class ActionResult(BaseModel):
    ok: bool
    action: str
    messages: list[str] = []
    error: Optional[str] = None


class TempRequest(BaseModel):
    temp: int


class LoginRequest(BaseModel):
    email: str
    password: str


class AuthResult(BaseModel):
    ok: bool


def _validate_setpoint(temp: int, bounds: tuple[int, int]) -> None:
    """Reject an out-of-bounds set point before any connection is opened.

    Bounds are the system-specific ranges from controls.py — the same ones
    the client reads from /api/status to bound its sliders.
    """
    lo, hi = bounds
    if not lo <= temp <= hi:
        raise HTTPException(status_code=422, detail=f"Set point must be {lo}-{hi}°F.")


def _handle_failure(exc: Exception) -> HTTPException:
    """Classify, record into the cache, and map to a user-facing HTTP error.

    The real exception text is recorded for development; the caller only
    sees the category-appropriate public message.
    """
    category = classify(exc)
    cache.record_failure(category, str(exc))
    status_code, message = http_response(category)
    return HTTPException(status_code=status_code, detail=message)


async def _run_action(name: str, fn: Optional[Any] = None) -> list[str]:
    """Open a connection, run an action against it, return status messages.

    `fn` defaults to the named entry in ACTIONS; endpoints with extra
    parameters (set points) pass a closure over the devices dict instead.
    Actions go live to Jandy (commands aren't cached). On success we confirm
    connectivity in the cache and ask the poller to refresh so the cached
    snapshot reflects the change quickly.
    """
    run = fn if fn is not None else ACTIONS[name]
    try:
        user, pw = get_credentials()
        async with AqualinkClient(user, pw) as client:
            devices = await open_devices(client)
            messages = await run(devices)
        cache.record_success()  # connectivity confirmed; no fresh snapshot
        poller.request_refresh()
        return messages
    except Exception as e:
        raise _handle_failure(e)


# Endpoints


@public.post("/login", response_model=AuthResult)
async def login(body: LoginRequest, request: Request) -> AuthResult:
    """Check the shared credential and mark the session authenticated."""
    try:
        ok = auth.verify_credentials(body.email, body.password)
    except MissingCredentials:
        raise HTTPException(status_code=500, detail="Server login is not configured.")
    if not ok:
        await asyncio.sleep(auth.FAILED_LOGIN_DELAY)
        raise HTTPException(status_code=401, detail="Invalid email or password.")
    request.session["authenticated"] = True
    return AuthResult(ok=True)


@public.post("/logout", response_model=AuthResult)
async def logout(request: Request) -> AuthResult:
    request.session.clear()
    return AuthResult(ok=True)


@protected.get("/status")
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


@protected.get("/health")
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


@protected.post("/spa/on", response_model=ActionResult)
async def spa_on() -> ActionResult:
    messages = await _run_action("spa-on")
    return ActionResult(ok=True, action="spa-on", messages=messages)


@protected.post("/spa/off", response_model=ActionResult)
async def spa_off() -> ActionResult:
    messages = await _run_action("spa-off")
    return ActionResult(ok=True, action="spa-off", messages=messages)


@protected.post("/pool/on", response_model=ActionResult)
async def pool_on() -> ActionResult:
    messages = await _run_action("pool-on")
    return ActionResult(ok=True, action="pool-on", messages=messages)


@protected.post("/pool/off", response_model=ActionResult)
async def pool_off() -> ActionResult:
    messages = await _run_action("pool-off")
    return ActionResult(ok=True, action="pool-off", messages=messages)


@protected.post("/pump/on", response_model=ActionResult)
async def pump_on() -> ActionResult:
    messages = await _run_action("pump-on")
    return ActionResult(ok=True, action="pump-on", messages=messages)


@protected.post("/pump/off", response_model=ActionResult)
async def pump_off() -> ActionResult:
    messages = await _run_action("pump-off")
    return ActionResult(ok=True, action="pump-off", messages=messages)


@protected.post("/spa/temp", response_model=ActionResult)
async def spa_temp(body: TempRequest) -> ActionResult:
    _validate_setpoint(body.temp, controls.SPA_SETPOINT_RANGE)
    messages = await _run_action(
        "spa-temp", lambda devices: controls.cmd_set_spa_temp(devices, body.temp)
    )
    return ActionResult(ok=True, action="spa-temp", messages=messages)


@protected.post("/pool/temp", response_model=ActionResult)
async def pool_temp(body: TempRequest) -> ActionResult:
    _validate_setpoint(body.temp, controls.POOL_SETPOINT_RANGE)
    messages = await _run_action(
        "pool-temp", lambda devices: controls.cmd_set_pool_temp(devices, body.temp)
    )
    return ActionResult(ok=True, action="pool-temp", messages=messages)


@protected.post("/safety", response_model=ActionResult)
async def safety() -> ActionResult:
    messages = await _run_action("safety")
    return ActionResult(ok=True, action="safety", messages=messages)


app.include_router(public)
app.include_router(protected)


# Static client (production).
# In dev the Vite server proxies /api here, and this mount is absent
# unless the client has been built. Registered last so /api routes win.

_client_dist = Path(__file__).resolve().parents[2] / "client" / "dist"

if _client_dist.is_dir():
    app.mount("/", StaticFiles(directory=_client_dist, html=True), name="client")
else:

    @app.get("/")
    def read_root() -> dict[str, str]:
        return {"status": "ok", "message": "hydro-script API (no built client found)"}
