"""Unit tests for load test workflow generation."""

import yaml
from distribution import RunnerAllocation
from workflow_generator import (
    CPU_CONTAINER,
    GPU_CONTAINER,
    MAX_MATRIX_SIZE,
    generate_workflow,
    sanitize_job_key,
)


# ── sanitize_job_key ─────────────────────────────────────────────────────


class TestSanitizeJobKey:
    def test_osdc_label(self):
        assert sanitize_job_key("l-x86iavx512-8-16") == "l-x86iavx512-8-16"

    def test_dots_replaced(self):
        assert sanitize_job_key("some.dotted.label") == "some-dotted-label"

    def test_preserves_hyphens_and_underscores(self):
        assert sanitize_job_key("my_runner-name") == "my_runner-name"


# ── generate_workflow ────────────────────────────────────────────────────


def _make_alloc(label, jobs, is_gpu=False, is_arm64=False, gpu_count=0):
    return RunnerAllocation(
        osdc_label=label,
        job_count=jobs,
        source_job_count=0,
        proportion=0.0,
        is_gpu=is_gpu,
        is_arm64=is_arm64,
        gpu_count=gpu_count,
    )


class TestGenerateWorkflow:
    def test_basic_structure(self):
        allocs = [_make_alloc("l-x86iavx512-8-16", 3)]
        result = generate_workflow(allocs, "mt-", "test-cluster")

        assert "name: OSDC Load Test (test-cluster)" in result
        assert "on:" in result
        assert "jobs:" in result

    def test_cpu_x86_job(self):
        allocs = [_make_alloc("l-x86iavx512-8-16", 2)]
        result = generate_workflow(allocs, "mt-", "test")

        assert "load-l-x86iavx512-8-16:" in result
        assert "runs-on: mt-l-x86iavx512-8-16" in result
        assert CPU_CONTAINER in result
        assert "x86_64" in result
        assert "Checkout pytorch" in result
        assert "index: [1, 2]" in result

    def test_arm64_job(self):
        allocs = [_make_alloc("l-arm64g3-16-62", 1, is_arm64=True)]
        result = generate_workflow(allocs, "", "test")

        assert "runs-on: l-arm64g3-16-62" in result
        assert CPU_CONTAINER in result
        assert "aarch64" in result
        assert "Checkout pytorch" in result

    def test_gpu_job(self):
        allocs = [_make_alloc("l-x86iavx512-29-115-t4", 1, is_gpu=True, gpu_count=1)]
        result = generate_workflow(allocs, "", "test")

        assert GPU_CONTAINER in result
        assert "nvidia-smi" in result
        assert "Checkout pytorch" not in result

    def test_gpu_count_verification(self):
        allocs = [_make_alloc("l-x86iavx512-45-172-t4-4", 1, is_gpu=True, gpu_count=4)]
        result = generate_workflow(allocs, "", "test")

        assert ">= 4 GPUs" in result

    def test_empty_prefix(self):
        allocs = [_make_alloc("l-x86iavx512-8-16", 1)]
        result = generate_workflow(allocs, "", "test")

        assert "runs-on: l-x86iavx512-8-16" in result

    def test_zero_job_count_skipped(self):
        allocs = [
            _make_alloc("l-x86iavx512-8-16", 0),
            _make_alloc("l-arm64g3-16-62", 1, is_arm64=True),
        ]
        result = generate_workflow(allocs, "", "test")

        assert "l-x86iavx512-8-16" not in result
        assert "l-arm64g3-16-62" in result

    def test_multiple_jobs(self):
        allocs = [
            _make_alloc("l-x86iavx512-8-16", 3),
            _make_alloc("l-arm64g3-16-62", 2, is_arm64=True),
        ]
        result = generate_workflow(allocs, "mt-", "test")

        assert "load-l-x86iavx512-8-16:" in result
        assert "load-l-arm64g3-16-62:" in result
        assert "index: [1, 2, 3]" in result
        assert "index: [1, 2]" in result

    def test_fail_fast_disabled(self):
        allocs = [_make_alloc("l-x86iavx512-8-16", 1)]
        result = generate_workflow(allocs, "", "test")

        assert "fail-fast: false" in result

    def test_metadata_step(self):
        allocs = [_make_alloc("l-x86iavx512-8-16", 1)]
        result = generate_workflow(allocs, "", "test")

        assert "Report metadata" in result
        assert 'runner_type=l-x86iavx512-8-16' in result

    def test_valid_yaml(self):
        """Generated workflow should be valid YAML."""
        allocs = [
            _make_alloc("l-x86iavx512-8-16", 3),
            _make_alloc("l-arm64g3-16-62", 2, is_arm64=True),
            _make_alloc("l-x86iavx512-29-115-t4", 1, is_gpu=True, gpu_count=1),
        ]
        result = generate_workflow(allocs, "mt-", "test")
        data = yaml.safe_load(result)

        assert data["name"] == "OSDC Load Test (test)"
        assert "jobs" in data
        assert len(data["jobs"]) == 3

    def test_matrix_split_over_256(self):
        allocs = [_make_alloc("l-x86iavx512-8-16", 300)]
        result = generate_workflow(allocs, "", "test")

        # Should produce two job blocks
        assert "load-l-x86iavx512-8-16-part0:" in result
        assert "load-l-x86iavx512-8-16-part1:" in result

        data = yaml.safe_load(result)
        part0 = data["jobs"]["load-l-x86iavx512-8-16-part0"]
        part1 = data["jobs"]["load-l-x86iavx512-8-16-part1"]
        total = (
            len(part0["strategy"]["matrix"]["index"])
            + len(part1["strategy"]["matrix"]["index"])
        )
        assert total == 300
        assert len(part0["strategy"]["matrix"]["index"]) <= MAX_MATRIX_SIZE
