"""Tests for the runner_busy helper module."""

from unittest.mock import MagicMock

from lightkube.core.exceptions import ApiError
from runner_busy import (
    BusyState,
    Decision,
    RunnerLiveness,
    age_over_threshold,
    build_busy_sets,
    classify_pod,
    gather_busy_state,
    get_ephemeralrunner,
    get_runner_pod_label,
    is_ephemeralrunner_pod,
    pod_phase,
    read_ephemeralrunner_jobids,
    recheck_liveness,
)

# Mirror the production defaults so classifier tests read naturally.
PENDING_MAX = 24
RUNNING_MAX = 24
BUSY_MAX = 48


def _api_error(code: int) -> ApiError:
    err = ApiError.__new__(ApiError)
    err.status = MagicMock(code=code)
    return err


def _classify(pod, liveness, phase="Running", age_hours=0.0):
    return classify_pod(pod, phase, age_hours, PENDING_MAX, RUNNING_MAX, BUSY_MAX, liveness)


class TestBusyState:
    def test_busy_by_jobid(self):
        state = BusyState({"r1": "job-1"}, set(), set(), False)
        assert state.is_runner_busy("r1")

    def test_busy_by_workflow(self):
        state = BusyState({"r1": ""}, set(), {"r1"}, False)
        assert state.is_runner_busy("r1")

    def test_not_busy_when_idle_and_unpinned(self):
        state = BusyState({"r1": ""}, {"r1"}, set(), False)
        assert not state.is_runner_busy("r1")

    def test_unknown_runner_not_busy(self):
        state = BusyState({}, set(), set(), False)
        assert not state.is_runner_busy("ghost")


class TestLivenessFor:
    def test_runner_pod_reflects_read_failure_and_busy(self, make_pod):
        state = BusyState({"r1": "job-1"}, {"r1"}, set(), False)
        live = state.liveness_for(make_pod("r1", owner_kind="EphemeralRunner"))
        assert live == RunnerLiveness(read_failed=False, runner_busy=True, anchor_present=False)

    def test_runner_pod_read_failure_propagates(self, make_pod):
        state = BusyState({}, set(), set(), True)
        live = state.liveness_for(make_pod("r1", owner_kind="EphemeralRunner"))
        assert live.read_failed is True

    def test_job_pod_anchor_present(self, make_pod):
        state = BusyState({}, {"runner-a"}, set(), False)
        live = state.liveness_for(make_pod("step", labels={"runner-pod": "runner-a"}))
        assert live == RunnerLiveness(anchor_present=True)

    def test_job_pod_anchor_absent(self, make_pod):
        state = BusyState({}, set(), set(), False)
        live = state.liveness_for(make_pod("step", labels={"runner-pod": "gone"}))
        assert live.anchor_present is False

    def test_bare_pod_all_false(self, make_pod):
        state = BusyState({}, {"anything"}, {"anything"}, True)
        live = state.liveness_for(make_pod("bare"))
        assert live == RunnerLiveness()


class TestDecision:
    def test_delete_decisions(self):
        assert Decision.HARD_CAP_DELETE.is_delete
        assert Decision.NORMAL_AGE_DELETE.is_delete

    def test_keep_decisions(self):
        for d in (
            Decision.FAIL_SAFE_PROTECT,
            Decision.BUSY_PROTECT,
            Decision.JOBPOD_PROTECT,
            Decision.NOT_OVER_AGE_KEEP,
        ):
            assert not d.is_delete


class TestClassifyPodRunner:
    def test_read_failure_protects(self, make_pod):
        pod = make_pod("r", owner_kind="EphemeralRunner")
        assert _classify(pod, RunnerLiveness(read_failed=True), age_hours=100) is Decision.FAIL_SAFE_PROTECT

    def test_busy_under_hardcap_protected(self, make_pod):
        pod = make_pod("r", owner_kind="EphemeralRunner")
        assert _classify(pod, RunnerLiveness(runner_busy=True), age_hours=30) is Decision.BUSY_PROTECT

    def test_busy_over_hardcap_deleted(self, make_pod):
        pod = make_pod("r", owner_kind="EphemeralRunner")
        assert _classify(pod, RunnerLiveness(runner_busy=True), age_hours=50) is Decision.HARD_CAP_DELETE

    def test_idle_over_age_deleted(self, make_pod):
        pod = make_pod("r", owner_kind="EphemeralRunner")
        assert _classify(pod, RunnerLiveness(), age_hours=30) is Decision.NORMAL_AGE_DELETE

    def test_idle_under_age_kept(self, make_pod):
        pod = make_pod("r", owner_kind="EphemeralRunner")
        assert _classify(pod, RunnerLiveness(), age_hours=5) is Decision.NOT_OVER_AGE_KEEP


class TestClassifyPodJobPod:
    def test_read_failure_protects(self, make_pod):
        pod = make_pod("step", labels={"runner-pod": "r"})
        assert _classify(pod, RunnerLiveness(read_failed=True), age_hours=100) is Decision.FAIL_SAFE_PROTECT

    def test_anchor_present_under_hardcap_protected(self, make_pod):
        pod = make_pod("step", labels={"runner-pod": "r"})
        assert _classify(pod, RunnerLiveness(anchor_present=True), age_hours=30) is Decision.JOBPOD_PROTECT

    def test_anchor_present_over_hardcap_deleted(self, make_pod):
        pod = make_pod("step", labels={"runner-pod": "r"})
        assert _classify(pod, RunnerLiveness(anchor_present=True), age_hours=50) is Decision.HARD_CAP_DELETE

    def test_anchor_absent_over_age_deleted(self, make_pod):
        pod = make_pod("step", labels={"runner-pod": "gone"})
        assert _classify(pod, RunnerLiveness(), age_hours=30) is Decision.NORMAL_AGE_DELETE

    def test_anchor_absent_under_age_kept(self, make_pod):
        pod = make_pod("step", labels={"runner-pod": "gone"})
        assert _classify(pod, RunnerLiveness(), age_hours=5) is Decision.NOT_OVER_AGE_KEEP


class TestClassifyPodBare:
    def test_pending_over_age_deleted(self, make_pod):
        pod = make_pod("p")
        assert _classify(pod, RunnerLiveness(), phase="Pending", age_hours=30) is Decision.NORMAL_AGE_DELETE

    def test_running_over_age_deleted(self, make_pod):
        pod = make_pod("p")
        assert _classify(pod, RunnerLiveness(), phase="Running", age_hours=30) is Decision.NORMAL_AGE_DELETE

    def test_under_age_kept(self, make_pod):
        pod = make_pod("p")
        assert _classify(pod, RunnerLiveness(), phase="Running", age_hours=5) is Decision.NOT_OVER_AGE_KEEP


class TestAgeOverThreshold:
    def test_pending_over(self):
        assert age_over_threshold("Pending", 30, PENDING_MAX, RUNNING_MAX) == PENDING_MAX

    def test_running_over(self):
        assert age_over_threshold("Running", 30, PENDING_MAX, RUNNING_MAX) == RUNNING_MAX

    def test_unknown_over(self):
        assert age_over_threshold("Unknown", 30, PENDING_MAX, RUNNING_MAX) == RUNNING_MAX

    def test_within_limits(self):
        assert age_over_threshold("Running", 5, PENDING_MAX, RUNNING_MAX) is None

    def test_none_phase(self):
        assert age_over_threshold(None, 1000, PENDING_MAX, RUNNING_MAX) is None


class TestIsEphemeralRunnerPod:
    def test_no_owner_references(self, make_pod):
        assert not is_ephemeralrunner_pod(make_pod("bare"))

    def test_empty_owner_references(self, make_pod):
        pod = make_pod("empty")
        pod.metadata.ownerReferences = []
        assert not is_ephemeralrunner_pod(pod)

    def test_ephemeralrunner_owner(self, make_pod):
        assert is_ephemeralrunner_pod(make_pod("runner", owner_kind="EphemeralRunner"))

    def test_other_owner(self, make_pod):
        assert not is_ephemeralrunner_pod(make_pod("rs-pod", owner_kind="ReplicaSet"))


class TestGetRunnerPodLabel:
    def test_no_labels(self, make_pod):
        assert get_runner_pod_label(make_pod("p")) is None

    def test_empty_labels(self, make_pod):
        assert get_runner_pod_label(make_pod("p", labels={})) is None

    def test_label_absent(self, make_pod):
        assert get_runner_pod_label(make_pod("p", labels={"other": "x"})) is None

    def test_label_present(self, make_pod):
        assert get_runner_pod_label(make_pod("p", labels={"runner-pod": "r1"})) == "r1"


class TestPodPhase:
    def test_phase_set(self, make_pod):
        assert pod_phase(make_pod("p", phase="Running")) == "Running"

    def test_no_status(self, make_pod):
        pod = make_pod("p")
        pod.status = None
        assert pod_phase(pod) is None


class TestReadEphemeralRunnerJobids:
    def test_maps_jobids(self, make_er):
        client = MagicMock()
        client.list.return_value = [
            make_er("r1", job_id="job-1"),
            make_er("r2", job_id=""),
            make_er("r3", with_status=False),
        ]
        jobids, failed = read_ephemeralrunner_jobids(client, "arc-runners")
        assert failed is False
        assert jobids == {"r1": "job-1", "r2": "", "r3": ""}

    def test_failure_returns_all_protected(self):
        client = MagicMock()
        client.list.side_effect = Exception("api down")
        jobids, failed = read_ephemeralrunner_jobids(client, "arc-runners")
        assert failed is True
        assert jobids == {}


class TestBuildBusySets:
    def test_only_ephemeralrunner_pods_are_live_names(self, make_pod):
        pods = [
            make_pod("runner-a", age_hours=1, owner_kind="EphemeralRunner"),
            make_pod("step-1", labels={"runner-pod": "runner-a"}),
            make_pod("done-step", phase="Succeeded", labels={"runner-pod": "runner-b"}),
            make_pod("failed-step", phase="Failed", labels={"runner-pod": "runner-c"}),
        ]
        live, busy = build_busy_sets(pods)
        # Only the ER-owned runner is a live runner name; bare job pods are not.
        assert live == {"runner-a"}
        # Only the non-terminal step pins its runner; terminal step pods don't.
        assert busy == {"runner-a"}

    def test_self_labeled_bare_pod_is_not_a_live_name(self, make_pod):
        # A bare pod self-applying runner-pod=<own name> must not anchor itself.
        pods = [make_pod("evil", labels={"runner-pod": "evil"})]
        live, busy = build_busy_sets(pods)
        assert live == set()
        assert busy == {"evil"}

    def test_terminal_ephemeralrunner_pod_excluded_from_live_names(self, make_pod):
        # A terminal (Succeeded) runner pod must not count as a live runner name,
        # or a lingering terminal runner would pin its orphaned workflow pods.
        pods = [
            make_pod("dead-runner", phase="Succeeded", owner_kind="EphemeralRunner"),
            make_pod("live-runner", phase="Running", owner_kind="EphemeralRunner"),
        ]
        live, _busy = build_busy_sets(pods)
        assert live == {"live-runner"}

    def test_empty(self):
        live, busy = build_busy_sets([])
        assert live == set()
        assert busy == set()


class TestGatherBusyState:
    def test_combines_pod_and_er_listings(self, make_pod, make_er):
        client = MagicMock()
        client.list.return_value = [make_er("runner-a", job_id="job-9")]
        pods = [
            make_pod("runner-a", age_hours=1, owner_kind="EphemeralRunner"),
            make_pod("step-1", labels={"runner-pod": "runner-a"}),
        ]
        state = gather_busy_state(client, "arc-runners", pods)
        assert state.er_read_failed is False
        assert state.er_jobid == {"runner-a": "job-9"}
        assert state.live_runner_names == {"runner-a"}
        assert state.busy_by_workflow == {"runner-a"}
        assert state.is_runner_busy("runner-a")

    def test_er_read_failure_propagates(self, make_pod):
        client = MagicMock()
        client.list.side_effect = Exception("api down")
        state = gather_busy_state(client, "arc-runners", [make_pod("runner-a", owner_kind="EphemeralRunner")])
        assert state.er_read_failed is True
        assert state.er_jobid == {}
        assert state.live_runner_names == {"runner-a"}


class TestGetEphemeralRunner:
    def test_found_returns_jobid(self, make_er):
        client = MagicMock()
        client.get.return_value = make_er("r1", job_id="job-7")
        assert get_ephemeralrunner(client, "r1", "arc-runners") == (True, "job-7", False)

    def test_found_idle_returns_empty_jobid(self, make_er):
        client = MagicMock()
        client.get.return_value = make_er("r1", job_id="")
        assert get_ephemeralrunner(client, "r1", "arc-runners") == (True, "", False)

    def test_found_without_status(self, make_er):
        client = MagicMock()
        client.get.return_value = make_er("r1", with_status=False)
        assert get_ephemeralrunner(client, "r1", "arc-runners") == (True, "", False)

    def test_not_found_is_not_read_failure(self):
        client = MagicMock()
        client.get.side_effect = _api_error(404)
        assert get_ephemeralrunner(client, "gone", "arc-runners") == (False, "", False)

    def test_other_api_error_is_read_failure(self):
        client = MagicMock()
        client.get.side_effect = _api_error(403)
        assert get_ephemeralrunner(client, "r1", "arc-runners") == (False, "", True)

    def test_generic_error_is_read_failure(self):
        client = MagicMock()
        client.get.side_effect = Exception("boom")
        assert get_ephemeralrunner(client, "r1", "arc-runners") == (False, "", True)


class TestRecheckLiveness:
    def test_runner_busy(self, make_pod, make_er):
        client = MagicMock()
        client.get.return_value = make_er("r1", job_id="job-1")
        live = recheck_liveness(client, make_pod("r1", owner_kind="EphemeralRunner"), "arc-runners")
        assert live == RunnerLiveness(runner_busy=True)

    def test_runner_idle(self, make_pod, make_er):
        client = MagicMock()
        client.get.return_value = make_er("r1", job_id="")
        live = recheck_liveness(client, make_pod("r1", owner_kind="EphemeralRunner"), "arc-runners")
        assert live == RunnerLiveness(runner_busy=False)

    def test_runner_er_gone_not_busy(self, make_pod):
        client = MagicMock()
        client.get.side_effect = _api_error(404)
        live = recheck_liveness(client, make_pod("r1", owner_kind="EphemeralRunner"), "arc-runners")
        assert live == RunnerLiveness(runner_busy=False, read_failed=False)

    def test_runner_read_failure(self, make_pod):
        client = MagicMock()
        client.get.side_effect = _api_error(500)
        live = recheck_liveness(client, make_pod("r1", owner_kind="EphemeralRunner"), "arc-runners")
        assert live == RunnerLiveness(read_failed=True)

    def test_job_pod_anchor_present(self, make_pod, make_er):
        client = MagicMock()
        client.get.return_value = make_er("runner-a", job_id="")
        live = recheck_liveness(client, make_pod("step", labels={"runner-pod": "runner-a"}), "arc-runners")
        assert live == RunnerLiveness(anchor_present=True)

    def test_job_pod_anchor_gone(self, make_pod):
        client = MagicMock()
        client.get.side_effect = _api_error(404)
        live = recheck_liveness(client, make_pod("step", labels={"runner-pod": "gone"}), "arc-runners")
        assert live == RunnerLiveness(anchor_present=False)

    def test_job_pod_read_failure(self, make_pod):
        client = MagicMock()
        client.get.side_effect = _api_error(500)
        live = recheck_liveness(client, make_pod("step", labels={"runner-pod": "runner-a"}), "arc-runners")
        assert live == RunnerLiveness(read_failed=True)

    def test_bare_pod_no_get(self, make_pod):
        client = MagicMock()
        live = recheck_liveness(client, make_pod("bare"), "arc-runners")
        assert live == RunnerLiveness()
        client.get.assert_not_called()
