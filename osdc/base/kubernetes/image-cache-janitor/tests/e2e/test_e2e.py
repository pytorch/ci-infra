"""End-to-end tests for the image-cache-janitor DaemonSet.

Run with ``pytest -x`` (stop on first failure) — tests are sequential and
build on shared cluster state.
"""

from __future__ import annotations

import logging

import pytest
from conftest import EVICTION_CONFIG, _log_stash_key
from helpers import (
    TEST_PULL_IMAGES,
    fetch_metrics,
    get_cached_images,
    get_janitor_logs,
    get_janitor_pod_on_node,
    image_ids_on_node,
    parse_eviction_sizes,
    parse_metric_value,
    patch_janitor_env,
    pull_image,
    search_logs,
    wait_for,
    wait_for_janitor_pod,
    wait_for_janitor_rollout,
)
from lightkube import Client

log = logging.getLogger("e2e")

# Timeouts
GC_CYCLE_TIMEOUT = 60  # wait for a single GC cycle
ROLLOUT_TIMEOUT = 180  # DaemonSet rolling update


class TestImageCacheJanitor:
    """Sequential e2e tests for the image-cache-janitor.

    The session fixture ``janitor_setup`` configures the DaemonSet with a high
    limit (no eviction) and fast cycle interval.  Individual tests reconfigure
    as needed.  Teardown restores production values.
    """

    @pytest.fixture(autouse=True)
    def _inject(
        self,
        client: Client,
        target_node: str,
        target_nodepool: tuple[str, str],
        test_namespace: str,
        request: pytest.FixtureRequest,
    ) -> None:
        self.client = client
        self.node = target_node
        self.pool, self.itype = target_nodepool
        self.ns = test_namespace
        # Resolve current janitor pod (may have changed due to rollout)
        pod = get_janitor_pod_on_node(client, target_node)
        if pod is None:
            pod = wait_for_janitor_pod(client, target_node, timeout_s=120)
        self.pod = pod
        # Keep log-dump hook up to date
        request.session.stash[_log_stash_key] = pod

    # ── helpers ──────────────────────────────────────────────────────────

    def _diagnostics(self) -> str:
        """Build a diagnostic string for timeout messages."""
        parts = [f"Node: {self.node}", f"Pod:  {self.pod}"]
        try:
            images = get_cached_images(self.pod)
            total = sum(i.size for i in images)
            parts.append(f"Cache: {total / (1024**3):.2f} GiB ({len(images)} images)")
            for img in sorted(images, key=lambda i: i.size, reverse=True)[:10]:
                tag = img.tags[0] if img.tags else img.id[:12]
                parts.append(f"  {tag:60s} {img.size / (1024**2):8.1f} MiB  {'pinned' if img.pinned else ''}")
        except Exception as exc:
            parts.append(f"(failed to list images: {exc})")
        try:
            recent = get_janitor_logs(self.pod)[-20:]
            parts.append("Recent logs:")
            parts.extend(f"  {line}" for line in recent)
        except Exception:
            log.debug("Could not fetch janitor logs for diagnostics")
        return "\n".join(parts)

    def _wait_for_gc_cycle(self, pattern: str = "Image cache:") -> None:
        """Wait until the janitor logs a line matching *pattern*."""
        wait_for(
            f"GC cycle ({pattern})",
            lambda: bool(search_logs(get_janitor_logs(self.pod), pattern)),
            timeout_s=GC_CYCLE_TIMEOUT,
            poll_s=5,
            on_timeout=self._diagnostics,
        )

    # ── Group A: no-eviction baseline ────────────────────────────────────

    def test_seed_and_no_eviction(self) -> None:
        """Pull test images and verify no eviction under a high limit."""
        for img_ref in TEST_PULL_IMAGES:
            pull_image(self.pod, img_ref)

        # Wait for a cycle that confirms "within limit"
        self._wait_for_gc_cycle("within limit")

        # Metrics must show zero evictions
        metrics = fetch_metrics(self.pod)
        evictions = parse_metric_value(
            metrics,
            "image_cache_janitor_gc_evictions_total",
        )
        assert evictions == 0.0, f"Expected 0 evictions, got {evictions}"

        # All test images should still be cached
        cached_tags = {tag for img in get_cached_images(self.pod) for tag in img.tags}
        for img_ref in TEST_PULL_IMAGES:
            assert img_ref in cached_tags, f"{img_ref} not found in cache after seeding"

    # ── Group B: eviction ────────────────────────────────────────────────

    def test_eviction_triggers(self) -> None:
        """Reconfigure to limit=0 and verify eviction happens."""
        pre_ids = image_ids_on_node(self.pod)
        assert pre_ids, "Expected non-empty image cache before eviction"

        # Reconfigure DaemonSet → limit=0, target=0
        old_pod = self.pod
        patch_janitor_env(self.client, EVICTION_CONFIG)
        self.pod = wait_for_janitor_rollout(
            self.client,
            self.node,
            old_pod,
            timeout_s=ROLLOUT_TIMEOUT,
        )

        # Wait for at least one removal log entry
        wait_for(
            "eviction to occur",
            lambda: bool(search_logs(get_janitor_logs(self.pod), "Removing:")),
            timeout_s=GC_CYCLE_TIMEOUT * 2,
            poll_s=5,
            on_timeout=self._diagnostics,
        )

        post_ids = image_ids_on_node(self.pod)
        removed = pre_ids - post_ids
        log.info(
            "Eviction complete: %d removed, %d remaining",
            len(removed),
            len(post_ids),
        )
        assert removed, (
            f"Expected some images to be evicted but none were removed. Pre: {len(pre_ids)}, Post: {len(post_ids)}"
        )

    def test_eviction_largest_first(self) -> None:
        """Verify the first eviction cycle removed images largest-first."""
        logs = get_janitor_logs(self.pod)
        sizes = parse_eviction_sizes(logs)
        assert sizes, "No eviction size entries found in logs"
        assert sizes == sorted(sizes, reverse=True), f"Eviction order is not largest-first: {sizes}"

    def test_in_use_images_survive(self) -> None:
        """Pinned and system images survive aggressive eviction."""
        # After limit=0 eviction, pinned images (system-critical) remain.
        # On containerd 2.x, crictl rmi may succeed even for in-use images
        # (layers stay referenced by running containers), so we don't assert
        # on gc_eviction_errors_total — that's runtime-version-dependent.
        cached = get_cached_images(self.pod)
        assert cached, "Image cache is completely empty — even pinned images are gone"

        # Log eviction errors for observability
        metrics = fetch_metrics(self.pod)
        errors = parse_metric_value(
            metrics,
            "image_cache_janitor_gc_eviction_errors_total",
        )
        log.info("Eviction errors (in-use refusals): %.0f", errors)

    def test_metrics_reflect_eviction(self) -> None:
        """Prometheus metrics must reflect the eviction that occurred."""
        metrics = fetch_metrics(self.pod)

        evictions = parse_metric_value(
            metrics,
            "image_cache_janitor_gc_evictions_total",
        )
        assert evictions > 0, f"gc_evictions_total should be >0, got {evictions}"

        evicted_bytes = parse_metric_value(
            metrics,
            "image_cache_janitor_gc_evicted_bytes_total",
        )
        assert evicted_bytes > 0, f"gc_evicted_bytes_total should be >0, got {evicted_bytes}"

        cycles = parse_metric_value(
            metrics,
            "image_cache_janitor_gc_cycles_total",
        )
        assert cycles >= 1, f"gc_cycles_total should be >=1, got {cycles}"

        cache_bytes = parse_metric_value(
            metrics,
            "image_cache_janitor_cache_size_bytes",
        )
        log.info(
            "Post-eviction metrics: evictions=%.0f, bytes_freed=%.0f, cycles=%.0f, cache_size=%.0f",
            evictions,
            evicted_bytes,
            cycles,
            cache_bytes,
        )
