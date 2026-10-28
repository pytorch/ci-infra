"""Offline tests for the drain-test canary workflow template."""

import http.client
import io
import json
import time
import urllib.error
import urllib.request
from unittest.mock import Mock

import drain_test_run as module
import pytest
import yaml

CLUSTER = "meta-staging-aws-uw1"
TEST_PROBE_LABEL = "test-prefix-l-x86iamx-8-16"


@pytest.fixture
def probe_gate(monkeypatch):
    """Compile the local probe with a mock clock and no external effects."""
    workflow = yaml.safe_load(module.generate_workflow("abc123", TEST_PROBE_LABEL, CLUSTER))
    monkeypatch.setenv("PR_URL", "https://api.github.com/repos/pytorch/pytorch-canary/pulls/1")
    monkeypatch.setenv("GH_TOKEN", "test")
    monkeypatch.setattr(time, "monotonic", Mock(return_value=0))
    monkeypatch.setattr(time, "sleep", Mock())
    monkeypatch.setattr(urllib.request, "urlopen", Mock(side_effect=AssertionError("Unexpected network access")))

    def compile_gate(phase, marker="OSDC_DRAIN_RELEASE_abc123"):
        monkeypatch.setenv("RELEASE_MARKER", marker)
        script = workflow["jobs"][f"drain-{phase}"]["steps"][0]["run"]
        return compile(script.split("\n", 1)[1].rsplit("\nPY", 1)[0], "<probe>", "exec")

    return compile_gate


def test_group_pinning_and_release_gate():
    content = module.generate_workflow("abc123", TEST_PROBE_LABEL, CLUSTER)
    workflow = yaml.safe_load(content)
    assert workflow["name"] == "OSDC Drain Test abc123"
    assert workflow["on"]["pull_request"]["types"] == ["opened", "synchronize"]
    # Editing the PR body releases the probe; it must not create another run.
    for job in workflow["jobs"].values():
        assert job["runs-on"] == {"group": CLUSTER, "labels": [TEST_PROBE_LABEL]}
        assert int(job["timeout-minutes"]) > 0
        assert "head.repo.full_name == github.repository" in job["if"]
    inflight = workflow["jobs"]["drain-inflight"]
    assert inflight["steps"][0]["env"]["RELEASE_MARKER"] == "OSDC_DRAIN_RELEASE_abc123"
    held = workflow["jobs"]["drain-held"]
    assert held["needs"] == "drain-inflight"
    assert held["name"] == "drain-held (${{ matrix.probe }})"
    gate = held["steps"][0]
    assert gate["name"] == module.HELD_RELEASE_STEP
    assert gate["env"]["RELEASE_MARKER"] == "OSDC_HELD_RELEASE_abc123_${{ matrix.probe }}"
    assert "monotonic() + 840" in gate["run"]
    assert held["steps"][1]["name"] == "Complete after undrain"
    assert workflow["permissions"] == {"contents": "read", "pull-requests": "read"}
    assert "action == 'opened'" in held["if"]
    assert held["timeout-minutes"] == 15
    assert "action == 'opened'" in inflight["if"]
    wakeup = workflow["jobs"]["drain-wakeup"]
    assert "action == 'synchronize'" in wakeup["if"]
    assert "needs" not in wakeup
    assert wakeup["timeout-minutes"] == 15
    assert len(held["strategy"]["matrix"]["probe"]) == 3
    assert "time.monotonic()" in inflight["steps"][0]["run"]


@pytest.mark.parametrize("key", ["", "../bad", "٣", "中文", "abc\uff11\uff12\uff13"])
def test_invalid_keys(key):
    with pytest.raises(ValueError, match="Run key"):
        module.generate_workflow(key, TEST_PROBE_LABEL, CLUSTER)


@pytest.mark.parametrize("phase", ["inflight", "held"])
@pytest.mark.parametrize("transient", [None, "network", 503, "incomplete", "timeout", "reset", "non_object"])
def test_probe_wait_code_releases_and_retries(monkeypatch, probe_gate, transient, phase):
    responses = [io.StringIO(json.dumps({"body": "OSDC_DRAIN_RELEASE_abc123"}))]
    if transient == "network":
        responses.insert(0, urllib.error.URLError("offline"))
    elif transient == "non_object":
        responses.insert(0, io.StringIO(json.dumps(["unexpected", "array"])))
    elif transient in ("incomplete", "timeout", "reset"):
        error = {
            "incomplete": http.client.IncompleteRead(b'{"body":', 20),
            "timeout": TimeoutError("Response body timed out"),
            "reset": ConnectionResetError("Connection reset while reading body"),
        }[transient]

        class InterruptedResponse(io.StringIO):
            def read(self, *args):
                raise error

        responses.insert(0, InterruptedResponse())
    elif transient is not None:
        responses.insert(0, urllib.error.HTTPError("https://api.github.com", transient, "retry", {}, None))
    read = Mock(side_effect=responses)
    monkeypatch.setattr(urllib.request, "urlopen", read)
    # Execute only our local template, with its external effects mocked.
    exec(probe_gate(phase), {})  # noqa: S102
    assert read.call_count == (1 if transient is None else 2)
    assert read.call_args.kwargs["timeout"] == 10


@pytest.mark.parametrize("phase", ["inflight", "held"])
def test_probe_wait_does_not_retry_auth_failures(monkeypatch, probe_gate, phase):
    sleep = Mock()
    monkeypatch.setattr(time, "sleep", sleep)
    monkeypatch.setattr(
        urllib.request,
        "urlopen",
        Mock(side_effect=urllib.error.HTTPError("https://api.github.com", 403, "forbidden", {}, None)),
    )
    with pytest.raises(urllib.error.HTTPError, match="403"):
        exec(probe_gate(phase), {})  # noqa: S102
    sleep.assert_not_called()


@pytest.mark.parametrize("phase", ["inflight", "held"])
def test_probe_wait_code_has_its_own_deadline(monkeypatch, probe_gate, phase):
    monkeypatch.setattr(time, "monotonic", Mock(side_effect=[0, 2401]))
    with pytest.raises(SystemExit, match="deadline"):
        exec(probe_gate(phase), {})  # noqa: S102 -- local template, mocked clock/network


@pytest.mark.parametrize(
    ("phase", "body"),
    [
        ("held", "OSDC_HELD_RELEASE_abc123_2"),
        ("held", "OSDC_HELD_RELEASE_other_1"),
        ("held", "OSDC_HELD_RELEASE_abc123_10"),
        ("inflight", "PREFIX_OSDC_DRAIN_RELEASE_abc123"),
        ("inflight", "OSDC_DRAIN_RELEASE_abc123_SUFFIX"),
        ("inflight", "OSDC_PRE_DRAIN_RELEASE_abc123"),
    ],
)
def test_probe_gate_requires_its_exact_release_marker(monkeypatch, probe_gate, phase, body):
    marker = "OSDC_HELD_RELEASE_abc123_1" if phase == "held" else "OSDC_DRAIN_RELEASE_abc123"
    monkeypatch.setattr(time, "monotonic", Mock(side_effect=[0, 0, 841 if phase == "held" else 2401]))
    monkeypatch.setattr(urllib.request, "urlopen", Mock(return_value=io.StringIO(json.dumps({"body": body}))))
    message = "did not verify and release" if phase == "held" else "did not release"
    with pytest.raises(SystemExit, match=message):
        exec(probe_gate(phase, marker), {})  # noqa: S102 -- local template, mocked clock/network


def test_configured_probe_values_are_yaml_strings():
    group = 'canary: "runners" # group'
    label = 'prefix-"quoted"-label'
    workflow = yaml.safe_load(module.generate_workflow("abc123", label, group))
    for job in workflow["jobs"].values():
        assert job["runs-on"] == {"group": group, "labels": [label]}
