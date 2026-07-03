"""Session auth: one shared login checked against the server-side credential.

The whole household shares the iAquaLink account, so the login form is
checked against the same IAQUALINK_USER / IAQUALINK_PASS the backend uses
upstream (no separate user store). Sessions are signed cookies
(SessionMiddleware) — nothing is stored server-side, so sessions survive
restarts as long as SESSION_SECRET is stable.
"""

import logging
import os
import secrets

from fastapi import HTTPException, Request

from app.aqualink import get_credentials

logger = logging.getLogger(__name__)

# Rolling session lifetime: the cookie is re-issued on every response, so an
# active user never hits this; it only expires abandoned sessions.
SESSION_MAX_AGE = 60 * 60 * 24 * 30  # 30 days

# Cheap brute-force friction on failed logins. Module constant so tests can
# zero it (same pattern as controls.COOLDOWN_DELAY).
FAILED_LOGIN_DELAY = 0.5


def get_session_secret() -> str:
    """Return SESSION_SECRET from the environment, or a random per-boot key.

    The fallback keeps dev and tests working without config, at the cost of
    invalidating sessions on restart (everyone re-logs-in) — an availability
    nuisance, not a security hole. Set SESSION_SECRET in .env for stable
    sessions, especially under uvicorn --reload.
    """
    secret = os.environ.get("SESSION_SECRET")
    if secret:
        return secret
    logger.warning(
        "SESSION_SECRET is not set; using a random per-boot secret "
        "(sessions will not survive a restart)."
    )
    return secrets.token_hex(32)


def verify_credentials(email: str, password: str) -> bool:
    """Constant-time check of a login attempt against the .env credential.

    Email is case/whitespace-insensitive (it's an email address); the
    password is exact. Both comparisons run before combining so timing
    doesn't reveal which field was wrong.

    Raises MissingCredentials if the server has no credential configured.
    """
    expected_email, expected_password = get_credentials()
    email_ok = secrets.compare_digest(
        email.strip().casefold().encode(), expected_email.strip().casefold().encode()
    )
    password_ok = secrets.compare_digest(password.encode(), expected_password.encode())
    return email_ok and password_ok


def require_auth(request: Request) -> None:
    """FastAPI dependency: reject the request unless the session is logged in."""
    if not request.session.get("authenticated"):
        raise HTTPException(status_code=401, detail="Not authenticated.")
