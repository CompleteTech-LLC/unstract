"""Characterize the shell scheduler's read-only readiness contract."""

from __future__ import annotations

from log_consumer.scheduler_health import evaluate_state


def _state(**overrides):
    state = {
        "parent_pid": 42,
        "last_log_success": 90.0,
        "last_buffer_success": 90.0,
        "last_log_failure": None,
        "last_buffer_failure": None,
    }
    state.update(overrides)
    return state


def _alive(_pid: int) -> bool:
    return True


def test_requires_both_periodic_tasks_to_succeed():
    result = evaluate_state(
        _state(last_buffer_success=None),
        now=100.0,
        stale_after=120.0,
        parent_alive=_alive,
    )

    assert result.status == "starting"
    assert result.http_status == 503
    assert "notification_buffer" in result.message


def test_recent_successes_are_healthy():
    result = evaluate_state(
        _state(),
        now=100.0,
        stale_after=120.0,
        parent_alive=_alive,
    )

    assert result.status == "healthy"
    assert result.http_status == 200
    assert result.details["log_history_age_seconds"] == 10.0


def test_new_failure_is_unhealthy_until_a_new_success():
    result = evaluate_state(
        _state(last_log_failure=95.0),
        now=100.0,
        stale_after=120.0,
        parent_alive=_alive,
    )

    assert result.status == "unhealthy"
    assert result.http_status == 503
    assert result.details["task"] == "log_history"


def test_old_success_becomes_stale():
    result = evaluate_state(
        _state(last_buffer_success=0.0),
        now=100.0,
        stale_after=30.0,
        parent_alive=_alive,
    )

    assert result.status == "unhealthy"
    assert "stale" in result.message


def test_dead_parent_is_not_healthy_even_with_fresh_state():
    result = evaluate_state(
        _state(),
        now=100.0,
        stale_after=120.0,
        parent_alive=lambda _pid: False,
    )

    assert result.status == "unhealthy"
    assert result.message == "scheduler loop is not running"
