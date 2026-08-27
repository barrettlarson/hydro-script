"""AWS Lambda entry points — two functions, one artifact.

`cli.py` adapts the pure logic to a terminal; this adapts it to Lambda. Both
stay thin: no control logic lives here, only the translation between an
invocation and a call into the app.

Deployed as two functions off the same zip, differing only in ``Handler``:

- ``app.handlers.http_handler`` — the FastAPI app behind a Function URL.
- ``app.handlers.poll_handler`` — the EventBridge-driven poll, plus the
  nightly safety shutoff (distinguished by ``{"action": "safety"}``).

**Lifespan is off** on the HTTP handler. FastAPI's lifespan would start the
background poller, and a Lambda execution environment is frozen between
invocations — the loop would stop mid-``await`` and never reliably resume. The
poll function exists precisely because that background loop cannot.

Polling policy: a scheduled tick only reaches upstream when a heat watch is
active. Nothing else needs a clock — `/api/status` refreshes on demand when
its snapshot has aged out (see ``main.ensure_fresh_snapshot``), so an idle
system with nobody watching makes no upstream calls at all. The cost of that
is no passive health signal while idle; the first request after a quiet period
pays one poll to find out how things are.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any, Optional

from mangum import Mangum

from app import controls, main

logger = logging.getLogger(__name__)

#: Lambda's entry point for HTTP. Function URL and API Gateway payloads are
#: both understood by Mangum; lifespan is off (see the module docstring).
http_handler = Mangum(main.app, lifespan="off")


async def _run_safety() -> list[str]:
    """Open a connection and run the nightly shutoff.

    Reached through ``main``'s module attributes rather than importing the
    connection layer directly, so the one seam the test suite fakes
    (``main.AqualinkClient`` / ``main.open_devices``) covers this path too.
    """
    user, pw = main.get_credentials()
    async with main.AqualinkClient(user, pw) as client:
        devices = await main.open_devices(client)
        return await controls.cmd_safety(devices)


async def _safety() -> dict[str, Any]:
    messages = await _run_safety()
    # Everything is off; any pending "your spa is ready" would be a lie.
    main.watcher.cancel_all()
    main.save_state()
    return {"ok": True, "action": "safety", "messages": messages}


async def _poll() -> dict[str, Any]:
    """Poll upstream, but only when someone is waiting on the result."""
    if not main.watcher.any_active():
        logger.info("no active heat watch; skipping upstream poll")
        return {"ok": True, "action": "poll", "polled": False}
    # poll_once never raises: failures are classified into the cache, and its
    # on_cycle hook persists the outcome either way.
    await main.poller.poll_once()
    return {"ok": True, "action": "poll", "polled": True}


def poll_handler(event: Optional[dict[str, Any]], context: Any = None) -> dict[str, Any]:
    """Scheduled entry point: routine poll, or the nightly safety shutoff.

    EventBridge sends the constant input configured on the rule, so the two
    schedules are told apart by ``action`` rather than by having two more
    functions to deploy and keep in sync.
    """
    main.load_state()  # module globals carry nothing across invocations
    action = (event or {}).get("action", "poll")
    if action == "safety":
        return asyncio.run(_safety())
    if action != "poll":
        raise ValueError(f"unknown scheduled action: {action!r}")
    return asyncio.run(_poll())
