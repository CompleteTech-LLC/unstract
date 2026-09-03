#!/usr/bin/env python3
"""Check host accelerator visibility before launching embedding services."""

from __future__ import annotations

import argparse
import csv
import json
import shutil
import subprocess
import sys
from typing import Any

NVIDIA_GPU_QUERY = "index,name,memory.total,driver_version"
NVIDIA_GPU_FIELDS = ("index", "name", "memory_total_mib", "driver_version")
INTEL_XPU_COMMAND = (
    "xpu-smi",
    "dump",
    "--json",
    "--device",
    "0",
    "--metrics",
    "MEMORY,UTILIZATION,POWER,CLOCK",
    "--number",
    "1",
)


def run_command(
    command: list[str],
) -> tuple[subprocess.CompletedProcess[str] | None, str | None]:
    """Run a short hardware query and return a useful error when it fails."""
    try:
        completed = subprocess.run(
            command,
            capture_output=True,
            check=False,
            text=True,
            timeout=10,
        )
    except subprocess.TimeoutExpired:
        return None, f"{command[0]} timed out after 10 seconds"
    except OSError as error:
        return None, f"could not execute {command[0]}: {error}"
    if completed.returncode:
        detail = completed.stderr.strip() or "no diagnostic was returned"
        return None, f"{command[0]} failed with exit code {completed.returncode}: {detail}"
    return completed, None


def query_nvidia_gpus(nvidia_smi: str) -> tuple[list[dict[str, str]], str | None]:
    """Return visible NVIDIA GPUs or a human-readable command error."""
    completed, error = run_command(
        [
            nvidia_smi,
            f"--query-gpu={NVIDIA_GPU_QUERY}",
            "--format=csv,noheader,nounits",
        ]
    )
    if error:
        return [], error
    assert completed is not None

    gpus: list[dict[str, str]] = []
    for row in csv.reader(line for line in completed.stdout.splitlines() if line.strip()):
        if len(row) != len(NVIDIA_GPU_FIELDS):
            return [], f"unexpected nvidia-smi output row: {row!r}"
        gpus.append(
            {
                field: value.strip()
                for field, value in zip(NVIDIA_GPU_FIELDS, row, strict=True)
            }
        )
    return gpus, None


def parse_json_output(output: str, command_name: str) -> Any:
    """Parse JSON even when a hardware tool prefixes diagnostic text."""
    text = output.strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        start = text.find("{")
        end = text.rfind("}")
        if start < 0 or end <= start:
            raise ValueError(f"{command_name} did not return JSON") from None
        try:
            return json.loads(text[start : end + 1])
        except json.JSONDecodeError as error:
            raise ValueError(f"{command_name} returned invalid JSON: {error}") from error


def memory_mib(value: Any) -> str | None:
    """Normalize a numeric hardware-tool memory value to integer MiB."""
    if value is None:
        return None
    try:
        return str(int(float(str(value).replace(",", "").strip())))
    except (TypeError, ValueError):
        return None


def query_intel_gpus(xpu_smi: str) -> tuple[list[dict[str, str]], str | None]:
    """Return visible Intel XPUs using xpu-smi's machine-readable telemetry."""
    command = [xpu_smi, *INTEL_XPU_COMMAND[1:]]
    completed, error = run_command(command)
    if error:
        return [], error
    assert completed is not None

    try:
        payload = parse_json_output(completed.stdout, xpu_smi)
    except ValueError as error:
        return [], str(error)
    rows = payload if isinstance(payload, list) else [payload]
    gpus: list[dict[str, str]] = []
    for row in rows:
        if not isinstance(row, dict):
            return [], f"unexpected {xpu_smi} JSON value: {row!r}"
        metrics = row.get("metrics")
        if not isinstance(metrics, dict):
            return [], f"{xpu_smi} JSON did not contain metrics: {row!r}"
        total = memory_mib(metrics.get("memory.total"))
        if total is None:
            return [], f"{xpu_smi} did not report memory.total: {row!r}"
        device = str(row.get("device", len(gpus)))
        gpu = {
            "index": device,
            "name": str(row.get("name", f"Intel XPU {device}")),
            "memory_total_mib": total,
            "driver_version": str(row.get("driver_version", "unknown")),
        }
        used = memory_mib(metrics.get("memory.used"))
        if used is not None:
            gpu["memory_used_mib"] = used
        gpus.append(gpu)
    return gpus, None


def check_requirements(
    report: dict[str, Any], minimum_gpus: int, minimum_memory_mib: int
) -> None:
    """Apply minimum device and memory requirements to a preflight report."""
    gpus = report["gpus"]
    if len(gpus) < minimum_gpus:
        report["error"] = f"found {len(gpus)} visible GPU(s), need at least {minimum_gpus}"
        return

    if minimum_memory_mib:
        low_memory = [
            gpu["index"]
            for gpu in gpus
            if int(gpu["memory_total_mib"]) < minimum_memory_mib
        ]
        if low_memory:
            report["error"] = (
                f"GPU(s) {', '.join(low_memory)} have less than "
                f"{minimum_memory_mib} MiB of memory"
            )
            return
    report["available"] = True


def build_report(
    minimum_gpus: int,
    minimum_memory_mib: int,
    backend: str = "auto",
) -> dict[str, Any]:
    """Build a JSON-serializable NVIDIA or Intel XPU preflight report."""
    nvidia_smi = shutil.which("nvidia-smi")
    xpu_smi = shutil.which("xpu-smi")
    report: dict[str, Any] = {
        "available": False,
        "backend": None,
        "minimum_gpus": minimum_gpus,
        "minimum_memory_mib": minimum_memory_mib,
        "nvidia_smi": nvidia_smi,
        "xpu_smi": xpu_smi,
        "gpus": [],
    }
    candidates = {
        "nvidia": (nvidia_smi, query_nvidia_gpus),
        "intel": (xpu_smi, query_intel_gpus),
    }
    selected = ("nvidia", "intel") if backend == "auto" else (backend,)
    errors: list[str] = []
    for candidate in selected:
        executable, query = candidates[candidate]
        if executable is None:
            errors.append(
                "nvidia-smi was not found on PATH"
                if candidate == "nvidia"
                else "xpu-smi was not found on PATH"
            )
            continue
        gpus, error = query(executable)
        if error:
            errors.append(error)
            continue
        report["backend"] = "nvidia-cuda" if candidate == "nvidia" else "intel-xpu"
        report["gpus"] = gpus
        check_requirements(report, minimum_gpus, minimum_memory_mib)
        if report["available"]:
            return report
        errors.append(str(report.get("error", "GPU requirements were not met")))

    report["error"] = "; ".join(errors) or "no supported GPU runtime was found"
    return report


def parse_args() -> argparse.Namespace:
    """Parse command-line arguments."""
    parser = argparse.ArgumentParser(
        description="Check NVIDIA or Intel XPU availability for local TEI services."
    )
    parser.add_argument(
        "--backend",
        choices=("auto", "nvidia", "intel"),
        default="auto",
        help="Hardware backend to check (default: auto).",
    )
    parser.add_argument(
        "--min-gpus",
        type=int,
        default=1,
        help="Minimum number of visible GPUs required (default: 1).",
    )
    parser.add_argument(
        "--min-memory-mib",
        type=int,
        default=0,
        help="Minimum total memory required per GPU (default: disabled).",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="Print the complete machine-readable report.",
    )
    return parser.parse_args()


def main() -> int:
    """Run the preflight and return a shell-friendly status."""
    args = parse_args()
    if args.min_gpus < 1:
        raise SystemExit("--min-gpus must be at least 1")
    if args.min_memory_mib < 0:
        raise SystemExit("--min-memory-mib cannot be negative")

    report = build_report(args.min_gpus, args.min_memory_mib, args.backend)
    if args.json:
        print(json.dumps(report, indent=2))
    else:
        status = "PASS" if report["available"] else "FAIL"
        backend = report.get("backend") or "none"
        print(f"GPU preflight: {status} ({backend})")
        for gpu in report["gpus"]:
            used = (
                f", {gpu['memory_used_mib']} MiB used"
                if "memory_used_mib" in gpu
                else ""
            )
            print(
                f"  GPU {gpu['index']}: {gpu['name']} "
                f"({gpu['memory_total_mib']} MiB{used}, driver {gpu['driver_version']})"
            )
        if report.get("error"):
            print(f"  {report['error']}", file=sys.stderr)
    return 0 if report["available"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
