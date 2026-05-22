"""Tests for runner_overhead module."""

from pathlib import Path

import pytest
import yaml
from runner_overhead import (
    RunnerPodOverhead,
    load_runner_pod_overhead,
    parse_cpu,
)

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------
# The autouse cache-clearing fixture lives in scripts/python/conftest.py so
# every test file in this directory inherits it automatically.


def _write_generated_yaml(
    path: Path,
    *,
    runner_cpu: str = "750m",
    runner_mem: str = "1Gi",
    listener_cpu: str = "100m",
    listener_mem: str = "128Mi",
    baseline_cpu: str = "8",
    baseline_mem: str = "16Gi",
    workflow_cpu: str = "8",
    workflow_mem: str = "16Gi",
) -> None:
    """Write a minimal valid 2-doc generated runner YAML."""
    path.parent.mkdir(parents=True, exist_ok=True)
    values_doc = {
        "template": {
            "spec": {
                "containers": [
                    {
                        "name": "runner",
                        "resources": {
                            "requests": {
                                "cpu": runner_cpu,
                                "memory": runner_mem,
                            }
                        },
                    }
                ]
            }
        },
        "listenerTemplate": {
            "spec": {
                "containers": [
                    {
                        "name": "listener",
                        "resources": {
                            "requests": {
                                "cpu": listener_cpu,
                                "memory": listener_mem,
                            }
                        },
                        "env": [
                            {"name": "CAPACITY_AWARE_WORKFLOW_CPU", "value": baseline_cpu},
                            {"name": "CAPACITY_AWARE_WORKFLOW_MEMORY", "value": baseline_mem},
                        ],
                    }
                ]
            }
        },
    }
    job_pod = {
        "spec": {
            "containers": [
                {
                    "name": "$job",
                    "resources": {
                        "requests": {
                            "cpu": workflow_cpu,
                            "memory": workflow_mem,
                        }
                    },
                }
            ]
        }
    }
    configmap_doc = {
        "apiVersion": "v1",
        "kind": "ConfigMap",
        "metadata": {"name": "arc-runner-hook-test"},
        "data": {"job-pod.yaml": yaml.dump(job_pod)},
    }
    path.write_text(yaml.dump(values_doc) + "---\n" + yaml.dump(configmap_doc))


# ---------------------------------------------------------------------------
# parse_cpu
# ---------------------------------------------------------------------------


class TestParseCpu:
    def test_millicores(self):
        assert parse_cpu("750m") == 750

    def test_whole_core_string(self):
        assert parse_cpu("1") == 1000

    def test_fractional_string(self):
        assert parse_cpu("1.5") == 1500

    def test_small_fractional_string(self):
        assert parse_cpu("0.1") == 100

    def test_integer_input(self):
        assert parse_cpu(1) == 1000

    def test_float_input(self):
        assert parse_cpu(0.5) == 500

    def test_rejects_negative_millicores(self):
        with pytest.raises(ValueError, match="negative cpu value"):
            parse_cpu("-100m")

    def test_rejects_negative_whole(self):
        with pytest.raises(ValueError, match="negative cpu value"):
            parse_cpu("-1")


# ---------------------------------------------------------------------------
# load_runner_pod_overhead
# ---------------------------------------------------------------------------


class TestLoadRunnerPodOverhead:
    def test_loads_from_synthetic_yaml(self, tmp_path):
        _write_generated_yaml(tmp_path / "modules" / "arc-runners" / "generated" / "r.yaml")
        result = load_runner_pod_overhead(tmp_path)
        assert result == RunnerPodOverhead(
            runner_cpu_m=750,
            runner_mem_mi=1024,
            listener_cpu_m=100,
            listener_mem_mi=128,
            workflow_extra_cpu_m=0,
            workflow_extra_mem_mi=0,
        )

    def test_workflow_extra_when_workflow_exceeds_baseline(self, tmp_path):
        # Workflow container requests 9 cpu / 18Gi but baseline says 8 / 16Gi.
        # Extra = 1 cpu (1000m), 2Gi (2048Mi).
        _write_generated_yaml(
            tmp_path / "modules" / "arc-runners" / "generated" / "r.yaml",
            baseline_cpu="8",
            baseline_mem="16Gi",
            workflow_cpu="9",
            workflow_mem="18Gi",
        )
        result = load_runner_pod_overhead(tmp_path)
        assert result.workflow_extra_cpu_m == 1000
        assert result.workflow_extra_mem_mi == 2048

    def test_workflow_extra_clamps_to_zero_when_workflow_below_baseline(self, tmp_path):
        # Workflow requests less than baseline — must clamp to 0/0, not go negative.
        _write_generated_yaml(
            tmp_path / "modules" / "arc-runners" / "generated" / "r.yaml",
            baseline_cpu="8",
            baseline_mem="16Gi",
            workflow_cpu="4",
            workflow_mem="8Gi",
        )
        result = load_runner_pod_overhead(tmp_path)
        assert result.workflow_extra_cpu_m == 0
        assert result.workflow_extra_mem_mi == 0

    def test_hard_fails_with_no_yamls(self, tmp_path):
        with pytest.raises(RuntimeError, match="No generated runner YAMLs found") as excinfo:
            load_runner_pod_overhead(tmp_path)
        # Searched path must appear in the message.
        assert str(tmp_path) in str(excinfo.value)

    def test_walks_consumer_modules(self, tmp_path, capsys):
        # Pick names so the upstream file sorts first alphabetically by
        # absolute path — the loader treats the alphabetically-first file as
        # the source of truth.
        upstream = tmp_path / "a-upstream"
        consumer = tmp_path / "z-consumer"
        _write_generated_yaml(
            upstream / "modules" / "arc-runners" / "generated" / "r.yaml",
            runner_cpu="750m",
        )
        # Consumer disagrees on runner_cpu_m so we can verify it was walked.
        _write_generated_yaml(
            consumer / "modules" / "arc-runners-b200" / "generated" / "r.yaml",
            runner_cpu="900m",
        )
        result = load_runner_pod_overhead(upstream, consumer)
        # First file (upstream) wins.
        assert result.runner_cpu_m == 750
        # Disagreement warning was emitted.
        err = capsys.readouterr().err
        assert "WARNING" in err
        assert "runner_cpu_m" in err

    def test_warns_on_disagreement(self, tmp_path, capsys):
        # Two files; the alphabetically first one wins.
        _write_generated_yaml(
            tmp_path / "modules" / "arc-runners" / "generated" / "a.yaml",
            runner_cpu="750m",
        )
        _write_generated_yaml(
            tmp_path / "modules" / "arc-runners" / "generated" / "b.yaml",
            runner_cpu="500m",
        )
        result = load_runner_pod_overhead(tmp_path)
        err = capsys.readouterr().err
        assert "WARNING" in err
        assert "runner_cpu_m" in err
        # First file (a.yaml) is the source of truth.
        assert result.runner_cpu_m == 750

    def test_consumer_same_as_upstream_not_double_walked(self, tmp_path, capsys):
        # consumer_root == upstream_dir → only one walk, no spurious self-disagreement.
        _write_generated_yaml(tmp_path / "modules" / "arc-runners" / "generated" / "r.yaml")
        load_runner_pod_overhead(tmp_path, tmp_path)
        err = capsys.readouterr().err
        assert "WARNING" not in err


# ---------------------------------------------------------------------------
# Error-message context (Issue 1)
# ---------------------------------------------------------------------------


class TestParseErrorContext:
    """Per-file errors must name the offending file and the missing thing."""

    def _make_path(self, tmp_path: Path) -> Path:
        path = tmp_path / "modules" / "arc-runners" / "generated" / "broken.yaml"
        path.parent.mkdir(parents=True, exist_ok=True)
        return path

    def test_malformed_yaml_missing_cpu_request(self, tmp_path):
        """Missing requests.cpu in runner container should name the file."""
        path = self._make_path(tmp_path)
        # Valid 2-doc file but the runner container has no cpu request,
        # so _container_requests will hit KeyError('cpu').
        values_doc = {
            "template": {
                "spec": {
                    "containers": [
                        {
                            "name": "runner",
                            "resources": {"requests": {"memory": "1Gi"}},
                        }
                    ]
                }
            },
            "listenerTemplate": {
                "spec": {
                    "containers": [
                        {
                            "name": "listener",
                            "resources": {"requests": {"cpu": "100m", "memory": "128Mi"}},
                            "env": [
                                {"name": "CAPACITY_AWARE_WORKFLOW_CPU", "value": "8"},
                                {"name": "CAPACITY_AWARE_WORKFLOW_MEMORY", "value": "16Gi"},
                            ],
                        }
                    ]
                }
            },
        }
        configmap_doc = {
            "apiVersion": "v1",
            "kind": "ConfigMap",
            "metadata": {"name": "x"},
            "data": {"job-pod.yaml": yaml.dump({"spec": {"containers": [{"name": "j"}]}})},
        }
        path.write_text(yaml.dump(values_doc) + "---\n" + yaml.dump(configmap_doc))

        with pytest.raises(ValueError, match="failed to parse runner overhead from") as excinfo:
            load_runner_pod_overhead(tmp_path)
        msg = str(excinfo.value)
        assert "broken.yaml" in msg
        assert "cpu" in msg

    def test_empty_job_pod_data(self, tmp_path):
        """Empty data['job-pod.yaml'] must produce a precise message."""
        path = self._make_path(tmp_path)
        values_doc = {
            "template": {
                "spec": {
                    "containers": [
                        {
                            "name": "runner",
                            "resources": {"requests": {"cpu": "750m", "memory": "1Gi"}},
                        }
                    ]
                }
            },
            "listenerTemplate": {
                "spec": {
                    "containers": [
                        {
                            "name": "listener",
                            "resources": {"requests": {"cpu": "100m", "memory": "128Mi"}},
                            "env": [
                                {"name": "CAPACITY_AWARE_WORKFLOW_CPU", "value": "8"},
                                {"name": "CAPACITY_AWARE_WORKFLOW_MEMORY", "value": "16Gi"},
                            ],
                        }
                    ]
                }
            },
        }
        # Empty string for job-pod.yaml: yaml.safe_load("") -> None.
        configmap_doc = {
            "apiVersion": "v1",
            "kind": "ConfigMap",
            "metadata": {"name": "x"},
            "data": {"job-pod.yaml": ""},
        }
        path.write_text(yaml.dump(values_doc) + "---\n" + yaml.dump(configmap_doc))

        with pytest.raises(ValueError, match=r"job-pod\.yaml.*empty") as excinfo:
            load_runner_pod_overhead(tmp_path)
        assert "broken.yaml" in str(excinfo.value)

    def test_listener_env_uses_valuefrom(self, tmp_path):
        """Listener env entry with valueFrom (no literal value) must error precisely."""
        path = self._make_path(tmp_path)
        values_doc = {
            "template": {
                "spec": {
                    "containers": [
                        {
                            "name": "runner",
                            "resources": {"requests": {"cpu": "750m", "memory": "1Gi"}},
                        }
                    ]
                }
            },
            "listenerTemplate": {
                "spec": {
                    "containers": [
                        {
                            "name": "listener",
                            "resources": {"requests": {"cpu": "100m", "memory": "128Mi"}},
                            "env": [
                                {
                                    "name": "CAPACITY_AWARE_WORKFLOW_CPU",
                                    "valueFrom": {"configMapKeyRef": {"name": "x", "key": "k"}},
                                },
                                {"name": "CAPACITY_AWARE_WORKFLOW_MEMORY", "value": "16Gi"},
                            ],
                        }
                    ]
                }
            },
        }
        configmap_doc = {
            "apiVersion": "v1",
            "kind": "ConfigMap",
            "metadata": {"name": "x"},
            "data": {"job-pod.yaml": yaml.dump({"spec": {"containers": [{"name": "j"}]}})},
        }
        path.write_text(yaml.dump(values_doc) + "---\n" + yaml.dump(configmap_doc))

        with pytest.raises(ValueError, match="valueFrom") as excinfo:
            load_runner_pod_overhead(tmp_path)
        msg = str(excinfo.value)
        assert "broken.yaml" in msg
        assert "CAPACITY_AWARE_WORKFLOW_CPU" in msg
