"""Tests for cluster_topology module.

All tests use synthetic YAML written to ``tmp_path`` — they do NOT touch the
real ``clusters.yaml`` or ``modules/`` tree.
"""

from __future__ import annotations

import textwrap
from typing import TYPE_CHECKING

import pytest
import yaml
from cluster_topology import (
    ClusterTopology,
    NodePoolEntry,
    RunnerEntry,
    _extract_runner_class,
    _extract_workflow_fleet,
    _mark_schedulability,
    derive_fleet_name,
    fleet_nodepool_name,
    is_excluded_for_region,
    load_nodepools,
    load_runners,
    parse_cpu,
    parse_memory,
    resolve_cluster,
)

if TYPE_CHECKING:
    from pathlib import Path

# ---------------------------------------------------------------------------
# Tiny pure helpers
# ---------------------------------------------------------------------------


class TestIsExcludedForRegion:
    def test_region_in_list_returns_true(self):
        assert is_excluded_for_region({"exclude_regions": ["us-west-1"]}, "us-west-1") is True

    def test_region_not_in_list_returns_false(self):
        assert is_excluded_for_region({"exclude_regions": ["us-west-1"]}, "us-east-2") is False

    def test_no_exclude_regions_key_returns_false(self):
        assert is_excluded_for_region({"name": "c7i"}, "us-west-1") is False

    def test_none_region_is_noop(self):
        assert is_excluded_for_region({"exclude_regions": ["us-west-1"]}, None) is False

    def test_empty_region_is_noop(self):
        assert is_excluded_for_region({"exclude_regions": ["us-west-1"]}, "") is False

    def test_explicit_none_in_list_value(self):
        assert is_excluded_for_region({"exclude_regions": None}, "us-west-1") is False


class TestDeriveFleetName:
    def test_simple_intel(self):
        assert derive_fleet_name("c7i.48xlarge") == "c7i"

    def test_graviton(self):
        assert derive_fleet_name("m8g.24xlarge") == "m8g"

    def test_dashed_family_b200(self):
        # The dash inside the family must survive — fleet is 'p6-b200', not 'p6'.
        assert derive_fleet_name("p6-b200.48xlarge") == "p6-b200"

    def test_metal_size_keeps_family(self):
        assert derive_fleet_name("c7i.metal-24xl") == "c7i"


class TestFleetNodepoolName:
    def test_default_when_fleet_matches_family(self):
        assert fleet_nodepool_name("c7i", "c7i.48xlarge") == "c7i-48xlarge"

    def test_uses_fleet_prefix_when_overriding(self):
        # c7i-runner is a name-only clone of c7i; nodepool name keeps the fleet prefix
        assert fleet_nodepool_name("c7i-runner", "c7i.48xlarge") == "c7i-runner-48xlarge"

    def test_release_suffix(self):
        assert fleet_nodepool_name("m8g", "m8g.48xlarge", "-release") == "m8g-48xlarge-release"

    def test_dashed_family(self):
        assert fleet_nodepool_name("p6-b200", "p6-b200.48xlarge") == "p6-b200-48xlarge"


class TestParseCpu:
    def test_milli_string(self):
        assert parse_cpu("750m") == 750

    def test_whole_number_string(self):
        assert parse_cpu("6") == 6000

    def test_fractional_string(self):
        assert parse_cpu("6.5") == 6500

    def test_int_value(self):
        assert parse_cpu(2) == 2000

    def test_float_value(self):
        assert parse_cpu(0.5) == 500


class TestParseMemory:
    def test_gib(self):
        assert parse_memory("29Gi") == 29 * 1024

    def test_mib(self):
        assert parse_memory("512Mi") == 512

    def test_kib(self):
        assert parse_memory("2048Ki") == 2

    def test_plain_bytes(self):
        # Plain numbers are bytes -> MiB
        assert parse_memory(str(1024 * 1024)) == 1


# ---------------------------------------------------------------------------
# Affinity extractors (workflow_fleet, runner_class)
# ---------------------------------------------------------------------------


class TestExtractWorkflowFleet:
    def test_finds_node_fleet_value(self):
        spec = {
            "affinity": {
                "nodeAffinity": {
                    "preferredDuringSchedulingIgnoredDuringExecution": [
                        {
                            "weight": 50,
                            "preference": {
                                "matchExpressions": [
                                    {"key": "node-fleet", "operator": "In", "values": ["m8g"]},
                                    {"key": "workload-type", "operator": "In", "values": ["github-runner"]},
                                ]
                            },
                        }
                    ]
                }
            }
        }
        assert _extract_workflow_fleet(spec) == "m8g"

    def test_finds_in_second_preference(self):
        spec = {
            "affinity": {
                "nodeAffinity": {
                    "preferredDuringSchedulingIgnoredDuringExecution": [
                        {"preference": {"matchExpressions": [{"key": "other", "values": ["x"]}]}},
                        {"preference": {"matchExpressions": [{"key": "node-fleet", "values": ["p6-b200"]}]}},
                    ]
                }
            }
        }
        assert _extract_workflow_fleet(spec) == "p6-b200"

    def test_returns_none_when_missing(self):
        assert _extract_workflow_fleet({}) is None
        assert _extract_workflow_fleet({"affinity": {}}) is None


class TestExtractRunnerClass:
    def test_release_runner(self):
        spec = {
            "affinity": {
                "nodeAffinity": {
                    "requiredDuringSchedulingIgnoredDuringExecution": {
                        "nodeSelectorTerms": [
                            {
                                "matchExpressions": [
                                    {
                                        "key": "osdc.io/runner-class",
                                        "operator": "In",
                                        "values": ["release"],
                                    }
                                ]
                            }
                        ]
                    }
                }
            }
        }
        assert _extract_runner_class(spec) == "release"

    def test_does_not_exist_returns_none(self):
        spec = {
            "affinity": {
                "nodeAffinity": {
                    "requiredDuringSchedulingIgnoredDuringExecution": {
                        "nodeSelectorTerms": [
                            {"matchExpressions": [{"key": "osdc.io/runner-class", "operator": "DoesNotExist"}]}
                        ]
                    }
                }
            }
        }
        assert _extract_runner_class(spec) is None

    def test_missing_returns_none(self):
        assert _extract_runner_class({}) is None


# ---------------------------------------------------------------------------
# load_nodepools — both schemas, region filter, release expansion
# ---------------------------------------------------------------------------


def _write_nodepools_defs(root: Path, files: dict[str, str]) -> None:
    defs = root / "modules" / "nodepools" / "defs"
    defs.mkdir(parents=True, exist_ok=True)
    for name, content in files.items():
        (defs / name).write_text(textwrap.dedent(content))


def _write_b200_defs(root: Path, files: dict[str, str]) -> None:
    defs = root / "modules" / "nodepools-b200" / "defs"
    defs.mkdir(parents=True, exist_ok=True)
    for name, content in files.items():
        (defs / name).write_text(textwrap.dedent(content))


class TestLoadNodepools:
    def test_fleet_schema_with_release(self, tmp_path):
        _write_nodepools_defs(
            tmp_path,
            {
                "m8g.yaml": """
                    fleet:
                      name: m8g
                      arch: arm64
                      gpu: false
                      instances:
                        - type: m8g.48xlarge
                          weight: 100
                          node_disk_size: 7000
                        - type: m8g.24xlarge
                          weight: 75
                          node_disk_size: 3500
                      release:
                        - type: m8g.48xlarge
                          weight: 100
                          node_disk_size: 7000
                """,
            },
        )
        entries = load_nodepools(["nodepools"], region="us-east-2", upstream_root=tmp_path)
        names = sorted(e.name for e in entries)
        assert names == ["m8g-24xlarge", "m8g-48xlarge", "m8g-48xlarge-release"]

        rel = next(e for e in entries if e.runner_class == "release")
        assert rel.fleet == "m8g"
        assert rel.instance_type == "m8g.48xlarge"
        assert rel.arch == "arm64"
        assert rel.gpu is False

        non_rel = [e for e in entries if e.runner_class is None]
        assert all(e.fleet == "m8g" for e in non_rel)

    def test_fleets_list_schema(self, tmp_path):
        # Multi-fleet single file (fleets: list) — synthetic since no current file uses it.
        _write_nodepools_defs(
            tmp_path,
            {
                "multi.yaml": """
                    fleets:
                      - name: c7i
                        arch: amd64
                        gpu: false
                        instances:
                          - type: c7i.48xlarge
                            weight: 100
                            node_disk_size: 100
                      - name: m8g
                        arch: arm64
                        gpu: false
                        instances:
                          - type: m8g.24xlarge
                            weight: 75
                            node_disk_size: 100
                """,
            },
        )
        entries = load_nodepools(["nodepools"], region="us-east-2", upstream_root=tmp_path)
        names = sorted(e.name for e in entries)
        assert names == ["c7i-48xlarge", "m8g-24xlarge"]

    def test_legacy_nodepool_schema_b200(self, tmp_path):
        _write_b200_defs(
            tmp_path,
            {
                "p6-b200-48xlarge.yaml": """
                    nodepool:
                      name: p6-b200-48xlarge
                      instance_type: p6-b200.48xlarge
                      arch: amd64
                      node_disk_size: 100
                      gpu: true
                      has_nvme: true
                """,
            },
        )
        entries = load_nodepools(["nodepools-b200"], region="us-east-2", upstream_root=tmp_path)
        assert len(entries) == 1
        e = entries[0]
        assert e.name == "p6-b200-48xlarge"
        # Fleet derives from the instance_type prefix — dash preserved.
        assert e.fleet == "p6-b200"
        assert e.instance_type == "p6-b200.48xlarge"
        assert e.arch == "amd64"
        assert e.gpu is True
        assert e.runner_class is None

    def test_region_filter_skips_excluded_fleet(self, tmp_path):
        _write_nodepools_defs(
            tmp_path,
            {
                "g5.yaml": """
                    fleet:
                      name: g5
                      arch: amd64
                      gpu: true
                      exclude_regions:
                        - us-west-1
                      instances:
                        - type: g5.48xlarge
                          weight: 100
                          node_disk_size: 600
                """,
            },
        )
        # In us-west-1 → skipped
        west = load_nodepools(["nodepools"], region="us-west-1", upstream_root=tmp_path)
        assert west == []
        # Elsewhere → included
        east = load_nodepools(["nodepools"], region="us-east-2", upstream_root=tmp_path)
        assert len(east) == 1
        assert east[0].fleet == "g5"

    def test_b200_module_not_enabled_skips_dir(self, tmp_path):
        # Only nodepools-b200 has defs but the module list omits it.
        _write_b200_defs(
            tmp_path,
            {
                "p6-b200-48xlarge.yaml": """
                    nodepool:
                      name: p6-b200-48xlarge
                      instance_type: p6-b200.48xlarge
                      arch: amd64
                      node_disk_size: 100
                      gpu: true
                """,
            },
        )
        entries = load_nodepools(["nodepools"], region="us-east-2", upstream_root=tmp_path)
        assert entries == []


# ---------------------------------------------------------------------------
# load_runners — synthetic generated/ files
# ---------------------------------------------------------------------------


def _runner_helm_doc(*, name: str, scale_set: str, runner_cpu: str, runner_mem: str) -> dict:
    return {
        "githubConfigUrl": "https://github.com/foo/bar",
        "githubConfigSecret": "secret",
        "runnerScaleSetName": scale_set,
        "template": {
            "spec": {
                "nodeSelector": {"workload-type": "github-runner", "node-fleet": "c7i-runner"},
                "containers": [
                    {
                        "name": "runner",
                        "resources": {
                            "requests": {"cpu": runner_cpu, "memory": runner_mem},
                            "limits": {"cpu": runner_cpu, "memory": runner_mem},
                        },
                    }
                ],
            }
        },
    }


def _workflow_pod_yaml(*, fleet: str, cpu: str, mem: str, gpu: int = 0, runner_class: str | None = None) -> str:
    runner_class_expr: dict
    if runner_class:
        runner_class_expr = {
            "key": "osdc.io/runner-class",
            "operator": "In",
            "values": [runner_class],
        }
    else:
        runner_class_expr = {"key": "osdc.io/runner-class", "operator": "DoesNotExist"}
    requests: dict[str, str | int] = {"cpu": cpu, "memory": mem}
    if gpu:
        requests["nvidia.com/gpu"] = gpu
    pod = {
        "metadata": {"annotations": {"karpenter.sh/do-not-disrupt": "true"}},
        "spec": {
            "serviceAccountName": "arc-workflow",
            "affinity": {
                "nodeAffinity": {
                    "requiredDuringSchedulingIgnoredDuringExecution": {
                        "nodeSelectorTerms": [{"matchExpressions": [runner_class_expr]}]
                    },
                    "preferredDuringSchedulingIgnoredDuringExecution": [
                        {
                            "weight": 50,
                            "preference": {
                                "matchExpressions": [
                                    {"key": "node-fleet", "operator": "In", "values": [fleet]},
                                    {"key": "workload-type", "operator": "In", "values": ["github-runner"]},
                                ]
                            },
                        }
                    ],
                }
            },
            "containers": [
                {
                    "name": "$job",
                    "resources": {"requests": requests, "limits": requests},
                }
            ],
        },
    }
    return yaml.safe_dump(pod)


def _configmap_doc(
    *, runner_name: str, fleet: str, cpu: str, mem: str, gpu: int = 0, runner_class: str | None = None
) -> dict:
    return {
        "apiVersion": "v1",
        "kind": "ConfigMap",
        "metadata": {"name": f"arc-runner-hook-{runner_name}", "namespace": "arc-runners"},
        "data": {
            "job-pod.yaml": _workflow_pod_yaml(fleet=fleet, cpu=cpu, mem=mem, gpu=gpu, runner_class=runner_class),
        },
    }


def _write_runner_def(root: Path, *, runner_name: str, instance_type: str, runner_class: str | None = None) -> None:
    defs = root / "modules" / "arc-runners" / "defs"
    defs.mkdir(parents=True, exist_ok=True)
    runner_block: dict = {
        "name": runner_name,
        "instance_type": instance_type,
        "vcpu": 6,
        "memory": "29Gi",
        "gpu": 0,
    }
    if runner_class:
        runner_block["runner_class"] = runner_class
    (defs / f"{runner_name}.yaml").write_text(yaml.safe_dump({"runner": runner_block}))


def _write_generated_runner(
    root: Path,
    *,
    runner_name: str,
    fleet: str,
    workflow_cpu: str,
    workflow_mem: str,
    runner_cpu: str = "750m",
    runner_mem: str = "512Mi",
    scale_set: str | None = None,
    workflow_gpu: int = 0,
    runner_class: str | None = None,
) -> None:
    gen = root / "modules" / "arc-runners" / "generated"
    gen.mkdir(parents=True, exist_ok=True)
    helm = _runner_helm_doc(
        name=runner_name,
        scale_set=scale_set or f"c-mt-{runner_name}",
        runner_cpu=runner_cpu,
        runner_mem=runner_mem,
    )
    cm = _configmap_doc(
        runner_name=runner_name,
        fleet=fleet,
        cpu=workflow_cpu,
        mem=workflow_mem,
        gpu=workflow_gpu,
        runner_class=runner_class,
    )
    out = yaml.safe_dump(helm) + "---\n" + yaml.safe_dump(cm)
    (gen / f"{runner_name}.yaml").write_text(out)


class TestLoadRunners:
    def test_extracts_pod_sizes_and_fleet(self, tmp_path):
        _write_runner_def(tmp_path, runner_name="r1", instance_type="m8g.48xlarge")
        _write_generated_runner(
            tmp_path,
            runner_name="r1",
            fleet="m8g",
            workflow_cpu="6",
            workflow_mem="29Gi",
        )
        runners = load_runners(["arc-runners"], upstream_root=tmp_path)
        assert len(runners) == 1
        r = runners[0]
        assert r.name == "r1"
        assert r.scale_set_name == "c-mt-r1"
        assert r.instance_type == "m8g.48xlarge"
        assert r.workflow_fleet == "m8g"
        assert r.runner_pod_cpu_m == 750
        assert r.runner_pod_mem_mi == 512
        assert r.workflow_pod_cpu_m == 6000
        assert r.workflow_pod_mem_mi == 29 * 1024
        assert r.workflow_pod_gpu == 0
        assert r.runner_class is None

    def test_extracts_gpu_and_runner_class(self, tmp_path):
        _write_runner_def(tmp_path, runner_name="rel-r1", instance_type="m8g.48xlarge", runner_class="release")
        _write_generated_runner(
            tmp_path,
            runner_name="rel-r1",
            fleet="m8g",
            workflow_cpu="16",
            workflow_mem="62Gi",
            workflow_gpu=2,
            runner_class="release",
        )
        runners = load_runners(["arc-runners"], upstream_root=tmp_path)
        assert len(runners) == 1
        r = runners[0]
        assert r.runner_class == "release"
        assert r.workflow_pod_gpu == 2
        assert r.workflow_pod_cpu_m == 16000
        assert r.workflow_pod_mem_mi == 62 * 1024

    def test_skips_module_when_disabled(self, tmp_path):
        _write_runner_def(tmp_path, runner_name="r1", instance_type="m8g.48xlarge")
        _write_generated_runner(tmp_path, runner_name="r1", fleet="m8g", workflow_cpu="6", workflow_mem="29Gi")
        # arc-runners not in module list → no runners returned.
        runners = load_runners(["nodepools"], upstream_root=tmp_path)
        assert runners == []

    def test_falls_back_when_def_missing(self, tmp_path):
        # Generate without a corresponding def — instance_type falls back.
        _write_generated_runner(tmp_path, runner_name="orphan", fleet="m8g", workflow_cpu="6", workflow_mem="29Gi")
        runners = load_runners(["arc-runners"], upstream_root=tmp_path)
        assert len(runners) == 1
        # Workflow fleet still parses — instance_type uses synthetic fallback.
        assert runners[0].workflow_fleet == "m8g"
        assert runners[0].instance_type.startswith("m8g.")


# ---------------------------------------------------------------------------
# Schedulability
# ---------------------------------------------------------------------------


def _make_runner(**overrides) -> RunnerEntry:
    base = {
        "name": "r1",
        "scale_set_name": "c-mt-r1",
        "instance_type": "m8g.48xlarge",
        "workflow_fleet": "m8g",
        "runner_class": None,
        "runner_pod_cpu_m": 750,
        "runner_pod_mem_mi": 512,
        "workflow_pod_cpu_m": 6000,
        "workflow_pod_mem_mi": 29 * 1024,
        "workflow_pod_gpu": 0,
        "schedulable": True,
        "schedulable_reason": None,
    }
    base.update(overrides)
    return RunnerEntry(**base)


def _make_pool(**overrides) -> NodePoolEntry:
    base = {
        "name": "m8g-48xlarge",
        "fleet": "m8g",
        "instance_type": "m8g.48xlarge",
        "arch": "arm64",
        "gpu": False,
        "runner_class": None,
    }
    base.update(overrides)
    return NodePoolEntry(**base)


class TestSchedulability:
    def test_all_satisfied(self):
        runners = [_make_runner()]
        nodepools = [_make_pool()]
        _mark_schedulability(runners, nodepools, {"m8g"})
        assert runners[0].schedulable is True
        assert runners[0].schedulable_reason is None

    def test_workflow_fleet_missing(self):
        runners = [_make_runner(workflow_fleet="r7g")]
        _mark_schedulability(runners, [], {"m8g"})
        assert runners[0].schedulable is False
        assert "r7g" in runners[0].schedulable_reason
        assert "m8g" in runners[0].schedulable_reason

    def test_release_without_release_pool(self):
        runners = [_make_runner(runner_class="release")]
        nodepools = [_make_pool()]  # no release variant
        _mark_schedulability(runners, nodepools, {"m8g"})
        assert runners[0].schedulable is False
        assert "release" in runners[0].schedulable_reason

    def test_release_with_release_pool(self):
        runners = [_make_runner(runner_class="release")]
        nodepools = [
            _make_pool(),
            _make_pool(name="m8g-48xlarge-release", runner_class="release"),
        ]
        _mark_schedulability(runners, nodepools, {"m8g"})
        assert runners[0].schedulable is True


# ---------------------------------------------------------------------------
# resolve_cluster end-to-end
# ---------------------------------------------------------------------------


def _write_clusters_yaml(root: Path, content: dict) -> None:
    (root / "clusters.yaml").write_text(yaml.safe_dump(content))


class TestResolveCluster:
    def test_full_topology(self, tmp_path, capsys):
        # Cluster "test-cluster" deploys nodepools + arc-runners in us-east-2.
        _write_clusters_yaml(
            tmp_path,
            {
                "clusters": {
                    "test-cluster": {
                        "region": "us-east-2",
                        "modules": ["nodepools", "arc-runners"],
                    }
                }
            },
        )
        # Two nodepool fleets: c7i-runner (runner pool) + m8g (workflow pool with release).
        _write_nodepools_defs(
            tmp_path,
            {
                "c7i-runner.yaml": """
                    fleet:
                      name: c7i-runner
                      arch: amd64
                      gpu: false
                      instances:
                        - type: c7i.48xlarge
                          weight: 100
                          node_disk_size: 100
                """,
                "m8g.yaml": """
                    fleet:
                      name: m8g
                      arch: arm64
                      gpu: false
                      instances:
                        - type: m8g.48xlarge
                          weight: 100
                          node_disk_size: 100
                      release:
                        - type: m8g.48xlarge
                          weight: 100
                          node_disk_size: 100
                """,
            },
        )
        # Runners: one regular (m8g), one release (m8g release), one orphan (no fleet support).
        _write_runner_def(tmp_path, runner_name="ok", instance_type="m8g.48xlarge")
        _write_generated_runner(tmp_path, runner_name="ok", fleet="m8g", workflow_cpu="6", workflow_mem="29Gi")
        _write_runner_def(tmp_path, runner_name="rel", instance_type="m8g.48xlarge", runner_class="release")
        _write_generated_runner(
            tmp_path,
            runner_name="rel",
            fleet="m8g",
            workflow_cpu="16",
            workflow_mem="62Gi",
            runner_class="release",
        )
        _write_runner_def(tmp_path, runner_name="orphan", instance_type="r7g.48xlarge")
        _write_generated_runner(
            tmp_path,
            runner_name="orphan",
            fleet="r7g",
            workflow_cpu="6",
            workflow_mem="29Gi",
        )

        topo = resolve_cluster("test-cluster", upstream_root=tmp_path, consumer_root=tmp_path)
        assert isinstance(topo, ClusterTopology)
        assert topo.cluster_id == "test-cluster"
        assert topo.region == "us-east-2"
        assert topo.modules == ["nodepools", "arc-runners"]
        assert topo.runner_pool_fleet == "c7i-runner"
        assert topo.workflow_pool_fleets == {"m8g"}

        by_name = {r.name: r for r in topo.runners}
        assert by_name["ok"].schedulable is True
        assert by_name["rel"].schedulable is True
        assert by_name["orphan"].schedulable is False
        assert "r7g" in by_name["orphan"].schedulable_reason

        pool_names = sorted(p.name for p in topo.nodepools)
        assert "c7i-runner-48xlarge" in pool_names
        assert "m8g-48xlarge" in pool_names
        assert "m8g-48xlarge-release" in pool_names

    def test_no_runner_pool_warns(self, tmp_path, capsys):
        _write_clusters_yaml(
            tmp_path,
            {
                "clusters": {
                    "tiny": {
                        "region": "us-east-2",
                        "modules": ["nodepools"],
                    }
                }
            },
        )
        _write_nodepools_defs(
            tmp_path,
            {
                "m8g.yaml": """
                    fleet:
                      name: m8g
                      arch: arm64
                      gpu: false
                      instances:
                        - type: m8g.48xlarge
                          weight: 100
                          node_disk_size: 100
                """,
            },
        )
        topo = resolve_cluster("tiny", upstream_root=tmp_path, consumer_root=tmp_path)
        assert topo.runner_pool_fleet is None
        assert topo.workflow_pool_fleets == {"m8g"}
        captured = capsys.readouterr()
        assert "no 'c7i-runner'" in captured.out

    def test_unknown_cluster_raises(self, tmp_path):
        _write_clusters_yaml(tmp_path, {"clusters": {"only-one": {"region": "x", "modules": []}}})
        with pytest.raises(KeyError, match="missing-cluster"):
            resolve_cluster("missing-cluster", upstream_root=tmp_path, consumer_root=tmp_path)

    def test_missing_clusters_yaml_raises(self, tmp_path):
        with pytest.raises(FileNotFoundError):
            resolve_cluster("foo", upstream_root=tmp_path, consumer_root=tmp_path)
