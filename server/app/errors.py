"""Error taxonomy + classifier for iAquaLink failures.

Pure logic, no I/O. Maps raw exceptions onto a small set of failure
*categories* so the rest of the system can reason about failures
("transient blip" vs. "something is actually broken") instead of
matching on stringified messages.

Per the project's design principles: we *classify and record* now;
we do NOT build reactions (backoff, alerting) here. Reactions wait
until real failures have been observed on hardware, at which point
``classify`` can be tightened. ``UNKNOWN`` is a first-class outcome.
"""

from __future__ import annotations

import asyncio
from enum import Enum

from iaqualink.exception import (
    AqualinkDeviceNotSupported,
    AqualinkException,
    AqualinkInvalidParameterException,
    AqualinkOperationNotSupportedException,
    AqualinkServiceException,
    AqualinkServiceUnauthorizedException,
    AqualinkSystemOfflineException,
    AqualinkSystemUnsupportedException,
)

from app.aqualink import (
    DeviceNotFound,
    MissingCredentials,
    NoSystemFound,
)

try:  # httpx is a transitive dep of iaqualink; guard just in case.
    import httpx

    _TRANSPORT_ERRORS: tuple[type[BaseException], ...] = (httpx.TransportError,)
except ImportError:  # pragma: no cover - httpx is always present in practice
    _TRANSPORT_ERRORS = ()


class FailureCategory(str, Enum):
    """A coarse classification of why a call to Jandy failed.

    str-valued so it serializes cleanly to JSON at the API edge.
    """

    AUTH = "auth"  # bad/expired/missing credentials — config, not transient
    RATE_LIMIT = "rate_limit"  # 429 throttling — transient (lib usually absorbs)
    UPSTREAM_OFFLINE = "upstream_offline"  # Jandy cloud / controller down — transient
    NETWORK = "network"  # connect/timeout reaching the service — transient
    CONFIG = "config"  # our setup is wrong (missing device, no system)
    UNKNOWN = "unknown"  # unclassified — flag it, don't pretend to know


#: Categories that represent "try again later" conditions rather than
#: "someone needs to fix something." Used by health reporting now and by
#: backoff/retry logic later.
TRANSIENT: frozenset[FailureCategory] = frozenset(
    {
        FailureCategory.RATE_LIMIT,
        FailureCategory.UPSTREAM_OFFLINE,
        FailureCategory.NETWORK,
    }
)


def is_transient(category: FailureCategory) -> bool:
    """True if the category is a retryable/transient condition."""
    return category in TRANSIENT


def classify(exc: BaseException) -> FailureCategory:
    """Map an exception onto a FailureCategory.

    Ordering matters: more specific subclasses are checked before their
    bases (e.g. Unauthorized/SystemOffline before AqualinkServiceException).
    """
    # --- Our own setup errors (raised by aqualink.py) ---------------
    if isinstance(exc, MissingCredentials):
        return FailureCategory.AUTH
    if isinstance(exc, (NoSystemFound, DeviceNotFound)):
        return FailureCategory.CONFIG

    # --- iAquaLink library errors (subclass-first) ------------------
    if isinstance(exc, AqualinkServiceUnauthorizedException):
        return FailureCategory.AUTH
    if isinstance(exc, AqualinkSystemOfflineException):
        return FailureCategory.UPSTREAM_OFFLINE
    if isinstance(exc, AqualinkServiceException):
        # No status code is carried on the exception, and the library
        # absorbs 429s internally, so this is best-effort. Treat an
        # explicit rate-limit signal as transient; everything else as
        # an upstream/service problem.
        text = str(exc).lower()
        if "429" in text or "rate limit" in text or "too many requests" in text:
            return FailureCategory.RATE_LIMIT
        return FailureCategory.UPSTREAM_OFFLINE
    if isinstance(
        exc,
        (
            AqualinkDeviceNotSupported,
            AqualinkInvalidParameterException,
            AqualinkOperationNotSupportedException,
            AqualinkSystemUnsupportedException,
        ),
    ):
        # We asked the system to do something it can't — a usage/setup issue.
        return FailureCategory.CONFIG

    # --- Transport / network ----------------------------------------
    if isinstance(exc, (asyncio.TimeoutError, ConnectionError, *_TRANSPORT_ERRORS)):
        return FailureCategory.NETWORK

    # --- Anything else (incl. unmapped AqualinkException) -----------
    if isinstance(exc, AqualinkException):
        return FailureCategory.UNKNOWN
    return FailureCategory.UNKNOWN


#: category -> (http_status, public_message). The public message is what a
#: caller/end-user sees; the real ``str(exc)`` is recorded in the cache for
#: development. We deliberately hide internal plumbing (rate limiting) behind
#: a generic message, while surfacing conditions a user can understand
#: (network down, service unavailable).
_HTTP_RESPONSES: dict[FailureCategory, tuple[int, str]] = {
    FailureCategory.AUTH: (500, "Service authentication failed. Check server configuration."),
    FailureCategory.RATE_LIMIT: (503, "Service temporarily unavailable. Please try again shortly."),
    FailureCategory.UPSTREAM_OFFLINE: (
        502,
        "The pool controller or iAquaLink service is currently unavailable.",
    ),
    FailureCategory.NETWORK: (503, "Unable to reach the iAquaLink service (network error)."),
    FailureCategory.CONFIG: (500, "System configuration error."),
    FailureCategory.UNKNOWN: (500, "An unexpected error occurred."),
}


def http_response(category: FailureCategory) -> tuple[int, str]:
    """Return (status_code, public_message) for a failure category."""
    return _HTTP_RESPONSES[category]
