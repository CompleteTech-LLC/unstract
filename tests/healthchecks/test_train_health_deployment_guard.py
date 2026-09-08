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


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("1m0s", 60_000_000_000),
        ("2m0s", 120_000_000_000),
        ("3m0s", 180_000_000_000),
        ("1.5s", 1_500_000_000),
        ("250ms", 250_000_000),
        ("1h2m3.25s", 3_723_250_000_000),
        (30_000_000_000, 30_000_000_000),
    ],
)
def test_duration_parser_accepts_full_compose_values(value, expected) -> None:
    assert guard.duration_ns(value) == expected


@pytest.mark.parametrize(
    "value",
    ["", "1mtrailing", "1e3s", "-1s", "nan", "inf", -1, float("nan")],
)
def test_duration_parser_rejects_invalid_or_nonfinite_values(value) -> None:
    assert guard.duration_ns(value) is None


def test_core_probe_checks_require_read_only_probe_mount() -> None:
    config, baseline, lock, authored = candidate_fixture()
    config["services"]["db"]["volumes"] = []

    with pytest.raises(guard.GuardError, match="trusted probe mount"):
        guard.check_candidate_config(config, baseline, lock, authored)


def test_bind_mount_source_accepts_lexical_parent_segment() -> None:
    config, baseline, lock, authored = candidate_fixture()
    source = "/home/completetrain/etl.home.complete.tech/unstract/tool-registry"
    db_container = next(
        container
        for container in baseline["containers"]
        if container["compose"]["com.docker.compose.service"] == "db"
    )
    db_container["mounts"] = [
        {
            "type": "bind",
            "source": source,
            "destination": "/data/tool_registry_config",
            "rw": True,
            "options": [],
        }
    ]
    trusted_probe = {
        "type": "bind",
        "source": "/staged/unstract-services.sh",
        "target": guard.PROBE_MOUNT_TARGET,
        "read_only": True,
    }
    config["services"]["db"]["volumes"] = [
        trusted_probe,
        {
            "type": "bind",
            "source": "/home/completetrain/etl.home.complete.tech/docker/../unstract/tool-registry",
            "target": "/data/tool_registry_config",
            "read_only": False,
        }
    ]
    authored["services"]["db"]["volumes"] = config["services"]["db"]["volumes"]

    guard.check_candidate_config(config, baseline, lock, authored)


def test_bind_mount_source_rejects_distinct_path() -> None:
    config, baseline, lock, authored = candidate_fixture()
    db_container = next(
        container
        for container in baseline["containers"]
        if container["compose"]["com.docker.compose.service"] == "db"
    )
    db_container["mounts"] = [
        {
            "type": "bind",
            "source": "/home/completetrain/etl.home.complete.tech/unstract/tool-registry",
            "destination": "/data/tool_registry_config",
            "rw": True,
            "options": [],
        }
    ]
    trusted_probe = {
        "type": "bind",
        "source": "/staged/unstract-services.sh",
        "target": guard.PROBE_MOUNT_TARGET,
        "read_only": True,
    }
    config["services"]["db"]["volumes"] = [
        trusted_probe,
        {
            "type": "bind",
            "source": "/home/completetrain/etl.home.complete.tech/other/tool-registry",
            "target": "/data/tool_registry_config",
            "read_only": False,
        }
    ]
    authored["services"]["db"]["volumes"] = config["services"]["db"]["volumes"]

    with pytest.raises(guard.GuardError, match="mount source"):
        guard.check_candidate_config(config, baseline, lock, authored)


def test_named_volume_alias_resolves_to_live_name() -> None:
    config, baseline, lock, authored = candidate_fixture()
    volume_name = "unstract-etl-home-complete-tech_prompt_studio_data"
    executor = next(
        container
        for container in baseline["containers"]
        if container["compose"]["com.docker.compose.service"]
        == "worker-pg-executor"
    )
    executor["mounts"] = [
        {
            "type": "volume",
            "name": volume_name,
            "source": f"/var/lib/containers/storage/volumes/{volume_name}/_data",
            "destination": "/app/prompt-studio-data",
            "rw": True,
            "options": [],
        }
    ]
    config["volumes"] = {"prompt_studio_data": {"name": volume_name}}
    config["services"]["worker-pg-executor"]["volumes"] = [
        {
            "type": "volume",
            "source": "prompt_studio_data",
            "target": "/app/prompt-studio-data",
            "read_only": False,
        }
    ]
    authored["services"]["worker-pg-executor"]["volumes"] = config["services"][
        "worker-pg-executor"
    ]["volumes"]

    guard.check_candidate_config(config, baseline, lock, authored)


def test_named_volume_alias_rejects_wrong_live_name() -> None:
    config, baseline, lock, authored = candidate_fixture()
    volume_name = "unstract-etl-home-complete-tech_prompt_studio_data"
    executor = next(
        container
        for container in baseline["containers"]
        if container["compose"]["com.docker.compose.service"]
        == "worker-pg-executor"
    )
    executor["mounts"] = [
        {
            "type": "volume",
            "name": volume_name,
            "source": f"/var/lib/containers/storage/volumes/{volume_name}/_data",
            "destination": "/app/prompt-studio-data",
            "rw": True,
            "options": [],
        }
    ]
    config["volumes"] = {
        "prompt_studio_data": {"name": "other_prompt_studio_data"}
    }
    config["services"]["worker-pg-executor"]["volumes"] = [
        {
            "type": "volume",
            "source": "prompt_studio_data",
            "target": "/app/prompt-studio-data",
            "read_only": False,
        }
    ]
    authored["services"]["worker-pg-executor"]["volumes"] = config["services"][
        "worker-pg-executor"
    ]["volumes"]

    with pytest.raises(guard.GuardError, match="mount source"):
        guard.check_candidate_config(config, baseline, lock, authored)
