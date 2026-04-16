"""Tests for zombie_cleanup module."""

from datetime import UTC, datetime, timedelta
from unittest.mock import MagicMock, patch

import pytest
from zombie_cleanup import (
    MANAGED_OWNER_KINDS,
    delete_zombies,
    find_zombie_pods,
    get_config,
    get_pod_age_hours,
    is_managed_pod,
    is_terminating,
)


def _make_pod(
    name: str,
    phase: str = "Running",
    age_hours: float = 0,
    owner_kind: str | None = None,
    terminating: bool = False,
):
    """Create a mock Pod with the given properties."""
    now = datetime.now(UTC)
    created = now - timedelta(hours=age_hours)

    pod = MagicMock()
    pod.metadata.name = name
    pod.metadata.creationTimestamp = created
    pod.metadata.deletionTimestamp = datetime.now(UTC) if terminating else None

    if owner_kind:
        ref = MagicMock()
        ref.kind = owner_kind
        pod.metadata.ownerReferences = [ref]
    else:
        pod.metadata.ownerReferences = None

    pod.status.phase = phase
    return pod


# --- is_managed_pod ---


class TestIsManagedPod:
    def test_no_owner_references(self):
        pod = _make_pod("bare-pod")
        assert not is_managed_pod(pod)

    @pytest.mark.parametrize("kind", sorted(MANAGED_OWNER_KINDS))
    def test_managed_owner_kinds(self, kind):
        pod = _make_pod("managed-pod", owner_kind=kind)
        assert is_managed_pod(pod)

    def test_unmanaged_owner_kind(self):
        pod = _make_pod("arc-pod", owner_kind="EphemeralRunner")
        assert not is_managed_pod(pod)

    def test_empty_owner_references(self):
        pod = _make_pod("empty-refs")
        pod.metadata.ownerReferences = []
        assert not is_managed_pod(pod)


# --- is_terminating ---


class TestIsTerminating:
    def test_not_terminating(self):
        pod = _make_pod("normal-pod")
        assert not is_terminating(pod)

    def test_terminating(self):
        pod = _make_pod("dying-pod", terminating=True)
        assert is_terminating(pod)

    def test_no_deletion_timestamp_attr(self):
        """Pods without deletionTimestamp attr should not be terminating."""
        pod = _make_pod("no-attr-pod")
        del pod.metadata.deletionTimestamp
        assert not is_terminating(pod)


# --- get_pod_age_hours ---


class TestGetPodAgeHours:
    def test_recent_pod(self):
        now = datetime.now(UTC)
        pod = _make_pod("new-pod", age_hours=0.5)
        age = get_pod_age_hours(pod, now)
        assert 0.4 < age < 0.6

    def test_old_pod(self):
        now = datetime.now(UTC)
        pod = _make_pod("old-pod", age_hours=25)
        age = get_pod_age_hours(pod, now)
        assert 24.9 < age < 25.1

    def test_no_timestamp(self):
        pod = _make_pod("no-ts")
        pod.metadata.creationTimestamp = None
        assert get_pod_age_hours(pod, datetime.now(UTC)) == -1.0

    def test_naive_timestamp_treated_as_utc(self):
        now = datetime.now(UTC)
        pod = _make_pod("naive-ts", age_hours=5)
        # Strip timezone to simulate naive datetime from lightkube
        pod.metadata.creationTimestamp = pod.metadata.creationTimestamp.replace(tzinfo=None)
        age = get_pod_age_hours(pod, now)
        assert 4.9 < age < 5.1


# --- find_zombie_pods ---


class TestFindZombiePods:
    def _make_config(self, pending_max=24, running_max=12):
        return {
            "namespace": "arc-runners",
            "pending_max_hours": pending_max,
            "running_max_hours": running_max,
            "dry_run": False,
        }

    def test_no_pods(self):
        client = MagicMock()
        client.list.return_value = []
        assert find_zombie_pods(client, self._make_config()) == []

    def test_skips_managed_pods(self):
        client = MagicMock()
        client.list.return_value = [
            _make_pod("listener", phase="Running", age_hours=100, owner_kind="ReplicaSet"),
            _make_pod("hooks-warmer", phase="Running", age_hours=100, owner_kind="DaemonSet"),
            _make_pod("cron-pod", phase="Running", age_hours=100, owner_kind="Job"),
        ]
        assert find_zombie_pods(client, self._make_config()) == []

    def test_detects_pending_zombie(self):
        client = MagicMock()
        old_pending = _make_pod("stuck-pending", phase="Pending", age_hours=25)
        client.list.return_value = [old_pending]
        zombies = find_zombie_pods(client, self._make_config())
        assert len(zombies) == 1
        assert zombies[0].metadata.name == "stuck-pending"

    def test_detects_running_zombie(self):
        client = MagicMock()
        old_running = _make_pod("stuck-running", phase="Running", age_hours=13)
        client.list.return_value = [old_running]
        zombies = find_zombie_pods(client, self._make_config())
        assert len(zombies) == 1
        assert zombies[0].metadata.name == "stuck-running"

    def test_ignores_young_pods(self):
        client = MagicMock()
        client.list.return_value = [
            _make_pod("new-pending", phase="Pending", age_hours=1),
            _make_pod("new-running", phase="Running", age_hours=2),
        ]
        assert find_zombie_pods(client, self._make_config()) == []

    def test_ignores_succeeded_failed(self):
        client = MagicMock()
        client.list.return_value = [
            _make_pod("done", phase="Succeeded", age_hours=100),
            _make_pod("err", phase="Failed", age_hours=100),
        ]
        assert find_zombie_pods(client, self._make_config()) == []

    def test_mixed_pods(self):
        client = MagicMock()
        client.list.return_value = [
            _make_pod("listener", phase="Running", age_hours=100, owner_kind="ReplicaSet"),
            _make_pod("young-runner", phase="Running", age_hours=1),
            _make_pod("zombie-runner", phase="Running", age_hours=15),
            _make_pod("zombie-pending", phase="Pending", age_hours=30),
            _make_pod("done", phase="Succeeded", age_hours=100),
        ]
        zombies = find_zombie_pods(client, self._make_config())
        names = {z.metadata.name for z in zombies}
        assert names == {"zombie-runner", "zombie-pending"}

    def test_arc_owned_pods_not_skipped(self):
        """Runner pods owned by EphemeralRunner CRD should be checked."""
        client = MagicMock()
        client.list.return_value = [
            _make_pod(
                "arc-runner",
                phase="Running",
                age_hours=15,
                owner_kind="EphemeralRunner",
            ),
        ]
        zombies = find_zombie_pods(client, self._make_config())
        assert len(zombies) == 1

    def test_pending_under_boundary(self):
        client = MagicMock()
        client.list.return_value = [
            _make_pod("under-boundary", phase="Pending", age_hours=23.5),
        ]
        assert find_zombie_pods(client, self._make_config()) == []

    def test_running_under_boundary(self):
        client = MagicMock()
        client.list.return_value = [
            _make_pod("under-boundary", phase="Running", age_hours=11.5),
        ]
        assert find_zombie_pods(client, self._make_config()) == []

    def test_detects_unknown_phase_zombie(self):
        """Pods in Unknown phase (e.g. node failure) use running threshold."""
        client = MagicMock()
        client.list.return_value = [
            _make_pod("unknown-pod", phase="Unknown", age_hours=15),
        ]
        zombies = find_zombie_pods(client, self._make_config())
        assert len(zombies) == 1
        assert zombies[0].metadata.name == "unknown-pod"

    def test_unknown_phase_under_threshold(self):
        client = MagicMock()
        client.list.return_value = [
            _make_pod("unknown-young", phase="Unknown", age_hours=5),
        ]
        assert find_zombie_pods(client, self._make_config()) == []

    def test_skips_terminating_pods(self):
        """Pods already being terminated should not be re-deleted."""
        client = MagicMock()
        client.list.return_value = [
            _make_pod("dying-pod", phase="Running", age_hours=15, terminating=True),
        ]
        assert find_zombie_pods(client, self._make_config()) == []

    def test_skips_pod_without_timestamp(self):
        """Pods with no creationTimestamp should be skipped."""
        client = MagicMock()
        pod = _make_pod("no-ts-pod", phase="Running", age_hours=15)
        pod.metadata.creationTimestamp = None
        client.list.return_value = [pod]
        assert find_zombie_pods(client, self._make_config()) == []


# --- delete_zombies ---


class TestDeleteZombies:
    def _make_config(self, dry_run=False):
        return {
            "namespace": "arc-runners",
            "pending_max_hours": 24,
            "running_max_hours": 12,
            "dry_run": dry_run,
        }

    def test_deletes_pods(self):
        client = MagicMock()
        zombies = [
            _make_pod("z1", phase="Running", age_hours=15),
            _make_pod("z2", phase="Pending", age_hours=30),
        ]
        deleted, failed = delete_zombies(client, zombies, self._make_config())
        assert deleted == 2
        assert failed == 0
        assert client.delete.call_count == 2

    def test_dry_run_skips_delete(self):
        client = MagicMock()
        zombies = [_make_pod("z1", phase="Running", age_hours=15)]
        deleted, failed = delete_zombies(client, zombies, self._make_config(dry_run=True))
        assert deleted == 1
        assert failed == 0
        client.delete.assert_not_called()

    def test_continues_on_delete_failure(self):
        client = MagicMock()
        client.delete.side_effect = [Exception("API error"), None]
        zombies = [
            _make_pod("z1", phase="Running", age_hours=15),
            _make_pod("z2", phase="Running", age_hours=16),
        ]
        deleted, failed = delete_zombies(client, zombies, self._make_config())
        assert deleted == 1
        assert failed == 1
        assert client.delete.call_count == 2

    def test_empty_list(self):
        client = MagicMock()
        deleted, failed = delete_zombies(client, [], self._make_config())
        assert deleted == 0
        assert failed == 0
        client.delete.assert_not_called()

    def test_404_counted_as_success(self):
        """Pod deleted between list and delete should count as success."""
        from lightkube.core.exceptions import ApiError

        client = MagicMock()
        not_found = ApiError.__new__(ApiError)
        not_found.status = MagicMock(code=404)
        client.delete.side_effect = not_found
        zombies = [_make_pod("gone-pod", phase="Running", age_hours=15)]
        deleted, failed = delete_zombies(client, zombies, self._make_config())
        assert deleted == 1
        assert failed == 0

    def test_api_error_non_404_counted_as_failure(self):
        """Non-404 ApiErrors should count as failures."""
        from lightkube.core.exceptions import ApiError

        client = MagicMock()
        forbidden = ApiError.__new__(ApiError)
        forbidden.status = MagicMock(code=403)
        client.delete.side_effect = forbidden
        zombies = [_make_pod("forbidden-pod", phase="Running", age_hours=15)]
        deleted, failed = delete_zombies(client, zombies, self._make_config())
        assert deleted == 0
        assert failed == 1


# --- get_config ---


class TestGetConfig:
    def test_defaults(self):
        with patch.dict("os.environ", {}, clear=True):
            config = get_config()
        assert config["namespace"] == "arc-runners"
        assert config["pending_max_hours"] == 24
        assert config["running_max_hours"] == 12
        assert config["dry_run"] is False

    def test_custom_values(self):
        env = {
            "TARGET_NAMESPACE": "custom-ns",
            "PENDING_MAX_AGE_HOURS": "48",
            "RUNNING_MAX_AGE_HOURS": "6",
            "DRY_RUN": "true",
        }
        with patch.dict("os.environ", env, clear=True):
            config = get_config()
        assert config["namespace"] == "custom-ns"
        assert config["pending_max_hours"] == 48
        assert config["running_max_hours"] == 6
        assert config["dry_run"] is True

    @pytest.mark.parametrize("value", ["true", "True", "TRUE", "1", "yes"])
    def test_dry_run_truthy(self, value):
        with patch.dict("os.environ", {"DRY_RUN": value}, clear=True):
            config = get_config()
        assert config["dry_run"] is True

    @pytest.mark.parametrize("value", ["false", "0", "no", ""])
    def test_dry_run_falsy(self, value):
        with patch.dict("os.environ", {"DRY_RUN": value}, clear=True):
            config = get_config()
        assert config["dry_run"] is False


# --- main ---


class TestMain:
    def test_no_zombies(self):
        with (
            patch("zombie_cleanup.Client") as mock_client_cls,
            patch.dict("os.environ", {"TARGET_NAMESPACE": "arc-runners"}, clear=True),
        ):
            mock_client = MagicMock()
            mock_client.list.return_value = []
            mock_client_cls.return_value = mock_client
            from zombie_cleanup import main

            assert main() == 0

    def test_deletes_zombies(self):
        zombie = _make_pod("old-runner", phase="Running", age_hours=15)
        with (
            patch("zombie_cleanup.Client") as mock_client_cls,
            patch.dict("os.environ", {"TARGET_NAMESPACE": "arc-runners"}, clear=True),
        ):
            mock_client = MagicMock()
            mock_client.list.return_value = [zombie]
            mock_client_cls.return_value = mock_client
            from zombie_cleanup import main

            assert main() == 0
            mock_client.delete.assert_called_once()

    def test_client_creation_failure(self):
        with (
            patch("zombie_cleanup.Client", side_effect=Exception("no cluster")),
            patch.dict("os.environ", {"TARGET_NAMESPACE": "arc-runners"}, clear=True),
        ):
            from zombie_cleanup import main

            assert main() == 1

    def test_list_failure(self):
        with (
            patch("zombie_cleanup.Client") as mock_client_cls,
            patch.dict("os.environ", {"TARGET_NAMESPACE": "arc-runners"}, clear=True),
        ):
            mock_client = MagicMock()
            mock_client.list.side_effect = Exception("API error")
            mock_client_cls.return_value = mock_client
            from zombie_cleanup import main

            assert main() == 1

    def test_partial_failure_returns_1(self):
        """main() returns 1 when some deletes fail."""
        zombie1 = _make_pod("z1", phase="Running", age_hours=15)
        zombie2 = _make_pod("z2", phase="Running", age_hours=16)
        with (
            patch("zombie_cleanup.Client") as mock_client_cls,
            patch.dict("os.environ", {"TARGET_NAMESPACE": "arc-runners"}, clear=True),
        ):
            mock_client = MagicMock()
            mock_client.list.return_value = [zombie1, zombie2]
            mock_client.delete.side_effect = [Exception("API error"), None]
            mock_client_cls.return_value = mock_client
            from zombie_cleanup import main

            assert main() == 1
