"""Tests for readiness bound to the Celery broker heartbeat lifecycle."""

from __future__ import annotations

from shared.infrastructure.monitoring.health import HealthStatus, WorkerHeartbeat


def test_worker_heartbeat_is_unhealthy_before_worker_ready():
    heartbeat = WorkerHeartbeat(stale_after_seconds=30)

    result = heartbeat.check()

    assert result.status is HealthStatus.UNHEALTHY
    assert result.message == "Worker broker readiness has not been established"


def test_worker_ready_establishes_initial_freshness():
    heartbeat = WorkerHeartbeat(stale_after_seconds=30)
    heartbeat.mark_ready()

    result = heartbeat.check()

    assert result.status is HealthStatus.HEALTHY
    assert result.details["ready"] is True
    assert result.details["heartbeat_age_seconds"] < 1


def test_heartbeat_refresh_is_ignored_after_shutdown():
    heartbeat = WorkerHeartbeat(stale_after_seconds=30)
    heartbeat.mark_ready()
    heartbeat.mark_stopped()
    heartbeat.mark_heartbeat()

    result = heartbeat.check()

    assert result.status is HealthStatus.UNHEALTHY
    assert result.message == "Worker shutdown has started"


def test_stale_heartbeat_is_unhealthy(monkeypatch):
    clock = iter((100.0, 140.0))
    monkeypatch.setattr(
        "shared.infrastructure.monitoring.health.time.monotonic",
        lambda: next(clock),
    )
    heartbeat = WorkerHeartbeat(stale_after_seconds=30)
    heartbeat.mark_ready()

    result = heartbeat.check()

    assert result.status is HealthStatus.UNHEALTHY
    assert result.message == "Worker heartbeat is stale"
