"""Integration: reproduce a KNOWN main-branch failure on a real pod.

Point at a specific commit + test that is known-red on trunk and assert it FAILS
here too (i.e. the failure reproduces). Parametrized so it works for any failure:

    GPU_DEV_REPRO_REF   a main commit sha (or pr/N) that is failing on HUD
    GPU_DEV_REPRO_TEST  the test args, e.g. "test/inductor/test_x.py Class.test_y"
    GPU_DEV_REPRO_GPU   gpu type for the box (default t4)
    GPU_DEV_REPRO_GPUS  gpu count (default 1)

Find a current known failure with the **treehugger MCP** (get_hud_data /
master_commit_red → a red commit + its failing test) and set the two env vars.

Caveat: the prebuilt PyTorch is h100/b200-arch, so a CUDA test that needs torch
will need a full build on t4 (slow) — prefer a failure that runs on the box's GPU
(or a cpu-runnable test). Skipped unless REF+TEST are set; SSH step skips if the
runner can't reach the pod.
"""
import os
import shlex

import pytest

from .conftest import reserved, exec_or_skip

pytestmark = pytest.mark.integration

REF = os.environ.get("GPU_DEV_REPRO_REF")
TEST = os.environ.get("GPU_DEV_REPRO_TEST")
GPU = os.environ.get("GPU_DEV_REPRO_GPU", "t4")
GPUS = int(os.environ.get("GPU_DEV_REPRO_GPUS", "1"))


@pytest.mark.skipif(
    not (REF and TEST),
    reason="set GPU_DEV_REPRO_REF + GPU_DEV_REPRO_TEST to a known-failing (commit, test) "
           "— find one via the treehugger MCP (master_commit_red / get_hud_data)")
def test_known_failure_reproduces(manager):
    with reserved(manager, gpu_type=GPU, gpu_count=GPUS, hours=1.0) as (rid, conn):
        remote = (
            "cd ~/pytorch && "
            f"git fetch origin {shlex.quote(REF)} 2>/dev/null && git checkout -f FETCH_HEAD 2>/dev/null "
            f"|| git checkout -f {shlex.quote(REF)}; "
            "echo HEAD=$(git rev-parse --short HEAD 2>/dev/null); "
            f"python {TEST}; echo REPRO_EXIT=$?"
        )
        rc, out = exec_or_skip(conn, remote, timeout=1800)
        markers = [l for l in out.splitlines() if l.startswith("REPRO_EXIT=")]
        assert markers, f"no REPRO_EXIT marker — checkout/run never completed:\n{out[-2000:]}"
        exit_code = int(markers[-1].split("=", 1)[1])
        # The known failure must REPRODUCE: the test should FAIL (non-zero exit).
        assert exit_code != 0, (
            f"expected {TEST} @ {REF} to FAIL (reproduce the known failure) but it "
            f"passed:\n{out[-2000:]}")
