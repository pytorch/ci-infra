# scripts/python/

Utility scripts for capacity planning, cluster simulation, and infrastructure management.

## Node Utilization Analysis

**`analyze_node_utilization.py`** — Evaluates runner-to-node packing efficiency.

For each node type (instance type), computes all valid runner combinations that fit after subtracting kubelet reserved resources, DaemonSet overhead, and runner sidecar costs. Flags combinations where resource utilization falls below a configurable threshold.

```bash
uv run scripts/python/analyze_node_utilization.py

# Options:
#   --threshold 90     Utilization % below which combos are flagged (default: 90)
#   --show-daemonsets   Print discovered DaemonSets and their resource overhead, then exit
```

Reads runner definitions from `modules/arc-runners/defs/*.yaml` and nodepool definitions from `modules/nodepools/defs/*.yaml`. When run from a consumer repo, also discovers definitions under the consumer's `modules/` directory.

## Monte Carlo Cluster Simulation

**`simulate_cluster_cli.py`** — Simulates placing PyTorch CI peak workload onto nodes.

Uses historical peak concurrent runner counts (from `pytorch_workload_data.py`) and weighted random draws with best-fit bin packing to estimate how many nodes of each type a cluster needs. Reports per-node utilization, deployment accuracy vs. targets, and cluster-wide resource efficiency.

```bash
uv run scripts/python/simulate_cluster_cli.py

# Options:
#   --seed 42          Random seed (default: 42)
#   --threshold 0.15   Weighted MAPE threshold to stop placing runners (default: 0.15)
#   --rounds 10        Run N rounds with different seeds, print percentile summary
#   --upstream-dir     Override upstream osdc/ path
#   --consumer-root    Override consumer osdc/ path
```

The simulation library lives in **`simulate_cluster.py`** (imported by the CLI).

## Supporting Modules

| File | Purpose |
|------|---------|
| `daemonset_overhead.py` | Discovers DaemonSets from Kubernetes manifests and computes their CPU/memory overhead per node. Also usable standalone: `uv run scripts/python/daemonset_overhead.py` |
| `pytorch_workload_data.py` | Static snapshot of PyTorch CI workload data: old-to-new runner label mapping and peak concurrent runner counts (sourced from `docs/`) |
| `cli_colors.py` | Shared ANSI color constants for terminal output |
| `configure_harbor_projects.py` | Configures Harbor proxy cache projects via API (used by base deploy) |

## Tests

Tests are co-located and run via `just test`:

| Test File | Covers |
|-----------|--------|
| `test_simulate_cluster.py` | `simulate_cluster.py` |
| `test_daemonset_overhead.py` | `daemonset_overhead.py` |
| `test_configure_harbor_projects.py` | `configure_harbor_projects.py` |
| `test_smoke_helpers.py` | Smoke test helper utilities |
