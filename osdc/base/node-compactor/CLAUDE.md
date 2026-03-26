# base/node-compactor/ — Node Compactor Controller

Proactively taints underutilized Karpenter-managed nodes with `NoSchedule` so new pods land on denser nodes. Existing pods finish naturally (no eviction). When a tainted node becomes empty, Karpenter's `WhenEmpty` consolidation policy (with `consolidateAfter: 2m`) deletes it.

This achieves cost savings without disrupting running CI jobs.

## What's here

| Path | Purpose |
|------|---------|
| `scripts/python/compactor.py` | Main controller loop |
| `scripts/python/models.py` | Data models and config parsing |
| `scripts/python/discovery.py` | NodePool and node discovery logic |
| `scripts/python/packing.py` | Bin-packing algorithm for node consolidation |
| `scripts/python/taints.py` | Taint management (apply, remove, SIGTERM cleanup) |
| `scripts/python/reservations.py` | Capacity reservation annotation management (do-not-disrupt) |
| `scripts/python/phantom.py` | Phantom load simulation for pending pods |
| `scripts/python/test_compactor.py` | Unit tests for main controller |
| `scripts/python/test_discovery.py` | Unit tests for discovery module |
| `scripts/python/test_taints.py` | Unit tests for taint management |
| `scripts/python/test_models_pod_helpers.py` | Unit tests for pod helper functions |
| `scripts/python/test_phantom.py` | Unit tests for phantom load simulation |
| `scripts/python/test_spare_capacity.py` | Unit tests for spare capacity feature |
| `scripts/python/test_reservations.py` | Unit tests for capacity reservation feature |
| `kubernetes/deployment.yaml` | Deployment manifest (runs in `kube-system`) |
| `kubernetes/rbac.yaml` | RBAC — node get/list/patch, pod list, NodePool list |
| `kubernetes/serviceaccount.yaml` | ServiceAccount |
| `docker/Dockerfile` | Container image (python:3.12.9-slim + lightkube) |
| `docker/pyproject.toml` | Python dependencies |
| `deploy.sh` | Build image, push to Harbor, apply manifests |
| `tests/e2e/` | End-to-end tests against live cluster (`just test-compactor <cluster>`) |

## Configuration

All config comes from `clusters.yaml` under `node_compactor:` and is injected as env vars via placeholder substitution in `deploy.sh`:

| clusters.yaml key | Env var | Default | What it does |
|---|---|---|---|
| `node_compactor.enabled` | — | `true` | Set `false` to skip deployment entirely |
| `node_compactor.interval_seconds` | `COMPACTOR_INTERVAL` | `20` | Seconds between compaction cycles |
| `node_compactor.max_uptime_hours` | `COMPACTOR_MAX_UPTIME_HOURS` | `48` | Prioritize tainting nodes older than this (hours) |
| `node_compactor.dry_run` | `COMPACTOR_DRY_RUN` | `false` | Log what would happen without tainting |
| `node_compactor.min_nodes` | `COMPACTOR_MIN_NODES` | `1` | Minimum nodes to keep untainted per NodePool |
| `node_compactor.min_node_age_seconds` | `COMPACTOR_MIN_NODE_AGE` | `900` | Grace period (seconds) before a new node can be tainted |
| `node_compactor.taint_rate` | `COMPACTOR_TAINT_RATE` | `0.3` | Max fraction of surplus nodes to taint per iteration (`ceil(surplus * rate)`) |
| `node_compactor.fleet_cooldown` | `COMPACTOR_FLEET_COOLDOWN` | `900` | Seconds after a burst untaint before new taints are allowed in the same pool |
| `node_compactor.spare_capacity_nodes` | `COMPACTOR_SPARE_CAPACITY_NODES` | `3` | Minimum low-utilization nodes to keep untainted per pool (floor) |
| `node_compactor.spare_capacity_ratio` | `COMPACTOR_SPARE_CAPACITY_RATIO` | `0.15` | Fraction of pool size to keep as spare capacity (scales with pool) |
| `node_compactor.spare_capacity_threshold` | `COMPACTOR_SPARE_CAPACITY_THRESHOLD` | `0.4` | Max utilization for a node to count as "spare capacity" |
| `node_compactor.capacity_reservation_nodes` | `COMPACTOR_CAPACITY_RESERVATION_NODES` | `0` | Number of young, low-utilization nodes per pool to protect from Karpenter deletion via `do-not-disrupt` annotation |

## How it works with Karpenter

1. NodePools labeled `osdc.io/node-compactor: "true"` are discovered automatically
2. Compactor evaluates utilization of nodes in those pools
3. Underutilized nodes get tainted `NoSchedule` — pods already running are unaffected
4. As pods complete, tainted nodes drain naturally
5. When a tainted node has zero non-DaemonSet pods, Karpenter's `WhenEmpty` policy deletes it after `consolidateAfter` (2 minutes)
6. Optionally, the compactor annotates a configurable number of young, low-utilization nodes with `karpenter.sh/do-not-disrupt` to prevent Karpenter from deleting them even when empty — maintaining ready-to-use capacity

## Safety properties

- Never taints all nodes in a pool — always keeps at least `min_nodes` untainted
- Burst absorption: temporarily removes taints when pending pods match tainted nodes (checks tolerations, nodeSelector, affinity, and resource fit)
- Phantom load: pending pods (30–120s old) are simulated on untainted nodes before utilization decisions, preventing premature tainting
- Rate limiting: caps new taints per iteration to `ceil(surplus * taint_rate)`, preventing taint storms
- Fleet cooldown: blocks new taints for `fleet_cooldown` seconds after a burst untaint, with override at >50% surplus
- Spare capacity floor: keeps at least `max(spare_capacity_nodes, ceil(pool_size * spare_capacity_ratio))` low-utilization nodes untainted
- Graceful shutdown: catches SIGTERM and removes all taints it applied before exiting
- Dry-run mode for safe testing
- Capacity reservation: annotates up to N young, low-utilization nodes per pool with `karpenter.sh/do-not-disrupt` to prevent Karpenter WhenEmpty deletion, maintaining ready-to-use capacity (disabled by default, `capacity_reservation_nodes=0`)

## Debugging

```bash
# Controller logs
kubectl logs -n kube-system deploy/node-compactor -f

# Check taint state on nodes
kubectl get nodes -o custom-columns='NAME:.metadata.name,TAINTS:.spec.taints[*].key'

# Enable dry-run (set in clusters.yaml, redeploy)
node_compactor:
  dry_run: true
```

## Dependencies

- Requires Karpenter with NodePools that have the label `osdc.io/node-compactor: "true"` (set by `modules/nodepools` when enabled in clusters.yaml)
- Runs on base infrastructure nodes (tolerates `CriticalAddonsOnly` taint)
