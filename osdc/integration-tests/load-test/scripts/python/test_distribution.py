"""Unit tests for load test distribution logic."""

import pytest
import yaml
from distribution import (
    OLD_TO_OSDC_LABEL,
    PRODUCTION_JOB_COUNTS,
    RunnerAllocation,
    _aggregate_production_counts,
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
    def _make_def(self, defs_dir, name, vcpu=4, memory="8Gi", gpu=0):
        defs_dir.mkdir(parents=True, exist_ok=True)
        (defs_dir / f"{name}.yaml").write_text(
            yaml.dump(
                {"runner": {"name": name, "instance_type": "test", "vcpu": vcpu, "memory": memory, "gpu": gpu}},
            ),
        )

    def test_scans_upstream(self, tmp_path):
        upstream = tmp_path / "upstream"
        self._make_def(upstream / "modules" / "arc-runners" / "defs", "l-x86iavx512-8-16")

        result = get_available_runners(upstream, tmp_path / "root")
        assert "l-x86iavx512-8-16" in result

    def test_scans_consumer(self, tmp_path):
        upstream = tmp_path / "upstream"
        root = tmp_path / "root"
        self._make_def(upstream / "modules" / "arc-runners" / "defs", "l-x86iavx512-8-16")
        self._make_def(root / "modules" / "arc-runners" / "defs", "l-custom-runner")

        result = get_available_runners(upstream, root)
        assert "l-x86iavx512-8-16" in result
        assert "l-custom-runner" in result


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
