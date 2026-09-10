from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

ROOT = Path(__file__).parents[2]
LAUNCHER = ROOT / "docker/scripts/unstract_durable_replay.py"
UNIT = ROOT / "docker/systemd/unstract-durable-replay.service"
DROP_IN = ROOT / "docker/systemd/podman-restart.service.d/60-unstract-durable-replay.conf"
SPEC = importlib.util.spec_from_file_location("unstract_durable_replay", LAUNCHER)
assert SPEC and SPEC.loader
launcher = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(launcher)


def launcher_environment(tmp_path: Path) -> dict[str, str]:
    paths = {
        "UNSTRACT_GUARD": tmp_path / "source/docker/scripts/train_health_deployment_guard.py",
        "UNSTRACT_STATE_DIR": tmp_path / "state",
        "UNSTRACT_PROJECT_DIR": tmp_path / "project",
        "UNSTRACT_LIVE_ENV_FILE": tmp_path / "project/docker/.env",
        "UNSTRACT_BASELINE": tmp_path / "state/baseline.json",
        "UNSTRACT_CANDIDATE_SOURCE": tmp_path / "source",
        "UNSTRACT_CANDIDATE_LOCK": tmp_path / "state/candidate-lock.json",
        "UNSTRACT_PROBE_SOURCE": tmp_path / "source/docker/healthchecks/unstract-services.sh",
        "UNSTRACT_COMPOSE_BASE": tmp_path / "project/docker/docker-compose.yaml",
        "UNSTRACT_COMPOSE_TRAIN": tmp_path / "project/docker/compose.train.yaml",
        "UNSTRACT_COMPOSE_WORKER_HEALTHCHECKS": tmp_path / "source/docker/compose.train.worker-healthchecks.yaml",
        "UNSTRACT_COMPOSE_CORE_HEALTHCHECKS": tmp_path / "source/docker/compose.train.healthchecks.yaml",
        "UNSTRACT_COMPOSE_ENV_DIR": tmp_path / "project/docker",
    }
    return {name: str(path) for name, path in paths.items()} | {
        "UNSTRACT_START_CONFIRM": launcher.CONFIRM_TOKEN,
        "UNSTRACT_OPERATION_TIMEOUT": "2400",
    }


def test_launcher_builds_guarded_start_with_all_persistent_inputs(tmp_path: Path) -> None:
    environment = launcher_environment(tmp_path)
    args = launcher.build_start_args(environment)

    assert args[:3] == [
        launcher.PYTHON,
        str(tmp_path / "source/docker/scripts/train_health_deployment_guard.py"),
        "start",
    ]
    assert args[-4:] == [
        "--operation-timeout",
        "2400",
        "--confirm",
        launcher.CONFIRM_TOKEN,
    ]
    assert args.count("--compose-file") == 4
    for value in environment.values():
        if value.startswith("/"):
            if value != environment["UNSTRACT_COMPOSE_ENV_DIR"]:
                assert value in args
    assert launcher.compose_environment(environment)["PWD"] == environment[
        "UNSTRACT_COMPOSE_ENV_DIR"
    ]


@pytest.mark.parametrize(
    ("name", "value", "message"),
    [
        ("UNSTRACT_GUARD", "relative/guard.py", "absolute path"),
        ("UNSTRACT_COMPOSE_BASE", "", "UNSTRACT_COMPOSE_BASE is required"),
        ("UNSTRACT_START_CONFIRM", "APPLY_UNSTRACT_HEALTH", "does not authorize"),
        ("UNSTRACT_OPERATION_TIMEOUT", "0", "positive integer"),
    ],
)
def test_launcher_fails_closed_on_invalid_environment(
    tmp_path: Path, name: str, value: str, message: str
) -> None:
    environment = launcher_environment(tmp_path)
    environment[name] = value

    with pytest.raises(ValueError, match=message):
        launcher.build_start_args(environment)


def test_systemd_owner_orders_guard_before_generic_restart() -> None:
    unit = UNIT.read_text(encoding="utf-8")
    drop_in = DROP_IN.read_text(encoding="utf-8")

    assert "Requires=train-rootless-boot-recovery.service" in unit
    assert "Before=podman-restart.service" in unit
    assert "ExecStart=/usr/bin/python3 %h/.local/libexec/unstract-durable-replay.py" in unit
    assert "PartOf=podman-restart.service" in unit
    assert "WantedBy=default.target" in unit
    assert "ConditionPathExists" not in unit
    assert "Requires=unstract-durable-replay.service" in drop_in
    assert "After=unstract-durable-replay.service" in drop_in
