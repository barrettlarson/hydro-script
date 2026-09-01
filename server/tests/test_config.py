"""Tests for the SSM secret bootstrap.

Exercised against a fake SSM client for the same reason ``test_store.py``
fakes DynamoDB: the surface we depend on is one call wide, and a fake keeps
the suite dependency-free and instant.
"""

from __future__ import annotations

import os
import sys
from typing import Any, Optional

import pytest

from app.config import PARAM_PATH_ENV, load_secrets

PATH = "/hydro-script/prod"


class FakeSSMClient:
    """``get_parameters_by_path``, over a dict, with optional pagination."""

    def __init__(self, params: dict[str, str], *, page_size: Optional[int] = None) -> None:
        #: name (last segment) -> value
        self.params = params
        self.page_size = page_size
        self.calls: list[dict[str, Any]] = []

    def get_parameters_by_path(self, **kwargs: Any) -> dict[str, Any]:
        self.calls.append(kwargs)
        items = [
            {"Name": f"{kwargs['Path']}/{name}", "Value": value, "Type": "SecureString"}
            for name, value in self.params.items()
        ]
        size = self.page_size or len(items) or 1
        start = int(kwargs.get("NextToken") or 0)
        page = items[start : start + size]
        response: dict[str, Any] = {"Parameters": page}
        if start + size < len(items):
            response["NextToken"] = str(start + size)
        return response


class RaisingSSMClient:
    def get_parameters_by_path(self, **kwargs: Any) -> dict[str, Any]:
        raise RuntimeError("AccessDeniedException: ssm:GetParametersByPath")


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """No path and no target vars, so each test states its own setup."""
    monkeypatch.delenv(PARAM_PATH_ENV, raising=False)
    for name in ("IAQUALINK_USER", "IAQUALINK_PASS", "SESSION_SECRET", "VAPID_PRIVATE_KEY"):
        monkeypatch.delenv(name, raising=False)


class TestDisabled:
    """Without a path configured this must not touch AWS at all."""

    def test_unset_path_is_a_no_op(self) -> None:
        client = FakeSSMClient({"SESSION_SECRET": "s3cret"})
        assert load_secrets(client=client) == []
        assert client.calls == []

    def test_blank_path_is_a_no_op(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv(PARAM_PATH_ENV, "   ")
        client = FakeSSMClient({"SESSION_SECRET": "s3cret"})
        assert load_secrets(client=client) == []
        assert client.calls == []

    def test_local_dev_needs_no_boto3(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # The default path builds a boto3 client; without a path it must
        # return before importing it, which is what keeps `just server` and
        # this suite free of an AWS dependency.
        monkeypatch.setitem(sys.modules, "boto3", None)
        assert load_secrets() == []


class TestLoading:
    def test_populates_environment_from_the_path(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv(PARAM_PATH_ENV, PATH)
        client = FakeSSMClient({"IAQUALINK_USER": "pool@example.com", "SESSION_SECRET": "abc123"})

        assert sorted(load_secrets(client=client)) == ["IAQUALINK_USER", "SESSION_SECRET"]
        # The point of the whole module: the existing readers see plain env vars.
        assert os.environ["IAQUALINK_USER"] == "pool@example.com"
        assert os.environ["SESSION_SECRET"] == "abc123"

    def test_decrypts(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # SecureStrings come back as ciphertext without this flag.
        monkeypatch.setenv(PARAM_PATH_ENV, PATH)
        client = FakeSSMClient({"SESSION_SECRET": "abc123"})
        load_secrets(client=client)
        assert client.calls[0]["WithDecryption"] is True

    def test_name_is_the_last_path_segment(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv(PARAM_PATH_ENV, PATH)
        client = FakeSSMClient({"VAPID_PRIVATE_KEY": "key"})
        assert load_secrets(client=client) == ["VAPID_PRIVATE_KEY"]

    def test_trailing_slash_is_tolerated(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv(PARAM_PATH_ENV, PATH + "/")
        client = FakeSSMClient({"SESSION_SECRET": "abc123"})
        load_secrets(client=client)
        assert client.calls[0]["Path"] == PATH

    def test_follows_pagination(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv(PARAM_PATH_ENV, PATH)
        client = FakeSSMClient(
            {"A": "1", "B": "2", "C": "3", "D": "4", "E": "5"},
            page_size=2,
        )
        assert sorted(load_secrets(client=client)) == ["A", "B", "C", "D", "E"]
        assert len(client.calls) == 3

    def test_first_call_sends_no_token(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv(PARAM_PATH_ENV, PATH)
        client = FakeSSMClient({"A": "1"})
        load_secrets(client=client)
        assert "NextToken" not in client.calls[0]

    def test_empty_path_returns_nothing_gracefully(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv(PARAM_PATH_ENV, PATH)
        assert load_secrets(client=FakeSSMClient({})) == []


class TestPrecedence:
    def test_existing_environment_wins(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # Overriding one value on the function must not require moving the set.
        monkeypatch.setenv(PARAM_PATH_ENV, PATH)
        monkeypatch.setenv("SESSION_SECRET", "set-on-the-function")
        client = FakeSSMClient({"SESSION_SECRET": "from-ssm", "IAQUALINK_USER": "u"})

        assert load_secrets(client=client) == ["IAQUALINK_USER"]
        assert os.environ["SESSION_SECRET"] == "set-on-the-function"

    def test_empty_parameter_never_masks_a_real_value(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Writing "" would satisfy `name in os.environ` for any later loader
        # while leaving the app unconfigured; skipping keeps the failure honest.
        monkeypatch.setenv(PARAM_PATH_ENV, PATH)
        client = FakeSSMClient({"SESSION_SECRET": ""})
        assert load_secrets(client=client) == []
        assert "SESSION_SECRET" not in os.environ


class TestFailure:
    def test_errors_propagate(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # Fail loudly at init rather than as a confusing MissingCredentials
        # from three modules away.
        monkeypatch.setenv(PARAM_PATH_ENV, PATH)
        with pytest.raises(RuntimeError, match="AccessDenied"):
            load_secrets(client=RaisingSSMClient())
