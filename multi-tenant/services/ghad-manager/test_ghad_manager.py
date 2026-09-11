import importlib.util
import sys
import types
import unittest
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock


docker = types.ModuleType("docker")
docker.DockerClient = object
docker.types = SimpleNamespace(DeviceRequest=object)
github = types.ModuleType("github")
github.Auth = SimpleNamespace()
github.Github = object
sys.modules["docker"] = docker
sys.modules["github"] = github

module_path = Path(__file__).with_name("ghad-manager.py")
spec = importlib.util.spec_from_file_location("ghad_manager", module_path)
assert spec is not None and spec.loader is not None
ghad_manager = importlib.util.module_from_spec(spec)
spec.loader.exec_module(ghad_manager)


class RecycleIdleRunnerTest(unittest.TestCase):
    def test_recycles_only_old_idle_runners(self) -> None:
        now = datetime(2026, 8, 28, 12, tzinfo=UTC)
        cases = [
            (timedelta(hours=9), False, False),
            (timedelta(hours=10), False, True),
            (timedelta(hours=13), True, False),
        ]

        for age, busy, expected_stop in cases:
            with self.subTest(age=age, busy=busy):
                container = Mock()
                container.name = ghad_manager.REQUIRED_CONTAINER_NAME
                container.status = "running"
                container.attrs = {"Created": (now - age).isoformat()}
                process = "Runner.Worker" if busy else "Runner.Listener"
                container.top.return_value = {"Processes": [["1", process]]}
                client = SimpleNamespace(
                    containers=SimpleNamespace(list=Mock(return_value=[container]))
                )

                ghad_manager.recycle_idle_runner(
                    client,
                    ghad_manager.REQUIRED_CONTAINER_NAME,
                    now,
                )

                self.assertEqual(container.stop.called, expected_stop)


if __name__ == "__main__":
    unittest.main()
