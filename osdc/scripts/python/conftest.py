"""Shared pytest fixtures and helpers for scripts/python/ test suite."""

import pytest
from runner_overhead import RunnerPodOverhead, load_runner_pod_overhead


@pytest.fixture(autouse=True)
def _clear_overhead_cache():
    """Clear load_runner_pod_overhead lru_cache before and after each test.

    The loader is memoized via ``functools.lru_cache``, so cached results
    from one test would otherwise leak into the next and mask staleness
    bugs. Autouse so every test in scripts/python/ inherits the cleanup
    without having to wire it up locally.
    """
    load_runner_pod_overhead.cache_clear()
    yield
    load_runner_pod_overhead.cache_clear()


def make_test_overhead(
    runner_cpu_m: int = 320,
    runner_mem_mi: int = 522,
    listener_cpu_m: int = 100,
    listener_mem_mi: int = 128,
    workflow_extra_cpu_m: int = 0,
    workflow_extra_mem_mi: int = 0,
) -> RunnerPodOverhead:
    """Build a RunnerPodOverhead for tests.

    Defaults match the OLD hardcoded HOOKS_OVERHEAD_* values so existing
    assertions don't have to change their expected numbers.
    """
    return RunnerPodOverhead(
        runner_cpu_m=runner_cpu_m,
        runner_mem_mi=runner_mem_mi,
        listener_cpu_m=listener_cpu_m,
        listener_mem_mi=listener_mem_mi,
        workflow_extra_cpu_m=workflow_extra_cpu_m,
        workflow_extra_mem_mi=workflow_extra_mem_mi,
    )
