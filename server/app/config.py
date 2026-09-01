"""Secret loading: SSM Parameter Store -> ``os.environ``.

Lambda's function environment variables are the obvious place to put
credentials and the wrong one: they sit in plaintext in the function
configuration, readable by anyone holding ``lambda:GetFunctionConfiguration``
and printed on the console's own configuration tab. So the deployed stack
carries only a *path* (``SSM_PARAM_PATH``) and the values are fetched, and
decrypted, at cold start.

CloudFormation cannot do this job in the template. Its
``{{resolve:ssm-secure:...}}`` dynamic reference is not supported on Lambda
environment variables, and the plaintext ``{{resolve:ssm:...}}`` form would
put the secret straight back into the function config -- the exact thing this
module exists to avoid.

Values land in ``os.environ`` rather than being handed around, so the four
existing readers -- :func:`app.aqualink.get_credentials`,
:func:`app.auth.get_session_secret`, and the two in :mod:`app.push` -- stay
exactly as they are, and local dev keeps using ``.env`` via ``load_dotenv``.
That is the whole trick: one bootstrap at the Lambda entry point, and nothing
downstream needs to know where its configuration came from.

Cost and timing: parameters are Standard tier (free, 4 KB each), read with a
single ``GetParametersByPath`` per *execution environment* rather than per
request -- Lambda freezes the process between invocations, so a warm container
pays nothing after the first call.
"""

from __future__ import annotations

import logging
import os
from typing import Any, Optional

logger = logging.getLogger(__name__)

#: Path prefix holding the SecureString parameters, e.g. ``/hydro-script/prod``.
#: Set only by the deployed stack; its absence is what keeps local dev and the
#: test suite on ``.env`` with no AWS involved.
PARAM_PATH_ENV = "SSM_PARAM_PATH"


def load_secrets(*, client: Optional[Any] = None) -> list[str]:
    """Copy every SecureString under ``SSM_PARAM_PATH`` into ``os.environ``.

    Each parameter's *last path segment* becomes the variable name, so
    ``/hydro-script/prod/SESSION_SECRET`` sets ``SESSION_SECRET``. Returns the
    names actually set, for logging -- never the values.

    A variable already present in the environment wins and is left alone. That
    lets a single value be overridden on the function (or in a test) without
    relocating the whole set, and keeps this a no-op on top of ``.env``.

    Errors are *not* swallowed. A missing parameter or a role without
    ``ssm:GetParametersByPath`` should surface here, at init, where the
    exception names the real problem; caught and logged, it would resurface
    later as a baffling "Set IAQUALINK_USER" from three modules away. Lambda
    re-runs initialization on the next invocation, so a transient failure
    costs one cold start rather than wedging the function.
    """
    path = os.environ.get(PARAM_PATH_ENV, "").strip().rstrip("/")
    if not path:
        return []

    if client is None:
        import boto3  # provided by the Lambda runtime, like store.py's client

        client = boto3.client("ssm")

    loaded: list[str] = []
    token: Optional[str] = None
    while True:
        kwargs: dict[str, Any] = {"Path": path, "WithDecryption": True}
        if token:
            kwargs["NextToken"] = token
        response = client.get_parameters_by_path(**kwargs)

        for param in response.get("Parameters", []):
            name = param["Name"].rsplit("/", 1)[-1]
            value = param.get("Value") or ""
            if not value:
                # An empty parameter is a configuration mistake; storing it
                # would let it mask a real value set elsewhere.
                logger.warning("SSM parameter %r is empty; skipping", param["Name"])
                continue
            if name in os.environ:
                continue
            os.environ[name] = value
            loaded.append(name)

        token = response.get("NextToken")
        if not token:
            break

    logger.info("loaded %d secret(s) from %s: %s", len(loaded), path, ", ".join(sorted(loaded)))
    return loaded
