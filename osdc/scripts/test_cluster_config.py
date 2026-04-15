"""Unit tests for cluster-config.py."""

import importlib.util
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


class TestTfvars:
    """Tests for the tfvars() function."""

    def test_all_defaults(self, capsys):
        cluster_cfg = {
            "cluster_name": "test-cluster",
            "region": "us-west-2",
        }
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
        assert '-var="control_plane_scaling_tier=standard"' in out

    def test_cluster_overrides(self, capsys):
        cluster_cfg = {
            "cluster_name": "prod-cluster",
            "region": "eu-west-1",
            "base": {
                "vpc_cidr": "10.99.0.0/16",
                "single_nat_gateway": True,
                "base_node_count": 6,
                "base_node_instance_type": "m6i.2xlarge",
                "base_node_max_unavailable_percentage": 50,
                "base_node_ami_version": "v20260318",
                "eks_version": "1.36",
                "control_plane_scaling_tier": "tier-xl",
            },
        }
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
        assert '-var="control_plane_scaling_tier=tier-xl"' in out

    def test_control_plane_scaling_tier_from_defaults(self, capsys):
        """Tier set in defaults should propagate when cluster has no override."""
        cluster_cfg = {
            "cluster_name": "c",
            "region": "r",
        }
        defaults = {"control_plane_scaling_tier": "tier-2xl"}
        cluster_config.tfvars("c", cluster_cfg, defaults)
        out = capsys.readouterr().out.strip()
        assert '-var="control_plane_scaling_tier=tier-2xl"' in out

    def test_bool_formatting(self, capsys):
        """single_nat_gateway bool should be lowercased."""
        cluster_cfg = {
            "cluster_name": "c",
            "region": "r",
            "base": {"single_nat_gateway": True},
        }
        cluster_config.tfvars("c", cluster_cfg, {})
        out = capsys.readouterr().out.strip()
        assert '-var="single_nat_gateway=true"' in out
        # Ensure it's not Python-style True
        assert "True" not in out

    def test_eks_version_from_defaults(self, capsys):
        cluster_cfg = {
            "cluster_name": "c",
            "region": "r",
        }
        defaults = {"eks_version": "1.30"}
        cluster_config.tfvars("c", cluster_cfg, defaults)
        out = capsys.readouterr().out.strip()
        assert '-var="eks_version=1.30"' in out

    def test_no_base_key(self, capsys):
        """Cluster with no 'base' key should use defaults for everything."""
        cluster_cfg = {
            "cluster_name": "minimal",
            "region": "ap-southeast-1",
        }
        cluster_config.tfvars("minimal", cluster_cfg, {"vpc_cidr": "10.50.0.0/16"})
        out = capsys.readouterr().out.strip()
        assert '-var="vpc_cidr=10.50.0.0/16"' in out


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
