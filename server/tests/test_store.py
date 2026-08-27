"""Tests for the persistence layer: backends, selection, and round-trips.

The DynamoDB backend is exercised against a fake client rather than moto —
the surface we depend on is three calls wide, and a fake keeps the suite
dependency-free and instant, matching how the rest of the suite fakes
hardware.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from app.cache import DEFAULT_HISTORY_SIZE, StateCache
from app.errors import FailureCategory
from app.store import (
    BACKEND_ENV,
    CACHE_KEY,
    DATA_DIR_ENV,
    TABLE_ENV,
    DocumentStore,
    DynamoStore,
    FileStore,
    MemoryStore,
    get_store,
)
from app.watcher import HeatWatcher

DOC = {"a": 1, "nested": {"b": [1, 2, 3]}, "f": 1.5}

SPA_HEATING = {
    "devices": {"spa_heater": {"label": "ON"}, "spa_set_point": {"state": "102"}},
    "temps": {"spa": 96.4},
}


class FakeDynamoClient:
    """The three DynamoDB calls DynamoStore makes, over a dict."""

    def __init__(self) -> None:
        self.items: dict[str, dict[str, Any]] = {}
        self.consistent_reads: list[bool] = []

    def get_item(
        self, TableName: str, Key: dict[str, Any], ConsistentRead: bool = False
    ) -> dict[str, Any]:
        self.consistent_reads.append(ConsistentRead)
        item = self.items.get(Key["pk"]["S"])
        return {"Item": item} if item else {}

    def put_item(self, TableName: str, Item: dict[str, Any]) -> None:
        self.items[Item["pk"]["S"]] = Item

    def delete_item(self, TableName: str, Key: dict[str, Any]) -> None:
        self.items.pop(Key["pk"]["S"], None)


def backends(tmp_path: Path) -> list[DocumentStore]:
    """Every backend, so the contract tests run against all of them."""
    return [
        MemoryStore(),
        FileStore(tmp_path),
        DynamoStore("table", client=FakeDynamoClient()),
    ]


class TestBackendContract:
    """Behavior every backend must share — the interface callers rely on."""

    def test_missing_key_is_none(self, tmp_path: Path) -> None:
        for store in backends(tmp_path):
            assert store.get("nope") is None

    def test_put_get_roundtrip_preserves_types(self, tmp_path: Path) -> None:
        for store in backends(tmp_path):
            store.put(CACHE_KEY, DOC)
            assert store.get(CACHE_KEY) == DOC

    def test_put_replaces(self, tmp_path: Path) -> None:
        for store in backends(tmp_path):
            store.put(CACHE_KEY, {"a": 1})
            store.put(CACHE_KEY, {"b": 2})
            assert store.get(CACHE_KEY) == {"b": 2}

    def test_delete_is_idempotent(self, tmp_path: Path) -> None:
        for store in backends(tmp_path):
            store.put(CACHE_KEY, DOC)
            store.delete(CACHE_KEY)
            store.delete(CACHE_KEY)  # absent is not an error
            assert store.get(CACHE_KEY) is None


class TestMemoryStore:
    def test_copies_so_callers_cannot_alias(self) -> None:
        store = MemoryStore()
        doc: dict[str, Any] = {"nested": {"n": 1}}
        store.put(CACHE_KEY, doc)
        doc["nested"]["n"] = 99  # mutating the original must not reach the store
        got = store.get(CACHE_KEY)
        assert got == {"nested": {"n": 1}}
        assert got is not None
        got["nested"]["n"] = 77  # nor must mutating what we got back
        assert store.get(CACHE_KEY) == {"nested": {"n": 1}}


class TestFileStore:
    def test_corrupt_file_reads_as_absent(self, tmp_path: Path) -> None:
        (tmp_path / f"{CACHE_KEY}.json").write_text("{not json", encoding="utf-8")
        assert FileStore(tmp_path).get(CACHE_KEY) is None

    def test_non_object_json_reads_as_absent(self, tmp_path: Path) -> None:
        (tmp_path / f"{CACHE_KEY}.json").write_text("[1, 2]", encoding="utf-8")
        assert FileStore(tmp_path).get(CACHE_KEY) is None

    def test_creates_directory_on_write(self, tmp_path: Path) -> None:
        nested = tmp_path / "does" / "not" / "exist"
        FileStore(nested).put(CACHE_KEY, DOC)
        assert FileStore(nested).get(CACHE_KEY) == DOC

    def test_leaves_no_temp_file_behind(self, tmp_path: Path) -> None:
        FileStore(tmp_path).put(CACHE_KEY, DOC)
        assert [p.name for p in tmp_path.iterdir()] == [f"{CACHE_KEY}.json"]


class TestDynamoStore:
    def test_stores_document_as_json_string(self) -> None:
        client = FakeDynamoClient()
        DynamoStore("table", client=client).put(CACHE_KEY, DOC)
        assert json.loads(client.items[CACHE_KEY]["doc"]["S"]) == DOC

    def test_floats_survive_the_roundtrip(self) -> None:
        # The reason documents are stored as JSON strings: DynamoDB has no
        # float type, so a marshalled map hands back Decimals and epoch
        # timestamps stop being the floats the rest of the code stores.
        store = DynamoStore("table", client=FakeDynamoClient())
        store.put(CACHE_KEY, {"ts": 1712345678.25})
        value = (store.get(CACHE_KEY) or {})["ts"]
        assert isinstance(value, float)
        assert value == 1712345678.25

    def test_reads_are_strongly_consistent(self) -> None:
        # A poll must never read back its own stale write.
        client = FakeDynamoClient()
        DynamoStore("table", client=client).get(CACHE_KEY)
        assert client.consistent_reads == [True]

    def test_invalid_stored_json_reads_as_absent(self) -> None:
        client = FakeDynamoClient()
        client.items[CACHE_KEY] = {"pk": {"S": CACHE_KEY}, "doc": {"S": "{not json"}}
        assert DynamoStore("table", client=client).get(CACHE_KEY) is None


class TestGetStore:
    def test_defaults_to_file_backend(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv(BACKEND_ENV, raising=False)
        monkeypatch.delenv(TABLE_ENV, raising=False)
        assert isinstance(get_store(), FileStore)

    def test_table_env_implies_dynamodb(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # The deployed stack sets STATE_TABLE; nothing else does, so Lambda
        # gets DynamoDB without a second switch to forget.
        monkeypatch.delenv(BACKEND_ENV, raising=False)
        monkeypatch.setenv(TABLE_ENV, "hydro-state")
        assert isinstance(get_store(), DynamoStore)

    def test_explicit_backend_wins(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv(BACKEND_ENV, "memory")
        monkeypatch.setenv(TABLE_ENV, "hydro-state")
        assert isinstance(get_store(), MemoryStore)

    def test_file_backend_honors_data_dir(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        monkeypatch.setenv(BACKEND_ENV, "file")
        monkeypatch.setenv(DATA_DIR_ENV, str(tmp_path))
        store = get_store()
        store.put(CACHE_KEY, DOC)
        assert (tmp_path / f"{CACHE_KEY}.json").exists()

    def test_dynamodb_without_a_table_is_a_config_error(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv(BACKEND_ENV, "dynamodb")
        monkeypatch.delenv(TABLE_ENV, raising=False)
        with pytest.raises(ValueError, match=TABLE_ENV):
            get_store()

    def test_unknown_backend_is_a_config_error(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv(BACKEND_ENV, "postgres")
        with pytest.raises(ValueError, match="postgres"):
            get_store()


class TestCacheRoundTrip:
    def test_snapshot_and_health_survive(self) -> None:
        cache = StateCache()
        cache.record_success({"devices": {"spa_pump": {"label": "ON"}}})
        cache.record_failure(FailureCategory.NETWORK, "no route to host")

        restored = StateCache()
        restored.load_doc(cache.to_doc())

        assert restored.state == cache.state
        assert restored.last_success_at == cache.last_success_at
        assert restored.last_attempt_at == cache.last_attempt_at
        assert restored.consecutive_failures == 1
        assert [(r.category, r.detail) for r in restored.recent_failures] == [
            (FailureCategory.NETWORK, "no route to host")
        ]

    def test_empty_document_leaves_cache_untouched(self) -> None:
        cache = StateCache()
        cache.record_success({"devices": {}})
        cache.load_doc(None)
        assert cache.state == {"devices": {}}

    def test_unknown_category_degrades_rather_than_raising(self) -> None:
        cache = StateCache()
        cache.load_doc({"history": [{"ts": 1.0, "category": "sharknado", "detail": "?"}]})
        assert cache.recent_failures[0].category == FailureCategory.UNKNOWN

    def test_malformed_history_entries_are_skipped(self) -> None:
        cache = StateCache()
        cache.load_doc({"history": ["not a dict", None]})
        assert cache.recent_failures == []

    def test_load_respects_the_history_bound(self) -> None:
        cache = StateCache()
        overflowing = [
            {"ts": float(i), "category": "network", "detail": str(i)}
            for i in range(DEFAULT_HISTORY_SIZE + 10)
        ]
        cache.load_doc({"history": overflowing})
        assert len(cache.recent_failures) == DEFAULT_HISTORY_SIZE
        assert cache.recent_failures[-1].detail == str(DEFAULT_HISTORY_SIZE + 9)

    def test_state_is_reset_when_the_document_has_none(self) -> None:
        cache = StateCache()
        cache.record_success({"devices": {}})
        cache.load_doc({"last_success_at": 5.0})
        assert cache.state is None


class TestWatcherRoundTrip:
    def test_active_watch_survives(self) -> None:
        watcher = HeatWatcher(clock=lambda: 100.0)
        watcher.start("spa", "dev-1")
        watcher.evaluate(SPA_HEATING)  # emits 96, latching last_degree

        restored = HeatWatcher(clock=lambda: 100.0)
        restored.load_doc(watcher.to_doc())
        assert restored.watching("spa")
        # last_degree carried over, so the same degree does not re-notify
        assert restored.evaluate(SPA_HEATING) == []

    def test_load_replaces_existing_watches(self) -> None:
        watcher = HeatWatcher()
        watcher.start("pool", "dev-1")
        watcher.load_doc({})
        assert not watcher.watching("pool")

    def test_unknown_zone_is_dropped(self) -> None:
        watcher = HeatWatcher()
        watcher.load_doc({"jacuzzi": {"device_id": "dev-1", "started_at": 1.0}})
        assert watcher.to_doc() == {}

    def test_malformed_entry_is_dropped(self) -> None:
        watcher = HeatWatcher()
        watcher.load_doc({"spa": {"started_at": 1.0}, "pool": "nonsense"})
        assert watcher.to_doc() == {}

    def test_null_last_degree_roundtrips(self) -> None:
        watcher = HeatWatcher(clock=lambda: 42.0)
        watcher.start("spa", "dev-1")
        restored = HeatWatcher()
        restored.load_doc(watcher.to_doc())
        assert restored.to_doc()["spa"] == {
            "device_id": "dev-1",
            "started_at": 42.0,
            "last_degree": None,
        }
