import logging
from typing import Any

from flask import Blueprint, jsonify

logger = logging.getLogger(__name__)

# Define a Blueprint with a root URL path
health_bp = Blueprint("health", __name__)


def _container_runtime_ready() -> tuple[bool, str | None]:
    """Perform a bounded, read-only ping against the mounted container socket."""
    try:
        # Import lazily so importing the Flask blueprint does not create a Docker
        # client or touch the socket. ``ping`` only asks the daemon for liveness;
        # it does not list, create, publish, or remove a tool container.
        from docker import DockerClient

        client = DockerClient.from_env(timeout=2)
        try:
            client.ping()
        finally:
            client.close()
    except Exception as exc:
        # Keep credentials, socket paths, and daemon error text out of the HTTP
        # body. The exception class is enough for an operator to identify the
        # failed dependency while logs retain only the same sanitized class name.
        logger.warning("Runner container runtime probe failed: %s", type(exc).__name__)
        return False, type(exc).__name__
    return True, None


@health_bp.route("/health", methods=["GET"])
def health_check() -> str | tuple[Any, int]:
    runtime_ready, error_type = _container_runtime_ready()
    if not runtime_ready:
        return (
            jsonify(
                {
                    "status": "unhealthy",
                    "dependency": "container_runtime",
                    "error": error_type or "unavailable",
                }
            ),
            503,
        )
    return "OK"
