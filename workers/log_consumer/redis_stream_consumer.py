"""Redis-list consumer for the log stream (UN-3755).

Drains the list ``LogPublisher.publish()`` writes to when ``LOG_TRANSPORT=redis`` and
runs the **existing** ``logs_consumer`` task body against each envelope. Nothing about
what a log *does* changes here: the task still RPUSHes to ``log_history_queue`` and
emits over Socket.IO, and the log-history scheduler still drains that to
``execution_log``. This module replaces only the transport hop that RabbitMQ used to
provide, so the log consumer no longer needs a Celery deployment.

**Why a reliable-queue pattern rather than a bare BLPOP.** The Celery consumer runs with
``task_acks_late=True`` (``shared/infrastructure/config/worker_config.py:545``, and
``backend/celery_config.py:63``), so a worker that dies mid-task gets the message
redelivered. ``BLPOP`` deletes on read, which would silently make crashes lossy — a real
regression, not a no-op. ``BLMOVE`` instead parks the envelope on a per-pod *processing*
list and removes it only after the handler returns, and startup re-queues whatever the
previous incarnation left behind.

Bound worth knowing: recovery is keyed on ``HOSTNAME``, so a **container restart** (crash
loop, OOM kill — the dominant failure mode) recovers fully, while a **pod replacement**
strands that pod's in-flight envelopes, at most one per concurrent loop. That is strictly
better than BLPOP and, for a stream already discarded wholesale at its 10k cap, close
enough to the Celery behaviour it replaces. Sweeping other pods' lists is deliberately not
attempted: with multiple replicas it cannot distinguish a dead owner from a live one, and
would duplicate log lines on every start.
"""

from __future__ import annotations

import json
import os
import signal
import socket
import sys
import threading
import time
from types import FrameType
from typing import Any

from shared.enums.worker_enums import WorkerType
from shared.infrastructure.config.builder import WorkerBuilder
from shared.infrastructure.logging import WorkerLogger

from unstract.core.cache.redis_client import create_redis_client
from unstract.core.constants import LogProcessingTask

logger = WorkerLogger.setup(WorkerType.LOG_CONSUMER)

# Build the Celery app before importing tasks: ``@worker_task`` is ``shared_task``, which
# binds to the current app at call time. Same ordering as ``log_consumer/worker.py``.
app, config = WorkerBuilder.build_celery_app(WorkerType.LOG_CONSUMER)

from log_consumer.tasks import logs_consumer  # noqa: E402

_QUEUE_NAME = os.getenv("LOG_STREAM_QUEUE_NAME", "log_stream_queue")
# BLMOVE blocks up to this long before returning None, which is the loop's only chance to
# notice a shutdown signal. Keep it well under the pod's terminationGracePeriodSeconds.
_BLOCK_TIMEOUT_SECONDS = int(os.getenv("LOG_STREAM_BLOCK_TIMEOUT", "5"))
# redis-py enforces ``socket_timeout`` on the BLMOVE read itself, so it MUST exceed the
# server-side block or every call aborts mid-block with ``redis.TimeoutError``.
# ``create_redis_client`` defaults it to 5s — exactly ``_BLOCK_TIMEOUT_SECONDS`` — and the
# socket won that race in integration: a traceback every ~5.01s while idle, plus a
# disconnect/reconnect each time. Worse, an envelope BLMOVE had already moved to the
# processing list was stranded there until this pod restarted, because the reply never
# reached the client. Identical trap to the one documented at
# ``workers/queue_backend/pg_queue/result_backend.py:152``.
_SOCKET_TIMEOUT_SECONDS = _BLOCK_TIMEOUT_SECONDS + 5

_shutdown = False


class _RedisStreamHealth:
    """Freshness state for the Redis stream poll loop.

    A process check cannot tell whether this consumer can still reach Redis. The
    loop records a successful ``BLMOVE`` return (including an empty-list timeout),
    which proves that the Redis read completed without publishing or consuming
    anything as a side effect of the health request. A failed or hung read leaves
    the previous success timestamp untouched, so the endpoint eventually returns
    503 after the configured bound.
    """

    def __init__(self, queue_name: str) -> None:
        self._queue_name = queue_name
        self._lock = threading.Lock()
        self._last_success: float | None = None
        self._poll_failures = 0

    def mark_success(self) -> None:
        with self._lock:
            self._last_success = time.monotonic()

    def mark_failure(self) -> None:
        with self._lock:
            self._poll_failures += 1

    def seconds_since_last_success(self) -> float:
        with self._lock:
            last_success = self._last_success
        # A finite, large value keeps the JSON response valid before the first
        # completed read while making the probe unambiguously stale.
        return 1_000_000.0 if last_success is None else max(
            0.0, time.monotonic() - last_success
        )

    def status(self) -> dict[str, object]:
        with self._lock:
            failures = self._poll_failures
        return {
            "queue": self._queue_name,
            "redis_poll_failures": failures,
        }


def _health_port_from_env() -> int | None:
    """Return the opt-in health port, validating bad deployment input early."""
    raw = os.getenv("LOG_STREAM_CONSUMER_HEALTH_PORT")
    if raw is None or raw == "":
        return None
    try:
        port = int(raw)
    except ValueError as exc:
        raise ValueError(
            f"Invalid LOG_STREAM_CONSUMER_HEALTH_PORT={raw!r}: {exc}"
        ) from exc
    if not 0 <= port <= 65535:
        raise ValueError(f"LOG_STREAM_CONSUMER_HEALTH_PORT={port} out of range")
    return port


def _health_stale_seconds() -> float:
    """Resolve a bound that outlives the blocking read and its socket timeout."""
    default = max(15.0, float(_SOCKET_TIMEOUT_SECONDS * 2))
    raw = os.getenv("LOG_STREAM_CONSUMER_HEALTH_STALE_SECONDS")
    if raw is None or raw == "":
        return default
    try:
        stale_after = float(raw)
    except ValueError as exc:
        raise ValueError(
            f"Invalid LOG_STREAM_CONSUMER_HEALTH_STALE_SECONDS={raw!r}: {exc}"
        ) from exc
    if stale_after <= 0:
        raise ValueError(
            "LOG_STREAM_CONSUMER_HEALTH_STALE_SECONDS must be positive"
        )
    return stale_after


def _maybe_start_health_server(
    health: _RedisStreamHealth,
) -> Any | None:
    """Start the opt-in loop probe without making it a process dependency."""
    port = _health_port_from_env()
    if port is None:
        return None

    from queue_backend.pg_queue.liveness import LivenessServer

    stale_after = _health_stale_seconds()
    server = LivenessServer(
        freshness_fn=health.seconds_since_last_success,
        stale_after=stale_after,
        port=port,
        check_name="redis_stream_loop",
        age_key="seconds_since_last_successful_poll",
        extra_status_fn=health.status,
        thread_name="redis-stream-liveness",
        log_label="redis stream consumer",
    )
    try:
        server.start()
    except OSError:
        logger.exception(
            "Redis stream consumer: liveness could not bind :%s; continuing "
            "without a probe",
            port,
        )
        return None
    logger.info(
        "Redis stream consumer: liveness on :%s/health (stale after %ss)",
        server.bound_port,
        stale_after,
    )
    return server


def _processing_list_name() -> str:
    """Per-pod parking list, so one pod never reclaims another's in-flight envelope."""
    return f"{_QUEUE_NAME}:processing:{os.getenv('HOSTNAME') or socket.gethostname()}"


def _handle_signal(signum: int, _frame: FrameType | None) -> None:
    """Finish the envelope in hand, then stop. Never abandon work mid-flight."""
    global _shutdown
    _shutdown = True
    logger.info("Signal %s received; finishing current log then shutting down", signum)


def _recover_in_flight(redis_client: Any, processing: str) -> None:
    """Re-queue anything this pod was mid-way through when it last died.

    LMOVE back to the *head* of the source list so recovered envelopes are re-processed
    before newer ones, preserving rough log ordering.
    """
    recovered = 0
    while redis_client.lmove(processing, _QUEUE_NAME, "RIGHT", "LEFT"):
        recovered += 1
    if recovered:
        logger.warning(
            "Recovered %d in-flight log envelope(s) from a previous run of this pod",
            recovered,
        )


def _dispatch(raw: bytes | str) -> None:
    """Run one envelope through the existing task body.

    Dispatches **by name** so a producer/consumer mismatch fails loudly here rather than
    silently discarding the message.
    """
    envelope = json.loads(raw)
    task_name = envelope.get("task")
    if task_name != LogProcessingTask.TASK_NAME:
        raise ValueError(
            f"Unexpected task {task_name!r} on '{_QUEUE_NAME}'; "
            f"expected {LogProcessingTask.TASK_NAME!r}"
        )
    logs_consumer(**envelope.get("kwargs", {}))


def run() -> int:
    signal.signal(signal.SIGTERM, _handle_signal)
    signal.signal(signal.SIGINT, _handle_signal)

    health = _RedisStreamHealth(_QUEUE_NAME)
    health_server = _maybe_start_health_server(health)
    try:
        # Not ``RedisQueueClient.from_env()``: that hard-codes the 5s socket timeout,
        # which cannot outlive this loop's block. Built directly so the two stay
        # related by construction — see _SOCKET_TIMEOUT_SECONDS.
        redis_client = create_redis_client(
            decode_responses=True,
            socket_timeout=_SOCKET_TIMEOUT_SECONDS,
        )
        processing = _processing_list_name()
        logger.info(
            "Log stream consumer starting: queue='%s' processing='%s'",
            _QUEUE_NAME,
            processing,
        )
        _recover_in_flight(redis_client, processing)

        while not _shutdown:
            try:
                raw = redis_client.blmove(
                    _QUEUE_NAME, processing, _BLOCK_TIMEOUT_SECONDS, "LEFT", "RIGHT"
                )
                # A nil result is still a successful Redis round trip and proves
                # that an idle queue is reachable. The health GET itself never
                # calls BLMOVE and therefore never consumes an envelope.
                health.mark_success()
            except Exception:
                health.mark_failure()
                # Connection blips must not kill the pod — the next iteration
                # reconnects via the client's own retry. Sleeping is unnecessary:
                # BLMOVE already blocks.
                logger.error("Log stream read failed; retrying", exc_info=True)
                continue

            if raw is None:  # timeout, so the loop can observe shutdown
                continue

            try:
                _dispatch(raw)
            except Exception:
                # Match the Celery consumer's posture: a poison envelope is logged
                # and dropped, never retried forever. logs_consumer already swallows
                # its own sink failures, so reaching here means a malformed envelope.
                logger.error("Discarding unprocessable log envelope", exc_info=True)
            finally:
                # Remove exactly one copy, whether it succeeded or was discarded —
                # leaving it parked would have it re-queued on the next restart and
                # replayed forever.
                redis_client.lrem(processing, 1, raw)
    finally:
        if health_server is not None:
            health_server.stop()

    logger.info("Log stream consumer stopped")
    return 0


if __name__ == "__main__":
    sys.exit(run())
