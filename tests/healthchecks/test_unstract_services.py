"""Contract tests for the bounded Unstract service probes."""

from __future__ import annotations

import http.server
import json
import os
import socketserver
import subprocess
import threading
from pathlib import Path

import pytest

ROOT = Path(__file__).parents[2]
SCRIPT = ROOT / "docker" / "healthchecks" / "unstract-services.sh"


def run_probe(service: str, env: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
    probe_env = os.environ.copy()
    if env:
        probe_env.update(env)
    return subprocess.run(
        ["sh", str(SCRIPT), service],
        check=False,
        capture_output=True,
        text=True,
        env=probe_env,
        timeout=8,
    )


def write_fake(tmp_path: Path, name: str, body: str) -> Path:
    path = tmp_path / name
    path.write_text("#!/bin/sh\n" + body, encoding="utf-8")
    path.chmod(0o755)
    return path


def test_probe_script_is_valid_posix_shell() -> None:
    result = subprocess.run(
        ["sh", "-n", str(SCRIPT)], check=False, capture_output=True, text=True
    )
    assert result.returncode == 0, result.stderr


def test_weaviate_requires_metadata_and_ready_status(tmp_path: Path) -> None:
    wget = write_fake(
        tmp_path,
        "wget",
        """
case "$*" in
  *v1/meta*) printf '%s' "$FAKE_META" ;;
  *well-known/ready*) : ;;
esac
exit "${FAKE_EXIT:-0}"
""",
    )
    base = {"WGET_BIN": str(wget), "FAKE_META": '{"version":"1.39.2"}'}
    assert run_probe("weaviate", base).returncode == 0

    failed = run_probe("weaviate", {**base, "FAKE_META": '{"modules":{}}'})
    assert failed.returncode != 0
    assert "modules" not in failed.stderr


def test_traefik_requires_nonempty_error_free_overview(tmp_path: Path) -> None:
    wget = write_fake(
        tmp_path,
        "wget",
        """
case "$*" in
  *api/overview*) printf '%s' "$FAKE_OVERVIEW" ;;
esac
""",
    )
    healthy = (
        '{"http":{"routers":{"total":9,"warnings":0,"errors":0},'
        '"services":{"total":8,"warnings":0,"errors":0}}}'
    )
    result = run_probe(
        "proxy", {"WGET_BIN": str(wget), "FAKE_OVERVIEW": healthy}
    )
    assert result.returncode == 0, result.stderr

    unhealthy = healthy.replace('"errors":0', '"errors":1', 1)
    failed = run_probe(
        "proxy", {"WGET_BIN": str(wget), "FAKE_OVERVIEW": unhealthy}
    )
    assert failed.returncode != 0


class _QdrantHandler(socketserver.BaseRequestHandler):
    response = b"HTTP/1.1 200 OK\r\nConnection: close\r\n\r\nhealthz check passed"

    def handle(self) -> None:
        self.request.recv(1024)
        self.request.sendall(self.response)


class _QdrantServer(socketserver.TCPServer):
    allow_reuse_address = True


def qdrant_server(response: bytes) -> tuple[_QdrantServer, int, threading.Thread]:
    handler = type("ResponseHandler", (_QdrantHandler,), {"response": response})
    server = _QdrantServer(("127.0.0.1", 0), handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server, server.server_address[1], thread


def test_qdrant_probe_checks_status_and_body() -> None:
    server, port, _ = qdrant_server(_QdrantHandler.response)
    try:
        result = run_probe(
            "vector-db",
            {"QDRANT_HOST": "127.0.0.1", "QDRANT_PORT": str(port)},
        )
        assert result.returncode == 0, result.stderr
    finally:
        server.shutdown()
        server.server_close()

    server, port, _ = qdrant_server(
        b"HTTP/1.1 503 Service Unavailable\r\nConnection: close\r\n\r\nnot ready"
    )
    try:
        result = run_probe(
            "vector-db",
            {"QDRANT_HOST": "127.0.0.1", "QDRANT_PORT": str(port)},
        )
        assert result.returncode != 0
    finally:
        server.shutdown()
        server.server_close()


@pytest.mark.parametrize(
    ("service", "command_env", "command_body", "failure_env"),
    [
        ("redis", "REDIS_CLI_BIN", 'printf "%s\\n" "${FAKE_REPLY}"', {"FAKE_REPLY": "NOPE"}),
        (
            "rabbitmq",
            "RABBITMQ_DIAGNOSTICS_BIN",
            'test "${FAKE_FAIL:-0}" = 1 && exit 1 || exit 0',
            {"FAKE_FAIL": "1"},
        ),
        (
            "minio",
            "CURL_BIN",
            'test "${FAKE_FAIL:-0}" = 1 && exit 1 || exit 0',
            {"FAKE_FAIL": "1"},
        ),
    ],
)
def test_native_datastore_probes_propagate_failures(
    tmp_path: Path,
    service: str,
    command_env: str,
    command_body: str,
    failure_env: dict[str, str],
) -> None:
    name = command_env.lower().replace("_bin", "")
    fake = write_fake(tmp_path, name, command_body)
    env = {command_env: str(fake)}
    if service == "redis":
        env["FAKE_REPLY"] = "PONG"
    assert run_probe(service, env).returncode == 0
    assert run_probe(service, {**env, **failure_env}).returncode != 0


def test_postgres_probe_requires_read_only_query_result(tmp_path: Path) -> None:
    pg_isready = write_fake(tmp_path, "pg_isready", "exit 0")
    psql = write_fake(
        tmp_path,
        "psql",
        'printf "%s" "${FAKE_RESULT:-1}"',
    )
    env = {
        "PG_ISREADY_BIN": str(pg_isready),
        "PSQL_BIN": str(psql),
        "POSTGRES_USER": "probe-user",
        "POSTGRES_DB": "probe-db",
    }
    result = run_probe("db", env)
    assert result.returncode == 0, result.stderr
    assert run_probe("db", {**env, "FAKE_RESULT": "0"}).returncode != 0


class _AppHandler(http.server.BaseHTTPRequestHandler):
    mode = "healthy"

    def do_GET(self) -> None:  # noqa: N802 - stdlib protocol hook
        if self.path == "/backend" and self.headers.get("Authorization") != "Bearer test-token":
            self.send_response(401)
            self.end_headers()
            return
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        if self.path == "/backend":
            body = {"status": "healthy", "authenticated": True}
            self.wfile.write(json.dumps(body).encode())
        elif self.path == "/frontend":
            self.wfile.write(b"<html><title>Unstract</title></html>")
        elif self.mode == "healthy":
            self.wfile.write(b"OK")
        else:
            self.wfile.write(b"BROKEN")

    def log_message(self, *_args: object) -> None:
        return


class _AppServer(http.server.ThreadingHTTPServer):
    allow_reuse_address = True


@pytest.fixture()
def app_server() -> tuple[_AppServer, str]:
    server = _AppServer(("127.0.0.1", 0), _AppHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server, f"http://127.0.0.1:{server.server_address[1]}"
    finally:
        server.shutdown()
        server.server_close()


@pytest.mark.parametrize(
    ("service", "path", "variable"),
    [
        ("x2text-service", "/x2text", "X2TEXT_HEALTH_URL"),
        ("platform-service", "/platform", "PLATFORM_HEALTH_URL"),
        ("backend", "/backend", "BACKEND_HEALTH_URL"),
        ("frontend", "/frontend", "FRONTEND_INDEX_URL"),
    ],
)
def test_application_probes_validate_response_contract(
    app_server: tuple[_AppServer, str], service: str, path: str, variable: str
) -> None:
    _server, base_url = app_server
    env = {variable: base_url + path, "PYTHON_BIN": os.environ.get("PYTHON", "python3")}
    if service == "backend":
        env["INTERNAL_SERVICE_API_KEY"] = "test-token"
    result = run_probe(service, env)
    assert result.returncode == 0, result.stderr


def test_application_probe_rejects_wrong_body(app_server: tuple[_AppServer, str]) -> None:
    server, base_url = app_server
    _AppHandler.mode = "broken"
    try:
        result = run_probe(
            "platform-service",
            {
                "PLATFORM_HEALTH_URL": base_url + "/platform",
                "PYTHON_BIN": os.environ.get("PYTHON", "python3"),
            },
        )
        assert result.returncode != 0
    finally:
        _AppHandler.mode = "healthy"
