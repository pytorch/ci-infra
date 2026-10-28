"""Regression cases: silent drain failures and misleading GitHub/metrics snapshots."""

import pytest
from drain_test_checks import (
    CAPACITY_METRIC,
    SCALE_SET_LABEL,
    advertised_capacity,
    held_probe_number,
    listener_pods,
    probe_jobs,
    probe_scale_set,
    require_limits,
    require_probe_placement,
    require_queued,
    require_success,
    runner_limits,
)

CLUSTER = "meta-staging-aws-uw1"
TEST_PROBE_LABEL = "test-prefix-l-x86iamx-8-16"


def placement_snapshot():
    job = {"id": 7, "runner_name": "probe-runner"}
    pods = [
        {
            "metadata": {
                "name": "probe-runner",
                "uid": "runner-pod-uid",
                "labels": {SCALE_SET_LABEL: TEST_PROBE_LABEL, "app.kubernetes.io/component": "runner"},
            },
            "spec": {"nodeName": "runner-node", "containers": [{"name": "runner"}]},
            "status": {"phase": "Running"},
        },
        {
            "metadata": {"name": "probe-workflow", "uid": "workflow-pod-uid", "labels": {"runner-pod": "probe-runner"}},
            "spec": {"nodeName": "workflow-node", "containers": [{"name": "job"}]},
            "status": {"phase": "Running"},
        },
    ]
    nodes = [{"metadata": {"name": name, "uid": f"{name}-uid"}} for name in ("runner-node", "workflow-node")]
    return job, pods, nodes


@pytest.mark.parametrize("number", [1, 2, 3])
def test_held_probe_matrix_identity(number):
    assert held_probe_number({"name": f"drain-held ({number})"}) == number


@pytest.mark.parametrize("name", ["drain-held", "drain-held (4)", "drain-held (1) extra", "drain-held (1)\n"])
def test_held_probe_rejects_ambiguous_names(name):
    with pytest.raises(RuntimeError, match="Unexpected held probe name"):
        held_probe_number({"name": name})


def test_probe_placement_records_both_pods_and_ignores_unrelated_capacity():
    job, pods, nodes = placement_snapshot()
    nodes += [{"metadata": {"name": "old-node", "uid": "stale"}}, {"metadata": {"name": "other-new", "uid": "other"}}]
    pods.append(
        {
            "metadata": {"name": "step-pod", "labels": {"runner-pod": "probe-runner"}},
            "spec": {"containers": [{"name": "step"}]},
        }
    )
    result = require_probe_placement(job, pods, nodes, {"stale"}, scale_set=TEST_PROBE_LABEL)
    assert result == {
        role: {
            "pod": pod["metadata"]["name"],
            "pod_uid": pod["metadata"]["uid"],
            "node": node["metadata"]["name"],
            "node_uid": node["metadata"]["uid"],
        }
        for role, pod, node in zip(("runner", "workflow"), pods[:2], nodes[:2], strict=True)
    }


@pytest.mark.parametrize("index", [0, 1])
def test_probe_rejects_stale_node_even_if_unrelated_new_nodes_exist(index):
    job, pods, nodes = placement_snapshot()
    nodes.append({"metadata": {"name": "unrelated-new", "uid": "new"}})
    with pytest.raises(RuntimeError, match="reused stale node"):
        require_probe_placement(job, pods, nodes, {nodes[index]["metadata"]["uid"]}, scale_set=TEST_PROBE_LABEL)


def test_probe_placement_uses_node_uid_not_reused_name():
    job, pods, nodes = placement_snapshot()
    nodes[0]["metadata"]["uid"] = "replacement"
    evidence = require_probe_placement(job, pods, nodes, {"runner-node-uid"}, scale_set=TEST_PROBE_LABEL)
    assert evidence["runner"]["node_uid"] == "replacement"


@pytest.mark.parametrize(
    "failure", ["runner-name", "runner-missing", "runner-duplicate", "workflow-missing", "workflow-duplicate"]
)
def test_probe_requires_unambiguous_pod_association(failure):
    job, pods, nodes = placement_snapshot()
    if failure == "runner-name":
        job.pop("runner_name")
    elif failure == "runner-missing":
        pods.pop(0)
    elif failure == "runner-duplicate":
        pods.append(pods[0])
    elif failure == "workflow-missing":
        pods[1]["metadata"]["labels"]["runner-pod"] = "other-runner"
    else:
        pods.append(pods[1])
    with pytest.raises(ValueError, match=r"Missing|ambiguous"):
        require_probe_placement(job, pods, nodes, set(), scale_set=TEST_PROBE_LABEL)


def test_probe_retries_workflow_container_missing_name():
    job, pods, nodes = placement_snapshot()
    pods[1]["spec"]["containers"] = [{}, {"name": "step"}]
    with pytest.raises(ValueError, match="Missing or ambiguous workflow pod"):
        require_probe_placement(job, pods, nodes, set(), scale_set=TEST_PROBE_LABEL)


def test_probe_placement_ignores_unnamed_container_before_job():
    job, pods, nodes = placement_snapshot()
    pods[1]["spec"]["containers"].insert(0, {})
    evidence = require_probe_placement(job, pods, nodes, set(), scale_set=TEST_PROBE_LABEL)
    assert evidence["workflow"]["pod"] == "probe-workflow"


@pytest.mark.parametrize("key", [SCALE_SET_LABEL, "app.kubernetes.io/component"])
def test_probe_rejects_runner_from_wrong_scale_set_or_component(key):
    job, pods, nodes = placement_snapshot()
    pods[0]["metadata"]["labels"][key] = "other"
    with pytest.raises(RuntimeError, match="does not belong"):
        require_probe_placement(job, pods, nodes, set(), scale_set=TEST_PROBE_LABEL)


@pytest.mark.parametrize("index", [0, 1])
@pytest.mark.parametrize(
    "failure", ["pod-uid", "deleting", "phase", "unscheduled", "node-missing", "node-duplicate", "node-uid"]
)
def test_probe_retries_incomplete_pod_or_node_evidence(index, failure):
    job, pods, nodes = placement_snapshot()
    pod = pods[index]
    if failure == "pod-uid":
        pod["metadata"].pop("uid")
    elif failure == "deleting":
        pod["metadata"]["deletionTimestamp"] = "now"
    elif failure == "phase":
        pod["status"]["phase"] = "Pending"
    elif failure == "unscheduled":
        pod["spec"].pop("nodeName")
    elif failure == "node-missing":
        nodes.pop(index)
    elif failure == "node-duplicate":
        nodes.append(nodes[index])
    else:
        nodes[index]["metadata"].pop("uid")
    with pytest.raises(ValueError, match="Incomplete"):
        require_probe_placement(job, pods, nodes, set(), scale_set=TEST_PROBE_LABEL)


def scale_set(name="cpu", limit=8, label=TEST_PROBE_LABEL, cluster=CLUSTER):
    return {"metadata": {"name": name}, "spec": {"maxRunners": limit, "runnerScaleSetName": label}}


def listener(name="cpu", ready="True", **metadata):
    return {
        "metadata": {"name": f"{name}-listener", "labels": {SCALE_SET_LABEL: name}, **metadata},
        "spec": {"containers": [{"ports": [{"name": "metrics", "containerPort": 8080}]}]},
        "status": {"conditions": [{"type": "Ready", "status": ready}]},
    }


def test_complete_limits_and_region_exclusion():
    items = [scale_set(), scale_set("excluded", 0, "gpu")]
    limits = runner_limits(items)
    require_limits(limits, {"cpu": 8, "excluded": 0})
    assert probe_scale_set(items, limits, label=TEST_PROBE_LABEL, cluster=CLUSTER) == "cpu"


@pytest.mark.parametrize("items", [[], [scale_set(), scale_set()], [scale_set(limit=-1)], [scale_set(limit=True)]])
def test_invalid_limits(items):
    with pytest.raises(ValueError, match=r"No AutoscalingRunnerSets|Invalid maxRunners"):
        runner_limits(items)


def test_optional_limit_has_arc_unlimited_semantics():
    assert runner_limits([scale_set(limit=None)]) == {"cpu": 2**31 - 1}
    with pytest.raises(ValueError, match="Runner limits differ"):
        require_limits(runner_limits([scale_set(limit=None)]), {"cpu": 0})


@pytest.mark.parametrize(
    ("actual", "expected"), [({}, {}), ({"cpu": 8}, {"cpu": 0}), ({"cpu": 0}, {"cpu": 0, "gpu": 0})]
)
def test_drain_and_restore_require_exact_map(actual, expected):
    with pytest.raises(ValueError, match="Runner limits differ"):
        require_limits(actual, expected)


@pytest.mark.parametrize(
    ("items", "desired"), [([], {}), ([scale_set()], {"cpu": 0}), ([scale_set(label="gpu")], {"cpu": 8})]
)
def test_probe_cannot_use_missing_or_excluded_label(items, desired):
    with pytest.raises(ValueError, match="No enabled scale set"):
        probe_scale_set(items, desired, label=TEST_PROBE_LABEL, cluster=CLUSTER)


def test_listener_snapshot():
    assert listener_pods([listener(), listener("unrelated")], {"cpu"}) == {"cpu": ("cpu-listener", 8080)}


@pytest.mark.parametrize(
    ("pods", "expected"),
    [
        ([], set()),
        ([], {"cpu"}),
        ([listener(ready="False")], {"cpu"}),
        ([listener(deletionTimestamp="now")], {"cpu"}),
        ([listener(), listener()], {"cpu"}),
    ],
)
def test_missing_unready_terminating_and_duplicate_listener(pods, expected):
    with pytest.raises(ValueError, match=r"Missing ready listeners|Ambiguous listener"):
        listener_pods(pods, expected)


def test_missing_metrics_port():
    pod = listener()
    pod["spec"]["containers"][0]["ports"] = [{"name": "http", "containerPort": 80}]
    with pytest.raises(ValueError, match="Ambiguous listener"):
        listener_pods([pod], {"cpu"})


@pytest.mark.parametrize("value", ["0", "3", "2147483647"])
def test_advertised_capacity(value):
    assert (
        advertised_capacity(
            f'{CAPACITY_METRIC}{{name="cpu",namespace="arc-runners"}} {value}\n',
            "cpu",
            metric_names={"cpu"},
        )
        == float(value)
    )


def test_advertised_capacity_unfiltered_accepts_any_name():
    assert advertised_capacity(f'{CAPACITY_METRIC}{{name="cpu-ers"}} 0\n', "cpu") == 0


@pytest.mark.parametrize(
    "metrics",
    [
        "",
        'other{name="cpu"} 0\n',
        f'{CAPACITY_METRIC}{{name="wrong"}} 0\n',
        f'{CAPACITY_METRIC}{{name="cpu"}} NaN\n',
        f'{CAPACITY_METRIC}{{name="cpu"}} +Inf\n',
        f'{CAPACITY_METRIC}{{name="cpu"}} -1\n',
    ],
)
def test_metric_absence_and_invalid_samples_fail_closed(metrics):
    with pytest.raises(ValueError, match="Missing or invalid"):
        advertised_capacity(metrics, "cpu", metric_names={"cpu"})


def test_duplicate_metric_sample_for_same_name_is_rejected():
    metrics = f'{CAPACITY_METRIC}{{name="cpu",org="a"}} 0\n{CAPACITY_METRIC}{{name="cpu",org="b"}} 0\n'
    with pytest.raises(ValueError, match="Duplicate"):
        advertised_capacity(metrics, "cpu")


def test_advertised_capacity_takes_max_when_ars_and_ers_both_report():
    metrics = (
        f'{CAPACITY_METRIC}{{name="cpu",namespace="arc-runners"}} 8\n'
        f'{CAPACITY_METRIC}{{name="cpu-ers",namespace="arc-runners"}} 4\n'
    )
    assert advertised_capacity(metrics, "cpu", metric_names={"cpu", "cpu-ers"}) == 8


def test_ers_metric_alias_must_be_linked_to_the_listener():
    metrics = f'{CAPACITY_METRIC}{{name="cpu-ers"}} 0\n'
    assert advertised_capacity(metrics, "cpu", metric_names={"cpu", "cpu-ers"}) == 0
    with pytest.raises(ValueError, match="Missing or invalid"):
        advertised_capacity(metrics, "cpu", metric_names={"cpu", "other-ers"})


def test_probe_job_selection():
    held = {"name": "drain-held (1)", "status": "queued", "runner_id": None}
    assert probe_jobs([held, {"name": "drain-inflight"}], "held", 1) == [held]
    require_queued([held])


def test_missing_probe_is_not_a_pass():
    with pytest.raises(ValueError, match="Expected 3 held jobs"):
        probe_jobs([], "held", 3)


@pytest.mark.parametrize(
    "jobs",
    [
        [],
        [{"status": "in_progress"}],
        [{"status": "completed", "conclusion": "success"}],
        [{"status": "queued", "runner_id": 1}],
    ],
)
def test_job_that_escaped_is_detected(jobs):
    with pytest.raises(ValueError, match="Jobs escaped"):
        require_queued(jobs)


def test_jobs_succeed_on_target_group():
    require_success([{"status": "completed", "conclusion": "success", "runner_group_name": CLUSTER}], CLUSTER)


@pytest.mark.parametrize(
    "jobs",
    [
        [],
        [{"status": "in_progress"}],
        [{"status": "completed", "conclusion": "failure", "runner_group_name": CLUSTER}],
        [{"status": "completed", "conclusion": "success", "runner_group_name": "another-staging"}],
    ],
)
def test_wrong_group_and_unsuccessful_job(jobs):
    with pytest.raises(ValueError, match="Jobs did not succeed"):
        require_success(jobs, CLUSTER)
