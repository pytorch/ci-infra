"""Unit tests for generate_buildkit.py — BuildKit manifest generator."""

import math
import sys
from unittest.mock import patch

import pytest
import yaml
from generate_buildkit import (
    DAEMONSET_OVERHEAD_CPU_M,
    DAEMONSET_OVERHEAD_MEM_MI,
    INSTANCE_SPECS,
    MARGIN,
    _kubelet_reserved,
    compute_pod_resources,
    generate_deployment_yaml,
    generate_nodepools_yaml,
)

# ============================================================================
# Helpers
# ============================================================================


def parse_all_yaml(text: str) -> list[dict]:
    """Parse multi-document YAML string, filtering None entries."""
    return [doc for doc in yaml.safe_load_all(text) if doc is not None]


# ============================================================================
# _kubelet_reserved
# ============================================================================


class TestKubeletReserved:
    """Tests for _kubelet_reserved CPU + memory tiered formula."""

    def test_1_vcpu(self):
        cpu, mem = _kubelet_reserved(1, 8)
        assert cpu == 60
        assert mem == 255 + 11 * 1 + 100

    def test_2_vcpu(self):
        cpu, mem = _kubelet_reserved(2, 16)
        assert cpu == 70
        assert mem == 255 + 11 * 2 + 100

    def test_4_vcpu_boundary(self):
        cpu, mem = _kubelet_reserved(4, 32)
        assert cpu == 80
        assert mem == 255 + 11 * 4 + 100

    def test_8_vcpu(self):
        cpu, _ = _kubelet_reserved(8, 64)
        assert cpu == 80 + int((8 - 4) * 2.5)  # 80 + 10 = 90

    def test_16_vcpu(self):
        cpu, _ = _kubelet_reserved(16, 128)
        assert cpu == 80 + int((16 - 4) * 2.5)  # 80 + 30 = 110

    def test_32_vcpu(self):
        cpu, _ = _kubelet_reserved(32, 256)
        assert cpu == 80 + int((32 - 4) * 2.5)  # 80 + 70 = 150

    def test_48_vcpu(self):
        cpu, _ = _kubelet_reserved(48, 384)
        assert cpu == 80 + int((48 - 4) * 2.5)  # 80 + 110 = 190

    def test_64_vcpu(self):
        cpu, mem = _kubelet_reserved(64, 256)
        assert cpu == 80 + int((64 - 4) * 2.5)  # 80 + 150 = 230
        assert mem == 255 + 11 * 64 + 100

    def test_96_vcpu(self):
        cpu, mem = _kubelet_reserved(96, 384)
        assert cpu == 80 + int((96 - 4) * 2.5)  # 80 + 230 = 310
        assert mem == 255 + 11 * 96 + 100

    def test_128_vcpu(self):
        cpu, _ = _kubelet_reserved(128, 512)
        assert cpu == 80 + int((128 - 4) * 2.5)  # 80 + 310 = 390

    def test_memory_formula(self):
        """Memory reservation: 255Mi base + 11Mi/core + 100Mi eviction."""
        for vcpu in [1, 4, 16, 64, 96]:
            _, mem = _kubelet_reserved(vcpu, vcpu * 4)
            assert mem == 255 + 11 * vcpu + 100


# ============================================================================
# compute_pod_resources
# ============================================================================


class TestComputePodResources:
    """Tests for compute_pod_resources — end-to-end sizing calculation."""

    def test_all_known_instance_types(self):
        """Every instance type in INSTANCE_SPECS produces valid results."""
        for instance_type in INSTANCE_SPECS:
            result = compute_pod_resources(instance_type, 2)
            assert result["cpu"] > 0, f"{instance_type}: cpu must be positive"
            assert result["memory_gi"] > 0, f"{instance_type}: memory must be positive"

    def test_guaranteed_qos_truncation(self):
        """CPU is truncated to whole vCPUs, memory to whole GiB."""
        for instance_type in INSTANCE_SPECS:
            result = compute_pod_resources(instance_type, 2)
            assert isinstance(result["cpu"], int)
            assert isinstance(result["memory_gi"], int)

    def test_pods_per_node_1(self):
        """Single pod per node gets more resources than 2 pods."""
        res_1 = compute_pod_resources("m8gd.24xlarge", 1)
        res_2 = compute_pod_resources("m8gd.24xlarge", 2)
        assert res_1["cpu"] > res_2["cpu"]
        assert res_1["memory_gi"] > res_2["memory_gi"]

    def test_pods_per_node_3(self):
        """Three pods per node get less than 2."""
        res_2 = compute_pod_resources("m8gd.24xlarge", 2)
        res_3 = compute_pod_resources("m8gd.24xlarge", 3)
        assert res_2["cpu"] > res_3["cpu"]
        assert res_2["memory_gi"] > res_3["memory_gi"]

    def test_margin_applied(self):
        """10% margin means pod resources are less than raw usable / pods."""
        spec = INSTANCE_SPECS["m6id.24xlarge"]
        reserved_cpu, _reserved_mem = _kubelet_reserved(spec["vcpu"], spec["memory_gib"])
        usable_cpu_m = spec["vcpu"] * 1000 - reserved_cpu - DAEMONSET_OVERHEAD_CPU_M

        result = compute_pod_resources("m6id.24xlarge", 2)
        # With margin, per-pod CPU (in millicores) should be ~90% of usable/2
        raw_per_pod_cpu_m = usable_cpu_m // 2
        margined_per_pod_cpu_m = math.floor(usable_cpu_m * MARGIN / 2)
        assert result["cpu"] == margined_per_pod_cpu_m // 1000
        assert result["cpu"] * 1000 < raw_per_pod_cpu_m

    def test_allocatable_in_result(self):
        """Result includes allocatable values for logging."""
        result = compute_pod_resources("c7gd.16xlarge", 2)
        assert "allocatable_cpu_m" in result
        assert "allocatable_mem_mi" in result
        spec = INSTANCE_SPECS["c7gd.16xlarge"]
        reserved_cpu, reserved_mem = _kubelet_reserved(spec["vcpu"], spec["memory_gib"])
        assert result["allocatable_cpu_m"] == spec["vcpu"] * 1000 - reserved_cpu
        assert result["allocatable_mem_mi"] == spec["memory_gib"] * 1024 - reserved_mem

    def test_unknown_instance_type_raises(self):
        """Unknown instance type raises KeyError."""
        with pytest.raises(KeyError):
            compute_pod_resources("x99.superlarge", 2)

    def test_m8gd_24xlarge_specific(self):
        """Spot-check m8gd.24xlarge with 2 pods."""
        spec = INSTANCE_SPECS["m8gd.24xlarge"]
        assert spec["vcpu"] == 96
        assert spec["memory_gib"] == 384

        result = compute_pod_resources("m8gd.24xlarge", 2)
        # Manually compute expected values
        reserved_cpu, reserved_mem = _kubelet_reserved(96, 384)
        alloc_cpu_m = 96 * 1000 - reserved_cpu
        alloc_mem_mi = 384 * 1024 - reserved_mem
        usable_cpu_m = alloc_cpu_m - DAEMONSET_OVERHEAD_CPU_M
        usable_mem_mi = alloc_mem_mi - DAEMONSET_OVERHEAD_MEM_MI
        expected_cpu = math.floor(usable_cpu_m * MARGIN / 2) // 1000
        expected_mem = math.floor(usable_mem_mi * MARGIN / 2) // 1024

        assert result["cpu"] == expected_cpu
        assert result["memory_gi"] == expected_mem


# ============================================================================
# generate_deployment_yaml
# ============================================================================


class TestGenerateDeploymentYaml:
    """Tests for generate_deployment_yaml — Deployment YAML output."""

    def _parse_deployments(self, yaml_text: str) -> list[dict]:
        return parse_all_yaml(yaml_text)

    def test_produces_two_deployments(self):
        output = generate_deployment_yaml("m8gd.24xlarge", "m6id.24xlarge", 4, 2)
        docs = self._parse_deployments(output)
        assert len(docs) == 2
        assert all(d["kind"] == "Deployment" for d in docs)

    def test_arch_names(self):
        output = generate_deployment_yaml("m8gd.24xlarge", "m6id.24xlarge", 4, 2)
        docs = self._parse_deployments(output)
        names = {d["metadata"]["name"] for d in docs}
        assert names == {"buildkitd-arm64", "buildkitd-amd64"}

    def test_namespace(self):
        output = generate_deployment_yaml("m8gd.24xlarge", "m6id.24xlarge", 4, 2)
        docs = self._parse_deployments(output)
        for d in docs:
            assert d["metadata"]["namespace"] == "buildkit"

    def test_replica_count(self):
        for replicas in [1, 4, 8]:
            output = generate_deployment_yaml("m8gd.24xlarge", "m6id.24xlarge", replicas, 2)
            docs = self._parse_deployments(output)
            for d in docs:
                assert d["spec"]["replicas"] == replicas

    def test_node_selector_per_arch(self):
        output = generate_deployment_yaml("m8gd.24xlarge", "m6id.24xlarge", 4, 2)
        docs = self._parse_deployments(output)
        for d in docs:
            ns = d["spec"]["template"]["spec"]["nodeSelector"]
            assert ns["workload-type"] == "buildkit"
            if d["metadata"]["name"] == "buildkitd-arm64":
                assert ns["instance-type"] == "m8gd.24xlarge"
            else:
                assert ns["instance-type"] == "m6id.24xlarge"

    def test_tolerations_present(self):
        output = generate_deployment_yaml("m8gd.24xlarge", "m6id.24xlarge", 4, 2)
        docs = self._parse_deployments(output)
        for d in docs:
            tolerations = d["spec"]["template"]["spec"]["tolerations"]
            assert len(tolerations) >= 1
            assert tolerations[0]["key"] == "instance-type"

    def test_guaranteed_qos_requests_eq_limits(self):
        output = generate_deployment_yaml("m8gd.24xlarge", "m6id.24xlarge", 4, 2)
        docs = self._parse_deployments(output)
        for d in docs:
            container = d["spec"]["template"]["spec"]["containers"][0]
            res = container["resources"]
            assert res["requests"]["cpu"] == res["limits"]["cpu"]
            assert res["requests"]["memory"] == res["limits"]["memory"]

    def test_volume_mounts_present(self):
        output = generate_deployment_yaml("m8gd.24xlarge", "m6id.24xlarge", 4, 2)
        docs = self._parse_deployments(output)
        for d in docs:
            container = d["spec"]["template"]["spec"]["containers"][0]
            mount_names = {vm["name"] for vm in container["volumeMounts"]}
            assert "config" in mount_names
            assert "buildkit-cache" in mount_names
            assert "git-cache" in mount_names

    def test_header_comment(self):
        output = generate_deployment_yaml("m8gd.24xlarge", "m6id.24xlarge", 4, 2)
        assert "auto-generated by generate_buildkit.py" in output


# ============================================================================
# generate_nodepools_yaml
# ============================================================================


class TestGenerateNodepoolsYaml:
    """Tests for generate_nodepools_yaml — NodePool + EC2NodeClass YAML."""

    def _parse_nodepools(self, yaml_text: str) -> list[dict]:
        return parse_all_yaml(yaml_text)

    def test_produces_four_documents(self):
        """Two arches x (NodePool + EC2NodeClass) = 4 docs."""
        output = generate_nodepools_yaml("m8gd.24xlarge", "m6id.24xlarge", 4, 2)
        docs = self._parse_nodepools(output)
        assert len(docs) == 4

    def test_document_kinds(self):
        output = generate_nodepools_yaml("m8gd.24xlarge", "m6id.24xlarge", 4, 2)
        docs = self._parse_nodepools(output)
        kinds = [d["kind"] for d in docs]
        assert kinds.count("NodePool") == 2
        assert kinds.count("EC2NodeClass") == 2

    def test_instance_types_in_nodepool(self):
        output = generate_nodepools_yaml("m8gd.24xlarge", "m6id.24xlarge", 4, 2)
        docs = self._parse_nodepools(output)
        nodepools = [d for d in docs if d["kind"] == "NodePool"]
        for np in nodepools:
            reqs = np["spec"]["template"]["spec"]["requirements"]
            instance_req = [r for r in reqs if r["key"] == "node.kubernetes.io/instance-type"]
            assert len(instance_req) == 1
            instance_type = instance_req[0]["values"][0]
            assert instance_type in INSTANCE_SPECS

    def test_cpu_manager_policy_in_userdata(self):
        output = generate_nodepools_yaml("m8gd.24xlarge", "m6id.24xlarge", 4, 2)
        docs = self._parse_nodepools(output)
        ec2_classes = [d for d in docs if d["kind"] == "EC2NodeClass"]
        for ec2 in ec2_classes:
            userdata = ec2["spec"]["userData"]
            assert "cpuManagerPolicy: static" in userdata
            assert "topologyManagerPolicy: restricted" in userdata

    def test_nodepool_limits_correct(self):
        """NodePool CPU/memory limits = 2x nodes_needed * instance capacity."""
        replicas = 4
        pods_per_node = 2
        output = generate_nodepools_yaml("m8gd.24xlarge", "m6id.24xlarge", replicas, pods_per_node)
        docs = self._parse_nodepools(output)
        nodepools = [d for d in docs if d["kind"] == "NodePool"]
        for np in nodepools:
            name = np["metadata"]["name"]
            spec = INSTANCE_SPECS["m8gd.24xlarge"] if name == "buildkit-arm64" else INSTANCE_SPECS["m6id.24xlarge"]
            nodes_needed = math.ceil(replicas / pods_per_node)
            max_nodes = nodes_needed * 2
            expected_cpu = str(max_nodes * spec["vcpu"])
            expected_mem = f"{max_nodes * spec['memory_gib']}Gi"
            assert np["spec"]["limits"]["cpu"] == expected_cpu
            assert np["spec"]["limits"]["memory"] == expected_mem

    def test_instance_store_policy(self):
        """Every EC2NodeClass must have instanceStorePolicy: RAID0."""
        output = generate_nodepools_yaml("m8gd.24xlarge", "m6id.24xlarge", 4, 2)
        docs = self._parse_nodepools(output)
        ec2_classes = [d for d in docs if d["kind"] == "EC2NodeClass"]
        assert len(ec2_classes) == 2
        for ec2 in ec2_classes:
            assert ec2["spec"]["instanceStorePolicy"] == "RAID0", (
                f"EC2NodeClass {ec2['metadata']['name']} missing instanceStorePolicy: RAID0"
            )

    def test_no_shellscript_mime_part(self):
        """userData must NOT contain text/x-shellscript MIME parts."""
        output = generate_nodepools_yaml("m8gd.24xlarge", "m6id.24xlarge", 4, 2)
        docs = self._parse_nodepools(output)
        ec2_classes = [d for d in docs if d["kind"] == "EC2NodeClass"]
        for ec2 in ec2_classes:
            userdata = ec2["spec"]["userData"]
            assert "text/x-shellscript" not in userdata

    def test_taints_present(self):
        output = generate_nodepools_yaml("m8gd.24xlarge", "m6id.24xlarge", 4, 2)
        docs = self._parse_nodepools(output)
        nodepools = [d for d in docs if d["kind"] == "NodePool"]
        for np in nodepools:
            taints = np["spec"]["template"]["spec"]["taints"]
            taint_keys = [t["key"] for t in taints]
            assert "instance-type" in taint_keys

    def test_startup_taints(self):
        output = generate_nodepools_yaml("m8gd.24xlarge", "m6id.24xlarge", 4, 2)
        docs = self._parse_nodepools(output)
        nodepools = [d for d in docs if d["kind"] == "NodePool"]
        for np in nodepools:
            startup_taints = np["spec"]["template"]["spec"]["startupTaints"]
            assert any(t["key"] == "git-cache-not-ready" for t in startup_taints)


# ============================================================================
# main() — integration tests
# ============================================================================


class TestMain:
    """Integration tests for main() with CLI args and file I/O."""

    def test_creates_output_files(self, tmp_path):
        output_dir = tmp_path / "output"

        import generate_buildkit

        test_args = [
            "generate_buildkit.py",
            "--arm64-instance-type",
            "m8gd.24xlarge",
            "--amd64-instance-type",
            "m6id.24xlarge",
            "--replicas",
            "4",
            "--pods-per-node",
            "2",
            "--output-dir",
            str(output_dir),
        ]
        with patch.object(sys, "argv", test_args):
            result = generate_buildkit.main()

        assert result == 0
        assert (output_dir / "deployment.yaml").exists()
        assert (output_dir / "nodepools.yaml").exists()

    def test_deployment_yaml_parseable(self, tmp_path):
        output_dir = tmp_path / "output"

        import generate_buildkit

        test_args = [
            "generate_buildkit.py",
            "--arm64-instance-type",
            "m8gd.24xlarge",
            "--amd64-instance-type",
            "m6id.24xlarge",
            "--replicas",
            "2",
            "--pods-per-node",
            "2",
            "--output-dir",
            str(output_dir),
        ]
        with patch.object(sys, "argv", test_args):
            result = generate_buildkit.main()

        assert result == 0
        deployment_text = (output_dir / "deployment.yaml").read_text()
        docs = parse_all_yaml(deployment_text)
        assert len(docs) == 2

    def test_unknown_instance_type_fails(self, tmp_path):
        output_dir = tmp_path / "output"
        test_args = [
            "generate_buildkit.py",
            "--arm64-instance-type",
            "x99.fake",
            "--amd64-instance-type",
            "m6id.24xlarge",
            "--replicas",
            "4",
            "--pods-per-node",
            "2",
            "--output-dir",
            str(output_dir),
        ]
        with patch.object(sys, "argv", test_args):
            import generate_buildkit

            result = generate_buildkit.main()
        assert result == 1

    def test_arch_mismatch_fails(self, tmp_path):
        """Using an amd64 instance as arm64 should fail."""
        output_dir = tmp_path / "output"
        test_args = [
            "generate_buildkit.py",
            "--arm64-instance-type",
            "m6id.24xlarge",  # amd64, not arm64
            "--amd64-instance-type",
            "m6id.24xlarge",
            "--replicas",
            "4",
            "--pods-per-node",
            "2",
            "--output-dir",
            str(output_dir),
        ]
        with patch.object(sys, "argv", test_args):
            import generate_buildkit

            result = generate_buildkit.main()
        assert result == 1
