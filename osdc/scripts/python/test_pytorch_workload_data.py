"""Tests for pytorch_workload_data module."""

from pytorch_workload_data import OLD_TO_NEW_LABEL, PEAK_CONCURRENT


class TestOldToNewLabel:
    def test_is_nonempty_dict(self):
        assert isinstance(OLD_TO_NEW_LABEL, dict)
        assert len(OLD_TO_NEW_LABEL) > 0

    def test_keys_are_strings(self):
        for key in OLD_TO_NEW_LABEL:
            assert isinstance(key, str)

    def test_values_are_strings(self):
        for value in OLD_TO_NEW_LABEL.values():
            assert isinstance(value, str)

    def test_known_mapping_x86_cpu(self):
        assert OLD_TO_NEW_LABEL["linux.2xlarge"] == "l-x86iavx512-8-16"

    def test_known_mapping_arm64(self):
        assert OLD_TO_NEW_LABEL["linux.arm64.2xlarge"] == "l-arm64g2-6-32"

    def test_known_mapping_gpu(self):
        assert OLD_TO_NEW_LABEL["linux.g5.4xlarge.nvidia.gpu"] == "l-x86aavx2-29-113-a10g"


class TestPeakConcurrent:
    def test_is_nonempty_dict(self):
        assert isinstance(PEAK_CONCURRENT, dict)
        assert len(PEAK_CONCURRENT) > 0

    def test_values_are_positive_ints(self):
        for key, value in PEAK_CONCURRENT.items():
            assert isinstance(value, int), f"{key} value is not int"
            assert value > 0, f"{key} has non-positive value"

    def test_known_peak(self):
        assert PEAK_CONCURRENT["linux.2xlarge"] == 1473

    def test_all_keys_are_strings(self):
        for key in PEAK_CONCURRENT:
            assert isinstance(key, str)
