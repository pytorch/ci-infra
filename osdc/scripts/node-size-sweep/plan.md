# Plan: real CPU + memory bin-packing in the sweep sim

## Goal

Replace `frac_n` (1D "fraction of biggest instance") with real 2D bin-packing on
`(cpu_millicores, memory_mib)` against per-node allocatable, computed properly
as `capacity − kubelet_reserved − daemonset_overhead`. Same for placement,
same for the util metric — matching prod's `node_compactor_node_utilization_ratio =
total_pod_requests / allocatable`.

## What changes

### A. Data-side (build_csv.py)

**No CSV schema change.** The existing CSV already carries `label`, which is
enough — `build_label_table()`'s derivation logic (label → def → vcpu, memory,
instance_type, gpu, fleet) is what the sim needs, not a wider CSV.

The stale `nodepool_fraction` column stays in the CSV but is ignored by the
sim. No re-extraction needed; the existing 200MB `pytorch_60d.csv` keeps
working.

`build_csv.py` itself doesn't need changes for this pass. (Future cleanup:
drop `nodepool_fraction` since nothing reads it anymore.)

### B. Sim-side (simulate.py)

**New instance-model table** at the top of `simulate.py` (or imported from
`instance_specs.py`):
- Per instance type: `vcpu`, `memory_mib`, `gpu`.
- Per fleet name (as it appears in CSV): list of instance types Karpenter can
  pick from (with weights, or in Karpenter's cheapest-first order).
- Cache `allocatable_cpu_m`, `allocatable_mem_mi`, `allocatable_gpu` per
  (fleet, instance_type) after subtracting:
  - Kubelet reserved: `analyze_node_utilization.kubelet_reserved(vcpu, mem_gib, max_pods)`.
    Need `ENI_MAX_PODS` from `instance_specs.py` for `max_pods`.
  - DaemonSet overhead: `daemonset_overhead.discover_daemonsets()` → filter
    `gpu_only` by whether the fleet's instance is GPU. Sum cpu_m and mem_mib.
  - Runner-hooks-warmer DaemonSet on the c7i-runner fleet only (already picked
    up by the discovery walk if we point at the right root).

Fleet-instance selection strategy for the sim (Karpenter emulation):
- For each pending pod, iterate instance types in the fleet's weighted order
  (biggest weight first — that's how the current defs are configured).
  Pick the first instance where `allocatable >= pod_request`.
- On placement, that instance's allocatable becomes the node's capacity.
- This is CLOSER to Karpenter's real behavior (which is
  `price-capacity-optimized`) than "always biggest" — but not exact. The
  simplification: we treat weight order as instance-selection order and
  don't model price at all. Good enough for a first pass; can refine later.

**Node dataclass changes**:
- Remove: `used: float`
- Add: `cpu_used_m: int`, `mem_used_mi: int`, `gpu_used: int`
- Add: `cpu_allocatable_m: int`, `mem_allocatable_mi: int`, `gpu_allocatable: int`
- Add: `instance_type: str` (for reporting + selecting overhead)

`daemonset_frac` field stays gone.

**Job dataclass changes**:
- Remove: `frac_n: int`
- Add: `cpu_m: int`, `mem_mi: int`, `gpu: int` (workflow pod requests)
- Add: `runner_cpu_m: int`, `runner_mem_mi: int` (runner pod requests, same
  every row, but easier to keep with the job than as globals)

`load_jobs` now reads the new columns. Runner pod becomes a synthesized paired
Job on the c7i-runner fleet with `cpu_m=runner_cpu_m`, `mem_mi=runner_mem_mi`,
`gpu=0`. Same lifetime as workflow job (unchanged behavior).

**Placeholder dataclass changes**:
- Remove: `frac_n: int`
- Add: `cpu_m: int`, `mem_mi: int`, `gpu: int` (must match the job it reserves for)

**`_place_free` / `_preempt_placeholder`**:
- Instead of `1.0 - n.used + EPS >= 1/frac_n`, check
  `n.cpu_allocatable_m - n.cpu_used_m >= job.cpu_m` AND
  `n.mem_allocatable_mi - n.mem_used_mi >= job.mem_mi` AND
  `n.gpu_allocatable - n.gpu_used >= job.gpu`.
- MostAllocated: score = `max(cpu_used_frac, mem_used_frac, gpu_used_frac)`
  where each is `used / allocatable`. Pick highest-scoring node that fits.
  This matches Kubernetes MostAllocated (which uses max of resource
  usage ratios, weighted).
- Preemption matches by exact (cpu_m, mem_mi, gpu) tuple (a placeholder must
  match the job it's reserving for). Not "matching frac_n" anymore.

**Fresh node creation** (step 2b, step 3, step 4c):
- Take the pending pod's `(cpu_m, mem_mi, gpu, fleet)`.
- Ask a `pick_instance(fleet, cpu_m, mem_mi, gpu)` helper for the smallest
  instance that fits (Karpenter emulation).
- Materialize the Node with that instance's allocatable values.
- If NO instance in the fleet fits: sim error (this shouldn't happen with
  real HUD data — but assert loudly so we know).

**Step 6 metric**:
- Per-node util = `max(cpu_used/cpu_allocatable, mem_used/mem_allocatable,
  gpu_used/gpu_allocatable)`. This matches the "which resource is most
  constrained" view.
- Alternative simpler: report BOTH CPU and memory util separately (as prod
  does — two panels). Prefer this. Two metrics: `cluster_cpu_util`,
  `cluster_mem_util`. GPU util reported per-pool only.
- Aggregation for the cluster-wide line: allocatable-weighted mean (matching
  prod's dashboard PromQL). Per-pool same.

**Step 6 output**: report both CPU and memory average + p10..p90. Per-pool
table gets extra columns for cpu/mem/gpu util.

### C. What DOESN'T change

- Warmup model: unchanged. Warming nodes still filter out of `_place_free`
  workload placement; still counted in denominator by their (empty) used.
- Placeholder timing: unchanged.
- 5-min bucketing: unchanged.
- Karpenter consolidation TTL: unchanged (`empty_ttl_buckets=2`).
- Shuffle: unchanged.
- CLI flags: `--no-warmup`, `--warmup-buckets*`, `--drop-provider`,
  `--keep-fraction`, `--seed` all stay.
- CSV path stays a positional arg.

### D. New CLI

None needed. All the new data comes from the CSV. If we want to A/B against
the old model, keep old sim as `simulate.py.bkp` locally.

## File impact

- `build_csv.py`: substantial rewrite of `build_label_table`, `cmd_extract`.
  CSV schema breaks (old CSVs won't work).
- `simulate.py`: substantial rewrite of Node, Job, Placeholder, `_place_free`,
  `_preempt_placeholder`, `simulate()`, `report()`. ~200 lines changed.
- Need to import from `scripts/python/`: `instance_specs`, `daemonset_overhead`,
  `analyze_node_utilization` (for `kubelet_reserved`), `runner_overhead`.
  Already done in `build_csv.py` — extend the same pattern to `simulate.py`.

## Fleet-to-instances mapping

`build_csv.py`'s `derive_fleet_name(instance, node_fleet)` already picks the
fleet name for a runner def. But the sim needs the REVERSE: given a fleet
name (e.g. `c7i-runner`, `g5`), which instance types can Karpenter pick?

Load from `modules/nodepools/defs/*.yaml` — these have the list of instance
types + weights. `scripts/python/nodepool_defs.py` probably already parses
these; if so, reuse. Otherwise, small YAML walker.

## Consequences to expect

- Cluster util WILL drop from the current sim number. Whole-node r7a jobs that
  used to report 100% will now report `worker_cpu / (allocatable - DS - kubelet)`
  = ~95-97%. That alone gets us closer to prod.
- Per-fleet packing accuracy improves. GPU fleets will show whichever resource
  (CPU vs memory vs GPU) is actually the bottleneck — often not CPU.
- Node counts will change per fleet: fleets whose defs are memory-heavy will
  provision more nodes (fewer fit per instance); CPU-heavy fewer nodes.
- Placeholders now match jobs on the tuple (cpu_m, mem_mi, gpu), so preemption
  is slightly stricter. Should still work fine on real data because
  placeholders are created from the same defs as jobs.

## Validation plan

1. Old sim on old CSV → baseline number (current: ~72.6% default warmup on
   sample).
2. New sim on new CSV, `--no-warmup` → should be reasonably close to prod's
   61% since we've now removed the tile-perfection assumption AND added
   real overhead accounting.
3. New sim with warmup → should be even lower.
4. Per-pool comparison against prod `dash.json` per-fleet breakdown.
5. Spot-check one pool: pick c7i, count max_nodes vs prod's observed peak.

## Non-goals for THIS pass

- Karpenter's actual `price-capacity-optimized` — we use "biggest weight
  first" as approximation.
- Spare-capacity floor (compactor doesn't terminate, just taints — we're
  not modeling that either yet).
- Batching latency (Karpenter batches for ~45s before provisioning).
- `karpenter.sh/do-not-disrupt` pinning of nodes.
- Multi-instance-type fleet load-balancing across sizes based on pod shape.
