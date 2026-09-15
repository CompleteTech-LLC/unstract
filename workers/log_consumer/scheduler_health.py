"""Read-only readiness endpoint for the log-history scheduler shell loop.

The scheduler intentionally keeps running when one periodic task fails so a
transient backend error does not kill the container. That makes a process check
misleading: this probe reads the scheduler's small local state file and returns
503 when either task has never succeeded, when a task failure is newer than its
last success, or when either success is stale. A GET never invokes either task.
"""

from __future__ import annotations

import json
import math
import os
import signal
import sys
import time
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Callable
from urllib.parse import urlsplit


@dataclass(frozen=True)
class SchedulerHealth:
    status: str
    message: str
    details: dict[str, Any]

    @property
    def http_status(self) -> int:
        return 200 if self.status == "healthy" else 503


def read_state(path: str | os.PathLike[str]) -> dict[str, Any] | None:
    """Read a bounded JSON state document, treating malformed state as missing."""
    try:
        # The shell writer keeps this document tiny. Refuse unexpectedly large
        # input so a damaged bind mount cannot make the probe unbounded.
        if Path(path).stat().st_size > 16 * 1024:
            return None
        with Path(path).open(encoding="utf-8") as state_file:
            value = json.load(state_file)
    except (OSError, ValueError, TypeError):
        return None
    return value if isinstance(value, dict) else None


def _parent_is_scheduler(parent_pid: int) -> bool:
    """Confirm the recorded PID is still the scheduler shell process."""
    try:
        os.kill(parent_pid, 0)
    except (OSError, TypeError, ValueError):
        return False

    try:
        command_line = Path(f"/proc/{parent_pid}/cmdline").read_bytes()
    except OSError:
        return False
    return b"scheduler.sh" in command_line


def evaluate_state(
    state: dict[str, Any] | None,
    *,
    now: float | None = None,
    stale_after: float,
    parent_alive: Callable[[int], bool] = _parent_is_scheduler,
) -> SchedulerHealth:
    """Evaluate scheduler task freshness without executing application work."""
    if not math.isfinite(stale_after) or stale_after <= 0:
        raise ValueError("stale_after must be positive")
    if state is None:
        return SchedulerHealth("unhealthy", "scheduler state unavailable", {})

    current_time = time.time() if now is None else now
    try:
        current_time = float(current_time)
    except (TypeError, ValueError):
        return SchedulerHealth(
            "unhealthy", "scheduler evaluation time is invalid", {}
        )
    if not math.isfinite(current_time):
        return SchedulerHealth(
            "unhealthy", "scheduler evaluation time is invalid", {}
        )
    try:
        parent_pid = int(state["parent_pid"])
    except (KeyError, TypeError, ValueError):
        return SchedulerHealth("unhealthy", "scheduler parent identity unavailable", {})
    if not parent_alive(parent_pid):
        return SchedulerHealth(
            "unhealthy",
            "scheduler loop is not running",
            {"parent_pid": parent_pid},
        )

    ages: dict[str, float] = {}
    failure_fields = {
        "log_history": "last_log_success",
        "notification_buffer": "last_buffer_success",
    }
    for task_name, success_key in failure_fields.items():
        success_value = state.get(success_key)
        failure_key = success_key.replace("success", "failure")
        failure_value = state.get(failure_key)
        if success_value is None:
            return SchedulerHealth(
                "starting",
                f"{task_name} task has not completed successfully",
                {"task": task_name},
            )
        try:
            success_time = float(success_value)
        except (TypeError, ValueError):
            return SchedulerHealth(
                "unhealthy",
                f"{task_name} task success timestamp is invalid",
                {"task": task_name},
            )
        if not math.isfinite(success_time):
            return SchedulerHealth(
                "unhealthy",
                f"{task_name} task success timestamp is invalid",
                {"task": task_name},
            )
        if success_time > current_time:
            return SchedulerHealth(
                "unhealthy",
                f"{task_name} task success timestamp is in the future",
                {"task": task_name},
            )
        ages[task_name] = current_time - success_time
        try:
            if failure_value is not None:
                failure_time = float(failure_value)
                if not math.isfinite(failure_time):
                    raise ValueError
                if failure_time > current_time:
                    return SchedulerHealth(
                        "unhealthy",
                        f"{task_name} task failure timestamp is in the future",
                        {"task": task_name},
                    )
                if failure_time > success_time:
                    return SchedulerHealth(
                        "unhealthy",
                        f"{task_name} task failed after its last success",
                        {"task": task_name, "age_seconds": round(ages[task_name], 3)},
                    )
        except (TypeError, ValueError):
            return SchedulerHealth(
                "unhealthy",
                f"{task_name} task failure timestamp is invalid",
                {"task": task_name},
            )
        if ages[task_name] > stale_after:
            return SchedulerHealth(
                "unhealthy",
                f"{task_name} task success is stale",
                {
                    "task": task_name,
                    "age_seconds": round(ages[task_name], 3),
                    "stale_after_seconds": stale_after,
                },
            )

    return SchedulerHealth(
        "healthy",
        "scheduler loop and both periodic tasks are fresh",
        {
            "parent_pid": parent_pid,
            "log_history_age_seconds": round(ages["log_history"], 3),
            "notification_buffer_age_seconds": round(
                ages["notification_buffer"], 3
            ),
            "stale_after_seconds": stale_after,
        },
    )


def serve(
    *,
    port: int,
    state_path: str,
    stale_after: float,
    parent_pid: int,
) -> None:
    """Serve the scheduler probe until the shell parent terminates this process."""

    class Handler(BaseHTTPRequestHandler):
        def setup(self) -> None:
            super().setup()
            # A health client that connects and never finishes its request must
            # not pin a server thread indefinitely.
            self.connection.settimeout(2.0)

        def do_GET(self) -> None:
            if urlsplit(self.path).path not in {"/health", "/healthz", "/livez"}:
                try:
                    self.send_response(404)
                    self.end_headers()
                except (BrokenPipeError, ConnectionResetError, TimeoutError):
                    pass
                return
            state = read_state(state_path)
            result = evaluate_state(
                state,
                stale_after=stale_after,
                parent_alive=lambda pid: (
                    pid == parent_pid and _parent_is_scheduler(pid)
                ),
            )
            body = json.dumps(
                {
                    "status": result.status,
                    "check": "log_history_scheduler",
                    "message": result.message,
                    **result.details,
                },
                separators=(",", ":"),
            ).encode("utf-8")
            try:
                self.send_response(result.http_status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
            except (BrokenPipeError, ConnectionResetError, TimeoutError):
                pass

        def log_message(self, *_: object) -> None:
            pass

    port = _positive_port(str(port), "LOG_HISTORY_SCHEDULER_HEALTH_PORT")
    server = ThreadingHTTPServer(("0.0.0.0", port), Handler)
    server.daemon_threads = True
    server.block_on_close = False

    def _stop(_signum: int, _frame: object) -> None:
        raise SystemExit(0)

    signal.signal(signal.SIGTERM, _stop)
    signal.signal(signal.SIGINT, _stop)
    try:
        server.serve_forever(poll_interval=0.5)
    finally:
        server.server_close()


def _positive_float(raw: str, name: str) -> float:
    try:
        value = float(raw)
    except ValueError as exc:
        raise ValueError(f"{name} must be numeric") from exc
    if not math.isfinite(value) or value <= 0:
        raise ValueError(f"{name} must be positive")
    return value


def _positive_port(raw: str, name: str) -> int:
    """Parse a concrete listening port; zero is not a deployable health port."""
    try:
        value = int(raw)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{name} must be an integer port") from exc
    if not 1 <= value <= 65535:
        raise ValueError(f"{name} must be between 1 and 65535")
    return value


def main() -> None:
    port = _positive_port(
        os.environ["LOG_HISTORY_SCHEDULER_HEALTH_PORT"],
        "LOG_HISTORY_SCHEDULER_HEALTH_PORT",
    )
    state_path = os.getenv(
        "LOG_HISTORY_SCHEDULER_HEALTH_STATE", "/tmp/log-history-scheduler-health.json"
    )
    stale_after = _positive_float(
        os.getenv("LOG_HISTORY_SCHEDULER_HEALTH_STALE_SECONDS", "120"),
        "LOG_HISTORY_SCHEDULER_HEALTH_STALE_SECONDS",
    )
    parent_pid = int(os.environ["LOG_HISTORY_SCHEDULER_HEALTH_PARENT_PID"])
    serve(
        port=port,
        state_path=state_path,
        stale_after=stale_after,
        parent_pid=parent_pid,
    )


if __name__ == "__main__":
    try:
        main()
    except (KeyError, ValueError) as exc:
        print(f"scheduler health configuration error: {exc}", file=sys.stderr)
        raise SystemExit(2) from exc
