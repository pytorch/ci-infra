"""Smoke tests for base infrastructure nodes."""

import pytest

pytestmark = [pytest.mark.live]


class TestBaseNodes:
    """Verify base infrastructure nodes are present and correctly configured."""

    def test_base_node_count(self, all_nodes, resolve_config):
        expected_count = int(resolve_config("base.base_node_count", "3"))
        base_nodes = [
            n
            for n in all_nodes["items"]
            if n.get("metadata", {}).get("labels", {}).get("role") == "base-infrastructure"
        ]
        assert len(base_nodes) >= expected_count, f"Expected >= {expected_count} base nodes, found {len(base_nodes)}"

    def test_base_nodes_ready(self, all_nodes):
        base_nodes = [
            n
            for n in all_nodes["items"]
            if n.get("metadata", {}).get("labels", {}).get("role") == "base-infrastructure"
        ]
        for node in base_nodes:
            name = node["metadata"]["name"]
            conditions = {c["type"]: c["status"] for c in node.get("status", {}).get("conditions", [])}
            assert conditions.get("Ready") == "True", f"Base node {name} is not Ready"

    def test_base_nodes_have_critical_addons_taint(self, all_nodes):
        base_nodes = [
            n
            for n in all_nodes["items"]
            if n.get("metadata", {}).get("labels", {}).get("role") == "base-infrastructure"
        ]
        assert len(base_nodes) > 0, "No base nodes found"
        for node in base_nodes:
            name = node["metadata"]["name"]
            taints = node.get("spec", {}).get("taints", [])
            has_taint = any(t.get("key") == "CriticalAddonsOnly" and t.get("effect") == "NoSchedule" for t in taints)
            assert has_taint, f"Base node {name} missing CriticalAddonsOnly taint"
