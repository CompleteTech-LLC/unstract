"""Tests for the Redis log-stream consumer's delivery semantics (UN-3755).

The consumer replaces a Celery worker running with ``task_acks_late=True``, where a
crash mid-task means redelivery. A bare ``BLPOP`` would have quietly made crashes lossy,
so the loop parks each envelope on a per-pod processing list and removes it only after
the handler returns. These pin that contract — it is the part that is easy to regress
into "logs vanish when a pod restarts" without any test noticing.

The module is loaded over faked worker-framework imports so the test needs neither a
Celery app nor a live Redis.
"""

from __future__ import annotations

import importlib.util
import json
import sys
import types
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

_MODULE = (
    Path(__file__).resolve().parent.parent / "log_consumer" / "redis_stream_consumer.py"
)


def _load(monkeypatch):
    """Import the consumer with its framework + task imports stubbed out."""
    monkeypatch.setenv("LOG_STREAM_QUEUE_NAME", "log_stream_queue")
    monkeypatch.setenv("HOSTNAME", "pod-abc")

    def _mod(name, **attrs):
        m = types.ModuleType(name)
        for k, v in attrs.items():
            setattr(m, k, v)
        return m

    logs_consumer = MagicMock(name="logs_consumer")
    stubs = {
        "shared": _mod("shared"),
        "shared.enums": _mod("shared.enums"),
        "shared.enums.worker_enums": _mod(
            "shared.enums.worker_enums",
            WorkerType=types.SimpleNamespace(LOG_CONSUMER="log_consumer"),
        ),
        "shared.infrastructure": _mod("shared.infrastructure"),
        "shared.infrastructure.config": _mod("shared.infrastructure.config"),
        "shared.infrastructure.config.builder": _mod(
            "shared.infrastructure.config.builder",
            WorkerBuilder=types.SimpleNamespace(
                build_celery_app=lambda _t: (MagicMock(), MagicMock())
            ),
        ),
        "shared.infrastructure.logging": _mod(
            "shared.infrastructure.logging",
            WorkerLogger=types.SimpleNamespace(setup=lambda _t: MagicMock()),
        ),
        "log_consumer": _mod("log_consumer"),
        "log_consumer.tasks": _mod("log_consumer.tasks", logs_consumer=logs_consumer),
    }
    for name, mod in stubs.items():
        monkeypatch.setitem(sys.modules, name, mod)

    spec = importlib.util.spec_from_file_location(
        "log_consumer.redis_stream_consumer", _MODULE
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    mod._test_logs_consumer = logs_consumer
    return mod


@pytest.fixture
def consumer(monkeypatch):
    return _load(monkeypatch)


def _envelope(task="logs_consumer", **kwargs):
    return json.dumps({"task": task, "kwargs": kwargs or {"event": "logs:c", "room": "c"}})


class TestDispatch:
    def test_runs_the_existing_task_body_with_the_envelope_kwargs(self, consumer):
        consumer._dispatch(_envelope(event="logs:c1", user_session_id="c1"))
        consumer._test_logs_consumer.assert_called_once_with(
            event="logs:c1", user_session_id="c1"
        )

    def test_rejects_an_unexpected_task_name_loudly(self, consumer):
        # Dispatching blind would run the log handler on a foreign payload; raising here
        # surfaces a producer/consumer mismatch instead of corrupting the stream.
        with pytest.raises(ValueError, match="Unexpected task"):
            consumer._dispatch(_envelope(task="something_else"))
        consumer._test_logs_consumer.assert_not_called()


class TestAtLeastOnceDelivery:
    def test_processing_list_is_scoped_to_this_pod(self, consumer):
        # A shared list would let one pod reclaim another's in-flight envelope and
        # replay it while the owner is still working on it.
        assert consumer._processing_list_name() == "log_stream_queue:processing:pod-abc"

    def test_startup_requeues_what_the_previous_run_left_in_flight(self, consumer):
        redis = MagicMock()
        redis.lmove.side_effect = [b"a", b"b", None]
        consumer._recover_in_flight(redis, "proc")
        assert redis.lmove.call_count == 3
        # Back to the HEAD of the source list, so recovered logs precede newer ones.
        assert redis.lmove.call_args_list[0][0] == ("proc", "log_stream_queue", "RIGHT", "LEFT")

    def _one_shot_redis(self, consumer, raw):
        """A redis mock that yields exactly one envelope, then ends the loop.

        ``lmove`` must return None or startup recovery spins forever — a real Redis
        returns nil on an empty list, but a bare MagicMock is truthy.
        """
        redis = MagicMock()
        redis.lmove.return_value = None

        def _blmove(*_a, **_k):
            if redis.blmove.call_count == 1:
                return raw
            consumer._shutdown = True
            return None

        redis.blmove.side_effect = _blmove
        return redis

    def test_envelope_is_removed_only_after_the_handler_returns(self, consumer):
        raw = _envelope()
        redis = self._one_shot_redis(consumer, raw)
        order = []
        redis.lrem.side_effect = lambda *a: order.append("lrem")
        consumer._test_logs_consumer.side_effect = lambda **_: order.append("handled")

        with patch.object(consumer, "create_redis_client", return_value=redis):
            consumer.run()

        # Order is the whole point: lrem before the handler would lose the envelope on
        # a crash, which is exactly the acks_late behaviour this replaces.
        assert order == ["handled", "lrem"]
        redis.lrem.assert_called_once_with("log_stream_queue:processing:pod-abc", 1, raw)

    def test_a_poison_envelope_is_dropped_not_replayed_forever(self, consumer):
        raw = b"not-json"
        redis = self._one_shot_redis(consumer, raw)
        with patch.object(consumer, "create_redis_client", return_value=redis):
            consumer.run()

        # Still removed from the processing list — otherwise startup recovery would
        # re-queue it on every restart and the loop would never drain.
        redis.lrem.assert_called_once_with("log_stream_queue:processing:pod-abc", 1, raw)


class TestSocketTimeoutOutlivesTheBlock:
    """redis-py applies ``socket_timeout`` to the BLMOVE read itself.

    Shipped equal to the block (both 5s) and the socket won the race in integration:
    ``redis.exceptions.TimeoutError: Timeout reading from socket`` every ~5.01s while
    idle, each one tearing down the connection. The quiet part is worse — BLMOVE is
    atomic server-side, so an envelope could be moved onto the processing list and its
    reply then lost with the socket, stranding that log until the pod restarted.
    """

    def test_socket_timeout_strictly_exceeds_the_block_timeout(self, consumer):
        assert consumer._SOCKET_TIMEOUT_SECONDS > consumer._BLOCK_TIMEOUT_SECONDS

    def test_the_margin_holds_when_the_block_is_tuned_up(self, monkeypatch):
        # The two must stay related by construction, not by both happening to be
        # defaults — a deployment raising the block alone would resurrect the bug.
        monkeypatch.setenv("LOG_STREAM_BLOCK_TIMEOUT", "45")
        mod = _load(monkeypatch)
        assert mod._BLOCK_TIMEOUT_SECONDS == 45
        assert mod._SOCKET_TIMEOUT_SECONDS > 45

    def test_the_client_is_actually_built_with_that_timeout(self, consumer):
        """The constant is inert unless it reaches ``create_redis_client``."""
        redis = MagicMock()
        redis.lmove.return_value = None

        def _blmove(*_a, **_k):
            consumer._shutdown = True
            return None

        redis.blmove.side_effect = _blmove

        with patch.object(
            consumer, "create_redis_client", return_value=redis
        ) as factory:
            consumer.run()

        assert factory.call_args.kwargs["socket_timeout"] == (
            consumer._SOCKET_TIMEOUT_SECONDS
        )

    def test_blmove_is_called_with_the_block_timeout(self, consumer):
        """Pins the other half of the pair the invariant is about."""
        redis = MagicMock()
        redis.lmove.return_value = None

        def _blmove(*_a, **_k):
            consumer._shutdown = True
            return None

        redis.blmove.side_effect = _blmove

        with patch.object(consumer, "create_redis_client", return_value=redis):
            consumer.run()

        assert redis.blmove.call_args[0][2] == consumer._BLOCK_TIMEOUT_SECONDS


class TestRedisStreamHealth:
    def test_is_stale_before_the_first_completed_read(self, consumer):
        health = consumer._RedisStreamHealth("log_stream_queue")

        assert health.seconds_since_last_success() > 100_000
        assert health.status() == {
            "queue": "log_stream_queue",
            "redis_poll_ready": False,
            "redis_poll_failures": 0,
        }

    def test_successful_empty_poll_is_a_real_readiness_signal(self, consumer):
        health = consumer._RedisStreamHealth("log_stream_queue")
        health.mark_success()

        assert health.seconds_since_last_success() < 1
        assert health.status()["redis_poll_ready"] is True

    def test_failures_do_not_refresh_the_success_timestamp(self, consumer):
        health = consumer._RedisStreamHealth("log_stream_queue")
        health.mark_success()
        health.mark_failure()
        health.mark_failure()

        assert health.seconds_since_last_success() < 1
        assert health.status()["redis_poll_failures"] == 2

    def test_health_port_is_opt_in_and_validated(self, consumer, monkeypatch):
        monkeypatch.delenv("LOG_STREAM_CONSUMER_HEALTH_PORT", raising=False)
        assert consumer._health_port_from_env() is None

        monkeypatch.setenv("LOG_STREAM_CONSUMER_HEALTH_PORT", "8091")
        assert consumer._health_port_from_env() == 8091

        monkeypatch.setenv("LOG_STREAM_CONSUMER_HEALTH_PORT", "not-a-port")
        with pytest.raises(ValueError, match="LOG_STREAM_CONSUMER_HEALTH_PORT"):
            consumer._health_port_from_env()

        monkeypatch.setenv("LOG_STREAM_CONSUMER_HEALTH_PORT", "0")
        with pytest.raises(ValueError, match="between 1 and 65535"):
            consumer._health_port_from_env()

    @pytest.mark.parametrize("value", ["0", "-1", "not-a-timeout"])
    def test_block_timeout_is_positive_and_named(self, consumer, monkeypatch, value):
        monkeypatch.setenv("LOG_STREAM_BLOCK_TIMEOUT", value)
        with pytest.raises(ValueError, match="LOG_STREAM_BLOCK_TIMEOUT"):
            consumer._positive_int_env("LOG_STREAM_BLOCK_TIMEOUT", 5)

    @pytest.mark.parametrize("value", ["nan", "inf", "-inf"])
    def test_health_stale_bound_is_finite(self, consumer, monkeypatch, value):
        monkeypatch.setenv("LOG_STREAM_CONSUMER_HEALTH_STALE_SECONDS", value)
        with pytest.raises(ValueError, match="must be positive"):
            consumer._health_stale_seconds()
