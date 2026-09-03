"""Tests for cross-vendor accelerator preflight reporting."""

import json
import sys
from pathlib import Path
from subprocess import CompletedProcess

sys.path.insert(0, str(Path(__file__).parent))

import gpu_preflight  # noqa: E402


def test_build_report_detects_intel_xpu_when_nvidia_is_absent(monkeypatch) -> None:
    """Intel XPU telemetry is sufficient when nvidia-smi is unavailable."""
    monkeypatch.setattr(
        gpu_preflight.shutil,
        "which",
        lambda command: "/usr/bin/xpu-smi" if command == "xpu-smi" else None,
    )
    telemetry = {
        "device": 0,
        "metrics": {
            "memory.total": "24480",
            "memory.used": "21707.04",
        },
    }

    def fake_run(command, **kwargs):
        assert command[0] == "/usr/bin/xpu-smi"
        assert kwargs["timeout"] == 10
        return CompletedProcess(command, 0, stdout=json.dumps(telemetry), stderr="")

    monkeypatch.setattr(gpu_preflight.subprocess, "run", fake_run)

    report = gpu_preflight.build_report(1, 24000)

    assert report["available"] is True
    assert report["backend"] == "intel-xpu"
    assert report["gpus"] == [
        {
            "index": "0",
            "name": "Intel XPU 0",
            "memory_total_mib": "24480",
            "memory_used_mib": "21707",
            "driver_version": "unknown",
        }
    ]


def test_intel_preflight_reports_bad_telemetry(monkeypatch) -> None:
    """Malformed Intel telemetry must fail closed with a useful error."""
    monkeypatch.setattr(gpu_preflight.shutil, "which", lambda command: "/usr/bin/xpu-smi")
    monkeypatch.setattr(
        gpu_preflight.subprocess,
        "run",
        lambda command, **kwargs: CompletedProcess(command, 0, stdout="{}", stderr=""),
    )

    report = gpu_preflight.build_report(1, 0, backend="intel")

    assert report["available"] is False
    assert "metrics" in report["error"]
