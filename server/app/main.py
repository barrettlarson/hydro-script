from fastapi import FastAPI
from typing import Optional, Any
from pydantic import BaseModel
from app import controls

app = FastAPI()

class StateCache:
    """Holds the latest system snapshot and metadata"""
    def __init__(self) -> None:
        self.state: Optional[dict[str, Any]] = None
        self.last_success_at: float = 0.0
        self.last_attempt_at: float = 0.0
        self.consecutive_failures: int = 0

cache = StateCache()

ACTIONS = {
    "spa-on": controls.spa_on,
    "spa-off": controls.spa_off,
    "pool-on": controls.pool_on,
    "pool-off": controls.pool_off,
    "safety": controls.safety
}

class ActionResult(BaseModel):
    ok: bool
    action: str

async def _run_action(name: str) -> None:
    pass

@app.post("/api/spa/on", response_model=ActionResult)
async def spa_on() -> ActionResult:
    await _run_action("spa-on")
    return ActionResult(ok=True, action="spa-on")

@app.post("/api/spa/off", response_model=ActionResult)
async def spa_off() -> ActionResult:
    await _run_action("spa-off")
    return ActionResult(ok=True, action="spa-off")

@app.post("/api/pool/on", response_model=ActionResult)
async def pool_on() -> ActionResult:
    await _run_action("pool-on")
    return ActionResult(ok=True, action="pool-on")

@app.post("/api/pool/off", response_model=ActionResult)
async def spa_off() -> ActionResult:
    await _run_action("pool-on")
    return ActionResult(ok=True, action="pool-on")

@app.get("/")
def read_root():
    return {"status": "success", "message": "FastAPI is running"}