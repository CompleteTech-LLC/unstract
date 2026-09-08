"""Local progress signals for workers without an HTTP health server.

Only successful polling/task cycles refresh these files; process existence or a
separate successful dependency probe must not hide a wedged worker loop.
Log-stream progress does not prove that every downstream sink accepted a log;
the consumer can intentionally discard poison envelopes and retry sink work.
"""

import os
import sys
import time
from pathlib import Path

DIRECTORY = Path("/tmp")
NAMES = {"log-stream", "log-history", "notification-buffer"}


def mark(name: str) -> None:
    if name not in NAMES:
        raise ValueError("Unknown heartbeat")
    (DIRECTORY / f"unstract-{name}.heartbeat").touch()


def healthy(names: list[str], max_age: float) -> bool:
    now = time.time()
    try:
        return bool(names) and all(
            name in NAMES
            and 0
            <= now - (DIRECTORY / f"unstract-{name}.heartbeat").stat().st_mtime
            <= max_age
            for name in names
        )
    except OSError:
        return False


def main() -> int:
    if len(sys.argv) == 3 and sys.argv[1] == "mark" and sys.argv[2] in NAMES:
        mark(sys.argv[2])
        return 0
    if sys.argv[1:] == ["check", "log-stream"]:
        max_age = max(1, float(os.getenv("LOG_STREAM_BLOCK_TIMEOUT", "5"))) + 120
        return 0 if healthy(["log-stream"], max_age) else 1
    if sys.argv[1:] == ["check", "scheduler"]:
        bounds = (
            ("log-history", "LOG_HISTORY_CONSUMER_INTERVAL", 5),
            ("notification-buffer", "NOTIFICATION_BUFFER_POLL_INTERVAL", 10),
        )
        return (
            0
            if all(
                healthy([name], max(1, float(os.getenv(variable, str(default)))) + 120)
                for name, variable, default in bounds
            )
            else 1
        )
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
