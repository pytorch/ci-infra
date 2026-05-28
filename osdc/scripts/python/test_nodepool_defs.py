"""Tests for nodepool_defs — shared parsers for nodepool def YAMLs."""

from __future__ import annotations

from typing import TYPE_CHECKING

import yaml
from nodepool_defs import is_excluded_for_region, iter_fleet_names, load_excluded_instance_types

if TYPE_CHECKING:
    from pathlib import Path


def _write(path: Path, data: dict | str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if isinstance(data, str):
        path.write_text(data)
    else:
        path.write_text(yaml.dump(data))


# is_excluded_for_region ------------------------------------------------------


def test_excluded_when_region_in_list():
    assert is_excluded_for_region({"exclude_regions": ["us-west-1", "eu-west-1"]}, "us-west-1")


def test_not_excluded_when_region_not_in_list():
    assert not is_excluded_for_region({"exclude_regions": ["us-west-1"]}, "us-east-2")


def test_not_excluded_when_no_exclude_regions_key():
    assert not is_excluded_for_region({"name": "fleet"}, "us-east-2")


def test_not_excluded_when_exclude_regions_null():
    assert not is_excluded_for_region({"exclude_regions": None}, "us-east-2")


def test_not_excluded_when_region_empty():
    assert not is_excluded_for_region({"exclude_regions": ["us-west-1"]}, "")


def test_not_excluded_when_region_none():
    assert not is_excluded_for_region({"exclude_regions": ["us-west-1"]}, None)  # type: ignore[arg-type]


def test_not_excluded_when_def_not_dict():
    assert not is_excluded_for_region("garbage", "us-west-1")  # type: ignore[arg-type]


# load_excluded_instance_types -----------------------------------------------


def test_load_excluded_fleet_shape(tmp_path: Path):
    _write(
        tmp_path / "g5.yaml",
        {
            "fleet": {
                "name": "g5",
                "exclude_regions": ["us-west-1"],
                "instances": [
                    {"type": "g5.8xlarge", "weight": 100, "node_disk_size": 600},
                    {"type": "g5.12xlarge", "weight": 100, "node_disk_size": 600},
                ],
            }
        },
    )
    assert load_excluded_instance_types(tmp_path, "us-west-1") == {"g5.8xlarge", "g5.12xlarge"}


def test_load_excluded_legacy_nodepool_shape(tmp_path: Path):
    _write(
        tmp_path / "p4d.yaml",
        {"nodepool": {"name": "p4d", "instance_type": "p4d.24xlarge", "exclude_regions": ["us-west-1"]}},
    )
    assert load_excluded_instance_types(tmp_path, "us-west-1") == {"p4d.24xlarge"}


def test_load_excluded_fleets_list_shape(tmp_path: Path):
    _write(
        tmp_path / "multi.yaml",
        {
            "fleets": [
                {
                    "name": "fa",
                    "exclude_regions": ["us-west-1"],
                    "instances": [{"type": "g5.48xlarge", "weight": 100, "node_disk_size": 600}],
                },
                {
                    "name": "fb",
                    "instances": [{"type": "g5.8xlarge", "weight": 100, "node_disk_size": 600}],
                },
            ]
        },
    )
    assert load_excluded_instance_types(tmp_path, "us-west-1") == {"g5.48xlarge"}


def test_load_excluded_no_match(tmp_path: Path):
    _write(
        tmp_path / "g5.yaml",
        {
            "fleet": {
                "name": "g5",
                "exclude_regions": ["us-west-1"],
                "instances": [{"type": "g5.8xlarge", "weight": 100, "node_disk_size": 600}],
            }
        },
    )
    assert load_excluded_instance_types(tmp_path, "us-east-2") == set()


def test_load_excluded_empty_region(tmp_path: Path):
    _write(
        tmp_path / "g5.yaml",
        {"fleet": {"name": "g5", "exclude_regions": ["us-west-1"], "instances": [{"type": "g5.8xlarge"}]}},
    )
    assert load_excluded_instance_types(tmp_path, "") == set()


def test_load_excluded_missing_dir(tmp_path: Path):
    assert load_excluded_instance_types(tmp_path / "nope", "us-west-1") == set()


def test_load_excluded_skips_malformed_yaml(tmp_path: Path):
    _write(tmp_path / "broken.yaml", "fleet: {name: x,\n  bad")
    _write(
        tmp_path / "ok.yaml",
        {
            "fleet": {
                "name": "g5",
                "exclude_regions": ["us-west-1"],
                "instances": [{"type": "g5.8xlarge"}],
            }
        },
    )
    assert load_excluded_instance_types(tmp_path, "us-west-1") == {"g5.8xlarge"}


def test_load_excluded_ignores_non_dict_root(tmp_path: Path):
    _write(tmp_path / "list.yaml", "- just\n- a\n- list\n")
    assert load_excluded_instance_types(tmp_path, "us-west-1") == set()


def test_load_excluded_ignores_non_string_instance_type(tmp_path: Path):
    _write(
        tmp_path / "weird.yaml",
        {
            "fleet": {
                "name": "x",
                "exclude_regions": ["us-west-1"],
                "instances": [{"type": None}, {"type": "g5.8xlarge"}, "not-a-dict"],
            }
        },
    )
    assert load_excluded_instance_types(tmp_path, "us-west-1") == {"g5.8xlarge"}


# iter_fleet_names -----------------------------------------------------------


def test_iter_fleet_single_fleet_shape():
    data = {"fleet": {"name": "g5", "instances": [{"type": "g5.8xlarge"}]}}
    assert iter_fleet_names(data, "us-east-2") == ["g5"]


def test_iter_fleet_fleets_list_shape():
    data = {
        "fleets": [
            {"name": "fa", "instances": []},
            {"name": "fb", "instances": []},
        ]
    }
    assert iter_fleet_names(data, "us-east-2") == ["fa", "fb"]


def test_iter_fleet_legacy_nodepool_shape():
    data = {"nodepool": {"name": "p", "instance_type": "p5.48xlarge"}}
    assert iter_fleet_names(data, "us-east-2") == ["p5"]


def test_iter_fleet_legacy_nodepool_excluded():
    data = {"nodepool": {"name": "p", "instance_type": "p5.48xlarge", "exclude_regions": ["us-west-1"]}}
    assert iter_fleet_names(data, "us-west-1") == []


def test_iter_fleet_fleet_excluded():
    data = {"fleet": {"name": "g5", "exclude_regions": ["us-west-1"]}}
    assert iter_fleet_names(data, "us-west-1") == []


def test_iter_fleet_combined_shapes():
    """A def with both ``fleet`` AND ``fleets`` contributes from both — defensive."""
    data = {
        "fleet": {"name": "primary"},
        "fleets": [{"name": "extra"}],
        "nodepool": {"name": "legacy", "instance_type": "p5.48xlarge"},
    }
    assert iter_fleet_names(data, "us-east-2") == ["primary", "extra", "p5"]


def test_iter_fleet_skips_invalid_entries():
    data = {
        "fleet": "not-a-dict",
        "fleets": ["nope", {"name": ""}, {"name": "ok"}, {}],
        "nodepool": {"name": "x", "instance_type": ""},
    }
    assert iter_fleet_names(data, "us-east-2") == ["ok"]


def test_iter_fleet_non_dict_input():
    assert iter_fleet_names("garbage", "us-east-2") == []  # type: ignore[arg-type]


def test_iter_fleet_fleets_filters_excluded():
    data = {
        "fleets": [
            {"name": "fa", "exclude_regions": ["us-west-1"]},
            {"name": "fb"},
        ]
    }
    assert iter_fleet_names(data, "us-west-1") == ["fb"]
