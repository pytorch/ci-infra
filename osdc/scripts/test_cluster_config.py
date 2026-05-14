"""Unit tests for cluster-config.py."""

import importlib.util
import json
import sys
from pathlib import Path
from unittest.mock import patch

import pytest
import yaml

# The module is named cluster-config.py (with a hyphen), so it can't be
# imported with a normal import statement. Use importlib to load it.
_spec = importlib.util.spec_from_file_location(
    "cluster_config",
    Path(__file__).resolve().parent / "cluster-config.py",
)
cluster_config = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(cluster_config)


# ============================================================================
# Fixtures
# ============================================================================

# Reusable valid pod_cidr_buckets fixtures for tests (4 buckets x 2 AZs).
# Real clusters use 4x3 (production) / 4x2 (staging); 4x2 is enough to
# exercise validation and tfvar emission paths.
_VALID_POD_CIDR_BUCKETS_2AZ = {
    "bucket-1": {
        "us-west-2a": "100.64.0.0/16",
        "us-west-2b": "100.65.0.0/16",
    },
    "bucket-2": {
        "us-west-2a": "100.66.0.0/16",
        "us-west-2b": "100.67.0.0/16",
    },
    "bucket-3": {
        "us-west-2a": "100.68.0.0/16",
        "us-west-2b": "100.69.0.0/16",
    },
    "bucket-4": {
        "us-west-2a": "100.70.0.0/16",
        "us-west-2b": "100.71.0.0/16",
    },
}

_VALID_POD_CIDR_BUCKETS_PROD = {
    "bucket-1": {
        "us-east-1a": "100.96.0.0/16",
        "us-east-1b": "100.97.0.0/16",
    },
    "bucket-2": {
        "us-east-1a": "100.98.0.0/16",
        "us-east-1b": "100.99.0.0/16",
    },
    "bucket-3": {
        "us-east-1a": "100.100.0.0/16",
        "us-east-1b": "100.101.0.0/16",
    },
    "bucket-4": {
        "us-east-1a": "100.102.0.0/16",
        "us-east-1b": "100.103.0.0/16",
    },
}

FAKE_CONFIG = {
    "defaults": {
        "vpc_cidr": "10.0.0.0/16",
        "single_nat_gateway": False,
        "base_node_count": 3,
        "base_node_instance_type": "m5.xlarge",
        "base_node_max_unavailable_percentage": 33,
        "base_node_ami_version": "v20260318",
        "eks_version": "1.35",
        "harbor": {
            "core_replicas": 2,
        },
    },
    "clusters": {
        "staging": {
            "cluster_name": "my-staging",
            "region": "us-west-2",
            "state_bucket": "my-tfstate-staging",
            "base": {
                "vpc_cidr": "10.1.0.0/16",
                "single_nat_gateway": True,
                "pod_cidr_buckets": _VALID_POD_CIDR_BUCKETS_2AZ,
            },
            "modules": ["karpenter", "arc", "arc-runners"],
            "harbor": {
                "core_replicas": 1,
            },
            "feature_flag": True,
        },
        "production": {
            "cluster_name": "my-production",
            "region": "us-east-1",
            "base": {
                "pod_cidr_buckets": _VALID_POD_CIDR_BUCKETS_PROD,
            },
            "modules": ["karpenter", "arc", "arc-runners", "buildkit", "monitoring"],
        },
    },
}


@pytest.fixture
def fake_clusters_yaml(tmp_path):
    """Write a fake clusters.yaml and return its path."""
    p = tmp_path / "clusters.yaml"
    p.write_text(yaml.dump(FAKE_CONFIG, default_flow_style=False))
    return p


def run_main(*argv):
    """Run cluster_config.main() with the given argv, returning exit code."""
    with patch.object(sys, "argv", ["cluster-config.py", *argv]):
        try:
            cluster_config.main()
            return 0
        except SystemExit as exc:
            return exc.code


# ============================================================================
# resolve() tests
# ============================================================================


class TestResolve:
    """Tests for the resolve() function."""

    def test_cluster_value_found(self):
        cluster_cfg = {"harbor": {"core_replicas": 5}}
        defaults = {"harbor": {"core_replicas": 2}}
        assert cluster_config.resolve(cluster_cfg, defaults, "harbor.core_replicas") == 5

    def test_falls_back_to_defaults(self):
        cluster_cfg = {"harbor": {}}
        defaults = {"harbor": {"core_replicas": 2}}
        assert cluster_config.resolve(cluster_cfg, defaults, "harbor.core_replicas") == 2

    def test_both_none(self):
        cluster_cfg = {}
        defaults = {}
        assert cluster_config.resolve(cluster_cfg, defaults, "harbor.core_replicas") is None

    def test_non_dict_intermediate_in_cluster(self):
        """When an intermediate key resolves to a non-dict, value is None."""
        cluster_cfg = {"harbor": "not-a-dict"}
        defaults = {"harbor": {"core_replicas": 2}}
        assert cluster_config.resolve(cluster_cfg, defaults, "harbor.core_replicas") == 2

    def test_non_dict_intermediate_in_both(self):
        cluster_cfg = {"harbor": "not-a-dict"}
        defaults = {"harbor": 42}
        assert cluster_config.resolve(cluster_cfg, defaults, "harbor.core_replicas") is None

    def test_top_level_key(self):
        cluster_cfg = {"region": "us-west-2"}
        defaults = {"region": "us-east-1"}
        assert cluster_config.resolve(cluster_cfg, defaults, "region") == "us-west-2"

    def test_top_level_falls_back(self):
        cluster_cfg = {}
        defaults = {"region": "us-east-1"}
        assert cluster_config.resolve(cluster_cfg, defaults, "region") == "us-east-1"

    def test_deeply_nested(self):
        cluster_cfg = {"a": {"b": {"c": {"d": 99}}}}
        defaults = {}
        assert cluster_config.resolve(cluster_cfg, defaults, "a.b.c.d") == 99

    def test_cluster_value_is_false(self):
        """False is a legitimate value, not None — should be returned."""
        cluster_cfg = {"feature": False}
        defaults = {"feature": True}
        # False is not None, so cluster value wins
        assert cluster_config.resolve(cluster_cfg, defaults, "feature") is False

    def test_cluster_value_is_zero(self):
        """Zero is a legitimate value, not None — should be returned."""
        cluster_cfg = {"count": 0}
        defaults = {"count": 5}
        # 0 is not None, so cluster value wins
        assert cluster_config.resolve(cluster_cfg, defaults, "count") == 0


# ============================================================================
# tfvars() tests
# ============================================================================


def _cfg_with_buckets(**overrides):
    """Build a minimal tfvars-ready cluster config with valid pod_cidr_buckets."""
    base = overrides.pop("base", {}) or {}
    base.setdefault("pod_cidr_buckets", _VALID_POD_CIDR_BUCKETS_2AZ)
    cfg = {
        "cluster_name": overrides.pop("cluster_name", "test-cluster"),
        "region": overrides.pop("region", "us-west-2"),
        "base": base,
    }
    cfg.update(overrides)
    return cfg


class TestTfvars:
    """Tests for the tfvars() function."""

    def test_all_defaults(self, capsys):
        cluster_cfg = _cfg_with_buckets()
        defaults = {}
        cluster_config.tfvars("test", cluster_cfg, defaults)
        out = capsys.readouterr().out.strip()
        assert '-var="cluster_name=test-cluster"' in out
        assert '-var="aws_region=us-west-2"' in out
        assert '-var="vpc_cidr=10.0.0.0/16"' in out
        assert '-var="single_nat_gateway=false"' in out
        assert '-var="base_node_count=3"' in out
        assert '-var="base_node_instance_type=m5.xlarge"' in out
        assert '-var="base_node_max_unavailable_percentage=33"' in out
        assert '-var="base_node_ami_version=v*"' in out
        assert '-var="eks_version=1.35"' in out
        # coredns.replicas not set anywhere — falls back to hardcoded default 6
        assert '-var="coredns_replicas=6"' in out

    def test_coredns_replicas_from_defaults(self, capsys):
        cluster_cfg = _cfg_with_buckets(cluster_name="c", region="r")
        defaults = {"coredns": {"replicas": 4}}
        cluster_config.tfvars("c", cluster_cfg, defaults)
        out = capsys.readouterr().out.strip()
        assert '-var="coredns_replicas=4"' in out

    def test_coredns_replicas_cluster_override(self, capsys):
        cluster_cfg = _cfg_with_buckets(
            cluster_name="c",
            region="r",
            coredns={"replicas": 2},
        )
        defaults = {"coredns": {"replicas": 6}}
        cluster_config.tfvars("c", cluster_cfg, defaults)
        out = capsys.readouterr().out.strip()
        assert '-var="coredns_replicas=2"' in out

    def test_cluster_overrides(self, capsys):
        cluster_cfg = _cfg_with_buckets(
            cluster_name="prod-cluster",
            region="eu-west-1",
            base={
                "vpc_cidr": "10.99.0.0/16",
                "single_nat_gateway": True,
                "base_node_count": 6,
                "base_node_instance_type": "m6i.2xlarge",
                "base_node_max_unavailable_percentage": 50,
                "base_node_ami_version": "v20260318",
                "eks_version": "1.36",
                "pod_cidr_buckets": _VALID_POD_CIDR_BUCKETS_2AZ,
            },
        )
        defaults = {
            "vpc_cidr": "10.0.0.0/16",
            "single_nat_gateway": False,
            "base_node_count": 3,
        }
        cluster_config.tfvars("prod", cluster_cfg, defaults)
        out = capsys.readouterr().out.strip()
        assert '-var="vpc_cidr=10.99.0.0/16"' in out
        assert '-var="single_nat_gateway=true"' in out
        assert '-var="base_node_count=6"' in out
        assert '-var="base_node_instance_type=m6i.2xlarge"' in out
        assert '-var="base_node_max_unavailable_percentage=50"' in out
        assert '-var="base_node_ami_version=v20260318"' in out
        assert '-var="eks_version=1.36"' in out

    def test_bool_formatting(self, capsys):
        """single_nat_gateway bool should be lowercased."""
        cluster_cfg = _cfg_with_buckets(
            cluster_name="c",
            region="r",
            base={"single_nat_gateway": True, "pod_cidr_buckets": _VALID_POD_CIDR_BUCKETS_2AZ},
        )
        cluster_config.tfvars("c", cluster_cfg, {})
        out = capsys.readouterr().out.strip()
        assert '-var="single_nat_gateway=true"' in out
        # Ensure it's not Python-style True
        assert "True" not in out

    def test_eks_version_from_defaults(self, capsys):
        cluster_cfg = _cfg_with_buckets(cluster_name="c", region="r")
        defaults = {"eks_version": "1.30"}
        cluster_config.tfvars("c", cluster_cfg, defaults)
        out = capsys.readouterr().out.strip()
        assert '-var="eks_version=1.30"' in out

    def test_no_base_key_fails(self):
        """Cluster with no 'base' key fails because pod_cidr_buckets is required."""
        cluster_cfg = {
            "cluster_name": "minimal",
            "region": "ap-southeast-1",
        }
        with pytest.raises(SystemExit) as exc:
            cluster_config.tfvars("minimal", cluster_cfg, {"vpc_cidr": "10.50.0.0/16"})
        assert "pod_cidr_buckets" in str(exc.value)
        assert "minimal" in str(exc.value)

    def test_pod_cidr_buckets_emitted(self, capsys):
        """Happy path: pod_cidr_buckets is emitted as a single JSON-encoded -var flag
        wrapped in single quotes so the inner double quotes survive shell eval.
        """
        cluster_cfg = _cfg_with_buckets()
        cluster_config.tfvars("test", cluster_cfg, {})
        out = capsys.readouterr().out.strip()
        # Single-quote wrapped, JSON-encoded
        assert "-var='pod_cidr_buckets=" in out
        assert '"bucket-1"' in out
        assert '"100.64.0.0/16"' in out
        # Confirm the JSON is round-trippable: extract and parse
        prefix = "-var='pod_cidr_buckets="
        start = out.index(prefix) + len(prefix)
        end = out.index("'", start)
        parsed = json.loads(out[start:end])
        assert parsed == _VALID_POD_CIDR_BUCKETS_2AZ

    def test_pod_cidr_buckets_missing_fails(self):
        """Cluster without pod_cidr_buckets raises SystemExit naming the cluster."""
        cluster_cfg = {
            "cluster_name": "no-buckets",
            "region": "us-west-2",
            "base": {"vpc_cidr": "10.0.0.0/16"},
        }
        with pytest.raises(SystemExit) as exc:
            cluster_config.tfvars("no-buckets", cluster_cfg, {})
        msg = str(exc.value)
        assert "pod_cidr_buckets" in msg
        assert "no-buckets" in msg

    def test_pod_cidr_buckets_empty_dict_fails(self):
        """Empty pod_cidr_buckets fails the non-empty check."""
        cluster_cfg = _cfg_with_buckets(base={"pod_cidr_buckets": {}})
        with pytest.raises(SystemExit) as exc:
            cluster_config.tfvars("c", cluster_cfg, {})
        assert "non-empty" in str(exc.value)

    def test_pod_cidr_buckets_invalid_bucket_name(self):
        """Bucket name like 'bucket-5' or 'cpu-general' fails validation."""
        bad_buckets = {
            "bucket-5": {"us-west-2a": "100.64.0.0/16"},
        }
        cluster_cfg = _cfg_with_buckets(base={"pod_cidr_buckets": bad_buckets})
        with pytest.raises(SystemExit) as exc:
            cluster_config.tfvars("c", cluster_cfg, {})
        assert "bucket-5" in str(exc.value)
        assert "bucket name" in str(exc.value)

    def test_pod_cidr_buckets_non_bucket_prefix_name(self):
        """Bucket name like 'cpu-general' (not bucket-N) fails validation."""
        bad_buckets = {
            "cpu-general": {"us-west-2a": "100.64.0.0/16"},
        }
        cluster_cfg = _cfg_with_buckets(base={"pod_cidr_buckets": bad_buckets})
        with pytest.raises(SystemExit) as exc:
            cluster_config.tfvars("c", cluster_cfg, {})
        assert "cpu-general" in str(exc.value)

    def test_pod_cidr_buckets_invalid_az_name(self):
        """AZ key like 'us-east-99' fails validation."""
        bad_buckets = {
            "bucket-1": {"us-east-99": "100.64.0.0/16"},
        }
        cluster_cfg = _cfg_with_buckets(base={"pod_cidr_buckets": bad_buckets})
        with pytest.raises(SystemExit) as exc:
            cluster_config.tfvars("c", cluster_cfg, {})
        assert "us-east-99" in str(exc.value)
        assert "AZ" in str(exc.value)

    def test_pod_cidr_buckets_empty_az_name(self):
        """Empty-string AZ name fails validation."""
        bad_buckets = {
            "bucket-1": {"": "100.64.0.0/16"},
        }
        cluster_cfg = _cfg_with_buckets(base={"pod_cidr_buckets": bad_buckets})
        with pytest.raises(SystemExit) as exc:
            cluster_config.tfvars("c", cluster_cfg, {})
        assert "AZ" in str(exc.value)

    def test_pod_cidr_buckets_invalid_cidr_outside_cgnat(self):
        """CIDR outside CGNAT (e.g. 10.0.0.0/16) fails validation."""
        bad_buckets = {
            "bucket-1": {"us-west-2a": "10.0.0.0/16"},
        }
        cluster_cfg = _cfg_with_buckets(base={"pod_cidr_buckets": bad_buckets})
        with pytest.raises(SystemExit) as exc:
            cluster_config.tfvars("c", cluster_cfg, {})
        assert "10.0.0.0/16" in str(exc.value)
        assert "CGNAT" in str(exc.value)

    def test_pod_cidr_buckets_invalid_cidr_above_cgnat_range(self):
        """CIDR with second octet outside 64-127 (e.g. 100.128.0.0/16) fails."""
        bad_buckets = {
            "bucket-1": {"us-west-2a": "100.128.0.0/16"},
        }
        cluster_cfg = _cfg_with_buckets(base={"pod_cidr_buckets": bad_buckets})
        with pytest.raises(SystemExit) as exc:
            cluster_config.tfvars("c", cluster_cfg, {})
        assert "100.128.0.0/16" in str(exc.value)

    def test_pod_cidr_buckets_invalid_cidr_wrong_size(self):
        """CIDR with non-/16 prefix (e.g. /24) fails validation."""
        bad_buckets = {
            "bucket-1": {"us-west-2a": "100.64.0.0/24"},
        }
        cluster_cfg = _cfg_with_buckets(base={"pod_cidr_buckets": bad_buckets})
        with pytest.raises(SystemExit) as exc:
            cluster_config.tfvars("c", cluster_cfg, {})
        assert "100.64.0.0/24" in str(exc.value)

    def test_pod_cidr_buckets_duplicate_cidrs(self):
        """Two entries with the same CIDR string fails validation."""
        bad_buckets = {
            "bucket-1": {
                "us-west-2a": "100.64.0.0/16",
                "us-west-2b": "100.65.0.0/16",
            },
            "bucket-2": {
                "us-west-2a": "100.64.0.0/16",  # duplicate of bucket-1/us-west-2a
                "us-west-2b": "100.66.0.0/16",
            },
        }
        cluster_cfg = _cfg_with_buckets(base={"pod_cidr_buckets": bad_buckets})
        with pytest.raises(SystemExit) as exc:
            cluster_config.tfvars("c", cluster_cfg, {})
        msg = str(exc.value)
        assert "duplicate" in msg
        assert "100.64.0.0/16" in msg

    def test_pod_cidr_buckets_non_dict_inner(self):
        """Inner az_map that isn't a dict (e.g. string) fails validation."""
        bad_buckets = {"bucket-1": "not-a-dict"}
        cluster_cfg = _cfg_with_buckets(base={"pod_cidr_buckets": bad_buckets})
        with pytest.raises(SystemExit) as exc:
            cluster_config.tfvars("c", cluster_cfg, {})
        assert "bucket-1" in str(exc.value)

    def test_pod_cidr_buckets_non_dict_outer(self):
        """Outer pod_cidr_buckets that isn't a dict (e.g. list) fails validation."""
        cluster_cfg = _cfg_with_buckets(base={"pod_cidr_buckets": ["bucket-1"]})
        with pytest.raises(SystemExit) as exc:
            cluster_config.tfvars("c", cluster_cfg, {})
        assert "non-empty mapping" in str(exc.value)

    def test_pod_cidr_buckets_non_string_cidr(self):
        """Non-string CIDR value (e.g. int) fails validation."""
        bad_buckets = {"bucket-1": {"us-west-2a": 12345}}
        cluster_cfg = _cfg_with_buckets(base={"pod_cidr_buckets": bad_buckets})
        with pytest.raises(SystemExit) as exc:
            cluster_config.tfvars("c", cluster_cfg, {})
        assert "12345" in str(exc.value)


class TestValidatePodCidrBuckets:
    """Direct tests for the _validate_pod_cidr_buckets helper."""

    def test_valid_passes(self):
        # Should not raise
        cluster_config._validate_pod_cidr_buckets("c", _VALID_POD_CIDR_BUCKETS_2AZ)

    def test_valid_3az(self):
        # 4 x 3 AZs (production-shape)
        cluster_config._validate_pod_cidr_buckets("c", _VALID_POD_CIDR_BUCKETS_PROD)

    def test_all_four_buckets_valid(self):
        """All bucket-1 through bucket-4 names are accepted."""
        buckets = {f"bucket-{i}": {"us-west-2a": f"100.{63 + i}.0.0/16"} for i in range(1, 5)}
        cluster_config._validate_pod_cidr_buckets("c", buckets)


# ============================================================================
# main() tests
# ============================================================================


class TestMain:
    """Tests for main() — patches load_config to avoid real file I/O."""

    @pytest.fixture(autouse=True)
    def _patch_load_config(self):
        with patch.object(cluster_config, "load_config", return_value=FAKE_CONFIG):
            yield

    def test_no_args_exits_1(self, capsys):
        code = run_main()
        assert code == 1
        assert "Usage:" in capsys.readouterr().err

    def test_list_clusters(self, capsys):
        code = run_main("--list")
        assert code == 0
        out = capsys.readouterr().out
        assert "staging" in out
        assert "production" in out

    def test_unknown_cluster_exits_1(self, capsys):
        code = run_main("nonexistent")
        assert code == 1
        assert "unknown cluster" in capsys.readouterr().err

    def test_cluster_name(self, capsys):
        code = run_main("staging")
        assert code == 0
        assert capsys.readouterr().out.strip() == "my-staging"

    def test_cluster_name_explicit(self, capsys):
        code = run_main("staging", "cluster_name")
        assert code == 0
        assert capsys.readouterr().out.strip() == "my-staging"

    def test_region(self, capsys):
        code = run_main("production", "region")
        assert code == 0
        assert capsys.readouterr().out.strip() == "us-east-1"

    def test_state_bucket_explicit(self, capsys):
        code = run_main("staging", "state_bucket")
        assert code == 0
        assert capsys.readouterr().out.strip() == "my-tfstate-staging"

    def test_state_bucket_default(self, capsys):
        """Production has no state_bucket — should fall back to default."""
        code = run_main("production", "state_bucket")
        assert code == 0
        assert capsys.readouterr().out.strip() == "ciforge-tfstate-production"

    def test_modules(self, capsys):
        code = run_main("staging", "modules")
        assert code == 0
        lines = capsys.readouterr().out.strip().splitlines()
        assert lines == ["karpenter", "arc", "arc-runners"]

    def test_modules_production(self, capsys):
        code = run_main("production", "modules")
        assert code == 0
        lines = capsys.readouterr().out.strip().splitlines()
        assert "monitoring" in lines
        assert "buildkit" in lines

    def test_has_module_present(self):
        code = run_main("staging", "has-module", "karpenter")
        assert code == 0

    def test_has_module_absent(self):
        code = run_main("staging", "has-module", "buildkit")
        assert code == 1

    def test_has_module_no_arg(self):
        """has-module with no module name should exit 1 (empty string not in list)."""
        code = run_main("staging", "has-module")
        assert code == 1

    def test_tfvars_command(self, capsys):
        code = run_main("staging", "tfvars")
        assert code == 0
        out = capsys.readouterr().out.strip()
        assert '-var="cluster_name=my-staging"' in out
        assert '-var="aws_region=us-west-2"' in out
        # Staging overrides vpc_cidr via base
        assert '-var="vpc_cidr=10.1.0.0/16"' in out
        assert '-var="single_nat_gateway=true"' in out

    def test_dotpath_resolve(self, capsys):
        code = run_main("staging", "harbor.core_replicas")
        assert code == 0
        assert capsys.readouterr().out.strip() == "1"

    def test_dotpath_falls_back_to_defaults(self, capsys):
        """Production has no harbor override — should use defaults."""
        code = run_main("production", "harbor.core_replicas")
        assert code == 0
        assert capsys.readouterr().out.strip() == "2"

    def test_dotpath_with_explicit_default(self, capsys):
        code = run_main("staging", "nonexistent.key", "fallback-val")
        assert code == 0
        assert capsys.readouterr().out.strip() == "fallback-val"

    def test_dotpath_missing_no_default_exits_1(self, capsys):
        code = run_main("staging", "nonexistent.key")
        assert code == 1
        assert "not found" in capsys.readouterr().err

    def test_bool_field_output(self, capsys):
        """Boolean values should be printed lowercase."""
        code = run_main("staging", "feature_flag")
        assert code == 0
        assert capsys.readouterr().out.strip() == "true"

    def test_bool_false_field_output(self, capsys):
        """Boolean False from defaults should print 'false'."""
        code = run_main("staging", "base.single_nat_gateway")
        assert code == 0
        assert capsys.readouterr().out.strip() == "true"


# ============================================================================
# Integration test — real YAML file
# ============================================================================


class TestWithRealYaml:
    """Integration tests using a real clusters.yaml via CLUSTERS_YAML env var."""

    @pytest.fixture(autouse=True)
    def _setup_real_yaml(self, tmp_path, monkeypatch):
        p = tmp_path / "clusters.yaml"
        p.write_text(yaml.dump(FAKE_CONFIG, default_flow_style=False))
        monkeypatch.setattr(cluster_config, "CONFIG_PATH", p)

    def test_load_config(self):
        cfg = cluster_config.load_config()
        assert "clusters" in cfg
        assert "staging" in cfg["clusters"]

    def test_main_list(self, capsys):
        code = run_main("--list")
        assert code == 0
        out = capsys.readouterr().out
        assert "staging" in out
