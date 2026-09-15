import logging
import re
from typing import Any

from docker import DockerClient
from docker.errors import APIError
from flask import Blueprint, jsonify
from requests.exceptions import ConnectionError as RequestsConnectionError
from requests.exceptions import Timeout as RequestsTimeout

logger = logging.getLogger(__name__)

RuntimeProbeFailure = tuple[str, str]

# Docker SDK client construction negotiates the daemon API version before
# ``ping`` runs. Keep both per-request budgets small enough that construction
# plus ping remains inside the runner's three-second HTTP healthcheck timeout.
_RUNTIME_PROBE_TIMEOUT_SECONDS = 1
_MAX_ERROR_DETAIL_LENGTH = 32
_SAFE_DETAIL = re.compile(r"[^a-z0-9_]+")

# Define a Blueprint with a root URL path
health_bp = Blueprint("health", __name__)


def _safe_error_detail(value: str) -> str:
    """Return a short identifier that cannot contain daemon-provided text."""
    detail = re.sub(r"(?<!^)(?=[A-Z])", "_", value)
    detail = _SAFE_DETAIL.sub("_", detail.lower()).strip("_")
    return (detail or "unknown")[:_MAX_ERROR_DETAIL_LENGTH]


def _exception_chain(exc: BaseException):
    """Yield a bounded exception chain without exposing exception messages."""
    current: BaseException | None = exc
    seen: set[int] = set()
    for _ in range(3):
        if current is None or id(current) in seen:
            return
        seen.add(id(current))
        yield current
        current = current.__cause__ or current.__context__


def _safe_exception_message(exc: BaseException) -> str:
    """Read an exception marker without allowing a broken ``__str__`` to escape."""
    try:
        return str(exc).lower()[:256]
    except Exception:
        return ""


def _runtime_probe_failure(exc: BaseException) -> RuntimeProbeFailure:
    """Classify a Docker probe error using only bounded, safe identifiers."""
    chain = tuple(_exception_chain(exc))

    for candidate in chain:
        if isinstance(candidate, APIError):
            status_code = candidate.status_code
            if isinstance(status_code, int) and 100 <= status_code <= 599:
                return "api", f"status_{status_code}"
            return "api", "status_unknown"

    for candidate in chain:
        if isinstance(candidate, (RequestsTimeout, TimeoutError)):
            return "timeout", _safe_error_detail(type(candidate).__name__)

    for candidate in chain:
        if isinstance(candidate, (RequestsConnectionError, ConnectionError)):
            return "connection", _safe_error_detail(type(candidate).__name__)

    # Docker SDK releases have wrapped transport failures in DockerException
    # in different layers. Inspect only lower-cased text for fixed markers and
    # never return or log the original message, which may contain socket paths.
    message = _safe_exception_message(exc)
    if "timeout" in message or "timed out" in message:
        return "timeout", "docker_exception"
    if "connection" in message or "connect" in message:
        return "connection", "docker_exception"

    return "docker", _safe_error_detail(type(exc).__name__)


def _container_runtime_ready() -> tuple[bool, RuntimeProbeFailure | None]:
    """Perform a bounded, read-only ping against the mounted container socket."""
    try:
        # Importing DockerClient does not create a client or touch the socket.
        # ``ping`` only asks the daemon for liveness; it does not list, create,
        # publish, or remove a tool container.
        client = DockerClient.from_env(timeout=_RUNTIME_PROBE_TIMEOUT_SECONDS)
        try:
            client.ping()
        finally:
            client.close()
    except Exception as exc:
        # Keep credentials, socket paths, and daemon error text out of the HTTP
        # body and logs. The fixed category/detail pair distinguishes transport
        # failures without weakening the fail-closed readiness decision.
        category, detail = _runtime_probe_failure(exc)
        logger.warning(
            "Runner container runtime probe failed category=%s detail=%s",
            category,
            detail,
        )
        return False, (category, detail)
    return True, None


@health_bp.route("/health", methods=["GET"])
def health_check() -> str | tuple[Any, int]:
    runtime_ready, failure = _container_runtime_ready()
    if not runtime_ready:
        if isinstance(failure, tuple):
            error, error_detail = failure
        else:
            # Keep compatibility with callers that replace the private probe
            # in tests or integrations with the original string result.
            error, error_detail = failure or "unavailable", None
        payload = {
            "status": "unhealthy",
            "dependency": "container_runtime",
            "error": error,
        }
        if error_detail:
            payload["error_detail"] = error_detail
        return (
            jsonify(payload),
            503,
        )
    return "OK"
