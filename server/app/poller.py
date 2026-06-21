"""Background poller — the single upstream reader.

One async loop polls Jandy on an interval and writes snapshots into the
shared :class:`StateCache`. Every HTTP read is then served from that cache,
so the number of clients never multiplies upstream load.

There is no overlap by construction: the loop awaits each poll to completion
before waiting out the interval, so polls never run concurrently. The interval
is the floor *between* polls.

The status fetcher is injected so the loop is testable without hardware: tests
pass a fake fetch and drive :meth:`Poller.poll_once` directly.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from typing import Any, Optional

from app.cache import StateCache
from app.errors import classify

logger = logging.getLogger(__name__)

#: Floor between upstream polls (seconds). Water temps change slowly; polling
#: Jandy more often than this buys nothing and risks rate limits.
POLL_INTERVAL = 30.0

#: Returns a fresh status snapshot, or raises on failure. No cache interaction.
StatusFetcher = Callable[[], Awaitable[dict[str, Any]]]


class Poller:
    """Owns the single background poll loop and records outcomes in the cache."""

    def __init__(
        self,
        cache: StateCache,
        fetch: StatusFetcher,
        *,
        interval: float = POLL_INTERVAL,
    ) -> None:
        self._cache = cache
        self._fetch = fetch
        self._interval = interval
        self._task: Optional[asyncio.Task[None]] = None
        self._stop = asyncio.Event()
        self._wake = asyncio.Event()

    async def poll_once(self) -> None:
        """Fetch one snapshot and record the outcome in the cache.

        Never raises: upstream failures are classified and recorded so the loop
        keeps running and the health surface reflects the failure. This is the
        same classify/record path the action endpoints use, so health has a
        single source of truth.
        """
        try:
            snapshot = await self._fetch()
        except Exception as exc:  # noqa: BLE001 - boundary: classify everything
            category = classify(exc)
            self._cache.record_failure(category, str(exc))
            logger.warning("poll failed: %s (%s)", category.value, exc)
        else:
            self._cache.record_success(snapshot)

    def request_refresh(self) -> None:
        """Ask the loop to poll now instead of waiting out the interval.

        Called after an action so the cache reflects the change quickly. Safe to
        call from any coroutine; it just sets an event the loop is waiting on.
        """
        self._wake.set()

    async def _run(self) -> None:
        # Immediate poll on startup so the cache isn't empty for first requests.
        await self.poll_once()
        while not self._stop.is_set():
            try:
                await asyncio.wait_for(self._wake.wait(), timeout=self._interval)
            except asyncio.TimeoutError:
                pass  # interval elapsed — normal periodic poll
            self._wake.clear()
            if self._stop.is_set():
                break
            await self.poll_once()

    def start(self) -> None:
        """Start the background loop. Idempotent."""
        if self._task is not None:
            return
        self._stop.clear()
        self._wake.clear()
        self._task = asyncio.create_task(self._run())

    async def stop(self) -> None:
        """Signal the loop to stop and wait for the task to finish."""
        self._stop.set()
        self._wake.set()  # break out of the interval wait promptly
        if self._task is not None:
            await self._task
            self._task = None
