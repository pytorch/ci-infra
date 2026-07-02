# Node/pod sizing optimizer — design

## Goal

For each fleet family (r7a, c7i, m7i, c7a, m8g, m7g, m6i, r7i, g5, g6, g4dn),
find the combination of (1) AWS instance sizes offered by the fleet and (2)
per-def pod shapes that maximizes **allocatable-weighted utilization of the
binding resource** — `max(CPU util, memory util)` — on real HUD workload data.

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

Sweet spot is usually 2-3 instance sizes per fleet with pod shapes tuned to
fit cleanly.

CPU util alone rewards packing patterns that strand expensive memory on
memory-optimized families (r7a, r7i). Memory util alone rewards patterns that
strand CPU on compute-optimized families (c7a, c7i). Picking `max(cpu, mem)`
optimizes for the resource that will actually run out first — the binding
constraint. The sim already reports both dimensions; the ranking function
takes the max.

## Logic — the search space

### Shape catalog

For each AWS instance size in each family, enumerate every valid split N:

```
N_max = min(
    alloc_cpu_m // req_cpu_m,
    alloc_mem_mi // req_mem_mi,
    alloc_gpu // req_gpu     if req_gpu > 0 else infinity,
)
enumerate N in 1..N_max
```

then compute per-slot allocatable capacity:

```
slot_cpu_m  = (alloc_cpu_m  - N * 0) // N       # even split
slot_mem_mi = alloc_mem_mi // N
slot_gpu    = alloc_gpu // N                    # only valid if gpu % N == 0
```

Reject shapes where `slot_gpu == 0` but the def requires a GPU, and shapes
where any slot dimension is <= 0.

This admits current production shapes (N ∈ {3, 5, 6, 12, 24, 96}) that a
power-of-2 constraint would exclude. Catalog size per family is typically
50-150 shapes, still tractable.

### Per-def eligibility

For each runner def, filter the family's catalog to shapes where
`slot >= request` on ALL 3 dimensions. Compute per-eligible-shape waste on
each axis (`slot - request`) so operators can eyeball the tradeoffs before any
sim runs.

### Objective

Two distinct metrics with distinct roles: one for optimization ranking, one
for calibration against prod.

**Ranking metric — optimization objective:**

```
opt_cpu = sum(cpu_used_m)  / sum(cpu_allocatable_m + ds_cpu_m)
opt_mem = sum(mem_used_mi) / sum(mem_allocatable_mi + ds_mem_mi)

objective(sim_result) = allocatable_weighted( max(opt_cpu, opt_mem) )
```

Numerator is workload requests only (excludes DaemonSets). Denominator is
capacity net of kubelet reserved (post-kubelet, pre-DS) — the physical space
available for real work if DS overhead were zero. DS appears in the
denominator as a fixed per-node tax, so configs that reduce per-node DS
fraction (bigger instances, fewer nodes) win by shrinking the denominator;
configs that pack workload tighter win by growing the numerator. Both are
real optimization axes.

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

Tie-breaker on the ranking metric: total node-hours (lower is better). Report
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

### D4: N enumeration is data-driven, not power-of-2

`N ∈ 1..N_max` where `N_max` is derived per (def, instance) from the
allocatable-vs-request division. This admits the shapes production actually
runs (N=3 on r7a.24xl for a 24c def; N=12 on r7a.48xl for the 8c amx def)
that a `{1,2,4,8,16,32}` constraint would exclude.

### D5: Shape represents pod REQUESTS, not real usage

Sim optimizes on requests, same as Karpenter's scheduler. A workload that
requests 46c but uses 15c is packed as 46c. Prod's Karpenter runs with
`WhenEmptyOrUnderutilized` consolidation, which reclaims based on real
utilization — so prod may see additional headroom sim does not model.
Expect a systematic gap between sim util and Grafana util; Phase 0
calibrates it.

### D6: Deliverable is git-apply-able patches

Recommendations are emitted as:

- Per-def unified diff against `modules/arc-runners/defs/<label>.yaml`
  updating `vcpu`, `memory`, `instance_type`, `node_fleet` fields as needed.
- Per-fleet unified diff against `modules/nodepools/defs/<fleet>.yaml` (or a
  new file for a virtual sub-fleet — see D7) updating the `instances` list.
- Per-rec metadata: does this def require a runner-name rename? If the
  label encodes shape (`l-x86iamx-8-64` means "8 vcpu / 64 GiB") and the
  shape changed, rename is required — auto-apply is halted for that rec and
  a manual-action item is emitted instead.

Patches must pass `git apply --check` and `just lint`.

### D7: Fleet composition via virtual sub-fleets (Option B)

`sim_nodes.pick_instance` picks the highest-weight instance in the fleet
whose allocatable fits the pod (sim_nodes.py:200-214). For a fleet with
both r7a.8xl (weight 20) and r7a.24xl (weight 80), a pod that fits both
lands on r7a.24xl. This means "add r7a.8xl to the fleet" alone does not
produce r7a.8xl placements — the small-instance shape gets shadowed.

The recommendation output creates virtual sub-fleets with disjoint instance
lists — e.g. `r7a-small` (r7a.8xl only), `r7a-medium` (r7a.16xl only),
`r7a-large` (existing). Each def is mapped to the fleet that contains the
recommended instance. This matches how prod already handles the
name-taint-isolated `r7a-large` fleet. Operator changes required:

- New `modules/nodepools/defs/<subfleet>.yaml` file per sub-fleet.
- Per-def `node_fleet` field updated to the target sub-fleet name.
- No sim changes needed — sub-fleets are just fleet YAMLs the sim already
  loads via `_load_fleet_specs`.

Chosen over Option A (threading `label` through `pick_instance` for per-def
overrides) because it matches prod deployment patterns and requires zero
sim-code changes.

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
   every in-scope family size that a Phase 1 catalog would enumerate but is
   not currently present (e.g. r7a.2xl, r7a.4xl). Fill `memory_mi` via
   `collect_instance_memory.py` where a live node exists; otherwise use the
   0.925 estimate as documented.
5. Compute per-fleet theoretical util ceiling from the Phase 1 shape catalog
   — the best achievable util assuming perfect packing and current def
   requests. Anchors the "how much room is there" question before any
   search.

### Phase 1 — Shape catalog (analytical, no sim runs)

Deliverable: `scripts/node-size-sweep/optimize_catalog.py`.

- Input: `INSTANCE_SPECS`, `ENI_MAX_PODS`, DaemonSet defs, runner defs.
- Output:
  1. **Shape catalog**: per (family, instance, N), the
     `slot_cpu_m / slot_mem_mi / slot_gpu / overhead_frac / slots_per_node`.
  2. **Per-def eligibility**: per runner def, the eligible shapes and
     waste-per-pod on each dimension.

Runs in seconds. Answers "for r7a defs, what are the viable shapes and their
waste profiles?" and surfaces obvious wins (waste > 30% shapes replaced by
waste < 10% shapes) without any sim run.

### Phase 2 — Sim-driven search

Deliverable: `scripts/node-size-sweep/optimize_search.py`.

For each in-scope family:

1. **Seed configs**: baseline (current config), greedy-tight (each def picks
   the shape maximizing `request / slot`), greedy-dense (each def picks the
   shape with fewest slots per node).
2. **Feasibility gate**: every candidate config (seed or neighbor) is
   checked: does every def in the family have at least one eligible shape
   in the resulting fleet list? Skip if not. Prevents the search from
   entering states where some def has no home.
3. **Multi-restart hill-climb**: `NUM_RESTARTS >= 20`. Neighbor = flip one
   def to a different eligible shape (which may add/remove an instance from
   the fleet). Try all neighbors, keep the best if it improves objective by
   more than `3σ` (σ from Phase 0).
4. **Neutral-move acceptance**: sideways moves (delta within noise floor)
   are accepted with limited budget per restart, to traverse plateaus
   without cycling. Prevents early termination on flat landscapes typical
   of coarse discrete search spaces.
5. **Memoize**: `sim(config) → objective` on config hash. See
   Checkpointing.

Emit to logs: `max(cpu, mem)` objective, cpu util alone, mem util alone,
node-hours. Ranking is `max(cpu, mem)`; node-hours breaks ties.

Expected sim runs per fleet: 50-500 depending on family size and restart
count. Times ~11 families. Total budget target: ~24h wall on a single box
without parallelism; parallelism across families is trivial.

### Phase 3 — Sensitivity analysis

For the top recommendation per fleet:

- Sweep ±1 shape per def and record util delta. If deltas are below noise
  floor, the recommendation is fragile — flag it.
- Sweep runner-container-hooks tax (currently 320m/522Mi) by ±50%. If a
  ±50% change to the hook tax would shift the recommendation, flag as
  "hook-overhead sensitive" — operator should consider hook optimization
  before shape optimization.

### Phase 4 — Deliverable

Per-fleet report AND git-apply-able patches. Report includes:

- Current util (both dimensions).
- Recommended util (both dimensions).
- Theoretical ceiling from Phase 0.
- Noise floor σ from Phase 0.
- Sim-vs-prod delta from Phase 0.
- Per-def rename-required flag from D6.
- Sensitivity flags from Phase 3.
- Rejection cases: fleets where no recommendation beats baseline by > 3σ
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
  / estimated total" where estimate = `num_restarts * avg_neighbors_per_restart *
  avg_convergence_steps` (estimate refined online).
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
    node_hours  REAL NOT NULL,
    computed_at INTEGER NOT NULL
);
```

Key input to the sha256:

```json
{
  "config":       {...},              // {label: {cpu_m, mem_mi, gpu, instance_type, node_fleet}}
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
--num-restarts <N>        default 20
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
  the search harness writes the candidate fleet YAMLs to a temp dir per
  config and points `ClusterModel` at it.

## Implementation details

### Config injection

A candidate configuration is:

```python
Config = dict[str, DefShape]
DefShape = {
    "cpu_m":         int,   # pod request millicores (includes hooks tax)
    "mem_mi":        int,   # pod request MiB (includes hooks tax)
    "gpu":           int,
    "instance_type": str,   # target instance size for this def
    "node_fleet":    str,   # target (possibly virtual) fleet name
}
```

Applied by:
- Writing a `<family>.yaml` (or per-sub-fleet YAML) to a temp defs dir.
- Constructing `ClusterModel(defs_dirs=[<tempdir>, <other-real-dirs>])`.
- Overriding `build_label_table()`'s output for the candidate labels before
  passing into `load_jobs` (or into a variant that accepts an already-built
  label table).

No changes to `simulate.py`'s inner loop. All optimizer logic lives in the
new files.

### Shape catalog generation

```python
def generate_catalog(family: str, defs: list[DefReq]) -> list[Shape]:
    instances = [i for i in INSTANCE_SPECS if i.startswith(f"{family}.")]
    out = []
    for inst in instances:
        alloc = compute_allocatable(inst, ...)
        n_max_by_def = max(
            min(
                alloc.cpu_m // d.cpu_m,
                alloc.mem_mi // d.mem_mi,
                (alloc.gpu // d.gpu) if d.gpu > 0 else 10_000,
            )
            for d in defs
        )
        for n in range(1, n_max_by_def + 1):
            if alloc.gpu > 0 and alloc.gpu % n != 0:
                continue
            slot_cpu = alloc.cpu_m // n
            slot_mem = alloc.mem_mi // n
            slot_gpu = alloc.gpu // n
            if slot_cpu <= 0 or slot_mem <= 0:
                continue
            out.append(Shape(
                instance=inst, n=n,
                slot_cpu_m=slot_cpu, slot_mem_mi=slot_mem, slot_gpu=slot_gpu,
                overhead_frac=1 - (alloc.cpu_m / (INSTANCE_SPECS[inst]["vcpu"] * 1000)),
            ))
    return out
```

### Per-def eligibility

```python
def eligible_shapes(req: DefReq, catalog: list[Shape]) -> list[Shape]:
    return [
        s for s in catalog
        if s.slot_cpu_m >= req.cpu_m
        and s.slot_mem_mi >= req.mem_mi
        and s.slot_gpu >= req.gpu
    ]
```

Waste per shape: `(slot_cpu_m - req.cpu_m, slot_mem_mi - req.mem_mi)`.

### Search harness

```
for family in FAMILIES:
    if state.family_done(family): continue
    catalog = generate_catalog(family, defs[family])
    baseline = load_current(family)
    if not feasible(baseline, catalog): raise ValueError("baseline infeasible")
    best_config, best_obj = baseline, run_sim_cached(baseline)
    for restart in range(NUM_RESTARTS):
        if state.restart_done(family, restart): continue
        state.mark_running(family, restart)
        config = seed(restart, baseline, catalog)  # 0=baseline, 1=tight, 2=dense, N>=3=random
        neutral_budget = NEUTRAL_MAX
        while True:
            neighbors = [n for n in enumerate_neighbors(config, catalog) if feasible(n, catalog)]
            scored = [(n, run_sim_cached(n)) for n in neighbors]
            best_n, best_n_obj = max(scored, key=lambda t: t[1])
            delta = best_n_obj - run_sim_cached(config)
            if delta > 3 * sigma:
                config = best_n
            elif abs(delta) <= sigma and neutral_budget > 0:
                config = best_n
                neutral_budget -= 1
            else:
                break
            state.checkpoint(family, restart, config, ...)
        obj = run_sim_cached(config)
        if obj > best_obj + 3 * sigma:
            best_config, best_obj = config, obj
        state.mark_restart_done(family, restart)
    verdict = "improved" if best_obj > baseline_obj + 3 * sigma else "no-change"
    state.mark_family_done(family, best_config, verdict)
    write_reports_and_patches(family, best_config, baseline)
```

### Neighbor generation

Per-def flip: for each def in the family, for each eligible shape different
from the current, emit `{**config, def: new_shape}`. Neighbors per config
typically 20-100.

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

Labels like `l-x86iamx-8-64` encode "8 vcpu / 64 GiB". Shrinking a def's
`cpu_m` / `mem_mi` produces a label that lies. Renames are cross-repo
coordinated changes.

Mitigation: emit `rename_required=true` per rec when the label encodes the
shape and the shape moved. Do NOT auto-apply patches for renamed defs;
emit a manual-action item with the proposed new label and the list of
downstream `.github/workflows/` files that would need updating.

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
prioritizes what production actually runs. Phase 1's catalog will trip on
missing entries.

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
- **Phase 1**: catalog + eligibility surface at least one obvious win per
  family (waste > 30% shape replaced by waste < 10% shape), with no sim
  runs.
- **Phase 2**: at least three fleets show `max(cpu, mem)` util improvement
  > 3σ vs baseline. Improvement stable across two disjoint validation
  windows (weekdays vs weekends, and first-30 vs last-30 days).
- **Phase 4**: patch files apply cleanly (`git apply --check` passes),
  pass `just lint`, pass `just test`. Reports include current util,
  recommended util, ceiling, noise floor, sim-vs-prod delta, and per-def
  rename flags. Rejection cases explicitly say "no change recommended".
