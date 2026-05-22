"""Unit tests for load test distribution logic."""

import pytest
import yaml
from distribution import (
    _LOAD_TEST_EXCLUDED_ARM_PATTERN,
    _LOAD_TEST_EXCLUDED_GPU_PATTERN,
    OLD_TO_OSDC_LABEL,
    PRODUCTION_JOB_COUNTS,
    RunnerAllocation,
    _aggregate_production_counts,
    _load_fleet_exclusions,
    classify_runner,
    compute_distribution,
    get_available_runners,
)

# ── classify_runner ──────────────────────────────────────────────────────


class TestClassifyRunner:
    def test_cpu_x86(self):
        is_gpu, is_arm64, gpu_count = classify_runner("l-x86iavx512-8-16")
        assert is_gpu is False
        assert is_arm64 is False
        assert gpu_count == 0

    def test_cpu_arm64(self):
        is_gpu, is_arm64, gpu_count = classify_runner("l-arm64g3-16-62")
        assert is_gpu is False
        assert is_arm64 is True
        assert gpu_count == 0

    def test_gpu_t4_single(self):
        is_gpu, is_arm64, gpu_count = classify_runner("l-x86iavx512-29-115-t4")
        assert is_gpu is True
        assert is_arm64 is False
        assert gpu_count == 1

    def test_gpu_t4_multi(self):
        is_gpu, is_arm64, gpu_count = classify_runner("l-x86iavx512-45-172-t4-4")
        assert is_gpu is True
        assert gpu_count == 4

    def test_gpu_a10g(self):
        is_gpu, _, gpu_count = classify_runner("l-x86aavx2-29-113-a10g")
        assert is_gpu is True
        assert gpu_count == 1

    def test_gpu_a10g_multi(self):
        is_gpu, _, gpu_count = classify_runner("l-x86aavx2-45-167-a10g-4")
        assert is_gpu is True
        assert gpu_count == 4

    def test_gpu_l4(self):
        is_gpu, _, gpu_count = classify_runner("l-x86aavx2-29-113-l4")
        assert is_gpu is True
        assert gpu_count == 1

    def test_gpu_t4_baremetal(self):
        is_gpu, _, gpu_count = classify_runner("l-bx86iavx512-94-344-t4-8")
        assert is_gpu is True
        assert gpu_count == 8

    def test_baremetal_arm64(self):
        is_gpu, is_arm64, gpu_count = classify_runner("l-barm64g4-62-226")
        assert is_gpu is False
        assert is_arm64 is True
        assert gpu_count == 0

    def test_baremetal_amx(self):
        is_gpu, is_arm64, gpu_count = classify_runner("l-bx86iamx-92-167")
        assert is_gpu is False
        assert is_arm64 is False
        assert gpu_count == 0


# ── OLD_TO_OSDC_LABEL mapping ───────────────────────────────────────────


class TestMapping:
    def test_all_osdc_labels_are_valid(self):
        """Every mapped OSDC label should be a plausible runner name."""
        for old, osdc in OLD_TO_OSDC_LABEL.items():
            assert osdc.startswith("l-"), f"Bad OSDC label for {old}: {osdc}"

    def test_many_to_one_collapse(self):
        """Multiple old labels should map to the same OSDC label."""
        assert OLD_TO_OSDC_LABEL["linux.2xlarge"] == "l-x86iavx512-8-16"
        assert OLD_TO_OSDC_LABEL["linux.c7i.2xlarge"] == "l-x86iavx512-8-16"

    def test_memory_collapse(self):
        """r5 and r7i labels collapse to same OSDC label."""
        assert (
            OLD_TO_OSDC_LABEL["linux.r7i.2xlarge"] == OLD_TO_OSDC_LABEL["linux.2xlarge.memory"] == "l-x86iavx512-8-64"
        )

    def test_production_counts_coverage(self):
        """Every old label with production traffic should be in the mapping."""
        unmapped_with_traffic = []
        for old_label, count in PRODUCTION_JOB_COUNTS.items():
            if old_label not in OLD_TO_OSDC_LABEL:
                unmapped_with_traffic.append((old_label, count))
        assert unmapped_with_traffic == [], f"Old labels with traffic but no mapping: {unmapped_with_traffic}"


# ── _aggregate_production_counts ────────────────────────────────────────


class TestAggregateProductionCounts:
    def test_aggregation(self):
        counts = _aggregate_production_counts()
        # linux.2xlarge (491396) + linux.c7i.2xlarge (350911) = 842307
        assert counts["l-x86iavx512-8-16"] == 491_396 + 350_911

    def test_no_unmapped_labels(self):
        """Aggregated counts should only contain valid OSDC labels."""
        counts = _aggregate_production_counts()
        valid_labels = set(OLD_TO_OSDC_LABEL.values())
        for label in counts:
            assert label in valid_labels, f"Unexpected label: {label}"


# ── get_available_runners ────────────────────────────────────────────────


class TestGetAvailableRunners:
    def _make_def(self, defs_dir, name, vcpu=4, memory="8Gi", gpu=0, instance_type="test.xlarge"):
        defs_dir.mkdir(parents=True, exist_ok=True)
        runner = {"name": name, "instance_type": instance_type, "vcpu": vcpu, "memory": memory, "gpu": gpu}
        (defs_dir / f"{name}.yaml").write_text(yaml.dump({"runner": runner}))

    def test_scans_upstream(self, tmp_path):
        upstream = tmp_path / "upstream"
        self._make_def(upstream / "modules" / "arc-runners" / "defs", "l-x86iavx512-8-16")

        labels, excluded = get_available_runners(upstream, tmp_path / "root")
        assert "l-x86iavx512-8-16" in labels
        assert excluded == 0

    def test_scans_consumer(self, tmp_path):
        upstream = tmp_path / "upstream"
        root = tmp_path / "root"
        self._make_def(upstream / "modules" / "arc-runners" / "defs", "l-x86iavx512-8-16")
        self._make_def(root / "modules" / "arc-runners" / "defs", "l-custom-runner")

        labels, excluded = get_available_runners(upstream, root)
        assert "l-x86iavx512-8-16" in labels
        assert "l-custom-runner" in labels
        assert excluded == 0

    def _make_fleet_def(self, defs_dir, name, exclude_regions=None):
        defs_dir.mkdir(parents=True, exist_ok=True)
        fleet = {
            "fleet": {
                "name": name,
                "arch": "amd64",
                "gpu": False,
                "instances": [{"type": f"{name}.48xlarge", "weight": 100, "node_disk_size": 3750}],
            },
        }
        if exclude_regions:
            fleet["fleet"]["exclude_regions"] = exclude_regions
        (defs_dir / f"{name}.yaml").write_text(yaml.dump(fleet))

    def test_excludes_runners_by_region(self, tmp_path):
        upstream = tmp_path / "upstream"
        fleet_defs = upstream / "modules" / "nodepools" / "defs"
        self._make_fleet_def(fleet_defs, "c7a", exclude_regions=["us-west-1"])

        runner_defs = upstream / "modules" / "arc-runners" / "defs"
        self._make_def(runner_defs, "l-c7a-runner", instance_type="c7a.48xlarge")
        self._make_def(runner_defs, "l-m7i-runner", instance_type="m7i.48xlarge")

        labels, excluded = get_available_runners(upstream, tmp_path / "root", region="us-west-1")
        assert "l-c7a-runner" not in labels
        assert "l-m7i-runner" in labels
        assert excluded == 1

    def test_no_exclusion_without_region(self, tmp_path):
        upstream = tmp_path / "upstream"
        fleet_defs = upstream / "modules" / "nodepools" / "defs"
        self._make_fleet_def(fleet_defs, "c7a", exclude_regions=["us-west-1"])

        runner_defs = upstream / "modules" / "arc-runners" / "defs"
        self._make_def(runner_defs, "l-c7a-runner", instance_type="c7a.48xlarge")
        self._make_def(runner_defs, "l-m7i-runner", instance_type="m7i.48xlarge")

        labels, excluded = get_available_runners(upstream, tmp_path / "root")
        assert "l-c7a-runner" in labels
        assert "l-m7i-runner" in labels
        assert excluded == 0

    def test_region_not_in_exclusion_list(self, tmp_path):
        upstream = tmp_path / "upstream"
        fleet_defs = upstream / "modules" / "nodepools" / "defs"
        self._make_fleet_def(fleet_defs, "c7a", exclude_regions=["us-west-1"])

        runner_defs = upstream / "modules" / "arc-runners" / "defs"
        self._make_def(runner_defs, "l-c7a-runner", instance_type="c7a.48xlarge")
        self._make_def(runner_defs, "l-m7i-runner", instance_type="m7i.48xlarge")

        labels, excluded = get_available_runners(upstream, tmp_path / "root", region="us-east-2")
        assert "l-c7a-runner" in labels
        assert "l-m7i-runner" in labels
        assert excluded == 0

    def test_multiple_fleets_excluded(self, tmp_path):
        upstream = tmp_path / "upstream"
        fleet_defs = upstream / "modules" / "nodepools" / "defs"
        self._make_fleet_def(fleet_defs, "c7a", exclude_regions=["us-west-1"])
        self._make_fleet_def(fleet_defs, "g5", exclude_regions=["us-west-1"])

        runner_defs = upstream / "modules" / "arc-runners" / "defs"
        self._make_def(runner_defs, "l-c7a-runner", instance_type="c7a.48xlarge")
        self._make_def(runner_defs, "l-g5-runner", instance_type="g5.8xlarge")
        self._make_def(runner_defs, "l-m7i-runner", instance_type="m7i.48xlarge")

        labels, excluded = get_available_runners(upstream, tmp_path / "root", region="us-west-1")
        assert "l-c7a-runner" not in labels
        assert "l-g5-runner" not in labels
        assert "l-m7i-runner" in labels
        assert excluded == 2

    def test_runner_without_instance_type_not_excluded(self, tmp_path):
        upstream = tmp_path / "upstream"
        fleet_defs = upstream / "modules" / "nodepools" / "defs"
        self._make_fleet_def(fleet_defs, "c7a", exclude_regions=["us-west-1"])

        runner_defs = upstream / "modules" / "arc-runners" / "defs"
        runner_defs.mkdir(parents=True, exist_ok=True)
        (runner_defs / "l-no-instance.yaml").write_text(
            yaml.dump({"runner": {"name": "l-no-instance", "vcpu": 4, "memory": "8Gi"}}),
        )

        labels, excluded = get_available_runners(upstream, tmp_path / "root", region="us-west-1")
        assert "l-no-instance" in labels

    def test_excludes_specialized_gpu_a100(self, tmp_path):
        """Single specialized GPU runner (a100) is excluded; counter increments."""
        upstream = tmp_path / "upstream"
        runner_defs = upstream / "modules" / "arc-runners" / "defs"
        self._make_def(runner_defs, "l-x86iavx512-44-500-a100-4")
        self._make_def(runner_defs, "l-x86iavx512-8-16")

        labels, excluded = get_available_runners(upstream, tmp_path / "root")
        assert "l-x86iavx512-44-500-a100-4" not in labels
        assert "l-x86iavx512-8-16" in labels
        assert excluded == 1

    def test_excludes_all_denied_gpu_types(self, tmp_path):
        """All denied GPU types (a100, h100, b200, h200, mi300, mi325) are excluded."""
        upstream = tmp_path / "upstream"
        runner_defs = upstream / "modules" / "arc-runners" / "defs"
        denied = [
            "l-x86iavx512-44-500-a100-4",
            "l-x86iavx512-44-500-h100-8",
            "l-x86iavx512-44-500-b200-8",
            "l-x86iavx512-44-500-h200-8",
            "l-x86aavx2-44-500-mi300",
            "l-x86aavx2-44-500-mi325",
        ]
        for name in denied:
            self._make_def(runner_defs, name)
        self._make_def(runner_defs, "l-x86iavx512-8-16")

        labels, excluded = get_available_runners(upstream, tmp_path / "root")
        for name in denied:
            assert name not in labels, f"{name} should have been excluded"
        assert labels == {"l-x86iavx512-8-16"}
        assert excluded == len(denied)

    def test_allowed_gpu_types_not_excluded(self, tmp_path):
        """Allowed GPU types (t4, a10g, l4) are NOT excluded."""
        upstream = tmp_path / "upstream"
        runner_defs = upstream / "modules" / "arc-runners" / "defs"
        allowed = [
            "l-x86iavx512-29-115-t4",
            "l-x86iavx512-45-172-t4-4",
            "l-x86aavx2-29-113-a10g",
            "l-x86aavx2-45-167-a10g-4",
            "l-x86aavx2-29-113-l4",
            "l-x86aavx2-45-172-l4-4",
        ]
        for name in allowed:
            self._make_def(runner_defs, name)

        labels, excluded = get_available_runners(upstream, tmp_path / "root")
        for name in allowed:
            assert name in labels, f"{name} should be available"
        assert excluded == 0

    def test_excludes_multi_gpu_suffix_variants(self, tmp_path):
        """Multi-GPU suffix variants (-a100-2, -a100-8) are excluded."""
        upstream = tmp_path / "upstream"
        runner_defs = upstream / "modules" / "arc-runners" / "defs"
        variants = [
            "l-bx86iavx512-88-1000-a100-8",
            "l-x86iavx512-22-250-a100-2",
        ]
        for name in variants:
            self._make_def(runner_defs, name)

        labels, excluded = get_available_runners(upstream, tmp_path / "root")
        for name in variants:
            assert name not in labels, f"{name} should have been excluded"
        assert excluded == len(variants)

    def test_pattern_does_not_false_match_cpu_runners(self, tmp_path):
        """A CPU runner whose name happens to contain digits in similar positions
        (e.g. l-x86iavx512-94-100) must NOT be excluded."""
        upstream = tmp_path / "upstream"
        runner_defs = upstream / "modules" / "arc-runners" / "defs"
        cpu_names = [
            "l-x86iavx512-94-100",
            "l-x86iavx512-94-192",
            "l-x86iavx512-46-85",
        ]
        for name in cpu_names:
            self._make_def(runner_defs, name)

        labels, excluded = get_available_runners(upstream, tmp_path / "root")
        for name in cpu_names:
            assert name in labels, f"{name} should NOT have been excluded"
            assert _LOAD_TEST_EXCLUDED_GPU_PATTERN.search(name) is None
        assert excluded == 0

    def test_excludes_arm64g2_runner(self, tmp_path):
        """g2 (Graviton 2) runners are excluded from load tests."""
        upstream = tmp_path / "upstream"
        runner_defs = upstream / "modules" / "arc-runners" / "defs"
        self._make_def(runner_defs, "l-arm64g2-6-32")
        self._make_def(runner_defs, "l-arm64g3-16-62")
        self._make_def(runner_defs, "l-arm64g4-16-62")

        labels, excluded = get_available_runners(upstream, tmp_path / "root")
        assert "l-arm64g2-6-32" not in labels
        assert "l-arm64g3-16-62" in labels
        assert "l-arm64g4-16-62" in labels
        assert excluded == 1

    def test_excludes_arm64g2_baremetal_variant(self, tmp_path):
        """Hypothetical baremetal g2 runner (l-barm64g2-*) is also excluded."""
        upstream = tmp_path / "upstream"
        runner_defs = upstream / "modules" / "arc-runners" / "defs"
        self._make_def(runner_defs, "l-barm64g2-12-32")

        labels, excluded = get_available_runners(upstream, tmp_path / "root")
        assert "l-barm64g2-12-32" not in labels
        assert excluded == 1

    def test_arm_pattern_does_not_match_g3_g4(self):
        """The g2 exclusion pattern must NOT match g3 or g4 runners."""
        assert _LOAD_TEST_EXCLUDED_ARM_PATTERN.search("l-arm64g3-16-62") is None
        assert _LOAD_TEST_EXCLUDED_ARM_PATTERN.search("l-arm64g4-16-62") is None
        assert _LOAD_TEST_EXCLUDED_ARM_PATTERN.search("l-barm64g4-62-226") is None
        assert _LOAD_TEST_EXCLUDED_ARM_PATTERN.search("l-arm64g2-6-32") is not None
        assert _LOAD_TEST_EXCLUDED_ARM_PATTERN.search("l-barm64g2-12-32") is not None


# ── _load_fleet_exclusions ──────────────────────────────────────────────


class TestLoadFleetExclusions:
    def test_loads_exclusions(self, tmp_path):
        upstream = tmp_path / "upstream"
        defs_dir = upstream / "modules" / "nodepools" / "defs"
        defs_dir.mkdir(parents=True, exist_ok=True)

        (defs_dir / "c7a.yaml").write_text(
            yaml.dump({
                "fleet": {
                    "name": "c7a",
                    "arch": "amd64",
                    "gpu": False,
                    "exclude_regions": ["us-west-1", "eu-west-1"],
                    "instances": [{"type": "c7a.48xlarge", "weight": 100, "node_disk_size": 3750}],
                },
            }),
        )
        (defs_dir / "m7i.yaml").write_text(
            yaml.dump({
                "fleet": {
                    "name": "m7i",
                    "arch": "amd64",
                    "gpu": False,
                    "instances": [{"type": "m7i.48xlarge", "weight": 100, "node_disk_size": 3750}],
                },
            }),
        )

        result = _load_fleet_exclusions(upstream, tmp_path / "root")
        assert result == {"c7a": ["us-west-1", "eu-west-1"]}

    def test_empty_dir(self, tmp_path):
        result = _load_fleet_exclusions(tmp_path / "nonexistent", tmp_path / "also-nonexistent")
        assert result == {}


# ── compute_distribution ─────────────────────────────────────────────────


class TestComputeDistribution:
    def test_empty_runners(self):
        assert compute_distribution(100, set()) == []

    def test_zero_jobs(self):
        assert compute_distribution(0, {"l-x86iavx512-8-16"}) == []

    def test_exact_total(self):
        """Allocated jobs must sum to exactly total_jobs."""
        runners = {"l-x86iavx512-8-16", "l-x86iavx512-16-32", "l-arm64g3-16-62"}
        result = compute_distribution(100, runners)
        total = sum(a.job_count for a in result)
        assert total == 100

    def test_min_one_per_type(self):
        """Each runner type gets at least 1 job."""
        runners = {"l-x86iavx512-8-16", "l-x86iavx512-94-768"}
        result = compute_distribution(10, runners)
        for a in result:
            assert a.job_count >= 1

    def test_proportional_allocation(self):
        """Runner types with more production traffic get more jobs."""
        # l-x86iavx512-8-16 has high traffic (842k), l-x86iavx512-94-768 has low (3.6k)
        runners = {"l-x86iavx512-8-16", "l-x86iavx512-94-768"}
        result = compute_distribution(100, runners)
        by_label = {a.osdc_label: a for a in result}
        assert by_label["l-x86iavx512-8-16"].job_count > by_label["l-x86iavx512-94-768"].job_count

    def test_unknown_runner_gets_min(self):
        """A runner with no production traffic still gets min allocation."""
        runners = {"l-x86iavx512-8-16", "l-unknown-runner"}
        result = compute_distribution(50, runners)
        by_label = {a.osdc_label: a for a in result}
        assert by_label["l-unknown-runner"].job_count >= 1
        assert by_label["l-unknown-runner"].source_job_count == 0

    def test_fewer_jobs_than_types(self):
        """When total_jobs < len(runners), not all types get a job."""
        runners = {"l-x86iavx512-8-16", "l-arm64g3-16-62", "l-x86iavx512-94-768"}
        result = compute_distribution(2, runners)
        total = sum(a.job_count for a in result)
        assert total == 2
        allocated = [a for a in result if a.job_count > 0]
        assert len(allocated) == 2

    def test_classification_propagated(self):
        """RunnerAllocation should have correct GPU/ARM classification."""
        runners = {"l-x86iavx512-8-16", "l-arm64g3-16-62", "l-x86iavx512-29-115-t4"}
        result = compute_distribution(30, runners)
        by_label = {a.osdc_label: a for a in result}

        cpu = by_label["l-x86iavx512-8-16"]
        assert cpu.is_gpu is False
        assert cpu.is_arm64 is False

        arm = by_label["l-arm64g3-16-62"]
        assert arm.is_arm64 is True

        gpu = by_label["l-x86iavx512-29-115-t4"]
        assert gpu.is_gpu is True
        assert gpu.gpu_count == 1

    def test_sorted_by_label(self):
        runners = {"l-z-runner", "l-a-runner", "l-m-runner"}
        result = compute_distribution(30, runners)
        labels = [a.osdc_label for a in result]
        assert labels == sorted(labels)

    def test_large_total(self):
        """Distribution works with large job counts."""
        runners = {"l-x86iavx512-8-16", "l-x86iavx512-16-32", "l-arm64g3-16-62"}
        result = compute_distribution(1000, runners)
        assert sum(a.job_count for a in result) == 1000
