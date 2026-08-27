"""Push plumbing tests: subscription store persistence, VAPID config, sender."""

from pathlib import Path
from typing import Any

import pytest

from app import push
from app.store import SUBSCRIPTIONS_KEY, FileStore, MemoryStore

SUB_A = {"endpoint": "https://push.example/a", "keys": {"p256dh": "pk", "auth": "au"}}
SUB_B = {"endpoint": "https://push.example/b", "keys": {"p256dh": "pk", "auth": "au"}}


class TestSubscriptionStore:
    def test_set_get_roundtrip_persists_across_instances(self, tmp_path: Path) -> None:
        push.SubscriptionStore(FileStore(tmp_path)).set("dev-1", SUB_A)
        # a fresh store instance simulates a restart / a cold Lambda
        assert push.SubscriptionStore(FileStore(tmp_path)).get("dev-1") == SUB_A

    def test_remove_persists(self, tmp_path: Path) -> None:
        store = push.SubscriptionStore(FileStore(tmp_path))
        store.set("dev-1", SUB_A)
        assert store.remove("dev-1") is True
        assert store.remove("dev-1") is False  # already gone
        assert push.SubscriptionStore(FileStore(tmp_path)).get("dev-1") is None

    def test_missing_document_means_empty_store(self, tmp_path: Path) -> None:
        assert push.SubscriptionStore(FileStore(tmp_path)).get("dev-1") is None

    def test_corrupt_document_is_tolerated(self, tmp_path: Path) -> None:
        (tmp_path / f"{SUBSCRIPTIONS_KEY}.json").write_text("{not json", encoding="utf-8")
        store = push.SubscriptionStore(FileStore(tmp_path))
        assert store.get("dev-1") is None
        store.set("dev-1", SUB_A)  # and it can still save afterwards
        assert push.SubscriptionStore(FileStore(tmp_path)).get("dev-1") == SUB_A

    def test_reads_through_so_a_warm_instance_sees_new_subscriptions(self) -> None:
        # Two SubscriptionStores over one backend stand in for two Lambda
        # execution environments: one must not serve a stale cached map.
        backend = MemoryStore()
        a, b = push.SubscriptionStore(backend), push.SubscriptionStore(backend)
        assert a.get("dev-1") is None  # a has now "seen" an empty store
        b.set("dev-1", SUB_A)
        assert a.get("dev-1") == SUB_A

    def test_resubscribe_after_relogin_drops_the_orphaned_id(self, tmp_path: Path) -> None:
        # logout clears the session, so the same browser re-subscribes under a
        # new device id with the same push endpoint
        store = push.SubscriptionStore(FileStore(tmp_path))
        store.set("old-id", SUB_A)
        store.set("new-id", SUB_A)
        assert store.get("old-id") is None
        assert store.get("new-id") == SUB_A

    def test_distinct_endpoints_coexist(self, tmp_path: Path) -> None:
        store = push.SubscriptionStore(FileStore(tmp_path))
        store.set("dev-1", SUB_A)
        store.set("dev-2", SUB_B)
        assert store.get("dev-1") == SUB_A
        assert store.get("dev-2") == SUB_B


class TestVapidConfig:
    def test_unset_env_disables_push(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv(push.VAPID_PRIVATE_KEY_ENV, raising=False)
        assert push.load_vapid_config() is None

    def test_garbage_key_disables_push(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv(push.VAPID_PRIVATE_KEY_ENV, "not-a-key")
        assert push.load_vapid_config() is None

    def test_generated_keys_load_and_derive_public(self, monkeypatch: pytest.MonkeyPatch) -> None:
        private, public = push.generate_vapid_keys()
        monkeypatch.setenv(push.VAPID_PRIVATE_KEY_ENV, private)
        monkeypatch.setenv(push.VAPID_SUBJECT_ENV, "mailto:owner@example.com")
        config = push.load_vapid_config()
        assert config is not None
        assert config.public_key == public
        assert config.subject == "mailto:owner@example.com"

    def test_subject_defaults_to_iaqualink_user(self, monkeypatch: pytest.MonkeyPatch) -> None:
        private, _ = push.generate_vapid_keys()
        monkeypatch.setenv(push.VAPID_PRIVATE_KEY_ENV, private)
        monkeypatch.delenv(push.VAPID_SUBJECT_ENV, raising=False)
        monkeypatch.setenv("IAQUALINK_USER", "owner@example.com")
        config = push.load_vapid_config()
        assert config is not None
        assert config.subject == "mailto:owner@example.com"


class FakeResponse:
    def __init__(self, status_code: int) -> None:
        self.status_code = status_code


VAPID = push.VapidConfig(private_key="priv", public_key="pub", subject="mailto:t@example.com")


class TestSendPush:
    async def test_success_returns_true(self, monkeypatch: pytest.MonkeyPatch) -> None:
        import pywebpush

        calls: list[dict[str, Any]] = []
        monkeypatch.setattr(pywebpush, "webpush", lambda **kw: calls.append(kw))
        assert await push.send_push(SUB_A, {"title": "hi"}, VAPID, ttl=120) is True
        assert calls[0]["subscription_info"] == SUB_A
        assert calls[0]["ttl"] == 120
        assert calls[0]["vapid_claims"] == {"sub": VAPID.subject}

    async def test_gone_subscription_returns_false(self, monkeypatch: pytest.MonkeyPatch) -> None:
        import pywebpush

        def gone(**_kw: Any) -> None:
            raise pywebpush.WebPushException("gone", response=FakeResponse(410))

        monkeypatch.setattr(pywebpush, "webpush", gone)
        assert await push.send_push(SUB_A, {"title": "hi"}, VAPID) is False

    async def test_other_push_errors_raise(self, monkeypatch: pytest.MonkeyPatch) -> None:
        import pywebpush

        def boom(**_kw: Any) -> None:
            raise pywebpush.WebPushException("server error", response=FakeResponse(500))

        monkeypatch.setattr(pywebpush, "webpush", boom)
        with pytest.raises(pywebpush.WebPushException):
            await push.send_push(SUB_A, {"title": "hi"}, VAPID)
