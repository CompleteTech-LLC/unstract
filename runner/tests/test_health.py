"""Runner readiness must include the mounted container runtime."""

from __future__ import annotations

import time
from types import SimpleNamespace

from docker.errors import DockerException
from flask import Flask
from requests.exceptions import ConnectionError, ReadTimeout

from unstract.runner.controller import health as health_module


def _client():
    app = Flask(__name__)
    app.register_blueprint(health_module.health_bp, url_prefix="/v1/api")
    return app.test_client()


def test_health_returns_ok_when_container_runtime_ping_passes(monkeypatch):
    monkeypatch.setattr(health_module, "_container_runtime_ready", lambda: (True, None))

    response = _client().get("/v1/api/health")

    assert response.status_code == 200
    assert response.get_data(as_text=True) == "OK"


def test_health_returns_503_without_runtime_readiness(monkeypatch):
    monkeypatch.setattr(
        health_module,
        "_container_runtime_ready",
        lambda: (False, "PermissionError"),
    )

    response = _client().get("/v1/api/health")

    assert response.status_code == 503
    assert response.get_json() == {
        "status": "unhealthy",
        "dependency": "container_runtime",
        "error": "PermissionError",
    }


def test_runtime_probe_reports_bounded_timeout_detail(monkeypatch):
    client = SimpleNamespace(
        ping=lambda: (_ for _ in ()).throw(ReadTimeout("/run/secrets/token")),
        close=lambda: None,
    )
    monkeypatch.setattr(
        health_module.DockerClient,
        "from_env",
        lambda *, timeout: client,
    )

    ready, failure = health_module._container_runtime_ready()

    assert not ready
    assert failure == ("timeout", "read_timeout")


def test_runtime_probe_reports_connection_detail_and_keeps_fail_closed(monkeypatch):
    client = SimpleNamespace(
        ping=lambda: (_ for _ in ()).throw(ConnectionError("unix:///run/docker.sock")),
        close=lambda: None,
    )
    monkeypatch.setattr(
        health_module.DockerClient,
        "from_env",
        lambda *, timeout: client,
    )

    ready, failure = health_module._container_runtime_ready()

    assert not ready
    assert failure == ("connection", "connection_error")


def test_health_exposes_api_status_without_exception_text(monkeypatch):
    class FakeAPIError(health_module.APIError):
        @property
        def status_code(self):
            return 503

    monkeypatch.setattr(
        health_module,
        "_container_runtime_ready",
        lambda: (False, health_module._runtime_probe_failure(FakeAPIError("secret"))),
    )

    response = _client().get("/v1/api/health")

    assert response.status_code == 503
    assert response.get_json() == {
        "status": "unhealthy",
        "dependency": "container_runtime",
        "error": "api",
        "error_detail": "status_503",
    }


def test_runtime_probe_sanitizes_unknown_exception_detail(monkeypatch):
    class SecretTransportFailure(Exception):
        pass

    client = SimpleNamespace(
        ping=lambda: (_ for _ in ()).throw(
            SecretTransportFailure("password=do-not-return")
        ),
        close=lambda: None,
    )
    monkeypatch.setattr(
        health_module.DockerClient,
        "from_env",
        lambda *, timeout: client,
    )

    ready, failure = health_module._container_runtime_ready()

    assert not ready
    assert failure == ("docker", "secret_transport_failure")
    assert "password" not in str(failure)


def test_runtime_probe_classifies_wrapped_docker_timeout_without_message(monkeypatch):
    client = SimpleNamespace(
        ping=lambda: (_ for _ in ()).throw(
            DockerException("Error while pinging /run/secrets/token: timed out")
        ),
        close=lambda: None,
    )
    monkeypatch.setattr(
        health_module.DockerClient,
        "from_env",
        lambda *, timeout: client,
    )

    ready, failure = health_module._container_runtime_ready()

    assert not ready
    assert failure == ("timeout", "docker_exception")
    assert "/run/secrets/token" not in str(failure)


def test_runtime_probe_passes_fixed_timeout_to_docker_client(monkeypatch):
    client = SimpleNamespace(ping=lambda: None, close=lambda: None)
    observed = {}

    def from_env(*, timeout):
        observed["timeout"] = timeout
        return client

    monkeypatch.setattr(health_module.DockerClient, "from_env", from_env)

    ready, failure = health_module._container_runtime_ready()

    assert (ready, failure) == (True, None)
    assert observed == {"timeout": 1}


def test_runtime_probe_construction_and_ping_fit_healthcheck_budget(monkeypatch):
    observed = []

    class SlowButHealthyClient:
        def ping(self):
            time.sleep(observed[-1] + 0.02)

        def close(self):
            return None

    def from_env(*, timeout):
        observed.append(timeout)
        time.sleep(timeout + 0.02)
        return SlowButHealthyClient()

    monkeypatch.setattr(health_module.DockerClient, "from_env", from_env)

    started = time.monotonic()
    ready, failure = health_module._container_runtime_ready()
    elapsed = time.monotonic() - started

    assert (ready, failure) == (True, None)
    assert observed == [1]
    assert elapsed < 3


def test_runtime_probe_survives_exception_with_broken_string_conversion(monkeypatch):
    class BrokenStringFailure(Exception):
        def __str__(self):
            raise RuntimeError("string conversion failed")

    client = SimpleNamespace(
        ping=lambda: (_ for _ in ()).throw(BrokenStringFailure()),
        close=lambda: None,
    )
    monkeypatch.setattr(
        health_module.DockerClient,
        "from_env",
        lambda *, timeout: client,
    )

    ready, failure = health_module._container_runtime_ready()

    assert not ready
    assert failure == ("docker", "broken_string_failure")
