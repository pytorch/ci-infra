"""Phase 2.5: cheapest instance to host the fixed-shape ARC runner pods.

The runner pod is a fixed 750m/1Gi Guaranteed-QoS pod paired with every CI job
on the dedicated ``c7i-runner`` fleet. At 1.33 GiB/vCPU of demand against every
non-GPU instance's >= 2.0 GiB/vCPU of allocatable memory, CPU binds and memory
never binds for this shape. Per-node capacity is the binding constraint of the two:

    slots = min(allocatable_cpu_m // runner_cpu_m, allocatable_mem_mi // runner_mem_mi)

so this phase is a DETERMINISTIC CLOSED-FORM, not a stochastic packing sim. It
builds the runner-pod concurrency timeline, divides by per-node slots, applies
Karpenter's 3h empty-node consolidation lag as a look-back sliding-window-max,
and ranks candidate instances by node-hours * price. Results are relative to the
current baseline (c7i.48xlarge, the weight-100 member of the c7i-runner fleet).
"""

from __future__ import annotations

import math
import sys
from collections import defaultdict, deque
from dataclasses import dataclass, field
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(REPO_ROOT / "scripts" / "python"))

import optimize_pricing as pricing  # noqa: E402
from instance_specs import INSTANCE_SPECS  # noqa: E402
from runner_hooks import load_runner_overhead  # noqa: E402
from sim_load import RUNNER_POD_POOL  # noqa: E402
from sim_nodes import ClusterModel, Job  # noqa: E402
from simulate import BUCKET_SEC  # noqa: E402

BASELINE_INSTANCE = "c7i.48xlarge"

# consolidateAfter: 3h on every c7i-runner NodePool (modules/nodepools/generated/
# c7i-runner-*.yaml). An empty node lingers up to 3h before Karpenter terminates
# it, so a node is alive at bucket t iff there was demand within the trailing 3h
# window [t-35, t] — a 36-bucket look-back max over the raw per-bucket node counts.
CONSOLIDATION_LAG_SECONDS = 3 * 3600
CONSOLIDATION_WINDOW_BUCKETS = math.ceil(CONSOLIDATION_LAG_SECONDS / BUCKET_SEC)

# Burstable families are the wrong instance class for steady runner-pod load
# (CPU-credit throttling under sustained use), so they are dropped from ranking.
BURSTABLE_FAMILIES: frozenset[str] = frozenset({"t4g"})

DEPLOYABLE_ARCH = "amd64"
ARM_CAVEAT = "requires multi-arch runner-orchestrator image + runner-hooks-warmer DaemonSet — not yet deployable"


@dataclass(frozen=True)
class CandidateResult:
    instance_type: str
    family: str
    arch: str
    slots: int
    node_hours: float
    blended_price: float | None
    blended_cost: float | None
    region_prices: dict[str, float | None]
    region_costs: dict[str, float | None]
    cost_ratio: float | None


@dataclass(frozen=True)
class RegionWinner:
    region: str
    instance_type: str
    cost: float


@dataclass(frozen=True)
class RunnerFleetResult:
    arch_allowlist: tuple[str, ...]
    peak_concurrency: int
    total_buckets: int
    window_buckets: int
    runner_cpu_m: int
    runner_mem_mi: int
    baseline_instance: str
    baseline_slots: int
    baseline_node_hours: float
    baseline_blended_cost: float | None
    ranked: list[CandidateResult] = field(default_factory=list)
    best_amd64: CandidateResult | None = None
    best_arm64: CandidateResult | None = None
    region_winners: list[RegionWinner] = field(default_factory=list)
    global_winner: CandidateResult | None = None
    global_cost: float | None = None
    per_region_optimal_total: float | None = None
    global_penalty_abs: float | None = None
    global_penalty_ratio: float | None = None


def _resolve_runner_shape(jobs: list[Job], runner_pool: str) -> tuple[int, int]:
    """(cpu_m, mem_mi) of one runner pod — from the jobs' own runner pods if present.

    Deriving from the loaded runner pods keeps this consistent with whatever
    shape the caller's sim_load synthesized; the live-YAML loader is only a
    fallback for job sets that carry no runner pods.
    """
    for j in jobs:
        if j.pool == runner_pool:
            return j.cpu_m, j.mem_mi
    _, _, runner_cpu_m, runner_mem_mi = load_runner_overhead()
    return runner_cpu_m, runner_mem_mi


def build_concurrency_timeline(jobs: list[Job], runner_pool: str) -> list[int]:
    """Runner pods alive per 300s bucket, half-open [start_bucket, end_bucket).

    Matches simulate.py's measurement: a pod is placed at start_bucket (step 4)
    and deprovisioned at the start of end_bucket (step 2) before that bucket is
    measured, so it is live exactly in buckets start_bucket <= t < end_bucket.
    """
    intervals = [(j.start_bucket, j.end_bucket) for j in jobs if j.pool == runner_pool]
    if not intervals:
        intervals = [(j.start_bucket, j.end_bucket) for j in jobs]
    if not intervals:
        return []
    delta: dict[int, int] = defaultdict(int)
    for start, end in intervals:
        if end <= start:
            continue
        delta[start] += 1
        delta[end] -= 1
    t_first = min(s for s, _ in intervals)
    t_last = max(e for _, e in intervals)
    running = 0
    timeline: list[int] = []
    for t in range(t_first, t_last + BUCKET_SEC, BUCKET_SEC):
        running += delta.get(t, 0)
        timeline.append(running)
    return timeline


def _sliding_window_max(values: list[int], window: int) -> list[int]:
    """out[i] = max(values[max(0, i-window+1) .. i]) — a look-BACK window."""
    if window <= 1:
        return list(values)
    out: list[int] = []
    dq: deque[int] = deque()
    for i, v in enumerate(values):
        while dq and values[dq[-1]] <= v:
            dq.pop()
        dq.append(i)
        if dq[0] <= i - window:
            dq.popleft()
        out.append(values[dq[0]])
    return out


def node_hours_for_slots(
    concurrent: list[int],
    slots: int,
    window: int,
    proactive_floor_pods: int = 0,
) -> float:
    """Closed-form node-hours: ceil(concurrent/slots), 3h lag, then sum * hours/bucket."""
    if slots < 1 or not concurrent:
        return 0.0
    nodes_raw = [-(-c // slots) for c in concurrent]
    nodes = _sliding_window_max(nodes_raw, window)
    floor_nodes = -(-proactive_floor_pods // slots) if proactive_floor_pods > 0 else 0
    if floor_nodes > 0:
        nodes = [max(n, floor_nodes) for n in nodes]
    return sum(nodes) * (BUCKET_SEC / 3600.0)


def _slot_counts(model: ClusterModel, runner_pool: str, instance_type: str, cpu_m: int, mem_mi: int) -> tuple[int, int]:
    """(cpu_slots, mem_slots) for one runner pod on an instance; a zero request yields 0 slots."""
    alloc = model.allocatable(runner_pool, instance_type)
    cpu_slots = alloc.cpu_m // cpu_m if cpu_m > 0 else 0
    mem_slots = alloc.mem_mi // mem_mi if mem_mi > 0 else 0
    return cpu_slots, mem_slots


def runner_slots(model: ClusterModel, runner_pool: str, instance_type: str, cpu_m: int, mem_mi: int) -> int:
    """Runner pods per node = the binding constraint min(cpu_slots, mem_slots); 750m/1Gi binds on CPU."""
    cpu_slots, mem_slots = _slot_counts(model, runner_pool, instance_type, cpu_m, mem_mi)
    return min(cpu_slots, mem_slots)


def _candidate_families(arch_allowlist: tuple[str, ...], prices: dict | None) -> set[str]:
    """Union of per-arch top families. Selecting per arch (not jointly) keeps each
    arch's cheapest families represented — a joint call lets one arch's families
    crowd out the other's, hiding the actually-cheapest instance for that arch."""
    families: set[str] = set()
    for arch in arch_allowlist:
        families.update(pricing.select_candidate_families([arch], require_all_regions=True, prices=prices))
    return families


def _candidate_instances(
    arch_allowlist: tuple[str, ...],
    prices: dict | None,
    exclude_burstable: bool,
) -> list[str]:
    families = _candidate_families(arch_allowlist, prices)
    instances: list[str] = []
    for itype, spec in INSTANCE_SPECS.items():
        family = itype.split(".")[0]
        if family not in families:
            continue
        if exclude_burstable and family in BURSTABLE_FAMILIES:
            continue
        if spec["gpu"] != 0:
            continue
        if spec["arch"] not in arch_allowlist:
            continue
        if pricing.blended_price(itype, prices) is None:
            continue
        instances.append(itype)
    return sorted(instances)


def _evaluate_candidate(
    itype: str,
    concurrent: list[int],
    model: ClusterModel,
    runner_pool: str,
    runner_cpu_m: int,
    runner_mem_mi: int,
    window: int,
    proactive_floor_pods: int,
    prices: dict | None,
    baseline_cost: float | None,
) -> CandidateResult | None:
    slots = runner_slots(model, runner_pool, itype, runner_cpu_m, runner_mem_mi)
    if slots < 1:
        return None
    node_hours = node_hours_for_slots(concurrent, slots, window, proactive_floor_pods)
    blended = pricing.blended_price(itype, prices)
    blended_cost = node_hours * blended if blended is not None else None
    region_prices: dict[str, float | None] = {}
    region_costs: dict[str, float | None] = {}
    for region in pricing.REGIONS:
        price = pricing.hourly_price(itype, region, prices) if pricing.region_available(itype, region) else None
        region_prices[region] = price
        region_costs[region] = node_hours * price if price is not None else None
    cost_ratio = (blended_cost / baseline_cost) if (blended_cost is not None and baseline_cost) else None
    return CandidateResult(
        instance_type=itype,
        family=itype.split(".")[0],
        arch=INSTANCE_SPECS[itype]["arch"],
        slots=slots,
        node_hours=node_hours,
        blended_price=blended,
        blended_cost=blended_cost,
        region_prices=region_prices,
        region_costs=region_costs,
        cost_ratio=cost_ratio,
    )


def _rank_key(c: CandidateResult) -> tuple[float, float]:
    return (c.blended_cost if c.blended_cost is not None else math.inf, c.node_hours)


def _region_winners_and_penalty(
    ranked: list[CandidateResult],
) -> tuple[list[RegionWinner], CandidateResult | None, float | None, float | None, float | None, float | None]:
    """Per-region cheapest + the $ penalty of forcing ONE global instance everywhere.

    node-hours is region-invariant (region only reweights via $/hr), so with no
    per-region job volumes this assumes equal node-hours per region and compares
    the single blended-cheapest instance against the best instance per region.
    """
    region_winners: list[RegionWinner] = []
    per_region_optimal_total = 0.0
    have_all_regions = True
    for region in pricing.REGIONS:
        best: tuple[float, str] | None = None
        for c in ranked:
            cost = c.region_costs.get(region)
            if cost is None:
                continue
            if best is None or cost < best[0]:
                best = (cost, c.instance_type)
        if best is None:
            have_all_regions = False
            continue
        region_winners.append(RegionWinner(region=region, instance_type=best[1], cost=best[0]))
        per_region_optimal_total += best[0]

    global_winner = ranked[0] if ranked else None
    if global_winner is None or not have_all_regions:
        return region_winners, global_winner, None, None, None, None

    region_costs = [global_winner.region_costs.get(r) for r in pricing.REGIONS]
    if any(rc is None for rc in region_costs):
        return region_winners, global_winner, None, per_region_optimal_total, None, None
    global_cost = sum(rc for rc in region_costs if rc is not None)
    penalty_abs = global_cost - per_region_optimal_total
    penalty_ratio = (global_cost / per_region_optimal_total) if per_region_optimal_total else None
    return region_winners, global_winner, global_cost, per_region_optimal_total, penalty_abs, penalty_ratio


def optimize_runner_fleet(
    jobs: list[Job],
    arch_allowlist: tuple[str, ...] | list[str] = (DEPLOYABLE_ARCH, "arm64"),
    *,
    runner_pool: str = RUNNER_POD_POOL,
    prices: dict | None = None,
    model: ClusterModel | None = None,
    baseline_instance: str = BASELINE_INSTANCE,
    window_buckets: int = CONSOLIDATION_WINDOW_BUCKETS,
    proactive_floor_pods: int = 0,
    exclude_burstable: bool = True,
) -> RunnerFleetResult:
    """Closed-form cheapest runner-pod host. Returns a structured, unprinted result."""
    arch_tuple = tuple(arch_allowlist)
    if model is None:
        model = ClusterModel()
    runner_cpu_m, runner_mem_mi = _resolve_runner_shape(jobs, runner_pool)
    concurrent = build_concurrency_timeline(jobs, runner_pool)
    peak = max(concurrent) if concurrent else 0

    baseline_slots = runner_slots(model, runner_pool, baseline_instance, runner_cpu_m, runner_mem_mi)
    baseline_node_hours = node_hours_for_slots(concurrent, baseline_slots, window_buckets, proactive_floor_pods)
    baseline_blended = pricing.blended_price(baseline_instance, prices)
    baseline_cost = baseline_node_hours * baseline_blended if baseline_blended is not None else None

    if not concurrent:
        return RunnerFleetResult(
            arch_allowlist=arch_tuple,
            peak_concurrency=peak,
            total_buckets=0,
            window_buckets=window_buckets,
            runner_cpu_m=runner_cpu_m,
            runner_mem_mi=runner_mem_mi,
            baseline_instance=baseline_instance,
            baseline_slots=baseline_slots,
            baseline_node_hours=baseline_node_hours,
            baseline_blended_cost=baseline_cost,
        )

    ranked: list[CandidateResult] = []
    for itype in _candidate_instances(arch_tuple, prices, exclude_burstable):
        candidate = _evaluate_candidate(
            itype,
            concurrent,
            model,
            runner_pool,
            runner_cpu_m,
            runner_mem_mi,
            window_buckets,
            proactive_floor_pods,
            prices,
            baseline_cost,
        )
        if candidate is not None:
            ranked.append(candidate)
    ranked.sort(key=_rank_key)

    best_amd64 = next((c for c in ranked if c.arch == DEPLOYABLE_ARCH), None)
    best_arm64 = next((c for c in ranked if c.arch == "arm64"), None)
    (
        region_winners,
        global_winner,
        global_cost,
        per_region_optimal_total,
        global_penalty_abs,
        global_penalty_ratio,
    ) = _region_winners_and_penalty(ranked)

    return RunnerFleetResult(
        arch_allowlist=arch_tuple,
        peak_concurrency=peak,
        total_buckets=len(concurrent),
        window_buckets=window_buckets,
        runner_cpu_m=runner_cpu_m,
        runner_mem_mi=runner_mem_mi,
        baseline_instance=baseline_instance,
        baseline_slots=baseline_slots,
        baseline_node_hours=baseline_node_hours,
        baseline_blended_cost=baseline_cost,
        ranked=ranked,
        best_amd64=best_amd64,
        best_arm64=best_arm64,
        region_winners=region_winners,
        global_winner=global_winner,
        global_cost=global_cost,
        per_region_optimal_total=per_region_optimal_total,
        global_penalty_abs=global_penalty_abs,
        global_penalty_ratio=global_penalty_ratio,
    )
