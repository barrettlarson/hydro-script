"""FastAPI application for pool/spa automation.

Action endpoints call controls.py logic via a fresh iAquaLink connection
per request. Background polling / StateCache is scaffolded for future work.
"""

from typing import Any, Optional

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException
from iaqualink.client import AqualinkClient
from pydantic import BaseModel

from app import controls
from app.aqualink import get_credentials, open_devices, AqualinkError

load_dotenv()

app = FastAPI()


# ---- State cache (stub for future background poller) ---------------


class StateCache:
    """Holds the latest system snapshot and metadata."""

    def __init__(self) -> None:
        self.state: Optional[dict[str, Any]] = None
        self.last_success_at: float = 0.0
        self.last_attempt_at: float = 0.0
        self.consecutive_failures: int = 0


cache = StateCache()


# ---- Helpers -------------------------------------------------------

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


async def _run_action(name: str) -> list[str]:
    """Open a connection, run the named action, return status messages."""
    try:
        user, pw = get_credentials()
        async with AqualinkClient(user, pw) as client:
            devices = await open_devices(client)
            return await ACTIONS[name](devices)
    except AqualinkError as e:
        raise HTTPException(status_code=500, detail=str(e))


async def _get_status() -> dict[str, Any]:
    """Open a connection and return structured status."""
    try:
        user, pw = get_credentials()
        async with AqualinkClient(user, pw) as client:
            devices = await open_devices(client)
            return await controls.cmd_status(devices)
    except AqualinkError as e:
        raise HTTPException(status_code=500, detail=str(e))


# ---- Endpoints -----------------------------------------------------


@app.get("/")
def read_root() -> dict[str, str]:
    return {"status": "ok", "message": "hydro-script API"}


@app.get("/api/status")
async def status() -> dict[str, Any]:
    return await _get_status()


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
