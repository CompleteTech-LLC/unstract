#!/usr/bin/env python3
"""Run the durable Unstract replay owned by the Train user systemd manager.

The systemd unit supplies only operator-owned, non-secret paths through its
EnvironmentFile.  This launcher validates the path contract and constructs
the guarded ``start`` command without a shell, so a malformed state file
fails closed before Compose is reached.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

CONFIRM_TOKEN = "START_UNSTRACT_HEALTH"
PYTHON = "/usr/bin/python3"
REQUIRED_PATHS = (
    "UNSTRACT_GUARD",
    "UNSTRACT_STATE_DIR",
    "UNSTRACT_PROJECT_DIR",
    "UNSTRACT_LIVE_ENV_FILE",
    "UNSTRACT_BASELINE",
    "UNSTRACT_CANDIDATE_SOURCE",
    "UNSTRACT_CANDIDATE_LOCK",
    "UNSTRACT_PROBE_SOURCE",
    "UNSTRACT_COMPOSE_BASE",
    "UNSTRACT_COMPOSE_TRAIN",
    "UNSTRACT_COMPOSE_WORKER_HEALTHCHECKS",
    "UNSTRACT_COMPOSE_CORE_HEALTHCHECKS",
    "UNSTRACT_COMPOSE_ENV_DIR",
)


def _required(environ: dict[str, str], name: str) -> str:
    value = environ.get(name, "")
    if not value:
        raise ValueError(f"{name} is required")
    return value


def _absolute(environ: dict[str, str], name: str) -> str:
    value = _required(environ, name)
    if not Path(value).is_absolute():
        raise ValueError(f"{name} must be an absolute path")
    return value


def build_start_args(environ: dict[str, str]) -> list[str]:
    """Build the exact guarded startup command from systemd's environment."""
    for name in REQUIRED_PATHS:
        _absolute(environ, name)

    confirm = environ.get("UNSTRACT_START_CONFIRM", "")
    if confirm != CONFIRM_TOKEN:
        raise ValueError("UNSTRACT_START_CONFIRM does not authorize durable replay")

    timeout = environ.get("UNSTRACT_OPERATION_TIMEOUT", "2400")
    if not timeout.isdigit() or int(timeout) <= 0:
        raise ValueError("UNSTRACT_OPERATION_TIMEOUT must be a positive integer")

    args = [
        PYTHON,
        _absolute(environ, "UNSTRACT_GUARD"),
        "start",
        "--state-dir",
        _absolute(environ, "UNSTRACT_STATE_DIR"),
        "--project-dir",
        _absolute(environ, "UNSTRACT_PROJECT_DIR"),
        "--live-env-file",
        _absolute(environ, "UNSTRACT_LIVE_ENV_FILE"),
        "--baseline",
        _absolute(environ, "UNSTRACT_BASELINE"),
        "--candidate-source",
        _absolute(environ, "UNSTRACT_CANDIDATE_SOURCE"),
        "--candidate-lock",
        _absolute(environ, "UNSTRACT_CANDIDATE_LOCK"),
        "--probe-source",
        _absolute(environ, "UNSTRACT_PROBE_SOURCE"),
    ]
    for name in (
        "UNSTRACT_COMPOSE_BASE",
        "UNSTRACT_COMPOSE_TRAIN",
        "UNSTRACT_COMPOSE_WORKER_HEALTHCHECKS",
        "UNSTRACT_COMPOSE_CORE_HEALTHCHECKS",
    ):
        args.extend(("--compose-file", _absolute(environ, name)))
    args.extend(("--operation-timeout", timeout, "--confirm", CONFIRM_TOKEN))
    return args


def compose_environment(environ: dict[str, str]) -> dict[str, str]:
    """Return the child environment with the live Compose interpolation root."""
    result = dict(environ)
    result["PWD"] = _absolute(environ, "UNSTRACT_COMPOSE_ENV_DIR")
    return result


def main() -> int:
    environ = dict(os.environ)
    try:
        args = build_start_args(environ)
    except ValueError as exc:
        print(f"unstract durable replay: {exc}", file=sys.stderr)
        return 2
    os.environ.update(compose_environment(environ))
    os.execv(args[0], args)
    return 127


if __name__ == "__main__":
    raise SystemExit(main())
