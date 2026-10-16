"""Regression cases: silent drain failures and misleading GitHub/metrics snapshots."""

import pytest
from drain_test_checks import (
    CAPACITY_METRIC,
    CLUSTER,
    PROBE_LABEL,
    SCALE_SET_LABEL,
    advertised_capacity,
    listener_pods,
    probe_jobs,
    probe_scale_set,
    require_limits,
    require_queued,
    require_success,
    runner_limits,
)


def scale_set(name="cpu", limit=8, label=PROBE_LABEL):
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
    assert probe_scale_set(items, limits) == "cpu"


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
        probe_scale_set(items, desired)


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
    assert advertised_capacity(f'{CAPACITY_METRIC}{{name="cpu",namespace="arc-runners"}} {value}\n', "cpu") == float(
        value
    )


@pytest.mark.parametrize(
    "metrics",
    [
        "",
        'other{name="cpu"} 0\n',
        f'{CAPACITY_METRIC}{{name="wrong"}} 0\n',
        f'{CAPACITY_METRIC}{{name="cpu"}} NaN\n',
        f'{CAPACITY_METRIC}{{name="cpu"}} +Inf\n',
        f'{CAPACITY_METRIC}{{name="cpu"}} -1\n',
        f'{CAPACITY_METRIC}{{name="cpu",org="a"}} 0\n{CAPACITY_METRIC}{{name="cpu",org="b"}} 0\n',
    ],
)
def test_metric_absence_and_invalid_samples_fail_closed(metrics):
    with pytest.raises(ValueError, match="Missing or invalid"):
        advertised_capacity(metrics, "cpu")


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
