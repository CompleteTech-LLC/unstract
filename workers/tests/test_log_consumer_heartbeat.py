"""A fresh process alone must not pass worker progress checks."""

import os
import time

from log_consumer import heartbeat


def test_missing_fresh_and_stale_heartbeat(tmp_path, monkeypatch):
    monkeypatch.setattr(heartbeat, "DIRECTORY", tmp_path)
    assert not heartbeat.healthy(["log-stream"], 120)
    heartbeat.mark("log-stream")
    assert heartbeat.healthy(["log-stream"], 120)
    stale = time.time() - 121
    os.utime(tmp_path / "unstract-log-stream.heartbeat", (stale, stale))
    assert not heartbeat.healthy(["log-stream"], 120)


def test_both_scheduler_tasks_must_report_success(tmp_path, monkeypatch):
    monkeypatch.setattr(heartbeat, "DIRECTORY", tmp_path)
    monkeypatch.setattr(heartbeat.sys, "argv", ["heartbeat", "check", "scheduler"])
    heartbeat.mark("log-history")
    assert heartbeat.main() == 1
    heartbeat.mark("notification-buffer")
    assert heartbeat.main() == 0
    stale = time.time() - 500
    os.utime(tmp_path / "unstract-notification-buffer.heartbeat", (stale, stale))
    assert heartbeat.main() == 1


def test_future_timestamp_cannot_mask_failure(tmp_path, monkeypatch):
    monkeypatch.setattr(heartbeat, "DIRECTORY", tmp_path)
    heartbeat.mark("log-stream")
    future = time.time() + 500
    os.utime(tmp_path / "unstract-log-stream.heartbeat", (future, future))
    assert not heartbeat.healthy(["log-stream"], 120)
