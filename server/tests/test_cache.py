"""Tests for StateCache observability behavior."""

from app.cache import StateCache
from app.errors import FailureCategory


class FakeClock:
    """Controllable monotonic-ish clock for deterministic time tests."""

    def __init__(self, start: float = 1000.0) -> None:
        self.now = start

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


def make_cache(**kwargs) -> tuple[StateCache, FakeClock]:
    clock = FakeClock()
    cache = StateCache(clock=clock, **kwargs)
    return cache, clock


class TestRecording:
    def test_starts_down_with_no_history(self):
        cache, _ = make_cache()
        assert cache.status() == "down"
        assert cache.state is None
        assert cache.is_stale()
        assert cache.recent_failures == []

    def test_success_stores_snapshot_and_resets_streak(self):
        cache, _ = make_cache()
        cache.record_failure(FailureCategory.NETWORK, "blip")
        cache.record_failure(FailureCategory.NETWORK, "blip")
        assert cache.consecutive_failures == 2

        cache.record_success({"devices": {}})
        assert cache.consecutive_failures == 0
        assert cache.state == {"devices": {}}
        assert cache.status() == "ok"

    def test_success_without_state_keeps_prior_snapshot(self):
        cache, _ = make_cache()
        cache.record_success({"devices": {"spa": "on"}})
        cache.record_success()  # action success, no fresh snapshot
        assert cache.state == {"devices": {"spa": "on"}}

    def test_failure_increments_streak_and_history(self):
        cache, _ = make_cache()
        cache.record_failure(FailureCategory.RATE_LIMIT, "429")
        cache.record_failure(FailureCategory.UPSTREAM_OFFLINE, "down")
        assert cache.consecutive_failures == 2
        assert len(cache.recent_failures) == 2
        assert cache.recent_failures[0].category == FailureCategory.RATE_LIMIT


class TestHistoryBounds:
    def test_history_respects_maxlen(self):
        cache, _ = make_cache(history_size=3)
        for i in range(5):
            cache.record_failure(FailureCategory.NETWORK, f"err{i}")
        records = cache.recent_failures
        assert len(records) == 3
        # oldest dropped; newest kept, oldest-first ordering preserved
        assert [r.detail for r in records] == ["err2", "err3", "err4"]

    def test_failures_by_category_counts(self):
        cache, _ = make_cache()
        cache.record_failure(FailureCategory.NETWORK, "a")
        cache.record_failure(FailureCategory.NETWORK, "b")
        cache.record_failure(FailureCategory.AUTH, "c")
        assert cache.failures_by_category() == {"network": 2, "auth": 1}


class TestStaleness:
    def test_fresh_success_is_not_stale(self):
        cache, clock = make_cache(stale_after=60.0)
        cache.record_success({"ok": True})
        clock.advance(30)
        assert not cache.is_stale()
        assert cache.age_seconds() == 30
        assert cache.status() == "ok"

    def test_old_success_goes_stale_and_degraded(self):
        cache, clock = make_cache(stale_after=60.0)
        cache.record_success({"ok": True})
        clock.advance(90)
        assert cache.is_stale()
        assert cache.status() == "degraded"

    def test_failure_after_success_is_degraded_not_down(self):
        cache, _ = make_cache()
        cache.record_success({"ok": True})
        cache.record_failure(FailureCategory.NETWORK, "blip")
        assert cache.status() == "degraded"

    def test_age_is_none_before_first_success(self):
        cache, _ = make_cache()
        assert cache.age_seconds() is None


class TestHealthSummary:
    def test_health_shape(self):
        cache, clock = make_cache()
        cache.record_success({"devices": {}})
        clock.advance(5)
        cache.record_failure(FailureCategory.RATE_LIMIT, "429 throttle")

        h = cache.health()
        assert h["status"] == "degraded"
        assert h["consecutive_failures"] == 1
        assert h["failures_by_category"] == {"rate_limit": 1}
        assert h["last_success_at"] == 1000.0  # epoch float, not yet ISO
        assert h["recent_failures"][0]["category"] == "rate_limit"
        assert h["recent_failures"][0]["detail"] == "429 throttle"
