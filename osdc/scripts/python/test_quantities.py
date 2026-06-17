"""Unit tests for quantities.py — Kubernetes quantity parsing."""

import pytest
from quantities import parse_memory_bytes


class TestParseMemoryBytes:
    def test_binary_suffixes(self):
        assert parse_memory_bytes("256Ki") == 256 * 1024
        assert parse_memory_bytes("512Mi") == 512 * 1024**2
        assert parse_memory_bytes("115Gi") == 115 * 1024**3
        assert parse_memory_bytes("1Ti") == 1 * 1024**4

    def test_decimal_si_suffixes(self):
        assert parse_memory_bytes("500K") == 500 * 1000
        assert parse_memory_bytes("500M") == 500 * 1000**2
        assert parse_memory_bytes("10G") == 10 * 1000**3
        assert parse_memory_bytes("2T") == 2 * 1000**4

    def test_bare_integer_is_bytes(self):
        assert parse_memory_bytes("1024") == 1024
        assert parse_memory_bytes(1024) == 1024

    def test_zero(self):
        assert parse_memory_bytes("0") == 0
        assert parse_memory_bytes(0) == 0

    def test_whitespace_is_tolerated(self):
        assert parse_memory_bytes("  8500Mi  ") == 8500 * 1024**2

    def test_boot_gate_values_round_trip(self):
        # The exact p4d boot-gate numbers: 8500Mi + 0 + 100Mi == 4300Mi + 4300Mi
        lhs = parse_memory_bytes("8500Mi") + parse_memory_bytes("0") + parse_memory_bytes("100Mi")
        rhs = parse_memory_bytes("4300Mi") + parse_memory_bytes("4300Mi")
        assert lhs == rhs == 8600 * 1024**2

    def test_mixed_units_compare_exactly(self):
        # 4Gi + 4504Mi == 8600Mi (binary), and Gi != Mi is not conflated
        assert parse_memory_bytes("4Gi") + parse_memory_bytes("4504Mi") == parse_memory_bytes("8600Mi")

    @pytest.mark.parametrize("bad", ["4.5Gi", "1.5Mi", "1e3Mi", "abc", "Mi", ""])
    def test_fractional_or_garbage_raises(self, bad):
        # Fractional mantissa and non-numeric input must fail loudly, never
        # silently truncate — reservation math depends on exactness.
        with pytest.raises(ValueError, match="invalid literal"):
            parse_memory_bytes(bad)
