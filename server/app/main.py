"""FastAPI application for pool/spa automation.

Reads (`/api/status`, `/api/health`) are served from an in-memory
:class:`StateCache` kept fresh by a single background :class:`Poller`, so
client count never multiplies upstream load. Actions still go live to Jandy
(commands aren't cached) and trigger a refresh poll so the cache reflects the
change quickly.
"""

import asyncio
import logging
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, AsyncIterator, Optional
from uuid import uuid4

from dotenv import load_dotenv
from fastapi import APIRouter, Depends, FastAPI, HTTPException, Request
from fastapi.staticfiles import StaticFiles
from iaqualink.client import AqualinkClient
from pydantic import BaseModel
from starlette.middleware.sessions import SessionMiddleware

from app import auth, controls, push
from app.aqualink import MissingCredentials, get_credentials, open_devices
from app.cache import StateCache
from app.errors import classify, http_response
from app.poller import Poller
from app.store import CACHE_KEY, WATCHES_KEY, get_store
from app.watcher import HeatWatcher

load_dotenv()

logger = logging.getLogger(__name__)


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

# Web Push: per-device subscriptions + heat watches. vapid is None when
# VAPID_PRIVATE_KEY isn't configured — push endpoints then report disabled and
# no watch is ever created.
vapid = push.load_vapid_config()
watcher = HeatWatcher()

# Everything above is process-local; the store is where it survives. Locally
# that's JSON under .data/; on Lambda it's DynamoDB, because each invocation is
# a different process and module globals don't carry across (see app.store).
store = get_store()
subscriptions = push.SubscriptionStore(store)


def load_state() -> None:
    """Populate the in-process cache and watches from the store."""
    cache.load_doc(store.get(CACHE_KEY))
    watcher.load_doc(store.get(WATCHES_KEY))


def save_state() -> None:
    """Write the in-process cache and watches back to the store.

    Called after every poll and every action. Deliberately synchronous: the
    file backend is sub-millisecond, and the DynamoDB backend only runs under
    Lambda, where an execution environment handles one request at a time and
    so has no event loop to starve.
    """
    store.put(CACHE_KEY, cache.to_doc())
    store.put(WATCHES_KEY, watcher.to_doc())


#: Push service queueing for in-place progress updates: a "96° now" that
#: arrives after the phone was offline for an hour is worse than nothing.
PROGRESS_TTL = 120


async def notify_from_snapshot(snapshot: dict[str, Any]) -> None:
    """Poller hook: turn a fresh snapshot into push deliveries.

    Best-effort by design — a failed delivery is logged and skipped (the
    poller also guards), and a subscription the push service reports gone is
    pruned along with its watch.
    """
    if vapid is None:
        return
    for msg in watcher.evaluate(snapshot):
        sub = subscriptions.get(msg.device_id)
        if sub is None:
            watcher.cancel(msg.zone)  # device unsubscribed mid-heat
            continue
        payload = {"title": msg.title, "body": msg.body, "tag": msg.tag, "ready": msg.ready}
        try:
            alive = await push.send_push(
                sub, payload, vapid, ttl=3600 if msg.ready else PROGRESS_TTL
            )
        except Exception:  # noqa: BLE001 - delivery is best-effort
            logger.exception("push delivery for %s failed", msg.tag)
            continue
        if not alive:
            subscriptions.remove(msg.device_id)
            watcher.cancel(msg.zone)


poller = Poller(cache, fetch_status, on_snapshot=notify_from_snapshot, on_cycle=save_state)


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    """Start the background poller for the app's lifetime, stop it on shutdown."""
    load_state()  # pick up a heat-up already in progress across a restart
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


async def persist_changes(request: Request) -> AsyncIterator[None]:
    """Write cache + watches back to the store after any mutating request.

    One per-request hook rather than a save at each mutation site: every action
    endpoint changes the cache, the watches, or both, and an endpoint added
    later that forgot to persist would drop a heat watch silently — no error,
    no failing test, just a notification that never arrives.

    Runs on failures too (a ``yield`` dependency's teardown still executes when
    the endpoint raised), which is what gets a recorded failure into the store.
    Reads are skipped: clients poll /api/status every 15s and a read never
    changes anything worth writing.
    """
    try:
        yield
    finally:
        if request.method != "GET":
            save_state()


# All state + action endpoints live on the protected router, so any future
# route is gated — and persisted — by default; only login/logout are public.
public = APIRouter(prefix="/api")
protected = APIRouter(
    prefix="/api",
    dependencies=[Depends(auth.require_auth), Depends(persist_changes)],
)


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


class PushConfig(BaseModel):
    enabled: bool
    public_key: Optional[str]
    subscribed: bool


class SubscribeRequest(BaseModel):
    # The browser's PushSubscription.toJSON(): endpoint + keys. Stored opaque
    # and passed straight through to pywebpush.
    subscription: dict[str, Any]


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


def _device_id(request: Request) -> Optional[str]:
    """The opaque per-browser id riding in the session cookie, if any."""
    device_id = request.session.get("device_id")
    return device_id if isinstance(device_id, str) else None


def _start_heat_watch(zone: str, request: Request) -> None:
    """Latch "notify when ready" for the acting device, if it can receive pushes.

    Strictly per-device: a session without a push subscription starts no
    watch, so nobody else gets pinged for someone else's button press.
    """
    device_id = _device_id(request)
    if vapid is not None and device_id and subscriptions.get(device_id):
        watcher.start(zone, device_id)


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


@protected.get("/push/config", response_model=PushConfig)
async def push_config(request: Request) -> PushConfig:
    """What the client needs to offer notifications: server key + own state."""
    device_id = _device_id(request)
    return PushConfig(
        enabled=vapid is not None,
        public_key=vapid.public_key if vapid else None,
        subscribed=bool(device_id and subscriptions.get(device_id)),
    )


@protected.post("/push/subscribe", response_model=AuthResult)
async def push_subscribe(body: SubscribeRequest, request: Request) -> AuthResult:
    """Store this browser's push subscription under its session device id."""
    if vapid is None:
        raise HTTPException(status_code=503, detail="Push is not configured on the server.")
    if not body.subscription.get("endpoint"):
        raise HTTPException(status_code=422, detail="Subscription must include an endpoint.")
    device_id = _device_id(request)
    if device_id is None:
        # First subscription from this browser: mint the id that ties future
        # actions to this device. It rides in the signed session cookie.
        device_id = uuid4().hex
        request.session["device_id"] = device_id
    subscriptions.set(device_id, body.subscription)
    return AuthResult(ok=True)


@protected.post("/push/unsubscribe", response_model=AuthResult)
async def push_unsubscribe(request: Request) -> AuthResult:
    device_id = _device_id(request)
    if device_id:
        subscriptions.remove(device_id)
    return AuthResult(ok=True)


@protected.post("/spa/on", response_model=ActionResult)
async def spa_on(request: Request) -> ActionResult:
    messages = await _run_action("spa-on")
    # Spa-on steals the shared heater from the pool, so any pool watch is moot.
    watcher.cancel("pool")
    _start_heat_watch("spa", request)
    return ActionResult(ok=True, action="spa-on", messages=messages)


@protected.post("/spa/off", response_model=ActionResult)
async def spa_off() -> ActionResult:
    messages = await _run_action("spa-off")
    watcher.cancel("spa")
    return ActionResult(ok=True, action="spa-off", messages=messages)


@protected.post("/pool/on", response_model=ActionResult)
async def pool_on(request: Request) -> ActionResult:
    messages = await _run_action("pool-on")
    watcher.cancel("spa")  # mutual exclusion: pool-on turns the spa off
    _start_heat_watch("pool", request)
    return ActionResult(ok=True, action="pool-on", messages=messages)


@protected.post("/pool/off", response_model=ActionResult)
async def pool_off() -> ActionResult:
    messages = await _run_action("pool-off")
    watcher.cancel("pool")
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
    watcher.cancel_all()
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
