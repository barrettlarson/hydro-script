"""Tests for the error taxonomy + classifier."""

import asyncio

import pytest
from iaqualink.exception import (
    AqualinkDeviceNotSupported,
    AqualinkException,
    AqualinkInvalidParameterException,
    AqualinkServiceException,
    AqualinkServiceUnauthorizedException,
    AqualinkSystemOfflineException,
)

from app.aqualink import DeviceNotFound, MissingCredentials, NoSystemFound
from app.errors import (
    FailureCategory,
    classify,
    http_response,
    is_transient,
)


class TestClassify:
    def test_missing_credentials_is_auth(self):
        assert classify(MissingCredentials("nope")) == FailureCategory.AUTH

    def test_unauthorized_is_auth(self):
        assert classify(AqualinkServiceUnauthorizedException()) == FailureCategory.AUTH

    def test_no_system_is_config(self):
        assert classify(NoSystemFound("none")) == FailureCategory.CONFIG

    def test_device_not_found_is_config(self):
        assert classify(DeviceNotFound("missing")) == FailureCategory.CONFIG

    def test_system_offline_is_upstream(self):
        assert classify(AqualinkSystemOfflineException()) == FailureCategory.UPSTREAM_OFFLINE

    def test_generic_service_error_is_upstream(self):
        assert classify(AqualinkServiceException("500 server error")) == (
            FailureCategory.UPSTREAM_OFFLINE
        )

    @pytest.mark.parametrize("text", ["429 Too Many Requests", "rate limit exceeded"])
    def test_rate_limit_signal_is_rate_limit(self, text):
        assert classify(AqualinkServiceException(text)) == FailureCategory.RATE_LIMIT

    def test_unsupported_usage_is_config(self):
        assert classify(AqualinkDeviceNotSupported()) == FailureCategory.CONFIG
        assert classify(AqualinkInvalidParameterException()) == FailureCategory.CONFIG

    def test_timeout_is_network(self):
        assert classify(asyncio.TimeoutError()) == FailureCategory.NETWORK

    def test_connection_error_is_network(self):
        assert classify(ConnectionError("refused")) == FailureCategory.NETWORK

    def test_httpx_transport_error_is_network(self):
        httpx = pytest.importorskip("httpx")
        assert classify(httpx.ConnectError("boom")) == FailureCategory.NETWORK

    def test_unmapped_aqualink_exception_is_unknown(self):
        assert classify(AqualinkException("weird")) == FailureCategory.UNKNOWN

    def test_unknown_exception_is_unknown(self):
        assert classify(ValueError("???")) == FailureCategory.UNKNOWN


class TestTransient:
    def test_transient_categories(self):
        for cat in (
            FailureCategory.RATE_LIMIT,
            FailureCategory.UPSTREAM_OFFLINE,
            FailureCategory.NETWORK,
        ):
            assert is_transient(cat)

    def test_non_transient_categories(self):
        for cat in (FailureCategory.AUTH, FailureCategory.CONFIG, FailureCategory.UNKNOWN):
            assert not is_transient(cat)


class TestHttpResponse:
    def test_every_category_has_a_response(self):
        for cat in FailureCategory:
            status_code, message = http_response(cat)
            assert 400 <= status_code < 600
            assert message

    def test_rate_limit_message_hides_internals(self):
        _, message = http_response(FailureCategory.RATE_LIMIT)
        assert "rate" not in message.lower()

    def test_network_message_is_user_facing(self):
        status_code, message = http_response(FailureCategory.NETWORK)
        assert status_code == 503
        assert "network" in message.lower()
