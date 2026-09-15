"""Ensure the guard's artifact inputs survive a clean Git checkout."""

from __future__ import annotations

import io
import subprocess
import tarfile
from pathlib import Path

ROOT = Path(__file__).parents[2]
REQUIRED_ARTIFACTS = (
    "docker/healthchecks/unstract-services.sh",
    "docker/healthchecks/http-readiness.sh",
    "docker/healthchecks/postgres-readiness.sh",
    "docker/docker-compose-dev-essentials.yaml",
    "docker/compose.train.healthchecks.yaml",
    "docker/compose.train.worker-healthchecks.yaml",
)


def test_guard_artifacts_are_tracked_and_present_in_clean_git_archive(
    tmp_path: Path,
) -> None:
    tracked = subprocess.run(
        ["git", "ls-files", "--error-unmatch", "--", *REQUIRED_ARTIFACTS],
        cwd=ROOT,
        check=False,
        capture_output=True,
        text=True,
    )
    assert tracked.returncode == 0, tracked.stderr

    archive = subprocess.run(
        ["git", "archive", "--format=tar", "HEAD", "--", *REQUIRED_ARTIFACTS],
        cwd=ROOT,
        check=False,
        capture_output=True,
    )
    assert archive.returncode == 0, archive.stderr.decode(errors="replace")

    with tarfile.open(fileobj=io.BytesIO(archive.stdout), mode="r:") as tar:
        tar.extractall(tmp_path)

    missing = [
        path
        for path in REQUIRED_ARTIFACTS
        if not (tmp_path / path).is_file()
    ]
    assert not missing, f"clean Git archive omitted guarded artifacts: {missing}"
