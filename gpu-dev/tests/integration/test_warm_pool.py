"""Integration: warm pool on staging (t4 + cpu).

Requires WARM_POOL_TARGETS to be deployed on the target env (lambda.tf, workspace
"test" → t4 + cpu). If a reservation comes up cold (no warm pool yet), the warm
tests skip with a clear reason.

1. A warm-eligible reservation is claimed FAST and takes a pre-booted warm pod
   (`warm_claimed=True` on the record).
2. A custom-image reservation is warm-INELIGIBLE → does not consume a warm pod and
   still comes up cleanly. (The eviction-under-pressure logic — "cleanly kill the
   minimum warm pods to free a node" — is unit-tested in
   tests/unit/lambda_fn/test_warm_pool.py::_evict_warm_for_capacity.)
"""
import os
import time

import pytest

from .conftest import reserved, read_reservation

pytestmark = pytest.mark.integration

WARM_TYPE = os.environ.get("GPU_DEV_WARM_TYPE", "t4")           # warm type on staging
WARM_GPUS = int(os.environ.get("GPU_DEV_WARM_GPUS", "1"))


def test_warm_claim_is_fast_and_takes_a_warm_pod(manager):
    t0 = time.time()
    with reserved(manager, gpu_type=WARM_TYPE, gpu_count=WARM_GPUS, hours=0.5) as (rid, conn):
        elapsed = time.time() - t0
        rec = read_reservation(manager, rid)
        if not rec.get("warm_claimed"):
            pytest.skip(
                f"{WARM_TYPE} came up cold (no warm pool here yet) — deploy "
                f"WARM_POOL_TARGETS on staging (lambda.tf, workspace 'test') + tf apply")
        assert rec["warm_claimed"] is True
        assert conn.get("ssh_command"), f"warm pod {rid} has no ssh_command"
        # Warm claim is near-instant; a cold reserve is minutes. Generous ceiling.
        assert elapsed < 120, f"warm claim took {elapsed:.0f}s — not fast"


def test_custom_image_does_not_take_a_warm_pod(manager):
    image = os.environ.get("GPU_DEV_TEST_IMAGE")
    if not image:
        pytest.skip(
            "set GPU_DEV_TEST_IMAGE to a staging-pullable image to test the "
            "custom-image (warm-ineligible) path")
    with reserved(manager, gpu_type=WARM_TYPE, gpu_count=WARM_GPUS, hours=0.5,
                  dockerimage=image) as (rid, conn):
        rec = read_reservation(manager, rid)
        assert rec.get("warm_claimed") is not True, \
            "a custom image must NOT claim a warm pod (warm-ineligible)"
        assert conn.get("ssh_command"), \
            "custom-image reservation should still come up active (clean cold path)"
