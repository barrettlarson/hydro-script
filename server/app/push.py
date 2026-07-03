"""Web Push delivery: VAPID config, per-device subscription store, sender.

Configuration (``.env``):

- ``VAPID_PRIVATE_KEY`` — base64url raw EC private key. Generate a pair with
  ``python -m app.push`` (or ``just vapid-keys``). Push is disabled when unset.
- ``VAPID_SUBJECT`` — contact claim sent to push services; defaults to
  ``mailto:`` + IAQUALINK_USER.

Subscriptions persist to one JSON file so a server restart doesn't silently
drop everyone's notifications. Active heat *watches* are in-memory only (see
``watcher.py``) — a restart mid-heat loses the pending notification, which is
acceptable for now.
"""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional

logger = logging.getLogger(__name__)

VAPID_PRIVATE_KEY_ENV = "VAPID_PRIVATE_KEY"
VAPID_SUBJECT_ENV = "VAPID_SUBJECT"


def _b64url(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()


@dataclass(frozen=True)
class VapidConfig:
    """Server keypair identity for Web Push (RFC 8292)."""

    private_key: str  # base64url raw EC key, as pywebpush expects
    public_key: str  # base64url uncompressed point, as the browser expects
    subject: str  # "mailto:" contact claim sent to push services


def _public_key_from_private(private_key: str) -> str:
    """Derive the browser-facing applicationServerKey from the private key.

    Deriving (rather than storing both halves in .env) makes a mismatched
    pair impossible.
    """
    from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat
    from py_vapid import Vapid

    vapid = Vapid.from_string(private_key)
    raw = vapid.public_key.public_bytes(Encoding.X962, PublicFormat.UncompressedPoint)
    return _b64url(raw)


def load_vapid_config() -> Optional[VapidConfig]:
    """Build the VAPID config from the environment, or None if push is off."""
    private_key = os.environ.get(VAPID_PRIVATE_KEY_ENV)
    if not private_key:
        return None
    subject = os.environ.get(VAPID_SUBJECT_ENV)
    if not subject:
        subject = f"mailto:{os.environ.get('IAQUALINK_USER', 'admin@example.invalid')}"
    try:
        public_key = _public_key_from_private(private_key)
    except Exception:
        logger.exception("%s is set but invalid; push disabled", VAPID_PRIVATE_KEY_ENV)
        return None
    return VapidConfig(private_key=private_key, public_key=public_key, subject=subject)


def generate_vapid_keys() -> tuple[str, str]:
    """Return a fresh (private, public) VAPID pair as base64url strings."""
    from py_vapid import Vapid

    vapid = Vapid()
    vapid.generate_keys()
    raw_private = vapid.private_key.private_numbers().private_value.to_bytes(32, "big")
    private_key = _b64url(raw_private)
    return private_key, _public_key_from_private(private_key)


class SubscriptionStore:
    """Per-device Web Push subscriptions, persisted as one JSON file.

    Keyed by the opaque ``device_id`` carried in each browser's session
    cookie. Writes are atomic (temp file + replace) so a crash can't leave a
    truncated store. All access happens on the event loop thread, so no lock
    is needed.
    """

    def __init__(self, path: Path) -> None:
        self._path = path
        self._subs: dict[str, dict[str, Any]] = self._load()

    def _load(self) -> dict[str, dict[str, Any]]:
        try:
            data = json.loads(self._path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return {}
        except (OSError, json.JSONDecodeError):
            logger.warning("could not read %s; starting with no subscriptions", self._path)
            return {}
        return data if isinstance(data, dict) else {}

    def _save(self) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self._path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self._subs, indent=2), encoding="utf-8")
        os.replace(tmp, self._path)

    def get(self, device_id: str) -> Optional[dict[str, Any]]:
        return self._subs.get(device_id)

    def set(self, device_id: str, subscription: dict[str, Any]) -> None:
        """Store a device's subscription.

        A logout clears the session (and its device_id), so re-subscribing
        after re-login arrives under a new id with the same push endpoint —
        drop any old ids for that endpoint instead of accumulating orphans.
        """
        endpoint = subscription.get("endpoint")
        stale = [
            did
            for did, sub in self._subs.items()
            if did != device_id and sub.get("endpoint") == endpoint
        ]
        for did in stale:
            del self._subs[did]
        self._subs[device_id] = subscription
        self._save()

    def remove(self, device_id: str) -> bool:
        if device_id not in self._subs:
            return False
        del self._subs[device_id]
        self._save()
        return True


async def send_push(
    subscription: dict[str, Any],
    payload: dict[str, Any],
    vapid: VapidConfig,
    *,
    ttl: int = 3600,
) -> bool:
    """Deliver one push message. Returns False if the subscription is gone.

    A 404/410 from the push service means the browser revoked the
    subscription — the caller should prune it. Other failures raise.
    pywebpush is blocking (requests), so it runs in a thread.

    ``ttl`` is how long the push service queues the message for an offline
    device (pywebpush's default of 0 would drop it). Progress updates should
    pass something short — a stale "94° now" is worse than nothing.
    """
    from pywebpush import WebPushException, webpush

    def _post() -> None:
        webpush(
            subscription_info=subscription,
            data=json.dumps(payload),
            vapid_private_key=vapid.private_key,
            # pywebpush mutates the claims dict (adds aud/exp) — pass a fresh one
            vapid_claims={"sub": vapid.subject},
            ttl=ttl,
        )

    try:
        await asyncio.to_thread(_post)
    except WebPushException as exc:
        status = exc.response.status_code if exc.response is not None else None
        if status in (404, 410):
            return False
        raise
    return True


if __name__ == "__main__":  # pragma: no cover - tiny setup utility
    private, public = generate_vapid_keys()
    print("# Add to .env (public key is derived from private at startup):")
    print(f"{VAPID_PRIVATE_KEY_ENV}={private}")
    print(f"# {VAPID_SUBJECT_ENV}=mailto:you@example.com  (optional)")
    print(f"# derived public key (for reference): {public}")
