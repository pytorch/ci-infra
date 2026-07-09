"""Tests for fleet_naming module."""

import pytest
from fleet_naming import (
    RESERVED_NODE_FLEET_NAMES,
    derive_fleet_name,
    derive_release_runner_group,
    validate_node_fleet,
)

# ============================================================================
# derive_fleet_name
# ============================================================================


class TestDeriveFleetName:
    def test_family_cpu_c7i(self):
        assert derive_fleet_name("c7i.24xlarge") == "c7i"

    def test_family_cpu_c7i_metal(self):
        assert derive_fleet_name("c7i.metal-24xl") == "c7i"

    def test_family_gpu_g5(self):
        assert derive_fleet_name("g5.8xlarge") == "g5"

    def test_family_gpu_g5_48xlarge(self):
        assert derive_fleet_name("g5.48xlarge") == "g5"

    def test_family_gpu_p4d(self):
        assert derive_fleet_name("p4d.24xlarge") == "p4d"

    def test_family_gpu_p5(self):
        assert derive_fleet_name("p5.48xlarge") == "p5"

    def test_family_gpu_p6_b200(self):
        assert derive_fleet_name("p6-b200.48xlarge") == "p6-b200"

    def test_family_unknown_returns_prefix(self):
        assert derive_fleet_name("z99.xlarge") == "z99"

    def test_override_takes_precedence_over_family(self):
        assert derive_fleet_name("g5.48xlarge", override="g5-48xlarge") == "g5-48xlarge"

    def test_override_none_falls_back_to_family(self):
        assert derive_fleet_name("g5.48xlarge", override=None) == "g5"

    @pytest.mark.parametrize(
        "override",
        ["", 0, False, []],
    )
    def test_override_invalid_value_raises(self, override):
        with pytest.raises(ValueError, match="node_fleet override invalid"):
            derive_fleet_name("g5.48xlarge", override=override)

    def test_override_reserved_raises(self):
        with pytest.raises(ValueError, match="reserved"):
            derive_fleet_name("c7i.24xlarge", override="c7i-runner")


# ============================================================================
# derive_release_runner_group
# ============================================================================


class TestDeriveReleaseRunnerGroup:
    def test_cluster_group_gets_release_suffix(self):
        assert derive_release_runner_group("meta-prod-aws-ue1") == "meta-prod-aws-ue1-release-runners"

    def test_staging_group_gets_release_suffix(self):
        assert derive_release_runner_group("meta-staging-aws-ue1") == "meta-staging-aws-ue1-release-runners"

    @pytest.mark.parametrize("empty", [None, ""])
    def test_no_cluster_group_falls_back_to_default(self, empty):
        # A group-less cluster must not become "-release-runners" (invalid) nor
        # double-suffix; it falls back to the "default" GitHub group.
        assert derive_release_runner_group(empty) == "default"


# ============================================================================
# validate_node_fleet — reserved set surface
# ============================================================================


class TestReservedNames:
    def test_c7i_runner_is_reserved(self):
        assert "c7i-runner" in RESERVED_NODE_FLEET_NAMES

    def test_reserved_set_is_frozenset(self):
        assert isinstance(RESERVED_NODE_FLEET_NAMES, frozenset)


# ============================================================================
# validate_node_fleet — valid inputs
# ============================================================================


class TestValidateNodeFleetValid:
    @pytest.mark.parametrize(
        "value",
        [
            "g5-48xlarge",
            "g4dn-metal",
            "p6-b200",
            "a",
            "a1",
            "g5",
            "z99",
            "m6i",
            "c7i",
            "abc-def-ghi",
            "a" * 63,
        ],
    )
    def test_valid_dns1123_label_accepted(self, value):
        ok, err = validate_node_fleet(value)
        assert ok is True, f"expected {value!r} to be valid; got error {err!r}"
        assert err is None


# ============================================================================
# validate_node_fleet — invalid type
# ============================================================================


class TestValidateNodeFleetInvalidType:
    @pytest.mark.parametrize(
        ("value", "type_name"),
        [
            (None, "NoneType"),
            (123, "int"),
            (True, "bool"),
            (1.5, "float"),
            (["g5"], "list"),
            ({"a": 1}, "dict"),
            ((), "tuple"),
        ],
    )
    def test_non_string_rejected(self, value, type_name):
        ok, err = validate_node_fleet(value)
        assert ok is False
        assert err is not None
        assert "must be a string" in err
        assert type_name in err


# ============================================================================
# validate_node_fleet — invalid format (DNS-1123 violations)
# ============================================================================


class TestValidateNodeFleetInvalidFormat:
    @pytest.mark.parametrize(
        "value",
        [
            "",  # empty
            " ",  # single space
            "   ",  # whitespace
            "G5-48xlarge",  # uppercase
            "G5",  # uppercase short
            "a" * 64,  # too long
            "-g5",  # leading dash
            "g5-",  # trailing dash
            "-",  # only dash
            "g5_48xlarge",  # underscore
            "g5/48",  # slash
            "g5.48",  # dot
            "g5\nevil",  # newline (YAML injection vector)
            "g5\tevil",  # tab
            "g5 evil",  # internal space
            'g5"evil',  # quote (YAML injection vector)
            "g5'evil",  # apostrophe
            "g5\\evil",  # backslash
            "g5\x00null",  # null byte
            "g5\revil",  # carriage return
            "  g5-48xlarge  ",  # leading/trailing whitespace
            "g5-48xlarge ",  # trailing space only
            " g5-48xlarge",  # leading space only
            "g5@runner",  # at sign
            "g5#runner",  # hash
        ],
    )
    def test_invalid_format_rejected_with_dns1123_message(self, value):
        ok, err = validate_node_fleet(value)
        assert ok is False, f"expected {value!r} to be rejected"
        assert err is not None
        assert "DNS-1123" in err


# ============================================================================
# validate_node_fleet — reserved names
# ============================================================================


class TestValidateNodeFleetReserved:
    def test_c7i_runner_rejected_as_reserved(self):
        ok, err = validate_node_fleet("c7i-runner")
        assert ok is False
        assert err is not None
        assert "reserved" in err
        # The error must include the rejected value so the operator can match it
        # against the def file they're editing.
        assert "c7i-runner" in err

    def test_reserved_message_mentions_override(self):
        _, err = validate_node_fleet("c7i-runner")
        assert err is not None
        assert "override" in err
