# Node/pod sizing optimizer — design

## Goal

For each fleet family (r7a, c7i, m7i, c7a, m8g, m7g, m6i, r7i, g5, g6, g4dn),
decide whether to keep the current single-nodepool layout or **split the
family into multiple sub-nodepools with different instance sizes** and how to
**partition its runner defs across those sub-nodepools**, to maximize
allocatable-weighted `max(cpu_util, mem_util)` on real HUD workload data.

Pod requests are not a free search axis. Once a def is assigned to a
sub-nodepool with a chosen instance size, its cpu/mem request is derived
deterministically from the tight-fit rule (D4) — the search never proposes
arbitrary pod upsizing.

Skip fleets that are already right-sized or out of scope: `p4d`, `p5`,
`p5-large`, `p6-b200`, `p6-b200-large`, all `-metal` variants, the
reserved-capacity fleets, `-large` variants (whole-node-per-pod by design), and
`c7i-runner`.

## Why this is the right optimization target

Two failure modes we're steering between:

- **Fragmentation waste** dominates when instance size is too large. A 8c/64Gi
  pod on a r7a.48xl (192c/1536Gi) with 1/1 packing wastes ~184c of the node.
  Small pods on big nodes underutilize.
- **Overhead waste** dominates when instance size is too small. A 8c/64Gi pod
  on a r7a.2xl (8c/64Gi) with 1/1 packing pays ~1c kubelet + ~0.5c DaemonSets
  = ~15-20% of the node lost to fixed overhead before any workload. Small
  instances have terrible overhead ratios.

Current state deploys ONE nodepool per family using ONE instance size (the
biggest in the family). All pods in a family share that nodepool. This is
efficient for pods whose shape is close to a clean divisor of the big
instance, and terrible for pods that are much smaller. Splitting the family
into sub-nodepools lets small pods run on smaller instances (lower
fragmentation) while larger pods keep the big-instance home (lower per-pod
overhead).

CPU util alone rewards packing patterns that strand expensive memory on
memory-optimized families (r7a, r7i). Memory util alone rewards patterns that
strand CPU on compute-optimized families (c7a, c7i). Picking `max(cpu, mem)`
optimizes for the resource that will actually run out first — the binding
constraint. The sim already reports both dimensions; the ranking function
takes the max.

## Logic — the search space

### Configuration structure

Per family, a candidate configuration is a partition of the family's runner
defs across one or more sub-nodepools plus an instance-size choice per
sub-nodepool:

```
{
  <sub_nodepool_name_1>: {instance: <aws_size>, pods: [def_label, ...]},
  <sub_nodepool_name_2>: {instance: <aws_size>, pods: [def_label, ...]},
  ...
}
```

- Sub-nodepool count ranges from 1 (current state — no split) to `N_defs`
  (one sub-nodepool per def).
- Each sub-nodepool uses exactly one instance size.
- Each def is assigned to exactly one sub-nodepool.
- Pod cpu/mem requests are NOT stored in the config; they are derived
  deterministically from `(def, sub_nodepool.instance)` via the tight-fit
  rule below (D4).

### Eligibility catalog

For each `(def, in-family instance)` pair, precompute whether the pair is
feasible under the tight-fit rule and what the resulting adjusted slot shape
and per-node count `N` would be:

- Reuse the existing `compute_allocatable(instance, scoped_daemonsets)`
  helper. Returns `alloc_cpu_m, alloc_mem_mi, alloc_gpu`.
- Compute `N = min(alloc_cpu_m // orig_cpu, alloc_mem_mi // orig_mem)` (also
  divide alloc_gpu by orig_gpu when the def is a GPU def).
- Compute the tight-fit slot: `slot_cpu = alloc_cpu_m // N`,
  `slot_mem = alloc_mem_mi // N`.
- Apply the bounds check from D4. If any dimension is out of bounds, mark
  the pair infeasible.

The catalog is small (K defs × M in-family sizes = tens to low hundreds of
entries per family) and runs in seconds. It is the feasibility oracle every
partition-level candidate consults.

### Objective

Two distinct metrics with distinct roles: one for optimization ranking, one
for calibration against prod.

**Ranking metric — optimization objective:**

```
opt_cpu = sum(cpu_used_m)  / sum(cpu_allocatable_m + ds_cpu_m)
opt_mem = sum(mem_used_mi) / sum(mem_allocatable_mi + ds_mem_mi)

objective(sim_result) = allocatable_weighted( max(opt_cpu, opt_mem) )
```

`cpu_used_m` and `mem_used_mi` in the numerator are the **tight-fit-adjusted
pod requests** (per D4), not the original def requests — because adjustment
is a mechanical byproduct of the (def, instance) pair the config selects.
Denominator is capacity net of kubelet reserved (post-kubelet, pre-DS) — the
physical space available for real work if DS overhead were zero. DS appears
in the denominator as a fixed per-node tax, so configs that reduce per-node
DS fraction (bigger instances, fewer nodes) win by shrinking the denominator;
configs that pack workload tighter (bigger adjusted slot per pod) win by
growing the numerator. Both are real optimization axes.

**Calibration metric — matches prod dashboard PromQL:**

```
cal_cpu = sum(cpu_used_m + ds_cpu_m) / sum(cpu_allocatable_m + ds_cpu_m)
cal_mem = sum(mem_used_mi + ds_mem_mi) / sum(mem_allocatable_mi + ds_mem_mi)
```

Matches `node_compactor_node_utilization_ratio` (all pod requests including
DaemonSets over `node.status.allocatable`, i.e. post-kubelet-pre-DS). Sim's
`--daemonsets-in-metric` flag already emits this. Used ONLY for Phase 0
sim-vs-prod calibration and for final reports so a human can cross-check the
sim number against Grafana. Not used for ranking.

Tie-breaker on the ranking metric: total vCPU-hours (lower is better).
vCPU-hours is size-invariant WITHIN a family — 1h × 192 vCPU on r7a.48xl and
12h × 16 vCPU on r7a.4xl are the same raw compute — and roughly proportional
to $/hr since $/vCPU is near-constant across sizes within a family. Report
opt AND cal for both CPU and memory in every output; ranking uses
`max(opt_cpu, opt_mem)`.

### Search structure

The problem factors along fleet-family boundaries. r7a workloads don't
consume c7i nodes. `c7i-runner` receives a fixed-shape 750m/1Gi pod per
workflow job regardless of the workflow's fleet (see runner.yaml.tpl —
`CAPACITY_AWARE_RUNNER_CPU` / `RUNNER_MEMORY` are hard-coded), so the
workflow-fleet optimization does not perturb c7i-runner load at all. Coupling
between families is zero at this metric. Outer loop: **for each family, run
an independent search**.

Within a family, the search space is (partitions of K defs) × (per-partition
instance assignment from M in-family instances). Bell number
B(K) counts the partitions: B(5)=52, B(6)=203, B(7)=877. For each partition,
each group independently picks from M instances (typically M ≈ 6-8). Raw
count is B(K) × sum over partitions of M^(group_count); feasibility filtering
prunes aggressively.

Two viable approaches:

- **Exhaustive enumeration**: iterate every feasible config, sim each, pick
  best. ~200-500 sim runs per family × 20s = 1-3 hours per family. Guarantees
  global optimum. Preferred for K ≤ 5.
- **Hill-climb over partition+assignment space**: multi-restart from random
  feasible seeds. Neighbor moves (see Phase 2). Preferred for K ≥ 6 where
  exhaustive gets expensive.

The choice is per-family and controlled by a CLI flag with a K-based
default.

## Decisions made

### D1: Two metrics — opt for ranking, cal for calibration

**Ranking (opt_cpu / opt_mem)**: numerator is workload requests only,
denominator is `allocatable + ds` (physical space post-kubelet, pre-DS).
Ranks configs by `max(opt_cpu, opt_mem)`. This is the true optimization
target: it rewards both packing workload tighter (grow numerator) and
reducing per-node DS fraction by using bigger instances (shrink denominator).

**Calibration (cal_cpu / cal_mem)**: matches prod's
`node_compactor_node_utilization_ratio` — DS included in both num and denom.
Used ONLY for Phase 0 sim-vs-prod delta measurement and for reports so the
number is cross-checkable against Grafana. NOT used for ranking.

Node-hours is the tie-breaker on the ranking metric.

### D2: Family-locked mapping

Every def stays in its current fleet family. This is a scoping decision, not
a technical limit. Reasons:

- ARC runner labels are addressable identities from GitHub workflows
  (`runs-on: linux.arm64.m7g.4xlarge`, etc.). The family name is encoded in
  the label; cross-family swaps break label semantics and require coordinated
  PRs across `pytorch/pytorch` `.github/workflows/`.
- Family choice encodes NUMA/network topology assumptions and arch (amd64 vs
  arm64) that pod `cpu_m` / `mem_mi` requests do not capture.
- Family boundaries match reserved-capacity boundaries in the account.

Cross-family moves are a separate optimization pass we are not doing here.

### D3: Fleet independence — zero coupling on the workload side

Per-fleet search. Other fleets held at current config. Coupling to
`c7i-runner` is zero because the runner-pod shape is fixed by the ARC
template, not derived from workflow fleet choice. No cross-fleet
re-optimization phase is required.

### D4: Pod adjustment rule — deterministic tight-fit with tolerance bounds

The runner def YAML has two knobs the operator sets: `vcpu` (an integer,
e.g. 2, 8, 16, 46 — the MAIN pod's cpu request) and `memory` (Gi, integer
or Gi-suffixed). The runner-container-hooks sidecar attaches a fixed
320 mcpu / 522 MiB per pod regardless of the main pod's size — it is not
part of the adjustment axis. So the total pod request is
`main_vcpu * 1000 + 320` mcpu of CPU and `memory_mi + 522` MiB of memory.

Given a def with original `(orig_main_vcpu, orig_mem_mi)` (and `orig_gpu`
if applicable) assigned to a sub-nodepool with instance `inst`:

1. `alloc_cpu_m, alloc_mem_mi, alloc_gpu = compute_allocatable(inst, ds)`.
2. Compute pod count `N` at ORIGINAL size (unadjusted):
   ```
   n_max_cpu = alloc_cpu_m // orig_cpu_m       # orig_cpu_m includes sidecar
   n_max_mem = alloc_mem_mi // orig_mem_mi
   n_max_gpu = (alloc_gpu // orig_gpu) if orig_gpu > 0 else inf
   N = min(n_max_cpu, n_max_mem, n_max_gpu)
   ```
   If `N == 0`, the pair is infeasible on capacity alone.
3. Derive the tight-fit MAIN vCPU:
   ```
   raw_available_per_pod_cpu_m = alloc_cpu_m // N
   available_main_cpu_m = raw_available_per_pod_cpu_m - sidecar_cpu_m
   new_main_vcpu = max(1, available_main_cpu_m // 1000)
   slot_cpu_m = new_main_vcpu * 1000 + sidecar_cpu_m
   ```
   The sidecar's 320 mcpu is subtracted BEFORE flooring to whole vCPU —
   otherwise the "rounded to whole vCPU" number lumps the sidecar in and the
   resulting slot cannot actually deploy (`main * 1000 + 320` would exceed
   the slot allowance).
4. Derive the tight-fit MAIN memory (in whole GiB):
   ```
   raw_available_per_pod_mem_mi = alloc_mem_mi // N
   available_main_mem_mi = raw_available_per_pod_mem_mi - sidecar_mem_mi
   new_main_memory_gib = max(1, available_main_mem_mi // 1024)
   slot_mem_mi = new_main_memory_gib * 1024 + sidecar_mem_mi
   ```
   The sidecar's 522 MiB is subtracted BEFORE flooring to whole GiB —
   otherwise the "rounded to whole GiB" number lumps the sidecar in and the
   resulting slot cannot actually deploy (`main * 1024 + 522` would exceed
   the slot allowance). `new_main_memory_gib` is the integer the operator
   writes into the def YAML's `memory:` field.
5. Bounds check on the NEW `main_vcpu` (integer vCPU, not total mcpu):
   ```
   lo = min(orig_main_vcpu - 1, ceil(orig_main_vcpu * 0.95))
   hi = max(orig_main_vcpu + 1, ceil(orig_main_vcpu * 1.35))
   feasible_cpu = lo <= new_main_vcpu <= hi
   ```
   Worked examples:
   - `orig_main_vcpu=2`  → range `[min(1, 2),  max(3, 3)]  = [1, 3]`
   - `orig_main_vcpu=8`  → range `[min(7, 8),  max(9, 11)] = [7, 11]`
   - `orig_main_vcpu=16` → range `[min(15, 16), max(17, 22)] = [15, 22]`
   - `orig_main_vcpu=46` → range `[min(45, 44), max(47, 63)] = [44, 63]`

   Memory bound is on the TOTAL slot (main + sidecar), preserving the prior
   semantics: `slot_mem_mi >= orig_mem_mi * 0.95` (5% shrink max on total).
   Memory upper bound: none.
6. If any dimension is out of bounds, the (def, instance) pair is
   **infeasible** and any config containing it is rejected.

`(slot_cpu_m, slot_mem_mi)` are the pod's cpu/mem requests fed to the
simulator. `new_main_vcpu` is the integer the operator writes into the
def YAML's `vcpu:` field — the ONLY axis this optimizer is allowed to
adjust on the CPU dimension.

**Scope — GPU defs**: the D4 tight-fit rule applies to ALL in-scope defs,
including GPU defs in the g5, g6, and g4dn families. The vcpu adjustment
range (`[min(orig-1, ceil(orig*0.95)), max(orig+1, ceil(orig*1.35))]`) and
the memory 5% lower bound apply symmetrically to CPU-only and GPU defs.
The vcpu tolerance is what lets a GPU-heavy pod right-fit onto a GPU
instance size when the vcpu allocation isn't perfectly divisible — e.g.
`l-x86iavx512-29-115-t4` (29 vcpu / 1 GPU) on `g4dn.8xlarge` (32 vcpu /
1 GPU): N=1, vcpu adjusts 29→31 (+7%), within bounds.

**GPU count is strict**: `orig_gpu` is NEVER reshapeable. A def declared
with 1 GPU must land on an instance size where its per-pod GPU slot is
exactly 1 (`alloc_gpu // N == orig_gpu`). A def declared with 4 GPUs
must land on a 4-GPU-per-pod slot exactly. Any pair whose tight-fit
would silently change the per-pod GPU count is rejected. GPU-typed defs
cannot land on non-GPU instances and vice versa (filtered before any
capacity math).

### D5: Shape represents pod REQUESTS, not real usage

Sim optimizes on requests, same as Karpenter's scheduler. A workload that
requests 46c but uses 15c is packed as 46c. Prod's Karpenter runs with
`WhenEmptyOrUnderutilized` consolidation, which reclaims based on real
utilization — so prod may see additional headroom sim does not model.
Expect a systematic gap between sim util and Grafana util; Phase 0
calibrates it.

### D6: Deliverable is git-apply-able patches with a rename flag

Recommendations per family are emitted as:

- One new `modules/nodepools/defs/<family>-<subfleet-label>.yaml` per
  sub-nodepool that does not already exist (each new sub-nodepool's
  `instances` list contains exactly its chosen instance size, name-taint
  isolated per prod convention — see D7).
- Per-def patch against `modules/arc-runners/defs/<label>.yaml`:
  - `node_fleet` updated to point at the assigned sub-nodepool.
  - `vcpu` / `memory` updated to the D4 tight-fit slot values (may be a
    small upsize or ≤5% memory shrink).
- Per-def rename flag: labels like `l-x86iamx-8-64` encode the shape
  ("8 vcpu / 64 GiB"). If the adjustment changes cpu or mem by more than
  ~10% relative to the original, the label lies about the shape. Emit
  `rename_required=true` for that def; auto-apply is halted for that def
  and a manual-action item is emitted with a proposed new label and the
  list of downstream `.github/workflows/` files that would need updating.

Patches must pass `git apply --check` and `just lint`.

### D7: Sub-nodepools are the deployment unit

Sub-nodepools are the first-class output of this optimization. Each
recommended sub-nodepool becomes its own fleet YAML with a single-entry
`instances` list, name-taint isolated so `sim_nodes.pick_instance` and
prod Karpenter both pick the right instance for pods routed to it — the
same pattern prod already uses for `r7a-large`. Each def's `node_fleet`
points at exactly one sub-nodepool.

This maps cleanly to how `sim_nodes.pick_instance` works
(sim_nodes.py:200-214): it picks the highest-weight instance in the fleet
whose allocatable fits the pod. With one instance per sub-nodepool, the
choice is trivially deterministic. No sim-code changes required —
sub-nodepools are just fleet YAMLs the sim already loads via
`_load_fleet_specs`.

### D8: Skip GPU/baremetal/reserved fleets and c7i-runner

Out of scope for this pass:

- `p4d`, `p5`, `p5-large`, `p6-b200`, `p6-b200-large` — GPU allocation
  dominates, reserved capacity constrains sizing.
- `g4dn-metal`, `m7g-metal`, `c7a-large`, `c7i-large`, `m8g-large`,
  `r7a-large`, `g5-large` — `-large` and metal variants are whole-node-per-pod
  by design.
- `c7i-runner` — the 750m/1Gi runner pod shape IS in principle tunable
  (it's a template variable in runner.yaml.tpl), but changing it affects
  every workflow across every family. Cross-cutting change, out of scope
  for this pass.

Scope: `r7a`, `c7i`, `m7i`, `c7a`, `m8g`, `m7g`, `m6i`, `r7i`, `g5`, `g6`,
`g4dn`.

## Plan

### Phase 0 — Measurement and calibration (before any optimizer code)

1. Benchmark one sim run in-process (no subprocess, no CSV reload). Measure
   wall-clock. Target: < 30s per config on the 60-day CSV.
2. Measure seed-variance noise floor: run sim over the same config across 20
   seeds, compute stddev of the objective. This is the σ used later for the
   "improved by > 3σ" gate.
3. Sim-vs-prod calibration: run sim over the past 30 days of HUD data,
   compare cluster CPU and memory util to prod's
   `node_compactor_node_utilization_ratio` from Mimir for the same window.
   Report the delta per fleet. If the delta is > 5pp systematically, all
   sim-based recs are reframed as **deltas vs baseline**, not absolutes.
4. Extend `INSTANCE_SPECS` in `scripts/python/instance_specs.py` to cover
   every in-scope family size that the Phase 1 catalog would need (every
   AWS size in each in-scope family — the catalog considers all of them as
   candidate sub-nodepool instances). Fill `memory_mi` via
   `collect_instance_memory.py` where a live node exists; otherwise use the
   0.925 estimate as documented.
5. Compute per-family theoretical util ceiling from the Phase 1 eligibility
   catalog — the best achievable util assuming perfect packing under D4
   constraints. Anchors the "how much room is there" question before any
   search.

### Phase 1 — Eligibility catalog (analytical, no sim runs)

Deliverable: `scripts/node-size-sweep/optimize_catalog.py`.

- Input: `INSTANCE_SPECS`, `ENI_MAX_PODS`, DaemonSet defs, runner defs.
- Output:
  1. **Eligibility catalog**: per `(def, in-family instance)` pair,
     `(feasible: bool, N, slot_cpu_m, slot_mem_mi, slot_gpu,
     overhead_frac)`. `feasible` reflects the D4 bounds check.
  2. **Per-def summary**: for each def, the list of feasible instances and
     per-instance waste on each dimension (`slot - orig`), so operators can
     see which instances a def is compatible with before any sim runs.

Runs in seconds. Answers "for the r7a family, which (def, instance) pairs
are feasible and what does each look like after adjustment?" and surfaces
obvious wins (large defs stranded on small instances, small defs stranded
on the current family-max instance) without any sim run.

### Phase 2 — Sim-driven search

Deliverable: `scripts/node-size-sweep/optimize_search.py`.

For each in-scope family:

1. **Enumeration mode selection**: default exhaustive for K ≤ 5, hill-climb
   for K ≥ 6. Overridable via `--mode {exhaustive,hillclimb}`.
2. **Feasibility gate**: a config is feasible iff every def in the family
   is assigned to a sub-nodepool whose instance is in the def's feasible
   list from Phase 1. Infeasible configs are skipped without a sim run.
3. **Exhaustive**: iterate all partitions of the K defs (Bell number B(K));
   for each partition, enumerate all M^(group_count) instance-assignment
   combinations; drop infeasible; sim the survivors; return the argmax on
   the ranking objective.
4. **Hill-climb**: `NUM_RESTARTS >= 20`. Neighbor moves on a
   (partition, assignment) config:
   - **move-pod**: reassign one def from its current sub-nodepool to a
     different existing sub-nodepool (or to a new singleton sub-nodepool).
   - **merge**: combine two sub-nodepools into one, picking the instance
     that keeps every merged def feasible; skip if none exists.
   - **split**: peel a subset of defs out of a sub-nodepool into a new
     sub-nodepool, with a newly chosen instance.
   - **change-instance**: swap one sub-nodepool's instance for another
     in-family instance that keeps every def in that sub-nodepool feasible.
   All neighbors are feasibility-gated before sim. Keep neighbor if
   objective improves by > 3σ (σ from Phase 0). Sideways moves within σ
   are accepted with limited budget per restart to traverse plateaus.
5. **Baseline**: the current single-sub-nodepool config (one sub-nodepool
   with the family's current instance, all defs assigned). Included as a
   seed and as the reference for "no change recommended" verdicts.
6. **Memoize**: `sim(config) → objective` on config hash (see
   Checkpointing).

Emit to logs: `max(cpu, mem)` objective, cpu util alone, mem util alone,
vCPU-hours. Ranking is `max(cpu, mem)`; vCPU-hours breaks ties.

Expected sim runs per family: 200-500 exhaustive on small families; 50-500
hill-climb on large ones. Times ~11 families. Total budget target: ~24h
wall on a single box without parallelism; parallelism across families is
trivial.

### Phase 3 — Sensitivity analysis

For the top recommendation per family:

- Neighbor perturbation: apply one of each D4-preserving neighbor move
  (move-pod, merge, split, change-instance) and record util delta. If
  deltas are below noise floor, the recommendation is fragile — flag it.
- Sweep runner-container-hooks tax (currently 320m/522Mi) by ±50%. If a
  ±50% change to the hook tax would shift the recommendation, flag as
  "hook-overhead sensitive" — operator should consider hook optimization
  before shape optimization.

### Phase 4 — Deliverable

Per-family report AND git-apply-able patches. Report format per family:

```
Family: r7a
Baseline:
  Sub-nodepool: r7a (instance r7a.48xlarge)
    Defs (K=6): [l-x86-r7a-8-64, l-x86-r7a-24-192, ...]
    opt_cpu=54.1%  opt_mem=48.7%  opt_max=54.1%
    vcpu-hours: 2,368,000

Recommendation:
  Sub-nodepool: r7a-small (instance r7a.8xlarge)
    Defs (2): [l-x86-r7a-8-64, l-x86-r7a-4-32]
    per-def adjustment:
      l-x86-r7a-8-64: cpu 8000m->8000m (unchanged), mem 65536Mi->65536Mi (unchanged), N=1
      l-x86-r7a-4-32: cpu 4000m->4000m (unchanged), mem 32768Mi->32768Mi (unchanged), N=2
  Sub-nodepool: r7a-large (instance r7a.48xlarge)
    Defs (4): [l-x86-r7a-24-192, ...]
    per-def adjustment: ...
  opt_max=68.4%  (delta +14.3pp vs baseline)
  vcpu-hours: 1,876,000  (delta -492,000 vs baseline, -20.8%)

Rename-required: l-x86-r7a-4-32 (mem adjusted -12% -> label lies)
Sensitivity: stable (all neighbor perturbations within σ)
Verdict: apply
```

Report also includes:

- Theoretical ceiling from Phase 0.
- Noise floor σ from Phase 0.
- Sim-vs-prod delta from Phase 0.
- Sensitivity flags from Phase 3.
- Rejection cases: families where no recommendation beats baseline by > 3σ
  get an explicit "no change recommended" verdict.

Output layout:

```
scripts/node-size-sweep/output/
  <timestamp>-<git-sha>/
    logs/
      global.log
      r7a.log
      c7i.log
      ...
    cache.sqlite            # sim(config) -> objective cache
    state.sqlite            # search state per fleet
    reports/
      global.md
      r7a.md
      r7a.patch
      ...
```

## Runtime, logging, and checkpointing

The search is long-running (hours to days) across many families. Runtime
observability and crash-safety are first-class requirements.

### Logging

- All output on stderr, structured as `[timestamp] [family:fleet] [phase] message`.
- Python's `logging` module with `%(asctime)s %(levelname)s [%(name)s] %(message)s`.
  Logger name = `<family>` (or `global` for orchestrator lines).
- Per-config sim call: log start
  (`starting run 47/500 for r7a, config=<hash>, restart=2`) and end with
  elapsed and result
  (`done in 42s, objective=64.3%, best=64.7%, cpu=61.2%, mem=64.3%`).
- Per-restart: log the convergence trail — neighbors tried, best improvement
  per step, wall-clock, final config hash.
- Per-family: on completion log total runs, wall-clock, best config, delta
  vs baseline, verdict (improved / no-change / rejected).
- Global heartbeat every 5 minutes: families done, families in-progress,
  ETA from running average per-family time.
- `--log-level {debug,info,warning}` controls verbosity. Default info.

### Progress bars

- Interactive (isatty): tqdm progress bar per family showing "runs completed
  / estimated total" where estimate = feasible-config count for exhaustive
  mode, or `num_restarts * avg_neighbors_per_restart * avg_convergence_steps`
  for hill-climb (estimate refined online).
- Log lines are ALSO emitted alongside progress bars so a `tail -F` on the
  log file gives useful output.

### Checkpointing

Persistent state on disk so a crash, SIGINT, or intentional restart resumes
without redoing work.

**Sim-result cache**: SQLite at `<output>/cache.sqlite`. Schema:

```sql
CREATE TABLE sim_cache (
    key         TEXT PRIMARY KEY,     -- sha256 of canonical JSON below
    config_json TEXT NOT NULL,
    objective   REAL NOT NULL,
    cpu_util    REAL NOT NULL,
    mem_util    REAL NOT NULL,
    vcpu_hours  REAL NOT NULL,
    computed_at INTEGER NOT NULL
);
```

Key input to the sha256:

```json
{
  "config":       {...},              // {subpool: {instance, pods: [...]}, ...}
  "adjusted":     {...},              // {def_label: {cpu_m, mem_mi}}  derived from config via D4
  "sim_flags":    {...},              // seed, empty_ttl, warmup, phantom, cap, etc.
  "csv_sha256":   "...",              // input CSV hash
  "simulate_py":  "...",              // sha256 of simulate.py
  "sim_nodes_py": "...",              // sha256 of sim_nodes.py
  "sim_load_py":  "..."               // sha256 of sim_load.py
}
```

Any change to the sim logic invalidates the cache automatically — no manual
bumping. Every `sim(config)` call first hits the cache; on hit, log
`cached hit for config <hash>, objective=X` and return.

**Search state**: SQLite at `<output>/state.sqlite`. Schema:

```sql
CREATE TABLE search_state (
    family              TEXT NOT NULL,
    restart_id          INTEGER NOT NULL,
    config_json         TEXT NOT NULL,
    best_objective      REAL,
    neighbors_evaluated INTEGER NOT NULL,
    status              TEXT NOT NULL,     -- 'pending' | 'running' | 'done'
    updated_at          INTEGER NOT NULL,
    PRIMARY KEY (family, restart_id)
);
CREATE TABLE family_state (
    family     TEXT PRIMARY KEY,
    status     TEXT NOT NULL,               -- 'pending' | 'running' | 'done' | 'rejected'
    verdict    TEXT,
    best_json  TEXT,
    updated_at INTEGER NOT NULL
);
```

Written after every improvement or every 30 seconds, whichever comes first.
SQLite gives concurrent-safe atomic writes; safe if we parallelize families
into subprocesses later.

**Resume logic**: on startup, if `--resume <path>` given (or output dir
exists and is not empty), load `state.sqlite` and `cache.sqlite`. Per
family: skip if status = `done` or `rejected` (unless `--force`); resume from
the last uncompleted restart otherwise. Sim cache is consulted for all
future sim calls automatically.

**Graceful shutdown**: catch SIGINT and SIGTERM. Flush current state and
cache. Log `checkpoint saved, resume with: <exact resume command>`. Exit 0.

**Crash safety**: state on disk is the last committed state, at most 30
seconds stale. Cache is safe (each sim result is committed on completion).

### CLI flags

```
--output-dir <path>       default: output/<timestamp>-<git-sha>
--resume <path>           resume from a prior run's directory
--fleet <name>            run just one family (repeatable; testing only)
--mode {exhaustive,hillclimb,auto}   default auto (exhaustive if K<=5)
--num-restarts <N>        default 20 (hill-climb only)
--noise-sigma <float>     override Phase 0 noise floor (testing)
--log-level {debug,info,warning}   default info
--dry-run                 emit Phase 1 catalog + eligibility, no sims
--force                   ignore 'done' status in resume, redo the family
```

### In-process sim invocation

- Import `simulate` and `load_jobs` directly. Never subprocess.
- Load CSV ONCE at start, reuse the `jobs` list across all sim calls.
- Snapshot `arrivals` per sim call before the loop mutates it (the current
  `simulate()` shuffles `arr = arrivals.get(t, [])` in place, which mutates
  the caller's list — reruns would see different order). Fix: either pass
  a `copy.deepcopy(arrivals)` per call, or refactor `simulate()` to shuffle
  a local copy of each bucket.
- Extend `simulate()` signature to accept a pre-parsed `jobs` list (already
  the case) plus a `ClusterModel` whose fleet YAML paths are overridden per
  candidate config. `ClusterModel.__init__` already accepts `defs_dirs`;
  the search harness writes the candidate sub-nodepool YAMLs to a temp dir
  per config and points `ClusterModel` at it.

## Implementation details

### Config representation and injection

```python
Config = dict[str, SubNodepool]   # subpool_name -> spec
SubNodepool = {
    "instance": str,               # AWS size, e.g. "r7a.8xlarge"
    "pods":     list[str],         # def labels assigned to this subpool
}
```

Applied by:

- For each subpool, writing a `<family>-<subpool-suffix>.yaml` to a temp
  defs dir with `instances: [<instance>]`.
- For each def, writing an adjusted-request override that maps
  `<def_label> -> {cpu_m: slot_cpu, mem_mi: slot_mem, node_fleet: <subpool>}`
  where `slot_cpu`, `slot_mem` come from D4 applied to (def, subpool.instance).
- Constructing `ClusterModel(defs_dirs=[<tempdir>, <other-real-dirs>])`.
- Overriding `build_label_table()`'s output for the candidate labels before
  passing into `load_jobs` (or into a variant that accepts an already-built
  label table).

No changes to `simulate.py`'s inner loop. All optimizer logic lives in the
new files.

### Eligibility catalog generation

```python
def generate_catalog(family: str, defs: list[DefReq]) -> dict[tuple[str, str], Eligibility]:
    instances = [i for i in INSTANCE_SPECS if i.startswith(f"{family}.")]
    out = {}
    for d in defs:
        for inst in instances:
            alloc = compute_allocatable(inst, scoped_daemonsets_for(inst))
            n_cpu = alloc.cpu_m // d.cpu_m
            n_mem = alloc.mem_mi // d.mem_mi
            n_gpu = (alloc.gpu // d.gpu) if d.gpu > 0 else 10_000
            N = min(n_cpu, n_mem, n_gpu)
            if N == 0:
                out[(d.label, inst)] = Eligibility(feasible=False, reason="capacity")
                continue
            n_cpu = alloc.cpu_m // d.cpu_m
            n_mem = alloc.mem_mi // d.mem_mi
            n_gpu = (alloc.gpu // d.gpu) if d.gpu > 0 else 10_000
            N = min(n_cpu, n_mem, n_gpu)
            if N == 0:
                out[(d.label, inst)] = Eligibility(feasible=False, reason="capacity")
                continue
            # Sidecar is fixed at hooks_cpu_m regardless of main pod size.
            sidecar_cpu_m = d.cpu_m - d.main_vcpu * 1000
            raw_per_pod = alloc.cpu_m // N
            new_main_vcpu = max(1, (raw_per_pod - sidecar_cpu_m) // 1000)
            slot_cpu = new_main_vcpu * 1000 + sidecar_cpu_m
            slot_mem = alloc.mem_mi // N
            slot_gpu = alloc.gpu // N if d.gpu > 0 else 0
            if not within_main_vcpu_bounds(new_main_vcpu, d.main_vcpu) \
                    or not within_mem_bounds(slot_mem, d.mem_mi):
                out[(d.label, inst)] = Eligibility(feasible=False, reason="tolerance")
                continue
            out[(d.label, inst)] = Eligibility(
                feasible=True, N=N,
                slot_cpu_m=slot_cpu, slot_mem_mi=slot_mem, slot_gpu=slot_gpu,
                new_main_vcpu=new_main_vcpu,
                overhead_frac=1 - (alloc.cpu_m / (INSTANCE_SPECS[inst]["vcpu"] * 1000)),
            )
    return out

def within_main_vcpu_bounds(new_main_vcpu: int, orig_main_vcpu: int) -> bool:
    lo = min(orig_main_vcpu - 1, math.ceil(orig_main_vcpu * 0.95))
    hi = max(orig_main_vcpu + 1, math.ceil(orig_main_vcpu * 1.35))
    return lo <= new_main_vcpu <= hi

def within_mem_bounds(slot_mem: int, orig_mem: int) -> bool:
    return slot_mem >= int(orig_mem * 0.95)
```

### Search harness

```python
for family in FAMILIES:
    if state.family_done(family): continue
    catalog = generate_catalog(family, defs[family])
    baseline = load_current(family)                 # 1 subpool, all defs
    assert feasible(baseline, catalog)
    baseline_obj = run_sim_cached(baseline)
    best_config, best_obj = baseline, baseline_obj

    if mode(family) == "exhaustive":
        for cfg in enumerate_configs(defs[family], instances[family], catalog):
            obj = run_sim_cached(cfg)
            if obj > best_obj + 3 * sigma:
                best_config, best_obj = cfg, obj
    else:  # hillclimb
        for restart in range(NUM_RESTARTS):
            if state.restart_done(family, restart): continue
            state.mark_running(family, restart)
            cfg = seed(restart, baseline, catalog)  # 0=baseline, N>=1=random feasible
            neutral_budget = NEUTRAL_MAX
            while True:
                neighbors = [n for n in neighbor_moves(cfg, catalog) if feasible(n, catalog)]
                scored = [(n, run_sim_cached(n)) for n in neighbors]
                if not scored: break
                best_n, best_n_obj = max(scored, key=lambda t: t[1])
                delta = best_n_obj - run_sim_cached(cfg)
                if delta > 3 * sigma:
                    cfg = best_n
                elif abs(delta) <= sigma and neutral_budget > 0:
                    cfg = best_n
                    neutral_budget -= 1
                else:
                    break
                state.checkpoint(family, restart, cfg, ...)
            obj = run_sim_cached(cfg)
            if obj > best_obj + 3 * sigma:
                best_config, best_obj = cfg, obj
            state.mark_restart_done(family, restart)

    verdict = "improved" if best_obj > baseline_obj + 3 * sigma else "no-change"
    state.mark_family_done(family, best_config, verdict)
    write_reports_and_patches(family, best_config, baseline)
```

### Neighbor generation (hill-climb only)

Four move types on a `(partition, assignment)` config:

- **move-pod**: for each def, for each existing sub-nodepool other than the
  def's current one, emit a config with the def reassigned; also emit a
  config with the def in a new singleton sub-nodepool (instance chosen from
  the def's feasible instances).
- **merge**: for each pair of sub-nodepools, emit a config merging them,
  with the merged sub-nodepool's instance chosen as the one that keeps every
  merged def feasible (skip if none exists).
- **split**: for each sub-nodepool containing ≥2 defs, for each non-empty
  proper subset of its defs, emit a config peeling that subset into a new
  sub-nodepool with a newly chosen instance.
- **change-instance**: for each sub-nodepool, for each in-family instance
  that keeps every assigned def feasible, emit a config with the instance
  swapped.

All neighbors are feasibility-gated (D4) before sim.

### File layout

```
scripts/node-size-sweep/
    optimize.md                  # this file
    optimize_catalog.py          # Phase 1
    optimize_search.py           # Phase 2 + 3 orchestrator
    optimize_reporting.py        # Phase 4 (report + patch emission)
    optimize_cache.py            # sim-cache + state SQLite helpers
    optimize_state.py            # search-state schema + resume logic
```

`simulate.py`, `sim_load.py`, `sim_nodes.py` require only minor additions:

- `sim_load.load_jobs` accepts an optional pre-built `label_table` param so
  the search harness can inject overrides without rewriting `build_csv.py`.
- `sim_nodes.ClusterModel.__init__` already accepts `defs_dirs`. Confirm
  temp-dir override works end-to-end.
- `simulate.simulate` needs the arrivals-mutation fix (shuffle a copy).

## Risks and mitigations

### R1: Optimizer exploits sim inaccuracies

Sim does not model: `karpenter.sh/do-not-disrupt` pinning, real Karpenter
`price-capacity-optimized` selection, spot interruption, per-instance
availability, real CPU/memory usage. Optimizer might find a config that
looks great in sim but fails in prod.

Mitigation: for the top-3 recommendations per fleet, sanity-check manually
— is the shape physically reasonable, does the picked instance have supply
in the target region, does the assignment respect
`karpenter.sh/do-not-disrupt` constraints (per-node util reasonable after
arbitrary pinning).

### R2: Overfitting to the training window

If the window had unusual workload mix, the optimum for that window may be
bad for the next one.

Mitigation: split the 60-day CSV into two disjoint 30-day windows.
Optimize on window 1, validate on window 2. If util delta collapses on
window 2, config is overfit. Reject and re-run with longer window.

### R3: DaemonSet overhead is a moving target

DS list changes over time. Overhead numbers we use today may not match
production 3 months from now.

Mitigation: catalog is regeneratable. Cache invalidates on sim-file changes
(source hashes in the cache key). If DS list changes, re-run — no manual
invalidation needed.

### R4: Family independence is a simplification

Fully rigorous joint search across all families would blow up
combinatorially. Per D3, coupling on the workload side is zero for
`c7i-runner`. Coupling for `karpenter.sh/do-not-disrupt` node-hour effects
across families exists in principle but is not modelable in the sim.

Mitigation: none required for correctness of per-family recs. Cross-family
effects would show up as sim-vs-prod delta, which Phase 0 measures.

### R5: Runner-name encodes shape

Labels like `l-x86iamx-8-64` encode "8 vcpu / 64 GiB". A D4 adjustment that
moves cpu or mem more than ~10% produces a label that lies. Renames are
cross-repo coordinated changes.

Mitigation: emit `rename_required=true` per rec when the label encodes the
shape and the adjusted shape drifts past the threshold. Do NOT auto-apply
patches for renamed defs; emit a manual-action item with the proposed new
label and the list of downstream `.github/workflows/` files that would need
updating.

### R6: Guaranteed QoS requires requests == limits

`validate_runner_qos.py` enforces that workflow pods use requests==limits
(runner.yaml.tpl:466-474 sets both to the same `{{VCPU}}` / `{{MEMORY}}`
template variables). Any shape change applies to both simultaneously.

Mitigation: the emitted per-def patch changes `vcpu` / `memory` which flow
through the template to both requests and limits — no separate action.
Verify in Phase 4 by regenerating a rendered pod spec for one changed def
and confirming requests == limits.

### R7: bin-pack-scheduler custom scheduler

Prod workflow pods use `schedulerName: bin-pack-scheduler` (see
runner.yaml.tpl:399 — `{{SCHEDULER_NAME_LINE}}`). Sim assumes MostAllocated
(see `sim_nodes.most_allocated_score`). If the custom scheduler does
something else (first-fit, best-fit, custom score), sim recs may not
translate.

Mitigation: read bin-pack-scheduler source and confirm the score function.
If mismatch, either update sim's score to match, or explicitly caveat all
recs as "assumes MostAllocated". Do before Phase 2.

### R8: karpenter.sh/do-not-disrupt pinning

Every workflow pod carries `karpenter.sh/do-not-disrupt: "true"`
(runner.yaml.tpl:218 and :391). Nodes cannot be consolidated while any
workflow pod runs. Sim does not model this — recs may overestimate
achievable util by whatever fraction of nodes are pinned by long-running
workflows at any moment.

Mitigation: measure pinned-fraction from prod (fraction of nodes with at
least one non-runner pod at a given moment) as part of Phase 0
calibration. Bake into the sim-vs-prod delta.

### R9: Data scope is pytorch/pytorch only

`pull_hud.py` filters to one repo. Clusters may serve other repos.

Mitigation: caveat the deliverable — tag which repos the rec applies to.
Add a cluster-scoped filter to `pull_hud.py` before scaling to multi-repo
clusters.

### R10: INSTANCE_SPECS incomplete

Not every in-scope family size is in `instance_specs.py` today — the file
prioritizes what production actually runs. The Phase 1 catalog needs every
size in every in-scope family.

Mitigation: Phase 0 step 4 adds every in-scope family size before the
catalog runs. Fixed cost, no reason to defer.

### R11: Sim-vs-prod delta uncalibrated

Without a calibration, absolute util numbers from sim are ungrounded.

Mitigation: Phase 0 step 3 measures the delta per fleet. If > 5pp
systematically, all rec numbers are reframed as **deltas vs baseline**, not
absolutes; the report text says so explicitly.

## Success criteria

- **Phase 0**: sim runs in-process at < 30s per config on 60-day CSV.
  Seed-variance stddev of the objective < 1pp cluster-wide util. Sim-vs-prod
  delta measured per fleet and documented; if > 5pp, explicit caveat added
  to all deliverables.
- **Phase 1**: eligibility catalog surfaces at least one obvious win per
  family (a def currently stranded on the family-max instance that is
  feasible on a much smaller instance with clearly lower overhead), with
  no sim runs.
- **Phase 2**: at least three families show `max(cpu, mem)` util improvement
  > 3σ vs baseline. Improvement stable across two disjoint validation
  windows (weekdays vs weekends, and first-30 vs last-30 days).
- **Phase 4**: patch files apply cleanly (`git apply --check` passes),
  pass `just lint`, pass `just test`. Reports include current util,
  recommended util, ceiling, noise floor, sim-vs-prod delta, and per-def
  rename flags. Rejection cases explicitly say "no change recommended".

## Prior misformulation

An earlier version of this document formulated the problem as a search over
per-def `(instance, N)` shapes drawn from a large enumerated shape catalog,
with pod cpu/mem requests treated as free search variables. That
formulation let the optimizer arbitrarily upsize pod requests to fit
whichever slot maximized the objective, which is not a valid deployment
change — pod requests are constrained by what the def actually needs, and
runner labels encode the shape. The correct formulation, above, treats the
search variable as "how do we partition this family's defs across
sub-nodepools, and which instance size does each sub-nodepool use?" Pod
adjustment is a mechanical consequence of the (def, instance) pair,
bounded by the tight tolerances in D4 (integer `new_main_vcpu` bounded by
`[min(orig - 1, ceil(orig × 0.95)), max(orig + 1, ceil(orig × 1.35))]`
on CPU, ≤ 5% memory down, memory up unbounded). Any
(def, instance) pair that would need a larger adjustment is infeasible and
excluded before the sim ever runs.
