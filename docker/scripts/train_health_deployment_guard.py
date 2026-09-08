#!/usr/bin/env python3
"""Guarded, targeted deployment helper for the Train Unstract health checks.

The default operation is read-only.  ``capture`` records a sanitized runtime
snapshot and ``preflight`` refuses to continue when the dirty live checkout,
container identity, mounts, networks, or environment hashes drift.  The
mutating ``apply`` and ``rollback`` phases require an explicit confirmation
token and an external candidate image lock.  They recreate only the 24
health-covered workloads in two bounded batches; they never build, pull,
delete source files, reset the checkout, or run project-wide Compose commands.

The script deliberately keeps secret values out of all output.  Environment
values are represented by length and SHA-256 digest only, and health log
outputs are not retained.
"""

from __future__ import annotations

import argparse
import contextlib
import datetime as dt
import fcntl
import hashlib
import json
import os
import select
import shlex
import subprocess
import sys
import tempfile
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any

PROJECT = "unstract-etl-home-complete-tech"
DEFAULT_PROJECT_DIR = Path("/home/completetrain/etl.home.complete.tech")
DEFAULT_COMPOSE_FILES = (
    "docker/docker-compose.yaml",
    "docker/compose.train.worker-healthchecks.yaml",
    "docker/compose.train.healthchecks.yaml",
)
CONFIRM_TOKEN = "APPLY_UNSTRACT_HEALTH"
DEFAULT_COMMAND_TIMEOUT_SECONDS = 300
DEFAULT_LOCK_TIMEOUT_SECONDS = 30
DEFAULT_BATCH_TIMEOUT_SECONDS = 900
DEFAULT_APPLY_TIMEOUT_SECONDS = 2400
DEFAULT_ROLLBACK_TIMEOUT_SECONDS = 1200
ADVISORY_LOCK_SQL = "SELECT pg_try_advisory_lock(hashtextextended('train-unstract-health-deploy', 0));"
ADVISORY_UNLOCK_SQL = "SELECT pg_advisory_unlock(hashtextextended('train-unstract-health-deploy', 0));"
EXPECTED_RUNTIME_ENDPOINT = "unix:///run/user/1000/podman/podman.sock"
EXPECTED_RUNTIME_UID = 1000
QUIESCENCE_REQUIRED_SAMPLES = 3
QUIESCENCE_SAMPLE_INTERVAL_SECONDS = 2.0
QUIESCENCE_MAX_WAIT_SECONDS = 10.0

# Compose service names.  Keep this explicit so a typo or a newly added service
# cannot silently turn a targeted deployment into a project-wide update.
WORKER_SERVICES = (
    "runner",
    "worker-log-history-scheduler-v2",
    "worker-pg-orchestrator-api",
    "worker-pg-orchestrator-general",
    "worker-pg-fileproc",
    "worker-pg-callback",
    "worker-pg-scheduler",
    "worker-pg-metrics",
    "worker-log-stream-consumer",
    "worker-pg-executor",
    "worker-pg-ide-callback",
    "worker-pg-notification",
    "worker-pg-reaper",
)
CORE_SERVICES = (
    "db",
    "redis",
    "minio",
    "reverse-proxy",
    "qdrant",
    "rabbitmq",
    "weaviate",
    "x2text-service",
    "platform-service",
    "backend",
    "frontend",
)
TARGET_SERVICES = WORKER_SERVICES + CORE_SERVICES
PROBE_MOUNT_TARGET = "/usr/local/bin/unstract-services.sh"
EXPECTED_NETWORK = "unstract-network"
SOURCE_STATE_SCHEMA = "unstract-source-state/v2"
BACKUP_SCHEMA = "unstract-health-backup/v2"
REPLACEMENT_SCHEMA = "unstract-health-replacements/v1"

# The two log consumers need these values to expose their source-level
# heartbeat endpoints.  Every other environment value must survive a
# replacement byte-for-byte (represented by length and SHA-256 in captures).
ALLOWED_ENV_ADDITIONS = {
    "worker-log-history-scheduler-v2": {
        "LOG_HISTORY_SCHEDULER_HEALTH_PORT",
        "LOG_HISTORY_SCHEDULER_HEALTH_STALE_SECONDS",
    },
    "worker-log-stream-consumer": {
        "LOG_STREAM_CONSUMER_HEALTH_PORT",
        "LOG_STREAM_CONSUMER_HEALTH_STALE_SECONDS",
    },
}


def _probe_test(service: str) -> list[str]:
    if service in CORE_SERVICES:
        probe_name = {
            "reverse-proxy": "proxy",
            "qdrant": "vector-db",
        }.get(service, service)
        return ["CMD", "/usr/local/bin/unstract-services.sh", probe_name]
    if service == "runner":
        port, path = "5002", "/v1/api/health"
    elif service == "worker-pg-reaper":
        port, path = "8086", "/health"
    elif service == "worker-log-history-scheduler-v2":
        port, path = "8092", "/health"
    elif service == "worker-log-stream-consumer":
        port, path = "8091", "/health"
    else:
        port, path = "8090", "/health"
    return [
        "CMD",
        "/usr/bin/curl",
        "--fail",
        "--silent",
        "--show-error",
        "--max-time",
        "3",
        f"http://127.0.0.1:{port}{path}",
    ]


def health_contract(service: str) -> dict[str, Any]:
    if service in CORE_SERVICES:
        start_period = {
            "db": "30s",
            "redis": "15s",
            "minio": "60s",
            "reverse-proxy": "60s",
            "qdrant": "60s",
            "rabbitmq": "60s",
            "weaviate": "120s",
            "x2text-service": "120s",
            "platform-service": "120s",
            "backend": "180s",
            "frontend": "60s",
        }[service]
        timeout = "10s"
    else:
        start_period, timeout = "30s", "5s"
    return {
        "test": _probe_test(service),
        "interval_ns": 30_000_000_000,
        "timeout_ns": duration_ns(timeout),
        "start_period_ns": duration_ns(start_period),
        "retries": 3,
    }


class OperationDeadline:
    """Monotonic deadline shared by every command in one guarded operation."""

    def __init__(self, seconds: float) -> None:
        if seconds <= 0 or seconds > 24 * 60 * 60:
            raise GuardError("operation timeout must be between 1 second and 24 hours")
        self.ends_at = time.monotonic() + seconds

    def remaining(self, requested: float | None = None) -> float:
        left = self.ends_at - time.monotonic()
        if left <= 0:
            raise GuardError("guarded operation exceeded its total deadline")
        if requested is None:
            return left
        if requested <= 0:
            raise GuardError("command timeout must be positive")
        return min(left, requested)


class GuardError(RuntimeError):
    """A precondition or postcondition failed."""


def utc_now() -> str:
    return dt.datetime.now(dt.UTC).isoformat()


def sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def sha256_file(path: Path) -> str:
    try:
        with path.open("rb") as handle:
            digest = hashlib.sha256()
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(chunk)
            return digest.hexdigest()
    except OSError as exc:
        raise GuardError(f"cannot hash required artifact {path}: {exc}") from exc


def duration_ns(value: Any) -> int | None:
    """Normalize Compose duration strings and Podman nanosecond values."""
    if value is None:
        return None
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return round(value)
    text = str(value).strip().lower()
    units = (
        ("ns", 1),
        ("us", 1_000),
        ("µs", 1_000),
        ("ms", 1_000_000),
        ("s", 1_000_000_000),
        ("m", 60_000_000_000),
        ("h", 3_600_000_000_000),
    )
    for suffix, multiplier in units:
        if text.endswith(suffix):
            try:
                return round(float(text[: -len(suffix)]) * multiplier)
            except ValueError:
                return None
    return None


def runtime_command_env(
    command: list[str], env: dict[str, str] | None = None
) -> dict[str, str] | None:
    """Bind Docker-compatible Compose and direct Podman calls to one socket."""
    if not command or command[0] not in {"docker", "podman"}:
        return env
    effective = os.environ.copy()
    if env is not None:
        effective.update(env)
    for key in ("DOCKER_HOST", "CONTAINER_HOST"):
        supplied = effective.get(key)
        if supplied and supplied != EXPECTED_RUNTIME_ENDPOINT:
            raise GuardError(
                f"{key} must be {EXPECTED_RUNTIME_ENDPOINT} for the rootless Train runtime"
            )
        effective[key] = EXPECTED_RUNTIME_ENDPOINT
    return effective


def run(
    args: list[str],
    *,
    cwd: Path | None = None,
    env: dict[str, str] | None = None,
    check: bool = True,
    input_text: str | None = None,
    timeout_seconds: float = DEFAULT_COMMAND_TIMEOUT_SECONDS,
    deadline: OperationDeadline | None = None,
) -> subprocess.CompletedProcess[str]:
    timeout = deadline.remaining(timeout_seconds) if deadline else timeout_seconds
    env = runtime_command_env(args, env)
    try:
        result = subprocess.run(
            args,
            cwd=str(cwd) if cwd else None,
            env=env,
            input=input_text,
            text=True,
            capture_output=True,
            check=False,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired as exc:
        raise GuardError(f"command timed out: {shlex.join(args)}") from exc
    except OSError as exc:
        raise GuardError(f"cannot execute {shlex.join(args)}: {exc}") from exc
    if check and result.returncode != 0:
        raise GuardError(f"command failed ({result.returncode}): {shlex.join(args)}")
    return result


def parse_json_output(result: subprocess.CompletedProcess[str], description: str) -> Any:
    try:
        return json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise GuardError(f"{description} did not return JSON") from exc


def image_digest(image: dict[str, Any]) -> str | None:
    return image.get("Digest") or next(iter(image.get("RepoDigests") or []), None)


def env_hashes(values: list[str] | None) -> dict[str, dict[str, Any]]:
    result: dict[str, dict[str, Any]] = {}
    for item in values or []:
        key, separator, value = item.partition("=")
        if not separator:
            value = ""
        result[key] = {"length": len(value), "sha256": sha256_bytes(value.encode())}
    return dict(sorted(result.items()))


def selected_labels(labels: dict[str, str]) -> dict[str, str]:
    keys = (
        "com.docker.compose.project",
        "com.docker.compose.service",
        "com.docker.compose.config-hash",
        "com.docker.compose.config_files",
        "com.docker.compose.project.working_dir",
        "com.docker.compose.project.environment_file",
        "com.docker.compose.version",
        "com.docker.compose.oneoff",
        "com.docker.compose.container-number",
    )
    return {key: labels[key] for key in keys if key in labels}


def health_config(value: dict[str, Any] | None) -> dict[str, Any]:
    if not value:
        return {"configured": False}
    test = value.get("Test") or []
    return {
        "configured": True,
        # Healthcheck command vectors contain no credentials and are retained
        # so post-apply verification binds healthy status to the trusted probe.
        "test": test,
        "test_sha256": sha256_bytes(json.dumps(test, separators=(",", ":")).encode()),
        "test_argv_count": len(test),
        "interval": value.get("Interval"),
        "timeout": value.get("Timeout"),
        "start_period": value.get("StartPeriod"),
        "retries": value.get("Retries"),
        "interval_ns": duration_ns(value.get("Interval")),
        "timeout_ns": duration_ns(value.get("Timeout")),
        "start_period_ns": duration_ns(value.get("StartPeriod")),
    }


def health_runtime(value: dict[str, Any] | None) -> dict[str, Any]:
    value = value or {}
    logs = value.get("Log") or []
    last = logs[-1] if logs else None
    return {
        "status": value.get("Status") or "none",
        "failing_streak": value.get("FailingStreak", 0),
        "log_count": len(logs),
        "last": {
            "start": last.get("Start"),
            "end": last.get("End"),
            "exit_code": last.get("ExitCode"),
        }
        if last
        else None,
    }


def normalize_mount(mount: dict[str, Any]) -> dict[str, Any]:
    return {
        "type": mount.get("Type"),
        "name": mount.get("Name"),
        "source": mount.get("Source"),
        "destination": mount.get("Destination"),
        "rw": mount.get("RW"),
        "options": sorted(mount.get("Options") or []),
    }


def normalize_networks(value: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """Keep stable network identity while omitting replacement-specific IPs."""
    result: dict[str, dict[str, Any]] = {}
    for name, network in sorted(value.items()):
        result[name] = {
            "aliases": sorted(network.get("Aliases") or []),
            "network_mode": network.get("NetworkID") or None,
            "driver_opts": network.get("DriverOpts") or {},
        }
    return result


def normalize_option(value: Any) -> Any:
    """Canonicalize Podman inspect fields without exposing command secrets."""
    if isinstance(value, dict):
        return {str(key): normalize_option(item) for key, item in sorted(value.items())}
    if isinstance(value, (list, tuple)):
        return [normalize_option(item) for item in value]
    return value


def runtime_options(item: dict[str, Any], config: dict[str, Any]) -> dict[str, Any]:
    host_config = item.get("HostConfig") or {}
    return normalize_option(
        {
            "command": config.get("Cmd"),
            "entrypoint": config.get("Entrypoint"),
            "user": config.get("User"),
            "working_dir": config.get("WorkingDir"),
            "stop_signal": config.get("StopSignal"),
            "stop_timeout": config.get("StopTimeout"),
            "tty": config.get("Tty"),
            "open_stdin": config.get("OpenStdin"),
            "read_only": host_config.get("ReadonlyRootfs"),
            "privileged": host_config.get("Privileged"),
            "cap_add": host_config.get("CapAdd"),
            "cap_drop": host_config.get("CapDrop"),
            "security_opt": host_config.get("SecurityOpt"),
            "restart_policy": host_config.get("RestartPolicy"),
            "shm_size": host_config.get("ShmSize"),
            "dns": host_config.get("Dns"),
            "extra_hosts": host_config.get("ExtraHosts"),
            "devices": host_config.get("Devices"),
            "ulimits": host_config.get("Ulimits"),
            "port_bindings": (item.get("NetworkSettings") or {}).get("Ports"),
        }
    )


def inspect_project(
    project: str = PROJECT, *, deadline: OperationDeadline | None = None
) -> list[dict[str, Any]]:
    ids_result = run(
        ["podman", "ps", "-aq", "--filter", f"label=com.docker.compose.project={project}"],
        deadline=deadline,
    )
    ids = ids_result.stdout.split()
    if not ids:
        return []
    raw = parse_json_output(
        run(["podman", "inspect", *ids], deadline=deadline), "podman inspect"
    )
    containers: list[dict[str, Any]] = []
    for item in sorted(raw, key=lambda value: value.get("Name", "")):
        config = item.get("Config") or {}
        labels = config.get("Labels") or {}
        state = item.get("State") or {}
        network_settings = item.get("NetworkSettings") or {}
        networks = network_settings.get("Networks") or {}
        containers.append(
            {
                "id": item.get("Id"),
                "name": (item.get("Name") or "").lstrip("/"),
                "compose": selected_labels(labels),
                "image": {
                    "name": item.get("ImageName"),
                    "id": item.get("Image"),
                    "digest": item.get("ImageDigest"),
                    "driver": item.get("Driver"),
                },
                "state": {
                    "status": state.get("Status"),
                    "running": state.get("Running"),
                    "started_at": state.get("StartedAt"),
                    "finished_at": state.get("FinishedAt"),
                    "exit_code": state.get("ExitCode"),
                    "error_present": bool(state.get("Error")),
                    "oom_killed": state.get("OOMKilled"),
                    "restarting": state.get("Restarting"),
                    "restart_count": item.get("RestartCount", 0),
                },
                "health": {
                    "configured": health_config(config.get("Healthcheck")),
                    "runtime": health_runtime(state.get("Health")),
                },
                "env_hashes": env_hashes(config.get("Env")),
                "mounts": [normalize_mount(mount) for mount in item.get("Mounts") or []],
                "options": runtime_options(item, config),
                "graphdriver": {
                    "driver": item.get("Driver"),
                    "upper_dir": (item.get("GraphDriver") or {}).get("Data", {}).get("UpperDir"),
                    "work_dir": (item.get("GraphDriver") or {}).get("Data", {}).get("WorkDir"),
                },
                "networks": sorted(networks),
                "network_details": normalize_networks(networks),
                "user": config.get("User"),
                "working_dir": config.get("WorkingDir"),
                "rootless_runtime": item.get("OCIRuntime"),
            }
        )
    return containers


def source_state(
    project_dir: Path, *, deadline: OperationDeadline | None = None
) -> dict[str, Any]:
    head = run(
        ["git", "-C", str(project_dir), "rev-parse", "HEAD"], deadline=deadline
    ).stdout.strip()
    status = run(
        [
            "git",
            "-C",
            str(project_dir),
            "status",
            "--porcelain=v1",
            "--untracked-files=all",
            "-z",
        ],
        deadline=deadline,
    ).stdout.split("\0")
    entries = [entry for entry in status if entry]
    paths: list[str] = []
    status_hashes: dict[str, dict[str, Any]] = {}
    for entry in entries:
        if len(entry) < 4:
            continue
        path = entry[3:]
        # Porcelain v1 uses a second NUL record for rename/copy destinations.
        # The destination is the only path that can be written by a concurrent
        # checkout, so retain both names when present and hash each separately.
        paths.append(path)
        candidate = project_dir / path
        try:
            if candidate.is_file() and not candidate.is_symlink():
                status_hashes[path] = {
                    "bytes": candidate.stat().st_size,
                    "sha256": sha256_file(candidate),
                }
            elif candidate.is_symlink():
                status_hashes[path] = {
                    "symlink": os.readlink(candidate),
                    "sha256": sha256_bytes(os.readlink(candidate).encode()),
                }
            else:
                status_hashes[path] = {"missing": True}
        except OSError as exc:
            raise GuardError(f"cannot hash dirty source path {path}: {exc}") from exc
    return {
        "schema": SOURCE_STATE_SCHEMA,
        "path": str(project_dir),
        "head": head,
        "status_paths": paths,
        "status_hashes": status_hashes,
        "tracked_dirty_or_untracked_count": len(paths),
    }


def queue_snapshot(deadline: OperationDeadline | None = None) -> dict[str, Any]:
    """Read queue and in-flight job state without claiming or consuming work."""
    rabbit = run(
        [
            "podman",
            "exec",
            "unstract-rabbitmq",
            "rabbitmqctl",
            "list_queues",
            "name",
            "messages",
            "messages_ready",
            "messages_unacknowledged",
            "consumers",
            "--formatter=json",
        ],
        check=False,
        deadline=deadline,
    )
    rabbit_queues: list[dict[str, Any]] = []
    rabbit_parse_ok = False
    if rabbit.returncode == 0:
        try:
            for row in json.loads(rabbit.stdout or "[]"):
                rabbit_queues.append(
                    {
                        "name_present": bool(row.get("name")),
                        "messages": row.get("messages"),
                        "messages_ready": row.get("messages_ready"),
                        "messages_unacknowledged": row.get("messages_unacknowledged"),
                        "consumers": row.get("consumers"),
                    }
                )
            rabbit_parse_ok = True
        except json.JSONDecodeError:
            pass

    sql = (
        "SELECT count(*) AS queue_rows, "
        "count(*) FILTER (WHERE state = 'claimed') AS claimed_rows, "
        "count(*) FILTER (WHERE state = 'scheduled') AS scheduled_rows "
        "FROM unstract.pg_queue_message; "
        "SELECT count(*) FILTER (WHERE remaining > 0) AS active_barriers, "
        "count(*) AS barrier_rows FROM unstract.pg_barrier_state; "
        "SELECT count(*) AS orchestration_claims "
        "FROM unstract.pg_orchestration_claim; "
        "SELECT count(*) AS task_result_rows FROM unstract.pg_task_result; "
        "SELECT count(*) AS batch_dedup_rows FROM unstract.pg_batch_dedup"
    )
    pg = run(
        [
            "podman",
            "exec",
            "unstract-db",
            "sh",
            "-c",
            "psql -XAtq -F '|' -U \"$POSTGRES_USER\" -d \"$POSTGRES_DB\" -c "
            + shlex.quote(sql),
        ],
        check=False,
        deadline=deadline,
    )
    pg_counts: dict[str, int] | None = None
    values = [line.strip() for line in pg.stdout.splitlines() if line.strip()]
    if pg.returncode == 0:
        parsed_rows: list[list[int]] = []
        try:
            for value in values:
                parts = value.split("|")
                if not parts or not all(part.isdigit() for part in parts):
                    raise ValueError
                parsed_rows.append([int(part) for part in parts])
        except ValueError:
            parsed_rows = []
        if (
            len(parsed_rows) == 5
            and len(parsed_rows[0]) == 3
            and len(parsed_rows[1]) == 2
            and all(len(row) == 1 for row in parsed_rows[2:])
        ):
            pg_counts = {
                "pg_queue_message": parsed_rows[0][0],
                "pg_queue_claimed": parsed_rows[0][1],
                "pg_queue_scheduled": parsed_rows[0][2],
                "pg_active_barriers": parsed_rows[1][0],
                "pg_barrier_rows": parsed_rows[1][1],
                "pg_orchestration_claims": parsed_rows[2][0],
                "pg_task_result": parsed_rows[3][0],
                "pg_batch_dedup": parsed_rows[4][0],
            }

    active_jobs = None
    if pg_counts is not None:
        active_jobs = {
            "pg_queue_claimed": pg_counts["pg_queue_claimed"],
            "pg_active_barriers": pg_counts["pg_active_barriers"],
            "pg_orchestration_claims": pg_counts["pg_orchestration_claims"],
            "total": (
                pg_counts["pg_queue_claimed"]
                + pg_counts["pg_active_barriers"]
                + pg_counts["pg_orchestration_claims"]
            ),
        }
    rabbit_empty = (
        rabbit.returncode == 0
        and rabbit_parse_ok
        and all(
            row.get("name_present")
            and row.get("messages") == 0
            and row.get("messages_ready") == 0
            and row.get("messages_unacknowledged") == 0
            for row in rabbit_queues
        )
    )
    quiescent = (
        rabbit_empty
        and pg_counts is not None
        and pg_counts["pg_queue_message"] == 0
        and active_jobs is not None
        and active_jobs["total"] == 0
    )
    return {
        "observed_at": utc_now(),
        "mode": "read_only_snapshot",
        "rabbitmq": {
            "command_succeeded": rabbit.returncode == 0,
            "parsed": rabbit_parse_ok,
            "empty": rabbit_empty,
            "queue_count": len(rabbit_queues),
            "queues": rabbit_queues,
        },
        "postgres": {
            "command_succeeded": pg.returncode == 0,
            "counts": pg_counts,
            "active_jobs": active_jobs,
        },
        "active_jobs": active_jobs,
        "quiescent": quiescent,
    }


def settled_queue_snapshot(deadline: OperationDeadline | None = None) -> dict[str, Any]:
    """Require consecutive zero-work samples before any destructive change."""
    started = time.monotonic()
    end = started + QUIESCENCE_MAX_WAIT_SECONDS
    consecutive = 0
    observations: list[dict[str, Any]] = []
    last: dict[str, Any] | None = None
    while True:
        last = queue_snapshot(deadline)
        observations.append(
            {
                "observed_at": last.get("observed_at"),
                "quiescent": last.get("quiescent"),
                "rabbitmq_empty": (last.get("rabbitmq") or {}).get("empty"),
                "postgres_counts": (last.get("postgres") or {}).get("counts"),
            }
        )
        if last.get("quiescent"):
            consecutive += 1
            if consecutive >= QUIESCENCE_REQUIRED_SAMPLES:
                last["stability"] = {
                    "stable": True,
                    "sample_count": len(observations),
                    "stable_for_seconds": round(time.monotonic() - started, 3),
                    "observations": observations,
                }
                return last
        else:
            consecutive = 0
        remaining = end - time.monotonic()
        if deadline:
            remaining = min(remaining, deadline.remaining())
        if remaining <= 0:
            raise GuardError(
                "queue and active-job state did not remain quiescent for the bounded stability interval"
            )
        time.sleep(min(QUIESCENCE_SAMPLE_INTERVAL_SECONDS, remaining))


def runtime_context(*, deadline: OperationDeadline | None = None) -> dict[str, Any]:
    """Prove direct Podman and Docker-compatible Compose share one rootless store."""
    uid = os.getuid() if hasattr(os, "getuid") else None
    if uid != EXPECTED_RUNTIME_UID:
        raise GuardError(
            f"guard must run as UID {EXPECTED_RUNTIME_UID}; observed {uid}"
        )
    podman_info = parse_json_output(
        run(["podman", "info", "--format", "json"], deadline=deadline),
        "Podman info",
    )
    host = podman_info.get("host") or {}
    security = host.get("security") or {}
    rootless = security.get("rootless")
    if rootless is not True and str(rootless).lower() != "true":
        raise GuardError("direct Podman runtime is not rootless")
    store = podman_info.get("store") or {}
    remote_socket = (host.get("remoteSocket") or {}).get("path")
    if remote_socket and remote_socket not in EXPECTED_RUNTIME_ENDPOINT:
        raise GuardError("direct Podman runtime reports a different API socket")
    docker_info = parse_json_output(
        run(["docker", "info", "--format", "{{json .}}"], deadline=deadline),
        "Docker-compatible runtime info",
    )
    graph_root = store.get("graphRoot")
    docker_root = docker_info.get("DockerRootDir")
    if graph_root and docker_root and graph_root != docker_root:
        raise GuardError(
            "Docker Compose and direct Podman report different container stores"
        )
    return {
        "endpoint": EXPECTED_RUNTIME_ENDPOINT,
        "uid": uid,
        "podman": {
            "rootless": True,
            "graph_root": graph_root,
            "run_root": store.get("runRoot"),
            "remote_socket": remote_socket,
        },
        "compose": {
            "docker_host": EXPECTED_RUNTIME_ENDPOINT,
            "server_version": docker_info.get("ServerVersion"),
            "name": docker_info.get("Name"),
            "docker_root_dir": docker_root,
        },
    }


def capture(
    project_dir: Path, *, deadline: OperationDeadline | None = None
) -> dict[str, Any]:
    uid = os.getuid() if hasattr(os, "getuid") else None
    return {
        "schema": "unstract-deployment-prep/v2",
        "captured_at": utc_now(),
        "host": {
            "uid": uid,
            "hostname": os.uname().nodename,
            "rootless_project": PROJECT,
        },
        "runtime_context": runtime_context(deadline=deadline),
        "source": source_state(project_dir, deadline=deadline),
        "job_quiescence": settled_queue_snapshot(deadline),
        "containers": inspect_project(deadline=deadline),
    }


def write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def service_map(snapshot: dict[str, Any]) -> dict[str, dict[str, Any]]:
    result: dict[str, dict[str, Any]] = {}
    for container in snapshot.get("containers", []):
        service = container.get("compose", {}).get("com.docker.compose.service")
        if service:
            if service in result:
                raise GuardError(f"duplicate Compose service container in snapshot: {service}")
            result[service] = container
    return result


def container_name_map(snapshot: dict[str, Any]) -> dict[str, dict[str, Any]]:
    result: dict[str, dict[str, Any]] = {}
    for container in snapshot.get("containers", []):
        name = container.get("name")
        if name:
            if name in result:
                raise GuardError(f"duplicate container name in snapshot: {name}")
            result[name] = container
    return result


def compare_preserved_runtime(
    previous: dict[str, Any], actual: dict[str, Any], service: str
) -> None:
    """Compare the identity/data contract while permitting a new container ID."""
    for field in (
        "name",
        "env_hashes",
        "networks",
        "network_details",
        "options",
        "user",
        "working_dir",
        "rootless_runtime",
    ):
        if previous.get(field) != actual.get(field):
            raise GuardError(f"runtime {field} changed for {service}")
    previous_mounts = {
        mount["destination"]: mount
        for mount in previous.get("mounts", [])
        if mount.get("destination") != PROBE_MOUNT_TARGET
    }
    actual_mounts = {
        mount["destination"]: mount
        for mount in actual.get("mounts", [])
        if mount.get("destination") != PROBE_MOUNT_TARGET
    }
    if previous_mounts != actual_mounts:
        raise GuardError(f"data mounts/options changed for {service}")
    if not actual.get("state", {}).get("running"):
        raise GuardError(f"service is not running: {service}")


def compare_untargeted_runtime(
    baseline: dict[str, Any], current: dict[str, Any]
) -> None:
    """Refuse to proceed if a non-target container changed during the apply."""
    old = container_name_map(baseline)
    new = container_name_map(current)
    target_names = {
        service_map(baseline)[service]["name"]
        for service in TARGET_SERVICES
        if service in service_map(baseline)
    }
    old_other = set(old) - target_names
    new_other = set(new) - target_names
    if old_other != new_other:
        raise GuardError("untargeted container set changed")
    for name in sorted(old_other):
        previous, actual = old[name], new[name]
        for field in (
            "id",
            "image",
            "env_hashes",
            "mounts",
            "options",
            "networks",
            "network_details",
        ):
            if previous.get(field) != actual.get(field):
                raise GuardError(f"untargeted container changed: {name}")


def compare_source_and_quiescence(
    baseline: dict[str, Any], current: dict[str, Any]
) -> None:
    if baseline.get("runtime_context") != current.get("runtime_context"):
        raise GuardError("Compose and direct Podman runtime context changed")
    if baseline.get("source") != current.get("source"):
        raise GuardError("dirty live source state changed since baseline capture")
    stability = current.get("job_quiescence", {}).get("stability") or {}
    if not current.get("job_quiescence", {}).get("quiescent") or not stability.get("stable"):
        raise GuardError("fresh queue or active-job settled-quiescence check failed")


def verify_untouched_targets(
    baseline: dict[str, Any],
    current: dict[str, Any],
    untouched_services: tuple[str, ...],
) -> None:
    old, new = service_map(baseline), service_map(current)
    for service in untouched_services:
        if service not in old or service not in new:
            raise GuardError(f"service missing during guarded apply: {service}")
        if old[service].get("id") != new[service].get("id"):
            raise GuardError(f"untouched target was recreated: {service}")
        compare_preserved_runtime(old[service], new[service], service)


def artifact_hashes(root: Path) -> dict[str, str]:
    files = (
        "docker/healthchecks/unstract-services.sh",
        "docker/healthchecks/http-readiness.sh",
        "docker/healthchecks/postgres-readiness.sh",
        "docker/docker-compose-dev-essentials.yaml",
        "docker/compose.train.healthchecks.yaml",
        "docker/compose.train.worker-healthchecks.yaml",
    )
    return {file: sha256_file(root / file) for file in files}


def source_tree_hash(root: Path) -> str:
    return run(["git", "-C", str(root), "rev-parse", "HEAD^{tree}"]).stdout.strip()


def load_lock(path: Path) -> dict[str, Any]:
    try:
        lock = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise GuardError(f"cannot read candidate lock {path}: {exc}") from exc
    if lock.get("schema") != "unstract-health-candidate/v1":
        raise GuardError("candidate lock has an unsupported schema")
    if not lock.get("source_commit") or not lock.get("candidate_version"):
        raise GuardError("candidate lock must pin source_commit and candidate_version")
    if not lock.get("source_tree"):
        raise GuardError("candidate lock must pin the candidate source tree")
    images = lock.get("images")
    if not isinstance(images, dict) or set(images) != set(TARGET_SERVICES):
        raise GuardError("candidate lock must pin exactly the targeted service images")
    for service in TARGET_SERVICES:
        image = images[service]
        if (
            not isinstance(image, dict)
            or not image.get("reference")
            or not image.get("id")
            or not image.get("digest")
        ):
            raise GuardError(f"candidate image lock is incomplete for {service}")
    return lock


def load_baseline(path: Path) -> dict[str, Any]:
    try:
        baseline = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise GuardError(f"cannot read baseline {path}: {exc}") from exc
    if baseline.get("schema") != "unstract-deployment-prep/v2":
        raise GuardError("baseline has an unsupported schema; capture a fresh baseline")
    if baseline.get("source", {}).get("schema") != SOURCE_STATE_SCHEMA:
        raise GuardError("baseline source state is incomplete; capture a fresh baseline")
    if not isinstance(baseline.get("containers"), list):
        raise GuardError("baseline container snapshot is missing")
    if not isinstance(baseline.get("runtime_context"), dict):
        raise GuardError("baseline runtime context is missing; capture a fresh baseline")
    stability = baseline.get("job_quiescence", {}).get("stability") or {}
    if not stability.get("stable"):
        raise GuardError("baseline quiescence is not settled; capture a fresh baseline")
    return baseline


def command_lock(args: argparse.Namespace) -> int:
    """Write a complete lock only from already-present candidate images."""
    source = Path(args.candidate_source)
    source_commit = run(["git", "-C", str(source), "rev-parse", "HEAD"]).stdout.strip()
    require_clean_candidate_source(source, source_commit)
    references: dict[str, str] = {}
    for item in args.image:
        service, separator, reference = item.partition("=")
        if not separator or service not in TARGET_SERVICES or not reference:
            raise GuardError(
                "--image must be repeated as service=image-reference for every target service"
            )
        if service in references:
            raise GuardError(f"duplicate candidate image mapping for {service}")
        references[service] = reference
    missing = set(TARGET_SERVICES) - set(references)
    if missing:
        raise GuardError(f"candidate image mappings are missing: {sorted(missing)}")
    images: dict[str, dict[str, str]] = {}
    for service, reference in sorted(references.items()):
        rows = parse_json_output(
            run(["podman", "image", "inspect", reference]),
            f"candidate image {reference}",
        )
        if not rows:
            raise GuardError(f"candidate image inspect returned no rows for {service}")
        row = rows[0]
        digest = image_digest(row)
        if not row.get("Id") or not digest:
            raise GuardError(f"candidate image has no immutable identity for {service}")
        images[service] = {"reference": reference, "id": row["Id"], "digest": digest}
    lock = {
        "schema": "unstract-health-candidate/v1",
        "created_at": utc_now(),
        "source_commit": source_commit,
        "source_tree": source_tree_hash(source),
        "candidate_version": args.candidate_version,
        "artifacts": artifact_hashes(source),
        "images": images,
    }
    write_json(Path(args.output), lock)
    print(f"lock: wrote immutable source and image lock for {len(images)} services")
    return 0


def candidate_image_snapshot(
    lock: dict[str, Any], *, deadline: OperationDeadline | None = None
) -> dict[str, dict[str, Any]]:
    result: dict[str, dict[str, Any]] = {}
    for service, expected in lock["images"].items():
        inspected = run(
            ["podman", "image", "inspect", expected["reference"]],
            check=False,
            deadline=deadline,
        )
        if inspected.returncode != 0:
            raise GuardError(f"candidate image is absent: {expected['reference']}")
        rows = parse_json_output(inspected, f"candidate image {expected['reference']}")
        if not rows:
            raise GuardError(f"candidate image inspect returned no rows for {service}")
        row = rows[0]
        actual = {
            "reference": expected["reference"],
            "id": row.get("Id") or row.get("ID"),
            "digest": image_digest(row),
        }
        if actual["id"] != expected["id"] or actual["digest"] != expected["digest"]:
            raise GuardError(
                f"candidate image identity changed for {service}: "
                f"expected {expected['id']} / {expected['digest']}, "
                f"found {actual['id']} / {actual['digest']}"
            )
        result[service] = actual
    return result


def compose_config(
    project_dir: Path,
    compose_files: tuple[str, ...],
    *,
    candidate_version: str,
    probe_source: Path,
    image_override: Path | None = None,
    deadline: OperationDeadline | None = None,
) -> dict[str, Any]:
    env = os.environ.copy()
    env["VERSION"] = candidate_version
    env["UNSTRACT_HEALTHCHECK_SOURCE"] = str(probe_source)
    files = compose_files + ((str(image_override),) if image_override else ())
    args = ["docker", "compose"]
    for compose_file in files:
        args.extend(["-f", compose_file])
    args.extend(["config", "--format", "json"])
    return parse_json_output(
        run(args, cwd=project_dir, env=env, deadline=deadline), "Compose config"
    )


def compose_environment(value: Any) -> dict[str, Any]:
    if value is None:
        return {}
    if isinstance(value, list):
        result: dict[str, Any] = {}
        for item in value:
            if not isinstance(item, str):
                raise GuardError("Compose environment contains a non-string entry")
            key, separator, item_value = item.partition("=")
            result[key] = item_value if separator else None
        return result
    if isinstance(value, dict):
        return {str(key): item for key, item in value.items()}
    raise GuardError("Compose environment is not a mapping")


def compose_value_hash(value: Any) -> dict[str, Any]:
    if value is None:
        raise GuardError("Compose environment contains a host-inherited value")
    text = str(value)
    return {"length": len(text), "sha256": sha256_bytes(text.encode())}


def check_health_contract(
    service: str, value: dict[str, Any], *, runtime: bool = False
) -> None:
    expected = health_contract(service)
    test = value.get("test") if not runtime else value.get("test")
    if test != expected["test"]:
        raise GuardError(f"healthcheck command identity mismatch for {service}")
    for field in ("interval_ns", "timeout_ns", "start_period_ns", "retries"):
        actual = value.get(field)
        if runtime and field.endswith("_ns") and actual is None:
            # Older captures did not include normalized timing fields.  A
            # fresh capture is required because timing identity is part of the
            # deployment contract.
            raise GuardError(f"healthcheck timing identity is missing for {service}")
        if actual != expected[field]:
            raise GuardError(f"healthcheck {field} mismatch for {service}")


def check_candidate_config(
    config: dict[str, Any],
    baseline: dict[str, Any],
    lock: dict[str, Any],
    baseline_config: dict[str, Any] | None = None,
) -> None:
    services = config.get("services") or {}
    missing = set(TARGET_SERVICES) - set(services)
    if missing:
        raise GuardError(f"candidate Compose config is missing services: {sorted(missing)}")
    if (config.get("networks") or {}).get("default", {}).get("name") != EXPECTED_NETWORK:
        raise GuardError("candidate Compose config changes the unstract-network name")
    old = service_map(baseline)
    for service in TARGET_SERVICES:
        candidate = services[service]
        expected_image = lock["images"][service]["reference"]
        if candidate.get("image") != expected_image:
            raise GuardError(
                f"candidate Compose image mismatch for {service}: "
                f"{candidate.get('image')} != {expected_image}"
            )
        if service not in old:
            raise GuardError(f"baseline has no existing container for {service}")
        old_name = old[service]["name"]
        if candidate.get("container_name") != old_name:
            raise GuardError(
                f"container identity changed for {service}: "
                f"{candidate.get('container_name')} != {old_name}"
            )
        networks = candidate.get("networks") or {}
        if EXPECTED_NETWORK not in networks and "default" not in networks:
            raise GuardError(f"candidate service {service} leaves {EXPECTED_NETWORK}")
        healthcheck = candidate.get("healthcheck") or {}
        unexpected_health_fields = set(healthcheck) - {
            "test",
            "interval",
            "timeout",
            "start_period",
            "retries",
        }
        if unexpected_health_fields:
            raise GuardError(
                f"candidate healthcheck has unsupported fields for {service}: "
                f"{sorted(unexpected_health_fields)}"
            )
        health_test = healthcheck.get("test") or []
        if not health_test or health_test == ["NONE"]:
            raise GuardError(f"candidate healthcheck is missing for {service}")
        check_health_contract(
            service,
            {
                "test": health_test,
                "interval_ns": duration_ns(healthcheck.get("interval")),
                "timeout_ns": duration_ns(healthcheck.get("timeout")),
                "start_period_ns": duration_ns(healthcheck.get("start_period")),
                "retries": healthcheck.get("retries"),
            },
        )
        if service in CORE_SERVICES:
            probe_mounts = [
                mount
                for mount in candidate.get("volumes", [])
                if mount.get("target") == PROBE_MOUNT_TARGET
            ]
            if len(probe_mounts) != 1 or probe_mounts[0].get("read_only") is not True:
                raise GuardError(
                    f"candidate trusted probe mount is missing or writable for {service}"
                )
        old_mounts = {
            mount["destination"]: mount
            for mount in old[service].get("mounts", [])
            if mount.get("destination") != PROBE_MOUNT_TARGET
        }
        candidate_mounts = {
            mount.get("target"): mount
            for mount in candidate.get("volumes", [])
            if mount.get("target") != PROBE_MOUNT_TARGET
        }
        for destination, old_mount in old_mounts.items():
            new_mount = candidate_mounts.get(destination)
            if not new_mount:
                raise GuardError(f"candidate removed {service} mount {destination}")
            old_source = (
                old_mount.get("name") or old_mount.get("source")
                if old_mount.get("type") == "volume"
                else old_mount.get("source")
            )
            new_source = new_mount.get("source")
            if old_source and new_source and old_source != new_source:
                raise GuardError(
                    f"candidate changed {service} mount source for {destination}: "
                    f"{new_source} != {old_source}"
                )
            candidate_read_only = new_mount.get("read_only", False)
            if not isinstance(candidate_read_only, bool):
                raise GuardError(
                    f"candidate mount access is not a boolean for {service} {destination}"
                )
            if old_mount.get("rw") is not None:
                expected_read_only = not bool(old_mount["rw"])
                if candidate_read_only != expected_read_only:
                    raise GuardError(
                        f"candidate changed mount access for {service} {destination}"
                    )
        if set(candidate_mounts) != set(old_mounts):
            raise GuardError(f"candidate changed data mounts for {service}")

        candidate_environment = compose_environment(candidate.get("environment"))
        authored_services = (baseline_config or {}).get("services") or {}
        if service not in authored_services:
            raise GuardError(f"baseline Compose environment is missing for {service}")
        old_authored_environment = compose_environment(
            authored_services[service].get("environment")
        )
        allowed_additions = ALLOWED_ENV_ADDITIONS.get(service, set())
        for key, value in old_authored_environment.items():
            if key not in candidate_environment:
                raise GuardError(f"candidate removed authored environment for {service}: {key}")
            if compose_value_hash(candidate_environment[key]) != compose_value_hash(value):
                raise GuardError(f"candidate changed environment for {service}: {key}")
        for key, value in candidate_environment.items():
            if key not in old_authored_environment and key not in allowed_additions:
                raise GuardError(f"candidate added environment for {service}: {key}")


def compare_baseline_current(
    baseline: dict[str, Any], current: dict[str, Any], *, allow_new_probe: bool
) -> None:
    if baseline.get("runtime_context") != current.get("runtime_context"):
        raise GuardError("Compose and direct Podman runtime context changed")
    old = service_map(baseline)
    new = service_map(current)
    for service in TARGET_SERVICES:
        if service not in old or service not in new:
            raise GuardError(f"service {service} is missing from baseline or current runtime")
        previous, actual = old[service], new[service]
        for field in (
            "name",
            "env_hashes",
            "networks",
            "network_details",
            "options",
            "user",
            "working_dir",
            "rootless_runtime",
        ):
            if previous.get(field) != actual.get(field):
                raise GuardError(f"runtime {field} drifted for {service}")
        previous_mounts = {
            mount["destination"]: mount
            for mount in previous.get("mounts", [])
            if allow_new_probe or mount.get("destination") != PROBE_MOUNT_TARGET
        }
        actual_mounts = {
            mount["destination"]: mount
            for mount in actual.get("mounts", [])
            if allow_new_probe or mount.get("destination") != PROBE_MOUNT_TARGET
        }
        if previous_mounts != actual_mounts:
            raise GuardError(f"runtime mounts/options drifted for {service}")
        if not previous.get("state", {}).get("running"):
            raise GuardError(f"baseline service {service} was not running")
    stability = current.get("job_quiescence", {}).get("stability") or {}
    if not current.get("job_quiescence", {}).get("quiescent") or not stability.get("stable"):
        raise GuardError("fresh job quiescence check did not remain settled")


def compare_post_apply(
    baseline: dict[str, Any],
    current: dict[str, Any],
    lock: dict[str, Any],
    expected_services: tuple[str, ...],
) -> None:
    old, new = service_map(baseline), service_map(current)
    for service in expected_services:
        previous, actual = old[service], new[service]
        candidate = lock["images"][service]
        if actual["image"]["id"] != candidate["id"]:
            raise GuardError(f"post-apply image ID mismatch for {service}")
        if actual["image"].get("digest") != candidate["digest"]:
            raise GuardError(f"post-apply image digest mismatch for {service}")
        if actual["name"] != previous["name"]:
            raise GuardError(f"post-apply container name changed for {service}")
        if actual["networks"] != previous["networks"]:
            raise GuardError(f"post-apply networks changed for {service}")
        if actual.get("network_details") != previous.get("network_details"):
            raise GuardError(f"post-apply network options changed for {service}")
        if actual.get("options") != previous.get("options"):
            raise GuardError(f"post-apply container options changed for {service}")
        old_mounts = {
            mount["destination"]: mount
            for mount in previous.get("mounts", [])
            if mount.get("destination") != PROBE_MOUNT_TARGET
        }
        new_mounts = {
            mount["destination"]: mount
            for mount in actual.get("mounts", [])
            if mount.get("destination") != PROBE_MOUNT_TARGET
        }
        if old_mounts != new_mounts:
            raise GuardError(f"post-apply persistent mounts/options changed for {service}")
        previous_env = previous.get("env_hashes", {})
        actual_env = actual.get("env_hashes", {})
        for key, value in previous_env.items():
            if actual_env.get(key) != value:
                raise GuardError(f"post-apply environment value changed for {service}: {key}")
        additions = set(actual_env) - set(previous_env)
        if additions - ALLOWED_ENV_ADDITIONS.get(service, set()):
            raise GuardError(f"post-apply environment additions changed for {service}")
        if not actual["state"].get("running"):
            raise GuardError(f"post-apply service is not running: {service}")
        actual_health = actual.get("health", {}).get("configured") or {}
        if not actual_health.get("configured"):
            raise GuardError(f"post-apply healthcheck is not configured: {service}")
        check_health_contract(service, actual_health, runtime=True)
        if actual["health"]["runtime"].get("status") != "healthy":
            raise GuardError(f"post-apply service is not healthy: {service}")
    stability = current.get("job_quiescence", {}).get("stability") or {}
    if not current.get("job_quiescence", {}).get("quiescent") or not stability.get("stable"):
        raise GuardError("post-apply queue snapshot is not settled")


def require_clean_candidate_source(
    path: Path, expected_commit: str, expected_tree: str | None = None
) -> None:
    if not path.exists():
        raise GuardError(f"candidate source path does not exist: {path}")
    actual = run(["git", "-C", str(path), "rev-parse", "HEAD"]).stdout.strip()
    if actual != expected_commit:
        raise GuardError(f"candidate source commit {actual} != {expected_commit}")
    if expected_tree:
        actual_tree = source_tree_hash(path)
        if actual_tree != expected_tree:
            raise GuardError(f"candidate source tree {actual_tree} != {expected_tree}")
    status = run(["git", "-C", str(path), "status", "--porcelain", "--untracked-files=all"]).stdout
    if status.strip():
        raise GuardError("candidate source must be clean; refusing an untracked build")


def verify_artifacts(root: Path, lock: dict[str, Any]) -> None:
    expected = lock.get("artifacts") or {}
    actual = artifact_hashes(root)
    if set(expected) != set(actual):
        raise GuardError(
            "candidate artifact manifest does not match the guarded source files"
        )
    for name, digest in expected.items():
        if actual.get(name) != digest:
            raise GuardError(f"candidate artifact hash mismatch: {name}")


@contextlib.contextmanager
def advisory_lock(
    *,
    timeout_seconds: float = DEFAULT_LOCK_TIMEOUT_SECONDS,
    deadline: OperationDeadline | None = None,
) -> Iterator[None]:
    """Hold a DB advisory lock across both targeted recreation batches."""
    process = subprocess.Popen(
        [
            "podman",
            "exec",
            "-i",
            "unstract-db",
            "sh",
            "-c",
            'exec psql -XAtq -U "$POSTGRES_USER" -d "$POSTGRES_DB"',
        ],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        env=runtime_command_env(["podman"], os.environ.copy()),
    )
    assert process.stdin is not None
    assert process.stdout is not None
    timeout = deadline.remaining(timeout_seconds) if deadline else timeout_seconds
    try:
        process.stdin.write(ADVISORY_LOCK_SQL + "\n")
        process.stdin.flush()
    except (BrokenPipeError, OSError) as exc:
        with contextlib.suppress(Exception):
            process.kill()
        raise GuardError("could not send the deployment lock query") from exc
    ready, _, _ = select.select([process.stdout], [], [], timeout)
    if not ready:
        with contextlib.suppress(Exception):
            process.kill()
        with contextlib.suppress(Exception):
            process.wait(timeout=5)
        raise GuardError("timed out acquiring the Train deployment advisory lock")
    result = process.stdout.readline().strip()
    if result != "t":
        with contextlib.suppress(Exception):
            process.kill()
        with contextlib.suppress(Exception):
            process.wait(timeout=5)
        raise GuardError("another Train health deployment already holds the advisory lock")
    try:
        yield
    finally:
        try:
            process.stdin.write(ADVISORY_UNLOCK_SQL + "\n\\q\n")
            process.stdin.flush()
            process.wait(timeout=deadline.remaining(10) if deadline else 10)
        except (OSError, subprocess.TimeoutExpired):
            with contextlib.suppress(Exception):
                process.kill()
            with contextlib.suppress(Exception):
                process.wait(timeout=5)


@contextlib.contextmanager
def local_operation_lock(
    path: Path, *, deadline: OperationDeadline | None = None
) -> Iterator[None]:
    """Serialize this guard even while the DB container itself is replaced."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a+", encoding="utf-8") as handle:
        while True:
            try:
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if deadline:
                    delay = min(1.0, deadline.remaining())
                else:
                    delay = 1.0
                time.sleep(delay)
        try:
            yield
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def capture_and_write(
    project_dir: Path,
    output: Path,
    *,
    operation_deadline: OperationDeadline | None = None,
) -> dict[str, Any]:
    snapshot = capture(project_dir, deadline=operation_deadline)
    write_json(output, snapshot)
    return snapshot


def compose_args(compose_files: tuple[str, ...]) -> list[str]:
    args = ["docker", "compose"]
    for compose_file in compose_files:
        args.extend(["-f", compose_file])
    return args


def write_image_override(lock: dict[str, Any], path: Path) -> None:
    """Pin each target to the exact locked reference without touching source."""
    lines = [
        "# Generated by train_health_deployment_guard.py; do not edit.",
        "services:",
    ]
    for service in TARGET_SERVICES:
        reference = lock["images"][service]["reference"]
        if any(character.isspace() for character in reference) or "\n" in reference:
            raise GuardError(f"candidate image reference contains whitespace: {service}")
        lines.extend([f"  {service}:", f"    image: {json.dumps(reference)}"])
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


@contextlib.contextmanager
def candidate_image_override(lock: dict[str, Any]) -> Iterator[Path]:
    handle = tempfile.NamedTemporaryFile(
        mode="w", prefix="unstract-health-images-", suffix=".yaml", delete=False
    )
    path = Path(handle.name)
    handle.close()
    try:
        write_image_override(lock, path)
        yield path
    finally:
        with contextlib.suppress(FileNotFoundError):
            path.unlink()


def targeted_up(
    project_dir: Path,
    compose_files: tuple[str, ...],
    services: tuple[str, ...],
    *,
    candidate_version: str,
    probe_source: Path,
    image_override: Path | None = None,
    deadline: OperationDeadline | None = None,
) -> None:
    env = os.environ.copy()
    env["VERSION"] = candidate_version
    env["UNSTRACT_HEALTHCHECK_SOURCE"] = str(probe_source)
    files = compose_files + ((str(image_override),) if image_override else ())
    args = compose_args(files)
    args.extend(
        [
            "up",
            "-d",
            "--no-deps",
            "--force-recreate",
            "--no-build",
            "--pull",
            "never",
            *services,
        ]
    )
    run(args, cwd=project_dir, env=env, deadline=deadline)


def wait_healthy(
    services: tuple[str, ...],
    timeout_seconds: int = 240,
    *,
    operation_deadline: OperationDeadline | None = None,
) -> None:
    end = time.monotonic() + timeout_seconds
    states: dict[str, Any] = {}
    while time.monotonic() < end:
        if operation_deadline:
            operation_deadline.remaining()
        current = service_map(
            {"containers": inspect_project(deadline=operation_deadline)}
        )
        states = {
            service: current.get(service, {}).get("health", {}).get("runtime", {}).get("status")
            for service in services
        }
        if all(value == "healthy" for value in states.values()):
            return
        delay = min(5, max(0, end - time.monotonic()))
        if operation_deadline:
            delay = min(delay, operation_deadline.remaining())
        if delay:
            time.sleep(delay)
    raise GuardError(f"targeted services did not become healthy: {states}")


def wait_running(
    services: tuple[str, ...],
    timeout_seconds: int = 240,
    *,
    operation_deadline: OperationDeadline | None = None,
) -> None:
    end = time.monotonic() + timeout_seconds
    states: dict[str, Any] = {}
    while time.monotonic() < end:
        if operation_deadline:
            operation_deadline.remaining()
        current = service_map(
            {"containers": inspect_project(deadline=operation_deadline)}
        )
        states = {
            service: current.get(service, {}).get("state", {}).get("status")
            for service in services
        }
        if all(value == "running" for value in states.values()):
            return
        delay = min(5, max(0, end - time.monotonic()))
        if operation_deadline:
            delay = min(delay, operation_deadline.remaining())
        if delay:
            time.sleep(delay)
    raise GuardError(f"targeted services did not become running: {states}")


def commit_backups(
    snapshot: dict[str, Any],
    backup_dir: Path,
    *,
    deadline: OperationDeadline | None = None,
) -> dict[str, Any]:
    backup_dir.mkdir(parents=True, exist_ok=True)
    write_json(backup_dir / "baseline.json", snapshot)
    tag_prefix = "localhost/unstract-health-backup-"
    backup_images: dict[str, Any] = {
        "schema": BACKUP_SCHEMA,
        "created_at": utc_now(),
        "services": {},
    }
    for service in TARGET_SERVICES:
        container = service_map(snapshot)[service]
        tag = tag_prefix + service.replace("_", "-") + ":" + snapshot["captured_at"].replace(":", "").replace("+", "-")
        run(
            ["podman", "commit", "--pause=false", container["id"], tag],
            deadline=deadline,
        )
        image_rows = parse_json_output(
            run(["podman", "image", "inspect", tag], deadline=deadline),
            f"backup image {service}",
        )
        if not image_rows or not image_rows[0].get("Id"):
            raise GuardError(f"backup image has no immutable ID for {service}")
        image = image_rows[0]
        digest = image_digest(image)
        if not digest:
            raise GuardError(f"backup image has no immutable digest for {service}")
        backup_images["services"][service] = {
            "reference": tag,
            "id": image["Id"],
            "digest": digest,
            "old_container_id": container["id"],
            "old_image": container.get("image"),
            "old_health": container.get("health"),
            "old_env_hashes": container.get("env_hashes"),
            "old_mounts": container.get("mounts"),
            "old_options": container.get("options"),
            "old_networks": container.get("networks"),
            "old_network_details": container.get("network_details"),
            "old_name": container.get("name"),
        }
    write_json(backup_dir / "backup-images.json", backup_images)
    return backup_images


def backup_service_record(backup_images: dict[str, Any], service: str) -> dict[str, Any]:
    records = backup_images.get("services")
    if isinstance(records, dict) and isinstance(records.get(service), dict):
        return records[service]
    # Read manifests produced by the first preparation revision so rollback
    # remains possible if the coordinator already has one on the private host.
    reference = backup_images.get(service)
    if isinstance(reference, str):
        return {"reference": reference}
    raise GuardError(f"backup manifest has no service record for {service}")


def rollback_override(
    backup_images: dict[str, Any], path: Path, services: tuple[str, ...] = TARGET_SERVICES
) -> None:
    lines = [
        "# Generated by train_health_deployment_guard.py; do not edit.",
        "services:",
    ]
    for service in services:
        record = backup_service_record(backup_images, service)
        reference = record.get("reference")
        if not reference:
            raise GuardError(f"backup image reference missing for {service}")
        lines.extend(
            [
                f"  {service}:",
                f"    image: {json.dumps(reference)}",
                '    healthcheck: {test: ["NONE"]}',
            ]
        )
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def command_capture(args: argparse.Namespace) -> int:
    deadline = OperationDeadline(args.operation_timeout)
    capture_and_write(
        Path(args.project_dir), Path(args.output), operation_deadline=deadline
    )
    return 0


def prepare(
    args: argparse.Namespace,
    image_override: Path,
    *,
    operation_deadline: OperationDeadline,
) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
    baseline = load_baseline(Path(args.baseline))
    lock = load_lock(Path(args.candidate_lock))
    require_clean_candidate_source(
        Path(args.candidate_source), lock["source_commit"], lock.get("source_tree")
    )
    verify_artifacts(Path(args.candidate_source), lock)
    candidate_images = candidate_image_snapshot(lock, deadline=operation_deadline)
    if not candidate_images:
        raise GuardError("no candidate images were verified")
    current = capture(Path(args.project_dir), deadline=operation_deadline)
    compare_baseline_current(baseline, current, allow_new_probe=False)
    compare_untargeted_runtime(baseline, current)
    compare_source_and_quiescence(baseline, current)
    config = compose_config(
        Path(args.project_dir),
        tuple(args.compose_file or DEFAULT_COMPOSE_FILES),
        candidate_version=lock["candidate_version"],
        probe_source=Path(args.probe_source),
        image_override=image_override,
        deadline=operation_deadline,
    )
    authored_baseline = compose_config(
        Path(args.project_dir),
        tuple(args.compose_file or DEFAULT_COMPOSE_FILES),
        candidate_version=lock["candidate_version"],
        probe_source=Path(args.probe_source),
        deadline=operation_deadline,
    )
    check_candidate_config(config, baseline, lock, authored_baseline)
    return baseline, lock, current


def record_replacements(
    baseline: dict[str, Any],
    current: dict[str, Any],
    lock: dict[str, Any],
    services: tuple[str, ...],
    *,
    strict: bool = True,
) -> dict[str, Any]:
    old, new = service_map(baseline), service_map(current)
    records: dict[str, Any] = {}
    unresolved: list[str] = []
    for service in services:
        previous = old.get(service)
        actual = new.get(service)
        if previous is None or actual is None:
            if strict:
                raise GuardError(f"cannot identify replacement container for {service}")
            unresolved.append(service)
            continue
        if actual.get("id") == previous.get("id"):
            continue
        expected = lock["images"][service]
        if actual.get("image", {}).get("id") != expected.get("id"):
            if strict:
                raise GuardError(
                    f"replacement image cannot be identified safely for {service}"
                )
            unresolved.append(service)
            continue
        if actual.get("image", {}).get("digest") != expected.get("digest"):
            if strict:
                raise GuardError(
                    f"replacement image digest cannot be identified safely for {service}"
                )
            unresolved.append(service)
            continue
        records[service] = {
            "old_container_id": previous.get("id"),
            "replacement_container_id": actual.get("id"),
            "candidate_image_id": expected.get("id"),
            "candidate_image_digest": expected.get("digest"),
            "observed_at": current.get("captured_at"),
        }
    return {
        "schema": REPLACEMENT_SCHEMA,
        "services": records,
        "unresolved": sorted(set(unresolved)),
    }


def write_replacement_manifest(
    backup_dir: Path, manifest: dict[str, Any], *, name: str = "replacements.json"
) -> None:
    write_json(backup_dir / name, manifest)


def verify_replacement_ids(
    current: dict[str, Any], replacement_manifest: dict[str, Any]
) -> None:
    current_services = service_map(current)
    for service, record in (replacement_manifest.get("services") or {}).items():
        actual = current_services.get(service)
        if actual is None or actual.get("id") != record.get("replacement_container_id"):
            raise GuardError(
                f"replacement container identity changed before rollback: {service}"
            )


def rollback_compose_files(args: argparse.Namespace) -> tuple[str, ...]:
    configured = tuple(
        getattr(args, "rollback_compose_file", None)
        or getattr(args, "compose_file", None)
        or DEFAULT_COMPOSE_FILES
    )
    # The old runtime did not have the new health overlay.  Keep the original
    # owner files and append the generated image/health override last.
    return tuple(
        path
        for path in configured
        if "compose.train.worker-healthchecks.yaml" not in path
        and "compose.train.healthchecks.yaml" not in path
    )


def verify_rollback_result(
    baseline: dict[str, Any],
    final: dict[str, Any],
    backup_images: dict[str, Any],
    replacement_manifest: dict[str, Any],
    *,
    operation_deadline: OperationDeadline,
) -> None:
    old, new = service_map(baseline), service_map(final)
    for service in replacement_manifest.get("services", {}):
        actual = new.get(service)
        previous = old.get(service)
        record = backup_service_record(backup_images, service)
        if actual is None or previous is None:
            raise GuardError(f"rollback service is missing: {service}")
        if actual.get("image", {}).get("id") != record.get("id"):
            raise GuardError(f"rollback backup image ID mismatch for {service}")
        if not record.get("digest"):
            raise GuardError(f"rollback backup image digest is missing for {service}")
        if actual.get("image", {}).get("digest") != record.get("digest"):
            raise GuardError(f"rollback backup image digest mismatch for {service}")
        compare_preserved_runtime(previous, actual, service)
        old_health = previous.get("health") or {}
        new_health = actual.get("health") or {}
        if old_health.get("configured") != new_health.get("configured"):
            raise GuardError(f"rollback health configuration changed for {service}")
        if (old_health.get("runtime") or {}).get("status") != (
            new_health.get("runtime") or {}
        ).get("status"):
            raise GuardError(f"rollback health state was not restored for {service}")
    compare_untargeted_runtime(baseline, final)
    compare_source_and_quiescence(baseline, final)


def compensating_rollback(
    args: argparse.Namespace,
    baseline: dict[str, Any],
    backup_images: dict[str, Any],
    replacement_manifest: dict[str, Any],
    backup_dir: Path,
    *,
    operation_deadline: OperationDeadline,
) -> None:
    services = tuple((replacement_manifest.get("services") or {}).keys())
    if not services:
        raise GuardError("no exact replacement IDs were recorded for compensating rollback")
    current = capture(Path(args.project_dir), deadline=operation_deadline)
    compare_source_and_quiescence(baseline, current)
    verify_replacement_ids(current, replacement_manifest)
    override = backup_dir / "compensating-rollback.override.yaml"
    rollback_override(backup_images, override, services)
    rollback_files = rollback_compose_files(args) + (str(override),)
    with advisory_lock(deadline=operation_deadline):
        # Recheck identity and quiescence after acquiring the DB lock.  The
        # process may be disconnected when db itself is recreated; the local
        # operation lock remains held for that bounded transaction.
        locked = capture(Path(args.project_dir), deadline=operation_deadline)
        compare_source_and_quiescence(baseline, locked)
        verify_replacement_ids(locked, replacement_manifest)
        targeted_up(
            Path(args.project_dir),
            rollback_files,
            services,
            candidate_version="rollback-unused",
            probe_source=Path(args.probe_source),
            deadline=operation_deadline,
        )
        wait_running(services, operation_deadline=operation_deadline)
    final = capture(Path(args.project_dir), deadline=operation_deadline)
    verify_rollback_result(
        baseline,
        final,
        backup_images,
        replacement_manifest,
        operation_deadline=operation_deadline,
    )
    write_json(backup_dir / "post-compensating-rollback.json", final)
    write_json(backup_dir / "compensating-rollback.json", replacement_manifest)


def apply_batch(
    args: argparse.Namespace,
    baseline: dict[str, Any],
    lock: dict[str, Any],
    backup_dir: Path,
    image_override: Path,
    services: tuple[str, ...],
    untouched_services: tuple[str, ...],
    applied_services: tuple[str, ...],
    *,
    operation_deadline: OperationDeadline,
) -> dict[str, Any]:
    compose_files = tuple(args.compose_file or DEFAULT_COMPOSE_FILES)
    with advisory_lock(deadline=operation_deadline):
        fresh = capture(Path(args.project_dir), deadline=operation_deadline)
        compare_untargeted_runtime(baseline, fresh)
        compare_source_and_quiescence(baseline, fresh)
        verify_untouched_targets(baseline, fresh, untouched_services)
        if applied_services:
            compare_post_apply(baseline, fresh, lock, applied_services)
        candidate_image_snapshot(lock, deadline=operation_deadline)
        config = compose_config(
            Path(args.project_dir),
            compose_files,
            candidate_version=lock["candidate_version"],
            probe_source=Path(args.probe_source),
            image_override=image_override,
            deadline=operation_deadline,
        )
        authored_baseline = compose_config(
            Path(args.project_dir),
            compose_files,
            candidate_version=lock["candidate_version"],
            probe_source=Path(args.probe_source),
            deadline=operation_deadline,
        )
        check_candidate_config(config, baseline, lock, authored_baseline)
        # Take the final settled sample after all preflight commands and
        # immediately before the targeted Compose mutation.
        final_quiescence = settled_queue_snapshot(operation_deadline)
        if not final_quiescence.get("stability", {}).get("stable"):
            raise GuardError("queue was not settled immediately before targeted recreation")
        targeted_up(
            Path(args.project_dir),
            compose_files,
            services,
            candidate_version=lock["candidate_version"],
            probe_source=Path(args.probe_source),
            image_override=image_override,
            deadline=operation_deadline,
        )
        observed = capture(Path(args.project_dir), deadline=operation_deadline)
        replacements = record_replacements(baseline, observed, lock, services)
        write_replacement_manifest(backup_dir, replacements, name=f"replacements-{services[0]}.json")
        wait_healthy(services, operation_deadline=operation_deadline)
        final = capture(Path(args.project_dir), deadline=operation_deadline)
        compare_post_apply(baseline, final, lock, services)
        compare_untargeted_runtime(baseline, final)
        compare_source_and_quiescence(baseline, final)
        return replacements


def command_preflight(args: argparse.Namespace) -> int:
    deadline = OperationDeadline(args.operation_timeout)
    lock = load_lock(Path(args.candidate_lock))
    with candidate_image_override(lock) as image_override:
        prepare(args, image_override, operation_deadline=deadline)
    print(
        "preflight: candidate source, image lock, Compose identity, runtime, "
        "data, network, environment, queue, and active-job state verified"
    )
    return 0


def command_apply(args: argparse.Namespace) -> int:
    if args.confirm != CONFIRM_TOKEN:
        raise GuardError(f"apply requires --confirm {CONFIRM_TOKEN}")
    operation_deadline = OperationDeadline(args.operation_timeout)
    backup_dir = Path(args.backup_dir)
    attempted: list[str] = []
    applied: list[str] = []
    backup_images: dict[str, Any] | None = None
    replacement_manifest: dict[str, Any] = {
        "schema": REPLACEMENT_SCHEMA,
        "services": {},
        "unresolved": [],
    }
    with local_operation_lock(backup_dir / ".guard.lock", deadline=operation_deadline):
        lock_hint = load_lock(Path(args.candidate_lock))
        with candidate_image_override(lock_hint) as image_override:
            try:
                baseline, lock, _ = prepare(
                    args, image_override, operation_deadline=operation_deadline
                )
                with advisory_lock(deadline=operation_deadline):
                    fresh = capture(Path(args.project_dir), deadline=operation_deadline)
                    compare_baseline_current(baseline, fresh, allow_new_probe=False)
                    compare_untargeted_runtime(baseline, fresh)
                    compare_source_and_quiescence(baseline, fresh)
                    candidate_image_snapshot(lock, deadline=operation_deadline)
                    backup_images = commit_backups(
                        fresh, backup_dir, deadline=operation_deadline
                    )
                rollback_override(backup_images, backup_dir / "rollback.override.yaml")
                write_json(backup_dir / "candidate-images.json", lock["images"])

                attempted.extend(WORKER_SERVICES)
                worker_replacements = apply_batch(
                    args,
                    baseline,
                    lock,
                    backup_dir,
                    image_override,
                    WORKER_SERVICES,
                    CORE_SERVICES,
                    (),
                    operation_deadline=operation_deadline,
                )
                replacement_manifest["services"].update(
                    worker_replacements.get("services", {})
                )
                applied.extend(WORKER_SERVICES)
                write_replacement_manifest(backup_dir, replacement_manifest)

                attempted.extend(CORE_SERVICES)
                core_replacements = apply_batch(
                    args,
                    baseline,
                    lock,
                    backup_dir,
                    image_override,
                    CORE_SERVICES,
                    (),
                    tuple(applied),
                    operation_deadline=operation_deadline,
                )
                replacement_manifest["services"].update(
                    core_replacements.get("services", {})
                )
                applied.extend(CORE_SERVICES)
                write_replacement_manifest(backup_dir, replacement_manifest)
                final = capture(Path(args.project_dir), deadline=operation_deadline)
                compare_post_apply(baseline, final, lock, TARGET_SERVICES)
                compare_untargeted_runtime(baseline, final)
                compare_source_and_quiescence(baseline, final)
                write_json(backup_dir / "post-apply.json", final)
            except Exception as exc:
                if backup_images is not None and attempted:
                    try:
                        failed_state = capture(
                            Path(args.project_dir), deadline=operation_deadline
                        )
                        discovered = record_replacements(
                            baseline,
                            failed_state,
                            lock,
                            tuple(attempted),
                            strict=False,
                        )
                        replacement_manifest["services"].update(
                            discovered.get("services", {})
                        )
                        replacement_manifest["unresolved"] = sorted(
                            set(replacement_manifest.get("unresolved", []))
                            | set(discovered.get("unresolved", []))
                        )
                        write_replacement_manifest(
                            backup_dir, replacement_manifest, name="failed-replacements.json"
                        )
                        if replacement_manifest["services"]:
                            compensating_rollback(
                                args,
                                baseline,
                                backup_images,
                                replacement_manifest,
                                backup_dir,
                                operation_deadline=operation_deadline,
                            )
                    except Exception as rollback_error:
                        raise GuardError(
                            "guarded apply failed and compensating rollback failed; "
                            f"manual recovery is required: {type(rollback_error).__name__}"
                        ) from exc
                raise
    print(f"apply: verified {len(TARGET_SERVICES)} targeted services; backup={backup_dir}")
    return 0


def command_rollback(args: argparse.Namespace) -> int:
    if args.confirm != CONFIRM_TOKEN:
        raise GuardError(f"rollback requires --confirm {CONFIRM_TOKEN}")
    operation_deadline = OperationDeadline(args.operation_timeout)
    backup_dir = Path(args.backup_dir)
    try:
        backup_images = json.loads(
            (backup_dir / "backup-images.json").read_text(encoding="utf-8")
        )
    except (OSError, json.JSONDecodeError) as exc:
        raise GuardError(f"cannot read backup image manifest: {exc}") from exc
    if backup_images.get("schema") != BACKUP_SCHEMA:
        raise GuardError("backup image manifest has an unsupported schema")
    if set(backup_images.get("services") or {}) != set(TARGET_SERVICES):
        raise GuardError("backup image manifest does not cover the exact target set")
    for service in TARGET_SERVICES:
        record = backup_service_record(backup_images, service)
        if not record.get("id") or not record.get("digest"):
            raise GuardError(f"backup image manifest lacks immutable identity for {service}")
    replacement_path = backup_dir / "replacements.json"
    if not replacement_path.exists():
        raise GuardError("rollback requires the exact replacement ID manifest")
    try:
        replacement_manifest = json.loads(replacement_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise GuardError(f"cannot read replacement manifest: {exc}") from exc
    if replacement_manifest.get("schema") != REPLACEMENT_SCHEMA:
        raise GuardError("replacement manifest has an unsupported schema")
    baseline = load_baseline(backup_dir / "baseline.json")
    with local_operation_lock(backup_dir / ".guard.lock", deadline=operation_deadline):
        compensating_rollback(
            args,
            baseline,
            backup_images,
            replacement_manifest,
            backup_dir,
            operation_deadline=operation_deadline,
        )
        final = json.loads(
            (backup_dir / "post-compensating-rollback.json").read_text(encoding="utf-8")
        )
        write_json(backup_dir / "post-rollback.json", final)
    print(
        "rollback: verified "
        f"{len(replacement_manifest.get('services', {}))} exact replacement services; "
        f"backup={backup_dir}"
    )
    return 0


def add_common(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--project-dir", default=str(DEFAULT_PROJECT_DIR))
    parser.add_argument("--compose-file", action="append", default=None)
    parser.add_argument("--baseline", required=True)
    parser.add_argument("--candidate-source", required=True)
    parser.add_argument("--candidate-lock", required=True)
    parser.add_argument("--probe-source", required=True)
    parser.add_argument(
        "--operation-timeout",
        type=float,
        default=DEFAULT_APPLY_TIMEOUT_SECONDS,
        help="total monotonic deadline for the guarded operation",
    )


def parser() -> argparse.ArgumentParser:
    root = argparse.ArgumentParser(description=__doc__)
    sub = root.add_subparsers(dest="command", required=True)
    lock_parser = sub.add_parser(
        "lock", help="lock a clean source commit and already-built candidate images"
    )
    lock_parser.add_argument("--candidate-source", required=True)
    lock_parser.add_argument("--candidate-version", required=True)
    lock_parser.add_argument("--image", action="append", required=True)
    lock_parser.add_argument("--output", required=True)
    capture_parser = sub.add_parser("capture", help="read-only sanitized runtime snapshot")
    capture_parser.add_argument("--project-dir", default=str(DEFAULT_PROJECT_DIR))
    capture_parser.add_argument("--output", required=True)
    capture_parser.add_argument(
        "--operation-timeout",
        type=float,
        default=DEFAULT_COMMAND_TIMEOUT_SECONDS,
    )
    preflight_parser = sub.add_parser("preflight", help="read-only candidate and drift checks")
    add_common(preflight_parser)
    apply_parser = sub.add_parser("apply", help="explicit targeted recreation")
    add_common(apply_parser)
    apply_parser.add_argument("--backup-dir", required=True)
    apply_parser.add_argument("--confirm", required=True)
    rollback_parser = sub.add_parser("rollback", help="explicit targeted compensating rollback")
    rollback_parser.add_argument("--project-dir", default=str(DEFAULT_PROJECT_DIR))
    rollback_parser.add_argument("--rollback-compose-file", action="append", default=None)
    rollback_parser.add_argument("--probe-source", required=True)
    rollback_parser.add_argument("--backup-dir", required=True)
    rollback_parser.add_argument("--confirm", required=True)
    rollback_parser.add_argument(
        "--operation-timeout",
        type=float,
        default=DEFAULT_ROLLBACK_TIMEOUT_SECONDS,
    )
    return root


def main() -> int:
    args = parser().parse_args()
    try:
        if args.command == "capture":
            return command_capture(args)
        if args.command == "lock":
            return command_lock(args)
        if args.command == "preflight":
            return command_preflight(args)
        if args.command == "apply":
            return command_apply(args)
        if args.command == "rollback":
            return command_rollback(args)
        raise GuardError(f"unknown command {args.command}")
    except GuardError as exc:
        print(f"guard: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
