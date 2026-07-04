"""Search algorithms and sim-invocation wrapper for the node-size optimizer.

Implements the partition + per-partition instance assignment search described in
`optimize.md`. Each candidate config is a mapping

    {sub_nodepool_id: {"instance": aws_size, "pods": [def_label, ...]}}

where sub_nodepool_id is synthetic (`<family>__<instance>`), one per unique
instance choice. Search strategies:

- Exhaustive: enumerate partitions via a set-partition generator, assign each
  block an in-family instance, feasibility-gate against the catalog, sim each
  survivor. Preferred for K <= 5.
- Hill-climb: multi-restart, neighbor moves = move-pod / merge / split /
  change-instance. Preferred for K >= 6.

Pod (cpu_m, mem_mi, gpu) requests are derived deterministically from the
(def, instance) pair via the eligibility catalog — never a search variable.
"""

from __future__ import annotations

import hashlib
import json
import logging
import random
import time
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Iterable

    from optimize_catalog import EligibleEntry
    from sim_nodes import FleetSpec, Job

from optimize_storage import SimCache, SimMetrics

# Ranking is `max(opt_cpu, opt_mem)` per family (opt uses workload-only numerator,
# alloc+DS denominator — see optimize.md D1). vCPU-hours breaks ties (lower wins).

BUCKET_SEC = 300

Config = dict[str, dict]  # sub_nodepool_id -> {"instance": str, "pods": [def_label, ...]}


def sub_nodepool_id(family: str, instance: str) -> str:
    """Synthetic sub-nodepool id: one virtual sub-nodepool per unique instance."""
    return f"{family}__{instance}"


# ---------- config canonicalization / hashing ----------


def canonical_config(config: Config) -> str:
    """Canonical JSON so equivalent configs hash to the same cache key.

    Sub-nodepool ids and pods within each are sorted; keys emitted deterministically.
    """
    normalized = {
        sub_id: {
            "instance": spec["instance"],
            "pods": sorted(spec["pods"]),
        }
        for sub_id, spec in sorted(config.items())
    }
    return json.dumps(normalized, sort_keys=True, separators=(",", ":"))


def config_cache_key(
    family: str,
    config: Config,
    sim_flags: dict,
    csv_sha: str,
    src_shas: dict[str, str],
) -> str:
    payload = {
        "family": family,
        "config": json.loads(canonical_config(config)),
        "sim_flags": sim_flags,
        "csv_sha256": csv_sha,
        "sim_source_shas": src_shas,
    }
    return hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()


# ---------- catalog helpers ----------


def build_family_catalog(entries: list["EligibleEntry"]) -> dict[tuple[str, str], "EligibleEntry"]:
    """(def_label, instance) -> EligibleEntry for O(1) lookup."""
    return {(e.def_label, e.instance): e for e in entries}


def instances_in_catalog(entries: list["EligibleEntry"]) -> list[str]:
    """Unique instances that appear at least once in the family catalog."""
    return sorted({e.instance for e in entries})


def eligible_instances_for_def(entries: list["EligibleEntry"], def_label: str) -> list[str]:
    return sorted({e.instance for e in entries if e.def_label == def_label})


def is_config_feasible(
    config: Config,
    catalog: dict[tuple[str, str], "EligibleEntry"],
) -> bool:
    """Every def in every sub-nodepool must have an EligibleEntry for its instance."""
    for spec in config.values():
        inst = spec["instance"]
        for pod in spec["pods"]:
            if (pod, inst) not in catalog:
                return False
    return True


def is_baseline_feasible(
    config: Config,
    defs: list[dict],
    scoped_daemonsets: list,
) -> bool:
    """Baseline uses PROD-REALITY pod shapes (no D4 adjustment), so it must
    NOT be gated against the recommendation catalog — the recommendation
    catalog rejects (def, instance) pairs whose tight-fit adjustment falls
    outside the D4 bounds, but the baseline runs the pod at its ORIGINAL
    shape with no adjustment. The correct feasibility check for the baseline
    is: does the def's original pod shape physically fit on the instance
    (i.e. N >= 1 after subtracting kubelet + daemonset overhead)?
    """
    from analyze_node_utilization import compute_allocatable

    from optimize_config import def_totals

    defs_by_name = {d["name"]: d for d in defs}
    for spec in config.values():
        inst = spec["instance"]
        alloc = compute_allocatable(inst, scoped_daemonsets)
        if alloc is None:
            return False
        alloc_cpu_m = alloc["allocatable_cpu_m"]
        alloc_mem_mi = alloc["allocatable_mem_mi"]
        alloc_gpu = alloc["allocatable_gpu"]
        for pod in spec["pods"]:
            defrow = defs_by_name.get(pod)
            if defrow is None:
                return False
            orig_cpu_m, orig_mem_mi, orig_gpu, _, _ = def_totals(defrow)
            if orig_cpu_m > alloc_cpu_m or orig_mem_mi > alloc_mem_mi:
                return False
            if orig_gpu > 0 and (alloc_gpu == 0 or orig_gpu > alloc_gpu):
                return False
            if orig_gpu == 0 and alloc_gpu > 0:
                return False
    return True


def baseline_config(family: str, defs: list[dict], catalog_entries: list["EligibleEntry"]) -> Config:
    """Current-prod baseline: pods routed to the REAL nodepool name (from each
    def's `nodepool` field, which is `derive_fleet_name(instance_type, node_fleet)`)
    with the family's largest in-family instance recorded for reference.

    Real nodepool routing is what prod actually does — a single nodepool per
    family with weighted Karpenter instance selection across sizes. Using the
    real pool name here keeps per-family baseline sim and cluster-validation
    baseline sim symmetric (both route to the same real pool). When
    `run_sim_for_config` sees this baseline config it drops `fleets_override`
    so `ClusterModel` loads the real weighted fleet from YAML.

    Falls back to a def's own instance_type when the largest isn't eligible for
    that def (should not happen for real prod configs — assert would fire
    elsewhere), keeping baseline uniquely defined.
    """
    if not defs:
        raise ValueError(f"family {family}: no defs, cannot build baseline")

    from fleet_naming import derive_fleet_name

    # Group defs by their real prod nodepool. A single family typically routes
    # every def to the same nodepool (the family name), but defs with an
    # explicit `node_fleet` override may land on a different pool — keep that
    # routing intact so baseline reflects actual prod placement.
    by_pool: dict[str, list[dict]] = {}
    for d in defs:
        pool = derive_fleet_name(d["instance_type"], d.get("node_fleet"))
        by_pool.setdefault(pool, []).append(d)

    prod_instances = {d["instance_type"] for d in defs}
    if len(prod_instances) == 1:
        inst = next(iter(prod_instances))
    else:
        catalog_insts = instances_in_catalog(catalog_entries)
        if not catalog_insts:
            raise ValueError(f"family {family}: catalog empty, cannot build baseline")
        inst = catalog_insts[-1]

    cfg: Config = {}
    for pool_name, pool_defs in by_pool.items():
        cfg[pool_name] = {
            "instance": inst,
            "pods": sorted(d["name"] for d in pool_defs),
        }
    return cfg


# ---------- partition enumeration (exhaustive) ----------


def _iter_set_partitions(items: list[str]) -> "Iterable[list[list[str]]]":
    """Yield every partition of `items` (a list of non-empty subsets whose union = items).

    Uses restricted-growth strings (RGS): for K items, an assignment vector a
    of length K where 0 <= a[i] <= max(a[:i]) + 1. Each RGS maps to exactly one
    partition. Yields lists whose block order matches the ascending first-item-index.
    """
    k = len(items)
    if k == 0:
        yield []
        return
    a = [0] * k
    m = [0] * k  # running max
    while True:
        blocks: dict[int, list[str]] = {}
        for i, block_id in enumerate(a):
            blocks.setdefault(block_id, []).append(items[i])
        yield [blocks[i] for i in sorted(blocks)]
        # Next RGS in lexicographic order.
        i = k - 1
        while i > 0 and a[i] == m[i - 1] + 1:
            a[i] = 0
            i -= 1
        if i == 0:
            return
        a[i] += 1
        # RGS invariant: m[i] = max(a[0..i]). Both m[i] and every m[j>i]
        # must be updated after bumping a[i].
        m[i] = max(m[i - 1], a[i])
        for j in range(i + 1, k):
            m[j] = m[i]


def _enumerate_configs_for_partition(
    family: str,
    blocks: list[list[str]],
    catalog: dict[tuple[str, str], "EligibleEntry"],
    per_def_instances: dict[str, list[str]],
) -> "Iterable[Config]":
    """For each block, pick a common instance eligible for all its defs. Yield
    configs; prune infeasible blocks early (empty intersection)."""
    # Pre-intersect each block's eligible instance set.
    block_options: list[list[str]] = []
    for block in blocks:
        opts: set[str] | None = None
        for pod in block:
            elig = set(per_def_instances.get(pod, ()))
            opts = elig if opts is None else (opts & elig)
            if not opts:
                return
        assert opts is not None
        block_options.append(sorted(opts))

    def _recurse(bi: int, chosen: list[str]) -> "Iterable[Config]":
        if bi == len(blocks):
            # Duplicate instance across blocks -> two virtual sub-nodepools with
            # the same instance. Per D7 (one instance per sub-nodepool), that's
            # a legitimate — if wasteful — split. Merge them so the config is
            # canonical and the search doesn't waste sim runs on redundant
            # configs; the merge move covers the same territory.
            merged: dict[str, dict] = {}
            for inst, block in zip(chosen, blocks, strict=True):
                sid = sub_nodepool_id(family, inst)
                if sid in merged:
                    merged[sid]["pods"].extend(block)
                else:
                    merged[sid] = {"instance": inst, "pods": list(block)}
            for spec in merged.values():
                spec["pods"].sort()
            yield merged
            return
        for inst in block_options[bi]:
            chosen.append(inst)
            yield from _recurse(bi + 1, chosen)
            chosen.pop()

    seen: set[str] = set()
    for cfg in _recurse(0, []):
        if not is_config_feasible(cfg, catalog):
            continue
        key = canonical_config(cfg)
        if key in seen:
            continue
        seen.add(key)
        yield cfg


def enumerate_feasible_configs(
    family: str,
    defs: list[dict],
    catalog_entries: list["EligibleEntry"],
    limit: int | None = None,
) -> tuple[list[Config], bool]:
    """Every feasible config for a family. Returns (configs, capped_flag)."""
    catalog = build_family_catalog(catalog_entries)
    per_def_instances = {d["name"]: eligible_instances_for_def(catalog_entries, d["name"]) for d in defs}
    items = sorted(d["name"] for d in defs)
    out: list[Config] = []
    seen: set[str] = set()
    for partition in _iter_set_partitions(items):
        for cfg in _enumerate_configs_for_partition(family, partition, catalog, per_def_instances):
            key = canonical_config(cfg)
            if key in seen:
                continue
            seen.add(key)
            out.append(cfg)
            if limit is not None and len(out) > limit:
                return out, True
    return out, False


# ---------- hill-climb neighbor moves ----------


def config_copy(config: Config) -> Config:
    return {sid: {"instance": spec["instance"], "pods": list(spec["pods"])} for sid, spec in config.items()}


_config_copy = config_copy


def _neighbors_move_pod(
    family: str,
    config: Config,
    per_def_instances: dict[str, list[str]],
) -> "Iterable[Config]":
    """Reassign one def from its current sub-nodepool to another (existing or new)."""
    sub_ids = sorted(config)
    for sid in sub_ids:
        pods = list(config[sid]["pods"])
        for pod in pods:
            # (a) move to a different EXISTING sub-nodepool if the pod fits there.
            for other in sub_ids:
                if other == sid:
                    continue
                other_inst = config[other]["instance"]
                if other_inst not in per_def_instances.get(pod, ()):
                    continue
                new = _config_copy(config)
                new[sid]["pods"].remove(pod)
                new[other]["pods"].append(pod)
                new[other]["pods"].sort()
                if not new[sid]["pods"]:
                    del new[sid]
                yield new
            # (b) move to a NEW singleton sub-nodepool, one per eligible instance.
            for inst in per_def_instances.get(pod, ()):
                new = _config_copy(config)
                new[sid]["pods"].remove(pod)
                new_sid = sub_nodepool_id(family, inst)
                if new_sid in new:
                    if pod not in new[new_sid]["pods"]:
                        new[new_sid]["pods"].append(pod)
                        new[new_sid]["pods"].sort()
                else:
                    new[new_sid] = {"instance": inst, "pods": [pod]}
                if not new[sid]["pods"]:
                    del new[sid]
                yield new


def _neighbors_merge(
    family: str,
    config: Config,
    per_def_instances: dict[str, list[str]],
) -> "Iterable[Config]":
    """Merge two sub-nodepools; the merged pool's instance must be eligible for every merged def."""
    sub_ids = sorted(config)
    for i, sid_a in enumerate(sub_ids):
        for sid_b in sub_ids[i + 1 :]:
            pods = config[sid_a]["pods"] + config[sid_b]["pods"]
            # Compute common eligible instances across every merged def.
            common: set[str] | None = None
            for pod in pods:
                elig = set(per_def_instances.get(pod, ()))
                common = elig if common is None else (common & elig)
                if not common:
                    break
            if not common:
                continue
            for inst in sorted(common):
                new = _config_copy(config)
                del new[sid_a]
                del new[sid_b]
                merged_sid = sub_nodepool_id(family, inst)
                if merged_sid in new:
                    new[merged_sid]["pods"].extend(pods)
                    new[merged_sid]["pods"] = sorted(set(new[merged_sid]["pods"]))
                else:
                    new[merged_sid] = {"instance": inst, "pods": sorted(pods)}
                yield new


def _neighbors_split(
    family: str,
    config: Config,
    per_def_instances: dict[str, list[str]],
) -> "Iterable[Config]":
    """Peel a non-empty proper subset out of a sub-nodepool into a new one."""
    sub_ids = sorted(config)
    for sid in sub_ids:
        pods = list(config[sid]["pods"])
        if len(pods) < 2:
            continue
        n = len(pods)
        # Enumerate every non-empty proper subset. The subset and its
        # complement are NOT symmetric here: they yield different configs
        # because the remainder stays on the source instance while the
        # subset moves to a (potentially different) target instance.
        # Duplicate configs are filtered by the canonical `seen` set in
        # enumerate_neighbors, not by mask arithmetic.
        for mask in range(1, (1 << n) - 1):
            subset = [pods[i] for i in range(n) if (mask >> i) & 1]
            remainder = [pods[i] for i in range(n) if not (mask >> i) & 1]
            common: set[str] | None = None
            for pod in subset:
                elig = set(per_def_instances.get(pod, ()))
                common = elig if common is None else (common & elig)
                if not common:
                    break
            if not common:
                continue
            for inst in sorted(common):
                new_sid = sub_nodepool_id(family, inst)
                # A "split" that lands the subset on the same instance as the
                # source is degenerate — same config with re-shuffled pods.
                # Skip to avoid clogging the neighbor list.
                if new_sid == sid:
                    continue
                new = _config_copy(config)
                new[sid]["pods"] = sorted(remainder)
                if new_sid in new:
                    new[new_sid]["pods"] = sorted(set(new[new_sid]["pods"] + subset))
                else:
                    new[new_sid] = {"instance": inst, "pods": sorted(subset)}
                yield new


def _neighbors_change_instance(
    family: str,
    config: Config,
    per_def_instances: dict[str, list[str]],
) -> "Iterable[Config]":
    """Swap one sub-nodepool's instance for another eligible for all its defs."""
    sub_ids = sorted(config)
    for sid in sub_ids:
        pods = config[sid]["pods"]
        cur_inst = config[sid]["instance"]
        common: set[str] | None = None
        for pod in pods:
            elig = set(per_def_instances.get(pod, ()))
            common = elig if common is None else (common & elig)
            if not common:
                break
        if not common:
            continue
        for inst in sorted(common):
            if inst == cur_inst:
                continue
            new = _config_copy(config)
            del new[sid]
            new_sid = sub_nodepool_id(family, inst)
            if new_sid in new:
                new[new_sid]["pods"] = sorted(set(new[new_sid]["pods"] + pods))
            else:
                new[new_sid] = {"instance": inst, "pods": list(pods)}
            yield new


def enumerate_neighbors(
    family: str,
    config: Config,
    defs: list[dict],
    catalog_entries: list["EligibleEntry"],
) -> list[Config]:
    per_def_instances = {d["name"]: eligible_instances_for_def(catalog_entries, d["name"]) for d in defs}
    catalog = build_family_catalog(catalog_entries)
    seen: set[str] = {canonical_config(config)}
    out: list[Config] = []
    for gen in (
        _neighbors_move_pod(family, config, per_def_instances),
        _neighbors_merge(family, config, per_def_instances),
        _neighbors_split(family, config, per_def_instances),
        _neighbors_change_instance(family, config, per_def_instances),
    ):
        for cfg in gen:
            if not cfg:
                continue
            if not is_config_feasible(cfg, catalog):
                continue
            key = canonical_config(cfg)
            if key in seen:
                continue
            seen.add(key)
            out.append(cfg)
    return out


def random_feasible_config(
    family: str,
    defs: list[dict],
    catalog_entries: list["EligibleEntry"],
    rng: random.Random,
) -> Config:
    """Random partition: each def independently picks one of its eligible instances.
    Defs that land on the same instance share a sub-nodepool."""
    catalog = build_family_catalog(catalog_entries)
    per_def_instances = {d["name"]: eligible_instances_for_def(catalog_entries, d["name"]) for d in defs}
    grouped: dict[str, list[str]] = {}
    for d in defs:
        name = d["name"]
        elig = per_def_instances.get(name, [])
        if not elig:
            continue
        inst = rng.choice(elig)
        grouped.setdefault(inst, []).append(name)
    cfg: Config = {}
    for inst, pods in grouped.items():
        cfg[sub_nodepool_id(family, inst)] = {"instance": inst, "pods": sorted(pods)}
    if not is_config_feasible(cfg, catalog):
        raise RuntimeError(f"random_feasible_config: generated infeasible config for {family}")
    return cfg


# ---------- sim wrapper ----------


def rebuild_jobs_for_config(
    family: str,
    config: Config,
    all_jobs: list["Job"],
    catalog: dict[tuple[str, str], "EligibleEntry"],
    family_def_names: set[str],
    baseline_defs: list[dict] | None = None,
) -> list["Job"]:
    """Rewrite each family job's pool/shape per its def's config assignment.

    Non-family jobs and runner-pod entries are dropped — c7i-runner is
    zero-coupled to workflow-fleet choices (D3) so they are pure sim overhead
    for the family's per-family metric extraction.

    When `baseline_defs` is provided the config is treated as the prod-reality
    baseline: jobs keep their ORIGINAL (unadjusted) pod shape from
    def_totals; the catalog is only consulted for pool routing / gpu counts.
    Baselines must never inherit D4 slot adjustments — those are the
    recommendation's shape, not prod's.
    """
    from sim_load import RUNNER_POD_LABEL
    from sim_nodes import Job

    from optimize_config import def_totals

    # Reverse map: def_label -> (sub_id, instance) — assumes each def is in
    # exactly one sub-nodepool (invariant of the config schema).
    assignment: dict[str, tuple[str, str]] = {}
    for sub_id, spec in config.items():
        inst = spec["instance"]
        for pod in spec["pods"]:
            assignment[pod] = (sub_id, inst)

    baseline_shapes: dict[str, tuple[int, int, int]] = {}
    if baseline_defs is not None:
        for d in baseline_defs:
            cpu_m, mem_mi, gpu, _, _ = def_totals(d)
            baseline_shapes[d["name"]] = (cpu_m, mem_mi, gpu)

    out: list["Job"] = []
    for j in all_jobs:
        if j.label == RUNNER_POD_LABEL:
            continue
        if j.label not in family_def_names:
            continue
        assigned = assignment.get(j.label)
        if assigned is None:
            continue
        sub_id, inst = assigned
        if baseline_defs is not None:
            shape = baseline_shapes.get(j.label)
            if shape is None:
                continue
            cpu_m, mem_mi, gpu = shape
            out.append(
                Job(
                    label=j.label,
                    pool=sub_id,
                    cpu_m=cpu_m,
                    mem_mi=mem_mi,
                    gpu=gpu,
                    start_bucket=j.start_bucket,
                    end_bucket=j.end_bucket,
                )
            )
            continue
        entry = catalog.get((j.label, inst))
        if entry is None:
            continue
        out.append(
            Job(
                label=j.label,
                pool=sub_id,
                cpu_m=entry.slot_cpu_m,
                mem_mi=entry.slot_mem_mi,
                gpu=entry.slot_gpu,
                start_bucket=j.start_bucket,
                end_bucket=j.end_bucket,
            )
        )
    return out


def build_fleets_override(
    config: Config,
    runner_fleet: "FleetSpec",
) -> dict[str, "FleetSpec"]:
    """One FleetSpec per sub-nodepool (single-instance) + preserved c7i-runner."""
    from sim_load import RUNNER_POD_POOL
    from sim_nodes import FleetSpec

    fleets: dict[str, "FleetSpec"] = {RUNNER_POD_POOL: runner_fleet}
    for sub_id, spec in config.items():
        inst = spec["instance"]
        from instance_specs import INSTANCE_SPECS

        is_gpu = bool(INSTANCE_SPECS.get(inst, {}).get("gpu", 0) > 0)
        fleets[sub_id] = FleetSpec(name=sub_id, is_gpu=is_gpu, instances=(inst,))
    return fleets


def _extract_family_metrics(
    sim_out: dict,
    family: str,
    config: Config,
    daemonsets: list | None = None,
) -> SimMetrics:
    """opt_cpu/opt_mem/opt_max/cal_cpu/cal_mem/vcpu_hours restricted to
    the family's virtual sub-pools (those matching `<family>__` prefix).

    Per spec D1: opt/cal metrics are allocatable-weighted (sum numerator /
    sum denominator across buckets), NOT per-bucket ratio means. `opt_max`
    is then `max(opt_cpu, opt_mem)` at family level, after aggregation.

    vCPU-hours proxies cost: sum of (post-kubelet-pre-DS) allocatable vCPU
    millicores across buckets, converted to vCPU-hours. Instance-size-invariant
    WITHIN a family and roughly proportional to $/hr — 1h × 192 vCPU on a
    r7a.48xl equals 12h × 16 vCPU on a r7a.4xl in raw compute AND in cost.
    """
    # BUCKET_SEC / 3600 = 1/12; converts allocatable-vcpu-millicores per bucket
    # to vcpu-hours. daemonsets kept in signature for interface stability with
    # callers that still pass it; the value is not needed for vcpu-hour math.
    del daemonsets

    prefix = f"{family}__"
    sub_ids = set(config.keys())

    sum_workload_cpu = 0
    sum_workload_mem = 0
    sum_alloc_cpu = 0  # allocatable + ds (opt denominator)
    sum_alloc_mem = 0
    sum_cal_used_cpu = 0
    sum_cal_used_mem = 0
    sum_cal_alloc_cpu = 0
    sum_cal_alloc_mem = 0

    for _t, per_pool in sim_out["per_bucket"]:
        for name, sums in per_pool.items():
            if name not in sub_ids and not name.startswith(prefix):
                continue
            alloc_cpu = sums["alloc_cpu_m_raw"] + sums["ds_cpu_m"]
            alloc_mem = sums["alloc_mem_mi_raw"] + sums["ds_mem_mi"]
            if alloc_cpu <= 0 and alloc_mem <= 0:
                continue
            sum_workload_cpu += sums["workload_cpu_m"]
            sum_workload_mem += sums["workload_mem_mi"]
            sum_alloc_cpu += alloc_cpu
            sum_alloc_mem += alloc_mem
            sum_cal_used_cpu += sums["cpu_used_m"]
            sum_cal_used_mem += sums["mem_used_mi"]
            sum_cal_alloc_cpu += sums["cpu_alloc_m"]
            sum_cal_alloc_mem += sums["mem_alloc_mi"]

    opt_cpu = sum_workload_cpu / sum_alloc_cpu if sum_alloc_cpu > 0 else 0.0
    opt_mem = sum_workload_mem / sum_alloc_mem if sum_alloc_mem > 0 else 0.0
    opt_max = max(opt_cpu, opt_mem)
    cal_cpu = sum_cal_used_cpu / sum_cal_alloc_cpu if sum_cal_alloc_cpu > 0 else 0.0
    cal_mem = sum_cal_used_mem / sum_cal_alloc_mem if sum_cal_alloc_mem > 0 else 0.0

    vcpu_hours = sum_alloc_cpu / 1000.0 * (BUCKET_SEC / 3600.0)

    return SimMetrics(
        opt_max=opt_max,
        opt_cpu=opt_cpu,
        opt_mem=opt_mem,
        cal_cpu=cal_cpu,
        cal_mem=cal_mem,
        vcpu_hours=vcpu_hours,
    )


# ---------- cluster-wide sim helpers ----------


def apply_recommendations_to_jobs(
    jobs: list["Job"],
    overrides: dict[str, dict],
) -> list["Job"]:
    """Rewrite jobs whose label is in `overrides` to the recommended
    pool/cpu_m/mem_mi/gpu; pass others through unchanged.

    Runner pods and any job whose def label has no override keep their
    original pool and shape — the cluster sim needs the full workload mix.
    """
    from sim_nodes import Job

    out: list["Job"] = []
    for j in jobs:
        ov = overrides.get(j.label)
        if ov is None:
            out.append(j)
            continue
        out.append(
            Job(
                label=j.label,
                pool=ov["pool"],
                cpu_m=ov["cpu_m"],
                mem_mi=ov["mem_mi"],
                gpu=ov["gpu"],
                start_bucket=j.start_bucket,
                end_bucket=j.end_bucket,
            )
        )
    return out


def build_cluster_fleets_extra(
    family_results: list,
) -> dict[str, "FleetSpec"]:
    """For every improved family, materialize a FleetSpec per sub_nodepool_id
    in its best_config. Merged INTO the default cluster fleets via
    ClusterModel(fleets_extra=...) so unchanged fleets keep their YAML shape.
    """
    from instance_specs import INSTANCE_SPECS
    from sim_nodes import FleetSpec

    out: dict[str, "FleetSpec"] = {}
    for r in family_results:
        if getattr(r, "verdict", None) != "improved":
            continue
        cfg = getattr(r, "best_config", None)
        if cfg is None:
            continue
        for sub_id, spec in cfg.items():
            inst = spec["instance"]
            is_gpu = bool(INSTANCE_SPECS.get(inst, {}).get("gpu", 0) > 0)
            out[sub_id] = FleetSpec(name=sub_id, is_gpu=is_gpu, instances=(inst,))
    return out


def _accumulate_pool_sums(sim_out: dict, pool_filter: set[str] | None) -> SimMetrics:
    """Aggregate D1 metrics across per-bucket per-pool sums.

    `pool_filter=None` sums every pool (cluster-wide). A non-None set restricts
    to the named pools (per-family contribution).
    """
    sum_workload_cpu = 0
    sum_workload_mem = 0
    sum_alloc_cpu = 0
    sum_alloc_mem = 0
    sum_cal_used_cpu = 0
    sum_cal_used_mem = 0
    sum_cal_alloc_cpu = 0
    sum_cal_alloc_mem = 0

    for _t, per_pool in sim_out["per_bucket"]:
        for name, sums in per_pool.items():
            if pool_filter is not None and name not in pool_filter:
                continue
            alloc_cpu = sums["alloc_cpu_m_raw"] + sums["ds_cpu_m"]
            alloc_mem = sums["alloc_mem_mi_raw"] + sums["ds_mem_mi"]
            if alloc_cpu <= 0 and alloc_mem <= 0:
                continue
            sum_workload_cpu += sums["workload_cpu_m"]
            sum_workload_mem += sums["workload_mem_mi"]
            sum_alloc_cpu += alloc_cpu
            sum_alloc_mem += alloc_mem
            sum_cal_used_cpu += sums["cpu_used_m"]
            sum_cal_used_mem += sums["mem_used_mi"]
            sum_cal_alloc_cpu += sums["cpu_alloc_m"]
            sum_cal_alloc_mem += sums["mem_alloc_mi"]

    opt_cpu = sum_workload_cpu / sum_alloc_cpu if sum_alloc_cpu > 0 else 0.0
    opt_mem = sum_workload_mem / sum_alloc_mem if sum_alloc_mem > 0 else 0.0
    opt_max = max(opt_cpu, opt_mem)
    cal_cpu = sum_cal_used_cpu / sum_cal_alloc_cpu if sum_cal_alloc_cpu > 0 else 0.0
    cal_mem = sum_cal_used_mem / sum_cal_alloc_mem if sum_cal_alloc_mem > 0 else 0.0
    vcpu_hours = sum_alloc_cpu / 1000.0 * (BUCKET_SEC / 3600.0)

    return SimMetrics(
        opt_max=opt_max,
        opt_cpu=opt_cpu,
        opt_mem=opt_mem,
        cal_cpu=cal_cpu,
        cal_mem=cal_mem,
        vcpu_hours=vcpu_hours,
    )


def extract_cluster_metrics(sim_out: dict) -> SimMetrics:
    """D1 metrics summed across every pool in `sim_out`."""
    return _accumulate_pool_sums(sim_out, pool_filter=None)


def extract_family_contribution_metrics(sim_out: dict, pool_names: set[str]) -> SimMetrics:
    """D1 metrics restricted to `pool_names` — one family's share of a
    cluster-wide sim."""
    return _accumulate_pool_sums(sim_out, pool_filter=pool_names)


def run_cluster_sim(
    jobs: list["Job"],
    fleets_extra: dict[str, "FleetSpec"] | None,
    sim_flags: dict,
) -> dict:
    """Full-cluster sim: preserves default YAML fleets, merges `fleets_extra`
    on top, feeds `jobs` to simulate() with the standard sim_flags pattern.

    Returns the raw sim_out dict so the caller can extract cluster-wide AND
    per-family contribution metrics from a single sim.
    """
    import simulate as sim_mod
    from sim_nodes import ClusterModel

    if not jobs:
        raise ValueError("run_cluster_sim: empty jobs list — nothing to simulate")

    model = ClusterModel(fleets_extra=fleets_extra)
    return sim_mod.simulate(
        jobs,
        model=model,
        seed=sim_flags["seed"],
        empty_ttl_buckets=sim_flags["empty_ttl_buckets"],
        placeholder_max_age=sim_flags["placeholder_max_age"],
        warmup_buckets_default=sim_flags["warmup_default"],
        warmup_buckets_gpu=sim_flags["warmup_gpu"],
        warmup_buckets_baremetal=sim_flags["warmup_baremetal"],
        placeholders_enabled=sim_flags["placeholders_enabled"],
        daemonsets_in_metric=sim_flags["daemonsets_in_metric"],
        phantom_pods_enabled=sim_flags["phantom_pods_enabled"],
        progress=False,
    )


def run_sim_for_config(
    family: str,
    config: Config,
    all_jobs: list["Job"],
    catalog: dict[tuple[str, str], "EligibleEntry"],
    family_def_names: set[str],
    runner_fleet: "FleetSpec",
    sim_flags: dict,
    daemonsets: list | None = None,
    baseline_defs: list[dict] | None = None,
) -> SimMetrics:
    import simulate as sim_mod
    from sim_nodes import ClusterModel

    jobs = rebuild_jobs_for_config(family, config, all_jobs, catalog, family_def_names, baseline_defs=baseline_defs)
    # simulate() blows up on min(...) over empty arrivals; short-circuit here.
    if not jobs:
        return SimMetrics(0.0, 0.0, 0.0, 0.0, 0.0, 0.0, empty=True)
    # Baseline routes to REAL prod nodepool names (see `baseline_config`), so
    # the sim must load the real weighted YAML fleet — passing a
    # single-instance override would silently constrain Karpenter's picker to
    # one size and break parity with prod. Recommendation configs use
    # synthetic sub-nodepool ids, one instance each, so the override is
    # required to materialize them.
    if baseline_defs is not None:
        model = ClusterModel()
    else:
        fleets_override = build_fleets_override(config, runner_fleet)
        model = ClusterModel(fleets_override=fleets_override)
    t0 = time.perf_counter()
    sim_out = sim_mod.simulate(
        jobs,
        model=model,
        seed=sim_flags["seed"],
        empty_ttl_buckets=sim_flags["empty_ttl_buckets"],
        placeholder_max_age=sim_flags["placeholder_max_age"],
        warmup_buckets_default=sim_flags["warmup_default"],
        warmup_buckets_gpu=sim_flags["warmup_gpu"],
        warmup_buckets_baremetal=sim_flags["warmup_baremetal"],
        placeholders_enabled=sim_flags["placeholders_enabled"],
        daemonsets_in_metric=sim_flags["daemonsets_in_metric"],
        phantom_pods_enabled=sim_flags["phantom_pods_enabled"],
        progress=False,
    )
    elapsed = time.perf_counter() - t0
    m = _extract_family_metrics(sim_out, family, config, daemonsets=daemonsets)
    return SimMetrics(
        opt_max=m.opt_max,
        opt_cpu=m.opt_cpu,
        opt_mem=m.opt_mem,
        cal_cpu=m.cal_cpu,
        cal_mem=m.cal_mem,
        vcpu_hours=m.vcpu_hours,
        elapsed_s=elapsed,
    )


def cached_sim(
    family: str,
    config: Config,
    all_jobs: list["Job"],
    catalog: dict[tuple[str, str], "EligibleEntry"],
    family_def_names: set[str],
    runner_fleet: "FleetSpec",
    sim_flags: dict,
    csv_sha: str,
    src_shas: dict[str, str],
    cache: SimCache,
    log: logging.Logger,
    daemonsets: list | None = None,
    baseline_defs: list[dict] | None = None,
) -> tuple[SimMetrics, bool]:
    """Returns (metrics, was_cache_hit). Callers should increment their hit
    counter on the boolean rather than probing cache.get() twice."""
    # Baseline sims run with pod ORIGINAL shape, not catalog slot shape — they
    # must not collide with recommendation-cache entries for the same config.
    cache_flags = dict(sim_flags)
    if baseline_defs is not None:
        cache_flags["_baseline_shape"] = True
    key = config_cache_key(family, config, cache_flags, csv_sha, src_shas)
    hit = cache.get(key)
    if hit is not None:
        log.debug("cache HIT %s: opt_max=%.4f", key[:12], hit.opt_max)
        return hit, True
    log.debug("cache MISS %s — running sim", key[:12])
    m = run_sim_for_config(
        family,
        config,
        all_jobs,
        catalog,
        family_def_names,
        runner_fleet,
        sim_flags,
        daemonsets=daemonsets,
        baseline_defs=baseline_defs,
    )
    cache.put(key, canonical_config(config), m)
    log.debug("cache STORE %s: opt_max=%.4f elapsed=%.1fs", key[:12], m.opt_max, m.elapsed_s)
    return m, False


def rank_key(m: SimMetrics) -> tuple[float, float]:
    """Ranking: (opt_max, -vcpu_hours). Lower vCPU-hours wins ties."""
    return (m.opt_max, -m.vcpu_hours)


# ---------- result dataclass ----------


@dataclass
class FamilyResult:
    family: str
    baseline_config: Config
    baseline_metrics: SimMetrics | None
    best_config: Config | None
    best_metrics: SimMetrics | None
    verdict: str
    skipped_reason: str | None = None
    configs_evaluated: int = 0
    elapsed_sec: float = 0.0
    restarts_run: int = 0
    cache_hit_rate: float = 0.0
    mode: str = ""
    per_def_shapes: dict = field(default_factory=dict)
    # Per-family SHARE of the single cluster-wide before/after sim run in the
    # cluster validation phase. Extracted by prefix-filtering the cluster sim
    # outputs (baseline uses the family's original nodepool names; rec uses
    # the family's sub_nodepool_ids from best_config).
    cluster_baseline_metrics: SimMetrics | None = None
    cluster_rec_metrics: SimMetrics | None = None
