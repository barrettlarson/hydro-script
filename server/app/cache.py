"""StateCache — latest system snapshot + failure observability.

Holds the most recent good status snapshot and a bounded, timestamped
history of failures classified by category. This is the observability
surface the health endpoint (and later, the frontend staleness
indicator) reads from.

Times are stored as epoch floats internally;
ISO conversion happens only at the API edge. A ``clock`` callable is
injectable so tests can control time without monkeypatching globals.
"""

from __future__ import annotations

import time
from collections import Counter, deque
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, Optional

from app.errors import FailureCategory

DEFAULT_HISTORY_SIZE = 50
DEFAULT_STALE_AFTER = 60.0  # seconds; a snapshot older than this is "stale"


@dataclass(frozen=True)
class FailureRecord:
    """A single classified failure, timestamped (epoch seconds)."""

    ts: float
    category: FailureCategory
    detail: str  # raw str(exc), for development — not shown to end users


class StateCache:
    """Latest snapshot + bounded failure history with health derivation."""

    def __init__(
        self,
        *,
        history_size: int = DEFAULT_HISTORY_SIZE,
        stale_after: float = DEFAULT_STALE_AFTER,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self.state: Optional[dict[str, Any]] = None
        self.last_success_at: float = 0.0
        self.last_attempt_at: float = 0.0
        self.consecutive_failures: int = 0
        self.stale_after = stale_after
        self._clock = clock
        self._history: deque[FailureRecord] = deque(maxlen=history_size)

    # recording

    def record_success(self, state: Optional[dict[str, Any]] = None) -> None:
        """Mark a successful round-trip.

        Pass `state` to also store a fresh snapshot (status calls).
        Actions that succeed without producing a snapshot can call with
        no argument: connectivity health is updated, `state` is kept.
        """
        now = self._clock()
        if state is not None:
            self.state = state
        self.last_success_at = now
        self.last_attempt_at = now
        self.consecutive_failures = 0

    def record_failure(self, category: FailureCategory, detail: str) -> FailureRecord:
        """Record a classified failure; returns the stored record."""
        now = self._clock()
        self.last_attempt_at = now
        self.consecutive_failures += 1
        record = FailureRecord(ts=now, category=category, detail=detail)
        self._history.append(record)
        return record

    # derived health

    def age_seconds(self) -> Optional[float]:
        """Seconds since the last successful snapshot, or None if never."""
        if self.last_success_at == 0.0:
            return None
        return self._clock() - self.last_success_at

    def is_stale(self) -> bool:
        """True if there's no recent success (never, or older than stale_after)."""
        age = self.age_seconds()
        return age is None or age > self.stale_after

    def status(self) -> str:
        """Coarse health: 'ok' | 'degraded' | 'down'.

        - down:     never succeeded (the system has never worked this run)
        - degraded: has succeeded before but is currently stale or failing
        - ok:       recent success and no active failure streak
        """
        if self.last_success_at == 0.0:
            return "down"
        if self.is_stale() or self.consecutive_failures > 0:
            return "degraded"
        return "ok"

    @property
    def recent_failures(self) -> list[FailureRecord]:
        """Failure history, oldest first."""
        return list(self._history)

    def failures_by_category(self) -> dict[str, int]:
        """Counts of recorded failures grouped by category value."""
        counts: Counter[str] = Counter(r.category.value for r in self._history)
        return dict(counts)

    def health(self) -> dict[str, Any]:
        """Health summary with epoch timestamps (ISO conversion at the edge)."""
        return {
            "status": self.status(),
            "is_stale": self.is_stale(),
            "last_success_at": self.last_success_at or None,
            "last_attempt_at": self.last_attempt_at or None,
            "age_seconds": self.age_seconds(),
            "consecutive_failures": self.consecutive_failures,
            "failures_by_category": self.failures_by_category(),
            "recent_failures": [
                {"ts": r.ts, "category": r.category.value, "detail": r.detail}
                for r in self._history
            ],
        }
