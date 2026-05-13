"""Unit tests for generate_nodepools.py — Karpenter NodePool generator."""

import os
import sys
from pathlib import Path
from unittest.mock import patch

import pytest
import yaml

# cni_constants lives in scripts/python/ at the repo root. Ensure that path is on
# sys.path so the import works when pytest runs (mirrors the same trick
# generate_nodepools.py uses internally).
_SCRIPTS_PYTHON = str(Path(__file__).resolve().parents[4] / "scripts" / "python")
if _SCRIPTS_PYTHON not in sys.path:
    sys.path.insert(0, _SCRIPTS_PYTHON)

from cni_constants import ENI_CONFIG_LABEL, bucket_eniconfig_name  # noqa: E402
from generate_nodepools import (  # noqa: E402
    _build_fleet_nodepool_def,
    _detect_arch,
    _fleet_nodepool_name,
    _get_node_disk_size,
    _parse_azs,
    _parse_valid_buckets,
    _read_user_data_script,
    _user_data_script_mime_part,
    _validate_bucket,
    compute_pd_max_pods,
    generate_nodepool_yaml,
    main,
    resolve_max_pods,
)

# ============================================================================
# Helpers
# ============================================================================


# Default AZ list used by tests that need to invoke main() — matches a
# 3-AZ production-shaped cluster.
DEFAULT_AZS = "us-east-2a,us-east-2b,us-east-2c"
DEFAULT_AZ = "us-east-2a"


def parse_all_yaml(text: str) -> list[dict]:
    """Parse multi-document YAML string, filtering None entries."""
    return [doc for doc in yaml.safe_load_all(text) if doc is not None]


def _make_nodepool_def(**overrides) -> dict:
    """Build a minimal nodepool def dict with sensible defaults."""
    base = {
        "name": "test-pool",
        "instance_type": "m6i.32xlarge",
        "node_disk_size": 200,
        "gpu": False,
        # Pod-IP bucket (required on every workload nodepool)
        "bucket": "bucket-1",
    }
    base.update(overrides)
    return base


REAL_DEFS_DIR = Path(__file__).parent.parent.parent / "defs"

# All real defs dirs — name-length guard MUST cover GPU sister modules so a
# future long-named GPU def doesn't slip past CI.
_MODULES_DIR = Path(__file__).parent.parent.parent.parent
ALL_REAL_DEFS_DIRS = [
    _MODULES_DIR / "nodepools" / "defs",
    _MODULES_DIR / "nodepools-h100" / "defs",
    _MODULES_DIR / "nodepools-b200" / "defs",
]


def _load_real_def(filename: str) -> dict:
    """Load a real def file from modules/nodepools/defs/.

    For legacy ``nodepool:`` files, returns the nodepool dict directly.
    For ``fleet:`` or ``fleets:`` files, returns the first expanded nodepool_def.
    """
    with open(REAL_DEFS_DIR / filename) as f:
        data = yaml.safe_load(f)
    if "nodepool" in data:
        return data["nodepool"]
    if "fleet" in data:
        fleet = data["fleet"]
        return _build_fleet_nodepool_def(fleet, fleet["instances"][0])
    if "fleets" in data:
        fleet = data["fleets"][0]
        return _build_fleet_nodepool_def(fleet, fleet["instances"][0])
    raise ValueError(f"Unknown format in {filename}")


def _load_all_real_defs(defs_dir: Path | None = None) -> list[dict]:
    """Load all real def files from ``defs_dir``, expanding fleets into individual nodepool_defs.

    Default ``defs_dir`` is ``modules/nodepools/defs/`` (preserves the original
    behavior for existing callers). Pass ``modules/nodepools-h100/defs/`` or
    ``modules/nodepools-b200/defs/`` for sister-GPU-module checks.
    """
    if defs_dir is None:
        defs_dir = REAL_DEFS_DIR
    result = []
    for f in sorted(defs_dir.glob("*.yaml")):
        with open(f) as fh:
            data = yaml.safe_load(fh)
        if not data:
            continue
        if "nodepool" in data:
            result.append(data["nodepool"])
        elif "fleet" in data:
            fleet = data["fleet"]
            for inst in fleet.get("instances", []):
                result.append(_build_fleet_nodepool_def(fleet, inst))
            for inst in fleet.get("release", []):
                result.append(
                    _build_fleet_nodepool_def(
                        fleet,
                        inst,
                        name_suffix="-release",
                        extra_labels={"osdc.io/runner-class": "release"},
                    )
                )
        elif "fleets" in data:
            for fleet in data["fleets"]:
                for inst in fleet.get("instances", []):
                    result.append(_build_fleet_nodepool_def(fleet, inst))
    return result


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
        assert _detect_arch("m6i.32xlarge", None) == "amd64"

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
        assert "topologyManagerPolicy: best-effort" in userdata
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

    def test_extra_labels(self):
        nodepool_def = _make_nodepool_def(extra_labels={"osdc.io/runner-class": "release", "custom-label": "value"})
        output = generate_nodepool_yaml(nodepool_def, "nodepools")
        docs = self._parse(output)
        np = docs[0]
        labels = np["spec"]["template"]["metadata"]["labels"]
        assert labels.get("osdc.io/runner-class") == "release"
        assert labels.get("custom-label") == "value"

    def test_no_extra_labels(self):
        nodepool_def = _make_nodepool_def()
        output = generate_nodepool_yaml(nodepool_def, "nodepools")
        docs = self._parse(output)
        np = docs[0]
        labels = np["spec"]["template"]["metadata"]["labels"]
        assert "osdc.io/runner-class" not in labels


# ============================================================================
# Fleet-specific tests
# ============================================================================


class TestFleetNodepoolGeneration:
    """Tests for fleet-format nodepool generation (weight, fleet label, fleet taint)."""

    def _parse(self, text: str) -> list[dict]:
        return parse_all_yaml(text)

    def test_fleet_weight_in_spec(self):
        nodepool_def = _make_nodepool_def(fleet_name="r7a", weight=100)
        output = generate_nodepool_yaml(nodepool_def, "nodepools")
        docs = self._parse(output)
        np = docs[0]
        assert np["spec"]["weight"] == 100

    def test_no_weight_without_fleet(self):
        nodepool_def = _make_nodepool_def()
        output = generate_nodepool_yaml(nodepool_def, "nodepools")
        docs = self._parse(output)
        np = docs[0]
        assert "weight" not in np["spec"]

    def test_fleet_label_present(self):
        nodepool_def = _make_nodepool_def(fleet_name="r7a", weight=80)
        output = generate_nodepool_yaml(nodepool_def, "nodepools")
        docs = self._parse(output)
        np = docs[0]
        labels = np["spec"]["template"]["metadata"]["labels"]
        assert labels.get("node-fleet") == "r7a"

    def test_no_fleet_label_without_fleet(self):
        nodepool_def = _make_nodepool_def()
        output = generate_nodepool_yaml(nodepool_def, "nodepools")
        docs = self._parse(output)
        np = docs[0]
        labels = np["spec"]["template"]["metadata"]["labels"]
        assert "node-fleet" not in labels

    def test_fleet_taint_present(self):
        nodepool_def = _make_nodepool_def(fleet_name="g5", weight=100)
        output = generate_nodepool_yaml(nodepool_def, "nodepools")
        docs = self._parse(output)
        np = docs[0]
        taints = np["spec"]["template"]["spec"]["taints"]
        fleet_taints = [t for t in taints if t["key"] == "node-fleet"]
        assert len(fleet_taints) == 1
        assert fleet_taints[0]["value"] == "g5"
        assert fleet_taints[0]["effect"] == "NoSchedule"

    def test_fleet_taint_before_instance_taint(self):
        nodepool_def = _make_nodepool_def(fleet_name="r7a", weight=100)
        output = generate_nodepool_yaml(nodepool_def, "nodepools")
        docs = self._parse(output)
        np = docs[0]
        taints = np["spec"]["template"]["spec"]["taints"]
        taint_keys = [t["key"] for t in taints]
        fleet_idx = taint_keys.index("node-fleet")
        instance_idx = taint_keys.index("instance-type")
        assert fleet_idx < instance_idx

    def test_no_fleet_taint_without_fleet(self):
        nodepool_def = _make_nodepool_def()
        output = generate_nodepool_yaml(nodepool_def, "nodepools")
        docs = self._parse(output)
        np = docs[0]
        taints = np["spec"]["template"]["spec"]["taints"]
        taint_keys = [t["key"] for t in taints]
        assert "node-fleet" not in taint_keys

    def test_instance_type_taint_still_present_with_fleet(self):
        nodepool_def = _make_nodepool_def(fleet_name="r7a", weight=50)
        output = generate_nodepool_yaml(nodepool_def, "nodepools")
        docs = self._parse(output)
        np = docs[0]
        taints = np["spec"]["template"]["spec"]["taints"]
        taint_keys = [t["key"] for t in taints]
        assert "instance-type" in taint_keys

    def test_fleet_gpu_has_all_taints(self):
        """Fleet GPU nodepool has fleet taint + instance-type taint + GPU taint."""
        nodepool_def = _make_nodepool_def(
            fleet_name="g5",
            weight=100,
            gpu=True,
            instance_type="g5.8xlarge",
            arch="amd64",
        )
        output = generate_nodepool_yaml(nodepool_def, "nodepools")
        docs = self._parse(output)
        np = docs[0]
        taints = np["spec"]["template"]["spec"]["taints"]
        taint_keys = [t["key"] for t in taints]
        assert "node-fleet" in taint_keys
        assert "instance-type" in taint_keys
        assert "nvidia.com/gpu" in taint_keys


class TestFleetNodepoolName:
    """Tests for _fleet_nodepool_name — fleet/instance name disambiguation."""

    def test_fleet_name_matches_instance_family(self):
        """Default case: fleet name == instance family → use instance type as name."""
        assert _fleet_nodepool_name("c7i", "c7i.48xlarge") == "c7i-48xlarge"
        assert _fleet_nodepool_name("g5", "g5.8xlarge") == "g5-8xlarge"
        assert _fleet_nodepool_name("c7i", "c7i.metal-24xl") == "c7i-metal-24xl"

    def test_fleet_name_differs_from_instance_family(self):
        """Disambiguation case: prepend fleet name to size when families differ."""
        assert _fleet_nodepool_name("c7i-runner", "c7i.48xlarge") == "c7i-runner-48xlarge"
        assert _fleet_nodepool_name("c7i-runner", "c7i.metal-24xl") == "c7i-runner-metal-24xl"

    def test_release_suffix_with_matching_family(self):
        assert _fleet_nodepool_name("r7a", "r7a.48xlarge", name_suffix="-release") == "r7a-48xlarge-release"

    def test_release_suffix_with_differing_family(self):
        assert (
            _fleet_nodepool_name("c7i-runner", "c7i.48xlarge", name_suffix="-release") == "c7i-runner-48xlarge-release"
        )


class TestBuildFleetNodepoolDef:
    """Tests for _build_fleet_nodepool_def helper."""

    def test_basic_construction(self):
        fleet = {"name": "r7a", "arch": "amd64", "gpu": False}
        inst = {"type": "r7a.48xlarge", "weight": 100, "node_disk_size": 4800}
        result = _build_fleet_nodepool_def(fleet, inst)
        assert result["name"] == "r7a-48xlarge"
        assert result["instance_type"] == "r7a.48xlarge"
        assert result["arch"] == "amd64"
        assert result["gpu"] is False
        assert result["fleet_name"] == "r7a"
        assert result["weight"] == 100
        assert result["node_disk_size"] == 4800

    def test_release_suffix(self):
        fleet = {"name": "r7a", "arch": "amd64", "gpu": False}
        inst = {"type": "r7a.48xlarge", "weight": 100, "node_disk_size": 4800}
        result = _build_fleet_nodepool_def(
            fleet,
            inst,
            name_suffix="-release",
            extra_labels={"osdc.io/runner-class": "release"},
        )
        assert result["name"] == "r7a-48xlarge-release"
        assert result["extra_labels"]["osdc.io/runner-class"] == "release"

    def test_gpu_fleet(self):
        fleet = {"name": "g5", "arch": "amd64", "gpu": True}
        inst = {"type": "g5.8xlarge", "weight": 100, "node_disk_size": 600, "has_nvme": True}
        result = _build_fleet_nodepool_def(fleet, inst)
        assert result["gpu"] is True
        assert result["has_nvme"] is True

    def test_baremetal_flag(self):
        fleet = {"name": "g4dn", "arch": "amd64", "gpu": True}
        inst = {"type": "g4dn.metal", "weight": 100, "node_disk_size": 600, "baremetal": True}
        result = _build_fleet_nodepool_def(fleet, inst)
        assert result["baremetal"] is True

    def test_optional_fields_default_correctly(self):
        fleet = {"name": "r7a", "arch": "amd64"}
        inst = {"type": "r7a.8xlarge", "weight": 20, "node_disk_size": 800}
        result = _build_fleet_nodepool_def(fleet, inst)
        assert result["capacity_type"] == "on-demand"
        assert result["capacity_reservation_ids"] == []
        assert result["extra_labels"] == {}
        # Optional keys with None defaults are omitted so generate_nodepool_yaml
        # falls through to its own defaults (e.g. topology_manager_policy="restricted")
        assert "topology_manager_policy" not in result
        assert "topology_manager_scope" not in result
        assert "user_data_script" not in result
        assert "node_compactor" not in result


# ============================================================================
# Real def files — round-trip validation
# ============================================================================


class TestRealDefFiles:
    """Round-trip tests using actual def files from modules/nodepools/defs/.

    Supports both legacy ``nodepool:`` files and new ``fleet:``/``fleets:`` files.
    Uses ``DEFAULT_AZ`` for the AZ-pinned variant — a single AZ is enough to
    exercise the rendering path; the AZ-expansion logic is covered separately
    in TestAzExpansion.
    """

    @pytest.fixture(params=_load_all_real_defs(), ids=lambda d: d["name"])
    def real_def(self, request):
        return request.param

    def test_real_def_has_valid_bucket(self, real_def):
        """Every real def must declare a valid bucket."""
        _validate_bucket(real_def.get("bucket"), where=f"real def {real_def['name']!r}")

    def test_generates_valid_yaml(self, real_def):
        output = generate_nodepool_yaml(real_def, "nodepools", REAL_DEFS_DIR, az=DEFAULT_AZ)
        docs = parse_all_yaml(output)
        assert len(docs) == 2
        assert docs[0]["kind"] == "NodePool"
        assert docs[1]["kind"] == "EC2NodeClass"

    def test_name_matches(self, real_def):
        output = generate_nodepool_yaml(real_def, "nodepools", REAL_DEFS_DIR, az=DEFAULT_AZ)
        docs = parse_all_yaml(output)
        expected_name = f"{real_def['name']}-{DEFAULT_AZ}"
        assert docs[0]["metadata"]["name"] == expected_name
        assert docs[1]["metadata"]["name"] == expected_name

    def test_instance_type_in_requirements(self, real_def):
        output = generate_nodepool_yaml(real_def, "nodepools", REAL_DEFS_DIR, az=DEFAULT_AZ)
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
                _make_nodepool_def(name="pool-a", instance_type="m6i.32xlarge"),
                _make_nodepool_def(name="pool-b", instance_type="r5.24xlarge"),
            ],
        )
        output_dir = tmp_path / "generated"

        env = {
            "NODEPOOLS_DEFS_DIR": str(defs_dir),
            "NODEPOOLS_OUTPUT_DIR": str(output_dir),
            "NODEPOOLS_MODULE_NAME": "test",
            "NODEPOOLS_AZS": DEFAULT_AZS,
        }
        with patch.dict(os.environ, env, clear=False):
            result = main()

        assert result == 0
        generated = list(output_dir.glob("*.yaml"))
        # 2 defs x 3 AZs = 6 generated NodePool/EC2NodeClass pairs
        assert len(generated) == 6

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
            "NODEPOOLS_AZS": DEFAULT_AZS,
        }
        with patch.dict(os.environ, env, clear=False):
            main()

        assert not (output_dir / "stale.yaml").exists()
        # AZ-suffixed names replace the legacy bare name
        assert (output_dir / "pool-a-us-east-2a.yaml").exists()
        assert (output_dir / "pool-a-us-east-2b.yaml").exists()
        assert (output_dir / "pool-a-us-east-2c.yaml").exists()

    def test_no_defs_returns_error(self, tmp_path):
        defs_dir = tmp_path / "empty_defs"
        defs_dir.mkdir()
        output_dir = tmp_path / "generated"

        env = {
            "NODEPOOLS_DEFS_DIR": str(defs_dir),
            "NODEPOOLS_OUTPUT_DIR": str(output_dir),
            "NODEPOOLS_AZS": DEFAULT_AZS,
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
            "NODEPOOLS_AZS": DEFAULT_AZS,
        }
        with patch.dict(os.environ, env, clear=False):
            result = main()

        # Missing name → invalid, aborts with error return code
        assert result == 1

    def test_legacy_nodepool_unknown_instance_type(self, tmp_path):
        """Legacy nodepool defs with instance types not in INSTANCE_SPECS fail."""
        defs_dir = self._create_defs(
            tmp_path,
            [_make_nodepool_def(name="unknown-pool", instance_type="z99.xlarge")],
        )
        output_dir = tmp_path / "generated"

        env = {
            "NODEPOOLS_DEFS_DIR": str(defs_dir),
            "NODEPOOLS_OUTPUT_DIR": str(output_dir),
            "NODEPOOLS_AZS": DEFAULT_AZS,
        }
        with patch.dict(os.environ, env, clear=False):
            result = main()

        assert result == 1

    def test_output_files_are_parseable_yaml(self, tmp_path):
        defs_dir = self._create_defs(
            tmp_path,
            [
                _make_nodepool_def(name="parseable", instance_type="m6i.32xlarge"),
            ],
        )
        output_dir = tmp_path / "generated"

        env = {
            "NODEPOOLS_DEFS_DIR": str(defs_dir),
            "NODEPOOLS_OUTPUT_DIR": str(output_dir),
            "NODEPOOLS_AZS": DEFAULT_AZS,
        }
        with patch.dict(os.environ, env, clear=False):
            main()

        # Verify every AZ variant parses as the expected pair
        for az in DEFAULT_AZS.split(","):
            content = (output_dir / f"parseable-{az}.yaml").read_text()
            docs = parse_all_yaml(content)
            assert len(docs) == 2
            assert docs[0]["kind"] == "NodePool"
            assert docs[1]["kind"] == "EC2NodeClass"

    def test_fleet_format_generates_multiple(self, tmp_path):
        """A fleet file with 3 instances x 3 AZs generates 9 output files."""
        defs_dir = tmp_path / "defs"
        defs_dir.mkdir()
        fleet_data = {
            "fleet": {
                "name": "r7a",
                "arch": "amd64",
                "gpu": False,
                "bucket": "bucket-3",
                "instances": [
                    {"type": "r7a.48xlarge", "weight": 100, "node_disk_size": 4800},
                    {"type": "r7a.24xlarge", "weight": 80, "node_disk_size": 2400},
                    {"type": "r7a.8xlarge", "weight": 20, "node_disk_size": 800},
                ],
            }
        }
        (defs_dir / "r7a.yaml").write_text(yaml.dump(fleet_data))
        output_dir = tmp_path / "generated"

        env = {
            "NODEPOOLS_DEFS_DIR": str(defs_dir),
            "NODEPOOLS_OUTPUT_DIR": str(output_dir),
            "NODEPOOLS_MODULE_NAME": "test",
            "NODEPOOLS_AZS": DEFAULT_AZS,
        }
        with patch.dict(os.environ, env, clear=False):
            result = main()

        assert result == 0
        generated = sorted(f.name for f in output_dir.glob("*.yaml"))
        expected = sorted(
            f"r7a-{size}-{az}.yaml" for size in ("8xlarge", "24xlarge", "48xlarge") for az in DEFAULT_AZS.split(",")
        )
        assert generated == expected

    def test_fleet_with_release_generates_extra(self, tmp_path):
        """Fleet with release entries generates both regular and release outputs (per-AZ)."""
        defs_dir = tmp_path / "defs"
        defs_dir.mkdir()
        fleet_data = {
            "fleet": {
                "name": "r7a",
                "arch": "amd64",
                "gpu": False,
                "bucket": "bucket-3",
                "instances": [
                    {"type": "r7a.48xlarge", "weight": 100, "node_disk_size": 4800},
                ],
                "release": [
                    {"type": "r7a.48xlarge", "weight": 100, "node_disk_size": 4800},
                ],
            }
        }
        (defs_dir / "r7a.yaml").write_text(yaml.dump(fleet_data))
        output_dir = tmp_path / "generated"

        env = {
            "NODEPOOLS_DEFS_DIR": str(defs_dir),
            "NODEPOOLS_OUTPUT_DIR": str(output_dir),
            "NODEPOOLS_AZS": DEFAULT_AZS,
        }
        with patch.dict(os.environ, env, clear=False):
            result = main()

        assert result == 0
        generated = sorted(f.name for f in output_dir.glob("*.yaml"))
        expected = sorted(
            [f"r7a-48xlarge-{az}.yaml" for az in DEFAULT_AZS.split(",")]
            + [f"r7a-48xlarge-release-{az}.yaml" for az in DEFAULT_AZS.split(",")]
        )
        assert generated == expected

        # Verify release has the runner-class label (check one AZ)
        release_content = (output_dir / "r7a-48xlarge-release-us-east-2a.yaml").read_text()
        docs = parse_all_yaml(release_content)
        labels = docs[0]["spec"]["template"]["metadata"]["labels"]
        assert labels.get("osdc.io/runner-class") == "release"

    def test_fleets_format_multi_fleet(self, tmp_path):
        """A fleets file (multi-fleet) generates outputs for all fleets, per AZ."""
        defs_dir = tmp_path / "defs"
        defs_dir.mkdir()
        fleet_data = {
            "fleets": [
                {
                    "name": "fleet-alpha",
                    "arch": "amd64",
                    "gpu": True,
                    "bucket": "bucket-2",
                    "instances": [
                        {"type": "g5.8xlarge", "weight": 100, "node_disk_size": 600, "has_nvme": True},
                    ],
                },
                {
                    "name": "fleet-beta",
                    "arch": "amd64",
                    "gpu": True,
                    "bucket": "bucket-2",
                    "instances": [
                        {"type": "g5.12xlarge", "weight": 100, "node_disk_size": 600, "has_nvme": True},
                    ],
                },
            ]
        }
        (defs_dir / "g5.yaml").write_text(yaml.dump(fleet_data))
        output_dir = tmp_path / "generated"

        env = {
            "NODEPOOLS_DEFS_DIR": str(defs_dir),
            "NODEPOOLS_OUTPUT_DIR": str(output_dir),
            "NODEPOOLS_AZS": DEFAULT_AZS,
        }
        with patch.dict(os.environ, env, clear=False):
            result = main()

        assert result == 0
        generated = sorted(f.name for f in output_dir.glob("*.yaml"))
        expected = sorted(
            [f"fleet-alpha-8xlarge-{az}.yaml" for az in DEFAULT_AZS.split(",")]
            + [f"fleet-beta-12xlarge-{az}.yaml" for az in DEFAULT_AZS.split(",")]
        )
        assert generated == expected

        # Verify fleet names are different on a single-AZ sample
        g5_8 = parse_all_yaml((output_dir / "fleet-alpha-8xlarge-us-east-2a.yaml").read_text())
        g5_12 = parse_all_yaml((output_dir / "fleet-beta-12xlarge-us-east-2a.yaml").read_text())
        assert g5_8[0]["spec"]["template"]["metadata"]["labels"]["node-fleet"] == "fleet-alpha"
        assert g5_12[0]["spec"]["template"]["metadata"]["labels"]["node-fleet"] == "fleet-beta"

    def test_mixed_nodepool_and_fleet(self, tmp_path):
        """Legacy nodepool and fleet files can coexist in the same defs dir."""
        defs_dir = tmp_path / "defs"
        defs_dir.mkdir()
        (defs_dir / "legacy.yaml").write_text(
            yaml.dump({"nodepool": _make_nodepool_def(name="legacy-pool", instance_type="r7i.48xlarge")})
        )
        fleet_data = {
            "fleet": {
                "name": "r7a",
                "arch": "amd64",
                "gpu": False,
                "bucket": "bucket-3",
                "instances": [
                    {"type": "r7a.48xlarge", "weight": 100, "node_disk_size": 4800},
                ],
            }
        }
        (defs_dir / "r7a.yaml").write_text(yaml.dump(fleet_data))
        output_dir = tmp_path / "generated"

        env = {
            "NODEPOOLS_DEFS_DIR": str(defs_dir),
            "NODEPOOLS_OUTPUT_DIR": str(output_dir),
            "NODEPOOLS_AZS": DEFAULT_AZS,
        }
        with patch.dict(os.environ, env, clear=False):
            result = main()

        assert result == 0
        generated = sorted(f.name for f in output_dir.glob("*.yaml"))
        expected = sorted(
            [f"legacy-pool-{az}.yaml" for az in DEFAULT_AZS.split(",")]
            + [f"r7a-48xlarge-{az}.yaml" for az in DEFAULT_AZS.split(",")]
        )
        assert generated == expected

    def test_fleet_missing_name_key(self, tmp_path):
        """Fleet missing required 'name' key fails with descriptive error."""
        defs_dir = tmp_path / "defs"
        defs_dir.mkdir()
        fleet_data = {
            "fleet": {
                "arch": "amd64",
                "bucket": "bucket-3",
                "instances": [
                    {"type": "r7a.48xlarge", "weight": 100, "node_disk_size": 4800},
                ],
            }
        }
        (defs_dir / "bad.yaml").write_text(yaml.dump(fleet_data))
        output_dir = tmp_path / "generated"

        env = {
            "NODEPOOLS_DEFS_DIR": str(defs_dir),
            "NODEPOOLS_OUTPUT_DIR": str(output_dir),
            "NODEPOOLS_AZS": DEFAULT_AZS,
        }
        with patch.dict(os.environ, env, clear=False):
            result = main()

        assert result == 1

    def test_fleet_missing_arch_key(self, tmp_path):
        """Fleet missing required 'arch' key fails with descriptive error."""
        defs_dir = tmp_path / "defs"
        defs_dir.mkdir()
        fleet_data = {
            "fleet": {
                "name": "r7a",
                "bucket": "bucket-3",
                "instances": [
                    {"type": "r7a.48xlarge", "weight": 100, "node_disk_size": 4800},
                ],
            }
        }
        (defs_dir / "bad.yaml").write_text(yaml.dump(fleet_data))
        output_dir = tmp_path / "generated"

        env = {
            "NODEPOOLS_DEFS_DIR": str(defs_dir),
            "NODEPOOLS_OUTPUT_DIR": str(output_dir),
            "NODEPOOLS_AZS": DEFAULT_AZS,
        }
        with patch.dict(os.environ, env, clear=False):
            result = main()

        assert result == 1

    def test_fleet_instance_missing_weight(self, tmp_path):
        """Fleet instance missing required 'weight' key fails."""
        defs_dir = tmp_path / "defs"
        defs_dir.mkdir()
        fleet_data = {
            "fleet": {
                "name": "r7a",
                "arch": "amd64",
                "bucket": "bucket-3",
                "instances": [
                    {"type": "r7a.48xlarge", "node_disk_size": 4800},
                ],
            }
        }
        (defs_dir / "bad.yaml").write_text(yaml.dump(fleet_data))
        output_dir = tmp_path / "generated"

        env = {
            "NODEPOOLS_DEFS_DIR": str(defs_dir),
            "NODEPOOLS_OUTPUT_DIR": str(output_dir),
            "NODEPOOLS_AZS": DEFAULT_AZS,
        }
        with patch.dict(os.environ, env, clear=False):
            result = main()

        assert result == 1

    def test_fleet_unknown_instance_type(self, tmp_path):
        """Fleet with instance type not in INSTANCE_SPECS fails."""
        defs_dir = tmp_path / "defs"
        defs_dir.mkdir()
        fleet_data = {
            "fleet": {
                "name": "z99",
                "arch": "amd64",
                "bucket": "bucket-1",
                "instances": [
                    {"type": "z99.xlarge", "weight": 100, "node_disk_size": 800},
                ],
            }
        }
        (defs_dir / "bad.yaml").write_text(yaml.dump(fleet_data))
        output_dir = tmp_path / "generated"

        env = {
            "NODEPOOLS_DEFS_DIR": str(defs_dir),
            "NODEPOOLS_OUTPUT_DIR": str(output_dir),
            "NODEPOOLS_AZS": DEFAULT_AZS,
        }
        with patch.dict(os.environ, env, clear=False):
            result = main()

        assert result == 1

    def test_output_dir_equals_defs_dir_is_rejected(self, tmp_path):
        """Setting NODEPOOLS_OUTPUT_DIR == NODEPOOLS_DEFS_DIR must NOT rmtree the defs."""
        defs_dir = self._create_defs(
            tmp_path,
            [_make_nodepool_def(name="keep-me", instance_type="m6i.32xlarge")],
        )
        # Sanity: file exists before main() runs
        original_files = sorted(p.name for p in defs_dir.glob("*.yaml"))
        assert original_files, "test setup bug: no defs to protect"

        env = {
            "NODEPOOLS_DEFS_DIR": str(defs_dir),
            # Misconfiguration: same path as defs_dir → would rmtree defs
            "NODEPOOLS_OUTPUT_DIR": str(defs_dir),
            "NODEPOOLS_AZS": DEFAULT_AZS,
        }
        with patch.dict(os.environ, env, clear=False):
            result = main()

        # Generator must refuse and return 1
        assert result == 1
        # Defs files must STILL exist — that's the whole point of the check
        surviving = sorted(p.name for p in defs_dir.glob("*.yaml"))
        assert surviving == original_files, f"defs files were destroyed: before={original_files} after={surviving}"

    def test_output_dir_ancestor_of_defs_dir_is_rejected(self, tmp_path):
        """Setting NODEPOOLS_OUTPUT_DIR to an ancestor of NODEPOOLS_DEFS_DIR is rejected."""
        # Layout: tmp_path/parent/defs/  with output_dir = tmp_path/parent
        parent = tmp_path / "parent"
        parent.mkdir()
        defs_dir = self._create_defs(
            parent,
            [_make_nodepool_def(name="keep-me", instance_type="m6i.32xlarge")],
        )
        original_files = sorted(p.name for p in defs_dir.glob("*.yaml"))
        assert original_files, "test setup bug: no defs to protect"

        env = {
            "NODEPOOLS_DEFS_DIR": str(defs_dir),
            "NODEPOOLS_OUTPUT_DIR": str(parent),  # ancestor of defs_dir
            "NODEPOOLS_AZS": DEFAULT_AZS,
        }
        with patch.dict(os.environ, env, clear=False):
            result = main()

        assert result == 1
        # defs/ subtree must still exist
        assert defs_dir.is_dir()
        surviving = sorted(p.name for p in defs_dir.glob("*.yaml"))
        assert surviving == original_files


# ============================================================================
# exclude_regions handling
# ============================================================================


class TestExcludeRegions:
    """Tests for honoring ``exclude_regions:`` on fleet and legacy nodepool defs."""

    def _write_fleet(self, defs_dir: Path, name: str, fleet: dict) -> None:
        (defs_dir / f"{name}.yaml").write_text(yaml.dump({"fleet": fleet}))

    def _run_main(self, defs_dir: Path, output_dir: Path, region: str | None = None) -> int:
        env = {
            "NODEPOOLS_DEFS_DIR": str(defs_dir),
            "NODEPOOLS_OUTPUT_DIR": str(output_dir),
            "NODEPOOLS_MODULE_NAME": "test",
            "NODEPOOLS_AZS": DEFAULT_AZS,
        }
        if region is not None:
            env["NODEPOOLS_REGION"] = region
        with patch.dict(os.environ, env, clear=False):
            return main()

    def test_fleet_excluded_for_matching_region(self, tmp_path):
        """A fleet with exclude_regions: [us-west-1] is skipped for a us-west-1 cluster."""
        defs_dir = tmp_path / "defs"
        defs_dir.mkdir()
        self._write_fleet(
            defs_dir,
            "g5",
            {
                "name": "g5",
                "arch": "amd64",
                "gpu": True,
                "bucket": "bucket-2",
                "exclude_regions": ["us-west-1"],
                "instances": [
                    {"type": "g5.8xlarge", "weight": 100, "node_disk_size": 600, "has_nvme": True},
                ],
            },
        )
        # Add a non-excluded fleet so main() doesn't bail on "no defs"
        self._write_fleet(
            defs_dir,
            "m6i",
            {
                "name": "m6i",
                "arch": "amd64",
                "gpu": False,
                "bucket": "bucket-3",
                "instances": [
                    {"type": "m6i.32xlarge", "weight": 100, "node_disk_size": 200},
                ],
            },
        )
        output_dir = tmp_path / "generated"

        result = self._run_main(defs_dir, output_dir, region="us-west-1")

        assert result == 0
        generated = sorted(f.name for f in output_dir.glob("*.yaml"))
        # No g5-* AZ variants
        assert not any(name.startswith("g5-") for name in generated)
        # All m6i AZ variants present
        for az in DEFAULT_AZS.split(","):
            assert f"m6i-32xlarge-{az}.yaml" in generated

    def test_fleet_rendered_for_other_region(self, tmp_path):
        """A fleet with exclude_regions: [us-west-1] is rendered for a us-east-2 cluster."""
        defs_dir = tmp_path / "defs"
        defs_dir.mkdir()
        self._write_fleet(
            defs_dir,
            "g5",
            {
                "name": "g5",
                "arch": "amd64",
                "gpu": True,
                "bucket": "bucket-2",
                "exclude_regions": ["us-west-1"],
                "instances": [
                    {"type": "g5.8xlarge", "weight": 100, "node_disk_size": 600, "has_nvme": True},
                ],
            },
        )
        output_dir = tmp_path / "generated"

        result = self._run_main(defs_dir, output_dir, region="us-east-2")

        assert result == 0
        generated = sorted(f.name for f in output_dir.glob("*.yaml"))
        expected = sorted(f"g5-8xlarge-{az}.yaml" for az in DEFAULT_AZS.split(","))
        assert generated == expected

    def test_fleet_without_exclude_regions_always_rendered(self, tmp_path):
        """A fleet with no exclude_regions is rendered for any cluster region."""
        defs_dir = tmp_path / "defs"
        defs_dir.mkdir()
        self._write_fleet(
            defs_dir,
            "m6i",
            {
                "name": "m6i",
                "arch": "amd64",
                "gpu": False,
                "bucket": "bucket-3",
                "instances": [
                    {"type": "m6i.32xlarge", "weight": 100, "node_disk_size": 200},
                ],
            },
        )

        expected = sorted(f"m6i-32xlarge-{az}.yaml" for az in DEFAULT_AZS.split(","))
        # Same def renders in every region (fresh output dir per iteration)
        for region in ("us-west-1", "us-east-2", "eu-central-1"):
            output_dir_r = tmp_path / f"generated-{region}"
            result = self._run_main(defs_dir, output_dir_r, region=region)
            assert result == 0
            generated = sorted(f.name for f in output_dir_r.glob("*.yaml"))
            assert generated == expected, f"fleet missing for region {region}"

    def test_no_region_set_renders_everything(self, tmp_path):
        """When NODEPOOLS_REGION is unset, exclude_regions is a no-op (back-compat)."""
        defs_dir = tmp_path / "defs"
        defs_dir.mkdir()
        self._write_fleet(
            defs_dir,
            "g5",
            {
                "name": "g5",
                "arch": "amd64",
                "gpu": True,
                "bucket": "bucket-2",
                "exclude_regions": ["us-west-1", "us-east-2"],
                "instances": [
                    {"type": "g5.8xlarge", "weight": 100, "node_disk_size": 600, "has_nvme": True},
                ],
            },
        )
        output_dir = tmp_path / "generated"

        result = self._run_main(defs_dir, output_dir, region=None)

        assert result == 0
        generated = sorted(f.name for f in output_dir.glob("*.yaml"))
        expected = sorted(f"g5-8xlarge-{az}.yaml" for az in DEFAULT_AZS.split(","))
        assert generated == expected

    def test_fleet_release_instances_also_excluded(self, tmp_path):
        """When a fleet is excluded, its release instances are excluded too."""
        defs_dir = tmp_path / "defs"
        defs_dir.mkdir()
        self._write_fleet(
            defs_dir,
            "r7a",
            {
                "name": "r7a",
                "arch": "amd64",
                "gpu": False,
                "bucket": "bucket-3",
                "exclude_regions": ["us-west-1"],
                "instances": [
                    {"type": "r7a.48xlarge", "weight": 100, "node_disk_size": 4800},
                ],
                "release": [
                    {"type": "r7a.48xlarge", "weight": 100, "node_disk_size": 4800},
                ],
            },
        )
        # Non-excluded fleet so main() doesn't bail on missing defs
        self._write_fleet(
            defs_dir,
            "m6i",
            {
                "name": "m6i",
                "arch": "amd64",
                "gpu": False,
                "bucket": "bucket-3",
                "instances": [
                    {"type": "m6i.32xlarge", "weight": 100, "node_disk_size": 200},
                ],
            },
        )
        output_dir = tmp_path / "generated"

        result = self._run_main(defs_dir, output_dir, region="us-west-1")

        assert result == 0
        generated = sorted(f.name for f in output_dir.glob("*.yaml"))
        # No r7a-* AZ variants of any kind (regular or release)
        assert not any(name.startswith("r7a-") for name in generated)

    def test_legacy_nodepool_excluded_for_matching_region(self, tmp_path):
        """Legacy ``nodepool:`` defs also honor exclude_regions."""
        defs_dir = tmp_path / "defs"
        defs_dir.mkdir()
        (defs_dir / "legacy.yaml").write_text(
            yaml.dump(
                {
                    "nodepool": {
                        "name": "legacy-pool",
                        "instance_type": "m6i.32xlarge",
                        "node_disk_size": 200,
                        "bucket": "bucket-3",
                        "exclude_regions": ["us-west-1"],
                    }
                }
            )
        )
        # Non-excluded fleet so main() doesn't bail on missing defs
        self._write_fleet(
            defs_dir,
            "m6i",
            {
                "name": "m6i",
                "arch": "amd64",
                "gpu": False,
                "bucket": "bucket-3",
                "instances": [
                    {"type": "m6i.32xlarge", "weight": 100, "node_disk_size": 200},
                ],
            },
        )
        output_dir = tmp_path / "generated"

        result = self._run_main(defs_dir, output_dir, region="us-west-1")

        assert result == 0
        generated = sorted(f.name for f in output_dir.glob("*.yaml"))
        # No legacy-pool-* AZ variants
        assert not any(name.startswith("legacy-pool-") for name in generated)


# ============================================================================
# Prefix-delegation max-pods
# ============================================================================


class TestComputePdMaxPods:
    """Tests for compute_pd_max_pods — the AWS max-pods-calculator formula."""

    @pytest.mark.parametrize(
        ("instance_type", "expected"),
        [
            ("c7i.48xlarge", 250),  # 14*49*16+2=10978 → cap 250
            ("c7i.8xlarge", 250),  # 7*29*16+2=3250 → cap 250
            ("r7i.2xlarge", 110),  # 3*14*16+2=674 → cap 110 (vcpu<=30)
            ("p5.48xlarge", 250),  # 1*49*16+2=786 → cap 250
            ("g5.48xlarge", 250),  # 6*49*16+2=4706 → cap 250
        ],
    )
    def test_compute_pd_max_pods_formula(self, instance_type, expected):
        assert compute_pd_max_pods(instance_type) == expected

    def test_compute_pd_max_pods_no_custom_networking(self):
        # Big instances stay capped at 250 even without custom networking.
        assert compute_pd_max_pods("c7i.48xlarge", custom_networking=False) == 250
        # Small instance still caps at 110 (vcpu<=30).
        assert compute_pd_max_pods("r7i.2xlarge", custom_networking=False) == 110


class TestResolveMaxPods:
    """Tests for resolve_max_pods — explicit override + default behavior."""

    def test_resolve_max_pods_default_caps_at_110(self):
        # No explicit max_pods → default cap of 110 even if PD ceiling is higher.
        nodepool_def = _make_nodepool_def(instance_type="c7i.48xlarge")
        nodepool_def.pop("max_pods", None)
        assert resolve_max_pods(nodepool_def) == 110

    def test_resolve_max_pods_explicit_below_ceiling(self):
        nodepool_def = _make_nodepool_def(instance_type="c7i.48xlarge", max_pods=200)
        assert resolve_max_pods(nodepool_def) == 200

    def test_resolve_max_pods_explicit_at_ceiling(self):
        nodepool_def = _make_nodepool_def(instance_type="c7i.48xlarge", max_pods=250)
        assert resolve_max_pods(nodepool_def) == 250

    def test_resolve_max_pods_explicit_above_ceiling_raises(self):
        nodepool_def = _make_nodepool_def(instance_type="c7i.48xlarge", max_pods=300)
        with pytest.raises(ValueError, match="exceeds PD ceiling"):
            resolve_max_pods(nodepool_def)

    def test_resolve_max_pods_explicit_negative_raises(self):
        nodepool_def = _make_nodepool_def(instance_type="c7i.48xlarge", max_pods=0)
        with pytest.raises(ValueError, match="must be >= 1"):
            resolve_max_pods(nodepool_def)

    def test_resolve_max_pods_explicit_non_int_raises(self):
        # str rejected
        nodepool_def_str = _make_nodepool_def(instance_type="c7i.48xlarge", max_pods="250")
        with pytest.raises(ValueError, match="must be an int"):
            resolve_max_pods(nodepool_def_str)
        # bool rejected (even though bool is technically an int subclass)
        nodepool_def_bool = _make_nodepool_def(instance_type="c7i.48xlarge", max_pods=True)
        with pytest.raises(ValueError, match="must be an int"):
            resolve_max_pods(nodepool_def_bool)


class TestEc2NodeClassMaxPods:
    """Tests for the rendered EC2NodeClass kubelet.maxPods block."""

    def _parse(self, text: str) -> list[dict]:
        return parse_all_yaml(text)

    def test_generated_ec2nodeclass_has_kubelet_max_pods(self):
        # Default (no explicit max_pods) → 110 for c7i.48xlarge.
        nodepool_def = _make_nodepool_def(instance_type="c7i.48xlarge")
        nodepool_def.pop("max_pods", None)
        output = generate_nodepool_yaml(nodepool_def, "nodepools")
        docs = self._parse(output)
        ec2 = docs[1]
        assert ec2["spec"]["kubelet"]["maxPods"] == 110

    def test_generated_ec2nodeclass_has_kubelet_max_pods_explicit(self):
        nodepool_def = _make_nodepool_def(instance_type="c7i.48xlarge", max_pods=250)
        output = generate_nodepool_yaml(nodepool_def, "nodepools")
        docs = self._parse(output)
        ec2 = docs[1]
        assert ec2["spec"]["kubelet"]["maxPods"] == 250

    def test_user_data_nodeconfig_does_NOT_have_max_pods(self):
        """Regression guard: maxPods must NOT be set inside the user-data NodeConfig.

        Karpenter sets kubelet keys via EC2NodeClass.spec.kubelet only.  Setting
        maxPods in two places risks silent disagreement and confuses Karpenter's
        scheduler math.
        """
        nodepool_def = _make_nodepool_def(instance_type="c7i.48xlarge", max_pods=250)
        output = generate_nodepool_yaml(nodepool_def, "nodepools")
        docs = self._parse(output)
        ec2 = docs[1]
        userdata = ec2["spec"]["userData"]
        assert "maxPods" not in userdata


class TestFleetMaxPodsPropagation:
    """Tests for fleet/per-instance max_pods propagation."""

    def _parse(self, text: str) -> list[dict]:
        return parse_all_yaml(text)

    def test_fleet_max_pods_propagates_to_instances(self):
        fleet = {"name": "c7i-runner", "arch": "amd64", "gpu": False, "max_pods": 250}
        instances = [
            {"type": "c7i.48xlarge", "weight": 100, "node_disk_size": 3750},
            {"type": "c7i.24xlarge", "weight": 85, "node_disk_size": 1900},
        ]
        for inst in instances:
            nodepool_def = _build_fleet_nodepool_def(fleet, inst)
            output = generate_nodepool_yaml(nodepool_def, "nodepools")
            docs = self._parse(output)
            ec2 = docs[1]
            assert ec2["spec"]["kubelet"]["maxPods"] == 250, (
                f"{inst['type']}: expected 250, got {ec2['spec']['kubelet']['maxPods']}"
            )

    def test_per_instance_max_pods_overrides_fleet(self):
        fleet = {"name": "c7i-runner", "arch": "amd64", "gpu": False, "max_pods": 200}
        # Override only on the 48xlarge entry.
        inst_override = {"type": "c7i.48xlarge", "weight": 100, "node_disk_size": 3750, "max_pods": 250}
        inst_default = {"type": "c7i.24xlarge", "weight": 85, "node_disk_size": 1900}

        np_override = _build_fleet_nodepool_def(fleet, inst_override)
        np_default = _build_fleet_nodepool_def(fleet, inst_default)

        ec2_override = self._parse(generate_nodepool_yaml(np_override, "nodepools"))[1]
        ec2_default = self._parse(generate_nodepool_yaml(np_default, "nodepools"))[1]

        assert ec2_override["spec"]["kubelet"]["maxPods"] == 250
        assert ec2_default["spec"]["kubelet"]["maxPods"] == 200

    def test_real_def_c7i_runner_uses_explicit_max_pods(self):
        """Round-trip: c7i-runner.yaml renders maxPods=250 on every instance."""
        c7i_runner_defs = [d for d in _load_all_real_defs() if d.get("fleet_name") == "c7i-runner"]
        assert c7i_runner_defs, "expected c7i-runner instances in real defs"
        for d in c7i_runner_defs:
            output = generate_nodepool_yaml(d, "nodepools", REAL_DEFS_DIR, az=DEFAULT_AZ)
            docs = parse_all_yaml(output)
            ec2 = docs[1]
            assert ec2["spec"]["kubelet"]["maxPods"] == 250, (
                f"{d['name']}: expected 250, got {ec2['spec']['kubelet']['maxPods']}"
            )


# ============================================================================
# Bucket field validation
# ============================================================================


class TestBucketField:
    """Tests for the per-def 'bucket' field validation."""

    def _run_main(self, defs_dir: Path, output_dir: Path) -> int:
        env = {
            "NODEPOOLS_DEFS_DIR": str(defs_dir),
            "NODEPOOLS_OUTPUT_DIR": str(output_dir),
            "NODEPOOLS_MODULE_NAME": "test",
            "NODEPOOLS_AZS": DEFAULT_AZS,
        }
        with patch.dict(os.environ, env, clear=False):
            return main()

    def test_validate_bucket_helper_accepts_valid_buckets(self):
        for valid in ("bucket-1", "bucket-2", "bucket-3", "bucket-4"):
            _validate_bucket(valid, where="test")  # must not raise

    @pytest.mark.parametrize(
        "invalid",
        ["bucket-0", "bucket-5", "bucket-x", "bucket1", "1", "", "bucket-10", "bucket-"],
    )
    def test_validate_bucket_helper_rejects_invalid_strings(self, invalid):
        with pytest.raises(ValueError, match="invalid bucket"):
            _validate_bucket(invalid, where="test")

    @pytest.mark.parametrize("invalid", [1, ["bucket-1"], {"name": "bucket-1"}, 1.0])
    def test_validate_bucket_helper_rejects_non_strings(self, invalid):
        with pytest.raises(ValueError, match="invalid bucket"):
            _validate_bucket(invalid, where="test")

    def test_validate_bucket_helper_rejects_none_with_missing_message(self):
        with pytest.raises(ValueError, match="missing required 'bucket' field"):
            _validate_bucket(None, where="fleet 'foo' in foo.yaml")

    def test_fleet_missing_bucket_raises_via_main(self, tmp_path):
        defs_dir = tmp_path / "defs"
        defs_dir.mkdir()
        (defs_dir / "no-bucket.yaml").write_text(
            yaml.dump(
                {
                    "fleet": {
                        "name": "r7a",
                        "arch": "amd64",
                        "gpu": False,
                        "instances": [
                            {"type": "r7a.48xlarge", "weight": 100, "node_disk_size": 4800},
                        ],
                    }
                }
            )
        )
        output_dir = tmp_path / "generated"
        assert self._run_main(defs_dir, output_dir) == 1

    def test_legacy_nodepool_missing_bucket_raises_via_main(self, tmp_path):
        defs_dir = tmp_path / "defs"
        defs_dir.mkdir()
        (defs_dir / "legacy.yaml").write_text(
            yaml.dump(
                {
                    "nodepool": {
                        "name": "legacy-pool",
                        "instance_type": "m6i.32xlarge",
                        "node_disk_size": 200,
                    }
                }
            )
        )
        output_dir = tmp_path / "generated"
        assert self._run_main(defs_dir, output_dir) == 1

    def test_fleets_one_missing_bucket_raises_via_main(self, tmp_path):
        """In a multi-fleet file, ANY fleet missing bucket aborts the run."""
        defs_dir = tmp_path / "defs"
        defs_dir.mkdir()
        (defs_dir / "multi.yaml").write_text(
            yaml.dump(
                {
                    "fleets": [
                        {
                            "name": "fleet-good",
                            "arch": "amd64",
                            "gpu": True,
                            "bucket": "bucket-2",
                            "instances": [
                                {"type": "g5.8xlarge", "weight": 100, "node_disk_size": 600, "has_nvme": True},
                            ],
                        },
                        {
                            "name": "fleet-bad",
                            "arch": "amd64",
                            "gpu": True,
                            # bucket missing here
                            "instances": [
                                {"type": "g5.12xlarge", "weight": 100, "node_disk_size": 600, "has_nvme": True},
                            ],
                        },
                    ]
                }
            )
        )
        output_dir = tmp_path / "generated"
        assert self._run_main(defs_dir, output_dir) == 1

    @pytest.mark.parametrize("bad", ["bucket-0", "bucket-5", "bucket-x", "bucket1"])
    def test_fleet_invalid_bucket_value_raises_via_main(self, tmp_path, bad):
        defs_dir = tmp_path / "defs"
        defs_dir.mkdir()
        (defs_dir / "bad.yaml").write_text(
            yaml.dump(
                {
                    "fleet": {
                        "name": "r7a",
                        "arch": "amd64",
                        "gpu": False,
                        "bucket": bad,
                        "instances": [
                            {"type": "r7a.48xlarge", "weight": 100, "node_disk_size": 4800},
                        ],
                    }
                }
            )
        )
        output_dir = tmp_path / "generated"
        assert self._run_main(defs_dir, output_dir) == 1

    @pytest.mark.parametrize("good", ["bucket-1", "bucket-2", "bucket-3", "bucket-4"])
    def test_fleet_valid_bucket_succeeds(self, tmp_path, good):
        defs_dir = tmp_path / "defs"
        defs_dir.mkdir()
        (defs_dir / "ok.yaml").write_text(
            yaml.dump(
                {
                    "fleet": {
                        "name": "r7a",
                        "arch": "amd64",
                        "gpu": False,
                        "bucket": good,
                        "instances": [
                            {"type": "r7a.48xlarge", "weight": 100, "node_disk_size": 4800},
                        ],
                    }
                }
            )
        )
        output_dir = tmp_path / "generated"
        assert self._run_main(defs_dir, output_dir) == 0


# ============================================================================
# AZ expansion + bucket label rendering
# ============================================================================


class TestAzExpansion:
    """Tests for per-AZ NodePool/EC2NodeClass expansion."""

    @pytest.mark.parametrize("azs_csv", ["us-east-2a,us-east-2b", "us-east-2a,us-east-2b,us-east-2c"])
    def test_emits_one_pair_per_az(self, tmp_path, azs_csv):
        defs_dir = tmp_path / "defs"
        defs_dir.mkdir()
        (defs_dir / "fleet.yaml").write_text(
            yaml.dump(
                {
                    "fleet": {
                        "name": "r7a",
                        "arch": "amd64",
                        "gpu": False,
                        "bucket": "bucket-3",
                        "instances": [
                            {"type": "r7a.48xlarge", "weight": 100, "node_disk_size": 4800},
                        ],
                    }
                }
            )
        )
        output_dir = tmp_path / "generated"
        env = {
            "NODEPOOLS_DEFS_DIR": str(defs_dir),
            "NODEPOOLS_OUTPUT_DIR": str(output_dir),
            "NODEPOOLS_AZS": azs_csv,
        }
        with patch.dict(os.environ, env, clear=False):
            assert main() == 0
        generated = sorted(f.name for f in output_dir.glob("*.yaml"))
        expected_azs = azs_csv.split(",")
        expected_files = sorted(f"r7a-48xlarge-{az}.yaml" for az in expected_azs)
        assert generated == expected_files

    def test_each_variant_has_zone_requirement(self, tmp_path):
        defs_dir = tmp_path / "defs"
        defs_dir.mkdir()
        (defs_dir / "fleet.yaml").write_text(
            yaml.dump(
                {
                    "fleet": {
                        "name": "r7a",
                        "arch": "amd64",
                        "gpu": False,
                        "bucket": "bucket-3",
                        "instances": [
                            {"type": "r7a.48xlarge", "weight": 100, "node_disk_size": 4800},
                        ],
                    }
                }
            )
        )
        output_dir = tmp_path / "generated"
        env = {
            "NODEPOOLS_DEFS_DIR": str(defs_dir),
            "NODEPOOLS_OUTPUT_DIR": str(output_dir),
            "NODEPOOLS_AZS": DEFAULT_AZS,
        }
        with patch.dict(os.environ, env, clear=False):
            assert main() == 0

        for az in DEFAULT_AZS.split(","):
            content = (output_dir / f"r7a-48xlarge-{az}.yaml").read_text()
            np = parse_all_yaml(content)[0]
            reqs = np["spec"]["template"]["spec"]["requirements"]
            zone_reqs = [r for r in reqs if r["key"] == "topology.kubernetes.io/zone"]
            assert len(zone_reqs) == 1
            assert zone_reqs[0]["operator"] == "In"
            assert zone_reqs[0]["values"] == [az]

    def test_no_cross_az_zone_leaks(self, tmp_path):
        """Each variant's zone requirement must list ONLY its own AZ."""
        defs_dir = tmp_path / "defs"
        defs_dir.mkdir()
        (defs_dir / "fleet.yaml").write_text(
            yaml.dump(
                {
                    "fleet": {
                        "name": "r7a",
                        "arch": "amd64",
                        "gpu": False,
                        "bucket": "bucket-3",
                        "instances": [
                            {"type": "r7a.48xlarge", "weight": 100, "node_disk_size": 4800},
                        ],
                    }
                }
            )
        )
        output_dir = tmp_path / "generated"
        env = {
            "NODEPOOLS_DEFS_DIR": str(defs_dir),
            "NODEPOOLS_OUTPUT_DIR": str(output_dir),
            "NODEPOOLS_AZS": DEFAULT_AZS,
        }
        with patch.dict(os.environ, env, clear=False):
            assert main() == 0

        all_azs = DEFAULT_AZS.split(",")
        for az in all_azs:
            content = (output_dir / f"r7a-48xlarge-{az}.yaml").read_text()
            np = parse_all_yaml(content)[0]
            zone_reqs = [
                r for r in np["spec"]["template"]["spec"]["requirements"] if r["key"] == "topology.kubernetes.io/zone"
            ]
            assert zone_reqs[0]["values"] == [az]
            for other_az in all_azs:
                if other_az != az:
                    assert other_az not in zone_reqs[0]["values"]

    def test_each_variant_name_ends_with_az(self, tmp_path):
        defs_dir = tmp_path / "defs"
        defs_dir.mkdir()
        (defs_dir / "fleet.yaml").write_text(
            yaml.dump(
                {
                    "fleet": {
                        "name": "r7a",
                        "arch": "amd64",
                        "gpu": False,
                        "bucket": "bucket-3",
                        "instances": [
                            {"type": "r7a.48xlarge", "weight": 100, "node_disk_size": 4800},
                        ],
                    }
                }
            )
        )
        output_dir = tmp_path / "generated"
        env = {
            "NODEPOOLS_DEFS_DIR": str(defs_dir),
            "NODEPOOLS_OUTPUT_DIR": str(output_dir),
            "NODEPOOLS_AZS": DEFAULT_AZS,
        }
        with patch.dict(os.environ, env, clear=False):
            assert main() == 0

        for az in DEFAULT_AZS.split(","):
            content = (output_dir / f"r7a-48xlarge-{az}.yaml").read_text()
            np, ec2 = parse_all_yaml(content)
            assert np["metadata"]["name"].endswith(f"-{az}")
            assert ec2["metadata"]["name"].endswith(f"-{az}")
            assert np["metadata"]["name"] == ec2["metadata"]["name"]
            # nodeClassRef must point at the same AZ-suffixed EC2NodeClass
            assert np["spec"]["template"]["spec"]["nodeClassRef"]["name"] == ec2["metadata"]["name"]

    def test_bucket_label_uses_constant_key(self, tmp_path):
        """The label KEY must be ENI_CONFIG_LABEL imported from cni_constants."""
        defs_dir = tmp_path / "defs"
        defs_dir.mkdir()
        (defs_dir / "fleet.yaml").write_text(
            yaml.dump(
                {
                    "fleet": {
                        "name": "r7a",
                        "arch": "amd64",
                        "gpu": False,
                        "bucket": "bucket-3",
                        "instances": [
                            {"type": "r7a.48xlarge", "weight": 100, "node_disk_size": 4800},
                        ],
                    }
                }
            )
        )
        output_dir = tmp_path / "generated"
        env = {
            "NODEPOOLS_DEFS_DIR": str(defs_dir),
            "NODEPOOLS_OUTPUT_DIR": str(output_dir),
            "NODEPOOLS_AZS": DEFAULT_AZS,
        }
        with patch.dict(os.environ, env, clear=False):
            assert main() == 0

        for az in DEFAULT_AZS.split(","):
            content = (output_dir / f"r7a-48xlarge-{az}.yaml").read_text()
            np = parse_all_yaml(content)[0]
            labels = np["spec"]["template"]["metadata"]["labels"]
            assert ENI_CONFIG_LABEL in labels

    def test_bucket_label_value_from_helper(self, tmp_path):
        """The label VALUE must be rendered from bucket_eniconfig_name()."""
        defs_dir = tmp_path / "defs"
        defs_dir.mkdir()
        (defs_dir / "fleet.yaml").write_text(
            yaml.dump(
                {
                    "fleet": {
                        "name": "r7a",
                        "arch": "amd64",
                        "gpu": False,
                        "bucket": "bucket-3",
                        "instances": [
                            {"type": "r7a.48xlarge", "weight": 100, "node_disk_size": 4800},
                        ],
                    }
                }
            )
        )
        output_dir = tmp_path / "generated"
        env = {
            "NODEPOOLS_DEFS_DIR": str(defs_dir),
            "NODEPOOLS_OUTPUT_DIR": str(output_dir),
            "NODEPOOLS_AZS": DEFAULT_AZS,
        }
        with patch.dict(os.environ, env, clear=False):
            assert main() == 0

        for az in DEFAULT_AZS.split(","):
            content = (output_dir / f"r7a-48xlarge-{az}.yaml").read_text()
            np = parse_all_yaml(content)[0]
            labels = np["spec"]["template"]["metadata"]["labels"]
            expected_value = bucket_eniconfig_name("bucket-3", az)
            assert labels[ENI_CONFIG_LABEL] == expected_value

    def test_bucket_label_for_each_bucket_number(self, tmp_path):
        """All four bucket numbers render with the matching {n} value."""
        defs_dir = tmp_path / "defs"
        defs_dir.mkdir()
        for i in (1, 2, 3, 4):
            (defs_dir / f"f{i}.yaml").write_text(
                yaml.dump(
                    {
                        "fleet": {
                            "name": f"f{i}",
                            "arch": "amd64",
                            "gpu": False,
                            "bucket": f"bucket-{i}",
                            "instances": [
                                {"type": "m6i.32xlarge", "weight": 100, "node_disk_size": 200},
                            ],
                        }
                    }
                )
            )
        output_dir = tmp_path / "generated"
        env = {
            "NODEPOOLS_DEFS_DIR": str(defs_dir),
            "NODEPOOLS_OUTPUT_DIR": str(output_dir),
            "NODEPOOLS_AZS": DEFAULT_AZ,
        }
        with patch.dict(os.environ, env, clear=False):
            assert main() == 0
        for i in (1, 2, 3, 4):
            content = (output_dir / f"f{i}-32xlarge-{DEFAULT_AZ}.yaml").read_text()
            np = parse_all_yaml(content)[0]
            labels = np["spec"]["template"]["metadata"]["labels"]
            assert labels[ENI_CONFIG_LABEL] == bucket_eniconfig_name(f"bucket-{i}", DEFAULT_AZ)

    def test_no_az_no_bucket_label_or_zone_requirement(self):
        """Backward-compat path: az=None means no bucket label, no zone requirement."""
        nodepool_def = _make_nodepool_def()
        output = generate_nodepool_yaml(nodepool_def, "nodepools")
        np = parse_all_yaml(output)[0]
        labels = np["spec"]["template"]["metadata"]["labels"]
        assert ENI_CONFIG_LABEL not in labels
        reqs = np["spec"]["template"]["spec"]["requirements"]
        assert not any(r["key"] == "topology.kubernetes.io/zone" for r in reqs)

    def test_az_pinned_name_in_node_tag(self):
        """The Name tag on the EC2NodeClass uses the AZ-suffixed name."""
        nodepool_def = _make_nodepool_def(name="t-pool")
        output = generate_nodepool_yaml(nodepool_def, "nodepools", az="us-east-2b")
        ec2 = parse_all_yaml(output)[1]
        assert ec2["spec"]["tags"]["Name"] == "CLUSTER_NAME_PLACEHOLDER-t-pool-us-east-2b"
        assert ec2["spec"]["tags"]["NodePool"] == "t-pool-us-east-2b"


# ============================================================================
# NODEPOOLS_AZS env var parsing
# ============================================================================


class TestAzsEnvVar:
    """Tests for the NODEPOOLS_AZS env var parsing + main() integration."""

    def _make_minimal_defs(self, tmp_path):
        defs_dir = tmp_path / "defs"
        defs_dir.mkdir()
        (defs_dir / "ok.yaml").write_text(
            yaml.dump(
                {
                    "fleet": {
                        "name": "m6i",
                        "arch": "amd64",
                        "gpu": False,
                        "bucket": "bucket-3",
                        "instances": [
                            {"type": "m6i.32xlarge", "weight": 100, "node_disk_size": 200},
                        ],
                    }
                }
            )
        )
        return defs_dir

    def test_parse_azs_accepts_simple_list(self):
        assert _parse_azs("us-east-2a,us-east-2b,us-east-2c") == [
            "us-east-2a",
            "us-east-2b",
            "us-east-2c",
        ]

    def test_parse_azs_strips_whitespace(self):
        assert _parse_azs(" us-east-2a , us-east-2b ") == ["us-east-2a", "us-east-2b"]

    def test_parse_azs_filters_empty_entries(self):
        assert _parse_azs("us-east-2a,,us-east-2b,") == ["us-east-2a", "us-east-2b"]

    def test_parse_azs_none_raises(self):
        with pytest.raises(ValueError, match="NODEPOOLS_AZS is required"):
            _parse_azs(None)

    def test_parse_azs_empty_raises(self):
        with pytest.raises(ValueError, match="NODEPOOLS_AZS is required"):
            _parse_azs("")

    def test_parse_azs_only_commas_raises(self):
        with pytest.raises(ValueError, match="parsed to empty list"):
            _parse_azs(",,,")

    @pytest.mark.parametrize(
        "bad",
        ["us-east-2", "useast2a", "us-east-2-a", "USEAST2A", "us_east_2a", "us--east-2a"],
    )
    def test_parse_azs_invalid_format_raises(self, bad):
        with pytest.raises(ValueError, match="invalid AZ"):
            _parse_azs(bad)

    def test_parse_azs_duplicate_raises(self):
        """Duplicate AZ entries silently overwrite generated files; reject them."""
        with pytest.raises(ValueError, match=r"duplicate AZ.*us-east-2a"):
            _parse_azs("us-east-2a,us-east-2a")

    def test_parse_azs_duplicate_among_unique_raises(self):
        """One duplicate among otherwise unique entries is still rejected."""
        with pytest.raises(ValueError, match=r"duplicate AZ.*us-east-2b"):
            _parse_azs("us-east-2a,us-east-2b,us-east-2c,us-east-2b")

    def test_parse_azs_multiple_duplicates_listed(self):
        """All duplicated AZs appear in the error message."""
        with pytest.raises(ValueError, match=r"duplicate AZ") as exc_info:
            _parse_azs("us-east-2a,us-east-2b,us-east-2a,us-east-2b")
        msg = str(exc_info.value)
        assert "us-east-2a" in msg
        assert "us-east-2b" in msg

    def test_main_missing_azs_returns_error(self, tmp_path):
        defs_dir = self._make_minimal_defs(tmp_path)
        output_dir = tmp_path / "generated"
        env = {
            "NODEPOOLS_DEFS_DIR": str(defs_dir),
            "NODEPOOLS_OUTPUT_DIR": str(output_dir),
        }
        # Explicitly remove NODEPOOLS_AZS in case parent shell has it set
        with patch.dict(os.environ, env, clear=False):
            os.environ.pop("NODEPOOLS_AZS", None)
            assert main() == 1

    def test_main_empty_azs_returns_error(self, tmp_path):
        defs_dir = self._make_minimal_defs(tmp_path)
        output_dir = tmp_path / "generated"
        env = {
            "NODEPOOLS_DEFS_DIR": str(defs_dir),
            "NODEPOOLS_OUTPUT_DIR": str(output_dir),
            "NODEPOOLS_AZS": "",
        }
        with patch.dict(os.environ, env, clear=False):
            assert main() == 1

    def test_main_invalid_azs_returns_error(self, tmp_path):
        defs_dir = self._make_minimal_defs(tmp_path)
        output_dir = tmp_path / "generated"
        env = {
            "NODEPOOLS_DEFS_DIR": str(defs_dir),
            "NODEPOOLS_OUTPUT_DIR": str(output_dir),
            "NODEPOOLS_AZS": "us-east-2",  # missing AZ letter
        }
        with patch.dict(os.environ, env, clear=False):
            assert main() == 1

    def test_main_whitespace_azs_handled(self, tmp_path):
        """Defensive whitespace stripping in env var works end-to-end."""
        defs_dir = self._make_minimal_defs(tmp_path)
        output_dir = tmp_path / "generated"
        env = {
            "NODEPOOLS_DEFS_DIR": str(defs_dir),
            "NODEPOOLS_OUTPUT_DIR": str(output_dir),
            "NODEPOOLS_AZS": " us-east-2a , us-east-2b ",
        }
        with patch.dict(os.environ, env, clear=False):
            assert main() == 0
        generated = sorted(f.name for f in output_dir.glob("*.yaml"))
        assert generated == ["m6i-32xlarge-us-east-2a.yaml", "m6i-32xlarge-us-east-2b.yaml"]


# ============================================================================
# Name length sanity
# ============================================================================


class TestNameLengthGuard:
    """Tests for the 63-char Kubernetes object name limit guard."""

    def test_short_name_succeeds(self):
        nodepool_def = _make_nodepool_def(name="short")
        output = generate_nodepool_yaml(nodepool_def, "nodepools", az="us-east-2a")
        np = parse_all_yaml(output)[0]
        assert np["metadata"]["name"] == "short-us-east-2a"
        assert len(np["metadata"]["name"]) <= 63

    def test_real_def_max_name_length_under_limit(self):
        """All real-def name + AZ suffix combos must fit in 63 chars.

        Covers all 3 nodepools modules — nodepools, nodepools-h100, nodepools-b200 —
        so a future long-named GPU def is caught by CI before deploy.
        """
        for defs_dir in ALL_REAL_DEFS_DIRS:
            assert defs_dir.is_dir(), f"expected real defs dir at {defs_dir}"
            for d in _load_all_real_defs(defs_dir):
                for az in DEFAULT_AZS.split(","):
                    name = f"{d['name']}-{az}"
                    assert len(name) <= 63, f"{name!r} (from {defs_dir.parent.name}/defs/) length {len(name)} > 63"

    def test_long_name_raises(self):
        """A def whose name + AZ suffix exceeds 63 chars MUST raise."""
        # Pick a base name long enough to push the AZ-suffixed total over 63.
        base = "x" * 60
        nodepool_def = _make_nodepool_def(name=base)
        with pytest.raises(ValueError, match="exceeds Kubernetes DNS-1123 limit"):
            generate_nodepool_yaml(nodepool_def, "nodepools", az="us-east-2a")


# ============================================================================
# Bucket-coherence cross-check vs cluster's pod_cidr_buckets
# ============================================================================


class TestValidBucketsCheck:
    """Tests for NODEPOOLS_VALID_BUCKETS coherence checking against per-def buckets.

    Catches the case where a cluster trims its bucket set (e.g. omits bucket-4
    on a cost-conscious cluster) but a def still references the missing bucket.
    The generator must fail fast at generation time instead of producing a
    NodePool whose ENI-config label points at a non-existent ENIConfig CR.
    """

    def _write_fleet(self, defs_dir: Path, name: str, fleet: dict) -> None:
        (defs_dir / f"{name}.yaml").write_text(yaml.dump({"fleet": fleet}))

    def _run_main(self, defs_dir, output_dir, valid_buckets_csv=None):
        env = {
            "NODEPOOLS_DEFS_DIR": str(defs_dir),
            "NODEPOOLS_OUTPUT_DIR": str(output_dir),
            "NODEPOOLS_AZS": DEFAULT_AZS,
        }
        if valid_buckets_csv is not None:
            env["NODEPOOLS_VALID_BUCKETS"] = valid_buckets_csv
        with patch.dict(os.environ, env, clear=False):
            # Make sure prior test runs haven't leaked the var
            if valid_buckets_csv is None:
                os.environ.pop("NODEPOOLS_VALID_BUCKETS", None)
            return main()

    # ----- _parse_valid_buckets unit tests -----

    def test_parse_valid_buckets_none_returns_none(self):
        assert _parse_valid_buckets(None) is None

    def test_parse_valid_buckets_empty_returns_none(self):
        assert _parse_valid_buckets("") is None

    def test_parse_valid_buckets_only_commas_returns_none(self):
        """Defensive: ',,,' parses to no real entries → None (legacy fallback)."""
        assert _parse_valid_buckets(",,,") is None

    def test_parse_valid_buckets_simple_list(self):
        assert _parse_valid_buckets("bucket-1,bucket-2,bucket-3,bucket-4") == [
            "bucket-1",
            "bucket-2",
            "bucket-3",
            "bucket-4",
        ]

    def test_parse_valid_buckets_strips_whitespace(self):
        assert _parse_valid_buckets(" bucket-1 , bucket-2 ") == ["bucket-1", "bucket-2"]

    def test_parse_valid_buckets_filters_empty_entries(self):
        assert _parse_valid_buckets("bucket-1,,bucket-2,") == ["bucket-1", "bucket-2"]

    @pytest.mark.parametrize("bad", ["bucket-x", "bucket-0", "bucket-5", "bucket1", "BUCKET-1"])
    def test_parse_valid_buckets_invalid_format_raises(self, bad):
        with pytest.raises(ValueError, match="invalid bucket"):
            _parse_valid_buckets(bad)

    def test_parse_valid_buckets_one_bad_among_good_raises(self):
        """A single malformed entry in an otherwise-valid list rejects the whole list."""
        with pytest.raises(ValueError, match="invalid bucket"):
            _parse_valid_buckets("bucket-1,bucket-x,bucket-3")

    # ----- _validate_bucket integration tests -----

    def test_validate_bucket_with_valid_set_accepts_member(self):
        _validate_bucket("bucket-1", where="t", valid_buckets={"bucket-1", "bucket-2"})

    def test_validate_bucket_with_valid_set_rejects_non_member(self):
        with pytest.raises(ValueError, match="not defined in this cluster's pod_cidr_buckets"):
            _validate_bucket("bucket-4", where="fleet 'foo' in foo.yaml", valid_buckets={"bucket-1", "bucket-2"})

    def test_validate_bucket_with_none_skips_coherence_check(self):
        """valid_buckets=None falls back to format-only (legacy/test-friendly behavior)."""
        # bucket-4 is well-formed and there's no cluster set to compare against → accepted
        _validate_bucket("bucket-4", where="t", valid_buckets=None)

    def test_validate_bucket_error_lists_valid_set_and_def_name(self):
        """Error message must name the def AND list the valid bucket set."""
        with pytest.raises(ValueError, match="not defined in this cluster's pod_cidr_buckets") as exc:
            _validate_bucket(
                "bucket-4", where="fleet 'c7i-runner' in c7i-runner.yaml", valid_buckets=["bucket-1", "bucket-2"]
            )
        msg = str(exc.value)
        assert "c7i-runner.yaml" in msg
        assert "bucket-4" in msg
        assert "bucket-1" in msg
        assert "bucket-2" in msg

    # ----- main() integration tests -----

    def test_def_referencing_bucket_outside_cluster_set_aborts(self, tmp_path):
        """Fleet referencing bucket-4 with NODEPOOLS_VALID_BUCKETS=bucket-1,2,3 → main() returns 1."""
        defs_dir = tmp_path / "defs"
        defs_dir.mkdir()
        self._write_fleet(
            defs_dir,
            "uses-bucket-4",
            {
                "name": "uses-bucket-4",
                "arch": "amd64",
                "gpu": False,
                "bucket": "bucket-4",
                "instances": [
                    {"type": "m6i.32xlarge", "weight": 100, "node_disk_size": 200},
                ],
            },
        )
        output_dir = tmp_path / "generated"
        assert self._run_main(defs_dir, output_dir, valid_buckets_csv="bucket-1,bucket-2,bucket-3") == 1

    def test_def_with_bucket_in_cluster_set_succeeds(self, tmp_path):
        """Fleet referencing bucket-3 with NODEPOOLS_VALID_BUCKETS=bucket-1,2,3 → main() returns 0."""
        defs_dir = tmp_path / "defs"
        defs_dir.mkdir()
        self._write_fleet(
            defs_dir,
            "uses-bucket-3",
            {
                "name": "uses-bucket-3",
                "arch": "amd64",
                "gpu": False,
                "bucket": "bucket-3",
                "instances": [
                    {"type": "m6i.32xlarge", "weight": 100, "node_disk_size": 200},
                ],
            },
        )
        output_dir = tmp_path / "generated"
        assert self._run_main(defs_dir, output_dir, valid_buckets_csv="bucket-1,bucket-2,bucket-3") == 0

    def test_unset_env_var_falls_back_to_format_only(self, tmp_path):
        """No NODEPOOLS_VALID_BUCKETS → format-only validation (legacy/test-friendly)."""
        defs_dir = tmp_path / "defs"
        defs_dir.mkdir()
        # Use bucket-4 (a valid format) — without the env var the coherence
        # check is skipped, so this MUST succeed (preserves backward-compat).
        self._write_fleet(
            defs_dir,
            "uses-bucket-4",
            {
                "name": "uses-bucket-4",
                "arch": "amd64",
                "gpu": False,
                "bucket": "bucket-4",
                "instances": [
                    {"type": "m6i.32xlarge", "weight": 100, "node_disk_size": 200},
                ],
            },
        )
        output_dir = tmp_path / "generated"
        assert self._run_main(defs_dir, output_dir, valid_buckets_csv=None) == 0

    def test_malformed_env_var_aborts(self, tmp_path):
        """A malformed entry in NODEPOOLS_VALID_BUCKETS aborts during parse."""
        defs_dir = tmp_path / "defs"
        defs_dir.mkdir()
        self._write_fleet(
            defs_dir,
            "fleet",
            {
                "name": "fleet",
                "arch": "amd64",
                "gpu": False,
                "bucket": "bucket-1",
                "instances": [
                    {"type": "m6i.32xlarge", "weight": 100, "node_disk_size": 200},
                ],
            },
        )
        output_dir = tmp_path / "generated"
        # bucket-x is malformed → _parse_valid_buckets raises → main() returns 1
        assert self._run_main(defs_dir, output_dir, valid_buckets_csv="bucket-x,bucket-1") == 1

    def test_defensive_whitespace_handled(self, tmp_path):
        """Whitespace around env-var entries is stripped end-to-end."""
        defs_dir = tmp_path / "defs"
        defs_dir.mkdir()
        self._write_fleet(
            defs_dir,
            "fleet",
            {
                "name": "fleet",
                "arch": "amd64",
                "gpu": False,
                "bucket": "bucket-2",
                "instances": [
                    {"type": "m6i.32xlarge", "weight": 100, "node_disk_size": 200},
                ],
            },
        )
        output_dir = tmp_path / "generated"
        assert self._run_main(defs_dir, output_dir, valid_buckets_csv=" bucket-1 , bucket-2 , bucket-3 ") == 0

    def test_legacy_nodepool_referencing_missing_bucket_aborts(self, tmp_path):
        """Legacy ``nodepool:`` defs are also subject to the coherence check."""
        defs_dir = tmp_path / "defs"
        defs_dir.mkdir()
        (defs_dir / "legacy.yaml").write_text(
            yaml.dump(
                {
                    "nodepool": {
                        "name": "legacy-pool",
                        "instance_type": "m6i.32xlarge",
                        "node_disk_size": 200,
                        "bucket": "bucket-4",
                    }
                }
            )
        )
        output_dir = tmp_path / "generated"
        assert self._run_main(defs_dir, output_dir, valid_buckets_csv="bucket-1,bucket-2,bucket-3") == 1

    def test_fleets_one_referencing_missing_bucket_aborts(self, tmp_path):
        """In a multi-fleet file, ANY fleet whose bucket isn't in the cluster set aborts the run."""
        defs_dir = tmp_path / "defs"
        defs_dir.mkdir()
        (defs_dir / "multi.yaml").write_text(
            yaml.dump(
                {
                    "fleets": [
                        {
                            "name": "fleet-good",
                            "arch": "amd64",
                            "gpu": False,
                            "bucket": "bucket-1",
                            "instances": [
                                {"type": "m6i.32xlarge", "weight": 100, "node_disk_size": 200},
                            ],
                        },
                        {
                            "name": "fleet-bad",
                            "arch": "amd64",
                            "gpu": False,
                            "bucket": "bucket-4",  # not in cluster set below
                            "instances": [
                                {"type": "m6i.32xlarge", "weight": 100, "node_disk_size": 200},
                            ],
                        },
                    ]
                }
            )
        )
        output_dir = tmp_path / "generated"
        assert self._run_main(defs_dir, output_dir, valid_buckets_csv="bucket-1,bucket-2,bucket-3") == 1
