#!/usr/bin/env python3
"""Check host NVIDIA visibility before launching GPU embedding services."""

from __future__ import annotations

import argparse
import csv
import json
import shutil
import subprocess
import sys
from typing import Any

GPU_QUERY = "index,name,memory.total,driver_version"
GPU_FIELDS = ("index", "name", "memory_total_mib", "driver_version")


def query_gpus(nvidia_smi: str) -> tuple[list[dict[str, str]], str | None]:
    """Return visible GPUs or a human-readable command error."""
    try:
        completed = subprocess.run(
            [
                nvidia_smi,
                f"--query-gpu={GPU_QUERY}",
                "--format=csv,noheader,nounits",
            ],
            capture_output=True,
            check=False,
            text=True,
        )
    except OSError as error:
        return [], f"could not execute {nvidia_smi}: {error}"

    if completed.returncode:
        detail = completed.stderr.strip() or "no diagnostic was returned"
        return [], f"{nvidia_smi} failed with exit code {completed.returncode}: {detail}"

    gpus: list[dict[str, str]] = []
    for row in csv.reader(line for line in completed.stdout.splitlines() if line.strip()):
        if len(row) != len(GPU_FIELDS):
            return [], f"unexpected {nvidia_smi} output row: {row!r}"
        gpus.append(
            {field: value.strip() for field, value in zip(GPU_FIELDS, row, strict=True)}
        )
    return gpus, None


def build_report(
    minimum_gpus: int,
    minimum_memory_mib: int,
) -> dict[str, Any]:
    """Build a JSON-serializable preflight report."""
    nvidia_smi = shutil.which("nvidia-smi")
    report: dict[str, Any] = {
        "available": False,
        "minimum_gpus": minimum_gpus,
        "minimum_memory_mib": minimum_memory_mib,
        "nvidia_smi": nvidia_smi,
        "gpus": [],
    }
    if nvidia_smi is None:
        report["error"] = "nvidia-smi was not found on PATH"
        return report

    gpus, error = query_gpus(nvidia_smi)
    report["gpus"] = gpus
    if error:
        report["error"] = error
        return report
    if len(gpus) < minimum_gpus:
        report["error"] = (
            f"found {len(gpus)} visible GPU(s), need at least {minimum_gpus}"
        )
        return report

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
            return report

    report["available"] = True
    return report


def parse_args() -> argparse.Namespace:
    """Parse command-line arguments."""
    parser = argparse.ArgumentParser(
        description="Check NVIDIA GPU availability for local TEI services."
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
    """Run the GPU preflight and return a shell-friendly status."""
    args = parse_args()
    if args.min_gpus < 1:
        raise SystemExit("--min-gpus must be at least 1")
    if args.min_memory_mib < 0:
        raise SystemExit("--min-memory-mib cannot be negative")

    report = build_report(args.min_gpus, args.min_memory_mib)
    if args.json:
        print(json.dumps(report, indent=2))
    else:
        status = "PASS" if report["available"] else "FAIL"
        print(f"NVIDIA GPU preflight: {status}")
        for gpu in report["gpus"]:
            print(
                f"  GPU {gpu['index']}: {gpu['name']} "
                f"({gpu['memory_total_mib']} MiB, driver {gpu['driver_version']})"
            )
        if report.get("error"):
            print(f"  {report['error']}", file=sys.stderr)
    return 0 if report["available"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
