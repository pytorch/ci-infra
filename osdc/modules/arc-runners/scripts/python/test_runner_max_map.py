"""Tests for runner_max_map.py."""

from __future__ import annotations

import json
import textwrap
from typing import TYPE_CHECKING

import pytest
import yaml
from runner_max_map import (
    MAX_INT32,
    build_max_runners_map,
    compute_ars_name,
    enabled_arc_runner_modules,
    find_repo_root,
    get_cluster,
    iter_def_files,
    load_clusters_yaml,
    main,
    parse_def_file,
    resolve_dotted,
    resolve_runner_name_prefix,
)

if TYPE_CHECKING:
    from pathlib import Path

# ============================================================================
# Helpers — build a fake repo (clusters.yaml + modules/<m>/defs/*.yaml)
# ============================================================================


def make_repo(tmp_path: Path, clusters_yaml: dict, modules: dict[str, dict[str, dict]]) -> Path:
    """Create a fake repo layout under tmp_path.

    `modules` is {module_name: {def_basename: runner_dict}} where def_basename
    is the YAML filename without the suffix and runner_dict becomes the
    `runner:` mapping in the def file.
    """
    (tmp_path / "clusters.yaml").write_text(yaml.safe_dump(clusters_yaml))
    for module, defs in modules.items():
        defs_dir = tmp_path / "modules" / module / "defs"
        defs_dir.mkdir(parents=True, exist_ok=True)
        for basename, runner in defs.items():
            (defs_dir / f"{basename}.yaml").write_text(yaml.safe_dump({"runner": runner}))
    return tmp_path


# ============================================================================
# MAX_INT32 sanity
# ============================================================================


class TestMaxInt32:
    def test_value_matches_controller_substitution(self):
        # Mirrors controllers/actions.github.com/resourcebuilder.go:94-101 in the
        # actions-runner-controller fork: math.MaxInt32 is the canonical "no cap".
        assert MAX_INT32 == 2147483647


# ============================================================================
# compute_ars_name — chart's runnerScaleSetName -> metadata.name transform
# ============================================================================


class TestComputeArsName:
    def test_no_prefix(self):
        assert compute_ars_name("", "linux-cpu") == "linux-cpu"

    def test_with_prefix(self):
        assert compute_ars_name("mt-", "linux-cpu") == "mt-linux-cpu"

    def test_underscores_replaced_with_dashes(self):
        # Chart: `replace "_" "-"`. Confirmed in
        # charts/gha-runner-scale-set/templates/autoscalingrunnerset.yaml.
        assert compute_ars_name("c-mt-", "weird_name_with_underscores") == "c-mt-weird-name-with-underscores"

    def test_dots_NOT_replaced(self):
        # Chart only strips underscores. Dots pass through (and would fail the
        # k8s name validator at apply time, but that's a def author bug, not
        # something we should silently mask).
        assert compute_ars_name("", "name.with.dots") == "name.with.dots"

    def test_underscore_in_prefix(self):
        assert compute_ars_name("c_mt_", "x") == "c-mt-x"

    def test_empty_runner_name(self):
        # Pathological — the parser rejects this upstream, but compute_ars_name
        # is a pure function and should still produce a deterministic answer.
        assert compute_ars_name("p-", "") == "p-"


# ============================================================================
# find_repo_root
# ============================================================================


class TestFindRepoRoot:
    def test_finds_repo_root_from_nested_dir(self, tmp_path):
        (tmp_path / "clusters.yaml").write_text("clusters: {}\n")
        nested = tmp_path / "modules" / "arc-runners" / "scripts" / "python"
        nested.mkdir(parents=True)
        assert find_repo_root(nested) == tmp_path.resolve()

    def test_uses_OSDC_ROOT_when_set(self, tmp_path, monkeypatch):
        (tmp_path / "clusters.yaml").write_text("clusters: {}\n")
        monkeypatch.setenv("OSDC_ROOT", str(tmp_path))
        # Even though cwd has no clusters.yaml, OSDC_ROOT wins.
        unrelated = tmp_path.parent
        assert find_repo_root(unrelated) == tmp_path

    def test_OSDC_ROOT_without_clusters_yaml_raises(self, tmp_path, monkeypatch):
        monkeypatch.setenv("OSDC_ROOT", str(tmp_path))
        with pytest.raises(FileNotFoundError, match="OSDC_ROOT"):
            find_repo_root(tmp_path)

    def test_no_clusters_yaml_anywhere_raises(self, tmp_path, monkeypatch):
        monkeypatch.delenv("OSDC_ROOT", raising=False)
        # tmp_path is under /private/tmp or /tmp; ensure no clusters.yaml exists
        # in the walk path. We point at a freshly created subdir to be safe.
        sub = tmp_path / "some" / "deep" / "dir"
        sub.mkdir(parents=True)
        with pytest.raises(FileNotFoundError, match=r"clusters\.yaml not found"):
            find_repo_root(sub)


# ============================================================================
# load_clusters_yaml + get_cluster + resolve_dotted + resolve_runner_name_prefix
# ============================================================================


class TestLoadClustersYaml:
    def test_loads_valid_yaml(self, tmp_path):
        (tmp_path / "clusters.yaml").write_text("clusters:\n  arc-x: {}\n")
        result = load_clusters_yaml(tmp_path)
        assert result == {"clusters": {"arc-x": {}}}

    def test_empty_file_returns_empty_dict(self, tmp_path):
        (tmp_path / "clusters.yaml").write_text("")
        assert load_clusters_yaml(tmp_path) == {}


class TestGetCluster:
    def test_returns_cluster_dict(self):
        cy = {"clusters": {"arc-x": {"region": "us-east-1"}}}
        assert get_cluster(cy, "arc-x") == {"region": "us-east-1"}

    def test_unknown_cluster_raises(self):
        cy = {"clusters": {"arc-known": {}}}
        with pytest.raises(KeyError, match="Unknown cluster 'arc-missing'"):
            get_cluster(cy, "arc-missing")

    def test_unknown_cluster_with_no_clusters_section(self):
        with pytest.raises(KeyError, match="Unknown cluster 'arc-missing'"):
            get_cluster({}, "arc-missing")


class TestResolveDotted:
    def test_finds_in_cluster_cfg(self):
        assert resolve_dotted({"a": {"b": "x"}}, {}, "a.b") == "x"

    def test_falls_back_to_defaults(self):
        assert resolve_dotted({}, {"a": {"b": "y"}}, "a.b") == "y"

    def test_cluster_overrides_defaults(self):
        assert resolve_dotted({"a": {"b": "x"}}, {"a": {"b": "y"}}, "a.b") == "x"

    def test_returns_none_when_missing(self):
        assert resolve_dotted({}, {}, "a.b") is None

    def test_dashed_key(self):
        # arc-runners is the actual dict key; only literal dots in the dotpath
        # split the path. Underscores and dashes pass through unchanged.
        cfg = {"arc-runners": {"runner_name_prefix": "mt-"}}
        assert resolve_dotted(cfg, {}, "arc-runners.runner_name_prefix") == "mt-"


class TestResolveRunnerNamePrefix:
    def test_reads_per_cluster_value(self):
        cy = {
            "defaults": {},
            "clusters": {"arc-x": {"arc-runners": {"runner_name_prefix": "mt-"}}},
        }
        assert resolve_runner_name_prefix(cy, "arc-x") == "mt-"

    def test_falls_back_to_defaults_when_cluster_block_absent(self):
        # Whole-block resolution: cluster has NO arc-runners block at all,
        # so defaults' arc-runners block applies in full.
        cy = {
            "defaults": {"arc-runners": {"runner_name_prefix": "default-"}},
            "clusters": {"arc-x": {}},
        }
        assert resolve_runner_name_prefix(cy, "arc-x") == "default-"

    def test_returns_empty_string_when_missing(self):
        cy = {"defaults": {}, "clusters": {"arc-x": {}}}
        assert resolve_runner_name_prefix(cy, "arc-x") == ""

    def test_partial_override_does_NOT_inherit_prefix_from_defaults(self):
        # REGRESSION: divergence case between per-key and whole-block resolution.
        # Cluster supplies its own arc-runners block (overriding the whole dict),
        # but does NOT specify runner_name_prefix. Whole-block resolution
        # (mirroring generate_runners.py:332) returns the cluster's dict as a
        # single unit — defaults' runner_name_prefix is NOT inherited. Per-key
        # resolution would have wrongly returned "default-", causing
        # `resume-runners` to compute ARS names that don't exist on the cluster.
        cy = {
            "defaults": {"arc-runners": {"runner_name_prefix": "default-"}},
            "clusters": {
                "arc-x": {
                    "modules": ["arc-runners"],
                    "arc-runners": {"github_config_url": "https://x"},
                }
            },
        }
        assert resolve_runner_name_prefix(cy, "arc-x") == ""

    def test_consistent_block_with_prefix_returns_prefix(self):
        # Cluster supplies a complete arc-runners block including prefix —
        # both per-key and whole-block resolution produce the same answer.
        cy = {
            "defaults": {},
            "clusters": {"arc-x": {"arc-runners": {"runner_name_prefix": "c-"}}},
        }
        assert resolve_runner_name_prefix(cy, "arc-x") == "c-"

    def test_explicit_null_prefix_falls_through_to_empty(self):
        # YAML may render an explicit `runner_name_prefix:` (no value) as None.
        # Mirror generate_runners.py's behaviour (cluster_config.get(..., "")):
        # None should be treated as no prefix, not propagated.
        cy = {
            "defaults": {},
            "clusters": {"arc-x": {"arc-runners": {"runner_name_prefix": None}}},
        }
        assert resolve_runner_name_prefix(cy, "arc-x") == ""


# ============================================================================
# enabled_arc_runner_modules
# ============================================================================


class TestEnabledArcRunnerModules:
    def test_only_arc_runners(self):
        cy = {"clusters": {"arc-x": {"modules": ["karpenter", "arc-runners", "buildkit"]}}}
        assert enabled_arc_runner_modules(cy, "arc-x") == ["arc-runners"]

    def test_includes_variant_modules(self):
        cy = {
            "clusters": {
                "arc-x": {
                    "modules": [
                        "karpenter",
                        "arc-runners",
                        "arc-runners-h100",
                        "arc-runners-b200",
                        "buildkit",
                    ]
                }
            }
        }
        assert enabled_arc_runner_modules(cy, "arc-x") == [
            "arc-runners",
            "arc-runners-h100",
            "arc-runners-b200",
        ]

    def test_excludes_unrelated_modules(self):
        # `arc-runners-rel` (any future suffix) is included; `arc` alone is NOT.
        cy = {"clusters": {"arc-x": {"modules": ["arc", "arc-runners-rel"]}}}
        assert enabled_arc_runner_modules(cy, "arc-x") == ["arc-runners-rel"]

    def test_no_modules_section(self):
        cy = {"clusters": {"arc-x": {}}}
        assert enabled_arc_runner_modules(cy, "arc-x") == []

    def test_explicit_null_modules(self):
        # YAML may render an empty list as None; make sure we don't crash.
        cy = {"clusters": {"arc-x": {"modules": None}}}
        assert enabled_arc_runner_modules(cy, "arc-x") == []


# ============================================================================
# iter_def_files
# ============================================================================


class TestIterDefFiles:
    def test_returns_sorted_yaml_files(self, tmp_path):
        defs_dir = tmp_path / "modules" / "arc-runners" / "defs"
        defs_dir.mkdir(parents=True)
        (defs_dir / "b.yaml").write_text("runner: {}\n")
        (defs_dir / "a.yaml").write_text("runner: {}\n")
        (defs_dir / "c.yaml").write_text("runner: {}\n")
        result = iter_def_files(tmp_path, "arc-runners")
        assert [p.name for p in result] == ["a.yaml", "b.yaml", "c.yaml"]

    def test_missing_module_returns_empty(self, tmp_path):
        # The "module enabled but defs/ doesn't exist" case is tolerable —
        # caller logs a warning and continues. Do not raise.
        assert iter_def_files(tmp_path, "arc-runners-missing") == []

    def test_empty_defs_dir_returns_empty(self, tmp_path):
        (tmp_path / "modules" / "arc-runners" / "defs").mkdir(parents=True)
        assert iter_def_files(tmp_path, "arc-runners") == []

    def test_ignores_non_yaml_files(self, tmp_path):
        defs_dir = tmp_path / "modules" / "arc-runners" / "defs"
        defs_dir.mkdir(parents=True)
        (defs_dir / "valid.yaml").write_text("runner: {}\n")
        (defs_dir / "README.md").write_text("# notes\n")
        (defs_dir / "stash.bak").write_text("ignored\n")
        result = iter_def_files(tmp_path, "arc-runners")
        assert [p.name for p in result] == ["valid.yaml"]


# ============================================================================
# parse_def_file
# ============================================================================


class TestParseDefFile:
    def test_returns_name_instance_type_and_max_runners(self, tmp_path):
        f = tmp_path / "r.yaml"
        f.write_text(
            yaml.safe_dump({"runner": {"name": "linux-cpu", "instance_type": "c7i.4xlarge", "max_runners": 8}})
        )
        assert parse_def_file(f, "arc-x") == ("linux-cpu", "c7i.4xlarge", 8)

    def test_max_runners_omitted_returns_none(self, tmp_path):
        f = tmp_path / "r.yaml"
        f.write_text(yaml.safe_dump({"runner": {"name": "linux-cpu", "instance_type": "c7i.4xlarge"}}))
        name, instance_type, max_runners = parse_def_file(f, "arc-x")
        assert name == "linux-cpu"
        assert instance_type == "c7i.4xlarge"
        assert max_runners is None

    def test_instance_type_omitted_returns_none(self, tmp_path):
        f = tmp_path / "r.yaml"
        f.write_text(yaml.safe_dump({"runner": {"name": "linux-cpu", "max_runners": 4}}))
        assert parse_def_file(f, "arc-x") == ("linux-cpu", None, 4)

    def test_non_string_instance_type_raises(self, tmp_path):
        f = tmp_path / "r.yaml"
        f.write_text(yaml.safe_dump({"runner": {"name": "x", "instance_type": 42}}))
        with pytest.raises(ValueError, match=r"instance_type must be a string"):
            parse_def_file(f, "arc-x")

    def test_missing_runner_section_raises(self, tmp_path):
        f = tmp_path / "r.yaml"
        f.write_text("other_key: 1\n")
        with pytest.raises(ValueError, match=r"missing required 'runner\.name'"):
            parse_def_file(f, "arc-x")

    def test_missing_name_raises(self, tmp_path):
        f = tmp_path / "r.yaml"
        f.write_text(yaml.safe_dump({"runner": {"instance_type": "m5.large"}}))
        with pytest.raises(ValueError, match=r"missing required 'runner\.name'"):
            parse_def_file(f, "arc-x")

    def test_empty_name_raises(self, tmp_path):
        f = tmp_path / "r.yaml"
        f.write_text(yaml.safe_dump({"runner": {"name": ""}}))
        with pytest.raises(ValueError, match=r"missing required 'runner\.name'"):
            parse_def_file(f, "arc-x")

    def test_non_string_name_raises(self, tmp_path):
        f = tmp_path / "r.yaml"
        f.write_text(yaml.safe_dump({"runner": {"name": 42}}))
        with pytest.raises(ValueError, match=r"missing required 'runner\.name'"):
            parse_def_file(f, "arc-x")

    def test_zero_max_runners_raises(self, tmp_path):
        # Mirrors generate_runners.py:162 — max_runners must be POSITIVE.
        f = tmp_path / "r.yaml"
        f.write_text(yaml.safe_dump({"runner": {"name": "x", "max_runners": 0}}))
        with pytest.raises(ValueError, match=r"max_runners must be a positive integer"):
            parse_def_file(f, "arc-x")

    def test_negative_max_runners_raises(self, tmp_path):
        f = tmp_path / "r.yaml"
        f.write_text(yaml.safe_dump({"runner": {"name": "x", "max_runners": -1}}))
        with pytest.raises(ValueError, match=r"max_runners must be a positive integer"):
            parse_def_file(f, "arc-x")

    def test_string_max_runners_raises(self, tmp_path):
        f = tmp_path / "r.yaml"
        f.write_text(yaml.safe_dump({"runner": {"name": "x", "max_runners": "8"}}))
        with pytest.raises(ValueError, match=r"max_runners must be a positive integer"):
            parse_def_file(f, "arc-x")

    def test_empty_yaml_file_raises(self, tmp_path):
        f = tmp_path / "r.yaml"
        f.write_text("")
        with pytest.raises(ValueError, match=r"missing required 'runner\.name'"):
            parse_def_file(f, "arc-x")

    def test_null_runner_section_raises(self, tmp_path):
        f = tmp_path / "r.yaml"
        f.write_text("runner: null\n")
        with pytest.raises(ValueError, match=r"missing required 'runner\.name'"):
            parse_def_file(f, "arc-x")

    def test_max_runners_mapping_per_cluster_override(self, tmp_path):
        f = tmp_path / "r.yaml"
        f.write_text(yaml.safe_dump({"runner": {"name": "h100", "max_runners": {"default": 1, "my-cluster": 5}}}))
        assert parse_def_file(f, "my-cluster") == ("h100", None, 5)

    def test_max_runners_mapping_falls_back_to_default(self, tmp_path):
        f = tmp_path / "r.yaml"
        f.write_text(yaml.safe_dump({"runner": {"name": "h100", "max_runners": {"default": 1, "my-cluster": 5}}}))
        assert parse_def_file(f, "other-cluster") == ("h100", None, 1)

    def test_max_runners_mapping_missing_default_raises(self, tmp_path):
        f = tmp_path / "r.yaml"
        f.write_text(yaml.safe_dump({"runner": {"name": "h100", "max_runners": {"my-cluster": 5}}}))
        with pytest.raises(ValueError, match=r"max_runners mapping must include a .default. key"):
            parse_def_file(f, "my-cluster")

    def test_max_runners_mapping_non_positive_value_raises(self, tmp_path):
        f = tmp_path / "r.yaml"
        f.write_text(yaml.safe_dump({"runner": {"name": "h100", "max_runners": {"default": 0, "my-cluster": 1}}}))
        with pytest.raises(ValueError, match=r"max_runners\[.default.\] must be a positive integer"):
            parse_def_file(f, "my-cluster")

    def test_max_runners_mapping_non_int_value_raises(self, tmp_path):
        f = tmp_path / "r.yaml"
        f.write_text(yaml.safe_dump({"runner": {"name": "h100", "max_runners": {"default": 1, "my-cluster": "lots"}}}))
        with pytest.raises(ValueError, match=r"max_runners\[.my-cluster.\] must be a positive integer"):
            parse_def_file(f, "my-cluster")

    def test_max_runners_scalar_valid_returns_value(self, tmp_path):
        f = tmp_path / "r.yaml"
        f.write_text(yaml.safe_dump({"runner": {"name": "x", "max_runners": 7}}))
        assert parse_def_file(f, "arc-x") == ("x", None, 7)


# ============================================================================
# build_max_runners_map (integration over fake repo)
# ============================================================================


class TestBuildMaxRunnersMap:
    def test_happy_path_single_module(self, tmp_path):
        clusters_yaml = {
            "defaults": {},
            "clusters": {
                "arc-x": {
                    "arc-runners": {"runner_name_prefix": "mt-"},
                    "modules": ["arc-runners"],
                }
            },
        }
        modules = {
            "arc-runners": {
                "linux-cpu": {"name": "linux-cpu", "max_runners": 4},
                "linux-arm": {"name": "linux-arm"},  # no max_runners -> MAX_INT32
            }
        }
        repo = make_repo(tmp_path, clusters_yaml, modules)
        result = build_max_runners_map(repo, clusters_yaml, "arc-x")
        assert result == {
            "mt-linux-cpu": 4,
            "mt-linux-arm": MAX_INT32,
        }

    def test_multi_module_merge(self, tmp_path):
        clusters_yaml = {
            "defaults": {},
            "clusters": {
                "arc-x": {
                    "arc-runners": {"runner_name_prefix": ""},
                    "modules": ["arc-runners", "arc-runners-h100", "arc-runners-b200"],
                }
            },
        }
        modules = {
            "arc-runners": {"cpu-1": {"name": "cpu-1"}},
            "arc-runners-h100": {"h100-1": {"name": "h100-1", "max_runners": 8}},
            "arc-runners-b200": {"b200-1": {"name": "b200-1", "max_runners": 1}},
        }
        repo = make_repo(tmp_path, clusters_yaml, modules)
        result = build_max_runners_map(repo, clusters_yaml, "arc-x")
        assert result == {
            "cpu-1": MAX_INT32,
            "h100-1": 8,
            "b200-1": 1,
        }

    def test_region_excluded_instance_forces_zero(self, tmp_path):
        """Runner whose instance_type is region-excluded gets max_runners=0, not MAX_INT32."""
        clusters_yaml = {
            "defaults": {},
            "clusters": {
                "arc-uw1": {
                    "region": "us-west-1",
                    "arc-runners": {"runner_name_prefix": "mt-"},
                    "modules": ["arc-runners"],
                }
            },
        }
        modules = {
            "arc-runners": {
                # Excluded in us-west-1 (g5 fleet) — must be zeroed even though def omits max_runners.
                "a10g-8": {"name": "a10g-8", "instance_type": "g5.48xlarge"},
                # Not excluded — preserves def-level value.
                "t4": {"name": "t4", "instance_type": "g4dn.4xlarge", "max_runners": 2},
            }
        }
        repo = make_repo(tmp_path, clusters_yaml, modules)
        # Add a nodepool fleet def that excludes us-west-1 for g5.48xlarge.
        nodepools_defs = repo / "modules" / "nodepools" / "defs"
        nodepools_defs.mkdir(parents=True)
        (nodepools_defs / "g5.yaml").write_text(
            yaml.safe_dump(
                {
                    "fleet": {
                        "name": "g5",
                        "exclude_regions": ["us-west-1"],
                        "instances": [{"type": "g5.48xlarge"}],
                    }
                }
            )
        )
        result = build_max_runners_map(repo, clusters_yaml, "arc-uw1")
        assert result == {"mt-a10g-8": 0, "mt-t4": 2}

    def test_region_excluded_overrides_def_max_runners(self, tmp_path):
        """A def-level max_runners is also overridden when instance_type is region-excluded."""
        clusters_yaml = {
            "defaults": {},
            "clusters": {
                "arc-uw1": {
                    "region": "us-west-1",
                    "arc-runners": {"runner_name_prefix": ""},
                    "modules": ["arc-runners"],
                }
            },
        }
        modules = {
            "arc-runners": {"a100": {"name": "a100", "instance_type": "p4d.24xlarge", "max_runners": 4}},
        }
        repo = make_repo(tmp_path, clusters_yaml, modules)
        nodepools_defs = repo / "modules" / "nodepools" / "defs"
        nodepools_defs.mkdir(parents=True)
        (nodepools_defs / "p4d-24xlarge.yaml").write_text(
            yaml.safe_dump(
                {
                    "nodepool": {
                        "name": "p4d-24xlarge",
                        "instance_type": "p4d.24xlarge",
                        "exclude_regions": ["us-west-1"],
                    }
                }
            )
        )
        result = build_max_runners_map(repo, clusters_yaml, "arc-uw1")
        assert result == {"a100": 0}

    def test_region_not_excluded_preserves_unbounded(self, tmp_path):
        """In a region not on the exclusion list, capacity stays at MAX_INT32 when def omits max_runners."""
        clusters_yaml = {
            "defaults": {},
            "clusters": {
                "arc-ue2": {
                    "region": "us-east-2",
                    "arc-runners": {"runner_name_prefix": ""},
                    "modules": ["arc-runners"],
                }
            },
        }
        modules = {
            "arc-runners": {"a10g-8": {"name": "a10g-8", "instance_type": "g5.48xlarge"}},
        }
        repo = make_repo(tmp_path, clusters_yaml, modules)
        nodepools_defs = repo / "modules" / "nodepools" / "defs"
        nodepools_defs.mkdir(parents=True)
        (nodepools_defs / "g5.yaml").write_text(
            yaml.safe_dump(
                {
                    "fleet": {
                        "name": "g5",
                        "exclude_regions": ["us-west-1"],
                        "instances": [{"type": "g5.48xlarge"}],
                    }
                }
            )
        )
        result = build_max_runners_map(repo, clusters_yaml, "arc-ue2")
        assert result == {"a10g-8": MAX_INT32}

    def test_normalization_underscores_in_def_name(self, tmp_path):
        clusters_yaml = {
            "defaults": {},
            "clusters": {
                "arc-x": {
                    "arc-runners": {"runner_name_prefix": "p-"},
                    "modules": ["arc-runners"],
                }
            },
        }
        modules = {
            "arc-runners": {"weird_name": {"name": "weird_name", "max_runners": 2}},
        }
        repo = make_repo(tmp_path, clusters_yaml, modules)
        result = build_max_runners_map(repo, clusters_yaml, "arc-x")
        # underscores -> dashes (chart behaviour)
        assert result == {"p-weird-name": 2}

    def test_missing_module_dir_skipped_with_warning(self, tmp_path, capsys):
        clusters_yaml = {
            "defaults": {},
            "clusters": {
                "arc-x": {
                    "arc-runners": {"runner_name_prefix": ""},
                    "modules": ["arc-runners", "arc-runners-h100"],
                }
            },
        }
        # Only arc-runners has defs; arc-runners-h100 is enabled but missing.
        modules = {"arc-runners": {"cpu": {"name": "cpu"}}}
        repo = make_repo(tmp_path, clusters_yaml, modules)
        result = build_max_runners_map(repo, clusters_yaml, "arc-x")
        captured = capsys.readouterr()
        assert result == {"cpu": MAX_INT32}
        assert "arc-runners-h100" in captured.err
        assert "no def files" in captured.err

    def test_unknown_cluster_raises(self, tmp_path):
        clusters_yaml = {"defaults": {}, "clusters": {"arc-real": {"modules": []}}}
        repo = make_repo(tmp_path, clusters_yaml, {})
        with pytest.raises(KeyError, match="Unknown cluster 'arc-missing'"):
            build_max_runners_map(repo, clusters_yaml, "arc-missing")

    def test_invalid_def_file_raises(self, tmp_path):
        clusters_yaml = {
            "defaults": {},
            "clusters": {
                "arc-x": {
                    "arc-runners": {"runner_name_prefix": ""},
                    "modules": ["arc-runners"],
                }
            },
        }
        modules = {"arc-runners": {"broken": {"instance_type": "m5.large"}}}
        repo = make_repo(tmp_path, clusters_yaml, modules)
        with pytest.raises(ValueError, match=r"missing required 'runner\.name'"):
            build_max_runners_map(repo, clusters_yaml, "arc-x")

    def test_no_arc_runner_modules_returns_empty(self, tmp_path):
        clusters_yaml = {
            "defaults": {},
            "clusters": {"arc-x": {"modules": ["karpenter", "buildkit"]}},
        }
        repo = make_repo(tmp_path, clusters_yaml, {})
        assert build_max_runners_map(repo, clusters_yaml, "arc-x") == {}

    def test_pause_runners_forces_zero(self, tmp_path):
        """pause_runners=true on the cluster forces every ARS to max_runners=0,
        even when the def specifies a positive value — mirrors generate_runners.py
        so `just resume-runners` cannot silently undo a pause.
        """
        clusters_yaml = {
            "defaults": {},
            "clusters": {
                "arc-x": {
                    "arc-runners": {"runner_name_prefix": ""},
                    "modules": ["arc-runners"],
                    "pause_runners": True,
                }
            },
        }
        modules = {"arc-runners": {"capped": {"name": "capped", "max_runners": 7}}}
        repo = make_repo(tmp_path, clusters_yaml, modules)
        result = build_max_runners_map(repo, clusters_yaml, "arc-x")
        assert result == {"capped": 0}


# ============================================================================
# main() — CLI surface
# ============================================================================


class TestMain:
    def test_happy_path_emits_sorted_json(self, tmp_path, monkeypatch, capsys):
        clusters_yaml = {
            "defaults": {},
            "clusters": {
                "arc-x": {
                    "arc-runners": {"runner_name_prefix": "mt-"},
                    "modules": ["arc-runners"],
                }
            },
        }
        modules = {
            "arc-runners": {
                "z-runner": {"name": "z-runner", "max_runners": 2},
                "a-runner": {"name": "a-runner"},
            }
        }
        repo = make_repo(tmp_path, clusters_yaml, modules)
        monkeypatch.setenv("OSDC_ROOT", str(repo))
        rc = main(["arc-x"])
        captured = capsys.readouterr()
        assert rc == 0
        parsed = json.loads(captured.out)
        assert parsed == {"mt-a-runner": MAX_INT32, "mt-z-runner": 2}
        # Confirm sorted-keys ordering in raw stdout (alphabetical).
        assert captured.out.index('"mt-a-runner"') < captured.out.index('"mt-z-runner"')
        # Confirm indented (human-readable) output, not single-line.
        assert "\n" in captured.out.strip()

    def test_no_args_prints_usage(self, capsys):
        rc = main([])
        captured = capsys.readouterr()
        assert rc == 2
        assert "Usage:" in captured.err
        assert captured.out == ""

    def test_too_many_args_prints_usage(self, capsys):
        rc = main(["arc-x", "extra"])
        captured = capsys.readouterr()
        assert rc == 2
        assert "Usage:" in captured.err

    def test_uses_argv_when_argv_param_is_none(self, tmp_path, monkeypatch, capsys):
        clusters_yaml = {
            "defaults": {},
            "clusters": {
                "arc-x": {
                    "arc-runners": {"runner_name_prefix": ""},
                    "modules": ["arc-runners"],
                }
            },
        }
        modules = {"arc-runners": {"r": {"name": "r"}}}
        repo = make_repo(tmp_path, clusters_yaml, modules)
        monkeypatch.setenv("OSDC_ROOT", str(repo))
        monkeypatch.setattr("sys.argv", ["runner_max_map.py", "arc-x"])
        rc = main()
        captured = capsys.readouterr()
        assert rc == 0
        assert json.loads(captured.out) == {"r": MAX_INT32}

    def test_unknown_cluster_returns_1_and_logs_stderr(self, tmp_path, monkeypatch, capsys):
        (tmp_path / "clusters.yaml").write_text(
            yaml.safe_dump({"defaults": {}, "clusters": {"arc-real": {"modules": []}}})
        )
        monkeypatch.setenv("OSDC_ROOT", str(tmp_path))
        rc = main(["arc-missing"])
        captured = capsys.readouterr()
        assert rc == 1
        assert "Unknown cluster" in captured.err
        assert captured.out == ""

    def test_invalid_def_returns_1(self, tmp_path, monkeypatch, capsys):
        clusters_yaml = {
            "defaults": {},
            "clusters": {
                "arc-x": {
                    "arc-runners": {"runner_name_prefix": ""},
                    "modules": ["arc-runners"],
                }
            },
        }
        modules = {"arc-runners": {"broken": {"instance_type": "m5.large"}}}
        repo = make_repo(tmp_path, clusters_yaml, modules)
        monkeypatch.setenv("OSDC_ROOT", str(repo))
        rc = main(["arc-x"])
        captured = capsys.readouterr()
        assert rc == 1
        assert "missing required 'runner.name'" in captured.err
        assert captured.out == ""

    def test_yaml_parse_error_returns_1(self, tmp_path, monkeypatch, capsys):
        # Malformed YAML in clusters.yaml -> caught and reported, exit 1.
        (tmp_path / "clusters.yaml").write_text("clusters: {not: valid: yaml}\n")
        monkeypatch.setenv("OSDC_ROOT", str(tmp_path))
        rc = main(["arc-x"])
        captured = capsys.readouterr()
        assert rc == 1
        assert "error:" in captured.err

    def test_missing_OSDC_ROOT_raises_caught_as_error(self, tmp_path, monkeypatch, capsys):
        # OSDC_ROOT pointing somewhere with no clusters.yaml -> handled as error.
        empty_dir = tmp_path / "empty"
        empty_dir.mkdir()
        monkeypatch.setenv("OSDC_ROOT", str(empty_dir))
        rc = main(["arc-x"])
        captured = capsys.readouterr()
        assert rc == 1
        assert "OSDC_ROOT" in captured.err


# ============================================================================
# End-to-end with realistic def-file content
# ============================================================================


class TestEndToEnd:
    def test_realistic_production_like_setup(self, tmp_path, monkeypatch, capsys):
        """Mirrors the meta-prod-aws-ue2 cluster shape: prefix=mt-, three modules."""
        clusters_yaml = {
            "defaults": {},
            "clusters": {
                "meta-prod-aws-ue2": {
                    "arc-runners": {"runner_name_prefix": "mt-"},
                    "modules": [
                        "karpenter",
                        "arc",
                        "nodepools",
                        "arc-runners",
                        "arc-runners-h100",
                        "arc-runners-b200",
                    ],
                }
            },
        }
        modules = {
            "arc-runners": {
                "l-x86iavx512-2-4": {"name": "l-x86iavx512-2-4"},
                "l-arm64g3-16-62": {"name": "l-arm64g3-16-62"},
            },
            "arc-runners-h100": {
                "l-x86iamx-22-225-h100": {"name": "l-x86iamx-22-225-h100", "max_runners": 8},
            },
            "arc-runners-b200": {
                "l-x86iamx-22-225-b200": {"name": "l-x86iamx-22-225-b200", "max_runners": 1},
            },
        }
        repo = make_repo(tmp_path, clusters_yaml, modules)
        monkeypatch.setenv("OSDC_ROOT", str(repo))
        rc = main(["meta-prod-aws-ue2"])
        captured = capsys.readouterr()
        assert rc == 0
        parsed = json.loads(captured.out)
        assert parsed == {
            "mt-l-arm64g3-16-62": MAX_INT32,
            "mt-l-x86iamx-22-225-b200": 1,
            "mt-l-x86iamx-22-225-h100": 8,
            "mt-l-x86iavx512-2-4": MAX_INT32,
        }

    def test_def_file_with_extra_fields_is_tolerated(self, tmp_path):
        """Real def files have many fields besides name/max_runners."""
        defs_dir = tmp_path / "modules" / "arc-runners" / "defs"
        defs_dir.mkdir(parents=True)
        full_def = textwrap.dedent("""\
            runner:
              name: l-x86iamx-8-16
              instance_type: c7i.12xlarge
              disk_size: 150
              vcpu: 8
              memory: 16Gi
              gpu: 0
              proactive_capacity: 30
              max_burst_capacity: 2000
        """)
        (defs_dir / "l-x86iamx-8-16.yaml").write_text(full_def)
        name, instance_type, max_runners = parse_def_file(defs_dir / "l-x86iamx-8-16.yaml", "arc-x")
        assert name == "l-x86iamx-8-16"
        assert instance_type == "c7i.12xlarge"
        assert max_runners is None
