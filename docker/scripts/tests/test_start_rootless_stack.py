"""Exercise startup sequencing without starting containers or contacting a host."""

import os
import subprocess
import tempfile
import unittest
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[1] / "start-rootless-stack.sh"
FAKE_DOCKER = """#!/usr/bin/env python3
import json, os, sys
args = sys.argv[1:]
with open(os.environ['CALL_LOG'], 'a') as log:
    log.write(json.dumps(args) + '\\n')
if args[-2:] == ['config', '--services']:
    print(os.environ.get('TEST_SERVICES', 'db\\nredis\\nx2text-service'))
if 'up' in args and args[-1] == 'redis':
    sys.exit(int(os.environ.get('DEPENDENCY_EXIT', '0')))
"""


class StartupTests(unittest.TestCase):
    def run_start(self, **overrides):
        import json

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            executable = root / "docker"
            executable.write_text(FAKE_DOCKER)
            executable.chmod(0o700)
            log = root / "calls"
            env = {
                **os.environ,
                "PATH": f"{root}:{os.environ['PATH']}",
                "UNSTRACT_PROJECT_DIRECTORY": "/existing/docker",
                "COMPOSE_FILE": "/existing/docker/docker-compose.yaml",
                "COMPOSE_PROJECT_NAME": "existing-project",
                "DOCKER_HOST": "unix:///run/user/1000/podman/podman.sock",
                "CALL_LOG": str(log),
                **overrides,
            }
            result = subprocess.run(
                ["bash", str(SCRIPT)], env=env, capture_output=True, text=True
            )
            calls = (
                [json.loads(line) for line in log.read_text().splitlines()]
                if log.exists()
                else []
            )
            return result, calls

    def test_waits_for_dependencies_before_application(self):
        result, calls = self.run_start()
        self.assertEqual(result.returncode, 0, result.stderr)
        starts = [call for call in calls if "up" in call]
        self.assertEqual(len(starts), 2)
        self.assertEqual(starts[0][-2:], ["db", "redis"])
        self.assertEqual(starts[1][-2:], ["--wait-timeout", "300"])
        for command in starts:
            self.assertIn("--no-recreate", command)
            self.assertIn("--no-build", command)
            self.assertIn("--wait", command)
            self.assertEqual(command[command.index("--pull") + 1], "never")

    def test_dependency_failure_never_starts_application(self):
        result, calls = self.run_start(DEPENDENCY_EXIT="17")
        self.assertEqual(result.returncode, 17)
        self.assertEqual(len([call for call in calls if "up" in call]), 1)

    def test_unknown_project_fails_before_starting(self):
        result, calls = self.run_start(TEST_SERVICES="unrelated-service")
        self.assertEqual(result.returncode, 2)
        self.assertFalse(any("up" in call for call in calls))

    def test_bad_timeout_fails_before_contacting_runtime(self):
        result, calls = self.run_start(UNSTRACT_START_TIMEOUT="unbounded")
        self.assertEqual(result.returncode, 2)
        self.assertEqual(calls, [])


if __name__ == "__main__":
    unittest.main()
