"""Tests for ``instance_specs`` ENI fields and basic invariants.

These tests guard the raw AWS ENI facts (``eni_count`` and ``ipv4_per_eni``)
that are used downstream for prefix-delegation max-pods math. They do NOT
assert anything about ``ENI_MAX_PODS``, which is allowed to deviate
from the AWS-stock formula (e.g. for safer node packing).
"""

from __future__ import annotations

import pytest
from instance_specs import ENI_MAX_PODS, INSTANCE_ENI_DATA, INSTANCE_SPECS

# Sanity bound: the AWS-stock max-pods formula
# (eni_count * (ipv4_per_eni - 1) + 2) is well under 750 for every
# instance shape we use. If a future entry blows past this, it's almost
# certainly a typo in eni_count/ipv4_per_eni.
AWS_STOCK_MAX_PODS_SANITY_BOUND = 750


class TestInstanceEniData:
    def test_every_instance_spec_has_eni_data(self) -> None:
        """Every INSTANCE_SPECS key must have a matching INSTANCE_ENI_DATA entry with positive ints."""
        for instance_type in INSTANCE_SPECS:
            assert instance_type in INSTANCE_ENI_DATA, f"{instance_type}: missing from INSTANCE_ENI_DATA"
            eni = INSTANCE_ENI_DATA[instance_type]
            assert "eni_count" in eni, f"{instance_type}: missing eni_count"
            assert "ipv4_per_eni" in eni, f"{instance_type}: missing ipv4_per_eni"
            assert isinstance(eni["eni_count"], int), (
                f"{instance_type}: eni_count must be int, got {type(eni['eni_count']).__name__}"
            )
            assert isinstance(eni["ipv4_per_eni"], int), (
                f"{instance_type}: ipv4_per_eni must be int, got {type(eni['ipv4_per_eni']).__name__}"
            )
            assert eni["eni_count"] > 0, f"{instance_type}: eni_count must be > 0, got {eni['eni_count']}"
            assert eni["ipv4_per_eni"] > 0, f"{instance_type}: ipv4_per_eni must be > 0, got {eni['ipv4_per_eni']}"

    @pytest.mark.parametrize(
        ("instance_type", "expected_eni_count", "expected_ipv4_per_eni"),
        [
            ("c7i.48xlarge", 15, 50),
            ("p5.48xlarge", 2, 50),
            ("p6-b200.48xlarge", 4, 50),
            ("g5.48xlarge", 7, 50),
            ("r7i.2xlarge", 4, 15),
            ("c7i.metal-24xl", 15, 50),
        ],
    )
    def test_specific_eni_values(self, instance_type: str, expected_eni_count: int, expected_ipv4_per_eni: int) -> None:
        """Spot-check a small set of representative rows to catch typos at edit time."""
        eni = INSTANCE_ENI_DATA[instance_type]
        assert eni["eni_count"] == expected_eni_count, (
            f"{instance_type}: eni_count expected {expected_eni_count}, got {eni['eni_count']}"
        )
        assert eni["ipv4_per_eni"] == expected_ipv4_per_eni, (
            f"{instance_type}: ipv4_per_eni expected {expected_ipv4_per_eni}, got {eni['ipv4_per_eni']}"
        )

    def test_known_invariants_aws_stock_max_pods_formula(self) -> None:
        """The AWS-stock max-pods formula must stay under our sanity bound for every entry.

        Formula: eni_count * (ipv4_per_eni - 1) + 2
        This intentionally does NOT compare against ENI_MAX_PODS — those values are
        deliberately allowed to differ.
        """
        for instance_type, eni in INSTANCE_ENI_DATA.items():
            aws_stock_max_pods = eni["eni_count"] * (eni["ipv4_per_eni"] - 1) + 2
            assert aws_stock_max_pods <= AWS_STOCK_MAX_PODS_SANITY_BOUND, (
                f"{instance_type}: AWS-stock max-pods formula yielded {aws_stock_max_pods}, "
                f"exceeds sanity bound {AWS_STOCK_MAX_PODS_SANITY_BOUND} "
                f"(eni_count={eni['eni_count']}, ipv4_per_eni={eni['ipv4_per_eni']})"
            )


class TestInstanceSpecs:
    def test_eni_max_pods_keys_subset_of_instance_specs(self) -> None:
        """Every ENI_MAX_PODS key must have a matching INSTANCE_SPECS entry."""
        missing = sorted(set(ENI_MAX_PODS.keys()) - set(INSTANCE_SPECS.keys()))
        assert not missing, f"ENI_MAX_PODS keys missing from INSTANCE_SPECS: {missing}"

    def test_vcpu_and_memory_positive(self) -> None:
        """Every INSTANCE_SPECS row must have positive vcpu and memory_gib."""
        for instance_type, spec in INSTANCE_SPECS.items():
            assert spec["vcpu"] > 0, f"{instance_type}: vcpu must be > 0, got {spec['vcpu']}"
            assert spec["memory_gib"] > 0, f"{instance_type}: memory_gib must be > 0, got {spec['memory_gib']}"
