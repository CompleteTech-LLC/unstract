from __future__ import annotations

import contextlib
import importlib.util
import http.server
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import threading
from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import parse_qs, urlsplit

import pytest
import yaml

GUARD_PATH = Path(__file__).parents[2] / "docker/scripts/train_health_deployment_guard.py"
SPEC = importlib.util.spec_from_file_location("train_health_deployment_guard", GUARD_PATH)
assert SPEC and SPEC.loader
guard = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(guard)


def _real_compose_provider() -> list[str]:
    """Return a locally installed Docker/Podman Compose provider for config-only tests."""
    configured = os.environ.get("UNSTRACT_COMPOSE_PROVIDER")
    if configured:
        provider = shlex.split(configured)
        if provider and shutil.which(provider[0]):
            return provider
        pytest.fail("UNSTRACT_COMPOSE_PROVIDER is not executable")
    for provider in (
        ("docker", "compose"),
        ("podman", "compose"),
        ("docker-compose",),
    ):
        if shutil.which(provider[0]):
            return list(provider)
    pytest.skip("Docker or Podman Compose provider is unavailable")


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


def test_healthcheck_none_is_equivalent_to_no_healthcheck() -> None:
    assert guard.health_config(None) == {"configured": False}
    assert guard.health_config({"Test": ["NONE"]}) == {"configured": False}
    assert guard.health_config({"Test": ["CMD", "true"]})["configured"] is True


def test_generated_hostname_is_excluded_but_custom_hostname_is_retained() -> None:
    container_id = "abcdef0123456789"
    generated = guard.env_hashes(
        ["HOSTNAME=abcdef012345", "APP_MODE=prod"],
        container_id=container_id,
        config_hostname="abcdef012345",
    )
    custom = guard.env_hashes(
        ["HOSTNAME=worker-custom", "APP_MODE=prod"],
        container_id=container_id,
        config_hostname="abcdef012345",
    )

    assert "HOSTNAME" not in generated
    assert "HOSTNAME" in custom


def test_hostname_normalization_requires_the_current_container_id() -> None:
    old_id = "abcdef0123456789"
    new_id = "fedcba9876543210"

    old = guard.env_hashes(
        ["HOSTNAME=abcdef012345"],
        container_id=old_id,
        config_hostname="abcdef012345",
    )
    new = guard.env_hashes(
        ["HOSTNAME=fedcba987654"],
        container_id=new_id,
        config_hostname="fedcba987654",
    )
    fixed_old_value = guard.env_hashes(
        ["HOSTNAME=abcdef012345"],
        container_id=new_id,
        config_hostname="fedcba987654",
    )
    mismatched_config_hostname = guard.env_hashes(
        ["HOSTNAME=fedcba987654"],
        container_id=new_id,
        config_hostname="source-fixed",
    )

    assert old == new == {}
    assert "HOSTNAME" in fixed_old_value
    assert "HOSTNAME" in mismatched_config_hostname


def test_runtime_environment_rejects_duplicate_keys() -> None:
    with pytest.raises(guard.GuardError, match="duplicate key: APP_MODE"):
        guard.env_hashes(["APP_MODE=first", "APP_MODE=second"])
    with pytest.raises(guard.GuardError, match="duplicate key: HOSTNAME"):
        guard.env_hashes(
            ["HOSTNAME=abcdef012345", "HOSTNAME=abcdef012345"],
            container_id="abcdef0123456789",
            config_hostname="abcdef012345",
        )
    with pytest.raises(guard.GuardError, match="duplicate key: APP_MODE"):
        guard.environment_values(["APP_MODE=first", "APP_MODE=second"])
    with pytest.raises(guard.GuardError, match="duplicate key: APP_MODE"):
        guard.compose_environment(["APP_MODE=first", "APP_MODE=second"])
    with pytest.raises(guard.GuardError, match="non-string or empty key"):
        guard.compose_environment({1: "first", "1": "second"})


@pytest.mark.parametrize("key", ["HOME", "container"])
def test_runtime_environment_plan_preserves_every_non_hostname_key(key: str) -> None:
    baseline = {
        "containers": [
            {
                "compose": {"com.docker.compose.service": "runner"},
                "name": "unstract-runner",
                "env_hashes": guard.environment_hashes({key: "baseline"}),
            }
        ]
    }
    runtime_environment = {"runner": {"id": "runner-id", "values": {key: "baseline"}}}
    candidate_images = {"runner": {"environment": {key: "candidate"}}}

    overrides, reviewed = guard.plan_runtime_environment_override(
        baseline,
        runtime_environment,
        candidate_images,
        {"services": {"runner": {}}},
        services=("runner",),
    )

    assert overrides == {"runner": {key: "baseline"}}
    assert reviewed == {"runner": {key}}


def test_runtime_environment_plan_rejects_hostname_without_both_generated_fields() -> None:
    container_id = "abcdef0123456789"
    baseline = {
        "containers": [
            {
                "compose": {"com.docker.compose.service": "runner"},
                "name": "unstract-runner",
                "env_hashes": guard.environment_hashes(
                    {"HOSTNAME": "abcdef012345"},
                    container_id=container_id,
                    config_hostname="abcdef012345",
                ),
            }
        ]
    }
    runtime_environment = {
        "runner": {
            "id": container_id,
            "hostname": "source-fixed",
            "values": {"HOSTNAME": "abcdef012345"},
        }
    }

    with pytest.raises(guard.GuardError, match="fresh runtime environment changed"):
        guard.plan_runtime_environment_override(
            baseline,
            runtime_environment,
            {"runner": {"environment": {}}},
            {"services": {"runner": {}}},
            services=("runner",),
        )


def test_candidate_environment_keeps_explicit_hostname_override() -> None:
    config = {"services": {"runner": {"environment": {"HOSTNAME": "source-fixed"}}}}

    values = guard.candidate_environment_values(config, "runner", {})

    assert values == {"HOSTNAME": "source-fixed"}

    fixed_hostname = guard.candidate_environment_values(
        {"services": {"runner": {"hostname": "fixed-source-hostname"}}},
        "runner",
        {},
    )

    assert fixed_hostname == {"HOSTNAME": "fixed-source-hostname"}


def test_generated_network_alias_is_excluded_but_explicit_alias_is_retained() -> None:
    container_id = "abcdef0123456789"
    network = {
        "unstract-network": {
            "Aliases": [
                "abcdef012345",
                "abcdef012345-explicit",
                "unstract-runner",
            ],
            "NetworkID": "network-id",
            "DriverOpts": {},
        }
    }

    normalized = guard.normalize_networks(network, container_id=container_id)

    assert normalized["unstract-network"]["aliases"] == [
        "abcdef012345-explicit",
        "unstract-runner",
    ]


def test_exception_reason_is_bounded_and_redacts_secret_assignments() -> None:
    reason = guard.exception_reason(
        RuntimeError("token=private-value password: another-private-value " + "x" * 500)
    )

    assert "private-value" not in reason
    assert "another-private-value" not in reason
    assert "<redacted>" in reason
    assert len(reason) < 260


def test_runtime_environment_plan_preserves_image_drift_and_missing_baseline_keys() -> None:
    baseline_values = {
        "qdrant": {"PATH": "/usr/local/bin", "QDRANT_DB": "baseline-db"},
        "runner": {"PATH": "/usr/local/bin", "UNSTRACT_APPS_VERSION": "old"},
    }
    baseline = {
        "containers": [
            {
                "compose": {"com.docker.compose.service": service},
                "name": f"unstract-{service}",
                "env_hashes": guard.environment_hashes(values),
            }
            for service, values in baseline_values.items()
        ]
    }
    runtime_environment = {
        service: {"id": f"{service}-id", "values": values}
        for service, values in baseline_values.items()
    }
    candidate_images = {
        "qdrant": {"environment": {"PATH": "/usr/local/bin"}},
        "runner": {
            "environment": {"PATH": "/usr/local/bin", "UNSTRACT_APPS_VERSION": "new"}
        },
    }
    config = {"services": {"qdrant": {}, "runner": {}}}

    overrides, reviewed = guard.plan_runtime_environment_override(
        baseline,
        runtime_environment,
        candidate_images,
        config,
        services=("qdrant", "runner"),
    )

    assert overrides == {
        "qdrant": {"QDRANT_DB": "baseline-db"},
        "runner": {"UNSTRACT_APPS_VERSION": "old"},
    }
    assert reviewed == {
        "qdrant": {"QDRANT_DB"},
        "runner": {"UNSTRACT_APPS_VERSION"},
    }


def test_runtime_environment_plan_rejects_unreviewed_image_default() -> None:
    baseline = {
        "containers": [
            {
                "compose": {"com.docker.compose.service": "runner"},
                "name": "unstract-runner",
                "env_hashes": guard.environment_hashes({"PATH": "/usr/local/bin"}),
            }
        ]
    }
    runtime_environment = {
        "runner": {"id": "runner-id", "values": {"PATH": "/usr/local/bin"}}
    }
    candidate_images = {
        "runner": {
            "environment": {"PATH": "/usr/local/bin", "UNREVIEWED_DEFAULT": "changed"}
        }
    }

    with pytest.raises(guard.GuardError, match="added environment"):
        guard.plan_runtime_environment_override(
            baseline,
            runtime_environment,
            candidate_images,
            {"services": {"runner": {}}},
            services=("runner",),
        )


def test_reviewed_environment_override_is_allowed_by_compose_identity_guard() -> None:
    config, baseline, lock, authored = candidate_fixture()
    config["services"]["db"]["environment"]["BASELINE_ONLY"] = "preserved"

    guard.check_candidate_config(
        config,
        baseline,
        lock,
        authored,
        reviewed_environment_keys={"db": {"BASELINE_ONLY"}},
    )


def test_runtime_environment_override_is_private(tmp_path: Path) -> None:
    path = tmp_path / "runtime-environment.override.yaml"

    guard.write_runtime_environment_override(
        {"qdrant": {"QDRANT_DB": "baseline-$DB-${DB_NAME}"}}, path
    )
    guard.validate_private_override(path)

    assert path.stat().st_mode & 0o777 == 0o600
    assert '"baseline-$$DB-$${DB_NAME}"' in path.read_text(encoding="utf-8")

    digest = guard.sha256_file(path)
    guard.validate_private_override(path, expected_sha256=digest)
    path.write_text(path.read_text(encoding="utf-8") + "# changed\n", encoding="utf-8")
    with pytest.raises(guard.GuardError, match="override changed"):
        guard.validate_private_override(path, expected_sha256=digest)


def test_durable_compose_inputs_are_private_and_reusable(tmp_path: Path) -> None:
    probe = tmp_path / "probe.sh"
    probe.write_text("#!/bin/sh\nprintf probe\n", encoding="utf-8")
    probe_sha256 = guard.sha256_file(probe)
    lock = {
        "images": {
            service: {"reference": f"candidate/{service}"}
            for service in guard.TARGET_SERVICES
        }
    }
    image_override = tmp_path / guard.CANDIDATE_IMAGE_FILENAME
    settings = tmp_path / guard.COMPOSE_SETTINGS_FILENAME

    guard.candidate_image_override(lock, image_override)
    guard.candidate_image_override(lock, image_override)
    guard.write_compose_settings(
        "goal09-test",
        probe,
        settings,
        probe_source_sha256=probe_sha256,
    )
    guard.write_compose_settings(
        "goal09-test",
        probe,
        settings,
        probe_source_sha256=probe_sha256,
    )

    assert image_override.stat().st_mode & 0o777 == 0o600
    assert settings.stat().st_mode & 0o777 == 0o600
    guard.validate_compose_settings(
        settings,
        candidate_version="goal09-test",
        probe_source=probe,
        probe_source_sha256=probe_sha256,
    )

    probe.write_text("#!/bin/sh\nprintf changed\n", encoding="utf-8")
    with pytest.raises(guard.GuardError, match="health probe source changed"):
        guard.validate_compose_settings(
            settings,
            candidate_version="goal09-test",
            probe_source=probe,
            probe_source_sha256=probe_sha256,
        )


def test_replay_manifest_binds_private_runtime_override_hash(tmp_path: Path) -> None:
    probe = tmp_path / "probe.sh"
    probe.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    probe_sha256 = guard.sha256_file(probe)
    lock = {
        "schema": "unstract-health-candidate/v1",
        "candidate_version": "goal09-test",
        "source_commit": "a" * 40,
        "source_tree": "b" * 40,
        "images": {
            service: {"reference": f"candidate/{service}"}
            for service in guard.TARGET_SERVICES
        },
    }
    lock_path = tmp_path / "candidate-lock.json"
    lock_path.write_text(json.dumps(lock), encoding="utf-8")
    image_override = guard.candidate_image_override(
        lock, tmp_path / guard.CANDIDATE_IMAGE_FILENAME
    )
    settings = tmp_path / guard.COMPOSE_SETTINGS_FILENAME
    guard.write_compose_settings(
        lock["candidate_version"],
        probe,
        settings,
        probe_source_sha256=probe_sha256,
    )
    runtime = tmp_path / guard.RUNTIME_ENVIRONMENT_FILENAME
    guard.write_runtime_environment_override({}, runtime)
    compose = tmp_path / "docker" / "compose.yaml"
    compose.parent.mkdir()
    compose.write_text("include:\n  - included.yaml\nservices: {}\n", encoding="utf-8")
    (compose.parent / "included.yaml").write_text("services: {}\n", encoding="utf-8")
    live_env = tmp_path / "docker" / ".env"
    live_env.write_text("COMPOSE_PROJECT_NAME=test\n", encoding="utf-8")
    snapshot = guard.create_compose_snapshot(
        state_dir=tmp_path / "state",
        project_dir=tmp_path,
        compose_files=("docker/compose.yaml", str(image_override), str(runtime)),
        live_env_file=live_env,
        probe_source=probe,
        probe_source_sha256=probe_sha256,
        image_override=image_override,
        image_override_sha256=guard.sha256_file(image_override),
        environment_override=runtime,
        environment_override_sha256=guard.sha256_file(runtime),
    )
    manifest = tmp_path / guard.REPLAY_MANIFEST_FILENAME

    guard.write_replay_manifest(
        manifest,
        lock_path=lock_path,
        lock=lock,
        image_override=image_override,
        settings_file=settings,
        runtime_environment_override=runtime,
        probe_source=probe,
        probe_source_sha256=probe_sha256,
        snapshot=snapshot,
    )
    loaded = guard.load_replay_manifest(
        manifest,
        lock_path=lock_path,
        lock=lock,
        image_override=image_override,
        settings_file=settings,
        runtime_environment_override=runtime,
        probe_source=probe,
        probe_source_sha256=probe_sha256,
        state_dir=tmp_path / "state",
    )
    assert loaded["runtime_environment_sha256"] == guard.sha256_file(runtime)
    assert loaded["compose_snapshot"].probe_path.read_bytes() == probe.read_bytes()
    assert manifest.stat().st_mode & 0o777 == 0o600

    runtime.write_text(runtime.read_text(encoding="utf-8") + "# changed\n", encoding="utf-8")
    with pytest.raises(guard.GuardError, match="runtime environment override changed"):
        guard.load_replay_manifest(
            manifest,
            lock_path=lock_path,
            lock=lock,
            image_override=image_override,
            settings_file=settings,
            runtime_environment_override=runtime,
            probe_source=probe,
            probe_source_sha256=probe_sha256,
            state_dir=tmp_path / "state",
        )


def test_compose_replay_consumes_durable_settings_and_overrides(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    probe = tmp_path / "probe.sh"
    probe.write_text("#!/bin/sh\nprintf probe\n", encoding="utf-8")
    probe_sha256 = guard.sha256_file(probe)
    lock = {
        "images": {
            service: {"reference": f"candidate/{service}"}
            for service in guard.TARGET_SERVICES
        }
    }
    image_override = guard.candidate_image_override(
        lock, tmp_path / guard.CANDIDATE_IMAGE_FILENAME
    )
    settings = tmp_path / guard.COMPOSE_SETTINGS_FILENAME
    guard.write_compose_settings(
        "goal09-test",
        probe,
        settings,
        probe_source_sha256=probe_sha256,
    )
    settings_sha256 = guard.sha256_file(settings)
    environment_override = tmp_path / guard.RUNTIME_ENVIRONMENT_FILENAME
    guard.write_runtime_environment_override(
        {"runner": {"APP_MODE": "test"}}, environment_override
    )
    live_env = tmp_path / "docker" / ".env"
    live_env.parent.mkdir()
    live_env.write_text(
        "TOOL_REGISTRY_CONFIG_SRC_PATH=/srv/tool-registry\nCOMPOSE_PROJECT_NAME=test\n",
        encoding="utf-8",
    )
    compose = tmp_path / "compose.yaml"
    compose.write_text("include:\n  - included.yaml\nservices: {}\n", encoding="utf-8")
    (tmp_path / "included.yaml").write_text("services: {}\n", encoding="utf-8")
    snapshot = guard.create_compose_snapshot(
        state_dir=tmp_path / "state",
        project_dir=tmp_path,
        compose_files=("compose.yaml", str(image_override), str(environment_override)),
        live_env_file=live_env,
        probe_source=probe,
        probe_source_sha256=probe_sha256,
        image_override=image_override,
        image_override_sha256=guard.sha256_file(image_override),
        environment_override=environment_override,
        environment_override_sha256=guard.sha256_file(environment_override),
    )
    calls: list[tuple[list[str], dict[str, str] | None]] = []

    def fake_run(
        args: list[str],
        *,
        cwd: Path | None = None,
        env: dict[str, str] | None = None,
        **kwargs: object,
    ) -> subprocess.CompletedProcess[str]:
        calls.append((args, env))
        if "config" in args:
            bound_files = {
                args[index + 1]
                for index, argument in enumerate(args[:-1])
                if argument == "-f"
            }
            bound_files.add(args[args.index("--env-file") + 1])
            assert len(bound_files) == 4
            assert all(path.startswith(str(snapshot.root)) for path in bound_files)
            assert all(Path(path).is_file() for path in bound_files)
            assert str(live_env) not in args
            assert str(compose) not in args
            assert str(image_override) not in args
            assert str(environment_override) not in args
            assert env is not None
            bound_probe = env["UNSTRACT_HEALTHCHECK_SOURCE"]
            assert bound_probe == str(snapshot.probe_path)
            bound_paths = {Path(value) for value in bound_files | {bound_probe}}
            assert len(bound_paths) == 5
            originals = {
                compose: compose.read_bytes(),
                live_env: live_env.read_bytes(),
                probe: probe.read_bytes(),
                image_override: image_override.read_bytes(),
                environment_override: environment_override.read_bytes(),
            }
            for source in originals:
                source.write_bytes(b"tampered after final verification\n")
            try:
                for source, original in originals.items():
                    assert any(path.read_bytes() == original for path in bound_paths), source
            finally:
                for source, original in originals.items():
                    source.write_bytes(original)
            env_file = Path(args[args.index("--env-file") + 1])
            assert "TOOL_REGISTRY_CONFIG_SRC_PATH=/srv/tool-registry" in env_file.read_text(
                encoding="utf-8"
            )
            assert Path(bound_probe).read_bytes() == originals[probe]
            assert kwargs["pass_fds"] == ()
            return subprocess.CompletedProcess(args, 0, json.dumps({"services": {}}), "")
        return subprocess.CompletedProcess(args, 0, "", "")

    monkeypatch.setattr(guard, "run", fake_run)
    guard.compose_config(
        tmp_path,
        ("compose.yaml",),
        candidate_version="goal09-test",
        probe_source=probe,
        live_env_file=live_env,
        image_override=image_override,
        image_override_sha256=guard.sha256_file(image_override),
        environment_override=environment_override,
        environment_override_sha256=guard.sha256_file(environment_override),
        settings_file=settings,
        settings_file_sha256=guard.sha256_file(settings),
        probe_source_sha256=probe_sha256,
        snapshot=snapshot,
    )
    guard.targeted_up(
        tmp_path,
        ("compose.yaml",),
        ("runner",),
        candidate_version="goal09-test",
        probe_source=probe,
        live_env_file=live_env,
        image_override=image_override,
        image_override_sha256=guard.sha256_file(image_override),
        environment_override=environment_override,
        environment_override_sha256=guard.sha256_file(environment_override),
        settings_file=settings,
        settings_file_sha256=guard.sha256_file(settings),
        probe_source_sha256=probe_sha256,
        snapshot=snapshot,
    )

    assert len(calls) == 3
    config_args, config_env = calls[0]
    assert config_args[:3] == ["docker", "compose", "--project-directory"]
    assert str(snapshot.root) in config_args
    assert str(live_env) not in config_args
    assert str(settings) not in config_args
    assert str(image_override) not in config_args
    assert str(environment_override) not in config_args
    assert sum(argument == "-f" for argument in config_args) == 3
    assert config_env and config_env["VERSION"] == "goal09-test"
    assert config_env["UNSTRACT_HEALTHCHECK_SOURCE"] == str(snapshot.probe_path)
    assert calls[1][0][-3:] == ["config", "--format", "json"]
    assert calls[2][0][-1] == "runner"

    monkeypatch.setattr(guard, "verify_candidate_source_state", lambda *_: None)
    guard.compose_start(
        tmp_path,
        ("compose.yaml",),
        candidate_version="goal09-test",
        probe_source=probe,
        live_env_file=live_env,
        expected_source=None,
        candidate_source=tmp_path,
        candidate_lock=lock,
        image_override=image_override,
        image_override_sha256=guard.sha256_file(image_override),
        environment_override=environment_override,
        environment_override_sha256=guard.sha256_file(environment_override),
        settings_file=settings,
        settings_file_sha256=guard.sha256_file(settings),
        probe_source_sha256=probe_sha256,
        snapshot=snapshot,
    )
    assert calls[3][0][-3:] == ["config", "--format", "json"]
    assert calls[4][0][-5:] == ["up", "-d", "--no-build", "--pull", "never"]

    settings.write_text(settings.read_text(encoding="utf-8").replace("goal09-test", "tampered"), encoding="utf-8")
    with pytest.raises(guard.GuardError, match="Compose settings changed"):
        guard.targeted_up(
            tmp_path,
            ("compose.yaml",),
            ("runner",),
            candidate_version="goal09-test",
            probe_source=probe,
            live_env_file=live_env,
            settings_file=settings,
            settings_file_sha256=settings_sha256,
            probe_source_sha256=probe_sha256,
        )


def test_compose_snapshot_probe_is_visible_to_a_separate_process(tmp_path: Path) -> None:
    project = tmp_path / "project"
    (project / "docker").mkdir(parents=True)
    compose = project / "docker" / "docker-compose.yaml"
    compose.write_text("include:\n  - docker-compose-dev-essentials.yaml\nservices: {}\n", encoding="utf-8")
    (project / "docker" / "docker-compose-dev-essentials.yaml").write_text(
        "services: {}\n", encoding="utf-8"
    )
    env_file = project / "docker" / ".env"
    env_file.write_text("COMPOSE_PROJECT_NAME=snapshot-test\n", encoding="utf-8")
    probe = tmp_path / "source" / "unstract-services.sh"
    probe.parent.mkdir()
    probe.write_text("#!/bin/sh\nprintf immutable-probe\n", encoding="utf-8")
    os.chmod(probe, 0o755)
    snapshot = guard.create_compose_snapshot(
        state_dir=tmp_path / "state",
        project_dir=project,
        compose_files=("docker/docker-compose.yaml",),
        live_env_file=env_file,
        probe_source=probe,
        probe_source_sha256=guard.sha256_file(probe),
        image_override=None,
        image_override_sha256=None,
        environment_override=None,
        environment_override_sha256=None,
    )

    child = subprocess.run(
        [
            sys.executable,
            "-c",
            "import pathlib,sys; p=pathlib.Path(sys.argv[1]); "
            "assert not str(p).startswith('/proc/self/fd/'); print(p.read_text())",
            str(snapshot.probe_path),
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    assert child.stdout == "#!/bin/sh\nprintf immutable-probe\n\n"
    executed = subprocess.run(
        [str(snapshot.probe_path)],
        check=True,
        capture_output=True,
        text=True,
    )
    assert executed.stdout == "immutable-probe"
    assert snapshot.probe_path.stat().st_mode & 0o777 == 0o555
    assert (snapshot.root / "docker" / "docker-compose-dev-essentials.yaml").is_file()
    source_args = guard.compose_args(
        project,
        ("docker/docker-compose.yaml",),
        live_env_file=env_file,
    )
    assert source_args[:4] == [
        "docker",
        "compose",
        "--project-directory",
        str(project / "docker"),
    ]
    snapshot_args = guard.compose_args(
        project,
        ("docker/docker-compose.yaml",),
        live_env_file=env_file,
        snapshot=snapshot,
    )
    assert snapshot_args[:4] == [
        "docker",
        "compose",
        "--project-directory",
        str(snapshot.root / "docker"),
    ]
    assert snapshot_args[-2:] == ["-f", "docker/docker-compose.yaml"]
    assert (
        guard.load_compose_snapshot(snapshot.manifest_path).probe_path
        == snapshot.probe_path
    )


def test_compose_snapshot_rejects_tampered_retained_input(tmp_path: Path) -> None:
    compose = tmp_path / "compose.yaml"
    compose.write_text("services: {}\n", encoding="utf-8")
    probe = tmp_path / "probe.sh"
    probe.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    snapshot = guard.create_compose_snapshot(
        state_dir=tmp_path / "state",
        project_dir=tmp_path,
        compose_files=("compose.yaml",),
        live_env_file=None,
        probe_source=probe,
        probe_source_sha256=guard.sha256_file(probe),
        image_override=None,
        image_override_sha256=None,
        environment_override=None,
        environment_override_sha256=None,
    )
    os.chmod(snapshot.probe_path, 0o600)
    snapshot.probe_path.write_text("tampered\n", encoding="utf-8")
    with pytest.raises(guard.GuardError, match="writable or not regular|changed"):
        guard.load_compose_snapshot(snapshot.manifest_path)


def test_real_compose_provider_preserves_snapshot_runner_paths_and_env(
    tmp_path: Path,
) -> None:
    project = tmp_path / "project"
    (project / "docker").mkdir(parents=True)
    (project / "docker" / "docker-compose.yaml").write_text(
        "include:\n  - docker-compose-dev-essentials.yaml\nservices: {}\n",
        encoding="utf-8",
    )
    (project / "docker" / "docker-compose-dev-essentials.yaml").write_text(
        "services:\n  included:\n    image: busybox:latest\n", encoding="utf-8"
    )
    workflow_data = project / "docker" / "workflow_data"
    workflow_data.mkdir()
    (workflow_data / "marker").write_text("runtime-data\n", encoding="utf-8")
    tool_registry = project / "tool-registry"
    tool_registry.mkdir()
    (tool_registry / "marker").write_text("registry-data\n", encoding="utf-8")
    runner_env = project / "runner" / ".env"
    runner_env.parent.mkdir()
    runner_env.write_text("RUNNER_ENV_FILE=from-runner-env-file\n", encoding="utf-8")
    (project / "docker" / "docker-compose.yaml").write_text(
        "include:\n"
        "  - docker-compose-dev-essentials.yaml\n"
        "services:\n"
        "  runner:\n"
        "    image: busybox:latest\n"
        "    hostname: source-fixed-hostname\n"
        "    env_file:\n"
        "      - ../runner/.env\n"
        "    environment:\n"
        "      RUNNER_SOURCE_ENV: ${RUNNER_SOURCE_ENV}\n"
        "    volumes:\n"
        "      - ./workflow_data:/data\n"
        "      - ${TOOL_REGISTRY_CONFIG_SRC_PATH}:/data/tool_registry_config\n",
        encoding="utf-8",
    )
    env_file = project / "docker" / ".env"
    env_file.write_text(
        "COMPOSE_PROJECT_NAME=snapshot-provider-test\n"
        "RUNNER_SOURCE_ENV=from-source-env\n"
        f"TOOL_REGISTRY_CONFIG_SRC_PATH={tool_registry}\n",
        encoding="utf-8",
    )
    probe = tmp_path / "probe.sh"
    probe.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    snapshot = guard.create_compose_snapshot(
        state_dir=tmp_path / "state",
        project_dir=project,
        compose_files=("docker/docker-compose.yaml",),
        live_env_file=env_file,
        probe_source=probe,
        probe_source_sha256=guard.sha256_file(probe),
        image_override=None,
        image_override_sha256=None,
        environment_override=None,
        environment_override_sha256=None,
    )
    provider = _real_compose_provider()
    launch_args = guard.compose_args(
        project,
        ("docker/docker-compose.yaml",),
        live_env_file=env_file,
        snapshot=snapshot,
    )
    bound_args = [snapshot.path_for(argument) for argument in launch_args[2:]]
    assert bound_args[:2] == ["--project-directory", str(snapshot.root / "docker")]
    result = subprocess.run(
        [
            *provider,
            *bound_args,
            "config",
            *(["--format", "json"] if provider[0] == "docker" else []),
        ],
        check=False,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr
    rendered = (
        json.loads(result.stdout)
        if provider[0] == "docker"
        else yaml.safe_load(result.stdout)
    )
    assert "included" in rendered["services"]
    runner = rendered["services"]["runner"]
    assert runner["hostname"] == "source-fixed-hostname"
    assert runner["environment"]["RUNNER_SOURCE_ENV"] == "from-source-env"
    assert runner["environment"]["RUNNER_ENV_FILE"] == "from-runner-env-file"
    normalized = guard.normalize_snapshot_bind_sources(rendered, snapshot)
    mounts = {mount["target"]: mount for mount in normalized["services"]["runner"]["volumes"]}
    assert Path(mounts["/data"]["source"]).resolve() == workflow_data.resolve()
    assert Path(mounts["/data/tool_registry_config"]["source"]).resolve() == tool_registry.resolve()


@contextlib.contextmanager
def _recording_docker_api():
    """Record real Compose create requests without connecting to any daemon."""
    created: dict[str, dict] = {}
    creation_lock = threading.Lock()
    started: set[str] = set()
    unexpected: list[tuple[str, str]] = []
    image_id = "sha256:" + "1" * 64

    class Engine(http.server.BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, *_args):
            pass

        def do_HEAD(self):
            self.respond()

        def do_GET(self):
            self.respond()

        def do_POST(self):
            self.respond()

        def do_DELETE(self):
            self.respond()

        def respond(self):
            url = urlsplit(self.path)
            path = re.sub(r"^/v[0-9.]+", "", url.path)
            query = parse_qs(url.query)
            length = int(self.headers.get("Content-Length", "0"))
            body = json.loads(self.rfile.read(length)) if length else None
            code, headers = 200, {}
            if path == "/_ping":
                payload = b"OK"
                headers = {"API-Version": "1.47", "OSType": "linux"}
            elif path == "/version":
                payload = {"ApiVersion": "1.47", "Version": "27.0.0", "Os": "linux", "Arch": "amd64"}
            elif path.startswith("/images/") and path.endswith("/json"):
                payload = {
                    "Id": image_id,
                    "Architecture": "amd64",
                    "Os": "linux",
                    "Config": {"Env": ["IMAGE_FIXTURE=present"], "Labels": {}},
                    "RootFS": {"Type": "layers", "Layers": []},
                    "Size": 1,
                }
            elif path == "/containers/json":
                filters = json.loads(query.get("filters", ["{}"])[0])
                payload = []
                for identifier, record in created.copy().items():
                    labels = record["request"]["Labels"]
                    if any(
                        (labels.get(key) != value if separator else key not in labels)
                        for key, separator, value in (
                            expression.partition("=")
                            for expression in filters.get("label", [])
                        )
                    ):
                        continue
                    payload.append({
                        "Id": identifier,
                        "Names": ["/" + record["name"]],
                        "Image": record["request"]["Image"],
                        "ImageID": image_id,
                        "State": "running" if identifier in started else "created",
                        "Labels": labels,
                        "HostConfig": {"NetworkMode": "none"},
                        "NetworkSettings": {"Networks": {}},
                        "Mounts": [],
                    })
            elif path == "/containers/create" and self.command == "POST":
                with creation_lock:
                    identifier = f"{len(created) + 1:064x}"
                    created[identifier] = {"name": query["name"][0], "request": body}
                code, payload = 201, {"Id": identifier, "Warnings": []}
            elif path.startswith("/containers/") and path.endswith("/json"):
                identifier = path.split("/")[2]
                request = created[identifier]["request"]
                payload = {
                    "Id": identifier,
                    "Name": "/" + created[identifier]["name"],
                    "Image": image_id,
                    "Config": {key: value for key, value in request.items() if key not in ("HostConfig", "NetworkingConfig")},
                    "HostConfig": request.get("HostConfig", {}),
                    "State": {"Status": "running" if identifier in started else "created", "Running": identifier in started, "ExitCode": 0},
                    "Mounts": [],
                    "NetworkSettings": {"Networks": {}},
                }
            elif path.startswith("/containers/") and path.endswith("/start"):
                started.add(path.split("/")[2])
                code, payload = 204, None
            else:
                unexpected.append((self.command, path))
                code, payload = 404, {"message": "unexpected isolated fixture endpoint"}
            raw = payload if isinstance(payload, bytes) else json.dumps(payload).encode() if payload is not None else b""
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(raw)))
            for key, value in headers.items():
                self.send_header(key, value)
            self.end_headers()
            if self.command != "HEAD" and raw:
                self.wfile.write(raw)

    class RecordingServer(http.server.ThreadingHTTPServer):
        # Compose may open requests for every target together even when its
        # dependency traversal is serial. Keep the local fixture backlog large
        # enough for all 24 without dropping a creation request.
        request_queue_size = 128

    server = RecordingServer(("127.0.0.1", 0), Engine)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"tcp://127.0.0.1:{server.server_port}", created, unexpected
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


@pytest.mark.parametrize("action", ["targeted_up", "compose_start"])
def test_real_compose_creation_uses_stable_sources_for_all_24_targets(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, action: str
) -> None:
    provider = _real_compose_provider()
    project = tmp_path / "project-$literal"
    docker = project / "docker"
    data = docker / "workflow_data"
    data.mkdir(parents=True)
    registry = project / "tool-registry"
    registry.mkdir()
    socket = docker / "runtime.sock"
    socket.touch()
    bind_files = {
        "db": ("./scripts/db-setup/db_setup.sh", "/docker-entrypoint-initdb.d/db_setup.sh"),
        "reverse-proxy": ("./proxy_overrides.yaml", "/proxy_overrides.yaml"),
    }
    for relative, _ in bind_files.values():
        path = docker / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("fixture bind file\n")
    runner_env = project / "runner" / ".env"
    runner_env.parent.mkdir()
    runner_env.write_text("ENV_FILE_VALUE=from-frozen-env-file\n")

    def service_lines(service: str) -> list[str]:
        result = [
            f"  {service}:",
            f"    image: fixture/{service}:original",
            "    network_mode: none",
            "    command: [sleep, '600']",
            "    env_file:",
            "      - ../runner/.env",
            "    environment:",
            "      DOTENV_VALUE: ${SOURCE_VALUE}",
            "      PRIVATE_VALUE: source-value",
            "    volumes:",
            "      - ./workflow_data:/data:rw",
            "      - ./workflow_data:/literal-$$target:ro",
            "      - ${TOOL_REGISTRY_CONFIG_SRC_PATH}:/data/tool_registry_config:ro",
            "      - ${SOCKET_SOURCE}:/var/run/docker.sock:ro",
        ]
        if service in bind_files:
            relative, target = bind_files[service]
            result.append(f"      - {relative}:{target}:ro")
        return result

    main = ["include:", "  - docker-compose-dev-essentials.yaml", "services:"]
    included = ["services:"]
    for service in guard.TARGET_SERVICES:
        (included if service in guard.CORE_SERVICES else main).extend(service_lines(service))
    (docker / "docker-compose.yaml").write_text("\n".join(main) + "\n")
    (docker / "docker-compose-dev-essentials.yaml").write_text("\n".join(included) + "\n")
    (docker / "compose.train.yaml").write_text("services: {}\n")
    shutil.copy2(GUARD_PATH.parents[1] / "compose.train.healthchecks.yaml", docker / "health.yaml")
    env_file = docker / ".env"
    env_file.write_text(
        "COMPOSE_PROJECT_NAME=stable-bind-fixture\n"
        "SOURCE_VALUE=from-frozen-dotenv\n"
        "TOOL_REGISTRY_CONFIG_SRC_PATH=${PWD}/../tool-registry\n"
        "SOCKET_SOURCE=${PWD}/runtime.sock\n"
    )
    probe = tmp_path / "probe.sh"
    probe.write_text("#!/bin/sh\nexit 0\n")
    probe.chmod(0o755)
    lock = {"images": {service: {"reference": f"fixture/{service}:locked"} for service in guard.TARGET_SERVICES}}
    image_override = guard.candidate_image_override(lock, tmp_path / guard.CANDIDATE_IMAGE_FILENAME)
    private_value = "opaque-$value ${literal} # fixture\nsecond line"
    environment_override = tmp_path / guard.RUNTIME_ENVIRONMENT_FILENAME
    guard.write_runtime_environment_override(
        {service: {"PRIVATE_VALUE": private_value} for service in guard.TARGET_SERVICES},
        environment_override,
    )
    settings = tmp_path / guard.COMPOSE_SETTINGS_FILENAME
    guard.write_compose_settings("fixture", probe, settings, probe_source_sha256=guard.sha256_file(probe))
    docker_config = tmp_path / "docker-config"
    docker_config.mkdir()
    snapshots: set[Path] = set()
    launch_paths: list[Path] = []
    with _recording_docker_api() as (endpoint, created, unexpected):
        def provider_run(args: list[str], *, cwd: Path, env: dict[str, str], **kwargs) -> subprocess.CompletedProcess[str]:
            assert args[:2] == ["docker", "compose"]
            assert (cwd / "docker" / "workflow_data").is_symlink()
            snapshots.add(cwd)
            frozen_env = Path(args[args.index("--env-file") + 1])
            assert frozen_env.is_relative_to(cwd)
            assert frozen_env.read_bytes() == env_file.read_bytes()
            if "up" in args:
                override = next(
                    (Path(value) for value in args if value.endswith("bind-sources.override.json")),
                    None,
                )
                if override is not None:
                    assert override.stat().st_mode & 0o777 == 0o400
                    assert override.parent.stat().st_mode & 0o777 == 0o500
                    assert all(set(value) == {"volumes"} for value in json.loads(override.read_text())["services"].values())
                    launch_paths.append(override)
            # The actual provider can reach only the isolated recording API.
            # Its environment contains no ambient Docker context or secrets.
            isolated_env = {
                "PATH": os.defpath,
                "DOCKER_HOST": endpoint,
                "DOCKER_API_VERSION": "1.47",
                "DOCKER_CONFIG": str(docker_config),
                "COMPOSE_PARALLEL_LIMIT": "1",
                "COMPOSE_ANSI": "never",
                "COMPOSE_PROGRESS": "plain",
                "PWD": str(docker),
                "VERSION": env["VERSION"],
                "UNSTRACT_HEALTHCHECK_SOURCE": env["UNSTRACT_HEALTHCHECK_SOURCE"],
            }
            result = subprocess.run([*provider, "--parallel", "1", *args[2:]], cwd=cwd, env=isolated_env, text=True, capture_output=True, timeout=45)
            assert result.returncode == 0, result.stderr
            return result

        monkeypatch.setattr(guard, "run", provider_run)
        monkeypatch.setattr(guard, "verify_candidate_source_state", lambda *_: None)
        common = dict(
            candidate_version="fixture",
            probe_source=probe,
            probe_source_sha256=guard.sha256_file(probe),
            live_env_file=env_file,
            image_override=image_override,
            image_override_sha256=guard.sha256_file(image_override),
            environment_override=environment_override,
            environment_override_sha256=guard.sha256_file(environment_override),
            settings_file=settings,
            settings_file_sha256=guard.sha256_file(settings),
        )
        files = ("docker/docker-compose.yaml", "docker/health.yaml")
        if action == "targeted_up":
            guard.targeted_up(project, files, guard.TARGET_SERVICES, **common)
        else:
            guard.compose_start(project, files, expected_source=None, candidate_source=project, candidate_lock=lock, **common)

    assert not unexpected
    assert len(created) == 24
    observed = {}
    for record in created.values():
        request = record["request"]
        service = request["Labels"]["com.docker.compose.service"]
        assert service not in observed
        observed[service] = request
        assert request["Image"] == lock["images"][service]["reference"]
        assert request["HostConfig"]["NetworkMode"] == "none"
        environment = dict(value.split("=", 1) for value in request["Env"])
        assert environment["DOTENV_VALUE"] == "from-frozen-dotenv"
        assert environment["ENV_FILE_VALUE"] == "from-frozen-env-file"
        assert environment["PRIVATE_VALUE"] == private_value
        # Compare raw provider creation strings. Resolving symlinks here would
        # hide precisely the snapshot alias that caused the real failure.
        mounts = {value.split(":")[1]: value.split(":") for value in request["HostConfig"]["Binds"]}
        assert mounts["/data"] == [str(data), "/data", "rw"]
        assert mounts["/literal-$target"] == [str(data), "/literal-$target", "ro"]
        assert mounts["/data/tool_registry_config"] == [str(docker / ".." / "tool-registry"), "/data/tool_registry_config", "ro"]
        assert mounts["/var/run/docker.sock"] == [str(socket), "/var/run/docker.sock", "ro"]
        if service in bind_files:
            relative, target = bind_files[service]
            assert mounts[target] == [str(docker / relative), target, "ro"]
        if service in guard.CORE_SERVICES:
            source, target, mode = mounts[guard.PROBE_MOUNT_TARGET]
            assert Path(source).is_relative_to(next(iter(snapshots)))
            assert target == guard.PROBE_MOUNT_TARGET and mode == "ro"
            assert request["Healthcheck"]["Test"] == guard._probe_test(service)
    assert set(observed) == set(guard.TARGET_SERVICES)
    assert len(launch_paths) == 1
    assert all(not path.exists() for path in snapshots | set(launch_paths))


def test_compose_config_maps_temporary_runner_data_bind_to_project_tree(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project = tmp_path / "project"
    docker_dir = project / "docker"
    docker_dir.mkdir(parents=True)
    workflow_data = docker_dir / "workflow_data"
    workflow_data.mkdir()
    tool_registry = project / "tool-registry"
    tool_registry.mkdir()
    compose = docker_dir / "docker-compose.yaml"
    compose.write_text(
        "services:\n"
        "  runner:\n"
        "    image: busybox:latest\n"
        "    volumes:\n"
        "      - ./workflow_data:/data\n"
        "      - ${TOOL_REGISTRY_CONFIG_SRC_PATH}:/data/tool_registry_config\n"
        "  db:\n"
        "    image: busybox:latest\n"
        "    volumes:\n"
        "      - ${UNSTRACT_HEALTHCHECK_SOURCE}:"
        "/usr/local/bin/unstract-services.sh:ro\n",
        encoding="utf-8",
    )
    (docker_dir / "compose.train.yaml").write_text("services: {}\n", encoding="utf-8")
    live_env = docker_dir / ".env"
    live_env.write_text(
        "TOOL_REGISTRY_CONFIG_SRC_PATH=" + str(tool_registry) + "\n",
        encoding="utf-8",
    )
    probe = tmp_path / "probe.sh"
    probe.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    temporary_roots: list[Path] = []

    def fake_run(
        args: list[str], *, env: dict[str, str] | None = None, **_: object
    ) -> subprocess.CompletedProcess[str]:
        assert env is not None
        project_directory = Path(args[args.index("--project-directory") + 1])
        temporary_roots.append(project_directory.parent)
        bound_env = Path(args[args.index("--env-file") + 1])
        assert "TOOL_REGISTRY_CONFIG_SRC_PATH=" + str(tool_registry) in bound_env.read_text(
            encoding="utf-8"
        )
        assert env["VERSION"] == "goal09-test"
        assert env["UNSTRACT_HEALTHCHECK_SOURCE"] == str(
            project_directory.parent / "__helper__" / "unstract-services.sh"
        )
        override = next(
            (Path(value) for value in args if value.endswith("bind-sources.override.json")),
            None,
        )
        if override:
            assert json.loads(override.read_text()) == {
                "services": {
                    "runner": {
                        "volumes": [{
                            "type": "bind",
                            "source": str(workflow_data),
                            "target": "/data",
                        }]
                    }
                }
            }
        return subprocess.CompletedProcess(
            args,
            0,
            json.dumps(
                {
                    "services": {
                        "runner": {
                            "volumes": [
                                {
                                    "type": "bind",
                                    "source": str(workflow_data if override else project_directory / "workflow_data"),
                                    "target": "/data",
                                },
                                {
                                    "type": "bind",
                                    "source": str(tool_registry),
                                    "target": "/data/tool_registry_config",
                                },
                            ]
                        },
                        "db": {
                            "volumes": [
                                {
                                    "type": "bind",
                                    "source": env["UNSTRACT_HEALTHCHECK_SOURCE"],
                                    "target": guard.PROBE_MOUNT_TARGET,
                                    "read_only": True,
                                }
                            ]
                        }
                    }
                }
            ),
            "",
        )

    monkeypatch.setattr(guard, "run", fake_run)
    config = guard.compose_config(
        project,
        ("docker/docker-compose.yaml",),
        candidate_version="goal09-test",
        probe_source=probe,
        live_env_file=live_env,
        probe_source_sha256=guard.sha256_file(probe),
    )

    mounts = {mount["target"]: mount for mount in config["services"]["runner"]["volumes"]}
    assert mounts["/data"]["source"] == str(workflow_data.resolve())
    assert mounts["/data/tool_registry_config"]["source"] == str(tool_registry.resolve())
    assert temporary_roots
    probe_mount = config["services"]["db"]["volumes"][0]
    assert probe_mount["source"] == str(
        temporary_roots[0] / "__helper__" / "unstract-services.sh"
    )
    assert not temporary_roots[0].exists()


def test_compose_config_rejects_wrong_snapshot_probe_bind(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project = tmp_path / "project"
    docker_dir = project / "docker"
    docker_dir.mkdir(parents=True)
    compose = docker_dir / "docker-compose.yaml"
    compose.write_text(
        "services:\n"
        "  db:\n"
        "    image: busybox:latest\n"
        "    volumes:\n"
        "      - ${UNSTRACT_HEALTHCHECK_SOURCE}:"
        "/usr/local/bin/unstract-services.sh:ro\n",
        encoding="utf-8",
    )
    (docker_dir / "compose.train.yaml").write_text("services: {}\n", encoding="utf-8")
    live_env = docker_dir / ".env"
    live_env.write_text("COMPOSE_PROJECT_NAME=test\n", encoding="utf-8")
    probe = tmp_path / "probe.sh"
    probe.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")

    def fake_run(args: list[str], **_: object) -> subprocess.CompletedProcess[str]:
        project_directory = Path(args[args.index("--project-directory") + 1])
        return subprocess.CompletedProcess(
            args,
            0,
            json.dumps(
                {
                    "services": {
                        "db": {
                            "volumes": [
                                {
                                    "type": "bind",
                                    "source": str(project_directory / "untracked"),
                                    "target": guard.PROBE_MOUNT_TARGET,
                                    "read_only": True,
                                }
                            ]
                        }
                    }
                }
            ),
            "",
        )

    monkeypatch.setattr(guard, "run", fake_run)
    with pytest.raises(
        guard.GuardError, match="trusted probe mount does not use the frozen probe source"
    ):
        guard.compose_config(
            project,
            ("docker/docker-compose.yaml",),
            candidate_version="goal09-test",
            probe_source=probe,
            live_env_file=live_env,
            probe_source_sha256=guard.sha256_file(probe),
        )


@pytest.mark.parametrize("failure", [None, "temporary-source", "options", "environment", "tamper"])
def test_creation_bind_override_preserves_24_targets_and_rejects_drift(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, failure: str | None
) -> None:
    root = tmp_path / "snapshot"
    root.mkdir()
    live_data = tmp_path / "project-$literal" / "docker" / "workflow_data"
    live_data.mkdir(parents=True)
    snapshot = guard.ComposeSnapshot(
        root=root,
        manifest_path=root / "manifest.json",
        project_dir=live_data.parents[1],
        paths={"__probe_source__": str(root / "probe.sh")},
        bind_sources={"docker/workflow_data": str(live_data)},
    )
    original = {
        "services": {
            service: {
                "image": f"fixture/{service}:locked",
                "environment": {"PRIVATE_VALUE": "literal-${untouched}"},
                "volumes": [
                    {
                        "type": "bind",
                        "source": str(root / "docker" / "workflow_data"),
                        "target": "/data",
                        "read_only": False,
                        "bind": {"propagation": "rshared", "create_host_path": False},
                    },
                    {"type": "volume", "source": "persistent", "target": "/named"},
                ],
            }
            for service in guard.TARGET_SERVICES
        }
    }
    expected = deepcopy(original)
    for definition in expected["services"].values():
        definition["volumes"][0]["source"] = str(live_data)
    overrides: list[Path] = []

    def fake_run(args: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        assert args[-3:] == ["config", "--format", "json"]
        override = next(
            (Path(value) for value in args if value.endswith("bind-sources.override.json")),
            None,
        )
        if override is None:
            return subprocess.CompletedProcess(args, 0, json.dumps(original), "")
        overrides.append(override)
        assert override.stat().st_mode & 0o777 == 0o400
        assert override.parent.stat().st_mode & 0o777 == 0o500
        # This file carries only changed typed bind mounts, never private env
        # values, images, named volumes, or the separately frozen probe mount.
        written = json.loads(override.read_text())
        assert set(written) == {"services"}
        assert set(written["services"]) == set(guard.TARGET_SERVICES)
        for service, definition in written["services"].items():
            mount = deepcopy(expected["services"][service]["volumes"][0])
            mount["source"] = mount["source"].replace("$", "$$")
            assert definition == {"volumes": [mount]}
        rendered = deepcopy(expected)
        for definition in rendered["services"].values():
            definition["volumes"][0]["source"] = str(live_data).replace("$", "$$")
        if failure == "temporary-source":
            rendered = original
        elif failure == "options":
            rendered["services"]["runner"]["volumes"][0]["read_only"] = True
        elif failure == "environment":
            rendered["services"]["runner"]["environment"]["PRIVATE_VALUE"] = "changed"
        elif failure == "tamper":
            os.chmod(override, 0o600)
            override.write_text("{}")
            os.chmod(override, 0o400)
        return subprocess.CompletedProcess(args, 0, json.dumps(rendered), "")

    monkeypatch.setattr(guard, "run", fake_run)
    manager = guard.stable_bind_compose_config(
        ["docker", "compose", "-f", str(root / "compose.yaml")],
        snapshot,
        env={},
        deadline=None,
        pass_fds=(),
    )
    if failure:
        with pytest.raises(guard.GuardError, match="effective config|snapshot file changed"):
            with manager:
                pytest.fail("invalid creation input was made available to up")
    else:
        with manager as (creation_args, config):
            assert config == expected
            assert creation_args[-2:] == ["-f", str(overrides[0])]
            assert overrides[0].is_file()
    assert len(overrides) == 1
    assert not overrides[0].exists()


def test_compose_config_rejects_unrecognized_snapshot_bind_source(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project = tmp_path / "project"
    docker_dir = project / "docker"
    docker_dir.mkdir(parents=True)
    (docker_dir / "workflow_data").mkdir()
    compose = docker_dir / "docker-compose.yaml"
    compose.write_text(
        "services:\n  runner:\n    image: busybox:latest\n    volumes:\n      - ./workflow_data:/data\n",
        encoding="utf-8",
    )
    (docker_dir / "compose.train.yaml").write_text("services: {}\n", encoding="utf-8")
    live_env = docker_dir / ".env"
    live_env.write_text("COMPOSE_PROJECT_NAME=test\n", encoding="utf-8")
    probe = tmp_path / "probe.sh"
    probe.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")

    def fake_run(args: list[str], **_: object) -> subprocess.CompletedProcess[str]:
        project_directory = Path(args[args.index("--project-directory") + 1])
        return subprocess.CompletedProcess(
            args,
            0,
            json.dumps(
                {
                    "services": {
                        "runner": {
                            "volumes": [
                                {
                                    "type": "bind",
                                    "source": str(project_directory / "untracked"),
                                    "target": "/data",
                                }
                            ]
                        }
                    }
                }
            ),
            "",
        )

    monkeypatch.setattr(guard, "run", fake_run)
    with pytest.raises(guard.GuardError, match="not authorized by its snapshot"):
        guard.compose_config(
            project,
            ("docker/docker-compose.yaml",),
            candidate_version="goal09-test",
            probe_source=probe,
            live_env_file=live_env,
            probe_source_sha256=guard.sha256_file(probe),
        )


def test_compose_snapshot_reloads_authoritative_bind_source_mapping(
    tmp_path: Path,
) -> None:
    project = tmp_path / "project"
    docker_dir = project / "docker"
    docker_dir.mkdir(parents=True)
    workflow_data = docker_dir / "workflow_data"
    workflow_data.mkdir()
    compose = docker_dir / "docker-compose.yaml"
    compose.write_text(
        "services:\n  runner:\n    image: busybox:latest\n    volumes:\n      - ./workflow_data:/data\n",
        encoding="utf-8",
    )
    probe = tmp_path / "probe.sh"
    probe.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")

    snapshot = guard.create_compose_snapshot(
        state_dir=tmp_path / "state",
        project_dir=project,
        compose_files=("docker/docker-compose.yaml",),
        live_env_file=None,
        probe_source=probe,
        probe_source_sha256=guard.sha256_file(probe),
        image_override=None,
        image_override_sha256=None,
        environment_override=None,
        environment_override_sha256=None,
    )

    loaded = guard.load_compose_snapshot(snapshot.manifest_path)
    assert loaded.bind_sources == {
        "docker/workflow_data": str(workflow_data.resolve())
    }


def test_compose_rejects_ignored_live_input_drift(tmp_path: Path) -> None:
    project_dir = tmp_path
    (project_dir / "docker").mkdir()
    train_compose = project_dir / guard.LIVE_COMPOSE_TRAIN
    live_env = project_dir / guard.LIVE_ENV_RELATIVE
    train_compose.write_text("services: {}\n", encoding="utf-8")
    live_env.write_text("TOOL_REGISTRY_CONFIG_SRC_PATH=/srv/tool-registry\n", encoding="utf-8")
    expected = {"live_inputs": guard.live_compose_inputs(project_dir, live_env_file=live_env)}
    live_env.write_text("TOOL_REGISTRY_CONFIG_SRC_PATH=/changed\n", encoding="utf-8")
    probe = tmp_path / "probe.sh"
    probe.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")

    with pytest.raises(guard.GuardError, match="ignored live Compose input drifted"):
        guard.compose_config(
            project_dir,
            ("compose.yaml",),
            candidate_version="goal09-test",
            probe_source=probe,
            live_env_file=live_env,
            expected_source=expected,
            probe_source_sha256=guard.sha256_file(probe),
        )


def test_compose_rechecks_candidate_artifacts_before_each_config(tmp_path: Path) -> None:
    (tmp_path / "compose.yaml").write_text("services: {}\n", encoding="utf-8")
    source = tmp_path / "candidate"
    source.mkdir()
    source_root = GUARD_PATH.parents[2]
    for relative in (
        "docker/healthchecks/unstract-services.sh",
        "docker/healthchecks/http-readiness.sh",
        "docker/healthchecks/postgres-readiness.sh",
        "docker/docker-compose-dev-essentials.yaml",
        "docker/compose.train.healthchecks.yaml",
        "docker/compose.train.worker-healthchecks.yaml",
    ):
        destination = source / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source_root / relative, destination)
    subprocess.run(["git", "init", "-q"], cwd=source, check=True)
    subprocess.run(["git", "config", "user.email", "test@example.invalid"], cwd=source, check=True)
    subprocess.run(["git", "config", "user.name", "test"], cwd=source, check=True)
    subprocess.run(["git", "add", "."], cwd=source, check=True)
    subprocess.run(["git", "commit", "-qm", "candidate"], cwd=source, check=True)
    lock = {
        "source_commit": subprocess.check_output(
            ["git", "rev-parse", "HEAD"], cwd=source, text=True
        ).strip(),
        "source_tree": subprocess.check_output(
            ["git", "rev-parse", "HEAD^{tree}"], cwd=source, text=True
        ).strip(),
        "artifacts": guard.artifact_hashes(source),
    }
    probe = source / "docker/healthchecks/unstract-services.sh"

    real_run = guard.run

    def fake_run(args: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        if args and args[0] == "git":
            return real_run(args, **kwargs)
        return subprocess.CompletedProcess(args, 0, json.dumps({"services": {}}), "")

    monkeypatch = pytest.MonkeyPatch()
    monkeypatch.setattr(guard, "run", fake_run)
    try:
        guard.compose_config(
            tmp_path,
            ("compose.yaml",),
            candidate_version="goal09-test",
            probe_source=probe,
            candidate_source=source,
            candidate_lock=lock,
            probe_source_sha256=guard.sha256_file(probe),
        )
        (source / "docker/compose.train.healthchecks.yaml").write_text(
            "services: {}\n", encoding="utf-8"
        )
        with pytest.raises(guard.GuardError, match="candidate source must be clean"):
            guard.compose_config(
                tmp_path,
                ("compose.yaml",),
                candidate_version="goal09-test",
                probe_source=probe,
                candidate_source=source,
                candidate_lock=lock,
                probe_source_sha256=guard.sha256_file(probe),
            )
    finally:
        monkeypatch.undo()


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


def test_bind_mount_source_rejects_symlink_parent_to_distinct_physical_path(
    tmp_path: Path,
) -> None:
    config, baseline, lock, authored = candidate_fixture()
    live_source = tmp_path / "data"
    redirected_source = tmp_path / "other" / "data"
    live_source.mkdir()
    redirected_source.mkdir(parents=True)
    symlink_parent = tmp_path / "docker"
    symlink_parent.symlink_to(tmp_path / "other" / "nested", target_is_directory=True)
    db_container = next(
        container
        for container in baseline["containers"]
        if container["compose"]["com.docker.compose.service"] == "db"
    )
    db_container["mounts"] = [
        {
            "type": "bind",
            "source": str(live_source),
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
            "source": str(symlink_parent / ".." / "data"),
            "target": "/data/tool_registry_config",
            "read_only": False,
        },
    ]
    authored["services"]["db"]["volumes"] = config["services"]["db"]["volumes"]

    with pytest.raises(guard.GuardError, match="mount source"):
        guard.check_candidate_config(config, baseline, lock, authored)


def test_mount_type_change_rejected_even_when_source_matches() -> None:
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
    config["services"]["worker-pg-executor"]["volumes"] = [
        {
            "type": "bind",
            "source": volume_name,
            "target": "/app/prompt-studio-data",
            "read_only": False,
        }
    ]
    authored["services"]["worker-pg-executor"]["volumes"] = config["services"][
        "worker-pg-executor"
    ]["volumes"]

    with pytest.raises(guard.GuardError, match="mount type"):
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


def test_settled_queue_timeout_retains_sanitized_counts_and_phase(monkeypatch) -> None:
    def non_quiescent_snapshot(_deadline):
        return {
            "observed_at": "2026-09-09T17:14:00+00:00",
            "quiescent": False,
            "rabbitmq": {"empty": True},
            "postgres": {
                "counts": {
                    "pg_queue_message": 1,
                    "pg_queue_claimed": 0,
                    "pg_active_barriers": 0,
                    "pg_orchestration_claims": 0,
                }
            },
        }

    monkeypatch.setattr(guard, "queue_snapshot", non_quiescent_snapshot)

    with pytest.raises(guard.QuiescenceTimeout) as raised:
        guard.settled_queue_snapshot(
            max_wait_seconds=0.001,
            phase="post-recreation:runner",
        )

    evidence = raised.value.evidence()
    assert evidence["reason"] == "bounded settled-quiescence interval elapsed"
    assert evidence["phase"] == "post-recreation:runner"
    assert evidence["observations"]
    assert evidence["observations"][0]["postgres_counts"]["pg_queue_message"] == 1


def test_settled_queue_deadline_timeout_retains_counts_and_reason(monkeypatch) -> None:
    def non_quiescent_snapshot(_deadline):
        return {
            "observed_at": "2026-09-09T17:14:00+00:00",
            "quiescent": False,
            "rabbitmq": {"empty": True},
            "postgres": {"counts": {"pg_queue_message": 2}},
        }

    class ExhaustedDeadline:
        def remaining(self) -> float:
            raise guard.GuardError("guarded operation exceeded its total deadline")

    monkeypatch.setattr(guard, "queue_snapshot", non_quiescent_snapshot)

    with pytest.raises(guard.QuiescenceTimeout) as raised:
        guard.settled_queue_snapshot(
            ExhaustedDeadline(),
            max_wait_seconds=120,
            phase="post-recreation:runner",
        )

    evidence = raised.value.evidence()
    assert evidence["reason"] == "guarded operation deadline elapsed before settled quiescence"
    assert evidence["observations"][-1]["postgres_counts"]["pg_queue_message"] == 2


def test_raw_transition_capture_never_substitutes_for_a_settled_capture(monkeypatch) -> None:
    raw_job_state = {"quiescent": False, "postgres": {"counts": {"pg_queue_message": 1}}}
    monkeypatch.setattr(guard, "runtime_context", lambda **_kwargs: {"runtime": "ok"})
    monkeypatch.setattr(guard, "source_state", lambda *_args, **_kwargs: {"source": "ok"})
    monkeypatch.setattr(guard, "queue_snapshot", lambda _deadline: raw_job_state)
    monkeypatch.setattr(guard, "inspect_project", lambda **_kwargs: [])

    def settled_must_not_run(*_args, **_kwargs):
        raise AssertionError("raw transition capture must not claim settled quiescence")

    monkeypatch.setattr(guard, "settled_queue_snapshot", settled_must_not_run)
    snapshot = guard.capture(
        Path("/project"),
        require_settled_quiescence=False,
    )

    assert snapshot["job_quiescence"] is raw_job_state
    assert snapshot["job_quiescence"]["quiescent"] is False


def test_post_recreation_evidence_is_private_and_contains_only_job_state(tmp_path: Path) -> None:
    snapshot = {
        "captured_at": "2026-09-09T17:14:00+00:00",
        "job_quiescence": {
            "quiescent": False,
            "postgres": {"counts": {"pg_queue_message": 1}},
        },
    }

    guard.write_post_recreation_quiescence(
        tmp_path,
        ("runner",),
        snapshot,
        stage="observed",
    )

    path = tmp_path / "post-recreation-observed-runner.json"
    evidence = json.loads(path.read_text(encoding="utf-8"))
    assert path.stat().st_mode & 0o777 == 0o600
    assert evidence == {
        "captured_at": "2026-09-09T17:14:00+00:00",
        "job_quiescence": snapshot["job_quiescence"],
        "schema": "unstract-health-post-recreation-quiescence/v1",
        "services": ["runner"],
        "stage": "observed",
    }


def test_apply_batch_records_raw_transition_then_requires_strict_settlement(monkeypatch) -> None:
    events: list[str] = []
    capture_kwargs: list[dict] = []
    capture_results = [{}, {}, {}]
    args = SimpleNamespace(
        compose_file=None,
        project_dir="/project",
        candidate_source="/candidate",
        probe_source="/probe",
    )
    baseline = {"source": {}}
    lock = {"candidate_version": "candidate"}

    def fake_capture(*_args, **kwargs):
        capture_kwargs.append(kwargs)
        events.append(
            "raw-capture"
            if kwargs.get("require_settled_quiescence") is False
            else "settled-capture"
        )
        return capture_results.pop(0)

    monkeypatch.setattr(guard, "resolve_live_env_file", lambda *_args: Path("/env"))
    monkeypatch.setattr(guard, "validate_private_override", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(guard, "advisory_lock", lambda **_kwargs: contextlib.nullcontext())
    monkeypatch.setattr(guard, "capture", fake_capture)
    monkeypatch.setattr(guard, "compare_untargeted_runtime", lambda *_args: None)
    monkeypatch.setattr(guard, "compare_source_and_quiescence", lambda *_args: None)
    monkeypatch.setattr(guard, "verify_untouched_targets", lambda *_args: None)
    monkeypatch.setattr(guard, "candidate_image_snapshot", lambda *_args, **_kwargs: {})
    monkeypatch.setattr(guard, "compose_config", lambda *_args, **_kwargs: {})
    monkeypatch.setattr(guard, "check_candidate_config", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(
        guard,
        "settled_queue_snapshot",
        lambda *_args, **_kwargs: {"stability": {"stable": True}},
    )
    monkeypatch.setattr(guard, "targeted_up", lambda *_args, **_kwargs: events.append("targeted-up"))
    monkeypatch.setattr(
        guard,
        "write_post_recreation_quiescence",
        lambda *_args, stage, **_kwargs: events.append(f"evidence-{stage}"),
    )
    monkeypatch.setattr(
        guard,
        "record_replacements",
        lambda *_args, **_kwargs: {"services": {"runner": {}}},
    )
    monkeypatch.setattr(guard, "write_replacement_manifest", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(guard, "wait_healthy", lambda *_args, **_kwargs: events.append("healthy"))
    monkeypatch.setattr(guard, "compare_post_apply", lambda *_args: None)

    result = guard.apply_batch(
        args,
        baseline,
        lock,
        Path("/backup"),
        Path("/image-override"),
        ("runner",),
        (),
        (),
        settings_file=Path("/settings"),
        settings_file_sha256="settings-sha",
        image_override_sha256="image-sha",
        probe_source_sha256="probe-sha",
        runtime_environment_override=Path("/runtime-override"),
        runtime_environment_sha256="runtime-sha",
        reviewed_environment_keys={},
        snapshot=SimpleNamespace(),
        operation_deadline=guard.OperationDeadline(60),
    )

    assert result == {"services": {"runner": {}}}
    assert events == [
        "settled-capture",
        "targeted-up",
        "raw-capture",
        "evidence-observed",
        "healthy",
        "settled-capture",
        "evidence-settled",
    ]
    assert capture_kwargs[1]["require_settled_quiescence"] is False
    assert capture_kwargs[2]["quiescence_max_wait_seconds"] == (
        guard.POST_RECREATION_QUIESCENCE_MAX_WAIT_SECONDS
    )
    assert capture_kwargs[2]["quiescence_phase"] == "post-recreation:runner"
