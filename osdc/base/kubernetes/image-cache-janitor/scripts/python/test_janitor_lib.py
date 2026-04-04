"""Unit tests for janitor_lib — extracted testable functions from janitor.py."""

import json

from janitor_lib import (
    ImageInfo,
    MetricsServer,
    calculate_total_cache_size,
    parse_crictl_images,
    select_images_to_remove,
)

GI = 1024**3


# ============================================================================
# Helper: build crictl images JSON
# ============================================================================


def _make_crictl_json(images: list[dict]) -> str:
    """Build a crictl images -o json string from a list of image dicts."""
    return json.dumps({"images": images})


def _make_image(
    image_id: str = "sha256:abc123",
    repo_tags: list[str] | None = None,
    repo_digests: list[str] | None = None,
    size: int = 100 * 1024 * 1024,
    pinned: bool = False,
) -> dict:
    """Build a single image entry for crictl JSON."""
    return {
        "id": image_id,
        "repoTags": repo_tags or [],
        "repoDigests": repo_digests or [],
        "size": str(size),
        "pinned": pinned,
    }


# ============================================================================
# parse_crictl_images tests
# ============================================================================


class TestParseCrictlImages:
    """Tests for parse_crictl_images() — crictl JSON parsing."""

    def test_standard_output(self):
        data = _make_crictl_json(
            [
                _make_image("sha256:aaa", ["nginx:latest"], size=50_000_000),
                _make_image("sha256:bbb", ["python:3.12"], size=200_000_000),
            ]
        )
        images = parse_crictl_images(data)
        assert len(images) == 2
        assert images[0].id == "sha256:aaa"
        assert images[0].repo_tags == ["nginx:latest"]
        assert images[0].size == 50_000_000
        assert images[1].id == "sha256:bbb"
        assert images[1].size == 200_000_000

    def test_empty_list(self):
        data = _make_crictl_json([])
        images = parse_crictl_images(data)
        assert images == []

    def test_missing_fields_use_defaults(self):
        data = json.dumps({"images": [{"id": "sha256:minimal"}]})
        images = parse_crictl_images(data)
        assert len(images) == 1
        assert images[0].id == "sha256:minimal"
        assert images[0].repo_tags == []
        assert images[0].repo_digests == []
        assert images[0].size == 0
        assert images[0].pinned is False

    def test_size_as_string(self):
        """crictl may return size as a string — must be parsed to int."""
        data = _make_crictl_json([_make_image(size=999_999)])
        images = parse_crictl_images(data)
        assert images[0].size == 999_999

    def test_size_as_int(self):
        """crictl may also return size as an integer."""
        data = json.dumps({"images": [{"id": "sha256:x", "size": 42}]})
        images = parse_crictl_images(data)
        assert images[0].size == 42

    def test_pinned_image(self):
        data = _make_crictl_json([_make_image(pinned=True)])
        images = parse_crictl_images(data)
        assert images[0].pinned is True

    def test_null_repo_tags(self):
        """repoTags can be null in crictl output."""
        data = json.dumps({"images": [{"id": "sha256:x", "repoTags": None}]})
        images = parse_crictl_images(data)
        assert images[0].repo_tags == []

    def test_repo_digests_preserved(self):
        digests = ["sha256:deadbeef"]
        data = _make_crictl_json(
            [
                _make_image(repo_digests=digests),
            ]
        )
        images = parse_crictl_images(data)
        assert images[0].repo_digests == digests


# ============================================================================
# calculate_total_cache_size tests
# ============================================================================


class TestCalculateTotalCacheSize:
    """Tests for calculate_total_cache_size() — sum image sizes."""

    def test_empty(self):
        assert calculate_total_cache_size([]) == 0

    def test_single_image(self):
        images = [ImageInfo(id="a", size=100 * GI)]
        assert calculate_total_cache_size(images) == 100 * GI

    def test_multiple_images(self):
        images = [
            ImageInfo(id="a", size=100 * GI),
            ImageInfo(id="b", size=200 * GI),
            ImageInfo(id="c", size=50 * GI),
        ]
        assert calculate_total_cache_size(images) == 350 * GI


# ============================================================================
# select_images_to_remove tests
# ============================================================================


class TestSelectImagesToRemove:
    """Tests for select_images_to_remove() — eviction selection."""

    def test_under_limit_returns_empty(self):
        images = [ImageInfo(id="a", size=100 * GI)]
        result = select_images_to_remove(images, 100 * GI, 500 * GI, 400 * GI)
        assert result == []

    def test_at_limit_returns_empty(self):
        images = [ImageInfo(id="a", size=500 * GI)]
        result = select_images_to_remove(images, 500 * GI, 500 * GI, 400 * GI)
        assert result == []

    def test_over_limit_removes_largest_first(self):
        images = [
            ImageInfo(id="small", size=50 * GI),
            ImageInfo(id="large", size=200 * GI),
            ImageInfo(id="medium", size=100 * GI),
        ]
        total = 600 * GI  # over 500 GiB limit
        result = select_images_to_remove(images, total, 500 * GI, 400 * GI)
        # Should pick largest first
        assert result[0].id == "large"

    def test_stops_at_target(self):
        """Should stop removing once projected size is at or below target."""
        images = [
            ImageInfo(id="a", size=100 * GI),
            ImageInfo(id="b", size=80 * GI),
            ImageInfo(id="c", size=60 * GI),
            ImageInfo(id="d", size=40 * GI),
        ]
        total = 550 * GI  # over 500 limit, target 400
        result = select_images_to_remove(images, total, 500 * GI, 400 * GI)
        # Need to remove 150 GiB to get from 550 to 400
        # Sorted by size: a(100), b(80), c(60), d(40)
        # After a: 450 > 400, continue
        # After b: 370 <= 400, stop
        assert len(result) == 2
        assert result[0].id == "a"
        assert result[1].id == "b"

    def test_skips_pinned(self):
        images = [
            ImageInfo(id="pinned", size=300 * GI, pinned=True),
            ImageInfo(id="normal", size=100 * GI),
        ]
        total = 600 * GI
        result = select_images_to_remove(images, total, 500 * GI, 400 * GI)
        assert all(img.id != "pinned" for img in result)
        assert any(img.id == "normal" for img in result)

    def test_all_pinned_returns_empty(self):
        images = [
            ImageInfo(id="a", size=300 * GI, pinned=True),
            ImageInfo(id="b", size=300 * GI, pinned=True),
        ]
        total = 600 * GI
        result = select_images_to_remove(images, total, 500 * GI, 400 * GI)
        assert result == []

    def test_cannot_reach_target(self):
        """When removable images can't bring cache below target, return all removable."""
        images = [
            ImageInfo(id="a", size=50 * GI),
            ImageInfo(id="b", size=50 * GI),
            ImageInfo(id="c", size=300 * GI, pinned=True),
        ]
        total = 600 * GI  # over 500 GI limit
        # Removing a+b = 100 GI, projected = 500 GI, still above 400 GI target
        result = select_images_to_remove(images, total, 500 * GI, 400 * GI)
        assert len(result) == 2
        assert {img.id for img in result} == {"a", "b"}


# ============================================================================
# MetricsServer tests
# ============================================================================


class TestMetricsServerSet:
    """Tests for MetricsServer.set() — gauge values."""

    def test_set_value(self):
        m = MetricsServer()
        m.set("image_cache_janitor_cache_size_bytes", 42.0)
        output = m.format()
        assert "image_cache_janitor_cache_size_bytes 42.0" in output

    def test_set_overwrites_previous(self):
        m = MetricsServer()
        m.set("image_cache_janitor_cache_size_bytes", 1.0)
        m.set("image_cache_janitor_cache_size_bytes", 99.0)
        output = m.format()
        lines = [line for line in output.splitlines() if line.startswith("image_cache_janitor_cache_size_bytes ")]
        assert len(lines) == 1
        assert lines[0].endswith("99.0")


class TestMetricsServerInc:
    """Tests for MetricsServer.inc() — counter values."""

    def test_inc_from_zero(self):
        m = MetricsServer()
        m.inc("image_cache_janitor_gc_cycles_total")
        output = m.format()
        assert "image_cache_janitor_gc_cycles_total 1.0" in output

    def test_inc_accumulates(self):
        m = MetricsServer()
        m.inc("image_cache_janitor_gc_evictions_total")
        m.inc("image_cache_janitor_gc_evictions_total")
        m.inc("image_cache_janitor_gc_evictions_total")
        output = m.format()
        assert "image_cache_janitor_gc_evictions_total 3.0" in output

    def test_inc_custom_delta(self):
        m = MetricsServer()
        m.inc("image_cache_janitor_gc_evicted_bytes_total", 5.0)
        output = m.format()
        assert "image_cache_janitor_gc_evicted_bytes_total 5.0" in output


class TestMetricsServerFormat:
    """Tests for Prometheus text exposition format output."""

    def test_help_and_type_lines(self):
        m = MetricsServer()
        output = m.format()
        assert "# HELP image_cache_janitor_cache_size_bytes Total size of container image cache in bytes" in output
        assert "# TYPE image_cache_janitor_cache_size_bytes gauge" in output

    def test_all_defined_metrics_present(self):
        m = MetricsServer()
        output = m.format()
        assert "image_cache_janitor_cache_size_bytes" in output
        assert "image_cache_janitor_cache_image_count" in output
        assert "image_cache_janitor_gc_cycles_total" in output
        assert "image_cache_janitor_gc_evictions_total" in output
        assert "image_cache_janitor_gc_eviction_errors_total" in output
        assert "image_cache_janitor_gc_evicted_bytes_total" in output
        assert "image_cache_janitor_last_cycle_timestamp" in output
        assert "image_cache_janitor_cache_limit_bytes" in output
        assert "image_cache_janitor_cache_target_bytes" in output

    def test_trailing_newline(self):
        m = MetricsServer()
        output = m.format()
        assert output.endswith("\n")
