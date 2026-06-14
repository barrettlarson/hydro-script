"""Tests for aqualink.py connection helpers."""

import pytest

from app.aqualink import (
    DeviceNotFound,
    MissingCredentials,
    get_credentials,
    require,
)


# get_credentials


class TestGetCredentials:
    def test_returns_credentials(self, monkeypatch):
        monkeypatch.setenv("IAQUALINK_USER", "user@test.com")
        monkeypatch.setenv("IAQUALINK_PASS", "secret")
        assert get_credentials() == ("user@test.com", "secret")

    def test_missing_user(self, monkeypatch):
        monkeypatch.delenv("IAQUALINK_USER", raising=False)
        monkeypatch.setenv("IAQUALINK_PASS", "secret")
        with pytest.raises(MissingCredentials):
            get_credentials()

    def test_missing_pass(self, monkeypatch):
        monkeypatch.setenv("IAQUALINK_USER", "user@test.com")
        monkeypatch.delenv("IAQUALINK_PASS", raising=False)
        with pytest.raises(MissingCredentials):
            get_credentials()

    def test_empty_strings(self, monkeypatch):
        monkeypatch.setenv("IAQUALINK_USER", "")
        monkeypatch.setenv("IAQUALINK_PASS", "")
        with pytest.raises(MissingCredentials):
            get_credentials()


# require


class TestRequire:
    def test_returns_device(self):
        devices = {"spa_pump": "fake_device"}
        assert require(devices, "spa_pump") == "fake_device"

    def test_missing_key_raises(self):
        devices = {"spa_pump": "fake", "pool_heater": "fake2"}
        with pytest.raises(DeviceNotFound, match="unknown_key"):
            require(devices, "unknown_key")

    def test_error_lists_available(self):
        devices = {"b_device": "x", "a_device": "y"}
        with pytest.raises(DeviceNotFound, match="a_device, b_device"):
            require(devices, "missing")
