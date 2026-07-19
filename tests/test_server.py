import logging

from china_a_share.server import (
    APPLICATION_LOG_FORMAT,
    configure_logging,
    server_address,
)


def test_server_address_defaults_to_localhost(monkeypatch):
    monkeypatch.delenv("APP_HOST", raising=False)
    monkeypatch.delenv("PORT", raising=False)

    assert server_address() == ("127.0.0.1", 8000)


def test_server_address_supports_cloud_run_environment(monkeypatch):
    monkeypatch.setenv("APP_HOST", "0.0.0.0")
    monkeypatch.setenv("PORT", "8080")

    assert server_address() == ("0.0.0.0", 8080)


def test_configure_logging_enables_structured_application_info(monkeypatch):
    calls = []
    monkeypatch.setattr(logging, "basicConfig", lambda **kwargs: calls.append(kwargs))

    configure_logging()

    assert calls == [{"level": logging.INFO, "format": APPLICATION_LOG_FORMAT}]
