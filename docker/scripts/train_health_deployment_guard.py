#!/usr/bin/env python3
"""Guarded, targeted deployment helper for the Train Unstract health checks.

``capture`` records a sanitized runtime snapshot. ``preflight`` refuses to
continue when the dirty live checkout, ignored Compose inputs, container
identity, mounts, networks, or environment hashes drift, and refreshes the
private durable replay files beside the candidate lock. The mutating ``start``,
``apply``, and ``rollback`` phases require explicit confirmation tokens and an
external candidate image lock. ``start`` is the authoritative whole-project
startup path; ``apply`` and ``rollback`` recreate only the 24 health-covered
workloads in two bounded batches. They never build, pull, delete source files,
reset the checkout, or run project-wide destructive Compose commands.

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
import math
import os
import re
import select
import shlex
import stat
import subprocess
import sys
import tempfile
import time
from collections.abc import Iterator
from copy import deepcopy
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, NamedTuple

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
# A recreation may need time to settle before the existing strict zero-work
# predicate is met again.  The longer post-recreation window does not permit
# work to be consumed, cleared, or ignored: every following mutation still
# requires three consecutive all-zero samples.
POST_RECREATION_QUIESCENCE_MAX_WAIT_SECONDS = 120.0

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
FAILURE_SCHEMA = "unstract-health-failure/v1"
RUNTIME_ENVIRONMENT_SCHEMA = "unstract-health-runtime-environment/v1"
RUNTIME_ENVIRONMENT_FILENAME = "runtime-environment.override.yaml"
CANDIDATE_IMAGE_SCHEMA = "unstract-health-candidate-image/v1"
CANDIDATE_IMAGE_FILENAME = "candidate-image.override.yaml"
COMPOSE_SETTINGS_SCHEMA = "unstract-health-compose-settings/v1"
COMPOSE_SETTINGS_FILENAME = "compose-settings.env"
START_CONFIRM_TOKEN = "START_UNSTRACT_HEALTH"
LIVE_COMPOSE_TRAIN = "docker/compose.train.yaml"
LIVE_ENV_RELATIVE = "docker/.env"
REPLAY_MANIFEST_SCHEMA = "unstract-durable-replay/v2"
REPLAY_MANIFEST_FILENAME = "durable-replay-manifest.json"
COMPOSE_SNAPSHOT_SCHEMA = "unstract-compose-snapshot/v2"
COMPOSE_SNAPSHOT_MANIFEST_FILENAME = "compose-snapshot-manifest.json"
COMPOSE_SNAPSHOT_DIR_PREFIX = "compose-snapshot-"
COMPOSE_SNAPSHOT_PROBE_RELATIVE = Path("__helper__/unstract-services.sh")
COMPOSE_SNAPSHOT_PRIVATE_RELATIVE = Path("__private__")
DURATION_TOKEN = re.compile(
    r"(?P<number>(?:\d+(?:\.\d*)?|\.\d+))(?P<unit>ns|us|µs|ms|h|m|s)"
)
DURATION_UNITS_NS = {
    "ns": 1,
    "us": 1_000,
    "µs": 1_000,
    "ms": 1_000_000,
    "s": 1_000_000_000,
    "m": 60_000_000_000,
    "h": 3_600_000_000_000,
}

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


class ComposeSnapshot(NamedTuple):
    """A daemon-visible immutable copy of every Compose launch input."""

    root: Path
    manifest_path: Path
    project_dir: Path
    paths: dict[str, str]
    # Maps a rendered snapshot-relative bind source back to the authoritative
    # project-tree path it represented when the snapshot was frozen.  Compose
    # providers are allowed to retain the snapshot spelling in ``config``
    # output, but creation must receive the authoritative source in an explicit
    # bind override as well as use it for comparison with the running service.
    bind_sources: dict[str, str]

    @property
    def probe_path(self) -> Path:
        return Path(self.paths["__probe_source__"])

    def path_for(self, value: str | Path) -> str:
        return self.paths.get(str(value), str(value))


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


class QuiescenceTimeout(GuardError):
    """A bounded zero-work wait that retains only sanitized observations."""

    def __init__(
        self,
        *,
        phase: str,
        max_wait_seconds: float,
        observations: list[dict[str, Any]],
        reason: str = "bounded settled-quiescence interval elapsed",
    ) -> None:
        self.phase = phase
        self.max_wait_seconds = max_wait_seconds
        self.observations = observations
        self.reason = reason
        super().__init__(
            "queue and active-job state did not remain quiescent for the bounded "
            f"stability interval (phase={phase})"
        )

    def evidence(self) -> dict[str, Any]:
        """Return failure evidence without command output or environment values."""
        return {
            "schema": "unstract-health-quiescence-timeout/v1",
            "reason": self.reason,
            "phase": self.phase,
            "max_wait_seconds": self.max_wait_seconds,
            "required_consecutive_samples": QUIESCENCE_REQUIRED_SAMPLES,
            "sample_interval_seconds": QUIESCENCE_SAMPLE_INTERVAL_SECONDS,
            "observations": self.observations,
        }


def exception_reason(error: BaseException, *, limit: int = 240) -> str:
    """Return bounded, single-line failure context without secret-looking values."""
    text = " ".join(str(error).split())
    text = re.sub(
        r"(?i)(password|passwd|secret|token|authorization|api[_-]?key)(\s*[=:]\s*)\S+",
        r"\1\2<redacted>",
        text,
    )
    if not text:
        text = "<no detail>"
    if len(text) > limit:
        text = text[: limit - 3] + "..."
    return f"{type(error).__name__}: {text}"


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
    """Normalize full Compose durations and Podman nanosecond values."""
    if value is None:
        return None
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value if value >= 0 else None
    if isinstance(value, float):
        if not math.isfinite(value) or value < 0:
            return None
        return round(value)
    text = str(value).strip().lower()
    if text == "0":
        return 0
    position = 0
    total = Decimal(0)
    while position < len(text):
        match = DURATION_TOKEN.match(text, position)
        if match is None:
            return None
        try:
            number = Decimal(match.group("number"))
        except InvalidOperation:
            return None
        if not number.is_finite() or number < 0:
            return None
        total += number * DURATION_UNITS_NS[match.group("unit")]
        if not total.is_finite():
            return None
        position = match.end()
    return int(total.to_integral_value()) if position else None


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
    pass_fds: tuple[int, ...] = (),
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
            pass_fds=pass_fds,
        )
    except subprocess.TimeoutExpired as exc:
        raise GuardError(f"command timed out: {shlex.join(args)}") from exc
    except OSError as exc:
        raise GuardError(f"cannot execute {shlex.join(args)}: {exc}") from exc
    if check and result.returncode != 0:
        raise GuardError(f"command failed ({result.returncode}): {shlex.join(args)}")
    return result


def _compose_input_path(project_dir: Path, value: str | Path) -> Path:
    """Resolve a Compose input the same way the explicit project directory does."""
    path = Path(value)
    if path.is_absolute():
        return path
    return project_dir.resolve() / path


def _compose_project_directory(
    project_dir: Path,
    compose_files: tuple[str, ...],
    *,
    snapshot: ComposeSnapshot | None = None,
) -> Path:
    """Return Compose's path base for the first launch file.

    Compose uses the first ``-f`` file's parent directory as its default
    project directory. An explicit ``--project-directory`` overrides that
    default for includes and merged-file relative paths, so a frozen snapshot
    must preserve the first file's parent rather than promote the snapshot
    root to the path base.
    """
    if not compose_files:
        raise GuardError("Compose launch requires at least one Compose file")
    first = str(compose_files[0])
    if snapshot is None:
        return _compose_input_path(project_dir, first).resolve().parent
    frozen = snapshot.paths.get(first)
    if frozen is None:
        raise GuardError("Compose snapshot does not contain the base Compose file")
    frozen_path = Path(frozen).resolve()
    try:
        frozen_path.relative_to(snapshot.root)
    except ValueError as exc:
        raise GuardError("Compose snapshot base file escapes its root") from exc
    return frozen_path.parent


def _read_compose_input(path: Path, *, description: str) -> bytes:
    """Read one Compose input while resolving a symlink only once."""
    try:
        metadata = path.lstat()
        resolved = path.resolve(strict=True) if stat.S_ISLNK(metadata.st_mode) else path
        resolved_metadata = resolved.stat()
        if not stat.S_ISREG(resolved_metadata.st_mode):
            raise GuardError(f"{description} is not a regular file: {path}")
        flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
        try:
            descriptor = os.open(resolved, flags)
        except OSError:
            # The fallback is only for platforms without O_NOFOLLOW support.
            descriptor = os.open(resolved, os.O_RDONLY | getattr(os, "O_CLOEXEC", 0))
        try:
            chunks: list[bytes] = []
            while True:
                chunk = os.read(descriptor, 1024 * 1024)
                if not chunk:
                    break
                chunks.append(chunk)
            return b"".join(chunks)
        finally:
            os.close(descriptor)
    except GuardError:
        raise
    except OSError as exc:
        raise GuardError(f"cannot read {description}: {path}") from exc


def _snapshot_source_relative(project_dir: Path, source: Path, *, fallback: str) -> Path:
    """Choose a safe snapshot path while retaining project-relative layout."""
    project = project_dir.resolve()
    source = source.resolve()
    try:
        relative = source.relative_to(project)
    except ValueError:
        digest = sha256_bytes(str(source).encode("utf-8"))[:16]
        return Path("__external__") / digest / fallback
    if not relative.parts or any(part in {"", ".", ".."} for part in relative.parts):
        raise GuardError(f"Compose input has an unsafe relative path: {source}")
    return relative


def _compose_include_paths(path: Path) -> list[Path]:
    """Find literal Compose ``include`` entries without parsing interpolation."""
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError as exc:
        raise GuardError(f"cannot read Compose include source: {path}") from exc
    result: list[Path] = []
    for index, line in enumerate(lines):
        stripped = line.strip()
        if not stripped.startswith("include:"):
            continue
        indent = len(line) - len(line.lstrip())
        tail = stripped[len("include:") :].strip()
        values: list[str] = []
        if tail.startswith("[") and tail.endswith("]"):
            values.extend(part.strip().strip("'\"") for part in tail[1:-1].split(","))
        elif tail and not tail.startswith("#"):
            values.append(tail.strip("'\""))
        for following in lines[index + 1 :]:
            if following.strip() and len(following) - len(following.lstrip()) <= indent:
                break
            candidate = following.strip()
            if candidate.startswith("-"):
                value = candidate[1:].split("#", 1)[0].strip().strip("'\"")
                if value:
                    values.append(value)
        for value in values:
            if value and "${" not in value:
                result.append((path.parent / value).resolve())
    return result


def _compose_relative_references(path: Path) -> list[tuple[str, bool]]:
    """Find literal relative env-file and bind-source references.

    Compose files in the guarded stack use the short list syntax for these
    fields.  Keeping this scanner lexical avoids making the systemd launcher
    depend on a YAML package while still allowing us to freeze the files the
    provider opens itself.
    """
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError as exc:
        raise GuardError(f"cannot read Compose reference source: {path}") from exc
    result: list[tuple[str, bool]] = []
    section: str | None = None
    section_indent = -1
    for line in lines:
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        indent = len(line) - len(line.lstrip())
        if section is not None and indent <= section_indent and not stripped.startswith("-"):
            section = None
        if stripped.startswith("env_file:"):
            section, section_indent = "env_file", indent
            tail = stripped[len("env_file:") :].strip()
            if tail and not tail.startswith("#"):
                result.append((tail.strip("'\""), True))
            continue
        if stripped.startswith("volumes:"):
            section, section_indent = "volumes", indent
            continue
        if section is None or not stripped.startswith("-"):
            continue
        value = stripped[1:].split("#", 1)[0].strip().strip("'\"")
        if section == "env_file":
            reference = value
            private = True
        else:
            # Long syntax starts with ``type:``/``source:`` and is handled by
            # the provider directly; the repository's guarded files use the
            # short ``source:target[:options]`` form.
            if value.startswith(("type:", "source:")):
                continue
            reference = value.split(":", 1)[0]
            private = False
        if reference.startswith((".", "..")) and "${" not in reference:
            result.append((reference, private))
    return result


def _write_snapshot_file(
    root: Path,
    relative: Path,
    data: bytes,
    *,
    private: bool,
    executable: bool = False,
) -> str:
    """Write a snapshot file and return its content digest.

    Snapshot inputs are immutable after materialization, but executable
    helpers still need their execute bits when Compose runs them in a
    container.  Preserve only execute bits from the reviewed source while
    stripping every write bit.
    """
    destination = root / relative
    try:
        destination.relative_to(root)
    except ValueError as exc:
        raise GuardError(f"snapshot path escapes its root: {relative}") from exc
    destination.parent.mkdir(parents=True, exist_ok=True, mode=0o755)
    mode = 0o400 if private else 0o444
    if executable and not private:
        mode |= 0o111
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_CLOEXEC", 0)
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        descriptor = os.open(destination, flags, mode)
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
    except OSError as exc:
        raise GuardError(f"cannot write Compose snapshot input: {destination}") from exc
    os.chmod(destination, mode)
    return sha256_bytes(data)


def _write_snapshot_directory_passthrough(
    root: Path,
    relative: Path,
    source: Path,
) -> str:
    """Keep a runtime data directory at its original daemon-visible location."""
    destination = root / relative
    destination.parent.mkdir(parents=True, exist_ok=True, mode=0o755)
    try:
        resolved = source.resolve(strict=True)
        if not resolved.is_dir():
            raise GuardError(f"Compose directory reference is not a directory: {source}")
        destination.relative_to(root)
        if destination.is_symlink():
            if destination.resolve(strict=True) != resolved:
                raise GuardError(f"Compose directory reference changed: {destination}")
            return str(resolved)
        if destination.exists():
            raise GuardError(f"Compose directory reference collides with a snapshot file: {destination}")
        os.symlink(resolved, destination, target_is_directory=True)
    except GuardError:
        raise
    except OSError as exc:
        raise GuardError(f"cannot preserve Compose directory reference: {source}") from exc
    return str(resolved)


def _freeze_snapshot_tree(root: Path) -> None:
    """Make every snapshot directory traversable but non-user-writable."""
    for directory in sorted(
        (path for path in root.rglob("*") if path.is_dir() and not path.is_symlink()),
        reverse=True,
    ):
        os.chmod(directory, 0o500 if directory.name == COMPOSE_SNAPSHOT_PRIVATE_RELATIVE.name else 0o555)
    os.chmod(root, 0o555)


def materialize_compose_snapshot(
    *,
    project_dir: Path,
    compose_files: tuple[str, ...],
    live_env_file: Path | None,
    probe_source: Path,
    probe_source_sha256: str | None,
    image_override: Path | None,
    image_override_sha256: str | None,
    environment_override: Path | None,
    environment_override_sha256: str | None,
    destination: Path,
) -> ComposeSnapshot:
    """Create a durable daemon-visible snapshot preserving Compose includes."""
    if destination.exists():
        raise GuardError(f"Compose snapshot destination already exists: {destination}")
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.mkdir(mode=0o755)
    paths: dict[str, str] = {}
    entries: dict[str, dict[str, Any]] = {}
    passthroughs: dict[str, str] = {}
    bind_sources: dict[str, str] = {}
    copied: set[Path] = set()
    private_sources = {
        source.resolve()
        for source in (image_override, environment_override)
        if source is not None
    }

    def copy_source(
        source: Path,
        relative: Path,
        *,
        key: str | None = None,
        expected_sha256: str | None = None,
        private: bool = False,
        description: str = "Compose input",
    ) -> None:
        resolved = source.resolve(strict=True)
        if resolved in copied and (destination / relative).is_file():
            if key is not None:
                paths[key] = str((destination / relative).resolve())
            return
        data = _read_compose_input(source, description=description)
        actual = sha256_bytes(data)
        if expected_sha256 is not None and actual != expected_sha256:
            raise GuardError(f"{description} changed: {source}")
        source_mode = resolved.stat().st_mode
        digest = _write_snapshot_file(
            destination,
            relative,
            data,
            private=private,
            executable=bool(source_mode & 0o111),
        )
        copied.add(resolved)
        entries[str(relative)] = {
            "sha256": digest,
            "private": private,
        }
        if key is not None:
            paths[key] = str((destination / relative).resolve())

    def register_bind_source(relative: Path, source: Path) -> None:
        """Bind a rendered snapshot path to one exact project-tree source.

        The snapshot may retain an immutable copy (or a protected symlink) for
        Compose itself.  Its rendered ``config`` can still spell the temporary
        snapshot path, so record only ordinary project-relative bind sources
        that can be mapped back to the authoritative tree without inference.
        """
        if (
            not relative.parts
            or relative.is_absolute()
            or any(part in {"", ".", ".."} for part in relative.parts)
            or relative.parts[0].startswith("__")
        ):
            raise GuardError(f"Compose bind source has an unsafe snapshot path: {source}")
        try:
            authoritative = source.resolve(strict=True)
            expected = (project_dir.resolve() / relative).resolve(strict=True)
        except OSError as exc:
            raise GuardError(f"cannot resolve Compose bind source: {source}") from exc
        if authoritative != expected:
            raise GuardError(
                "Compose bind source does not map to the authoritative project tree: "
                f"{source}"
            )
        key = str(relative)
        prior = bind_sources.get(key)
        if prior is not None and prior != str(authoritative):
            raise GuardError(f"Compose bind source mapping changed: {source}")
        bind_sources[key] = str(authoritative)

    compose_sources: list[Path] = []
    pending_includes: list[Path] = []
    for argument in compose_files:
        source = _compose_input_path(project_dir, argument)
        resolved = source.resolve()
        if resolved in private_sources:
            relative = COMPOSE_SNAPSHOT_PRIVATE_RELATIVE / source.name
            copy_source(
                source,
                relative,
                key=argument,
                private=True,
                expected_sha256=(
                    image_override_sha256
                    if image_override is not None and resolved == image_override.resolve()
                    else environment_override_sha256
                ),
                description="private Compose override",
            )
        else:
            relative = _snapshot_source_relative(
                project_dir, source, fallback=Path(argument).name
            )
            copy_source(source, relative, key=argument, description="Compose file")
        compose_sources.append(source)
        pending_includes.extend(_compose_include_paths(source))
    seen_includes: set[Path] = set()
    while pending_includes:
        source = pending_includes.pop(0)
        source = source.resolve()
        if source in seen_includes:
            continue
        seen_includes.add(source)
        relative = _snapshot_source_relative(
            project_dir, source, fallback=source.name
        )
        copy_source(source, relative, description="Compose include")
        compose_sources.append(source)
        pending_includes.extend(_compose_include_paths(source))

    for compose_source in compose_sources:
        for reference, private in _compose_relative_references(compose_source):
            source = (compose_source.parent / reference).resolve()
            relative = _snapshot_source_relative(
                project_dir, source, fallback=source.name
            )
            if source.is_dir():
                passthroughs[str(relative)] = _write_snapshot_directory_passthrough(
                    destination, relative, source
                )
            else:
                copy_source(
                    source,
                    relative,
                    private=private,
                    description="private Compose env file" if private else "Compose bind file",
                )
            if not private:
                register_bind_source(relative, source)

    if live_env_file is not None:
        source = _compose_input_path(project_dir, live_env_file)
        relative = _snapshot_source_relative(project_dir, source, fallback=source.name)
        copy_source(
            source,
            relative,
            key=str(live_env_file),
            private=True,
            description="Compose environment file",
        )

    copy_source(
        probe_source,
        COMPOSE_SNAPSHOT_PROBE_RELATIVE,
        key=str(probe_source),
        expected_sha256=probe_source_sha256,
        description="health probe source",
    )
    paths["__probe_source__"] = paths[str(probe_source)]
    for source, expected_sha256, description in (
        (image_override, image_override_sha256, "candidate image override"),
        (environment_override, environment_override_sha256, "runtime environment override"),
    ):
        if source is None:
            continue
        relative = COMPOSE_SNAPSHOT_PRIVATE_RELATIVE / source.name
        copy_source(
            source,
            relative,
            key=str(source),
            expected_sha256=expected_sha256,
            private=True,
            description=description,
        )

    manifest_path = destination / COMPOSE_SNAPSHOT_MANIFEST_FILENAME
    manifest = {
        "schema": COMPOSE_SNAPSHOT_SCHEMA,
        "root": str(destination.resolve()),
        "project_dir": str(project_dir.resolve()),
        "paths": {
            key: str(Path(value).relative_to(destination.resolve()))
            for key, value in paths.items()
        },
        "entries": entries,
        "passthroughs": passthroughs,
        "bind_sources": bind_sources,
    }
    try:
        manifest_path.write_text(json.dumps(manifest, sort_keys=True, indent=2) + "\n", encoding="utf-8")
        os.chmod(manifest_path, 0o400)
    except OSError as exc:
        raise GuardError(f"cannot write Compose snapshot manifest: {manifest_path}") from exc
    _freeze_snapshot_tree(destination)
    return ComposeSnapshot(
        root=destination.resolve(),
        manifest_path=manifest_path.resolve(),
        project_dir=project_dir.resolve(),
        paths={key: str(destination / relative) for key, relative in manifest["paths"].items()},
        bind_sources=bind_sources,
    )


def _validate_snapshot_file(path: Path, expected_sha256: str, *, private: bool) -> None:
    try:
        metadata = path.lstat()
    except OSError as exc:
        raise GuardError(f"cannot read Compose snapshot file: {path}") from exc
    if not stat.S_ISREG(metadata.st_mode) or stat.S_IMODE(metadata.st_mode) & 0o222:
        raise GuardError(f"Compose snapshot file is writable or not regular: {path}")
    if hasattr(os, "getuid") and os.getuid() == 0 and metadata.st_uid != 0:
        raise GuardError(f"Compose snapshot file is not root-owned: {path}")
    actual = sha256_file(path)
    if actual != expected_sha256:
        raise GuardError(f"Compose snapshot file changed: {path}")
    if private and stat.S_IMODE(metadata.st_mode) != 0o400:
        raise GuardError(f"private Compose snapshot file has an unsafe mode: {path}")


def _validate_snapshot_parents(path: Path, root: Path) -> None:
    """Ensure no snapshot directory can be replaced by the launching user."""
    current = path.parent
    while True:
        try:
            metadata = current.lstat()
        except OSError as exc:
            raise GuardError(f"cannot read Compose snapshot directory: {current}") from exc
        if not stat.S_ISDIR(metadata.st_mode) or stat.S_IMODE(metadata.st_mode) & 0o222:
            raise GuardError(f"Compose snapshot directory is writable or not a directory: {current}")
        if hasattr(os, "getuid") and os.getuid() == 0 and metadata.st_uid != 0:
            raise GuardError(f"Compose snapshot directory is not root-owned: {current}")
        if current == root:
            return
        if current.parent == current:
            raise GuardError("Compose snapshot directory escaped its root")
        current = current.parent


def load_compose_snapshot(path: Path) -> ComposeSnapshot:
    """Validate a retained snapshot immediately before a Compose launch."""
    try:
        metadata = path.lstat()
        if not stat.S_ISREG(metadata.st_mode) or stat.S_IMODE(metadata.st_mode) & 0o222:
            raise GuardError(f"Compose snapshot manifest is writable or not regular: {path}")
        if hasattr(os, "getuid") and os.getuid() == 0 and metadata.st_uid != 0:
            raise GuardError(f"Compose snapshot manifest is not root-owned: {path}")
        manifest = json.loads(path.read_text(encoding="utf-8"))
    except GuardError:
        raise
    except (OSError, json.JSONDecodeError) as exc:
        raise GuardError(f"cannot read Compose snapshot manifest: {path}") from exc
    if manifest.get("schema") != COMPOSE_SNAPSHOT_SCHEMA:
        raise GuardError("Compose snapshot manifest has an unsupported schema")
    root = Path(manifest.get("root", "")).resolve()
    if root != path.resolve().parent or not root.is_dir():
        raise GuardError("Compose snapshot root does not match its manifest")
    root_metadata = root.lstat()
    if stat.S_IMODE(root_metadata.st_mode) & 0o222:
        raise GuardError("Compose snapshot root is writable")
    if hasattr(os, "getuid") and os.getuid() == 0 and root_metadata.st_uid != 0:
        raise GuardError("Compose snapshot root is not root-owned")
    entries = manifest.get("entries")
    paths_manifest = manifest.get("paths")
    passthroughs = manifest.get("passthroughs") or {}
    bind_sources = manifest.get("bind_sources")
    if (
        not isinstance(entries, dict)
        or not isinstance(paths_manifest, dict)
        or not isinstance(passthroughs, dict)
        or not isinstance(bind_sources, dict)
    ):
        raise GuardError("Compose snapshot manifest is incomplete")
    paths: dict[str, str] = {}
    for relative, entry in entries.items():
        if not isinstance(relative, str) or not isinstance(entry, dict):
            raise GuardError("Compose snapshot manifest contains an invalid entry")
        candidate = (root / relative).resolve()
        if candidate.parent == root and candidate.name == COMPOSE_SNAPSHOT_MANIFEST_FILENAME:
            raise GuardError("Compose snapshot manifest lists itself as an input")
        try:
            candidate.relative_to(root)
        except ValueError as exc:
            raise GuardError("Compose snapshot entry escapes its root") from exc
        _validate_snapshot_parents(candidate, root)
        digest = entry.get("sha256")
        if not isinstance(digest, str) or not re.fullmatch(r"[0-9a-f]{64}", digest):
            raise GuardError("Compose snapshot entry has an invalid digest")
        _validate_snapshot_file(candidate, digest, private=bool(entry.get("private")))
    for relative, target in passthroughs.items():
        if not isinstance(relative, str) or not isinstance(target, str):
            raise GuardError("Compose snapshot directory passthrough is invalid")
        candidate = root / relative
        try:
            candidate.relative_to(root)
            _validate_snapshot_parents(candidate, root)
            metadata = candidate.lstat()
            actual_target = candidate.resolve(strict=True)
        except (OSError, ValueError) as exc:
            raise GuardError("Compose snapshot directory passthrough is invalid") from exc
        if not stat.S_ISLNK(metadata.st_mode) or not actual_target.is_dir():
            raise GuardError("Compose snapshot directory passthrough is not a directory symlink")
        if str(actual_target) != str(Path(target).resolve()):
            raise GuardError("Compose snapshot directory passthrough target changed")
    for key, relative in paths_manifest.items():
        if not isinstance(key, str) or not isinstance(relative, str):
            raise GuardError("Compose snapshot path mapping is invalid")
        candidate = (root / relative).resolve()
        try:
            candidate.relative_to(root)
        except ValueError as exc:
            raise GuardError("Compose snapshot path escapes its root") from exc
        if relative not in entries:
            raise GuardError("Compose snapshot path mapping has no hashed entry")
        paths[key] = str(candidate)
    if "__probe_source__" not in paths:
        raise GuardError("Compose snapshot lacks the immutable probe source")
    project_dir_value = manifest.get("project_dir")
    if not isinstance(project_dir_value, str) or not project_dir_value:
        raise GuardError("Compose snapshot project directory is invalid")
    project_dir = Path(project_dir_value).resolve()
    if not Path(project_dir_value).is_absolute() or not project_dir.exists():
        raise GuardError("Compose snapshot project directory is invalid")
    validated_bind_sources: dict[str, str] = {}
    for relative, target in bind_sources.items():
        if not isinstance(relative, str) or not isinstance(target, str):
            raise GuardError("Compose snapshot bind source mapping is invalid")
        relative_path = Path(relative)
        if (
            not relative_path.parts
            or relative_path.is_absolute()
            or any(part in {"", ".", ".."} for part in relative_path.parts)
            or relative_path.parts[0].startswith("__")
            or relative not in entries and relative not in passthroughs
        ):
            raise GuardError("Compose snapshot bind source mapping is unsafe")
        if relative in entries and bool(entries[relative].get("private")):
            raise GuardError("Compose snapshot bind source mapping references private input")
        try:
            authoritative = Path(target)
            if not authoritative.is_absolute():
                raise ValueError("bind source is not absolute")
            expected = (project_dir / relative_path).resolve(strict=True)
            actual = authoritative.resolve(strict=True)
        except (OSError, ValueError) as exc:
            raise GuardError("Compose snapshot bind source mapping is invalid") from exc
        if actual != expected:
            raise GuardError("Compose snapshot bind source no longer matches the project tree")
        if relative in passthroughs:
            try:
                if (root / relative_path).resolve(strict=True) != actual:
                    raise GuardError(
                        "Compose snapshot bind source passthrough target changed"
                    )
            except OSError as exc:
                raise GuardError("Compose snapshot bind source passthrough is invalid") from exc
        validated_bind_sources[relative] = str(actual)
    return ComposeSnapshot(
        root=root,
        manifest_path=path.resolve(),
        project_dir=project_dir,
        paths=paths,
        bind_sources=validated_bind_sources,
    )


@contextlib.contextmanager
def _compose_snapshot_context(
    *,
    project_dir: Path,
    compose_files: tuple[str, ...],
    live_env_file: Path | None,
    probe_source: Path,
    probe_source_sha256: str | None,
    image_override: Path | None,
    image_override_sha256: str | None,
    environment_override: Path | None,
    environment_override_sha256: str | None,
    snapshot: ComposeSnapshot | None,
) -> Iterator[ComposeSnapshot]:
    if snapshot is not None:
        validated = load_compose_snapshot(snapshot.manifest_path)
        if validated != snapshot:
            raise GuardError("Compose snapshot changed after it was loaded")
        if snapshot.project_dir != project_dir.resolve():
            raise GuardError("Compose snapshot project directory changed")
        yield snapshot
        return
    with tempfile.TemporaryDirectory(prefix=".unstract-compose-") as temporary:
        yield materialize_compose_snapshot(
            project_dir=project_dir,
            compose_files=compose_files,
            live_env_file=live_env_file,
            probe_source=probe_source,
            probe_source_sha256=probe_source_sha256,
            image_override=image_override,
            image_override_sha256=image_override_sha256,
            environment_override=environment_override,
            environment_override_sha256=environment_override_sha256,
            destination=Path(temporary) / "tree",
        )


@contextlib.contextmanager
def bound_compose_inputs(
    *,
    project_dir: Path,
    compose_files: tuple[str, ...],
    live_env_file: Path | None,
    probe_source: Path,
    probe_source_sha256: str | None,
    image_override: Path | None,
    image_override_sha256: str | None,
    environment_override: Path | None,
    environment_override_sha256: str | None,
    snapshot: ComposeSnapshot | None = None,
) -> Iterator[tuple[dict[str, str], tuple[int, ...], ComposeSnapshot]]:
    """Bind every Compose input to a daemon-visible immutable snapshot path."""
    with _compose_snapshot_context(
        project_dir=project_dir,
        compose_files=compose_files,
        live_env_file=live_env_file,
        probe_source=probe_source,
        probe_source_sha256=probe_source_sha256,
        image_override=image_override,
        image_override_sha256=image_override_sha256,
        environment_override=environment_override,
        environment_override_sha256=environment_override_sha256,
        snapshot=snapshot,
    ) as active:
        required = list(compose_files)
        if live_env_file:
            required.append(str(live_env_file))
        required.append(str(probe_source))
        if image_override:
            required.append(str(image_override))
        if environment_override:
            required.append(str(environment_override))
        missing = [argument for argument in required if argument not in active.paths]
        if missing:
            raise GuardError(
                "Compose snapshot does not contain every launch input: "
                + ", ".join(missing)
            )
        replacements = {argument: active.path_for(argument) for argument in required}
        yield replacements, (), active


def _snapshot_bind_source_relative(value: str, snapshot: ComposeSnapshot) -> str | None:
    """Return a lexical snapshot-relative bind source, if one was rendered.

    Do not resolve the candidate before deciding whether it belongs to the
    snapshot: directory passthroughs intentionally resolve to their original
    host paths.  A provider spelling a source inside the snapshot must match a
    manifest-authorized bind source exactly; an unrecognized or escaping
    spelling is a hard error rather than a fallback to the temporary path.
    """
    if not os.path.isabs(value):
        return None
    root = os.path.abspath(str(snapshot.root))
    candidate = os.path.abspath(value)
    claims_snapshot_root = value == root or value.startswith(root + os.sep)
    try:
        inside_snapshot = os.path.commonpath((root, candidate)) == root
        resolves_inside_snapshot = (
            os.path.commonpath((root, os.path.realpath(value))) == root
        )
    except ValueError:
        return None
    if claims_snapshot_root and not inside_snapshot:
        raise GuardError("Compose rendered bind source escapes its snapshot")
    if not inside_snapshot:
        if resolves_inside_snapshot:
            raise GuardError("Compose rendered bind source aliases its snapshot")
        return None
    relative = Path(os.path.relpath(candidate, root))
    if (
        not relative.parts
        or relative == Path(".")
        or relative.is_absolute()
        or any(part in {"", ".", ".."} for part in relative.parts)
    ):
        raise GuardError("Compose rendered bind source is not a snapshot input")
    return str(relative)


def normalize_snapshot_bind_sources(
    config: dict[str, Any], snapshot: ComposeSnapshot
) -> dict[str, Any]:
    """Map provider-rendered snapshot bind paths to authoritative host paths.

    Compose still reads all includes, env files, and overrides from the frozen
    snapshot.  The normalized model supplies both guard comparison and the
    explicit bind-source override passed to container creation.
    """
    services = config.get("services")
    if not isinstance(services, dict):
        return config
    for service, definition in services.items():
        if not isinstance(definition, dict):
            continue
        mounts = definition.get("volumes")
        if mounts is None:
            continue
        if not isinstance(mounts, list):
            raise GuardError(f"Compose rendered volumes are invalid for {service}")
        for mount in mounts:
            if not isinstance(mount, dict):
                raise GuardError(f"Compose rendered mount is invalid for {service}")
            if mount.get("type") != "bind":
                continue
            source = mount.get("source")
            if mount.get("target") == PROBE_MOUNT_TARGET:
                expected_probe = os.path.abspath(str(snapshot.probe_path))
                if (
                    not isinstance(source, str)
                    or os.path.abspath(source) != expected_probe
                ):
                    raise GuardError(
                        "Compose rendered trusted probe mount does not use "
                        "the frozen probe source"
                    )
                # The immutable probe deliberately lives inside the snapshot.
                # It is content-hashed separately and excluded from persistent
                # mount identity comparison, so do not treat it as project data.
                continue
            if not isinstance(source, str) or not source:
                continue
            relative = _snapshot_bind_source_relative(source, snapshot)
            if relative is None:
                continue
            authoritative = snapshot.bind_sources.get(relative)
            if authoritative is None:
                raise GuardError(
                    "Compose rendered bind source is not authorized by its snapshot: "
                    f"{source}"
                )
            mount["source"] = authoritative
    return config


@contextlib.contextmanager
def stable_bind_compose_config(
    args: list[str],
    snapshot: ComposeSnapshot,
    *,
    env: dict[str, str],
    deadline: OperationDeadline | None,
    pass_fds: tuple[int, ...],
) -> Iterator[tuple[list[str], dict[str, Any]]]:
    """Give Compose the same stable bind sources that the guard compares.

    Frozen includes and env files must retain their snapshot path base.  A
    provider can retain that base in a bind source even when it names a
    symlink to live data, so resolving only the comparison model is not enough.
    Override only manifest-authorized bind sources, preserve every mount
    option, and require a second provider render to match the intended model
    exactly before the caller can use these same files for ``up``.
    """

    def render(launch_args: list[str]) -> dict[str, Any]:
        result = run(
            [*launch_args, "config", "--format", "json"],
            cwd=snapshot.root,
            env=env,
            deadline=deadline,
            pass_fds=pass_fds,
        )
        model = parse_json_output(result, "Compose config")
        if not isinstance(model, dict):
            raise GuardError("Compose config did not return an object")
        return model

    original = render(args)
    normalized = normalize_snapshot_bind_sources(deepcopy(original), snapshot)
    provider_model = deepcopy(normalized)
    services: dict[str, Any] = {}
    definitions = normalized.get("services")
    if not isinstance(definitions, dict):
        raise GuardError("Compose config services are invalid")
    for service, definition in definitions.items():
        if not isinstance(definition, dict):
            continue
        original_mounts = original["services"][service].get("volumes") or []
        mounts = definition.get("volumes") or []
        changed = []
        for index, (before, mount) in enumerate(zip(original_mounts, mounts)):
            if before.get("source") == mount.get("source"):
                continue
            override = deepcopy(mount)
            # Compose's JSON config retains replay escaping for literal dollar
            # signs; its create request decodes that escaping. Only the new
            # authoritative source is an unescaped runtime value. Every other
            # mount field was already serialized by the provider, so preserve
            # it exactly instead of escaping it a second time.
            override["source"] = mount["source"].replace("$", "$$")
            provider_model["services"][service]["volumes"][index]["source"] = override["source"]
            changed.append(override)
        if changed:
            services[service] = {"volumes": changed}
    if not services:
        yield args, normalized
        return

    with tempfile.TemporaryDirectory(prefix=".unstract-compose-binds-") as temporary:
        root = Path(temporary)
        relative = Path("bind-sources.override.json")
        digest = _write_snapshot_file(
            root,
            relative,
            json.dumps({"services": services}).encode("utf-8"),
            private=True,
        )
        os.chmod(root, 0o500)
        launch_args = [*args, "-f", str(root / relative)]
        rendered = render(launch_args)
        if rendered != provider_model:
            raise GuardError("Compose bind-source override changed the effective config")
        _validate_snapshot_file(root / relative, digest, private=True)
        _validate_snapshot_parents(root / relative, root)
        yield launch_args, normalized


def parse_json_output(result: subprocess.CompletedProcess[str], description: str) -> Any:
    try:
        return json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise GuardError(f"{description} did not return JSON") from exc


def image_digest(image: dict[str, Any]) -> str | None:
    return image.get("Digest") or next(iter(image.get("RepoDigests") or []), None)


def env_hashes(
    values: list[str] | None,
    *,
    container_id: str | None = None,
    config_hostname: Any = None,
) -> dict[str, dict[str, Any]]:
    result: dict[str, dict[str, Any]] = {}
    seen: set[str] = set()
    for item in values or []:
        if not isinstance(item, str):
            raise GuardError("runtime environment contains a non-string entry")
        key, separator, value = item.partition("=")
        if not separator:
            value = ""
        if not key:
            raise GuardError("runtime environment contains an empty key")
        if key in seen:
            raise GuardError(f"runtime environment contains a duplicate key: {key}")
        seen.add(key)
        # Podman injects HOSTNAME from the container ID only when both inspect
        # fields have the current ID prefix.  Recreated containers then receive
        # a new value even when the application environment is unchanged.  An
        # authored image/Compose environment or fixed hostname remains part of
        # the contract, including a HOSTNAME value that merely resembles an ID.
        if (
            key == "HOSTNAME"
            and isinstance(container_id, str)
            and len(container_id) >= 12
            and value == container_id[:12]
            and config_hostname == container_id[:12]
        ):
            continue
        result[key] = {"length": len(value), "sha256": sha256_bytes(value.encode())}
    return dict(sorted(result.items()))


def environment_values(values: list[str] | None) -> dict[str, str]:
    """Parse an inspect environment vector without exposing its values."""
    result: dict[str, str] = {}
    for item in values or []:
        if not isinstance(item, str):
            raise GuardError("runtime environment contains a non-string entry")
        key, separator, value = item.partition("=")
        if not key:
            raise GuardError("runtime environment contains an empty key")
        if key in result:
            raise GuardError(f"runtime environment contains a duplicate key: {key}")
        result[key] = value if separator else ""
    return result


def environment_hashes(
    values: dict[str, str],
    *,
    container_id: str | None = None,
    config_hostname: Any = None,
) -> dict[str, dict[str, Any]]:
    return env_hashes(
        [f"{key}={value}" for key, value in values.items()],
        container_id=container_id,
        config_hostname=config_hostname,
    )


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
    # Compose's ``healthcheck: {test: ["NONE"]}`` disables a healthcheck.
    # Podman reports that override as a Healthcheck object while an unchanged
    # container reports no object at all; normalize both to the same state so
    # rollback verification compares effective configuration.
    if test == ["NONE"]:
        return {"configured": False}
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
    source = mount.get("Source")
    if mount.get("Type") != "volume":
        source = normalize_bind_mount_source(source)
    return {
        "type": mount.get("Type"),
        "name": mount.get("Name"),
        "source": source,
        "destination": mount.get("Destination"),
        "rw": mount.get("RW"),
        "options": sorted(mount.get("Options") or []),
    }


def normalize_bind_mount_source(value: Any) -> Any:
    """Return a bind source's filesystem-aware absolute identity.

    Resolve symlinks before comparing paths so a parent segment such as
    ``docker/../data`` cannot hide a symlink that redirects the bind source.
    Keep relative-path handling separate from ``abspath`` because ``abspath``
    normalizes ``..`` before symlink resolution.
    """
    if not isinstance(value, str) or not value:
        return value
    if not os.path.isabs(value):
        value = os.path.join(os.getcwd(), value)
    return os.path.realpath(value)


def bind_mount_sources_match(left: Any, right: Any) -> bool:
    """Compare bind sources while failing closed on uncertain filesystem state."""
    left_path = normalize_bind_mount_source(left)
    right_path = normalize_bind_mount_source(right)
    if not isinstance(left_path, str) or not isinstance(right_path, str):
        return False
    try:
        left_exists = os.path.exists(left_path)
        right_exists = os.path.exists(right_path)
    except OSError:
        return False
    if left_exists != right_exists:
        return False
    if left_exists:
        try:
            return os.path.samefile(left_path, right_path)
        except OSError:
            return False
    return left_path == right_path


def compose_mount_source(config: dict[str, Any], mount: dict[str, Any]) -> Any:
    """Resolve a Compose volume alias to its project-scoped name."""
    source = mount.get("source")
    if mount.get("type") != "volume" or not isinstance(source, str):
        return source
    volumes = config.get("volumes") or {}
    definition = volumes.get(source)
    if isinstance(definition, dict):
        return definition.get("name") or source
    return source


def normalize_networks(
    value: dict[str, Any], *, container_id: str | None = None
) -> dict[str, dict[str, Any]]:
    """Keep stable network identity while omitting replacement-specific IPs."""
    result: dict[str, dict[str, Any]] = {}
    generated_alias = container_id[:12] if container_id else None
    for name, network in sorted(value.items()):
        aliases = network.get("Aliases") or []
        if generated_alias:
            aliases = [alias for alias in aliases if alias != generated_alias]
        result[name] = {
            "aliases": sorted(aliases),
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
                "env_hashes": env_hashes(
                    config.get("Env"),
                    container_id=item.get("Id"),
                    config_hostname=config.get("Hostname"),
                ),
                "mounts": [normalize_mount(mount) for mount in item.get("Mounts") or []],
                "options": runtime_options(item, config),
                "graphdriver": {
                    "driver": item.get("Driver"),
                    "upper_dir": (item.get("GraphDriver") or {}).get("Data", {}).get("UpperDir"),
                    "work_dir": (item.get("GraphDriver") or {}).get("Data", {}).get("WorkDir"),
                },
                "networks": sorted(networks),
                "network_details": normalize_networks(networks, container_id=item.get("Id")),
                "user": config.get("User"),
                "working_dir": config.get("WorkingDir"),
                "rootless_runtime": item.get("OCIRuntime"),
            }
        )
    return containers


def inspect_runtime_environment(
    project: str = PROJECT, *, deadline: OperationDeadline | None = None
) -> dict[str, dict[str, Any]]:
    """Read raw environment values privately for reviewed Compose preservation."""
    ids_result = run(
        ["podman", "ps", "-aq", "--filter", f"label=com.docker.compose.project={project}"],
        deadline=deadline,
    )
    ids = ids_result.stdout.split()
    if not ids:
        return {}
    raw = parse_json_output(
        run(["podman", "inspect", *ids], deadline=deadline),
        "podman inspect runtime environment",
    )
    result: dict[str, dict[str, Any]] = {}
    for item in raw:
        config = item.get("Config") or {}
        labels = config.get("Labels") or {}
        service = labels.get("com.docker.compose.service")
        if not service:
            continue
        if service in result:
            raise GuardError(f"duplicate Compose service environment: {service}")
        result[service] = {
            "id": item.get("Id"),
            "hostname": config.get("Hostname"),
            "values": environment_values(config.get("Env")),
        }
    return result


def source_file_fingerprint(path: Path) -> dict[str, Any]:
    """Hash one ignored Compose input without retaining its contents."""
    try:
        metadata = path.lstat()
    except FileNotFoundError:
        return {"missing": True}
    except OSError as exc:
        raise GuardError(f"cannot stat live Compose input {path}: {exc}") from exc
    if stat.S_ISLNK(metadata.st_mode):
        target = os.readlink(path)
        resolved = path.resolve()
        target_metadata = resolved.lstat()
        if not stat.S_ISREG(target_metadata.st_mode):
            raise GuardError(f"live Compose input does not resolve to a regular file: {path}")
        return {
            "symlink": target,
            "target_bytes": target_metadata.st_size,
            "target_sha256": sha256_file(resolved),
        }
    if not stat.S_ISREG(metadata.st_mode):
        return {"special": True, "mode": stat.S_IFMT(metadata.st_mode)}
    return {"bytes": metadata.st_size, "sha256": sha256_file(path)}


def live_compose_inputs(
    project_dir: Path, *, live_env_file: Path | None = None
) -> dict[str, dict[str, Any]]:
    env_path = live_env_file or (project_dir / LIVE_ENV_RELATIVE)
    return {
        LIVE_COMPOSE_TRAIN: source_file_fingerprint(project_dir / LIVE_COMPOSE_TRAIN),
        "live_env_file": {
            "path": str(env_path.resolve()),
            **source_file_fingerprint(env_path),
        },
    }


def source_state(
    project_dir: Path,
    *,
    live_env_file: Path | None = None,
    deadline: OperationDeadline | None = None,
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
        "live_inputs": live_compose_inputs(project_dir, live_env_file=live_env_file),
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


def settled_queue_snapshot(
    deadline: OperationDeadline | None = None,
    *,
    max_wait_seconds: float = QUIESCENCE_MAX_WAIT_SECONDS,
    phase: str = "guarded operation",
) -> dict[str, Any]:
    """Require consecutive zero-work samples before any destructive change."""
    if not math.isfinite(max_wait_seconds) or max_wait_seconds <= 0:
        raise GuardError("quiescence wait must be a positive finite duration")
    started = time.monotonic()
    end = started + max_wait_seconds
    consecutive = 0
    observations: list[dict[str, Any]] = []
    last: dict[str, Any] | None = None
    while True:
        try:
            last = queue_snapshot(deadline)
        except GuardError as exc:
            if deadline is not None and time.monotonic() >= deadline.ends_at:
                raise QuiescenceTimeout(
                    phase=phase,
                    max_wait_seconds=max_wait_seconds,
                    observations=observations,
                    reason="guarded operation deadline elapsed before settled quiescence",
                ) from exc
            raise
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
            try:
                remaining = min(remaining, deadline.remaining())
            except GuardError as exc:
                raise QuiescenceTimeout(
                    phase=phase,
                    max_wait_seconds=max_wait_seconds,
                    observations=observations,
                    reason="guarded operation deadline elapsed before settled quiescence",
                ) from exc
        if remaining <= 0:
            raise QuiescenceTimeout(
                phase=phase,
                max_wait_seconds=max_wait_seconds,
                observations=observations,
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
    project_dir: Path,
    *,
    live_env_file: Path | None = None,
    deadline: OperationDeadline | None = None,
    require_settled_quiescence: bool = True,
    quiescence_max_wait_seconds: float = QUIESCENCE_MAX_WAIT_SECONDS,
    quiescence_phase: str = "capture",
) -> dict[str, Any]:
    """Capture sanitized state, optionally retaining one raw transition sample.

    A raw sample is only for recording exact replacement IDs and timeout context
    after a recreation.  It is never accepted as a precondition for another
    mutation; callers that could mutate must use the default settled capture.
    """
    uid = os.getuid() if hasattr(os, "getuid") else None
    current_runtime_context = runtime_context(deadline=deadline)
    current_source = source_state(
        project_dir, live_env_file=live_env_file, deadline=deadline
    )
    job_quiescence = (
        settled_queue_snapshot(
            deadline,
            max_wait_seconds=quiescence_max_wait_seconds,
            phase=quiescence_phase,
        )
        if require_settled_quiescence
        else queue_snapshot(deadline)
    )
    containers = inspect_project(deadline=deadline)
    return {
        "schema": "unstract-deployment-prep/v2",
        "captured_at": utc_now(),
        "host": {
            "uid": uid,
            "hostname": os.uname().nodename,
            "rootless_project": PROJECT,
        },
        "runtime_context": current_runtime_context,
        "source": current_source,
        "job_quiescence": job_quiescence,
        "containers": containers,
    }


def write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def write_private_json(path: Path, value: Any, *, description: str) -> None:
    """Write sanitized replay metadata with the same private contract as overrides."""
    write_private_text(
        path,
        json.dumps(value, indent=2, sort_keys=True) + "\n",
        replace=True,
        description=description,
    )


def write_post_recreation_quiescence(
    backup_dir: Path,
    services: tuple[str, ...],
    snapshot: dict[str, Any],
    *,
    stage: str,
) -> None:
    """Persist a small, sanitized observation around one recreated batch."""
    if not services:
        raise GuardError("post-recreation quiescence evidence needs at least one service")
    if stage not in {"observed", "settled"}:
        raise GuardError(f"unsupported post-recreation quiescence stage: {stage}")
    job_quiescence = snapshot.get("job_quiescence")
    if not isinstance(job_quiescence, dict):
        raise GuardError("post-recreation snapshot lacks job quiescence")
    write_private_json(
        backup_dir / f"post-recreation-{stage}-{services[0]}.json",
        {
            "schema": "unstract-health-post-recreation-quiescence/v1",
            "stage": stage,
            "services": list(services),
            "captured_at": snapshot.get("captured_at"),
            "job_quiescence": job_quiescence,
        },
        description=f"post-recreation {stage} quiescence evidence",
    )


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
    if not isinstance(baseline.get("source", {}).get("live_inputs"), dict):
        raise GuardError(
            "baseline live Compose input hashes are missing; capture a fresh baseline"
        )
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
            "environment": environment_values((row.get("Config") or {}).get("Env")),
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
    live_env_file: Path | None = None,
    expected_source: dict[str, Any] | None = None,
    candidate_source: Path | None = None,
    candidate_lock: dict[str, Any] | None = None,
    image_override: Path | None = None,
    image_override_sha256: str | None = None,
    environment_override: Path | None = None,
    environment_override_sha256: str | None = None,
    settings_file: Path | None = None,
    settings_file_sha256: str | None = None,
    probe_source_sha256: str | None = None,
    snapshot: ComposeSnapshot | None = None,
    deadline: OperationDeadline | None = None,
) -> dict[str, Any]:
    verify_candidate_source_state(candidate_source, candidate_lock)
    verify_live_compose_inputs(
        project_dir,
        live_env_file=live_env_file,
        expected_source=expected_source,
    )
    env = os.environ.copy()
    if settings_file:
        settings = validate_compose_settings(
            settings_file,
            candidate_version=candidate_version,
            probe_source=probe_source,
            probe_source_sha256=probe_source_sha256,
            expected_sha256=settings_file_sha256,
        )
        env["VERSION"] = settings["VERSION"]
        env["UNSTRACT_HEALTHCHECK_SOURCE"] = settings["UNSTRACT_HEALTHCHECK_SOURCE"]
        probe_source_sha256 = settings["UNSTRACT_HEALTHCHECK_SOURCE_SHA256"]
    else:
        env["VERSION"] = candidate_version
        env["UNSTRACT_HEALTHCHECK_SOURCE"] = str(probe_source)
        validate_probe_source(probe_source, expected_sha256=probe_source_sha256)
    files = compose_files
    if image_override:
        validate_private_file(
            image_override,
            expected_sha256=image_override_sha256,
            description="candidate image override",
        )
        files += (str(image_override),)
    if environment_override:
        validate_private_override(
            environment_override, expected_sha256=environment_override_sha256
        )
        files += (str(environment_override),)
    with bound_compose_inputs(
        project_dir=project_dir,
        compose_files=files,
        live_env_file=live_env_file,
        probe_source=probe_source,
        probe_source_sha256=probe_source_sha256,
        image_override=image_override,
        image_override_sha256=image_override_sha256,
        environment_override=environment_override,
        environment_override_sha256=environment_override_sha256,
        snapshot=snapshot,
    ) as (bound_paths, pass_fds, active_snapshot):
        args = compose_args(
            project_dir,
            files,
            live_env_file=live_env_file,
            snapshot=active_snapshot,
        )
        bound_args = [bound_paths.get(argument, argument) for argument in args]
        final_settings = verify_compose_inputs_before_run(
            project_dir=project_dir,
            live_env_file=live_env_file,
            expected_source=expected_source,
            candidate_source=candidate_source,
            candidate_lock=candidate_lock,
            probe_source=probe_source,
            probe_source_sha256=probe_source_sha256,
            image_override=image_override,
            image_override_sha256=image_override_sha256,
            environment_override=environment_override,
            environment_override_sha256=environment_override_sha256,
            settings_file=settings_file,
            settings_file_sha256=settings_file_sha256,
            candidate_version=candidate_version,
        )
        if final_settings:
            env["VERSION"] = final_settings["VERSION"]
        env["UNSTRACT_HEALTHCHECK_SOURCE"] = bound_paths.get(
            str(probe_source), final_settings["UNSTRACT_HEALTHCHECK_SOURCE"]
            if final_settings
            else env["UNSTRACT_HEALTHCHECK_SOURCE"]
        )
        with stable_bind_compose_config(
            bound_args,
            active_snapshot,
            env=env,
            deadline=deadline,
            pass_fds=pass_fds,
        ) as (_, rendered):
            return rendered


def compose_environment(value: Any) -> dict[str, Any]:
    if value is None:
        return {}
    if isinstance(value, list):
        result: dict[str, Any] = {}
        for item in value:
            if not isinstance(item, str):
                raise GuardError("Compose environment contains a non-string entry")
            key, separator, item_value = item.partition("=")
            if not key:
                raise GuardError("Compose environment contains an empty key")
            if key in result:
                raise GuardError(f"Compose environment contains a duplicate key: {key}")
            result[key] = item_value if separator else None
        return result
    if isinstance(value, dict):
        result: dict[str, Any] = {}
        for key, item in value.items():
            if not isinstance(key, str) or not key:
                raise GuardError("Compose environment contains a non-string or empty key")
            if key in result:
                raise GuardError(f"Compose environment contains a duplicate key: {key}")
            result[key] = item
        return result
    raise GuardError("Compose environment is not a mapping")


def candidate_environment_values(
    config: dict[str, Any], service: str, image_values: dict[str, str]
) -> dict[str, str]:
    """Merge candidate image defaults with the effective Compose service env."""
    service_config = (config.get("services") or {}).get(service) or {}
    values = dict(image_values)
    compose_values = compose_environment(service_config.get("environment"))
    for key, value in compose_values.items():
        if value is None:
            raise GuardError(f"candidate environment inherits host value for {service}: {key}")
        values[str(key)] = str(value)
    # Docker/Podman exposes an explicit Compose hostname through HOSTNAME.
    # When neither `hostname:` nor an image/Compose HOSTNAME is authored, the
    # runtime-generated container ID is normalized out of the baseline and is
    # deliberately omitted here.
    hostname = service_config.get("hostname")
    if hostname is not None:
        values["HOSTNAME"] = str(hostname)
    elif "HOSTNAME" not in image_values and "HOSTNAME" not in compose_values:
        values.pop("HOSTNAME", None)
    return values


def validate_probe_source(path: Path, *, expected_sha256: str | None = None) -> str:
    """Require the staged probe to remain the reviewed source artifact."""
    try:
        metadata = path.lstat()
    except OSError as exc:
        raise GuardError(f"cannot read health probe source: {path}") from exc
    if not stat.S_ISREG(metadata.st_mode):
        raise GuardError(f"health probe source is not a regular file: {path}")
    actual_sha256 = sha256_file(path)
    if expected_sha256 is not None and actual_sha256 != expected_sha256:
        raise GuardError(f"health probe source changed: {path}")
    return actual_sha256


def compose_settings_values(
    candidate_version: str,
    probe_source: Path,
    *,
    probe_source_sha256: str | None = None,
) -> dict[str, str]:
    """Return the non-secret Compose interpolation values for a deployment."""
    if not isinstance(candidate_version, str) or not candidate_version:
        raise GuardError("candidate version must be a non-empty string")
    if any(character in candidate_version for character in "\r\n=\x00"):
        raise GuardError("candidate version contains an invalid character")
    source = str(probe_source)
    if any(character in source for character in "\r\n\x00"):
        raise GuardError("health probe source contains an invalid character")
    if probe_source_sha256 is None:
        probe_source_sha256 = validate_probe_source(probe_source)
    elif not re.fullmatch(r"[0-9a-f]{64}", probe_source_sha256):
        raise GuardError("health probe source digest is invalid")
    return {
        "VERSION": candidate_version,
        "UNSTRACT_HEALTHCHECK_SOURCE": source,
        "UNSTRACT_HEALTHCHECK_SOURCE_SHA256": probe_source_sha256,
    }


def parse_compose_settings(path: Path) -> dict[str, str]:
    """Read the small private interpolation file without exposing its values."""
    values: dict[str, str] = {}
    source_digest: str | None = None
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError as exc:
        raise GuardError(f"cannot read private Compose settings: {path}") from exc
    for line in lines:
        if not line or line.startswith("#"):
            if line.startswith("# probe-source-sha256="):
                if source_digest is not None:
                    raise GuardError(
                        f"private Compose settings contain duplicate probe digests: {path}"
                    )
                source_digest = line.split("=", 1)[1]
            continue
        key, separator, value = line.partition("=")
        if not separator or key not in {
            "VERSION",
            "UNSTRACT_HEALTHCHECK_SOURCE",
        }:
            raise GuardError(f"private Compose settings contain an unsupported entry: {path}")
        if key in values:
            raise GuardError(f"private Compose settings contain a duplicate entry: {path}")
        values[key] = value
    if set(values) != {"VERSION", "UNSTRACT_HEALTHCHECK_SOURCE"}:
        raise GuardError(f"private Compose settings are incomplete: {path}")
    if source_digest is None:
        raise GuardError(f"private Compose settings lack the probe source digest: {path}")
    if not re.fullmatch(r"[0-9a-f]{64}", source_digest):
        raise GuardError(f"private Compose settings have an invalid probe source digest: {path}")
    values["UNSTRACT_HEALTHCHECK_SOURCE_SHA256"] = source_digest
    return values


def validate_compose_settings(
    path: Path,
    *,
    candidate_version: str,
    probe_source: Path,
    probe_source_sha256: str | None = None,
    expected_sha256: str | None = None,
) -> dict[str, str]:
    validate_private_file(path, expected_sha256=expected_sha256, description="Compose settings")
    values = parse_compose_settings(path)
    expected = compose_settings_values(
        candidate_version,
        probe_source,
        probe_source_sha256=probe_source_sha256
        or values["UNSTRACT_HEALTHCHECK_SOURCE_SHA256"],
    )
    if values != expected:
        raise GuardError(f"durable Compose settings drifted: {path}")
    validate_probe_source(
        Path(values["UNSTRACT_HEALTHCHECK_SOURCE"]),
        expected_sha256=values["UNSTRACT_HEALTHCHECK_SOURCE_SHA256"],
    )
    return values


def write_replay_manifest(
    path: Path,
    *,
    lock_path: Path,
    lock: dict[str, Any],
    image_override: Path,
    settings_file: Path,
    runtime_environment_override: Path,
    probe_source: Path,
    probe_source_sha256: str,
    snapshot: ComposeSnapshot | None = None,
) -> None:
    """Bind every private replay input to a durable, sanitized hash record."""
    validate_candidate_image_override(image_override)
    settings = validate_compose_settings(
        settings_file,
        candidate_version=lock["candidate_version"],
        probe_source=probe_source,
        probe_source_sha256=probe_source_sha256,
    )
    validate_private_override(runtime_environment_override)
    if snapshot is None:
        raise GuardError("durable replay requires a retained Compose snapshot")
    load_compose_snapshot(snapshot.manifest_path)
    write_private_json(
        path,
        {
            "schema": REPLAY_MANIFEST_SCHEMA,
            "candidate_version": lock["candidate_version"],
            "source_commit": lock["source_commit"],
            "source_tree": lock["source_tree"],
            "candidate_lock_sha256": sha256_file(lock_path),
            "probe_source_sha256": probe_source_sha256,
            "files": {
                CANDIDATE_IMAGE_FILENAME: sha256_file(image_override),
                COMPOSE_SETTINGS_FILENAME: sha256_file(settings_file),
                RUNTIME_ENVIRONMENT_FILENAME: sha256_file(runtime_environment_override),
            },
            "runtime_environment_keys": {
                service: sorted(keys)
                for service, keys in reviewed_environment_keys_from_override(
                    runtime_environment_override
                ).items()
                if keys
            },
            "settings": {
                "VERSION": settings["VERSION"],
                "UNSTRACT_HEALTHCHECK_SOURCE_SHA256": settings[
                    "UNSTRACT_HEALTHCHECK_SOURCE_SHA256"
                ],
            },
            "compose_snapshot": {
                "schema": COMPOSE_SNAPSHOT_SCHEMA,
                "manifest": str(snapshot.manifest_path),
                "manifest_sha256": sha256_file(snapshot.manifest_path),
            },
        },
        description="durable replay manifest",
    )


def load_replay_manifest(
    path: Path,
    *,
    lock_path: Path,
    lock: dict[str, Any],
    image_override: Path,
    settings_file: Path,
    runtime_environment_override: Path,
    probe_source: Path,
    probe_source_sha256: str,
    state_dir: Path | None = None,
) -> dict[str, Any]:
    """Verify the persisted private replay files before normal startup."""
    validate_private_file(path, description="durable replay manifest")
    try:
        manifest = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise GuardError(f"cannot read durable replay manifest {path}: {exc}") from exc
    if manifest.get("schema") != REPLAY_MANIFEST_SCHEMA:
        raise GuardError("durable replay manifest has an unsupported schema")
    if manifest.get("candidate_version") != lock.get("candidate_version"):
        raise GuardError("durable replay candidate version changed")
    if manifest.get("source_commit") != lock.get("source_commit"):
        raise GuardError("durable replay source commit changed")
    if manifest.get("source_tree") != lock.get("source_tree"):
        raise GuardError("durable replay source tree changed")
    expected_lock_sha256 = manifest.get("candidate_lock_sha256")
    if not isinstance(expected_lock_sha256, str):
        raise GuardError("durable replay manifest lacks candidate lock digest")
    if sha256_file(lock_path) != expected_lock_sha256:
        raise GuardError("candidate lock changed since durable replay was prepared")
    if manifest.get("probe_source_sha256") != probe_source_sha256:
        raise GuardError("durable replay probe digest changed")
    files = manifest.get("files")
    if not isinstance(files, dict):
        raise GuardError("durable replay manifest lacks private file digests")
    image_sha256 = files.get(CANDIDATE_IMAGE_FILENAME)
    settings_sha256 = files.get(COMPOSE_SETTINGS_FILENAME)
    runtime_sha256 = files.get(RUNTIME_ENVIRONMENT_FILENAME)
    if not all(isinstance(value, str) for value in (image_sha256, settings_sha256, runtime_sha256)):
        raise GuardError("durable replay manifest has incomplete private file digests")
    validate_candidate_image_override(image_override, expected_sha256=image_sha256)
    settings = validate_compose_settings(
        settings_file,
        candidate_version=lock["candidate_version"],
        probe_source=probe_source,
        probe_source_sha256=probe_source_sha256,
        expected_sha256=settings_sha256,
    )
    if manifest.get("settings") != {
        "VERSION": settings["VERSION"],
        "UNSTRACT_HEALTHCHECK_SOURCE_SHA256": settings[
            "UNSTRACT_HEALTHCHECK_SOURCE_SHA256"
        ],
    }:
        raise GuardError("durable Compose settings metadata changed")
    validate_private_override(
        runtime_environment_override, expected_sha256=runtime_sha256
    )
    expected_keys = manifest.get("runtime_environment_keys") or {}
    if expected_keys != {
        service: sorted(keys)
        for service, keys in reviewed_environment_keys_from_override(
            runtime_environment_override
        ).items()
        if keys
    }:
        raise GuardError("runtime environment contract changed since durable replay was prepared")
    snapshot_metadata = manifest.get("compose_snapshot")
    if not isinstance(snapshot_metadata, dict):
        raise GuardError("durable replay manifest lacks the Compose snapshot")
    if snapshot_metadata.get("schema") != COMPOSE_SNAPSHOT_SCHEMA:
        raise GuardError("durable replay Compose snapshot has an unsupported schema")
    snapshot_manifest_text = snapshot_metadata.get("manifest")
    snapshot_manifest_sha256 = snapshot_metadata.get("manifest_sha256")
    if not isinstance(snapshot_manifest_text, str) or not isinstance(snapshot_manifest_sha256, str):
        raise GuardError("durable replay Compose snapshot metadata is incomplete")
    snapshot_manifest = Path(snapshot_manifest_text).resolve()
    if state_dir is not None:
        try:
            snapshot_manifest.relative_to(state_dir.resolve())
        except ValueError as exc:
            raise GuardError("durable replay Compose snapshot is outside state") from exc
    if sha256_file(snapshot_manifest) != snapshot_manifest_sha256:
        raise GuardError("durable replay Compose snapshot manifest changed")
    snapshot = load_compose_snapshot(snapshot_manifest)
    return {
        "image_override_sha256": image_sha256,
        "settings_sha256": settings_sha256,
        "runtime_environment_sha256": runtime_sha256,
        "settings": settings,
        "compose_snapshot": snapshot,
    }


def plan_runtime_environment_override(
    baseline: dict[str, Any],
    runtime_environment: dict[str, dict[str, Any]],
    candidate_images: dict[str, dict[str, Any]],
    config: dict[str, Any],
    *,
    services: tuple[str, ...] = TARGET_SERVICES,
) -> tuple[dict[str, dict[str, str]], dict[str, set[str]]]:
    """Plan private Compose values that restore the baseline env contract."""
    baseline_services = service_map(baseline)
    overrides: dict[str, dict[str, str]] = {}
    reviewed_keys: dict[str, set[str]] = {}
    for service in services:
        previous = baseline_services.get(service)
        observed = runtime_environment.get(service)
        candidate = candidate_images.get(service)
        if previous is None or observed is None or candidate is None:
            raise GuardError(f"runtime environment plan is incomplete for {service}")
        observed_id = observed.get("id")
        observed_values = observed.get("values") or {}
        observed_hashes = environment_hashes(
            observed_values,
            container_id=observed_id,
            config_hostname=observed.get("hostname"),
        )
        if observed_hashes != (previous.get("env_hashes") or {}):
            raise GuardError(f"fresh runtime environment changed for {service}")
        candidate_values = candidate_environment_values(
            config, service, candidate.get("environment") or {}
        )
        baseline_hashes = previous.get("env_hashes") or {}
        candidate_hashes = environment_hashes(candidate_values)
        service_overrides: dict[str, str] = {}
        for key, expected_hash in baseline_hashes.items():
            if candidate_hashes.get(key) == expected_hash:
                continue
            if key == "HOSTNAME":
                raise GuardError(f"candidate hostname changed for {service}")
            if key not in observed_values:
                raise GuardError(f"baseline environment value is unavailable for {service}: {key}")
            service_overrides[key] = observed_values[key]
        for key in candidate_hashes:
            if key in baseline_hashes or key in ALLOWED_ENV_ADDITIONS.get(service, set()):
                continue
            raise GuardError(f"candidate added environment for {service}: {key}")
        effective_values = dict(candidate_values)
        effective_values.update(service_overrides)
        effective_hashes = environment_hashes(effective_values)
        if any(effective_hashes.get(key) != value for key, value in baseline_hashes.items()):
            raise GuardError(f"candidate environment cannot preserve {service}")
        overrides[service] = service_overrides
        reviewed_keys[service] = set(service_overrides)
    return overrides, reviewed_keys


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
    reviewed_environment_keys: dict[str, set[str]] | None = None,
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
            if old_mount.get("type") != new_mount.get("type"):
                raise GuardError(
                    f"candidate changed {service} mount type for {destination}"
                )
            old_source = (
                old_mount.get("name") or old_mount.get("source")
                if old_mount.get("type") == "volume"
                else old_mount.get("source")
            )
            new_source = compose_mount_source(config, new_mount)
            if old_mount.get("type") == "volume" or new_mount.get("type") == "volume":
                sources_match = old_source == new_source
            else:
                sources_match = bind_mount_sources_match(old_source, new_source)
            if old_source and new_source and not sources_match:
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
        reviewed_keys = (reviewed_environment_keys or {}).get(service, set())
        allowed_additions = ALLOWED_ENV_ADDITIONS.get(service, set()) | reviewed_keys
        for key, value in old_authored_environment.items():
            if key not in candidate_environment:
                if key not in reviewed_keys:
                    raise GuardError(
                        f"candidate removed authored environment for {service}: {key}"
                    )
                continue
            if compose_value_hash(candidate_environment[key]) != compose_value_hash(value):
                if key not in reviewed_keys:
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


def verify_candidate_source_state(
    candidate_source: Path | None, lock: dict[str, Any] | None
) -> None:
    if candidate_source is None or lock is None:
        return
    require_clean_candidate_source(
        candidate_source, lock["source_commit"], lock.get("source_tree")
    )
    verify_artifacts(candidate_source, lock)


def verify_live_compose_inputs(
    project_dir: Path,
    *,
    live_env_file: Path | None,
    expected_source: dict[str, Any] | None,
) -> None:
    """Recheck ignored live inputs before each Compose invocation."""
    actual = live_compose_inputs(project_dir, live_env_file=live_env_file)
    if expected_source is None:
        return
    expected = expected_source.get("live_inputs")
    if not isinstance(expected, dict):
        raise GuardError(
            "baseline source state lacks live Compose input hashes; capture a fresh baseline"
        )
    if expected != actual:
        raise GuardError("ignored live Compose input drifted since baseline capture")


def verify_compose_inputs_before_run(
    *,
    project_dir: Path,
    live_env_file: Path | None,
    expected_source: dict[str, Any] | None,
    candidate_source: Path | None,
    candidate_lock: dict[str, Any] | None,
    probe_source: Path,
    probe_source_sha256: str | None,
    image_override: Path | None,
    image_override_sha256: str | None,
    environment_override: Path | None,
    environment_override_sha256: str | None,
    settings_file: Path | None,
    settings_file_sha256: str | None,
    candidate_version: str,
) -> dict[str, str] | None:
    """Recheck every mutable Compose input immediately before subprocess start."""
    verify_candidate_source_state(candidate_source, candidate_lock)
    verify_live_compose_inputs(
        project_dir,
        live_env_file=live_env_file,
        expected_source=expected_source,
    )
    if settings_file:
        settings = validate_compose_settings(
            settings_file,
            candidate_version=candidate_version,
            probe_source=probe_source,
            probe_source_sha256=probe_source_sha256,
            expected_sha256=settings_file_sha256,
        )
    else:
        validate_probe_source(probe_source, expected_sha256=probe_source_sha256)
        settings = None
    if image_override:
        validate_candidate_image_override(
            image_override, expected_sha256=image_override_sha256
        )
    if environment_override:
        validate_private_override(
            environment_override, expected_sha256=environment_override_sha256
        )
    return settings


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
    live_env_file: Path | None = None,
    operation_deadline: OperationDeadline | None = None,
) -> dict[str, Any]:
    snapshot = capture(
        project_dir,
        live_env_file=live_env_file,
        deadline=operation_deadline,
    )
    write_json(output, snapshot)
    return snapshot


def compose_args(
    project_dir: Path,
    compose_files: tuple[str, ...],
    *,
    live_env_file: Path | None = None,
    snapshot: ComposeSnapshot | None = None,
) -> list[str]:
    # Compose resolves the base file's includes and merged-file relative
    # references from --project-directory. The snapshot retains the original
    # layout, so selecting its frozen base file parent keeps those resolutions
    # inside the snapshot without changing their meaning.
    effective_project_dir = _compose_project_directory(
        project_dir, compose_files, snapshot=snapshot
    )
    args = ["docker", "compose", "--project-directory", str(effective_project_dir)]
    if live_env_file:
        args.extend(["--env-file", str(live_env_file)])
    for compose_file in compose_files:
        args.extend(["-f", compose_file])
    return args


def write_private_text(
    path: Path,
    text: str,
    *,
    replace: bool,
    description: str,
    reuse_if_identical: bool = False,
) -> None:
    """Write one private state file without creating a transient reference."""
    if "\x00" in text:
        raise GuardError(f"{description} contains a NUL byte")
    path.parent.mkdir(parents=True, exist_ok=True)
    mode = stat.S_IRUSR | stat.S_IWUSR
    if path.exists() and not replace and reuse_if_identical:
        validate_private_file(path, description=description)
        if sha256_file(path) != sha256_bytes(text.encode("utf-8")):
            raise GuardError(f"{description} already exists with different contents: {path}")
        return
    if replace:
        staging = path.with_name(f".{path.name}.{os.getpid()}.new")
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        try:
            descriptor = os.open(staging, flags, mode)
            with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
                handle.write(text)
            os.chmod(staging, mode)
            os.replace(staging, path)
        except FileExistsError as exc:
            raise GuardError(f"private state staging file already exists: {staging}") from exc
        finally:
            with contextlib.suppress(FileNotFoundError):
                staging.unlink()
    else:
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        try:
            descriptor = os.open(path, flags, mode)
            with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
                handle.write(text)
        except FileExistsError as exc:
            raise GuardError(f"{description} already exists: {path}") from exc
    os.chmod(path, mode)


def write_image_override(
    lock: dict[str, Any], path: Path, *, replace: bool = False
) -> None:
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
    write_private_text(
        path,
        "\n".join(lines) + "\n",
        replace=replace,
        description="candidate image override",
        reuse_if_identical=True,
    )


def write_compose_settings(
    candidate_version: str,
    probe_source: Path,
    path: Path,
    *,
    probe_source_sha256: str | None = None,
    replace: bool = False,
) -> None:
    values = compose_settings_values(
        candidate_version,
        probe_source,
        probe_source_sha256=probe_source_sha256,
    )
    text = "\n".join(
        [
            "# Generated by train_health_deployment_guard.py; do not edit.",
            f"VERSION={values['VERSION']}",
            f"UNSTRACT_HEALTHCHECK_SOURCE={values['UNSTRACT_HEALTHCHECK_SOURCE']}",
            f"# probe-source-sha256={values['UNSTRACT_HEALTHCHECK_SOURCE_SHA256']}",
            "",
        ]
    )
    write_private_text(
        path,
        text,
        replace=replace,
        description="Compose settings",
        reuse_if_identical=True,
    )


def write_runtime_environment_override(
    overrides: dict[str, dict[str, str]], path: Path, *, replace: bool = False
) -> None:
    """Write reviewed baseline values to a private mode-600 Compose override."""
    lines = [
        "# Generated by train_health_deployment_guard.py; do not edit.",
        "services:",
    ]
    written_services = 0
    for service in TARGET_SERVICES:
        values = overrides.get(service) or {}
        if not values:
            continue
        written_services += 1
        lines.extend([f"  {service}:", "    environment:"])
        for key in sorted(values, key=lambda item: str(item)):
            value = values[key]
            if not isinstance(key, str) or not isinstance(value, str):
                raise GuardError(f"runtime environment entry is not a string for {service}")
            if not key or any(character in key for character in "\r\n=\x00"):
                raise GuardError(f"runtime environment key is invalid for {service}")
            if "\x00" in value:
                raise GuardError(f"runtime environment value is invalid for {service}: {key}")
            # Compose interpolates ``$VAR`` in YAML values even when they are
            # quoted.  ``$$`` is Compose's escaped literal dollar sign.
            lines.append(f"      {json.dumps(key)}: {json.dumps(value.replace('$', '$$'))}")
    if not written_services:
        lines = [
            "# Generated by train_health_deployment_guard.py; do not edit.",
            "services: {}",
        ]
    write_private_text(
        path,
        "\n".join(lines) + "\n",
        replace=replace,
        description="runtime environment override",
        reuse_if_identical=True,
    )


def reviewed_environment_keys_from_override(path: Path) -> dict[str, set[str]]:
    """Recover reviewed key names from durable YAML without reading values aloud."""
    validate_private_override(path)
    result: dict[str, set[str]] = {}
    current_service: str | None = None
    in_environment = False
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError as exc:
        raise GuardError(f"cannot read runtime environment override: {path}") from exc
    for line in lines:
        if line.startswith("  ") and not line.startswith("    ") and line.endswith(":"):
            current_service = line[2:-1]
            in_environment = False
            continue
        if current_service and line == "    environment:":
            in_environment = True
            result.setdefault(current_service, set())
            continue
        if current_service and in_environment and line.startswith("      "):
            key_text, separator, _ = line[6:].partition(":")
            if not separator:
                raise GuardError(f"runtime environment override has invalid YAML: {path}")
            try:
                key = json.loads(key_text.strip())
            except json.JSONDecodeError as exc:
                raise GuardError(
                    f"runtime environment override has an invalid key: {path}"
                ) from exc
            if not isinstance(key, str):
                raise GuardError(f"runtime environment override key is not a string: {path}")
            result[current_service].add(key)
    return result


def validate_private_file(
    path: Path,
    *,
    expected_sha256: str | None = None,
    description: str,
) -> None:
    try:
        metadata = path.lstat()
    except OSError as exc:
        raise GuardError(f"cannot read private {description}: {path}") from exc
    if not stat.S_ISREG(metadata.st_mode) or stat.S_IMODE(metadata.st_mode) != 0o600:
        raise GuardError(f"{description} is not a private regular file: {path}")
    if hasattr(os, "getuid") and metadata.st_uid != os.getuid():
        raise GuardError(f"{description} has the wrong owner: {path}")
    if expected_sha256 is not None:
        actual_sha256 = sha256_file(path)
        if actual_sha256 != expected_sha256:
            raise GuardError(f"{description} changed: {path}")


def validate_private_override(path: Path, *, expected_sha256: str | None = None) -> None:
    validate_private_file(
        path,
        expected_sha256=expected_sha256,
        description="runtime environment override",
    )


def validate_candidate_image_override(
    path: Path, *, expected_sha256: str | None = None
) -> None:
    validate_private_file(
        path,
        expected_sha256=expected_sha256,
        description="candidate image override",
    )


def candidate_image_override(
    lock: dict[str, Any], path: Path, *, replace: bool = False
) -> Path:
    """Materialize a durable private image override for every Compose replay."""
    write_image_override(lock, path, replace=replace)
    validate_candidate_image_override(path)
    return path


def targeted_up(
    project_dir: Path,
    compose_files: tuple[str, ...],
    services: tuple[str, ...],
    *,
    candidate_version: str,
    probe_source: Path,
    live_env_file: Path | None = None,
    expected_source: dict[str, Any] | None = None,
    candidate_source: Path | None = None,
    candidate_lock: dict[str, Any] | None = None,
    image_override: Path | None = None,
    image_override_sha256: str | None = None,
    environment_override: Path | None = None,
    environment_override_sha256: str | None = None,
    settings_file: Path | None = None,
    settings_file_sha256: str | None = None,
    probe_source_sha256: str | None = None,
    snapshot: ComposeSnapshot | None = None,
    deadline: OperationDeadline | None = None,
) -> None:
    verify_candidate_source_state(candidate_source, candidate_lock)
    verify_live_compose_inputs(
        project_dir,
        live_env_file=live_env_file,
        expected_source=expected_source,
    )
    env = os.environ.copy()
    if settings_file:
        settings = validate_compose_settings(
            settings_file,
            candidate_version=candidate_version,
            probe_source=probe_source,
            probe_source_sha256=probe_source_sha256,
            expected_sha256=settings_file_sha256,
        )
        env["VERSION"] = settings["VERSION"]
        env["UNSTRACT_HEALTHCHECK_SOURCE"] = settings["UNSTRACT_HEALTHCHECK_SOURCE"]
    else:
        env["VERSION"] = candidate_version
        env["UNSTRACT_HEALTHCHECK_SOURCE"] = str(probe_source)
        validate_probe_source(probe_source, expected_sha256=probe_source_sha256)
    files = compose_files
    if image_override:
        validate_candidate_image_override(
            image_override, expected_sha256=image_override_sha256
        )
        files += (str(image_override),)
    if environment_override:
        validate_private_override(
            environment_override, expected_sha256=environment_override_sha256
        )
        files += (str(environment_override),)
    with bound_compose_inputs(
        project_dir=project_dir,
        compose_files=files,
        live_env_file=live_env_file,
        probe_source=probe_source,
        probe_source_sha256=probe_source_sha256,
        image_override=image_override,
        image_override_sha256=image_override_sha256,
        environment_override=environment_override,
        environment_override_sha256=environment_override_sha256,
        snapshot=snapshot,
    ) as (bound_paths, pass_fds, active_snapshot):
        args = compose_args(
            project_dir,
            files,
            live_env_file=live_env_file,
            snapshot=active_snapshot,
        )
        bound_args = [bound_paths.get(argument, argument) for argument in args]
        verification = dict(
            project_dir=project_dir,
            live_env_file=live_env_file,
            expected_source=expected_source,
            candidate_source=candidate_source,
            candidate_lock=candidate_lock,
            probe_source=probe_source,
            probe_source_sha256=probe_source_sha256,
            image_override=image_override,
            image_override_sha256=image_override_sha256,
            environment_override=environment_override,
            environment_override_sha256=environment_override_sha256,
            settings_file=settings_file,
            settings_file_sha256=settings_file_sha256,
            candidate_version=candidate_version,
        )
        final_settings = verify_compose_inputs_before_run(**verification)
        if final_settings:
            env["VERSION"] = final_settings["VERSION"]
        env["UNSTRACT_HEALTHCHECK_SOURCE"] = bound_paths.get(
            str(probe_source), final_settings["UNSTRACT_HEALTHCHECK_SOURCE"]
            if final_settings
            else env["UNSTRACT_HEALTHCHECK_SOURCE"]
        )
        with stable_bind_compose_config(
            bound_args,
            active_snapshot,
            env=env,
            deadline=deadline,
            pass_fds=pass_fds,
        ) as (launch_args, _):
            verify_compose_inputs_before_run(**verification)
            run(
                [
                    *launch_args,
                    "up",
                    "-d",
                    "--no-deps",
                    "--force-recreate",
                    "--no-build",
                    "--pull",
                    "never",
                    *services,
                ],
                cwd=active_snapshot.root,
                env=env,
                deadline=deadline,
                pass_fds=pass_fds,
            )


def compose_start(
    project_dir: Path,
    compose_files: tuple[str, ...],
    *,
    candidate_version: str,
    probe_source: Path,
    live_env_file: Path,
    expected_source: dict[str, Any] | None,
    candidate_source: Path,
    candidate_lock: dict[str, Any],
    image_override: Path,
    image_override_sha256: str,
    environment_override: Path,
    environment_override_sha256: str,
    settings_file: Path,
    settings_file_sha256: str,
    probe_source_sha256: str,
    snapshot: ComposeSnapshot | None = None,
    deadline: OperationDeadline | None = None,
) -> None:
    """Start the whole stack through the durable guarded Compose inputs."""
    verify_candidate_source_state(candidate_source, candidate_lock)
    verify_live_compose_inputs(
        project_dir,
        live_env_file=live_env_file,
        expected_source=expected_source,
    )
    env = os.environ.copy()
    settings = validate_compose_settings(
        settings_file,
        candidate_version=candidate_version,
        probe_source=probe_source,
        probe_source_sha256=probe_source_sha256,
        expected_sha256=settings_file_sha256,
    )
    env["VERSION"] = settings["VERSION"]
    env["UNSTRACT_HEALTHCHECK_SOURCE"] = settings["UNSTRACT_HEALTHCHECK_SOURCE"]
    validate_candidate_image_override(
        image_override, expected_sha256=image_override_sha256
    )
    validate_private_override(
        environment_override, expected_sha256=environment_override_sha256
    )
    files = compose_files + (str(image_override), str(environment_override))
    with bound_compose_inputs(
        project_dir=project_dir,
        compose_files=files,
        live_env_file=live_env_file,
        probe_source=probe_source,
        probe_source_sha256=probe_source_sha256,
        image_override=image_override,
        image_override_sha256=image_override_sha256,
        environment_override=environment_override,
        environment_override_sha256=environment_override_sha256,
        snapshot=snapshot,
    ) as (bound_paths, pass_fds, active_snapshot):
        args = compose_args(
            project_dir,
            files,
            live_env_file=live_env_file,
            snapshot=active_snapshot,
        )
        bound_args = [bound_paths.get(argument, argument) for argument in args]
        verification = dict(
            project_dir=project_dir,
            live_env_file=live_env_file,
            expected_source=expected_source,
            candidate_source=candidate_source,
            candidate_lock=candidate_lock,
            probe_source=probe_source,
            probe_source_sha256=probe_source_sha256,
            image_override=image_override,
            image_override_sha256=image_override_sha256,
            environment_override=environment_override,
            environment_override_sha256=environment_override_sha256,
            settings_file=settings_file,
            settings_file_sha256=settings_file_sha256,
            candidate_version=candidate_version,
        )
        final_settings = verify_compose_inputs_before_run(**verification)
        if final_settings:
            env["VERSION"] = final_settings["VERSION"]
        env["UNSTRACT_HEALTHCHECK_SOURCE"] = bound_paths.get(
            str(probe_source), final_settings["UNSTRACT_HEALTHCHECK_SOURCE"]
            if final_settings
            else env["UNSTRACT_HEALTHCHECK_SOURCE"]
        )
        with stable_bind_compose_config(
            bound_args,
            active_snapshot,
            env=env,
            deadline=deadline,
            pass_fds=pass_fds,
        ) as (launch_args, _):
            verify_compose_inputs_before_run(**verification)
            run(
                [*launch_args, "up", "-d", "--no-build", "--pull", "never"],
                cwd=active_snapshot.root,
                env=env,
                deadline=deadline,
                pass_fds=pass_fds,
            )


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
    runtime_environment_override: Path,
    runtime_environment_sha256: str,
    reviewed_environment_keys: dict[str, set[str]],
    image_override: Path,
    image_override_sha256: str,
    settings_file: Path,
    settings_file_sha256: str,
    probe_source: Path,
    probe_source_sha256: str,
    deadline: OperationDeadline | None = None,
) -> dict[str, Any]:
    backup_dir.mkdir(parents=True, exist_ok=True)
    validate_private_override(
        runtime_environment_override, expected_sha256=runtime_environment_sha256
    )
    validate_candidate_image_override(image_override, expected_sha256=image_override_sha256)
    validate_compose_settings(
        settings_file,
        candidate_version=parse_compose_settings(settings_file)["VERSION"],
        probe_source=probe_source,
        probe_source_sha256=probe_source_sha256,
        expected_sha256=settings_file_sha256,
    )
    write_json(backup_dir / "baseline.json", snapshot)
    tag_prefix = "localhost/unstract-health-backup-"
    backup_images: dict[str, Any] = {
        "schema": BACKUP_SCHEMA,
        "created_at": utc_now(),
        "runtime_environment": {
            "schema": RUNTIME_ENVIRONMENT_SCHEMA,
            "file": runtime_environment_override.name,
            "sha256": runtime_environment_sha256,
            "reviewed_environment_keys": {
                service: sorted(keys)
                for service, keys in reviewed_environment_keys.items()
                if keys
            },
        },
        "candidate_image_override": {
            "schema": CANDIDATE_IMAGE_SCHEMA,
            "file": image_override.name,
            "sha256": image_override_sha256,
        },
        "compose_settings": {
            "schema": COMPOSE_SETTINGS_SCHEMA,
            "file": settings_file.name,
            "sha256": settings_file_sha256,
            "probe_source": str(probe_source),
            "probe_source_sha256": probe_source_sha256,
            "candidate_version": parse_compose_settings(settings_file)["VERSION"],
        },
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


def runtime_environment_backup(
    backup_images: dict[str, Any], backup_dir: Path
) -> tuple[Path, str]:
    metadata = backup_images.get("runtime_environment")
    if not isinstance(metadata, dict):
        raise GuardError("backup manifest has no runtime environment metadata")
    if metadata.get("schema") != RUNTIME_ENVIRONMENT_SCHEMA:
        raise GuardError("backup runtime environment has an unsupported schema")
    if metadata.get("file") != RUNTIME_ENVIRONMENT_FILENAME:
        raise GuardError("backup runtime environment file is not the guarded override")
    digest = metadata.get("sha256")
    if not isinstance(digest, str) or not re.fullmatch(r"[0-9a-f]{64}", digest):
        raise GuardError("backup runtime environment digest is invalid")
    path = backup_dir / RUNTIME_ENVIRONMENT_FILENAME
    validate_private_override(path, expected_sha256=digest)
    return path, digest


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
    write_private_text(
        path,
        "\n".join(lines) + "\n",
        replace=True,
        description="rollback image override",
    )


def command_capture(args: argparse.Namespace) -> int:
    deadline = OperationDeadline(args.operation_timeout)
    project_dir = Path(args.project_dir)
    live_env_file = resolve_live_env_file(args, project_dir)
    capture_and_write(
        project_dir,
        Path(args.output),
        live_env_file=live_env_file,
        operation_deadline=deadline,
    )
    return 0


def resolve_live_env_file(args: argparse.Namespace, project_dir: Path) -> Path:
    configured = getattr(args, "live_env_file", None)
    return Path(configured).resolve() if configured else (project_dir / LIVE_ENV_RELATIVE).resolve()


def compose_snapshot_destination(state_dir: Path) -> Path:
    """Return a fresh retained snapshot directory below guarded state."""
    stamp = dt.datetime.now(dt.UTC).strftime("%Y%m%dT%H%M%S%fZ")
    return state_dir / f"{COMPOSE_SNAPSHOT_DIR_PREFIX}{stamp}-{os.getpid()}"


def create_compose_snapshot(
    *,
    state_dir: Path,
    project_dir: Path,
    compose_files: tuple[str, ...],
    live_env_file: Path | None,
    probe_source: Path,
    probe_source_sha256: str | None,
    image_override: Path | None,
    image_override_sha256: str | None,
    environment_override: Path | None,
    environment_override_sha256: str | None,
) -> ComposeSnapshot:
    return materialize_compose_snapshot(
        project_dir=project_dir,
        compose_files=compose_files,
        live_env_file=live_env_file,
        probe_source=probe_source,
        probe_source_sha256=probe_source_sha256,
        image_override=image_override,
        image_override_sha256=image_override_sha256,
        environment_override=environment_override,
        environment_override_sha256=environment_override_sha256,
        destination=compose_snapshot_destination(state_dir),
    )


def prepare(
    args: argparse.Namespace,
    image_override: Path,
    *,
    runtime_environment_override: Path,
    settings_file: Path,
    replace_runtime_environment_override: bool = False,
    replace_settings_file: bool = False,
    operation_deadline: OperationDeadline,
) -> tuple[
    dict[str, Any], dict[str, Any], dict[str, Any], dict[str, set[str]], str
]:
    baseline = load_baseline(Path(args.baseline))
    lock = load_lock(Path(args.candidate_lock))
    require_clean_candidate_source(
        Path(args.candidate_source), lock["source_commit"], lock.get("source_tree")
    )
    verify_artifacts(Path(args.candidate_source), lock)
    project_dir = Path(args.project_dir)
    candidate_source = Path(args.candidate_source)
    live_env_file = resolve_live_env_file(args, project_dir)
    probe_source = Path(args.probe_source)
    probe_source_sha256 = (lock.get("artifacts") or {}).get(
        "docker/healthchecks/unstract-services.sh"
    )
    if not isinstance(probe_source_sha256, str):
        raise GuardError("candidate lock lacks the guarded probe source digest")
    validate_probe_source(probe_source, expected_sha256=probe_source_sha256)
    write_compose_settings(
        lock["candidate_version"],
        probe_source,
        settings_file,
        probe_source_sha256=probe_source_sha256,
        replace=replace_settings_file,
    )
    validate_compose_settings(
        settings_file,
        candidate_version=lock["candidate_version"],
        probe_source=probe_source,
        probe_source_sha256=probe_source_sha256,
    )
    validate_candidate_image_override(image_override)
    image_override_sha256 = sha256_file(image_override)
    settings_file_sha256 = sha256_file(settings_file)
    candidate_images = candidate_image_snapshot(lock, deadline=operation_deadline)
    if not candidate_images:
        raise GuardError("no candidate images were verified")
    current = capture(
        project_dir,
        live_env_file=live_env_file,
        deadline=operation_deadline,
    )
    compare_baseline_current(baseline, current, allow_new_probe=False)
    compare_untargeted_runtime(baseline, current)
    compare_source_and_quiescence(baseline, current)
    runtime_environment = inspect_runtime_environment(
        deadline=operation_deadline
    )
    config = compose_config(
        project_dir,
        tuple(args.compose_file or DEFAULT_COMPOSE_FILES),
        candidate_version=lock["candidate_version"],
        probe_source=probe_source,
        live_env_file=live_env_file,
        expected_source=baseline["source"],
        candidate_source=candidate_source,
        candidate_lock=lock,
        image_override=image_override,
        image_override_sha256=image_override_sha256,
        settings_file=settings_file,
        settings_file_sha256=settings_file_sha256,
        probe_source_sha256=probe_source_sha256,
        deadline=operation_deadline,
    )
    overrides, reviewed_keys = plan_runtime_environment_override(
        baseline,
        runtime_environment,
        candidate_images,
        config,
    )
    write_runtime_environment_override(
        overrides,
        runtime_environment_override,
        replace=replace_runtime_environment_override,
    )
    validate_private_override(runtime_environment_override)
    config = compose_config(
        project_dir,
        tuple(args.compose_file or DEFAULT_COMPOSE_FILES),
        candidate_version=lock["candidate_version"],
        probe_source=probe_source,
        live_env_file=live_env_file,
        expected_source=baseline["source"],
        candidate_source=candidate_source,
        candidate_lock=lock,
        settings_file=settings_file,
        settings_file_sha256=settings_file_sha256,
        probe_source_sha256=probe_source_sha256,
        image_override=image_override,
        environment_override=runtime_environment_override,
        environment_override_sha256=sha256_file(runtime_environment_override),
        deadline=operation_deadline,
    )
    authored_baseline = compose_config(
        project_dir,
        tuple(args.compose_file or DEFAULT_COMPOSE_FILES),
        candidate_version=lock["candidate_version"],
        probe_source=probe_source,
        live_env_file=live_env_file,
        expected_source=baseline["source"],
        candidate_source=candidate_source,
        candidate_lock=lock,
        settings_file=settings_file,
        settings_file_sha256=settings_file_sha256,
        probe_source_sha256=probe_source_sha256,
        deadline=operation_deadline,
    )
    check_candidate_config(
        config,
        baseline,
        lock,
        authored_baseline,
        reviewed_environment_keys=reviewed_keys,
    )
    runtime_environment_sha256 = sha256_file(runtime_environment_override)
    validate_private_override(
        runtime_environment_override, expected_sha256=runtime_environment_sha256
    )
    validate_candidate_image_override(
        image_override, expected_sha256=image_override_sha256
    )
    validate_compose_settings(
        settings_file,
        candidate_version=lock["candidate_version"],
        probe_source=probe_source,
        probe_source_sha256=probe_source_sha256,
        expected_sha256=settings_file_sha256,
    )
    return baseline, lock, current, reviewed_keys, runtime_environment_sha256


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
    runtime_environment_override: Path,
    runtime_environment_sha256: str,
    operation_deadline: OperationDeadline,
) -> None:
    services = tuple((replacement_manifest.get("services") or {}).keys())
    if not services:
        raise GuardError("no exact replacement IDs were recorded for compensating rollback")
    project_dir = Path(args.project_dir)
    live_env_file = resolve_live_env_file(args, project_dir)
    current = capture(
        project_dir,
        live_env_file=live_env_file,
        deadline=operation_deadline,
        quiescence_max_wait_seconds=POST_RECREATION_QUIESCENCE_MAX_WAIT_SECONDS,
        quiescence_phase=f"pre-compensating-rollback:{services[0]}",
    )
    compare_source_and_quiescence(baseline, current)
    verify_replacement_ids(current, replacement_manifest)
    override = backup_dir / "compensating-rollback.override.yaml"
    rollback_override(backup_images, override, services)
    validate_private_override(
        runtime_environment_override, expected_sha256=runtime_environment_sha256
    )
    rollback_files = rollback_compose_files(args) + (str(override),)
    rollback_snapshot = create_compose_snapshot(
        state_dir=backup_dir,
        project_dir=project_dir,
        compose_files=rollback_files,
        live_env_file=live_env_file,
        probe_source=Path(args.probe_source),
        probe_source_sha256=None,
        image_override=override,
        image_override_sha256=sha256_file(override),
        environment_override=runtime_environment_override,
        environment_override_sha256=runtime_environment_sha256,
    )
    with advisory_lock(deadline=operation_deadline):
        # Recheck identity and quiescence after acquiring the DB lock.  The
        # process may be disconnected when db itself is recreated; the local
        # operation lock remains held for that bounded transaction.
        locked = capture(
            project_dir,
            live_env_file=live_env_file,
            deadline=operation_deadline,
            quiescence_max_wait_seconds=POST_RECREATION_QUIESCENCE_MAX_WAIT_SECONDS,
            quiescence_phase=f"locked-compensating-rollback:{services[0]}",
        )
        compare_source_and_quiescence(baseline, locked)
        verify_replacement_ids(locked, replacement_manifest)
        targeted_up(
            project_dir,
            rollback_files,
            services,
            candidate_version="rollback-unused",
            probe_source=Path(args.probe_source),
            live_env_file=live_env_file,
            expected_source=baseline["source"],
            image_override=override,
            image_override_sha256=sha256_file(override),
            environment_override=runtime_environment_override,
            environment_override_sha256=runtime_environment_sha256,
            snapshot=rollback_snapshot,
            deadline=operation_deadline,
        )
        wait_running(services, operation_deadline=operation_deadline)
    final = capture(
        project_dir,
        live_env_file=live_env_file,
        deadline=operation_deadline,
        quiescence_max_wait_seconds=POST_RECREATION_QUIESCENCE_MAX_WAIT_SECONDS,
        quiescence_phase=f"post-compensating-rollback:{services[0]}",
    )
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
    settings_file: Path,
    settings_file_sha256: str,
    image_override_sha256: str,
    probe_source_sha256: str,
    runtime_environment_override: Path,
    runtime_environment_sha256: str,
    reviewed_environment_keys: dict[str, set[str]],
    snapshot: ComposeSnapshot,
    operation_deadline: OperationDeadline,
) -> dict[str, Any]:
    compose_files = tuple(args.compose_file or DEFAULT_COMPOSE_FILES)
    project_dir = Path(args.project_dir)
    candidate_source = Path(args.candidate_source)
    live_env_file = resolve_live_env_file(args, project_dir)
    validate_private_override(
        runtime_environment_override, expected_sha256=runtime_environment_sha256
    )
    with advisory_lock(deadline=operation_deadline):
        fresh = capture(
            project_dir,
            live_env_file=live_env_file,
            deadline=operation_deadline,
        )
        compare_untargeted_runtime(baseline, fresh)
        compare_source_and_quiescence(baseline, fresh)
        verify_untouched_targets(baseline, fresh, untouched_services)
        if applied_services:
            compare_post_apply(baseline, fresh, lock, applied_services)
        candidate_image_snapshot(lock, deadline=operation_deadline)
        config = compose_config(
            project_dir,
            compose_files,
            candidate_version=lock["candidate_version"],
            probe_source=Path(args.probe_source),
            live_env_file=live_env_file,
            expected_source=baseline["source"],
            candidate_source=candidate_source,
            candidate_lock=lock,
            image_override=image_override,
            image_override_sha256=image_override_sha256,
            environment_override=runtime_environment_override,
            environment_override_sha256=runtime_environment_sha256,
            settings_file=settings_file,
            settings_file_sha256=settings_file_sha256,
            probe_source_sha256=probe_source_sha256,
            snapshot=snapshot,
            deadline=operation_deadline,
        )
        authored_baseline = compose_config(
            project_dir,
            compose_files,
            candidate_version=lock["candidate_version"],
            probe_source=Path(args.probe_source),
            live_env_file=live_env_file,
            expected_source=baseline["source"],
            candidate_source=candidate_source,
            candidate_lock=lock,
            settings_file=settings_file,
            settings_file_sha256=settings_file_sha256,
            probe_source_sha256=probe_source_sha256,
            snapshot=snapshot,
            deadline=operation_deadline,
        )
        check_candidate_config(
            config,
            baseline,
            lock,
            authored_baseline,
            reviewed_environment_keys=reviewed_environment_keys,
        )
        # Take the final settled sample after all preflight commands and
        # immediately before the targeted Compose mutation.
        final_quiescence = settled_queue_snapshot(
            operation_deadline,
            phase=f"pre-recreation:{services[0]}",
        )
        if not final_quiescence.get("stability", {}).get("stable"):
            raise GuardError("queue was not settled immediately before targeted recreation")
        targeted_up(
            project_dir,
            compose_files,
            services,
            candidate_version=lock["candidate_version"],
            probe_source=Path(args.probe_source),
            live_env_file=live_env_file,
            expected_source=baseline["source"],
            candidate_source=candidate_source,
            candidate_lock=lock,
            image_override=image_override,
            image_override_sha256=image_override_sha256,
            environment_override=runtime_environment_override,
            environment_override_sha256=runtime_environment_sha256,
            settings_file=settings_file,
            settings_file_sha256=settings_file_sha256,
            probe_source_sha256=probe_source_sha256,
            deadline=operation_deadline,
        )
        observed = capture(
            project_dir,
            live_env_file=live_env_file,
            deadline=operation_deadline,
            require_settled_quiescence=False,
        )
        write_post_recreation_quiescence(
            backup_dir, services, observed, stage="observed"
        )
        replacements = record_replacements(baseline, observed, lock, services)
        write_replacement_manifest(backup_dir, replacements, name=f"replacements-{services[0]}.json")
        wait_healthy(services, operation_deadline=operation_deadline)
        final = capture(
            project_dir,
            live_env_file=live_env_file,
            deadline=operation_deadline,
            quiescence_max_wait_seconds=POST_RECREATION_QUIESCENCE_MAX_WAIT_SECONDS,
            quiescence_phase=f"post-recreation:{services[0]}",
        )
        write_post_recreation_quiescence(
            backup_dir, services, final, stage="settled"
        )
        compare_post_apply(baseline, final, lock, services)
        compare_untargeted_runtime(baseline, final)
        compare_source_and_quiescence(baseline, final)
        return replacements


def command_preflight(args: argparse.Namespace) -> int:
    deadline = OperationDeadline(args.operation_timeout)
    lock = load_lock(Path(args.candidate_lock))
    state_dir = Path(args.candidate_lock).resolve().parent
    image_override = candidate_image_override(
        lock, state_dir / CANDIDATE_IMAGE_FILENAME, replace=True
    )
    runtime_environment_override = state_dir / RUNTIME_ENVIRONMENT_FILENAME
    settings_file = state_dir / COMPOSE_SETTINGS_FILENAME
    prepare(
        args,
        image_override,
        runtime_environment_override=runtime_environment_override,
        settings_file=settings_file,
        replace_runtime_environment_override=True,
        replace_settings_file=True,
        operation_deadline=deadline,
    )
    lock = load_lock(Path(args.candidate_lock))
    project_dir = Path(args.project_dir)
    live_env_file = resolve_live_env_file(args, project_dir)
    probe_source = Path(args.probe_source)
    probe_source_sha256 = (lock.get("artifacts") or {}).get(
        "docker/healthchecks/unstract-services.sh"
    )
    if not isinstance(probe_source_sha256, str):
        raise GuardError("candidate lock lacks the guarded probe source digest")
    snapshot = create_compose_snapshot(
        state_dir=state_dir,
        project_dir=project_dir,
        compose_files=tuple(args.compose_file or DEFAULT_COMPOSE_FILES),
        live_env_file=live_env_file,
        probe_source=probe_source,
        probe_source_sha256=probe_source_sha256,
        image_override=image_override,
        image_override_sha256=sha256_file(image_override),
        environment_override=runtime_environment_override,
        environment_override_sha256=sha256_file(runtime_environment_override),
    )
    write_replay_manifest(
        state_dir / REPLAY_MANIFEST_FILENAME,
        lock_path=Path(args.candidate_lock),
        lock=lock,
        image_override=image_override,
        settings_file=settings_file,
        runtime_environment_override=runtime_environment_override,
        probe_source=probe_source,
        probe_source_sha256=probe_source_sha256,
        snapshot=snapshot,
    )
    print(
        "preflight: candidate source, image lock, Compose identity, runtime, "
        "data, network, environment, queue, and active-job state verified; "
        f"durable replay state refreshed at {state_dir}"
    )
    return 0


def command_start(args: argparse.Namespace) -> int:
    """Replay the durable state through the normal whole-project startup path."""
    if args.confirm != START_CONFIRM_TOKEN:
        raise GuardError(f"start requires --confirm {START_CONFIRM_TOKEN}")
    operation_deadline = OperationDeadline(args.operation_timeout)
    project_dir = Path(args.project_dir)
    live_env_file = resolve_live_env_file(args, project_dir)
    state_dir = Path(args.state_dir).resolve()
    image_override = state_dir / CANDIDATE_IMAGE_FILENAME
    settings_file = state_dir / COMPOSE_SETTINGS_FILENAME
    runtime_environment_override = state_dir / RUNTIME_ENVIRONMENT_FILENAME
    baseline = load_baseline(Path(args.baseline))
    lock = load_lock(Path(args.candidate_lock))
    candidate_source = Path(args.candidate_source)
    probe_source = Path(args.probe_source)
    require_clean_candidate_source(
        candidate_source, lock["source_commit"], lock.get("source_tree")
    )
    verify_artifacts(candidate_source, lock)
    probe_source_sha256 = (lock.get("artifacts") or {}).get(
        "docker/healthchecks/unstract-services.sh"
    )
    if not isinstance(probe_source_sha256, str):
        raise GuardError("candidate lock lacks the guarded probe source digest")
    validate_probe_source(probe_source, expected_sha256=probe_source_sha256)
    validate_candidate_image_override(image_override)
    replay_manifest = load_replay_manifest(
        state_dir / REPLAY_MANIFEST_FILENAME,
        lock_path=Path(args.candidate_lock),
        lock=lock,
        image_override=image_override,
        settings_file=settings_file,
        runtime_environment_override=runtime_environment_override,
        probe_source=probe_source,
        probe_source_sha256=probe_source_sha256,
        state_dir=state_dir,
    )
    snapshot = replay_manifest["compose_snapshot"]
    settings = validate_compose_settings(
        settings_file,
        candidate_version=lock["candidate_version"],
        probe_source=probe_source,
        probe_source_sha256=probe_source_sha256,
        expected_sha256=replay_manifest["settings_sha256"],
    )
    image_override_sha256 = replay_manifest["image_override_sha256"]
    settings_file_sha256 = replay_manifest["settings_sha256"]
    runtime_environment_sha256 = replay_manifest["runtime_environment_sha256"]
    candidate_images = candidate_image_snapshot(lock, deadline=operation_deadline)
    if not candidate_images:
        raise GuardError("no candidate images were verified")
    compose_files = tuple(args.compose_file or DEFAULT_COMPOSE_FILES)
    config = compose_config(
        project_dir,
        compose_files,
        candidate_version=lock["candidate_version"],
        probe_source=probe_source,
        live_env_file=live_env_file,
        expected_source=baseline["source"],
        candidate_source=candidate_source,
        candidate_lock=lock,
        image_override=image_override,
        image_override_sha256=image_override_sha256,
        environment_override=runtime_environment_override,
        environment_override_sha256=runtime_environment_sha256,
        settings_file=settings_file,
        settings_file_sha256=settings_file_sha256,
        probe_source_sha256=probe_source_sha256,
        snapshot=snapshot,
        deadline=operation_deadline,
    )
    authored_baseline = compose_config(
        project_dir,
        compose_files,
        candidate_version=lock["candidate_version"],
        probe_source=probe_source,
        live_env_file=live_env_file,
        expected_source=baseline["source"],
        candidate_source=candidate_source,
        candidate_lock=lock,
        settings_file=settings_file,
        settings_file_sha256=settings_file_sha256,
        probe_source_sha256=probe_source_sha256,
        snapshot=snapshot,
        deadline=operation_deadline,
    )
    reviewed_keys = reviewed_environment_keys_from_override(runtime_environment_override)
    check_candidate_config(
        config,
        baseline,
        lock,
        authored_baseline,
        reviewed_environment_keys=reviewed_keys,
    )
    compose_start(
        project_dir,
        compose_files,
        candidate_version=settings["VERSION"],
        probe_source=probe_source,
        live_env_file=live_env_file,
        expected_source=baseline["source"],
        candidate_source=candidate_source,
        candidate_lock=lock,
        image_override=image_override,
        image_override_sha256=image_override_sha256,
        environment_override=runtime_environment_override,
        environment_override_sha256=runtime_environment_sha256,
        settings_file=settings_file,
        settings_file_sha256=settings_file_sha256,
        probe_source_sha256=probe_source_sha256,
        snapshot=snapshot,
        deadline=operation_deadline,
    )
    wait_running(TARGET_SERVICES, operation_deadline=operation_deadline)
    post = capture(
        project_dir,
        live_env_file=live_env_file,
        deadline=operation_deadline,
    )
    write_json(state_dir / "post-start.json", post)
    print(f"start: verified durable Compose replay; state={state_dir}")
    return 0


def command_apply(args: argparse.Namespace) -> int:
    if args.confirm != CONFIRM_TOKEN:
        raise GuardError(f"apply requires --confirm {CONFIRM_TOKEN}")
    operation_deadline = OperationDeadline(args.operation_timeout)
    backup_dir = Path(args.backup_dir).resolve()
    project_dir = Path(args.project_dir)
    live_env_file = resolve_live_env_file(args, project_dir)
    attempted: list[str] = []
    applied: list[str] = []
    backup_images: dict[str, Any] | None = None
    runtime_environment_override = backup_dir / RUNTIME_ENVIRONMENT_FILENAME
    runtime_environment_sha256 = ""
    compose_snapshot: ComposeSnapshot | None = None
    reviewed_environment_keys: dict[str, set[str]] = {}
    replacement_manifest: dict[str, Any] = {
        "schema": REPLACEMENT_SCHEMA,
        "services": {},
        "unresolved": [],
    }
    with local_operation_lock(backup_dir / ".guard.lock", deadline=operation_deadline):
        lock_hint = load_lock(Path(args.candidate_lock))
        image_override = candidate_image_override(
            lock_hint, backup_dir / CANDIDATE_IMAGE_FILENAME
        )
        settings_file = backup_dir / COMPOSE_SETTINGS_FILENAME
        try:
            (
                baseline,
                lock,
                _,
                reviewed_environment_keys,
                runtime_environment_sha256,
            ) = prepare(
                args,
                image_override,
                runtime_environment_override=runtime_environment_override,
                settings_file=settings_file,
                operation_deadline=operation_deadline,
            )
            image_override_sha256 = sha256_file(image_override)
            settings_file_sha256 = sha256_file(settings_file)
            probe_source_sha256 = (lock.get("artifacts") or {}).get(
                "docker/healthchecks/unstract-services.sh"
            )
            if not isinstance(probe_source_sha256, str):
                raise GuardError("candidate lock lacks the guarded probe source digest")
            compose_snapshot = create_compose_snapshot(
                state_dir=backup_dir,
                project_dir=project_dir,
                compose_files=tuple(args.compose_file or DEFAULT_COMPOSE_FILES),
                live_env_file=live_env_file,
                probe_source=Path(args.probe_source),
                probe_source_sha256=probe_source_sha256,
                image_override=image_override,
                image_override_sha256=image_override_sha256,
                environment_override=runtime_environment_override,
                environment_override_sha256=runtime_environment_sha256,
            )
            with advisory_lock(deadline=operation_deadline):
                fresh = capture(
                    project_dir,
                    live_env_file=live_env_file,
                    deadline=operation_deadline,
                )
                compare_baseline_current(baseline, fresh, allow_new_probe=False)
                compare_untargeted_runtime(baseline, fresh)
                compare_source_and_quiescence(baseline, fresh)
                candidate_image_snapshot(lock, deadline=operation_deadline)
                backup_images = commit_backups(
                    fresh,
                    backup_dir,
                    runtime_environment_override=runtime_environment_override,
                    runtime_environment_sha256=runtime_environment_sha256,
                    reviewed_environment_keys=reviewed_environment_keys,
                    image_override=image_override,
                    image_override_sha256=image_override_sha256,
                    settings_file=settings_file,
                    settings_file_sha256=settings_file_sha256,
                    probe_source=Path(args.probe_source),
                    probe_source_sha256=probe_source_sha256,
                    deadline=operation_deadline,
                )
            write_replay_manifest(
                backup_dir / REPLAY_MANIFEST_FILENAME,
                lock_path=Path(args.candidate_lock),
                lock=lock,
                image_override=image_override,
                settings_file=settings_file,
                runtime_environment_override=runtime_environment_override,
                probe_source=Path(args.probe_source),
                probe_source_sha256=probe_source_sha256,
                snapshot=compose_snapshot,
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
                settings_file=settings_file,
                settings_file_sha256=settings_file_sha256,
                image_override_sha256=image_override_sha256,
                probe_source_sha256=probe_source_sha256,
                runtime_environment_override=runtime_environment_override,
                runtime_environment_sha256=runtime_environment_sha256,
                reviewed_environment_keys=reviewed_environment_keys,
                snapshot=compose_snapshot,
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
                settings_file=settings_file,
                settings_file_sha256=settings_file_sha256,
                image_override_sha256=image_override_sha256,
                probe_source_sha256=probe_source_sha256,
                runtime_environment_override=runtime_environment_override,
                runtime_environment_sha256=runtime_environment_sha256,
                reviewed_environment_keys=reviewed_environment_keys,
                snapshot=compose_snapshot,
                operation_deadline=operation_deadline,
            )
            replacement_manifest["services"].update(
                core_replacements.get("services", {})
            )
            applied.extend(CORE_SERVICES)
            write_replacement_manifest(backup_dir, replacement_manifest)
            final = capture(
                project_dir,
                live_env_file=live_env_file,
                deadline=operation_deadline,
                quiescence_max_wait_seconds=POST_RECREATION_QUIESCENCE_MAX_WAIT_SECONDS,
                quiescence_phase="post-apply",
            )
            compare_post_apply(baseline, final, lock, TARGET_SERVICES)
            compare_untargeted_runtime(baseline, final)
            compare_source_and_quiescence(baseline, final)
            write_json(backup_dir / "post-apply.json", final)
        except Exception as exc:
            failure_record = {
                "schema": FAILURE_SCHEMA,
                "original_error": exception_reason(exc),
            }
            if isinstance(exc, QuiescenceTimeout):
                failure_record["quiescence_timeout"] = exc.evidence()
            try:
                write_json(backup_dir / "apply-failure.json", failure_record)
            except OSError:
                pass
            if backup_images is not None and attempted:
                try:
                    failed_state = capture(
                        project_dir,
                        live_env_file=live_env_file,
                        deadline=operation_deadline,
                        require_settled_quiescence=False,
                    )
                    write_json(backup_dir / "failed-state.json", failed_state)
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
                            runtime_environment_override=runtime_environment_override,
                            runtime_environment_sha256=runtime_environment_sha256,
                            operation_deadline=operation_deadline,
                        )
                except Exception as rollback_error:
                    failure_record["rollback_error"] = exception_reason(rollback_error)
                    if isinstance(rollback_error, QuiescenceTimeout):
                        failure_record["rollback_quiescence_timeout"] = (
                            rollback_error.evidence()
                        )
                    try:
                        write_json(backup_dir / "apply-failure.json", failure_record)
                    except OSError:
                        pass
                    raise GuardError(
                        "guarded apply failed and compensating rollback failed; "
                        "manual recovery is required; "
                        f"original failure: {failure_record['original_error']}; "
                        f"recovery failure: {failure_record['rollback_error']}"
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
    runtime_environment_override, runtime_environment_sha256 = runtime_environment_backup(
        backup_images, backup_dir
    )
    with local_operation_lock(backup_dir / ".guard.lock", deadline=operation_deadline):
        compensating_rollback(
            args,
            baseline,
            backup_images,
            replacement_manifest,
            backup_dir,
            runtime_environment_override=runtime_environment_override,
            runtime_environment_sha256=runtime_environment_sha256,
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
    parser.add_argument(
        "--live-env-file",
        default=None,
        help="the existing private Compose .env; its values are read but never printed or copied",
    )
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
    capture_parser.add_argument("--live-env-file", default=None)
    capture_parser.add_argument("--output", required=True)
    capture_parser.add_argument(
        "--operation-timeout",
        type=float,
        default=DEFAULT_COMMAND_TIMEOUT_SECONDS,
    )
    preflight_parser = sub.add_parser(
        "preflight",
        help="candidate and drift checks; refreshes private durable replay state",
    )
    add_common(preflight_parser)
    start_parser = sub.add_parser(
        "start",
        help="guarded whole-project startup using durable candidate/settings/environment state",
    )
    add_common(start_parser)
    start_parser.add_argument("--state-dir", required=True)
    start_parser.add_argument("--confirm", required=True)
    apply_parser = sub.add_parser("apply", help="explicit targeted recreation")
    add_common(apply_parser)
    apply_parser.add_argument("--backup-dir", required=True)
    apply_parser.add_argument("--confirm", required=True)
    rollback_parser = sub.add_parser("rollback", help="explicit targeted compensating rollback")
    rollback_parser.add_argument("--project-dir", default=str(DEFAULT_PROJECT_DIR))
    rollback_parser.add_argument("--rollback-compose-file", action="append", default=None)
    rollback_parser.add_argument("--live-env-file", default=None)
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
        if args.command == "start":
            return command_start(args)
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
