"""Tests for runner_fleet_validator — runner def vs nodepool def consistency."""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml
from runner_fleet_validator import validate_cluster_runner_fleets

REPO_ROOT = Path(__file__).resolve().parents[2]
CLUSTERS_YAML_PATH = REPO_ROOT / "clusters.yaml"


def _load_clusters_yaml() -> dict:
    with open(CLUSTERS_YAML_PATH) as f:
        return yaml.safe_load(f)


def pytest_generate_tests(metafunc):
    if "live_cluster_id" in metafunc.fixturenames:
        try:
            clusters_yaml = _load_clusters_yaml()
            cluster_ids = sorted((clusters_yaml.get("clusters") or {}).keys())
        except (OSError, yaml.YAMLError) as e:
            metafunc.parametrize(
                "live_cluster_id",
                [pytest.param(None, marks=pytest.mark.skip(reason=f"cannot load clusters.yaml: {e}"))],
            )
            return
        metafunc.parametrize("live_cluster_id", cluster_ids, ids=cluster_ids)


def test_repo_cluster_runners_have_matching_fleets(live_cluster_id: str):
    clusters_yaml = _load_clusters_yaml()
    errors = validate_cluster_runner_fleets(
        cluster_id=live_cluster_id,
        clusters_yaml=clusters_yaml,
        upstream_dir=REPO_ROOT,
    )
    assert errors == [], "\n".join(errors)


# Fixture helpers --------------------------------------------------------------


def _make_upstream(tmp_path: Path) -> Path:
    upstream = tmp_path / "upstream"
    (upstream / "modules" / "nodepools" / "scripts" / "python").mkdir(parents=True)
    (upstream / "modules" / "arc-runners" / "scripts" / "python").mkdir(parents=True)
    return upstream


def _write_yaml(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.dump(data))


def _nodepool_fleet(name: str, instance_type: str, **extra) -> dict:
    fleet = {
        "name": name,
        "arch": "amd64",
        "gpu": True,
        "instances": [{"type": instance_type, "weight": 100, "node_disk_size": 600, "has_nvme": True}],
    }
    fleet.update(extra)
    return {"fleet": fleet}


def _legacy_nodepool(name: str, instance_type: str, **extra) -> dict:
    nodepool = {"name": name, "instance_type": instance_type, "arch": "amd64", "node_disk_size": 100, "gpu": True}
    nodepool.update(extra)
    return {"nodepool": nodepool}


def _runner(name: str, instance_type: str, node_fleet: str | None = None) -> dict:
    runner = {
        "name": name,
        "instance_type": instance_type,
        "disk_size": 150,
        "vcpu": 4,
        "memory": "8Gi",
        "gpu": 0,
        "max_runners": 1,
        "min_runners": 0,
        "proactive_capacity": 0,
    }
    if node_fleet is not None:
        runner["node_fleet"] = node_fleet
    return {"runner": runner}


def _clusters_yaml(modules: list[str], region: str = "us-east-2", defaults: dict | None = None) -> dict:
    cfg: dict = {"cluster_name": "test", "state_bucket": "b", "modules": modules}
    if region is not None:
        cfg["region"] = region
    return {"defaults": defaults or {}, "clusters": {"test": cfg}}


# Unit tests -------------------------------------------------------------------


def test_happy_path_family_match(tmp_path):
    upstream = _make_upstream(tmp_path)
    _write_yaml(upstream / "modules" / "nodepools" / "defs" / "g5.yaml", _nodepool_fleet("g5", "g5.8xlarge"))
    _write_yaml(upstream / "modules" / "arc-runners" / "defs" / "r.yaml", _runner("r", "g5.8xlarge"))

    errors = validate_cluster_runner_fleets(
        "test",
        _clusters_yaml(["nodepools", "arc-runners"]),
        upstream,
    )
    assert errors == []


def test_override_happy_path(tmp_path):
    upstream = _make_upstream(tmp_path)
    _write_yaml(
        upstream / "modules" / "nodepools" / "defs" / "g5-large.yaml", _nodepool_fleet("g5-large", "g5.48xlarge")
    )
    _write_yaml(
        upstream / "modules" / "arc-runners" / "defs" / "big.yaml",
        _runner("big", "g5.48xlarge", node_fleet="g5-large"),
    )

    errors = validate_cluster_runner_fleets(
        "test",
        _clusters_yaml(["nodepools", "arc-runners"]),
        upstream,
    )
    assert errors == []


def test_typo_override_reports_orphan(tmp_path):
    upstream = _make_upstream(tmp_path)
    _write_yaml(
        upstream / "modules" / "nodepools" / "defs" / "g5-large.yaml", _nodepool_fleet("g5-large", "g5.48xlarge")
    )
    _write_yaml(
        upstream / "modules" / "arc-runners" / "defs" / "typo.yaml",
        _runner("typo-runner", "g5.48xlarge", node_fleet="g5-xlarge"),
    )

    errors = validate_cluster_runner_fleets(
        "test",
        _clusters_yaml(["nodepools", "arc-runners"]),
        upstream,
    )
    assert len(errors) == 1
    assert "typo-runner" in errors[0]
    assert "g5-xlarge" in errors[0]


def test_family_fallback_miss(tmp_path):
    upstream = _make_upstream(tmp_path)
    _write_yaml(upstream / "modules" / "nodepools" / "defs" / "g5.yaml", _nodepool_fleet("g5", "g5.8xlarge"))
    _write_yaml(
        upstream / "modules" / "arc-runners" / "defs" / "orph.yaml",
        _runner("orph", "madeup.xlarge"),
    )

    errors = validate_cluster_runner_fleets(
        "test",
        _clusters_yaml(["nodepools", "arc-runners"]),
        upstream,
    )
    assert len(errors) == 1
    assert "orph" in errors[0]
    assert "madeup" in errors[0]


def test_excluded_instance_type_is_skipped(tmp_path):
    upstream = _make_upstream(tmp_path)
    _write_yaml(
        upstream / "modules" / "nodepools" / "defs" / "g5.yaml",
        _nodepool_fleet("g5", "g5.48xlarge", exclude_regions=["us-west-1"]),
    )
    _write_yaml(
        upstream / "modules" / "arc-runners" / "defs" / "excl.yaml",
        _runner("excl-runner", "g5.48xlarge"),
    )

    errors = validate_cluster_runner_fleets(
        "test",
        _clusters_yaml(["nodepools", "arc-runners"], region="us-west-1"),
        upstream,
    )
    assert errors == []


def test_cluster_without_arc_runners_returns_empty(tmp_path):
    upstream = _make_upstream(tmp_path)
    _write_yaml(upstream / "modules" / "nodepools" / "defs" / "g5.yaml", _nodepool_fleet("g5", "g5.8xlarge"))

    errors = validate_cluster_runner_fleets(
        "test",
        _clusters_yaml(["nodepools", "karpenter"]),
        upstream,
    )
    assert errors == []


def test_arc_runners_without_nodepools_hard_fails(tmp_path):
    upstream = _make_upstream(tmp_path)
    _write_yaml(upstream / "modules" / "arc-runners" / "defs" / "r.yaml", _runner("r", "g5.8xlarge"))

    errors = validate_cluster_runner_fleets(
        "test",
        _clusters_yaml(["arc-runners"]),
        upstream,
    )
    assert len(errors) == 1
    assert "no nodepools" in errors[0]
    assert "arc-runners" in errors[0]


def test_reserved_name_override_rejected(tmp_path):
    upstream = _make_upstream(tmp_path)
    _write_yaml(upstream / "modules" / "nodepools" / "defs" / "c7i.yaml", _nodepool_fleet("c7i", "c7i.48xlarge"))
    _write_yaml(
        upstream / "modules" / "arc-runners" / "defs" / "bad.yaml",
        _runner("bad-runner", "c7i.48xlarge", node_fleet="c7i-runner"),
    )

    errors = validate_cluster_runner_fleets(
        "test",
        _clusters_yaml(["nodepools", "arc-runners"]),
        upstream,
    )
    assert len(errors) == 1
    assert "bad-runner" in errors[0]
    assert "invalid override" in errors[0]


def test_fleet_collision_across_modules(tmp_path):
    upstream = _make_upstream(tmp_path)
    _write_yaml(upstream / "modules" / "nodepools" / "defs" / "g5.yaml", _nodepool_fleet("g5", "g5.8xlarge"))
    _write_yaml(upstream / "modules" / "nodepools-h100" / "defs" / "g5.yaml", _nodepool_fleet("g5", "g5.12xlarge"))
    _write_yaml(upstream / "modules" / "arc-runners" / "defs" / "r.yaml", _runner("r", "g5.8xlarge"))

    errors = validate_cluster_runner_fleets(
        "test",
        _clusters_yaml(["nodepools", "nodepools-h100", "arc-runners"]),
        upstream,
    )
    collision = [e for e in errors if "collision" in e]
    assert len(collision) == 1
    assert "'g5'" in collision[0]


def test_multi_module_nodepool_fleet_resolved(tmp_path):
    upstream = _make_upstream(tmp_path)
    _write_yaml(upstream / "modules" / "nodepools" / "defs" / "g5.yaml", _nodepool_fleet("g5", "g5.8xlarge"))
    _write_yaml(
        upstream / "modules" / "nodepools-h100" / "defs" / "p5.yaml", _legacy_nodepool("p5-48xlarge", "p5.48xlarge")
    )
    _write_yaml(
        upstream / "modules" / "arc-runners" / "defs" / "h100.yaml",
        _runner("h100-r", "p5.48xlarge", node_fleet="p5"),
    )

    errors = validate_cluster_runner_fleets(
        "test",
        _clusters_yaml(["nodepools", "nodepools-h100", "arc-runners"]),
        upstream,
    )
    assert errors == []


def test_multi_module_arc_runners_both_validated(tmp_path):
    upstream = _make_upstream(tmp_path)
    _write_yaml(upstream / "modules" / "nodepools" / "defs" / "g5.yaml", _nodepool_fleet("g5", "g5.8xlarge"))
    _write_yaml(
        upstream / "modules" / "nodepools-h100" / "defs" / "p5.yaml", _legacy_nodepool("p5-48xlarge", "p5.48xlarge")
    )
    _write_yaml(upstream / "modules" / "arc-runners" / "defs" / "ok.yaml", _runner("ok", "g5.8xlarge"))
    _write_yaml(
        upstream / "modules" / "arc-runners-h100" / "defs" / "bad.yaml",
        _runner("h100-bad", "p5.48xlarge", node_fleet="p5-missing"),
    )

    errors = validate_cluster_runner_fleets(
        "test",
        _clusters_yaml(["nodepools", "nodepools-h100", "arc-runners", "arc-runners-h100"]),
        upstream,
    )
    assert len(errors) == 1
    assert "h100-bad" in errors[0]
    assert "p5-missing" in errors[0]


def test_consumer_fork_runner_def_is_checked(tmp_path):
    upstream = _make_upstream(tmp_path)
    consumer = tmp_path / "consumer"
    _write_yaml(upstream / "modules" / "nodepools" / "defs" / "g5.yaml", _nodepool_fleet("g5", "g5.8xlarge"))
    _write_yaml(
        consumer / "modules" / "arc-runners" / "defs" / "consumer-only.yaml",
        _runner("consumer-runner", "nosuch.xlarge"),
    )

    errors = validate_cluster_runner_fleets(
        "test",
        _clusters_yaml(["nodepools", "arc-runners"]),
        upstream,
        consumer_root=consumer,
    )
    assert len(errors) == 1
    assert "consumer-runner" in errors[0]


def test_missing_cluster_id_returns_error(tmp_path):
    upstream = _make_upstream(tmp_path)
    errors = validate_cluster_runner_fleets(
        "absent",
        _clusters_yaml(["nodepools", "arc-runners"]),
        upstream,
    )
    assert len(errors) == 1
    assert "not found" in errors[0]
    assert "absent" in errors[0]


def test_region_from_defaults_when_cluster_omits(tmp_path):
    upstream = _make_upstream(tmp_path)
    _write_yaml(
        upstream / "modules" / "nodepools" / "defs" / "g5.yaml",
        _nodepool_fleet("g5", "g5.48xlarge", exclude_regions=["us-west-1"]),
    )
    _write_yaml(
        upstream / "modules" / "arc-runners" / "defs" / "r.yaml",
        _runner("r", "g5.48xlarge"),
    )

    clusters = _clusters_yaml(["nodepools", "arc-runners"], region=None, defaults={"region": "us-west-1"})
    clusters["clusters"]["test"].pop("region", None)
    errors = validate_cluster_runner_fleets("test", clusters, upstream)
    assert errors == []


def test_runner_def_missing_required_fields_ignored(tmp_path):
    upstream = _make_upstream(tmp_path)
    _write_yaml(upstream / "modules" / "nodepools" / "defs" / "g5.yaml", _nodepool_fleet("g5", "g5.8xlarge"))
    _write_yaml(upstream / "modules" / "arc-runners" / "defs" / "empty.yaml", {"runner": {}})
    _write_yaml(upstream / "modules" / "arc-runners" / "defs" / "non-dict.yaml", {"runner": "junk"})

    errors = validate_cluster_runner_fleets(
        "test",
        _clusters_yaml(["nodepools", "arc-runners"]),
        upstream,
    )
    assert errors == []


def test_malformed_runner_yaml_reports_parse_error(tmp_path):
    upstream = _make_upstream(tmp_path)
    _write_yaml(upstream / "modules" / "nodepools" / "defs" / "g5.yaml", _nodepool_fleet("g5", "g5.8xlarge"))
    bad = upstream / "modules" / "arc-runners" / "defs" / "broken.yaml"
    bad.parent.mkdir(parents=True, exist_ok=True)
    bad.write_text("runner: {name: x,\n  bad")

    errors = validate_cluster_runner_fleets(
        "test",
        _clusters_yaml(["nodepools", "arc-runners"]),
        upstream,
    )
    assert len(errors) == 1
    assert "YAML parse error" in errors[0]


def test_malformed_nodepool_yaml_reports_parse_error(tmp_path):
    upstream = _make_upstream(tmp_path)
    bad = upstream / "modules" / "nodepools" / "defs" / "broken.yaml"
    bad.parent.mkdir(parents=True, exist_ok=True)
    bad.write_text("fleet: {name: x,\n  bad")
    _write_yaml(upstream / "modules" / "arc-runners" / "defs" / "r.yaml", _runner("r", "g5.8xlarge"))

    errors = validate_cluster_runner_fleets(
        "test",
        _clusters_yaml(["nodepools", "arc-runners"]),
        upstream,
    )
    parse = [e for e in errors if "YAML parse error" in e]
    assert len(parse) == 1


def test_fleets_list_format_supported(tmp_path):
    upstream = _make_upstream(tmp_path)
    _write_yaml(
        upstream / "modules" / "nodepools" / "defs" / "multi.yaml",
        {
            "fleets": [
                {
                    "name": "fa",
                    "arch": "amd64",
                    "gpu": True,
                    "instances": [{"type": "g5.8xlarge", "weight": 100, "node_disk_size": 600, "has_nvme": True}],
                },
            ]
        },
    )
    _write_yaml(
        upstream / "modules" / "arc-runners" / "defs" / "r.yaml",
        _runner("r", "g5.8xlarge", node_fleet="fa"),
    )

    errors = validate_cluster_runner_fleets(
        "test",
        _clusters_yaml(["nodepools", "arc-runners"]),
        upstream,
    )
    assert errors == []


def test_consumer_root_same_as_upstream_not_duplicated(tmp_path):
    upstream = _make_upstream(tmp_path)
    _write_yaml(upstream / "modules" / "nodepools" / "defs" / "g5.yaml", _nodepool_fleet("g5", "g5.8xlarge"))
    _write_yaml(upstream / "modules" / "arc-runners" / "defs" / "r.yaml", _runner("r", "g5.8xlarge"))

    errors = validate_cluster_runner_fleets(
        "test",
        _clusters_yaml(["nodepools", "arc-runners"]),
        upstream,
        consumer_root=upstream,
    )
    assert errors == []
