"""Unit tests for the metrics module (refresh_gauge helper)."""

from prometheus_client import CollectorRegistry, Gauge

from metrics import _known_labels, refresh_gauge


def _make_gauge(name: str, labels: list[str], registry: CollectorRegistry) -> Gauge:
    """Create a Gauge with an isolated registry to avoid cross-test pollution."""
    return Gauge(name, "test gauge", labels, registry=registry)


def _get_sample_value(
    registry: CollectorRegistry, metric_name: str, labels: dict[str, str]
) -> float | None:
    """Read a sample value from a registry by metric name and label dict."""
    for metric in registry.collect():
        for sample in metric.samples:
            if sample.name == metric_name and sample.labels == labels:
                return sample.value
    return None


def _collect_label_sets(
    registry: CollectorRegistry, metric_name: str
) -> set[tuple[str, ...]]:
    """Collect all label-value tuples present for a metric."""
    result: set[tuple[str, ...]] = set()
    for metric in registry.collect():
        for sample in metric.samples:
            if sample.name == metric_name:
                result.add(tuple(sample.labels.values()))
    return result


class TestRefreshGauge:
    """Tests for refresh_gauge()."""

    def setup_method(self):
        """Fresh registry and clean _known_labels for each test."""
        self.registry = CollectorRegistry()
        _known_labels.clear()

    # --- Core behavior ---

    def test_sets_values_for_current_labels(self):
        g = _make_gauge("test_set", ["pool"], self.registry)
        refresh_gauge(g, {("pool-a",): 5.0, ("pool-b",): 3.0})

        assert _get_sample_value(self.registry, "test_set", {"pool": "pool-a"}) == 5.0
        assert _get_sample_value(self.registry, "test_set", {"pool": "pool-b"}) == 3.0

    def test_removes_stale_labels(self):
        g = _make_gauge("test_stale", ["pool"], self.registry)

        # First call: two pools
        refresh_gauge(g, {("pool-a",): 1.0, ("pool-b",): 2.0})
        assert _collect_label_sets(self.registry, "test_stale") == {
            ("pool-a",), ("pool-b",),
        }

        # Second call: only pool-a remains
        refresh_gauge(g, {("pool-a",): 10.0})
        assert _collect_label_sets(self.registry, "test_stale") == {("pool-a",)}
        assert _get_sample_value(self.registry, "test_stale", {"pool": "pool-a"}) == 10.0

    def test_empty_dict_removes_all_labels(self):
        g = _make_gauge("test_empty", ["pool"], self.registry)

        refresh_gauge(g, {("pool-a",): 1.0, ("pool-b",): 2.0})
        assert len(_collect_label_sets(self.registry, "test_empty")) == 2

        refresh_gauge(g, {})
        assert _collect_label_sets(self.registry, "test_empty") == set()

    def test_first_call_no_prior_state(self):
        """First call with no prior _known_labels entry works correctly."""
        g = _make_gauge("test_first", ["pool"], self.registry)

        refresh_gauge(g, {("pool-x",): 42.0})
        assert _get_sample_value(self.registry, "test_first", {"pool": "pool-x"}) == 42.0
        assert _known_labels["test_first"] == {("pool-x",)}

    def test_idempotent_repeated_calls(self):
        """Calling refresh_gauge with the same data twice is a no-op."""
        g = _make_gauge("test_idem", ["pool"], self.registry)
        data = {("pool-a",): 7.0}

        refresh_gauge(g, data)
        refresh_gauge(g, data)

        assert _get_sample_value(self.registry, "test_idem", {"pool": "pool-a"}) == 7.0
        assert _known_labels["test_idem"] == {("pool-a",)}

    # --- Multi-label gauges ---

    def test_multi_label_gauge(self):
        """Works with gauges that have multiple labels."""
        g = _make_gauge("test_multi", ["node", "pool", "resource"], self.registry)

        refresh_gauge(g, {
            ("node-1", "pool-a", "cpu"): 0.75,
            ("node-1", "pool-a", "memory"): 0.50,
        })

        assert _get_sample_value(
            self.registry, "test_multi",
            {"node": "node-1", "pool": "pool-a", "resource": "cpu"},
        ) == 0.75
        assert _get_sample_value(
            self.registry, "test_multi",
            {"node": "node-1", "pool": "pool-a", "resource": "memory"},
        ) == 0.50

    def test_multi_label_stale_removal(self):
        """Stale removal works correctly with multi-label gauges."""
        g = _make_gauge("test_multi_stale", ["node", "pool", "resource"], self.registry)

        refresh_gauge(g, {
            ("node-1", "pool-a", "cpu"): 0.5,
            ("node-2", "pool-b", "cpu"): 0.3,
        })

        # node-2 disappears
        refresh_gauge(g, {("node-1", "pool-a", "cpu"): 0.6})

        labels = _collect_label_sets(self.registry, "test_multi_stale")
        assert labels == {("node-1", "pool-a", "cpu")}

    # --- Edge cases ---

    def test_empty_to_populated_transition(self):
        """Going from empty to populated works (reverse of clearing)."""
        g = _make_gauge("test_e2p", ["pool"], self.registry)

        refresh_gauge(g, {})
        assert _collect_label_sets(self.registry, "test_e2p") == set()

        refresh_gauge(g, {("pool-new",): 99.0})
        assert _get_sample_value(self.registry, "test_e2p", {"pool": "pool-new"}) == 99.0

    def test_value_update_without_label_change(self):
        """Values update correctly when label sets don't change."""
        g = _make_gauge("test_update", ["pool"], self.registry)

        refresh_gauge(g, {("pool-a",): 1.0})
        assert _get_sample_value(self.registry, "test_update", {"pool": "pool-a"}) == 1.0

        refresh_gauge(g, {("pool-a",): 999.0})
        assert _get_sample_value(self.registry, "test_update", {"pool": "pool-a"}) == 999.0

    def test_disjoint_label_sets_across_calls(self):
        """Completely different label sets between calls: old removed, new added."""
        g = _make_gauge("test_disjoint", ["pool"], self.registry)

        refresh_gauge(g, {("pool-a",): 1.0, ("pool-b",): 2.0})
        refresh_gauge(g, {("pool-c",): 3.0, ("pool-d",): 4.0})

        labels = _collect_label_sets(self.registry, "test_disjoint")
        assert labels == {("pool-c",), ("pool-d",)}
        assert _get_sample_value(self.registry, "test_disjoint", {"pool": "pool-a"}) is None

    def test_known_labels_tracking(self):
        """_known_labels dict is updated correctly across calls."""
        g = _make_gauge("test_tracking", ["pool"], self.registry)

        assert "test_tracking" not in _known_labels

        refresh_gauge(g, {("a",): 1.0})
        assert _known_labels["test_tracking"] == {("a",)}

        refresh_gauge(g, {("a",): 1.0, ("b",): 2.0})
        assert _known_labels["test_tracking"] == {("a",), ("b",)}

        refresh_gauge(g, {})
        assert _known_labels["test_tracking"] == set()
