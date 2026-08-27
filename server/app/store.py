"""Persistence for state that must outlive a single process.

Everything here exists because of one Lambda fact: the execution environment
is *frozen* between invocations and there may be several of them. Anything
held in module globals — the status snapshot, the heat watches, the push
subscriptions — is per-instance and evaporates. So the state moves out of the
process and the pure logic stays where it is.

The interface is deliberately tiny: a key -> JSON document map. Three
documents exist (``cache``, ``watches``, ``subscriptions``), each small enough
to read and write whole, which keeps the backends trivial and means
:class:`StateCache` and :class:`HeatWatcher` only need to grow a
``to_doc``/``load_doc`` pair rather than a storage-shaped API.

Backends:

- :class:`MemoryStore` — tests, and any single-process run that doesn't care
  about restarts.
- :class:`FileStore` — local dev. One JSON file per key under ``.data/``,
  written atomically, matching what the app did before this module existed.
- :class:`DynamoStore` — Lambda. One item per key.

Concurrency is last-write-wins. That is a deliberate call, not an oversight:
the poll function runs at reserved concurrency 1 (so nothing races it on the
watches document, the only one mutated on every tick), and the remaining
writers are a household's worth of button presses. A compare-and-swap would
buy correctness nobody here can observe.
"""

from __future__ import annotations

import json
import logging
import os
from copy import deepcopy
from pathlib import Path
from typing import Any, Optional, Protocol

logger = logging.getLogger(__name__)

#: Document keys. One per piece of cross-invocation state.
CACHE_KEY = "cache"
WATCHES_KEY = "watches"
SUBSCRIPTIONS_KEY = "subscriptions"

#: Env vars selecting and configuring the backend.
BACKEND_ENV = "STATE_BACKEND"  # "memory" | "file" | "dynamodb"
TABLE_ENV = "STATE_TABLE"  # DynamoDB table name
DATA_DIR_ENV = "STATE_DIR"  # FileStore directory


class DocumentStore(Protocol):
    """A key -> JSON document map. Documents are read and written whole."""

    def get(self, key: str) -> Optional[dict[str, Any]]:
        """Return the document, or None if absent."""
        ...

    def put(self, key: str, doc: dict[str, Any]) -> None:
        """Write the document, replacing any previous value."""
        ...

    def delete(self, key: str) -> None:
        """Remove the document. Absent keys are not an error."""
        ...


class MemoryStore:
    """In-process store. Copies on the way in and out so callers can't alias."""

    def __init__(self) -> None:
        self._docs: dict[str, dict[str, Any]] = {}

    def get(self, key: str) -> Optional[dict[str, Any]]:
        doc = self._docs.get(key)
        return deepcopy(doc) if doc is not None else None

    def put(self, key: str, doc: dict[str, Any]) -> None:
        self._docs[key] = deepcopy(doc)

    def delete(self, key: str) -> None:
        self._docs.pop(key, None)


class FileStore:
    """One JSON file per key in a directory. Writes are atomic.

    A corrupt or unreadable file reads as absent rather than raising: losing a
    cached snapshot should degrade the health surface, not refuse to boot.
    """

    def __init__(self, directory: Path) -> None:
        self._dir = directory

    def _path(self, key: str) -> Path:
        return self._dir / f"{key}.json"

    def get(self, key: str) -> Optional[dict[str, Any]]:
        try:
            data = json.loads(self._path(key).read_text(encoding="utf-8"))
        except FileNotFoundError:
            return None
        except (OSError, json.JSONDecodeError):
            logger.warning("could not read %s; treating as empty", self._path(key))
            return None
        return data if isinstance(data, dict) else None

    def put(self, key: str, doc: dict[str, Any]) -> None:
        path = self._path(key)
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(doc, indent=2), encoding="utf-8")
        os.replace(tmp, path)

    def delete(self, key: str) -> None:
        self._path(key).unlink(missing_ok=True)


class DynamoStore:
    """One DynamoDB item per key, with the document as a JSON string.

    The document is stored as a single JSON *string* attribute rather than a
    marshalled map. That is intentional: DynamoDB has no float type, so a
    nested map round-trips epoch timestamps through ``Decimal`` and hands back
    values that aren't the floats the rest of the code stores (see the design
    principle on epoch floats). A JSON blob keeps types exactly as written,
    and these documents are far too small for the loss of queryability to
    matter — nothing ever queries *into* them.
    """

    #: Partition key attribute name; matches the SAM template.
    PK = "pk"
    DOC = "doc"

    def __init__(self, table_name: str, *, client: Optional[Any] = None) -> None:
        self._table_name = table_name
        self._client = client

    @property
    def client(self) -> Any:
        """Lazily built boto3 client, so importing this module needs no AWS."""
        if self._client is None:
            import boto3

            self._client = boto3.client("dynamodb")
        return self._client

    def get(self, key: str) -> Optional[dict[str, Any]]:
        response = self.client.get_item(
            TableName=self._table_name,
            Key={self.PK: {"S": key}},
            ConsistentRead=True,  # a poll must not read its own stale write
        )
        item = response.get("Item")
        if not item:
            return None
        raw = item.get(self.DOC, {}).get("S")
        if not raw:
            return None
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            logger.warning("item %r holds invalid JSON; treating as empty", key)
            return None
        return data if isinstance(data, dict) else None

    def put(self, key: str, doc: dict[str, Any]) -> None:
        self.client.put_item(
            TableName=self._table_name,
            Item={self.PK: {"S": key}, self.DOC: {"S": json.dumps(doc)}},
        )

    def delete(self, key: str) -> None:
        self.client.delete_item(TableName=self._table_name, Key={self.PK: {"S": key}})


def default_data_dir() -> Path:
    """Repo-local ``.data/`` — the directory the file backend uses by default."""
    return Path(__file__).resolve().parents[2] / ".data"


def get_store() -> DocumentStore:
    """Build the backend the environment asks for.

    Explicit ``STATE_BACKEND`` wins. Otherwise the presence of ``STATE_TABLE``
    implies DynamoDB (that env var is only ever set by the deployed stack), so
    a developer running ``just server`` gets the file backend with no config
    and Lambda gets DynamoDB without a second switch to forget.
    """
    backend = os.environ.get(BACKEND_ENV, "").strip().lower()
    table = os.environ.get(TABLE_ENV, "").strip()
    if not backend:
        backend = "dynamodb" if table else "file"

    if backend == "memory":
        return MemoryStore()
    if backend == "dynamodb":
        if not table:
            raise ValueError(f"{BACKEND_ENV}=dynamodb requires {TABLE_ENV} to be set.")
        return DynamoStore(table)
    if backend == "file":
        directory = os.environ.get(DATA_DIR_ENV)
        return FileStore(Path(directory) if directory else default_data_dir())
    raise ValueError(f"Unknown {BACKEND_ENV}: {backend!r} (memory|file|dynamodb)")
