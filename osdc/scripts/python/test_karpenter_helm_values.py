"""Tests for modules/karpenter/helm/values.yaml.

Guards the Karpenter Helm values that affect scheduling and node provisioning.
The reservedENIs setting is required for VPC CNI Custom Networking so the
scheduler subtracts the primary ENI from each instance's pod-IP capacity.
"""

from pathlib import Path

import yaml
from cni_constants import RESERVED_ENIS_COUNT

VALUES_PATH = Path(__file__).parents[2] / "modules" / "karpenter" / "helm" / "values.yaml"


def _load_values() -> dict:
    with VALUES_PATH.open() as fh:
        return yaml.safe_load(fh)


class TestKarpenterHelmValues:
    def test_reserved_enis_matches_constant(self):
        data = _load_values()
        assert data["settings"]["reservedENIs"] == RESERVED_ENIS_COUNT, (
            f"settings.reservedENIs in Helm values.yaml must equal RESERVED_ENIS_COUNT "
            f"({RESERVED_ENIS_COUNT}) from cni_constants.py — these are coupled constants. "
            f"Update both together."
        )
        # Must be an integer, not a string — the chart maps it to RESERVED_ENIS env var
        # and the controller parses it as int.
        assert isinstance(data["settings"]["reservedENIs"], int)
        assert not isinstance(data["settings"]["reservedENIs"], bool)

    def test_settings_block_required_keys(self):
        data = _load_values()
        settings = data["settings"]
        expected = {"clusterName", "clusterEndpoint", "interruptionQueue", "reservedENIs", "featureGates"}
        missing = expected - set(settings.keys())
        assert not missing, f"settings block is missing required keys: {sorted(missing)}"

    def test_no_top_level_reserved_enis_key(self):
        data = _load_values()
        # The setting MUST live under settings:, never at the top level.
        # The chart only reads settings.reservedENIs; a top-level key would be silently ignored.
        assert "reservedENIs" not in data
        assert "RESERVED_ENIS" not in data
