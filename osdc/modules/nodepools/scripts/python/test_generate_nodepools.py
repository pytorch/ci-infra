"""Unit tests for generate_nodepools.py — Karpenter NodePool generator."""

import os
from pathlib import Path
from unittest.mock import patch

import pytest
import yaml
from generate_nodepools import (
    _detect_arch,
    _get_node_disk_size,
    _read_user_data_script,
    _user_data_script_mime_part,
    generate_nodepool_yaml,
    main,
)

# ============================================================================
# Helpers
# ============================================================================


def parse_all_yaml(text: str) -> list[dict]:
    """Parse multi-document YAML string, filtering None entries."""
    return [doc for doc in yaml.safe_load_all(text) if doc is not None]


def _make_nodepool_def(**overrides) -> dict:
    """Build a minimal nodepool def dict with sensible defaults."""
    base = {
        "name": "test-pool",
        "instance_type": "m5.4xlarge",
        "node_disk_size": 200,
        "gpu": False,
    }
    base.update(overrides)
    return base


REAL_DEFS_DIR = Path(__file__).parent.parent.parent / "defs"


def _load_real_def(filename: str) -> dict:
    """Load a real def file from modules/nodepools/defs/."""
    with open(REAL_DEFS_DIR / filename) as f:
        data = yaml.safe_load(f)
    return data["nodepool"]


# ============================================================================
# _detect_arch
# ============================================================================


class TestDetectArch:
    """Tests for _detect_arch — architecture detection from instance type."""

    def test_explicit_hint_amd64(self):
        assert _detect_arch("r7g.16xlarge", "amd64") == "amd64"

    def test_explicit_hint_arm64(self):
        assert _detect_arch("m5.xlarge", "arm64") == "arm64"

    def test_graviton_r7g(self):
        assert _detect_arch("r7g.16xlarge", None) == "arm64"

    def test_graviton_c7gd(self):
        assert _detect_arch("c7gd.16xlarge", None) == "arm64"

    def test_graviton_m8g(self):
        assert _detect_arch("m8g.xlarge", None) == "arm64"

    def test_graviton_m8gd(self):
        assert _detect_arch("m8gd.24xlarge", None) == "arm64"

    def test_non_graviton_m5(self):
        assert _detect_arch("m5.4xlarge", None) == "amd64"

    def test_non_graviton_r5(self):
        assert _detect_arch("r5.24xlarge", None) == "amd64"

    def test_non_graviton_g4dn(self):
        assert _detect_arch("g4dn.12xlarge", None) == "amd64"

    def test_non_graviton_g5(self):
        assert _detect_arch("g5.48xlarge", None) == "amd64"

    def test_explicit_hint_overrides_heuristic(self):
        """Explicit arch hint overrides the Graviton heuristic."""
        assert _detect_arch("r7g.16xlarge", "amd64") == "amd64"

    def test_empty_string_hint_uses_heuristic(self):
        """Empty string is falsy, so heuristic is used."""
        assert _detect_arch("r7g.16xlarge", "") == "arm64"


# ============================================================================
# _get_node_disk_size
# ============================================================================


class TestGetNodeDiskSize:
    """Tests for _get_node_disk_size — EBS volume size calculation."""

    def test_direct_node_disk_size(self):
        assert _get_node_disk_size({"node_disk_size": 500}) == 500

    def test_legacy_fallback_defaults(self):
        """No node_disk_size → max_pods_per_node * disk_size + 100."""
        result = _get_node_disk_size({})
        # defaults: max_pods_per_node=10, disk_size=100
        assert result == 10 * 100 + 100

    def test_legacy_fallback_custom_values(self):
        result = _get_node_disk_size({"max_pods_per_node": 5, "disk_size": 200})
        assert result == 5 * 200 + 100

    def test_node_disk_size_zero_uses_fallback(self):
        """node_disk_size=0 is falsy, so fallback is used."""
        result = _get_node_disk_size({"node_disk_size": 0, "max_pods_per_node": 2, "disk_size": 50})
        assert result == 2 * 50 + 100


# ============================================================================
# _read_user_data_script
# ============================================================================


class TestReadUserDataScript:
    """Tests for _read_user_data_script — file I/O + indentation."""

    def test_none_path_returns_none(self):
        assert _read_user_data_script(None, Path("/tmp")) is None

    def test_empty_path_returns_none(self):
        assert _read_user_data_script("", Path("/tmp")) is None

    def test_missing_file_raises(self, tmp_path):
        defs_dir = tmp_path / "defs"
        defs_dir.mkdir()
        with pytest.raises(FileNotFoundError):
            _read_user_data_script("scripts/nonexistent.sh", defs_dir)

    def test_reads_and_indents(self, tmp_path):
        # Structure: module_dir/scripts/test.sh, defs_dir = module_dir/defs
        module_dir = tmp_path / "mymodule"
        module_dir.mkdir()
        defs_dir = module_dir / "defs"
        defs_dir.mkdir()
        scripts_dir = module_dir / "scripts"
        scripts_dir.mkdir()
        script = scripts_dir / "test.sh"
        script.write_text("#!/bin/bash\necho hello\n")

        result = _read_user_data_script("scripts/test.sh", defs_dir)
        lines = result.splitlines()
        assert lines[0] == "    #!/bin/bash"
        assert lines[1] == "    echo hello"

    def test_blank_lines_not_indented(self, tmp_path):
        module_dir = tmp_path / "mod"
        module_dir.mkdir()
        defs_dir = module_dir / "defs"
        defs_dir.mkdir()
        script = module_dir / "s.sh"
        script.write_text("line1\n\nline2\n")

        result = _read_user_data_script("s.sh", defs_dir)
        lines = result.splitlines()
        assert lines[0] == "    line1"
        assert lines[1] == ""
        assert lines[2] == "    line2"


# ============================================================================
# _user_data_script_mime_part
# ============================================================================


class TestUserDataScriptMimePart:
    """Tests for _user_data_script_mime_part — MIME block generation."""

    def test_none_returns_empty(self):
        assert _user_data_script_mime_part(None) == ""

    def test_empty_returns_empty(self):
        assert _user_data_script_mime_part("") == ""

    def test_contains_mime_headers(self):
        result = _user_data_script_mime_part("    echo hello")
        assert "--==BOUNDARY==" in result
        assert 'Content-Type: text/x-shellscript; charset="us-ascii"' in result

    def test_script_content_embedded(self):
        result = _user_data_script_mime_part("    echo hello")
        assert "    echo hello" in result


# ============================================================================
# generate_nodepool_yaml — core generator
# ============================================================================


class TestGenerateNodepoolYaml:
    """Tests for generate_nodepool_yaml — NodePool + EC2NodeClass output."""

    def _parse(self, text: str) -> list[dict]:
        return parse_all_yaml(text)

    def test_produces_two_documents(self):
        nodepool_def = _make_nodepool_def()
        output = generate_nodepool_yaml(nodepool_def, "nodepools")
        docs = self._parse(output)
        assert len(docs) == 2

    def test_document_kinds(self):
        nodepool_def = _make_nodepool_def()
        output = generate_nodepool_yaml(nodepool_def, "nodepools")
        docs = self._parse(output)
        assert docs[0]["kind"] == "NodePool"
        assert docs[1]["kind"] == "EC2NodeClass"

    def test_module_label(self):
        nodepool_def = _make_nodepool_def()
        output = generate_nodepool_yaml(nodepool_def, "my-module")
        docs = self._parse(output)
        for doc in docs:
            assert doc["metadata"]["labels"]["osdc.io/module"] == "my-module"

    def test_cpu_nodepool_no_gpu_taint(self):
        nodepool_def = _make_nodepool_def(gpu=False)
        output = generate_nodepool_yaml(nodepool_def, "nodepools")
        docs = self._parse(output)
        np = docs[0]
        taints = np["spec"]["template"]["spec"]["taints"]
        taint_keys = [t["key"] for t in taints]
        assert "instance-type" in taint_keys
        assert "nvidia.com/gpu" not in taint_keys

    def test_gpu_nodepool_has_gpu_taint(self):
        nodepool_def = _make_nodepool_def(gpu=True, instance_type="g4dn.12xlarge", arch="amd64")
        output = generate_nodepool_yaml(nodepool_def, "nodepools")
        docs = self._parse(output)
        np = docs[0]
        taints = np["spec"]["template"]["spec"]["taints"]
        taint_keys = [t["key"] for t in taints]
        assert "nvidia.com/gpu" in taint_keys

    def test_gpu_nodepool_has_gpu_label(self):
        nodepool_def = _make_nodepool_def(gpu=True, instance_type="g4dn.12xlarge", arch="amd64")
        output = generate_nodepool_yaml(nodepool_def, "nodepools")
        docs = self._parse(output)
        np = docs[0]
        labels = np["spec"]["template"]["metadata"]["labels"]
        assert labels.get("nvidia.com/gpu") == "true"

    def test_gpu_ec2nodeclass_ami_family(self):
        nodepool_def = _make_nodepool_def(gpu=True, instance_type="g4dn.12xlarge", arch="amd64")
        output = generate_nodepool_yaml(nodepool_def, "nodepools")
        docs = self._parse(output)
        ec2 = docs[1]
        assert ec2["spec"]["amiFamily"] == "AL2023"

    def test_cpu_ec2nodeclass_no_ami_family(self):
        nodepool_def = _make_nodepool_def(gpu=False)
        output = generate_nodepool_yaml(nodepool_def, "nodepools")
        docs = self._parse(output)
        ec2 = docs[1]
        assert "amiFamily" not in ec2["spec"]

    def test_startup_taints_present(self):
        nodepool_def = _make_nodepool_def()
        output = generate_nodepool_yaml(nodepool_def, "nodepools")
        docs = self._parse(output)
        np = docs[0]
        startup_taints = np["spec"]["template"]["spec"]["startupTaints"]
        assert any(t["key"] == "git-cache-not-ready" for t in startup_taints)

    def test_cluster_name_placeholder(self):
        nodepool_def = _make_nodepool_def()
        output = generate_nodepool_yaml(nodepool_def, "nodepools")
        assert "CLUSTER_NAME_PLACEHOLDER" in output

    def test_nvme_instance_store_policy(self):
        nodepool_def = _make_nodepool_def(has_nvme=True)
        output = generate_nodepool_yaml(nodepool_def, "nodepools")
        docs = self._parse(output)
        ec2 = docs[1]
        assert ec2["spec"]["instanceStorePolicy"] == "RAID0"

    def test_no_nvme_no_instance_store_policy(self):
        nodepool_def = _make_nodepool_def(has_nvme=False)
        output = generate_nodepool_yaml(nodepool_def, "nodepools")
        docs = self._parse(output)
        ec2 = docs[1]
        assert "instanceStorePolicy" not in ec2["spec"]

    def test_capacity_reservation_ids(self):
        nodepool_def = _make_nodepool_def(
            capacity_type="capacity-block",
            capacity_reservation_ids=["cr-abc123", "cr-def456"],
        )
        output = generate_nodepool_yaml(nodepool_def, "nodepools")
        docs = self._parse(output)
        ec2 = docs[1]
        terms = ec2["spec"]["capacityReservationSelectorTerms"]
        ids = [t["id"] for t in terms]
        assert "cr-abc123" in ids
        assert "cr-def456" in ids

    def test_no_capacity_reservation(self):
        nodepool_def = _make_nodepool_def()
        output = generate_nodepool_yaml(nodepool_def, "nodepools")
        docs = self._parse(output)
        ec2 = docs[1]
        assert "capacityReservationSelectorTerms" not in ec2["spec"]

    @patch.dict(os.environ, {"NODEPOOLS_COMPACTOR_ENABLED": "true"}, clear=False)
    def test_compactor_enabled_sets_when_empty(self):
        nodepool_def = _make_nodepool_def()
        output = generate_nodepool_yaml(nodepool_def, "nodepools")
        docs = self._parse(output)
        np = docs[0]
        assert np["spec"]["disruption"]["consolidationPolicy"] == "WhenEmpty"
        assert np["metadata"]["labels"].get("osdc.io/node-compactor") == "true"

    @patch.dict(os.environ, {"NODEPOOLS_COMPACTOR_ENABLED": "false"}, clear=False)
    def test_compactor_disabled_sets_underutilized(self):
        nodepool_def = _make_nodepool_def()
        output = generate_nodepool_yaml(nodepool_def, "nodepools")
        docs = self._parse(output)
        np = docs[0]
        assert np["spec"]["disruption"]["consolidationPolicy"] == "WhenEmptyOrUnderutilized"
        assert "osdc.io/node-compactor" not in np["metadata"].get("labels", {})

    def test_per_def_compactor_override(self):
        """Per-def node_compactor=true overrides cluster default of false."""
        with patch.dict(os.environ, {"NODEPOOLS_COMPACTOR_ENABLED": "false"}, clear=False):
            nodepool_def = _make_nodepool_def(node_compactor=True)
            output = generate_nodepool_yaml(nodepool_def, "nodepools")
            docs = self._parse(output)
            np = docs[0]
            assert np["spec"]["disruption"]["consolidationPolicy"] == "WhenEmpty"

    @patch.dict(
        os.environ,
        {
            "NODEPOOLS_COMPACTOR_ENABLED": "true",
            "NODEPOOLS_GPU_CONSOLIDATE_AFTER": "20m",
            "NODEPOOLS_BAREMETAL_CONSOLIDATE_AFTER": "1h",
        },
        clear=False,
    )
    def test_baremetal_gpu_uses_baremetal_consolidate_after(self):
        """Baremetal GPU nodepool uses the baremetal consolidate_after env var."""
        nodepool_def = _make_nodepool_def(gpu=True, instance_type="g4dn.metal", arch="amd64", baremetal=True)
        output = generate_nodepool_yaml(nodepool_def, "nodepools")
        docs = self._parse(output)
        np = docs[0]
        assert np["spec"]["disruption"]["consolidateAfter"] == "1h"

    @patch.dict(
        os.environ,
        {
            "NODEPOOLS_COMPACTOR_ENABLED": "true",
            "NODEPOOLS_CPU_CONSOLIDATE_AFTER": "20m",
            "NODEPOOLS_BAREMETAL_CONSOLIDATE_AFTER": "1h",
        },
        clear=False,
    )
    def test_baremetal_cpu_uses_baremetal_consolidate_after(self):
        """Baremetal CPU nodepool uses the baremetal consolidate_after env var."""
        nodepool_def = _make_nodepool_def(gpu=False, baremetal=True)
        output = generate_nodepool_yaml(nodepool_def, "nodepools")
        docs = self._parse(output)
        np = docs[0]
        assert np["spec"]["disruption"]["consolidateAfter"] == "1h"

    @patch.dict(
        os.environ,
        {"NODEPOOLS_COMPACTOR_ENABLED": "true", "NODEPOOLS_GPU_CONSOLIDATE_AFTER": "20m"},
        clear=False,
    )
    def test_non_baremetal_gpu_uses_gpu_default(self):
        """Non-baremetal GPU nodepool uses the GPU consolidate_after env var."""
        nodepool_def = _make_nodepool_def(gpu=True, instance_type="g4dn.12xlarge", arch="amd64")
        output = generate_nodepool_yaml(nodepool_def, "nodepools")
        docs = self._parse(output)
        np = docs[0]
        assert np["spec"]["disruption"]["consolidateAfter"] == "20m"

    @patch.dict(
        os.environ,
        {
            "NODEPOOLS_COMPACTOR_ENABLED": "true",
            "NODEPOOLS_GPU_CONSOLIDATE_AFTER": "10s",
            "NODEPOOLS_BAREMETAL_CONSOLIDATE_AFTER": "",
        },
        clear=False,
    )
    def test_baremetal_empty_env_falls_through_to_gpu_default(self):
        """Baremetal with empty env var falls through to GPU/CPU default (staging)."""
        nodepool_def = _make_nodepool_def(gpu=True, instance_type="g4dn.metal", arch="amd64", baremetal=True)
        output = generate_nodepool_yaml(nodepool_def, "nodepools")
        docs = self._parse(output)
        np = docs[0]
        assert np["spec"]["disruption"]["consolidateAfter"] == "10s"

    def test_gpu_no_compactor_disruption_zero(self):
        """GPU + no compactor → disruption budget 0."""
        with patch.dict(os.environ, {"NODEPOOLS_COMPACTOR_ENABLED": "false"}, clear=False):
            nodepool_def = _make_nodepool_def(gpu=True, instance_type="g4dn.12xlarge", arch="amd64")
            output = generate_nodepool_yaml(nodepool_def, "nodepools")
            docs = self._parse(output)
            np = docs[0]
            assert np["spec"]["disruption"]["budgets"][0]["nodes"] == "0"

    def test_topology_manager_defaults(self):
        nodepool_def = _make_nodepool_def()
        output = generate_nodepool_yaml(nodepool_def, "nodepools")
        docs = self._parse(output)
        ec2 = docs[1]
        userdata = ec2["spec"]["userData"]
        assert "topologyManagerPolicy: restricted" in userdata
        assert "topologyManagerScope: container" in userdata

    def test_topology_manager_custom(self):
        nodepool_def = _make_nodepool_def(
            topology_manager_policy="single-numa-node",
            topology_manager_scope="pod",
        )
        output = generate_nodepool_yaml(nodepool_def, "nodepools")
        docs = self._parse(output)
        ec2 = docs[1]
        userdata = ec2["spec"]["userData"]
        assert "topologyManagerPolicy: single-numa-node" in userdata
        assert "topologyManagerScope: pod" in userdata

    def test_container_log_rotation_present(self):
        nodepool_def = _make_nodepool_def()
        output = generate_nodepool_yaml(nodepool_def, "nodepools")
        docs = self._parse(output)
        ec2 = docs[1]
        userdata = ec2["spec"]["userData"]
        assert "containerLogMaxSize: 50Mi" in userdata
        assert "containerLogMaxFiles: 5" in userdata

    def test_container_log_rotation_with_topology_options(self):
        """Log rotation must appear after topologyManagerPolicyOptions on own lines."""
        nodepool_def = _make_nodepool_def(
            topology_manager_policy="restricted",
            topology_manager_scope="container",
        )
        output = generate_nodepool_yaml(nodepool_def, "nodepools")
        docs = self._parse(output)
        ec2 = docs[1]
        userdata = ec2["spec"]["userData"]
        assert "topologyManagerPolicyOptions:" in userdata
        assert "containerLogMaxSize: 50Mi" in userdata
        assert "containerLogMaxFiles: 5" in userdata
        # Ensure containerLogMaxSize is on its own line (not concatenated)
        for line in userdata.splitlines():
            if "containerLogMaxSize" in line:
                assert line.strip() == "containerLogMaxSize: 50Mi"
            if "containerLogMaxFiles" in line:
                assert line.strip() == "containerLogMaxFiles: 5"

    def test_block_device_disk_size(self):
        nodepool_def = _make_nodepool_def(node_disk_size=2500)
        output = generate_nodepool_yaml(nodepool_def, "nodepools")
        docs = self._parse(output)
        ec2 = docs[1]
        bdm = ec2["spec"]["blockDeviceMappings"][0]
        assert bdm["ebs"]["volumeSize"] == "2500Gi"

    def test_gpu_iops_and_throughput(self):
        nodepool_def = _make_nodepool_def(gpu=True, instance_type="g4dn.12xlarge", arch="amd64")
        output = generate_nodepool_yaml(nodepool_def, "nodepools")
        docs = self._parse(output)
        ec2 = docs[1]
        bdm = ec2["spec"]["blockDeviceMappings"][0]
        assert bdm["ebs"]["iops"] == 16000
        assert bdm["ebs"]["throughput"] == 1000

    def test_cpu_iops_and_throughput(self):
        nodepool_def = _make_nodepool_def(gpu=False)
        output = generate_nodepool_yaml(nodepool_def, "nodepools")
        docs = self._parse(output)
        ec2 = docs[1]
        bdm = ec2["spec"]["blockDeviceMappings"][0]
        assert bdm["ebs"]["iops"] == 16000
        assert bdm["ebs"]["throughput"] == 1000

    def test_gpu_tags(self):
        nodepool_def = _make_nodepool_def(gpu=True, instance_type="g4dn.12xlarge", arch="amd64")
        output = generate_nodepool_yaml(nodepool_def, "nodepools")
        docs = self._parse(output)
        ec2 = docs[1]
        assert ec2["spec"]["tags"]["GPU"] == "nvidia"

    def test_cpu_no_gpu_tags(self):
        nodepool_def = _make_nodepool_def(gpu=False)
        output = generate_nodepool_yaml(nodepool_def, "nodepools")
        docs = self._parse(output)
        ec2 = docs[1]
        assert "GPU" not in ec2["spec"]["tags"]


# ============================================================================
# Real def files — round-trip validation
# ============================================================================


class TestRealDefFiles:
    """Round-trip tests using actual def files from modules/nodepools/defs/."""

    @pytest.fixture(params=sorted(REAL_DEFS_DIR.glob("*.yaml")), ids=lambda p: p.stem)
    def real_def(self, request):
        with open(request.param) as f:
            data = yaml.safe_load(f)
        return data["nodepool"]

    def test_generates_valid_yaml(self, real_def):
        output = generate_nodepool_yaml(real_def, "nodepools", REAL_DEFS_DIR)
        docs = parse_all_yaml(output)
        assert len(docs) == 2
        assert docs[0]["kind"] == "NodePool"
        assert docs[1]["kind"] == "EC2NodeClass"

    def test_name_matches(self, real_def):
        output = generate_nodepool_yaml(real_def, "nodepools", REAL_DEFS_DIR)
        docs = parse_all_yaml(output)
        assert docs[0]["metadata"]["name"] == real_def["name"]
        assert docs[1]["metadata"]["name"] == real_def["name"]

    def test_instance_type_in_requirements(self, real_def):
        output = generate_nodepool_yaml(real_def, "nodepools", REAL_DEFS_DIR)
        docs = parse_all_yaml(output)
        np = docs[0]
        reqs = np["spec"]["template"]["spec"]["requirements"]
        instance_req = [r for r in reqs if r["key"] == "node.kubernetes.io/instance-type"]
        assert instance_req[0]["values"] == [real_def["instance_type"]]


# ============================================================================
# main() — integration tests
# ============================================================================


class TestMain:
    """Integration tests for main() with env vars and temp dirs."""

    def _create_defs(self, tmp_path, defs: list[dict]) -> Path:
        """Create def files in a temp directory, return defs dir path."""
        defs_dir = tmp_path / "defs"
        defs_dir.mkdir()
        for i, d in enumerate(defs):
            (defs_dir / f"pool{i}.yaml").write_text(yaml.dump({"nodepool": d}))
        return defs_dir

    def test_generates_correct_count(self, tmp_path):
        defs_dir = self._create_defs(
            tmp_path,
            [
                _make_nodepool_def(name="pool-a", instance_type="m5.4xlarge"),
                _make_nodepool_def(name="pool-b", instance_type="r5.24xlarge"),
            ],
        )
        output_dir = tmp_path / "generated"

        env = {
            "NODEPOOLS_DEFS_DIR": str(defs_dir),
            "NODEPOOLS_OUTPUT_DIR": str(output_dir),
            "NODEPOOLS_MODULE_NAME": "test",
        }
        with patch.dict(os.environ, env, clear=False):
            result = main()

        assert result == 0
        generated = list(output_dir.glob("*.yaml"))
        assert len(generated) == 2

    def test_cleans_output_dir(self, tmp_path):
        defs_dir = self._create_defs(
            tmp_path,
            [
                _make_nodepool_def(name="pool-a"),
            ],
        )
        output_dir = tmp_path / "generated"
        output_dir.mkdir()
        (output_dir / "stale.yaml").write_text("stale")

        env = {
            "NODEPOOLS_DEFS_DIR": str(defs_dir),
            "NODEPOOLS_OUTPUT_DIR": str(output_dir),
        }
        with patch.dict(os.environ, env, clear=False):
            main()

        assert not (output_dir / "stale.yaml").exists()
        assert (output_dir / "pool-a.yaml").exists()

    def test_no_defs_returns_error(self, tmp_path):
        defs_dir = tmp_path / "empty_defs"
        defs_dir.mkdir()
        output_dir = tmp_path / "generated"

        env = {
            "NODEPOOLS_DEFS_DIR": str(defs_dir),
            "NODEPOOLS_OUTPUT_DIR": str(output_dir),
        }
        with patch.dict(os.environ, env, clear=False):
            result = main()

        assert result == 1

    def test_invalid_def_missing_name(self, tmp_path):
        defs_dir = tmp_path / "defs"
        defs_dir.mkdir()
        (defs_dir / "bad.yaml").write_text(yaml.dump({"nodepool": {"instance_type": "m5.xlarge"}}))
        output_dir = tmp_path / "generated"

        env = {
            "NODEPOOLS_DEFS_DIR": str(defs_dir),
            "NODEPOOLS_OUTPUT_DIR": str(output_dir),
        }
        with patch.dict(os.environ, env, clear=False):
            result = main()

        # Missing name → invalid, aborts with error return code
        assert result == 1

    def test_output_files_are_parseable_yaml(self, tmp_path):
        defs_dir = self._create_defs(
            tmp_path,
            [
                _make_nodepool_def(name="parseable", instance_type="m5.4xlarge"),
            ],
        )
        output_dir = tmp_path / "generated"

        env = {
            "NODEPOOLS_DEFS_DIR": str(defs_dir),
            "NODEPOOLS_OUTPUT_DIR": str(output_dir),
        }
        with patch.dict(os.environ, env, clear=False):
            main()

        content = (output_dir / "parseable.yaml").read_text()
        docs = parse_all_yaml(content)
        assert len(docs) == 2
        assert docs[0]["kind"] == "NodePool"
        assert docs[1]["kind"] == "EC2NodeClass"
