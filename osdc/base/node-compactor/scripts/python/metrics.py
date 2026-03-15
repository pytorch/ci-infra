"""Prometheus metrics for the Node Compactor controller."""

from prometheus_client import Counter, Gauge, Histogram, Info

# Track known label-value tuples per gauge so we can remove stale series
# without touching prometheus_client internals.
_known_labels: dict[str, set[tuple[str, ...]]] = {}


def refresh_gauge(gauge: Gauge, current: dict[tuple[str, ...], float]) -> None:
    """Update a labeled gauge: set current values, remove stale label sets.

    Args:
        gauge: A prometheus_client Gauge with labels.
        current: Mapping of label-value tuples to metric values.
            Keys must match the gauge's label order.
    """
    name = gauge._name
    previous = _known_labels.get(name, set())
    stale = previous - set(current.keys())
    for label_values in stale:
        gauge.remove(*label_values)
    for label_values, value in current.items():
        gauge.labels(*label_values).set(value)
    _known_labels[name] = set(current.keys())


# --- Gauges (current state) ---

managed_nodes = Gauge(
    "node_compactor_managed_nodes",
    "Total nodes under compactor management",
    ["nodepool"],
)

tainted_nodes = Gauge(
    "node_compactor_tainted_nodes",
    "Currently tainted nodes (NoSchedule)",
    ["nodepool"],
)

workload_pods = Gauge(
    "node_compactor_workload_pods",
    "Workload pods on managed nodes",
    ["nodepool"],
)

pending_pods_compatible = Gauge(
    "node_compactor_pending_pods_compatible",
    "Pending pods that could run on tainted nodes (burst pressure indicator)",
)

node_utilization_ratio = Gauge(
    "node_compactor_node_utilization_ratio",
    "Per-node resource utilization (allocated / allocatable)",
    ["node", "nodepool", "resource"],
)

# --- Counters ---

reconcile_cycles_total = Counter(
    "node_compactor_reconcile_cycles_total",
    "Reconciliation cycles",
    ["status"],
)

taint_operations_total = Counter(
    "node_compactor_taint_operations_total",
    "Taint changes",
    ["action", "status"],
)

cooldown_blocks_total = Counter(
    "node_compactor_cooldown_blocks_total",
    "Untaint attempts blocked by cooldown timer",
)

# --- Histogram ---

reconcile_duration_seconds = Histogram(
    "node_compactor_reconcile_duration_seconds",
    "Time per reconciliation cycle",
    buckets=[0.1, 0.25, 0.5, 1, 2.5, 5, 10, 30, 60],
)

# --- Info ---

config_info = Info(
    "node_compactor_config",
    "Current configuration",
)
