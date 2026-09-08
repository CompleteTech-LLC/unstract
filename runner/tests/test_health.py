"""Runner readiness must include the mounted container runtime."""

from __future__ import annotations

from flask import Flask

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
