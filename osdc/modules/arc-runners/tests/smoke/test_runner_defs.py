"""Unit tests for runner_defs helper functions and generated-YAML readers.

Pure-Python tests with no live cluster dependency — runnable from this dir
via `pytest test_runner_defs.py`.
"""

from __future__ import annotations

import pytest
from runner_defs import (
    def_for_listener_pod,
    def_name_from_scale_set,
    read_generated_scale_sets,
    scale_set_label_from_values,
    scale_set_label_of_pod,
    scale_sets_by_label,
)
from test_capacity_parity import _proactive_capacity_from_generated
from test_runners import _capacity_aware_env, _listener_env_from_generated_yaml


class TestDefNameFromScaleSet:
    def test_strips_prefix(self):
        assert def_name_from_scale_set("c-mt-l-arm64g2-6-32", "c-mt-") == "l-arm64g2-6-32"

    def test_empty_prefix_returns_input(self):
        assert def_name_from_scale_set("l-arm64g2-6-32", "") == "l-arm64g2-6-32"

    def test_short_prefix(self):
        assert def_name_from_scale_set("mt-l-arm64g2-6-32", "mt-") == "l-arm64g2-6-32"

    def test_returns_none_when_prefix_missing(self):
        assert def_name_from_scale_set("c-mt-l-arm64g2-6-32", "other-") is None

    def test_returns_empty_when_name_equals_prefix(self):
        assert def_name_from_scale_set("c-mt-", "c-mt-") == ""


class TestDefForListenerPod:
    def test_finds_def_via_label(self):
        pod = {"metadata": {"labels": {"actions.github.com/scale-set-name": "c-mt-runner-1"}}}
        defs = {"runner-1": {"name": "runner-1", "vcpu": 4}}
        name, d = def_for_listener_pod(pod, defs, "c-mt-")
        assert name == "runner-1"
        assert d == defs["runner-1"]

    def test_returns_def_name_when_def_missing(self):
        pod = {"metadata": {"labels": {"actions.github.com/scale-set-name": "c-mt-runner-1"}}}
        defs: dict = {}
        name, d = def_for_listener_pod(pod, defs, "c-mt-")
        assert name == "runner-1"
        assert d is None

    def test_returns_none_when_label_missing(self):
        pod = {"metadata": {"labels": {}}}
        name, d = def_for_listener_pod(pod, {}, "c-mt-")
        assert name is None
        assert d is None

    def test_returns_none_when_prefix_does_not_match(self):
        pod = {"metadata": {"labels": {"actions.github.com/scale-set-name": "rogue-runner-1"}}}
        defs = {"runner-1": {"name": "runner-1"}}
        name, d = def_for_listener_pod(pod, defs, "c-mt-")
        assert name is None
        assert d is None

    def test_disambiguates_rel_vs_non_rel_suffix_collision(self):
        """rel-* defs share suffixes with their non-rel siblings.

        e.g. a rel-X def's name ends with a hypothetical non-rel X def's name
        (rel-l-arm64g3-44-340 ends with l-arm64g3-44-340). A naive endswith()
        lookup would grab the wrong def; exact-match against the prefix-stripped
        scale-set name must NOT. (No such collision pair ships today — release
        defs use distinct sizes — but the lookup must stay exact-match regardless.)
        """
        defs = {
            "l-arm64g3-44-340": {"name": "l-arm64g3-44-340", "proactive_capacity": 30},
            "rel-l-arm64g3-44-340": {"name": "rel-l-arm64g3-44-340", "proactive_capacity": 30},
        }

        rel_pod = {"metadata": {"labels": {"actions.github.com/scale-set-name": "c-mt-rel-l-arm64g3-44-340"}}}
        rel_name, rel_def = def_for_listener_pod(rel_pod, defs, "c-mt-")
        assert rel_name == "rel-l-arm64g3-44-340"
        assert rel_def is defs["rel-l-arm64g3-44-340"]

        non_rel_pod = {"metadata": {"labels": {"actions.github.com/scale-set-name": "c-mt-l-arm64g3-44-340"}}}
        non_rel_name, non_rel_def = def_for_listener_pod(non_rel_pod, defs, "c-mt-")
        assert non_rel_name == "l-arm64g3-44-340"
        assert non_rel_def is defs["l-arm64g3-44-340"]


class TestListenerEnvFromGeneratedYaml:
    """Unit tests for the generated-YAML env reader used by the coherence test."""

    def test_extracts_env_from_first_container(self):
        doc = {
            "listenerTemplate": {
                "spec": {
                    "containers": [
                        {
                            "name": "listener",
                            "env": [
                                {"name": "CAPACITY_AWARE_PROACTIVE_CAPACITY", "value": "30"},
                                {"name": "CAPACITY_AWARE_NODE_FLEET", "value": "m8g"},
                            ],
                        }
                    ]
                }
            }
        }
        env = _listener_env_from_generated_yaml(doc)
        assert env["CAPACITY_AWARE_PROACTIVE_CAPACITY"]["value"] == "30"
        assert env["CAPACITY_AWARE_NODE_FLEET"]["value"] == "m8g"

    def test_returns_empty_when_containers_missing(self):
        assert _listener_env_from_generated_yaml({}) == {}
        assert _listener_env_from_generated_yaml({"listenerTemplate": {}}) == {}
        assert _listener_env_from_generated_yaml({"listenerTemplate": {"spec": {"containers": []}}}) == {}

    def test_returns_empty_when_env_missing(self):
        doc = {"listenerTemplate": {"spec": {"containers": [{"name": "listener"}]}}}
        assert _listener_env_from_generated_yaml(doc) == {}

    def test_filter_capacity_aware(self):
        env = {
            "CAPACITY_AWARE_PROACTIVE_CAPACITY": {"value": "0"},
            "CAPACITY_AWARE_NODE_FLEET": {"value": "g4dn"},
            "OTHER_VAR": {"value": "ignored"},
        }
        out = _capacity_aware_env(env)
        assert "CAPACITY_AWARE_PROACTIVE_CAPACITY" in out
        assert "CAPACITY_AWARE_NODE_FLEET" in out
        assert "OTHER_VAR" not in out


class TestProactiveCapacityFromGenerated:
    """Unit tests for the parity test's proactive-capacity extractor."""

    def _doc(self, value):
        return {
            "listenerTemplate": {
                "spec": {
                    "containers": [
                        {
                            "env": [
                                {"name": "CAPACITY_AWARE_PROACTIVE_CAPACITY", "value": value},
                            ]
                        }
                    ]
                }
            }
        }

    def test_parses_positive_int(self):
        assert _proactive_capacity_from_generated(self._doc("30")) == 30

    def test_parses_zero(self):
        assert _proactive_capacity_from_generated(self._doc("0")) == 0

    def test_returns_zero_when_env_missing(self):
        doc = {"listenerTemplate": {"spec": {"containers": [{"env": []}]}}}
        assert _proactive_capacity_from_generated(doc) == 0

    def test_returns_zero_when_containers_missing(self):
        assert _proactive_capacity_from_generated({}) == 0
        assert _proactive_capacity_from_generated({"listenerTemplate": {"spec": {"containers": []}}}) == 0

    def test_returns_zero_when_value_unparseable(self):
        assert _proactive_capacity_from_generated(self._doc("not-an-int")) == 0
        assert _proactive_capacity_from_generated(self._doc(None)) == 0


class TestScaleSetLabelFromValues:
    """The live pod scale-set-name label derived from a generated chart-values doc."""

    def test_primary_uses_runner_scale_set_name(self):
        assert scale_set_label_from_values({"runnerScaleSetName": "mt-l-x86iamx-8-16"}) == "mt-l-x86iamx-8-16"

    def test_additional_org_uses_resource_name(self):
        # Additional-org fan-out sets resourceName; runnerScaleSetName stays the
        # shared GitHub label — the K8s label follows resourceName.
        values = {"runnerScaleSetName": "mt-l-x86iamx-8-16", "resourceName": "mp-l-x86iamx-8-16"}
        assert scale_set_label_from_values(values) == "mp-l-x86iamx-8-16"

    def test_missing_returns_empty(self):
        assert scale_set_label_from_values({}) == ""


class TestScaleSetLabelOfPod:
    def test_reads_label(self):
        pod = {"metadata": {"labels": {"actions.github.com/scale-set-name": "mp-l-x86iamx-8-16"}}}
        assert scale_set_label_of_pod(pod) == "mp-l-x86iamx-8-16"

    def test_missing_label_returns_none(self):
        assert scale_set_label_of_pod({"metadata": {"labels": {}}}) is None
        assert scale_set_label_of_pod({}) is None


def _write_generated(path, resource_id, runner_scale_set_name, resource_name=None, cluster_index="0"):
    """Write a minimal two-doc generated runner YAML mirroring the generator."""
    resource_line = f'resourceName: "{resource_name}"\n' if resource_name else ""
    path.write_text(
        f'runnerScaleSetName: "{runner_scale_set_name}"\n'
        f"{resource_line}"
        "listenerTemplate:\n"
        "  spec:\n"
        "    containers:\n"
        "      - name: listener\n"
        "        env:\n"
        "          - name: CAPACITY_AWARE_CLUSTER_INDEX\n"
        f'            value: "{cluster_index}"\n'
        "---\n"
        "apiVersion: v1\n"
        "kind: ConfigMap\n"
        "metadata:\n"
        f"  name: arc-runner-hook-{resource_id}\n"
        "  namespace: arc-runners\n"
    )


class TestReadGeneratedScaleSets:
    """The generated-files reader that is the source of truth for org-aware checks."""

    def test_multi_org_pair(self, tmp_path):
        # One def fanned out to primary (pytorch) + additional org (mp), exactly
        # as the generator emits it.
        _write_generated(tmp_path / "l-x86iamx-8-16.yaml", "l-x86iamx-8-16", "mt-l-x86iamx-8-16", cluster_index="1")
        _write_generated(
            tmp_path / "mp-l-x86iamx-8-16.yaml",
            "mp-l-x86iamx-8-16",
            "mt-l-x86iamx-8-16",
            resource_name="mp-l-x86iamx-8-16",
            cluster_index="0",
        )

        scale_sets = read_generated_scale_sets([tmp_path], "mt-")
        assert len(scale_sets) == 2
        by_label = scale_sets_by_label(scale_sets)

        # Org-unique labels: primary keeps the prefixed name, mp uses resourceName.
        assert set(by_label) == {"mt-l-x86iamx-8-16", "mp-l-x86iamx-8-16"}
        primary = by_label["mt-l-x86iamx-8-16"]
        mp = by_label["mp-l-x86iamx-8-16"]

        # Both map back to the SAME bare def, but to DIFFERENT org-unique CMs.
        assert primary.def_name == mp.def_name == "l-x86iamx-8-16"
        assert primary.configmap_name == "arc-runner-hook-l-x86iamx-8-16"
        assert mp.configmap_name == "arc-runner-hook-mp-l-x86iamx-8-16"
        assert primary.resource_id == "l-x86iamx-8-16"
        assert mp.resource_id == "mp-l-x86iamx-8-16"
        # Per-org values are preserved (different shard index here).
        assert (
            _capacity_aware_env(_listener_env_from_generated_yaml(primary.values))["CAPACITY_AWARE_CLUSTER_INDEX"][
                "value"
            ]
            == "1"
        )
        assert (
            _capacity_aware_env(_listener_env_from_generated_yaml(mp.values))["CAPACITY_AWARE_CLUSTER_INDEX"]["value"]
            == "0"
        )

    def test_single_org_matches_def_name_reconstruction(self, tmp_path):
        # Without an additional org the label is the prefixed name and the CM
        # name matches the classic arc-runner-hook-<def> reconstruction.
        _write_generated(tmp_path / "l-x86iamx-8-16.yaml", "l-x86iamx-8-16", "mt-l-x86iamx-8-16")
        (scale_set,) = read_generated_scale_sets([tmp_path], "mt-")
        assert scale_set.scale_set_label == "mt-l-x86iamx-8-16"
        assert scale_set.def_name == "l-x86iamx-8-16"
        assert scale_set.configmap_name == "arc-runner-hook-l-x86iamx-8-16"

    def test_missing_dir_is_skipped(self, tmp_path):
        assert read_generated_scale_sets([tmp_path / "does-not-exist"], "mt-") == []


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
