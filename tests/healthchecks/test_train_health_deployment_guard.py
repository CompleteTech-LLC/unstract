from __future__ import annotations

import importlib.util
from copy import deepcopy
from pathlib import Path

import pytest

GUARD_PATH = Path(__file__).parents[2] / "docker/scripts/train_health_deployment_guard.py"
SPEC = importlib.util.spec_from_file_location("train_health_deployment_guard", GUARD_PATH)
assert SPEC and SPEC.loader
guard = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(guard)


def candidate_fixture() -> tuple[dict, dict, dict, dict]:
    baseline = {"containers": []}
    config = {
        "networks": {"default": {"name": guard.EXPECTED_NETWORK}},
        "services": {},
    }
    authored = {"services": {}}
    lock = {"images": {}}
    start_periods = {
        "db": "30s",
        "redis": "15s",
        "minio": "60s",
        "reverse-proxy": "60s",
        "qdrant": "60s",
        "rabbitmq": "60s",
        "weaviate": "120s",
        "x2text-service": "120s",
        "platform-service": "120s",
        "backend": "180s",
        "frontend": "60s",
    }
    for service in guard.TARGET_SERVICES:
        name = f"unstract-{service}"
        baseline["containers"].append(
            {
                "compose": {"com.docker.compose.service": service},
                "name": name,
                "mounts": [],
                "env_hashes": {},
            }
        )
        healthcheck = {
            "test": guard._probe_test(service),
            "interval": "30s",
            "timeout": "10s" if service in guard.CORE_SERVICES else "5s",
            "start_period": start_periods.get(service, "30s"),
            "retries": 3,
        }
        volumes = (
            [
                {
                    "type": "bind",
                    "source": "/staged/unstract-services.sh",
                    "target": guard.PROBE_MOUNT_TARGET,
                    "read_only": True,
                }
            ]
            if service in guard.CORE_SERVICES
            else []
        )
        service_config = {
            "image": f"candidate/{service}",
            "container_name": name,
            "networks": {guard.EXPECTED_NETWORK: {}},
            "volumes": volumes,
            "environment": {},
            "healthcheck": healthcheck,
        }
        config["services"][service] = service_config
        authored["services"][service] = deepcopy(service_config)
        lock["images"][service] = {
            "reference": f"candidate/{service}",
            "id": f"id-{service}",
            "digest": f"sha256:{service}",
        }
    return config, baseline, lock, authored


def test_worker_curl_checks_do_not_require_probe_mount() -> None:
    config, baseline, lock, authored = candidate_fixture()

    guard.check_candidate_config(config, baseline, lock, authored)


def test_core_probe_checks_require_read_only_probe_mount() -> None:
    config, baseline, lock, authored = candidate_fixture()
    config["services"]["db"]["volumes"] = []

    with pytest.raises(guard.GuardError, match="trusted probe mount"):
        guard.check_candidate_config(config, baseline, lock, authored)
