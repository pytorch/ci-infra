"""Tests for the zombie_cleanup module."""

from datetime import UTC, datetime
from unittest.mock import MagicMock, patch

import pytest
import zombie_metrics as m
from lightkube.core.exceptions import ApiError
from zombie_cleanup import (
    MANAGED_OWNER_KINDS,
    delete_zombies,
    find_zombie_pods,
    get_config,
    get_pod_age_hours,
    is_managed_pod,
    is_terminating,
    main,
    validate_config,
)


@pytest.fixture(autouse=True)
def _reset_metrics():
    """Reset metrics before each test to avoid state leakage across tests."""
    for gauge in (
        m.pods_total,
        m.zombies_found,
        m.pods_deleted,
        m.pods_failed,
        m.pods_skipped,
        m.duration_seconds,
        m.pods_managed_skipped,
        m.oldest_zombie_age_hours,
        m.runner_pods_busy_skipped,
        m.job_pods_protected,
        m.ephemeralrunner_read_errors,
        m.recheck_skipped,
    ):
        gauge.set(0)
    # Counters have no .set(); reset the underlying value directly.
    m.busy_hardcap_deletions_total._value.set(0)


def _config(pending_max=24, running_max=24, busy_max=48, dry_run=False, namespace="arc-runners"):
    """Config dict with every key find_zombie_pods and delete_zombies read."""
    return {
        "namespace": namespace,
        "pending_max_hours": pending_max,
        "running_max_hours": running_max,
        "busy_max_hours": busy_max,
        "dry_run": dry_run,
    }


# --- is_managed_pod ---


class TestIsManagedPod:
    def test_no_owner_references(self, make_pod):
        assert not is_managed_pod(make_pod("bare-pod"))

    @pytest.mark.parametrize("kind", sorted(MANAGED_OWNER_KINDS))
    def test_managed_owner_kinds(self, make_pod, kind):
        assert is_managed_pod(make_pod("managed-pod", owner_kind=kind))

    def test_unmanaged_owner_kind(self, make_pod):
        assert not is_managed_pod(make_pod("arc-pod", owner_kind="EphemeralRunner"))

    def test_empty_owner_references(self, make_pod):
        pod = make_pod("empty-refs")
        pod.metadata.ownerReferences = []
        assert not is_managed_pod(pod)


# --- is_terminating ---


class TestIsTerminating:
    def test_not_terminating(self, make_pod):
        assert not is_terminating(make_pod("normal-pod"))

    def test_terminating(self, make_pod):
        assert is_terminating(make_pod("dying-pod", terminating=True))

    def test_no_deletion_timestamp_attr(self, make_pod):
        pod = make_pod("no-attr-pod")
        del pod.metadata.deletionTimestamp
        assert not is_terminating(pod)


# --- get_pod_age_hours ---


class TestGetPodAgeHours:
    def test_recent_pod(self, make_pod):
        age = get_pod_age_hours(make_pod("new-pod", age_hours=0.5), datetime.now(UTC))
        assert 0.4 < age < 0.6

    def test_old_pod(self, make_pod):
        age = get_pod_age_hours(make_pod("old-pod", age_hours=25), datetime.now(UTC))
        assert 24.9 < age < 25.1

    def test_no_timestamp(self, make_pod):
        pod = make_pod("no-ts")
        pod.metadata.creationTimestamp = None
        assert get_pod_age_hours(pod, datetime.now(UTC)) == -1.0

    def test_naive_timestamp_treated_as_utc(self, make_pod):
        now = datetime.now(UTC)
        pod = make_pod("naive-ts", age_hours=5)
        pod.metadata.creationTimestamp = pod.metadata.creationTimestamp.replace(tzinfo=None)
        age = get_pod_age_hours(pod, now)
        assert 4.9 < age < 5.1


# --- validate_config ---


class TestValidateConfig:
    def test_defaults_are_valid(self):
        assert validate_config(_config()) is None

    def test_busy_below_running_is_invalid(self):
        err = validate_config(_config(running_max=24, busy_max=10))
        assert err is not None
        assert "busy_max_age_hours" in err

    def test_busy_equal_running_is_valid(self):
        assert validate_config(_config(running_max=24, busy_max=24)) is None

    def test_running_max_zero_is_invalid(self):
        err = validate_config(_config(running_max=0))
        assert err is not None
        assert "running_max_age_hours" in err

    def test_pending_max_zero_is_invalid(self):
        err = validate_config(_config(pending_max=0))
        assert err is not None
        assert "pending_max_age_hours" in err


# --- find_zombie_pods ---


class TestFindZombiePods:
    def test_no_pods(self, set_list):
        client = MagicMock()
        set_list(client)
        assert find_zombie_pods(client, _config()) == []

    def test_skips_managed_pods(self, make_pod, set_list):
        client = MagicMock()
        set_list(
            client,
            pods=[
                make_pod("listener", age_hours=100, owner_kind="ReplicaSet"),
                make_pod("hooks-warmer", age_hours=100, owner_kind="DaemonSet"),
                make_pod("cron-pod", age_hours=100, owner_kind="Job"),
            ],
        )
        assert find_zombie_pods(client, _config()) == []

    def test_detects_pending_zombie(self, make_pod, set_list):
        client = MagicMock()
        set_list(client, pods=[make_pod("stuck-pending", phase="Pending", age_hours=25)])
        zombies = find_zombie_pods(client, _config())
        assert [z.metadata.name for z in zombies] == ["stuck-pending"]

    def test_detects_running_zombie(self, make_pod, set_list):
        client = MagicMock()
        set_list(client, pods=[make_pod("stuck-running", phase="Running", age_hours=30)])
        zombies = find_zombie_pods(client, _config())
        assert [z.metadata.name for z in zombies] == ["stuck-running"]

    def test_ignores_young_pods(self, make_pod, set_list):
        client = MagicMock()
        set_list(
            client,
            pods=[
                make_pod("new-pending", phase="Pending", age_hours=1),
                make_pod("new-running", phase="Running", age_hours=2),
            ],
        )
        assert find_zombie_pods(client, _config()) == []

    def test_ignores_succeeded_failed(self, make_pod, set_list):
        client = MagicMock()
        set_list(
            client,
            pods=[
                make_pod("done", phase="Succeeded", age_hours=100),
                make_pod("err", phase="Failed", age_hours=100),
            ],
        )
        assert find_zombie_pods(client, _config()) == []

    def test_mixed_pods(self, make_pod, set_list):
        client = MagicMock()
        set_list(
            client,
            pods=[
                make_pod("listener", age_hours=100, owner_kind="ReplicaSet"),
                make_pod("young-runner", age_hours=1),
                make_pod("zombie-runner", age_hours=30),
                make_pod("zombie-pending", phase="Pending", age_hours=30),
                make_pod("done", phase="Succeeded", age_hours=100),
            ],
        )
        zombies = find_zombie_pods(client, _config())
        assert {z.metadata.name for z in zombies} == {"zombie-runner", "zombie-pending"}

    def test_idle_over_age_runner_reaped(self, make_pod, make_er, set_list):
        """An idle EphemeralRunner pod past the running threshold is still reaped."""
        client = MagicMock()
        set_list(
            client,
            pods=[make_pod("arc-runner", age_hours=30, owner_kind="EphemeralRunner")],
            ers=[make_er("arc-runner", job_id="")],
        )
        zombies = find_zombie_pods(client, _config())
        assert [z.metadata.name for z in zombies] == ["arc-runner"]

    def test_idle_under_age_runner_kept(self, make_pod, make_er, set_list):
        client = MagicMock()
        set_list(
            client,
            pods=[make_pod("arc-runner", age_hours=5, owner_kind="EphemeralRunner")],
            ers=[make_er("arc-runner", job_id="")],
        )
        assert find_zombie_pods(client, _config()) == []

    def test_busy_by_jobid_skipped(self, make_pod, make_er, set_list):
        """A runner with an assigned status.jobId is protected from age-based reaping."""
        client = MagicMock()
        set_list(
            client,
            pods=[make_pod("busy-runner", age_hours=30, owner_kind="EphemeralRunner")],
            ers=[make_er("busy-runner", job_id="job-42")],
        )
        assert find_zombie_pods(client, _config()) == []
        assert m.registry.get_sample_value("zombie_cleanup_runner_pods_busy_skipped") == 1

    def test_busy_by_workflow_label_skipped(self, make_pod, make_er, set_list):
        """A live job pod pinning a runner via the runner-pod label makes it busy."""
        client = MagicMock()
        set_list(
            client,
            pods=[
                make_pod("wf-runner", age_hours=30, owner_kind="EphemeralRunner"),
                make_pod("job-pod", age_hours=1, labels={"runner-pod": "wf-runner"}),
            ],
            ers=[make_er("wf-runner", job_id="")],
        )
        assert find_zombie_pods(client, _config()) == []
        assert m.registry.get_sample_value("zombie_cleanup_runner_pods_busy_skipped") == 1
        assert m.registry.get_sample_value("zombie_cleanup_job_pods_protected") == 1

    def test_busy_over_hardcap_selected_but_counter_not_incremented(self, make_pod, make_er, set_list):
        """A busy runner older than the busy hard cap is selected, but find must NOT count it."""
        client = MagicMock()
        set_list(
            client,
            pods=[make_pod("hung-runner", age_hours=50, owner_kind="EphemeralRunner")],
            ers=[make_er("hung-runner", job_id="job-1")],
        )
        zombies = find_zombie_pods(client, _config())
        assert [z.metadata.name for z in zombies] == ["hung-runner"]
        # F5: the hard-cap counter is incremented only on an actual delete, never at selection.
        assert m.registry.get_sample_value("zombie_cleanup_busy_hardcap_deletions_total") == 0
        assert m.registry.get_sample_value("zombie_cleanup_runner_pods_busy_skipped") == 0

    def test_self_labeled_bare_pod_not_protected(self, make_pod, set_list):
        """A bare pod self-labeling runner-pod=<own name> cannot anchor its own protection (F1)."""
        client = MagicMock()
        set_list(client, pods=[make_pod("evil", age_hours=100, labels={"runner-pod": "evil"})])
        zombies = find_zombie_pods(client, _config())
        assert [z.metadata.name for z in zombies] == ["evil"]

    def test_job_pod_over_hardcap_reaped_despite_runner(self, make_pod, make_er, set_list):
        """A workflow/step pod past the busy hard cap is presumed hung even with its runner live (F1)."""
        client = MagicMock()
        set_list(
            client,
            pods=[
                make_pod("runner-x", age_hours=1, owner_kind="EphemeralRunner"),
                make_pod("step-pod", age_hours=50, labels={"runner-pod": "runner-x"}),
            ],
            ers=[make_er("runner-x", job_id="job-9")],
        )
        zombies = find_zombie_pods(client, _config())
        assert [z.metadata.name for z in zombies] == ["step-pod"]
        # Selection must not touch the hard-cap counter.
        assert m.registry.get_sample_value("zombie_cleanup_busy_hardcap_deletions_total") == 0

    def test_terminal_runner_does_not_protect_its_workflow_pod(self, make_pod, make_er, set_list):
        """A lingering terminal (Succeeded) runner pod must not anchor its orphaned workflow pod (F1)."""
        client = MagicMock()
        set_list(
            client,
            pods=[
                make_pod("dead-runner", phase="Succeeded", age_hours=100, owner_kind="EphemeralRunner"),
                make_pod("step-pod", age_hours=100, labels={"runner-pod": "dead-runner"}),
            ],
            ers=[make_er("dead-runner", job_id="")],
        )
        zombies = find_zombie_pods(client, _config())
        # The workflow pod is reaped, not protected by the terminal runner; the
        # terminal runner itself is left for ARC GC (never hard-capped).
        assert [z.metadata.name for z in zombies] == ["step-pod"]
        assert m.registry.get_sample_value("zombie_cleanup_job_pods_protected") == 0

    def test_terminal_runner_pinned_by_workflow_not_selected(self, make_pod, make_er, set_list):
        """A terminal (Succeeded) runner pinned by a live workflow pod is NOT hard-capped at find."""
        client = MagicMock()
        set_list(
            client,
            pods=[
                make_pod("dead-runner", phase="Succeeded", age_hours=100, owner_kind="EphemeralRunner"),
                make_pod("live-step", age_hours=1, labels={"runner-pod": "dead-runner"}),
            ],
            ers=[make_er("dead-runner", job_id="")],
        )
        assert find_zombie_pods(client, _config()) == []
        assert m.registry.get_sample_value("zombie_cleanup_busy_hardcap_deletions_total") == 0

    def test_er_read_failure_protects_runner_pods(self, make_pod, set_list):
        """When the EphemeralRunner listing fails, runner pods are protected but bare pods still clean."""
        client = MagicMock()
        set_list(
            client,
            pods=[
                make_pod("arc-runner", age_hours=100, owner_kind="EphemeralRunner"),
                make_pod("bare-zombie", age_hours=100),
            ],
            er_error=Exception("api down"),
        )
        zombies = find_zombie_pods(client, _config())
        assert [z.metadata.name for z in zombies] == ["bare-zombie"]
        assert m.registry.get_sample_value("zombie_cleanup_ephemeralrunner_read_errors") == 1

    def test_job_pod_protected_when_runner_live(self, make_pod, make_er, set_list):
        """A bare workflow/step pod under the hard cap is protected while its owning runner pod exists."""
        client = MagicMock()
        set_list(
            client,
            pods=[
                make_pod("runner-x", age_hours=1, owner_kind="EphemeralRunner"),
                make_pod("step-pod", age_hours=10, labels={"runner-pod": "runner-x"}),
            ],
            ers=[make_er("runner-x", job_id="job-9")],
        )
        assert find_zombie_pods(client, _config()) == []
        assert m.registry.get_sample_value("zombie_cleanup_job_pods_protected") == 1

    def test_orphaned_job_pod_reaped(self, make_pod, set_list):
        """A job pod whose owning runner no longer exists is reaped when over age."""
        client = MagicMock()
        set_list(client, pods=[make_pod("orphan-step", age_hours=100, labels={"runner-pod": "gone-runner"})])
        zombies = find_zombie_pods(client, _config())
        assert [z.metadata.name for z in zombies] == ["orphan-step"]

    def test_orphaned_job_pod_under_age_kept(self, make_pod, set_list):
        client = MagicMock()
        set_list(client, pods=[make_pod("orphan-young", age_hours=1, labels={"runner-pod": "gone"})])
        assert find_zombie_pods(client, _config()) == []

    def test_pending_under_boundary(self, make_pod, set_list):
        client = MagicMock()
        set_list(client, pods=[make_pod("under-boundary", phase="Pending", age_hours=23.5)])
        assert find_zombie_pods(client, _config()) == []

    def test_running_under_boundary(self, make_pod, set_list):
        client = MagicMock()
        set_list(client, pods=[make_pod("under-boundary", phase="Running", age_hours=23.5)])
        assert find_zombie_pods(client, _config()) == []

    def test_detects_unknown_phase_zombie(self, make_pod, set_list):
        """Pods in Unknown phase (e.g. node failure) use the running threshold."""
        client = MagicMock()
        set_list(client, pods=[make_pod("unknown-pod", phase="Unknown", age_hours=30)])
        zombies = find_zombie_pods(client, _config())
        assert [z.metadata.name for z in zombies] == ["unknown-pod"]

    def test_unknown_phase_under_threshold(self, make_pod, set_list):
        client = MagicMock()
        set_list(client, pods=[make_pod("unknown-young", phase="Unknown", age_hours=5)])
        assert find_zombie_pods(client, _config()) == []

    def test_skips_terminating_pods(self, make_pod, set_list):
        client = MagicMock()
        set_list(client, pods=[make_pod("dying-pod", age_hours=30, terminating=True)])
        assert find_zombie_pods(client, _config()) == []

    def test_skips_pod_without_timestamp(self, make_pod, set_list):
        client = MagicMock()
        pod = make_pod("no-ts-pod", age_hours=30)
        pod.metadata.creationTimestamp = None
        set_list(client, pods=[pod])
        assert find_zombie_pods(client, _config()) == []

    def test_sets_metrics_for_mixed_pods(self, make_pod, set_list):
        client = MagicMock()
        set_list(
            client,
            pods=[
                make_pod("listener", age_hours=100, owner_kind="ReplicaSet"),
                make_pod("ds-pod", age_hours=50, owner_kind="DaemonSet"),
                make_pod("young-runner", age_hours=1),
                make_pod("zombie-old", age_hours=30),
                make_pod("zombie-new", age_hours=25),
            ],
        )
        find_zombie_pods(client, _config())
        assert m.registry.get_sample_value("zombie_cleanup_pods_total") == 5
        assert m.registry.get_sample_value("zombie_cleanup_pods_managed_skipped") == 2
        assert m.registry.get_sample_value("zombie_cleanup_zombies_found") == 2
        age = m.registry.get_sample_value("zombie_cleanup_oldest_zombie_age_hours")
        assert age is not None
        assert age > 29

    def test_sets_busy_state_metrics(self, make_pod, make_er, set_list):
        """busy-skip, job-protect, hardcap and read-error gauges are all populated."""
        client = MagicMock()
        set_list(
            client,
            pods=[
                make_pod("busy-runner", age_hours=10, owner_kind="EphemeralRunner"),
                make_pod("idle-old-runner", age_hours=30, owner_kind="EphemeralRunner"),
                make_pod("step-pod", age_hours=5, labels={"runner-pod": "busy-runner"}),
            ],
            ers=[make_er("busy-runner", job_id="j1"), make_er("idle-old-runner", job_id="")],
        )
        zombies = find_zombie_pods(client, _config())
        assert [z.metadata.name for z in zombies] == ["idle-old-runner"]
        assert m.registry.get_sample_value("zombie_cleanup_runner_pods_busy_skipped") == 1
        assert m.registry.get_sample_value("zombie_cleanup_job_pods_protected") == 1
        assert m.registry.get_sample_value("zombie_cleanup_ephemeralrunner_read_errors") == 0
        assert m.registry.get_sample_value("zombie_cleanup_busy_hardcap_deletions_total") == 0

    def test_metrics_no_zombies(self, make_pod, set_list):
        client = MagicMock()
        set_list(client, pods=[make_pod("young-runner", age_hours=1)])
        find_zombie_pods(client, _config())
        assert m.registry.get_sample_value("zombie_cleanup_zombies_found") == 0
        assert m.registry.get_sample_value("zombie_cleanup_oldest_zombie_age_hours") == 0
        assert m.registry.get_sample_value("zombie_cleanup_pods_total") == 1
        assert m.registry.get_sample_value("zombie_cleanup_pods_managed_skipped") == 0

    def test_metrics_empty_namespace(self, set_list):
        client = MagicMock()
        set_list(client)
        find_zombie_pods(client, _config())
        assert m.registry.get_sample_value("zombie_cleanup_pods_total") == 0
        assert m.registry.get_sample_value("zombie_cleanup_zombies_found") == 0
        assert m.registry.get_sample_value("zombie_cleanup_oldest_zombie_age_hours") == 0


# --- delete_zombies (returns a 3-tuple: deleted, failed, recheck_skipped) ---


class TestDeleteZombies:
    def test_deletes_pods(self, make_pod, set_list):
        client = MagicMock()
        zombies = [
            make_pod("z1", phase="Running", age_hours=30),
            make_pod("z2", phase="Pending", age_hours=30),
        ]
        set_list(client, pods=zombies)
        assert delete_zombies(client, zombies, _config()) == (2, 0, 0)
        assert client.delete.call_count == 2

    def test_dry_run_skips_delete(self, make_pod, set_list):
        client = MagicMock()
        zombies = [make_pod("z1", phase="Running", age_hours=30)]
        set_list(client, pods=zombies)
        assert delete_zombies(client, zombies, _config(dry_run=True)) == (1, 0, 0)
        client.delete.assert_not_called()

    def test_continues_on_delete_failure(self, make_pod, set_list):
        client = MagicMock()
        zombies = [
            make_pod("z1", phase="Running", age_hours=30),
            make_pod("z2", phase="Running", age_hours=31),
        ]
        set_list(client, pods=zombies)
        client.delete.side_effect = [Exception("API error"), None]
        assert delete_zombies(client, zombies, _config()) == (1, 1, 0)
        assert client.delete.call_count == 2

    def test_empty_list(self, set_list):
        client = MagicMock()
        set_list(client)
        assert delete_zombies(client, [], _config()) == (0, 0, 0)
        client.delete.assert_not_called()

    def test_404_counted_as_success(self, make_pod, set_list):
        """A pod deleted between list and delete should count as success."""
        client = MagicMock()
        set_list(client)
        not_found = ApiError.__new__(ApiError)
        not_found.status = MagicMock(code=404)
        client.delete.side_effect = not_found
        zombies = [make_pod("gone-pod", phase="Running", age_hours=30)]
        assert delete_zombies(client, zombies, _config()) == (1, 0, 0)

    def test_api_error_non_404_counted_as_failure(self, make_pod, set_list):
        client = MagicMock()
        set_list(client)
        forbidden = ApiError.__new__(ApiError)
        forbidden.status = MagicMock(code=403)
        client.delete.side_effect = forbidden
        zombies = [make_pod("forbidden-pod", phase="Running", age_hours=30)]
        assert delete_zombies(client, zombies, _config()) == (0, 1, 0)

    def test_toctou_now_busy_skips_delete(self, make_pod, make_er, set_list):
        """A runner idle at find time but busy at the recheck is not deleted."""
        client = MagicMock()
        runner = make_pod("flip-runner", age_hours=15, owner_kind="EphemeralRunner")
        set_list(client, ers=[make_er("flip-runner", job_id="late-job")])
        assert delete_zombies(client, [runner], _config()) == (0, 0, 1)
        client.delete.assert_not_called()

    def test_toctou_er_read_failure_skips_delete(self, make_pod, set_list):
        """If the per-pod EphemeralRunner GET fails, runner pods are fail-safe skipped."""
        client = MagicMock()
        runner = make_pod("safe-runner", age_hours=30, owner_kind="EphemeralRunner")
        set_list(client, er_error=Exception("api down"))
        assert delete_zombies(client, [runner], _config()) == (0, 0, 1)
        client.delete.assert_not_called()
        assert m.registry.get_sample_value("zombie_cleanup_ephemeralrunner_read_errors") == 1

    def test_idle_runner_deleted_at_recheck(self, make_pod, make_er, set_list):
        """A runner still idle at recheck is deleted."""
        client = MagicMock()
        runner = make_pod("idle-runner", age_hours=30, owner_kind="EphemeralRunner")
        set_list(client, ers=[make_er("idle-runner", job_id="")])
        assert delete_zombies(client, [runner], _config()) == (1, 0, 0)
        client.delete.assert_called_once()

    def test_recheck_er_gone_deletes_runner(self, make_pod, set_list):
        """A runner whose EphemeralRunner is gone (404) at recheck is treated as not-busy and deleted."""
        client = MagicMock()
        runner = make_pod("done-runner", age_hours=30, owner_kind="EphemeralRunner")
        set_list(client, ers=[])
        assert delete_zombies(client, [runner], _config()) == (1, 0, 0)
        client.delete.assert_called_once()

    def test_hardcap_runner_deleted_at_recheck(self, make_pod, make_er, set_list):
        """A busy runner past the hard cap is deleted even though busy, and counts as a hard-cap delete."""
        client = MagicMock()
        runner = make_pod("hung-runner", age_hours=50, owner_kind="EphemeralRunner")
        set_list(client, ers=[make_er("hung-runner", job_id="stuck")])
        assert delete_zombies(client, [runner], _config()) == (1, 0, 0)
        client.delete.assert_called_once()
        assert m.registry.get_sample_value("zombie_cleanup_busy_hardcap_deletions_total") == 1

    def test_job_pod_protected_at_recheck(self, make_pod, make_er, set_list):
        """A job pod under the hard cap whose owning runner is live at recheck is not deleted."""
        client = MagicMock()
        step = make_pod("step-pod", age_hours=10, labels={"runner-pod": "runner-live"})
        set_list(client, ers=[make_er("runner-live", job_id="j")])
        assert delete_zombies(client, [step], _config()) == (0, 0, 1)
        client.delete.assert_not_called()

    def test_job_pod_over_hardcap_deleted_at_recheck(self, make_pod, make_er, set_list):
        """A job pod past the hard cap is deleted at recheck despite its runner being live (F1/F5)."""
        client = MagicMock()
        step = make_pod("step-pod", age_hours=50, labels={"runner-pod": "runner-live"})
        set_list(client, ers=[make_er("runner-live", job_id="j")])
        assert delete_zombies(client, [step], _config()) == (1, 0, 0)
        client.delete.assert_called_once()
        assert m.registry.get_sample_value("zombie_cleanup_busy_hardcap_deletions_total") == 1

    def test_orphan_job_pod_deleted_at_recheck(self, make_pod, set_list):
        """A job pod whose owning runner is gone (404) at recheck is deleted."""
        client = MagicMock()
        step = make_pod("orphan-step", age_hours=100, labels={"runner-pod": "gone"})
        set_list(client, ers=[])
        assert delete_zombies(client, [step], _config()) == (1, 0, 0)
        client.delete.assert_called_once()


# --- get_config ---


class TestGetConfig:
    def test_defaults(self):
        with patch.dict("os.environ", {}, clear=True):
            config = get_config()
        assert config["namespace"] == "arc-runners"
        assert config["pending_max_hours"] == 24
        assert config["running_max_hours"] == 24
        assert config["busy_max_hours"] == 48
        assert config["dry_run"] is False
        assert config["pushgateway_url"] == ""

    def test_pushgateway_url_from_env(self):
        with patch.dict("os.environ", {"PUSHGATEWAY_URL": "http://pushgw:9091"}, clear=True):
            config = get_config()
        assert config["pushgateway_url"] == "http://pushgw:9091"

    def test_custom_values(self):
        env = {
            "TARGET_NAMESPACE": "custom-ns",
            "PENDING_MAX_AGE_HOURS": "48",
            "RUNNING_MAX_AGE_HOURS": "6",
            "BUSY_MAX_AGE_HOURS": "72",
            "DRY_RUN": "true",
        }
        with patch.dict("os.environ", env, clear=True):
            config = get_config()
        assert config["namespace"] == "custom-ns"
        assert config["pending_max_hours"] == 48
        assert config["running_max_hours"] == 6
        assert config["busy_max_hours"] == 72
        assert config["dry_run"] is True

    @pytest.mark.parametrize("value", ["true", "True", "TRUE", "1", "yes"])
    def test_dry_run_truthy(self, value):
        with patch.dict("os.environ", {"DRY_RUN": value}, clear=True):
            assert get_config()["dry_run"] is True

    @pytest.mark.parametrize("value", ["false", "0", "no", ""])
    def test_dry_run_falsy(self, value):
        with patch.dict("os.environ", {"DRY_RUN": value}, clear=True):
            assert get_config()["dry_run"] is False


# --- main ---


class TestMain:
    def test_no_zombies(self, set_list):
        with (
            patch("zombie_cleanup.Client") as mock_client_cls,
            patch.dict("os.environ", {"TARGET_NAMESPACE": "arc-runners"}, clear=True),
        ):
            client = MagicMock()
            set_list(client)
            mock_client_cls.return_value = client
            assert main() == 0

    def test_deletes_zombies(self, make_pod, set_list):
        zombie = make_pod("old-runner", phase="Running", age_hours=30)
        with (
            patch("zombie_cleanup.Client") as mock_client_cls,
            patch.dict("os.environ", {"TARGET_NAMESPACE": "arc-runners"}, clear=True),
        ):
            client = MagicMock()
            set_list(client, pods=[zombie])
            mock_client_cls.return_value = client
            assert main() == 0
            client.delete.assert_called_once()

    def test_invalid_config_fails_closed(self):
        """busy_max < running_max exits 1 before any client is created or pod deleted."""
        with (
            patch("zombie_cleanup.Client") as mock_client_cls,
            patch.dict(
                "os.environ",
                {"BUSY_MAX_AGE_HOURS": "1", "RUNNING_MAX_AGE_HOURS": "24"},
                clear=True,
            ),
        ):
            assert main() == 1
            mock_client_cls.assert_not_called()

    def test_client_creation_failure(self):
        """Client() failure propagates (it is created outside the try block)."""
        with (
            patch("zombie_cleanup.Client", side_effect=Exception("no cluster")),
            patch.dict("os.environ", {"TARGET_NAMESPACE": "arc-runners"}, clear=True),
            pytest.raises(Exception, match="no cluster"),
        ):
            main()

    def test_list_failure(self):
        with (
            patch("zombie_cleanup.Client") as mock_client_cls,
            patch.dict("os.environ", {"TARGET_NAMESPACE": "arc-runners"}, clear=True),
        ):
            client = MagicMock()
            client.list.side_effect = Exception("API error")
            mock_client_cls.return_value = client
            assert main() == 1

    def test_partial_failure_returns_1(self, make_pod, set_list):
        z1 = make_pod("z1", phase="Running", age_hours=30)
        z2 = make_pod("z2", phase="Running", age_hours=31)
        with (
            patch("zombie_cleanup.Client") as mock_client_cls,
            patch.dict("os.environ", {"TARGET_NAMESPACE": "arc-runners"}, clear=True),
        ):
            client = MagicMock()
            set_list(client, pods=[z1, z2])
            client.delete.side_effect = [Exception("API error"), None]
            mock_client_cls.return_value = client
            assert main() == 1

    def test_cleanup_cap_limits_deletions(self, make_pod, set_list):
        # 20 total pods -> cap = max(20*0.1, 10) = 10; 15 zombies -> clean 10, skip 5
        pods = [make_pod(f"normal-{i}", phase="Running", age_hours=1) for i in range(5)] + [
            make_pod(f"zombie-{i}", phase="Running", age_hours=30) for i in range(15)
        ]
        with (
            patch("zombie_cleanup.Client") as mock_client_cls,
            patch.dict("os.environ", {"TARGET_NAMESPACE": "arc-runners"}, clear=True),
        ):
            client = MagicMock()
            set_list(client, pods=pods)
            mock_client_cls.return_value = client
            main()
            assert client.delete.call_count == 10
            assert m.registry.get_sample_value("zombie_cleanup_pods_skipped") == 5
            assert m.registry.get_sample_value("zombie_cleanup_zombies_found") == 15

    def test_cleanup_cap_no_truncation_when_under_cap(self, make_pod, set_list):
        pods = [make_pod(f"normal-{i}", phase="Running", age_hours=1) for i in range(95)] + [
            make_pod(f"zombie-{i}", phase="Running", age_hours=30) for i in range(5)
        ]
        with (
            patch("zombie_cleanup.Client") as mock_client_cls,
            patch.dict("os.environ", {"TARGET_NAMESPACE": "arc-runners"}, clear=True),
        ):
            client = MagicMock()
            set_list(client, pods=pods)
            mock_client_cls.return_value = client
            main()
            assert client.delete.call_count == 5
            assert m.registry.get_sample_value("zombie_cleanup_pods_skipped") == 0

    def test_cleanup_cap_uses_ten_percent_when_larger(self, make_pod, set_list):
        # 200 total pods -> cap = max(200*0.1, 10) = 20; 25 zombies -> clean 20, skip 5
        pods = [make_pod(f"normal-{i}", phase="Running", age_hours=1) for i in range(175)] + [
            make_pod(f"zombie-{i}", phase="Running", age_hours=30) for i in range(25)
        ]
        with (
            patch("zombie_cleanup.Client") as mock_client_cls,
            patch.dict("os.environ", {"TARGET_NAMESPACE": "arc-runners"}, clear=True),
        ):
            client = MagicMock()
            set_list(client, pods=pods)
            mock_client_cls.return_value = client
            main()
            assert client.delete.call_count == 20
            assert m.registry.get_sample_value("zombie_cleanup_pods_skipped") == 5

    def test_sets_duration_metric(self, set_list):
        with (
            patch("zombie_cleanup.Client") as mock_client_cls,
            patch.dict("os.environ", {"TARGET_NAMESPACE": "arc-runners"}, clear=True),
        ):
            client = MagicMock()
            set_list(client)
            mock_client_cls.return_value = client
            main()
            duration = m.registry.get_sample_value("zombie_cleanup_duration_seconds")
            assert duration is not None
            assert duration >= 0

    def test_increments_runs_total_success(self, set_list):
        with (
            patch("zombie_cleanup.Client") as mock_client_cls,
            patch.dict("os.environ", {"TARGET_NAMESPACE": "arc-runners"}, clear=True),
        ):
            client = MagicMock()
            set_list(client)
            mock_client_cls.return_value = client
            main()
            val = m.registry.get_sample_value("zombie_cleanup_runs_total", {"status": "success"})
            assert val is not None
            assert val >= 1

    def test_increments_runs_total_failure(self):
        with (
            patch("zombie_cleanup.Client") as mock_client_cls,
            patch.dict("os.environ", {"TARGET_NAMESPACE": "arc-runners"}, clear=True),
        ):
            client = MagicMock()
            client.list.side_effect = Exception("boom")
            mock_client_cls.return_value = client
            main()
            val = m.registry.get_sample_value("zombie_cleanup_runs_total", {"status": "failure"})
            assert val is not None
            assert val >= 1

    def test_pushes_metrics_no_zombies(self, set_list):
        with (
            patch("zombie_cleanup.Client") as mock_client_cls,
            patch("zombie_cleanup.m.push_metrics") as mock_push,
            patch.dict(
                "os.environ",
                {"TARGET_NAMESPACE": "arc-runners", "PUSHGATEWAY_URL": "http://pushgw:9091"},
                clear=True,
            ),
        ):
            client = MagicMock()
            set_list(client)
            mock_client_cls.return_value = client
            main()
            mock_push.assert_called_once_with("http://pushgw:9091")

    def test_pushes_metrics_after_delete(self, make_pod, set_list):
        zombie = make_pod("old-runner", phase="Running", age_hours=30)
        with (
            patch("zombie_cleanup.Client") as mock_client_cls,
            patch("zombie_cleanup.m.push_metrics") as mock_push,
            patch.dict(
                "os.environ",
                {"TARGET_NAMESPACE": "arc-runners", "PUSHGATEWAY_URL": "http://pushgw:9091"},
                clear=True,
            ),
        ):
            client = MagicMock()
            set_list(client, pods=[zombie])
            mock_client_cls.return_value = client
            main()
            mock_push.assert_called_once_with("http://pushgw:9091")

    def test_pushes_metrics_on_exception(self):
        with (
            patch("zombie_cleanup.Client") as mock_client_cls,
            patch("zombie_cleanup.m.push_metrics") as mock_push,
            patch.dict(
                "os.environ",
                {"TARGET_NAMESPACE": "arc-runners", "PUSHGATEWAY_URL": "http://pushgw:9091"},
                clear=True,
            ),
        ):
            client = MagicMock()
            client.list.side_effect = Exception("boom")
            mock_client_cls.return_value = client
            main()
            mock_push.assert_called_once_with("http://pushgw:9091")

    def test_skips_push_when_url_empty(self, set_list):
        with (
            patch("zombie_cleanup.Client") as mock_client_cls,
            patch("zombie_cleanup.m.push_metrics") as mock_push,
            patch.dict("os.environ", {"TARGET_NAMESPACE": "arc-runners"}, clear=True),
        ):
            client = MagicMock()
            set_list(client)
            mock_client_cls.return_value = client
            main()
            mock_push.assert_not_called()
