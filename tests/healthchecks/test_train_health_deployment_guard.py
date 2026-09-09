from __future__ import annotations

import importlib.util
import json
import os
import shlex
import shutil
import subprocess
import sys
from copy import deepcopy
from pathlib import Path

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
        ["HOSTNAME=abcdef012345", "APP_MODE=prod"], container_id=container_id
    )
    custom = guard.env_hashes(
        ["HOSTNAME=worker-custom", "APP_MODE=prod"], container_id=container_id
    )

    assert "HOSTNAME" not in generated
    assert "HOSTNAME" in custom


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

    assert len(calls) == 2
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
    assert calls[1][0][-1] == "runner"

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
    assert calls[2][0][-5:] == ["up", "-d", "--no-build", "--pull", "never"]

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


def test_real_compose_provider_resolves_snapshot_include(tmp_path: Path) -> None:
    project = tmp_path / "project"
    (project / "docker").mkdir(parents=True)
    (project / "docker" / "docker-compose.yaml").write_text(
        "include:\n  - docker-compose-dev-essentials.yaml\nservices: {}\n",
        encoding="utf-8",
    )
    (project / "docker" / "docker-compose-dev-essentials.yaml").write_text(
        "services:\n  included:\n    image: busybox:latest\n", encoding="utf-8"
    )
    relative_env = project / "docker" / "relative.env"
    relative_env.write_text("SNAPSHOT_MARKER=from-snapshot\n", encoding="utf-8")
    relative_data = project / "docker" / "relative-data"
    relative_data.mkdir()
    (relative_data / "marker").write_text("runtime-data\n", encoding="utf-8")
    (project / "docker" / "docker-compose.yaml").write_text(
        "include:\n"
        "  - docker-compose-dev-essentials.yaml\n"
        "services:\n"
        "  relative:\n"
        "    image: busybox:latest\n"
        "    env_file:\n"
        "      - ./relative.env\n"
        "    volumes:\n"
        "      - ./relative-data:/data:ro\n",
        encoding="utf-8",
    )
    env_file = project / "docker" / ".env"
    env_file.write_text("COMPOSE_PROJECT_NAME=snapshot-provider-test\n", encoding="utf-8")
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
    assert (
        rendered["services"]["relative"]["environment"]["SNAPSHOT_MARKER"]
        == "from-snapshot"
    )
    rendered_source = Path(rendered["services"]["relative"]["volumes"][0]["source"])
    assert rendered_source.resolve() == relative_data.resolve()


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
