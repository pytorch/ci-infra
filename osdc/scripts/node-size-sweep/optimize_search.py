#!/usr/bin/env python3
# /// script
# requires-python = ">=3.12"
# dependencies = ["pyyaml>=6.0"]
# ///
"""Phase 2 of the node-size optimizer: sim-driven per-family hill-climb search.

For each in-scope fleet family, run an independent multi-restart hill climb
over per-def (instance_type, N) shape choices. Ranking metric is
`max(opt_cpu, opt_mem)` averaged across time buckets on the family's virtual
sub-fleets. Sim results are memoized in SQLite; search state is checkpointed
so crashes and SIGINT resume without redoing work.

Family independence (D3) means each family runs as its own subprocess via
multiprocessing.Pool. c7i-runner is kept in the injected fleets so runner-pod
entries still schedule.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import logging
import multiprocessing as mp
import os
import random
import signal
import sqlite3
import subprocess
import sys
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(REPO_ROOT / "scripts" / "python"))

from daemonset_overhead import discover_daemonsets  # noqa: E402
from instance_specs import INSTANCE_SPECS  # noqa: E402

import optimize_catalog  # noqa: E402
import sim_load  # noqa: E402
import simulate as sim_mod  # noqa: E402
from optimize_config import (  # noqa: E402
    IN_SCOPE_FAMILIES,
    PROD_PARITY_SIM_FLAGS,
    def_totals,
    load_defs_by_family,
)
from sim_load import RUNNER_POD_LABEL, RUNNER_POD_POOL  # noqa: E402
from sim_nodes import ClusterModel, FleetSpec, Job  # noqa: E402

# Ranking uses max(opt_cpu, opt_mem) (spec D1); node-hours is the tie-break.
# Improvement threshold defaults to 0.1pp (3σ per Phase 0 measurements).
DEFAULT_IMPROVEMENT_THRESHOLD_PP = 0.1
DEFAULT_NEUTRAL_MOVE_LIMIT = 3
DEFAULT_NUM_RESTARTS = 20
DEFAULT_LAST_DAYS = 21
BUCKET_SEC = 300


DefChoice = tuple[str, int]
Config = dict[str, DefChoice]


@dataclass
class SimMetrics:
    opt_cpu: float
    opt_mem: float
    opt_max: float
    cal_cpu: float
    cal_mem: float
    node_hours: float
    elapsed_s: float


@dataclass
class ShapeInfo:
    """Per-eligible-shape data cached at search start."""

    instance: str
    n: int
    slot_cpu_m: int
    slot_mem_mi: int
    slot_gpu: int


def _sha256_file(p: Path) -> str:
    h = hashlib.sha256()
    with p.open("rb") as fh:
        for chunk in iter(lambda: fh.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def _sim_source_shas() -> dict[str, str]:
    """Hashes of the sim files that materially affect results — pinned into the cache key.

    Includes any script the sim/loader/analyzer pipeline touches, plus the discovered
    DaemonSet YAML set (as a canonical JSON blob) so bumping a DS request invalidates
    all cached entries.
    """
    scripts_python = REPO_ROOT / "scripts" / "python"
    file_list: list[tuple[str, Path]] = [
        ("simulate_py", HERE / "simulate.py"),
        ("sim_nodes_py", HERE / "sim_nodes.py"),
        ("sim_load_py", HERE / "sim_load.py"),
        ("optimize_catalog_py", HERE / "optimize_catalog.py"),
        ("optimize_config_py", HERE / "optimize_config.py"),
        ("analyze_node_utilization_py", scripts_python / "analyze_node_utilization.py"),
        ("build_csv_py", HERE / "build_csv.py"),
        ("runner_overhead_py", scripts_python / "runner_overhead.py"),
        ("daemonset_overhead_py", scripts_python / "daemonset_overhead.py"),
        ("fleet_naming_py", scripts_python / "fleet_naming.py"),
        ("instance_specs_py", scripts_python / "instance_specs.py"),
    ]
    out: dict[str, str] = {}
    for key, path in file_list:
        if path.is_file():
            out[key] = _sha256_file(path)
    # Canonical DS payload — drop `source` (absolute path) so identical DS content
    # from a differently-located checkout still hits the same cache entry. cpu /
    # memory / gpu_only remain in the hash so YAML edits invalidate cached sims.
    ds_canonical = sorted(
        (ds.name, ds.cpu_millicores, ds.memory_mib, ds.gpu_only)
        for ds in discover_daemonsets(REPO_ROOT)
    )
    ds_payload = json.dumps(ds_canonical, sort_keys=True).encode()
    out["daemonsets_discovered"] = hashlib.sha256(ds_payload).hexdigest()
    return out


def _git_sha() -> str:
    try:
        out = subprocess.check_output(
            ["git", "rev-parse", "--short", "HEAD"],
            cwd=REPO_ROOT,
            stderr=subprocess.DEVNULL,
        )
        return out.decode().strip()
    except Exception:
        return "nogit"


def _default_output_dir() -> Path:
    ts = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    return HERE / "output" / f"{ts}-{_git_sha()}"


def _is_gpu_instance(instance_type: str) -> bool:
    spec = INSTANCE_SPECS.get(instance_type)
    return bool(spec and spec.get("gpu", 0) > 0)


def _family_sub_pool(family: str, instance_type: str) -> str:
    """Virtual sub-fleet pool name — one fleet per (family, instance) in the search."""
    return f"{family}__{instance_type}"


def _defs_by_family() -> dict[str, list[dict]]:
    """Return {family: [def rows]} filtered by scope constants."""
    return load_defs_by_family()


def _real_runner_fleet() -> FleetSpec:
    """Steal the c7i-runner FleetSpec from a real ClusterModel so runner-pods schedule."""
    real = ClusterModel()
    fs = real.fleets.get(RUNNER_POD_POOL)
    if fs is None:
        raise RuntimeError(f"real cluster model missing fleet {RUNNER_POD_POOL!r}")
    return fs


def _build_eligibility(family: str, defs: list[dict], daemonsets) -> dict[str, list[ShapeInfo]]:
    """For each def in the family, the sorted list of eligible (instance, N) shapes.

    Reuses Phase 1's catalog + eligible_shapes so the two phases agree exactly.
    """
    catalog = optimize_catalog.generate_catalog(family, defs, daemonsets)
    out: dict[str, list[ShapeInfo]] = {}
    for d in defs:
        req_cpu, req_mem, req_gpu = def_totals(d)
        elig = optimize_catalog.eligible_shapes(req_cpu, req_mem, req_gpu, catalog)
        shapes = [
            ShapeInfo(
                instance=s["instance"],
                n=s["N"],
                slot_cpu_m=s["slot_cpu_m"],
                slot_mem_mi=s["slot_mem_mi"],
                slot_gpu=s["slot_gpu"],
            )
            for s in elig
        ]
        shapes.sort(key=lambda s: (s.instance, s.n))
        out[d["name"]] = shapes
    return out


def _baseline_config(defs: list[dict], eligibility: dict[str, list[ShapeInfo]]) -> Config:
    """Current-prod (instance_type, N) per def, using build_label_table's nodepool_fraction as N.

    Exact match preferred. Fallback: same instance, N closest to prod_n (prefer larger — smaller
    N means more slack, more wasteful, but a fair proxy for prod behavior). Final fallback:
    first eligible shape.
    """
    cfg: Config = {}
    for d in defs:
        name = d["name"]
        prod_inst = d["instance_type"]
        prod_n = int(d.get("nodepool_fraction", 1))
        shapes = eligibility.get(name, [])
        chosen: DefChoice | None = None
        for s in shapes:
            if s.instance == prod_inst and s.n == prod_n:
                chosen = (s.instance, s.n)
                break
        if chosen is None:
            same_inst = [s for s in shapes if s.instance == prod_inst]
            if same_inst:
                s = min(same_inst, key=lambda s: (abs(s.n - prod_n), -s.n))
                chosen = (s.instance, s.n)
        if chosen is None and shapes:
            chosen = (shapes[0].instance, shapes[0].n)
        if chosen is None:
            raise RuntimeError(f"def {name} has no eligible shapes — baseline infeasible")
        cfg[name] = chosen
    return cfg


def _random_config(
    eligibility: dict[str, list[ShapeInfo]],
    rng: random.Random,
) -> Config:
    cfg: Config = {}
    for name, shapes in eligibility.items():
        if not shapes:
            raise RuntimeError(f"def {name} has no eligible shapes")
        s = rng.choice(shapes)
        cfg[name] = (s.instance, s.n)
    return cfg


def _shape_lookup(eligibility: dict[str, list[ShapeInfo]], name: str, choice: DefChoice) -> ShapeInfo:
    for s in eligibility[name]:
        if s.instance == choice[0] and s.n == choice[1]:
            return s
    raise KeyError(f"shape {choice} not eligible for def {name}")


def _config_key(family: str, config: Config, sim_flags: dict, csv_sha: str, src_shas: dict[str, str]) -> str:
    canonical = {
        "family": family,
        "config": {k: list(v) for k, v in sorted(config.items())},
        "sim_flags": sim_flags,
        "csv_sha256": csv_sha,
        "sim_source_shas": src_shas,
    }
    payload = json.dumps(canonical, sort_keys=True).encode()
    return hashlib.sha256(payload).hexdigest()


class SimCache:
    """SQLite-backed sim(config) -> metrics cache; safe for concurrent writers."""

    def __init__(self, path: Path):
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._conn() as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS sim_cache (
                    key         TEXT PRIMARY KEY,
                    config_json TEXT NOT NULL,
                    opt_max     REAL NOT NULL,
                    opt_cpu     REAL NOT NULL,
                    opt_mem     REAL NOT NULL,
                    node_hours  REAL NOT NULL,
                    cal_cpu     REAL NOT NULL,
                    cal_mem     REAL NOT NULL,
                    elapsed_s   REAL NOT NULL,
                    computed_at INTEGER NOT NULL
                )
                """
            )

    def _conn(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path, timeout=30.0)
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        return conn

    def get(self, key: str) -> SimMetrics | None:
        with self._conn() as conn:
            row = conn.execute(
                "SELECT opt_cpu, opt_mem, opt_max, cal_cpu, cal_mem, node_hours, elapsed_s FROM sim_cache WHERE key=?",
                (key,),
            ).fetchone()
        if row is None:
            return None
        return SimMetrics(
            opt_cpu=row[0], opt_mem=row[1], opt_max=row[2],
            cal_cpu=row[3], cal_mem=row[4], node_hours=row[5], elapsed_s=row[6],
        )

    def put(self, key: str, config: Config, m: SimMetrics) -> None:
        with self._conn() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO sim_cache VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    key,
                    json.dumps({k: list(v) for k, v in sorted(config.items())}),
                    m.opt_max, m.opt_cpu, m.opt_mem, m.node_hours,
                    m.cal_cpu, m.cal_mem, m.elapsed_s,
                    int(time.time()),
                ),
            )


class StateStore:
    """SQLite-backed per-family / per-restart search state for resume."""

    def __init__(self, path: Path):
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._conn() as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS family_state (
                    family     TEXT PRIMARY KEY,
                    status     TEXT NOT NULL,
                    verdict    TEXT,
                    best_json  TEXT,
                    updated_at INTEGER NOT NULL
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS restart_state (
                    family              TEXT NOT NULL,
                    restart_id          INTEGER NOT NULL,
                    config_json         TEXT NOT NULL,
                    best_opt_max        REAL,
                    neighbors_evaluated INTEGER NOT NULL,
                    status              TEXT NOT NULL,
                    updated_at          INTEGER NOT NULL,
                    PRIMARY KEY (family, restart_id)
                )
                """
            )

    def _conn(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path, timeout=30.0)
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        return conn

    def family_status(self, family: str) -> str | None:
        with self._conn() as conn:
            row = conn.execute("SELECT status FROM family_state WHERE family=?", (family,)).fetchone()
        return row[0] if row else None

    def mark_family(self, family: str, status: str, verdict: str | None = None, best: dict | None = None) -> None:
        with self._conn() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO family_state VALUES (?, ?, ?, ?, ?)",
                (family, status, verdict, json.dumps(best) if best else None, int(time.time())),
            )

    def restart_status(self, family: str, restart_id: int) -> str | None:
        with self._conn() as conn:
            row = conn.execute(
                "SELECT status FROM restart_state WHERE family=? AND restart_id=?",
                (family, restart_id),
            ).fetchone()
        return row[0] if row else None

    def update_restart(
        self,
        family: str,
        restart_id: int,
        config: Config,
        best_opt_max: float,
        neighbors_evaluated: int,
        status: str,
    ) -> None:
        with self._conn() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO restart_state VALUES (?, ?, ?, ?, ?, ?, ?)",
                (
                    family, restart_id,
                    json.dumps({k: list(v) for k, v in sorted(config.items())}),
                    best_opt_max, neighbors_evaluated, status, int(time.time()),
                ),
            )


def _rebuild_jobs_for_family(
    all_jobs: list[Job],
    family: str,
    defs: list[dict],
    config: Config,
    eligibility: dict[str, list[ShapeInfo]],
) -> list[Job]:
    """Emit a job list scoped to a family + rewritten pod shapes.

    - Each family job's (cpu_m, mem_mi) becomes the slot capacity from its config
      choice, so the pod occupies the whole slot exactly (matches Karpenter's
      request-based scheduling). Pool becomes the virtual sub-fleet name.
    - Runner-pod entries are dropped: they live on c7i-runner and never contribute
      to the per-family metrics (`{family}__` prefix), so simulating them is pure
      overhead. Halves sim wall-time on the real dataset.
    - Jobs from other families are dropped — family independence (D3).
    """
    family_def_names = {d["name"] for d in defs}
    out: list[Job] = []
    for j in all_jobs:
        if j.label == RUNNER_POD_LABEL:
            continue
        if j.label not in family_def_names:
            continue
        choice = config.get(j.label)
        if choice is None:
            continue
        shape = _shape_lookup(eligibility, j.label, choice)
        out.append(
            Job(
                label=j.label,
                pool=_family_sub_pool(family, shape.instance),
                cpu_m=shape.slot_cpu_m,
                mem_mi=shape.slot_mem_mi,
                gpu=shape.slot_gpu,
                start_bucket=j.start_bucket,
                end_bucket=j.end_bucket,
            )
        )
    return out


def _fleets_override_for_config(
    family: str,
    config: Config,
    runner_fleet: FleetSpec,
) -> dict[str, FleetSpec]:
    """One FleetSpec per unique instance in the config + the untouched c7i-runner."""
    fleets: dict[str, FleetSpec] = {RUNNER_POD_POOL: runner_fleet}
    unique_insts = {inst for inst, _ in config.values()}
    for inst in unique_insts:
        pool_name = _family_sub_pool(family, inst)
        fleets[pool_name] = FleetSpec(
            name=pool_name,
            is_gpu=_is_gpu_instance(inst),
            instances=(inst,),
        )
    return fleets


def _extract_family_metrics(sim_out: dict, family: str) -> tuple[float, float, float, float, float, float]:
    """Return (opt_cpu, opt_mem, opt_max_mean, cal_cpu, cal_mem, node_hours) restricted to the family's pools."""
    prefix = f"{family}__"
    opt_cpu_series: list[float] = []
    opt_mem_series: list[float] = []
    cal_cpu_series: list[float] = []
    cal_mem_series: list[float] = []
    max_series: list[float] = []
    node_bucket_count = 0
    for _t, per_pool in sim_out["per_bucket"]:
        w_cpu = ds_cpu = a_cpu = 0
        w_mem = ds_mem = a_mem = 0
        used_cpu = alloc_cpu = 0
        used_mem = alloc_mem = 0
        for name, sums in per_pool.items():
            if not name.startswith(prefix):
                continue
            w_cpu += sums["workload_cpu_m"]
            ds_cpu += sums["ds_cpu_m"]
            a_cpu += sums["alloc_cpu_m_raw"]
            w_mem += sums["workload_mem_mi"]
            ds_mem += sums["ds_mem_mi"]
            a_mem += sums["alloc_mem_mi_raw"]
            used_cpu += sums["cpu_used_m"]
            alloc_cpu += sums["cpu_alloc_m"]
            used_mem += sums["mem_used_mi"]
            alloc_mem += sums["mem_alloc_mi"]
        opt_denom_cpu = a_cpu + ds_cpu
        opt_denom_mem = a_mem + ds_mem
        # Skip buckets where the family has zero footprint on both axes — otherwise
        # empty periods (weekends, gaps) dilute the ranking metric with zeros
        # while the per-axis series correctly skip them, biasing search decisions.
        if opt_denom_cpu <= 0 and opt_denom_mem <= 0:
            continue
        cur_max_cpu = 0.0
        cur_max_mem = 0.0
        if opt_denom_cpu > 0:
            cur_max_cpu = w_cpu / opt_denom_cpu
            opt_cpu_series.append(cur_max_cpu)
        if opt_denom_mem > 0:
            cur_max_mem = w_mem / opt_denom_mem
            opt_mem_series.append(cur_max_mem)
        if alloc_cpu > 0:
            cal_cpu_series.append(used_cpu / alloc_cpu)
        if alloc_mem > 0:
            cal_mem_series.append(used_mem / alloc_mem)
        max_series.append(max(cur_max_cpu, cur_max_mem))
        node_bucket_count += a_cpu

    def mean(xs: list[float]) -> float:
        return sum(xs) / len(xs) if xs else 0.0

    opt_cpu = mean(opt_cpu_series)
    opt_mem = mean(opt_mem_series)
    opt_max_mean = mean(max_series)
    cal_cpu = mean(cal_cpu_series)
    cal_mem = mean(cal_mem_series)

    node_hours = 0.0
    for _t, per_pool in sim_out["per_bucket"]:
        for name, sums in per_pool.items():
            if not name.startswith(prefix):
                continue
            inst = name[len(prefix):]
            spec = INSTANCE_SPECS.get(inst)
            if not spec:
                continue
            vcpu_m = spec["vcpu"] * 1000
            if vcpu_m > 0:
                nodes_in_bucket = sums["alloc_cpu_m_raw"] / (vcpu_m * 0.95)
                node_hours += nodes_in_bucket * (BUCKET_SEC / 3600.0)

    return opt_cpu, opt_mem, opt_max_mean, cal_cpu, cal_mem, node_hours


def _run_sim_for_config(
    family: str,
    config: Config,
    all_jobs: list[Job],
    defs: list[dict],
    eligibility: dict[str, list[ShapeInfo]],
    runner_fleet: FleetSpec,
    sim_flags: dict,
) -> SimMetrics:
    jobs = _rebuild_jobs_for_family(all_jobs, family, defs, config, eligibility)
    # simulate() would crash on min(...) over an empty sequence — return a sentinel
    # zero result so the search can compare configs uniformly instead of blowing up.
    if not jobs:
        return SimMetrics(
            opt_cpu=0.0, opt_mem=0.0, opt_max=0.0,
            cal_cpu=0.0, cal_mem=0.0, node_hours=0.0,
            elapsed_s=0.0,
        )
    fleets_override = _fleets_override_for_config(family, config, runner_fleet)
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
    opt_cpu, opt_mem, opt_max, cal_cpu, cal_mem, node_hours = _extract_family_metrics(sim_out, family)
    return SimMetrics(
        opt_cpu=opt_cpu, opt_mem=opt_mem, opt_max=opt_max,
        cal_cpu=cal_cpu, cal_mem=cal_mem, node_hours=node_hours,
        elapsed_s=elapsed,
    )


def _cached_sim(
    family: str,
    config: Config,
    all_jobs: list[Job],
    defs: list[dict],
    eligibility: dict[str, list[ShapeInfo]],
    runner_fleet: FleetSpec,
    sim_flags: dict,
    csv_sha: str,
    src_shas: dict[str, str],
    cache: SimCache,
    log: logging.Logger,
) -> SimMetrics:
    key = _config_key(family, config, sim_flags, csv_sha, src_shas)
    hit = cache.get(key)
    if hit is not None:
        log.debug("cache HIT for config %s: opt_max=%.4f", key[:12], hit.opt_max)
        return hit
    log.debug("cache MISS for config %s — running sim", key[:12])
    m = _run_sim_for_config(family, config, all_jobs, defs, eligibility, runner_fleet, sim_flags)
    cache.put(key, config, m)
    log.debug("cache STORE for config %s: opt_max=%.4f elapsed=%.1fs", key[:12], m.opt_max, m.elapsed_s)
    return m


def _enumerate_neighbors(config: Config, eligibility: dict[str, list[ShapeInfo]]) -> list[Config]:
    """Flip one def to a different eligible shape at a time."""
    neighbors: list[Config] = []
    for name, cur in config.items():
        for s in eligibility[name]:
            choice = (s.instance, s.n)
            if choice == cur:
                continue
            new = dict(config)
            new[name] = choice
            neighbors.append(new)
    return neighbors


@dataclass
class FamilyResult:
    family: str
    baseline_metrics: SimMetrics
    baseline_config: Config
    best_metrics: SimMetrics
    best_config: Config
    delta_pp: float
    verdict: str
    num_restarts: int
    winning_restart: int
    total_sim_calls: int
    total_neighbors_evaluated: int
    elapsed_s: float
    skipped_reason: str | None = None


def _rank_key(m: SimMetrics) -> tuple[float, float]:
    """Ranking tuple per spec D1: opt_max is primary, node_hours is the tie-break
    (fewer node-hours wins, hence negated)."""
    return (m.opt_max, -m.node_hours)


def _restart_seed(family: str, restart_id: int) -> int:
    """Deterministic per-(family, restart_id) seed. Uses SHA-256 because Python's
    built-in `hash()` on tuples containing strings is randomized per interpreter
    via PYTHONHASHSEED, which breaks cross-process reproducibility of the search."""
    seed_bytes = hashlib.sha256(f"{family}-{restart_id}-seed".encode()).digest()
    return int.from_bytes(seed_bytes[:4], "big")


def _cfg_from_json(raw: str, eligibility: dict[str, list[ShapeInfo]]) -> Config | None:
    """Rehydrate a persisted config JSON. Returns None if any def or shape has
    since become ineligible (e.g. specs/DS overhead changed between runs)."""
    try:
        data = json.loads(raw)
    except (TypeError, ValueError):
        return None
    if not isinstance(data, dict):
        return None
    out: Config = {}
    for name, choice in data.items():
        if name not in eligibility:
            return None
        if not isinstance(choice, (list, tuple)) or len(choice) != 2:
            return None
        inst, n = choice[0], int(choice[1])
        try:
            _shape_lookup(eligibility, name, (inst, n))
        except KeyError:
            return None
        out[name] = (inst, n)
    return out


def _load_persisted_best(
    state: StateStore,
    family: str,
    eligibility: dict[str, list[ShapeInfo]],
) -> tuple[Config | None, float | None, int | None]:
    """Recover the best (config, opt_max) previously observed for this family across
    the family_state row and any per-restart rows. Returns (config, opt_max, restart_id)
    or (None, None, None) if nothing recoverable."""
    best_cfg: Config | None = None
    best_opt: float | None = None
    best_rid: int | None = None
    with state._conn() as conn:
        fam_row = conn.execute(
            "SELECT best_json FROM family_state WHERE family=?", (family,)
        ).fetchone()
        rows = conn.execute(
            "SELECT restart_id, config_json, best_opt_max, status FROM restart_state WHERE family=?",
            (family,),
        ).fetchall()
    if fam_row and fam_row[0]:
        try:
            fam_best = json.loads(fam_row[0])
        except (TypeError, ValueError):
            fam_best = None
        if fam_best and "config" in fam_best and "opt_max" in fam_best:
            cfg = _cfg_from_json(json.dumps(fam_best["config"]), eligibility)
            opt = fam_best.get("opt_max")
            if cfg is not None and isinstance(opt, (int, float)):
                best_cfg = cfg
                best_opt = float(opt)
    for restart_id, cfg_json, opt_max, _status in rows:
        if opt_max is None:
            continue
        cfg = _cfg_from_json(cfg_json, eligibility)
        if cfg is None:
            continue
        if best_opt is None or opt_max > best_opt:
            best_opt = float(opt_max)
            best_cfg = cfg
            best_rid = restart_id
    return best_cfg, best_opt, best_rid


def _search_family(
    family: str,
    defs: list[dict],
    all_jobs: list[Job],
    daemonsets,
    runner_fleet: FleetSpec,
    sim_flags: dict,
    csv_sha: str,
    src_shas: dict[str, str],
    cache_path: Path,
    state_path: Path,
    logs_dir: Path,
    num_restarts: int,
    improvement_threshold_pp: float,
    neutral_move_limit: int,
    log_level: str,
) -> FamilyResult:
    log = _configure_family_logger(family, logs_dir, log_level)
    cache = SimCache(cache_path)
    state = StateStore(state_path)

    t0 = time.perf_counter()
    log.info("family=%s: building eligibility", family)
    eligibility = _build_eligibility(family, defs, daemonsets)
    for name, shapes in eligibility.items():
        if not shapes:
            log.warning("def %s has no eligible shapes — will be skipped", name)
    eligible_defs = [d for d in defs if eligibility[d["name"]]]
    if not eligible_defs:
        log.error("family=%s: no defs with eligible shapes — aborting", family)
        raise RuntimeError(f"family {family} has zero fittable defs")
    elig_only = {d["name"]: eligibility[d["name"]] for d in eligible_defs}

    # Empty-family guard: if the CSV window has no jobs for any of this family's
    # defs, simulate() would crash on min() over the empty arrivals. Return a
    # "skipped" result so the global report can render "no data" instead.
    # If a prior run under a wider window persisted a best, preserve it rather
    # than overwriting with a "skipped" verdict — the narrower window is not
    # evidence that the prior best is invalid.
    family_def_names = {d["name"] for d in eligible_defs}
    has_family_jobs = any(j.label in family_def_names for j in all_jobs)
    if not has_family_jobs:
        prior_cfg, prior_opt, _prior_rid = _load_persisted_best(state, family, elig_only)
        if prior_cfg is not None and prior_opt is not None:
            log.warning(
                "family=%s: no jobs in current window but prior best exists — "
                "preserving prior best, not marking done for this window",
                family,
            )
            elapsed = time.perf_counter() - t0
            # Only opt_max is durable across runs (persisted in state); the
            # remaining metric fields were computed against the prior window
            # and cannot be reconstructed from an empty current window.
            prior_m = SimMetrics(
                opt_cpu=0.0, opt_mem=0.0, opt_max=float(prior_opt),
                cal_cpu=0.0, cal_mem=0.0, node_hours=0.0, elapsed_s=0.0,
            )
            return FamilyResult(
                family=family,
                baseline_metrics=prior_m,
                baseline_config=prior_cfg,
                best_metrics=prior_m,
                best_config=prior_cfg,
                delta_pp=0.0,
                verdict="kept_prior_best",
                num_restarts=num_restarts,
                winning_restart=0,
                total_sim_calls=0,
                total_neighbors_evaluated=0,
                elapsed_s=elapsed,
                skipped_reason="no jobs in window (prior best preserved)",
            )
        log.warning("family=%s: no jobs in window — skipping search", family)
        empty = SimMetrics(0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0)
        baseline = _baseline_config(eligible_defs, elig_only)
        elapsed = time.perf_counter() - t0
        state.mark_family(family, "done", verdict="skipped", best=None)
        return FamilyResult(
            family=family,
            baseline_metrics=empty,
            baseline_config=baseline,
            best_metrics=empty,
            best_config=baseline,
            delta_pp=0.0,
            verdict="skipped",
            num_restarts=num_restarts,
            winning_restart=0,
            total_sim_calls=0,
            total_neighbors_evaluated=0,
            elapsed_s=elapsed,
            skipped_reason="no jobs in window",
        )

    baseline = _baseline_config(eligible_defs, elig_only)
    log.info("family=%s: running baseline sim", family)
    baseline_m = _cached_sim(
        family, baseline, all_jobs, eligible_defs, elig_only, runner_fleet,
        sim_flags, csv_sha, src_shas, cache, log,
    )
    log.info(
        "family=%s: baseline opt_max=%.4f (cpu=%.4f mem=%.4f) node_hours=%.1f",
        family, baseline_m.opt_max, baseline_m.opt_cpu, baseline_m.opt_mem, baseline_m.node_hours,
    )

    best_config = baseline
    best_m = baseline_m
    winning_restart = 0
    total_sim_calls = 1
    total_neighbors_evaluated = 0
    improvement_threshold = improvement_threshold_pp / 100.0

    # Resume path: if a prior run persisted a better-than-baseline config,
    # materialize it as the starting best so restarts that already improved
    # don't get thrown away on the next launch.
    prior_cfg, prior_opt, prior_rid = _load_persisted_best(state, family, elig_only)
    if prior_cfg is not None and prior_opt is not None:
        prior_m = _cached_sim(
            family, prior_cfg, all_jobs, eligible_defs, elig_only, runner_fleet,
            sim_flags, csv_sha, src_shas, cache, log,
        )
        total_sim_calls += 1
        if _rank_key(prior_m) > _rank_key(best_m):
            log.info(
                "family=%s: resumed with prior best opt_max=%.4f from restart %s",
                family, prior_m.opt_max, prior_rid if prior_rid is not None else "family_state",
            )
            best_m = prior_m
            best_config = prior_cfg
            if prior_rid is not None:
                winning_restart = prior_rid

    for restart_id in range(num_restarts):
        prior_status = state.restart_status(family, restart_id)
        if prior_status == "done":
            log.info("family=%s: restart %d already done, skipping", family, restart_id)
            continue

        seed_int = _restart_seed(family, restart_id)
        rng = random.Random(seed_int)  # noqa: S311
        log.info("family=%s restart=%d: seed_int=%d", family, restart_id, seed_int)
        if restart_id == 0:
            cur = dict(baseline)
        else:
            cur = _random_config(elig_only, rng)

        cur_m = _cached_sim(
            family, cur, all_jobs, eligible_defs, elig_only, runner_fleet,
            sim_flags, csv_sha, src_shas, cache, log,
        )
        total_sim_calls += 1
        log.info(
            "family=%s restart=%d: seed opt_max=%.4f (cpu=%.4f mem=%.4f)",
            family, restart_id, cur_m.opt_max, cur_m.opt_cpu, cur_m.opt_mem,
        )
        # Track the per-restart PEAK independently from cur/cur_m so neutral moves
        # cannot dilute what this restart contributes to the global best.
        restart_best_m = cur_m
        restart_best_cfg = cur
        neutral_budget = neutral_move_limit
        neighbors_in_restart = 0
        step = 0

        while True:
            neighbors = _enumerate_neighbors(cur, elig_only)
            if not neighbors:
                break
            best_n_m: SimMetrics | None = None
            best_n_cfg: Config | None = None
            for n_cfg in neighbors:
                n_m = _cached_sim(
                    family, n_cfg, all_jobs, eligible_defs, elig_only, runner_fleet,
                    sim_flags, csv_sha, src_shas, cache, log,
                )
                total_sim_calls += 1
                neighbors_in_restart += 1
                total_neighbors_evaluated += 1
                if best_n_m is None or _rank_key(n_m) > _rank_key(best_n_m):
                    best_n_m = n_m
                    best_n_cfg = n_cfg
            assert best_n_m is not None and best_n_cfg is not None

            delta_pp = (best_n_m.opt_max - cur_m.opt_max) * 100.0
            if delta_pp > improvement_threshold_pp:
                log.info(
                    "family=%s restart=%d step=%d: IMPROVE opt_max %.4f -> %.4f (+%.3fpp)",
                    family, restart_id, step, cur_m.opt_max, best_n_m.opt_max, delta_pp,
                )
                cur = best_n_cfg
                cur_m = best_n_m
                if _rank_key(cur_m) > _rank_key(restart_best_m):
                    restart_best_m = cur_m
                    restart_best_cfg = cur
                state.update_restart(family, restart_id, cur, cur_m.opt_max, neighbors_in_restart, "running")
            elif (
                delta_pp > -improvement_threshold_pp
                and neutral_budget > 0
                and _rank_key(best_n_m) > _rank_key(cur_m)
            ):
                log.info(
                    "family=%s restart=%d step=%d: NEUTRAL move opt_max %.4f -> %.4f (%.3fpp; budget=%d)",
                    family, restart_id, step, cur_m.opt_max, best_n_m.opt_max, delta_pp, neutral_budget - 1,
                )
                cur = best_n_cfg
                cur_m = best_n_m
                neutral_budget -= 1
                if _rank_key(cur_m) > _rank_key(restart_best_m):
                    restart_best_m = cur_m
                    restart_best_cfg = cur
                state.update_restart(family, restart_id, cur, cur_m.opt_max, neighbors_in_restart, "running")
            else:
                log.info(
                    "family=%s restart=%d step=%d: STOP best_neighbor delta=%.3fpp <= %.3fpp",
                    family, restart_id, step, delta_pp, improvement_threshold_pp,
                )
                break
            step += 1

        # Compare the restart's PEAK (not its diluted end-state) against the global
        # best, using the (opt_max, -node_hours) tuple so ties break toward fewer nodes.
        improved_opt = restart_best_m.opt_max > best_m.opt_max + improvement_threshold
        is_tie_break_win = (
            abs(restart_best_m.opt_max - best_m.opt_max) <= improvement_threshold
            and _rank_key(restart_best_m) > _rank_key(best_m)
        )
        if improved_opt or is_tie_break_win:
            log.info(
                "family=%s restart=%d: NEW BEST opt_max %.4f -> %.4f (node_hours %.1f -> %.1f)",
                family, restart_id, best_m.opt_max, restart_best_m.opt_max,
                best_m.node_hours, restart_best_m.node_hours,
            )
            best_m = restart_best_m
            best_config = restart_best_cfg
            winning_restart = restart_id
        state.update_restart(
            family, restart_id, restart_best_cfg, restart_best_m.opt_max, neighbors_in_restart, "done",
        )

    delta_pp = (best_m.opt_max - baseline_m.opt_max) * 100.0
    verdict = "improved" if delta_pp > improvement_threshold_pp else "no-change"

    elapsed = time.perf_counter() - t0
    log.info(
        "family=%s: DONE verdict=%s baseline=%.4f best=%.4f delta=%.3fpp restart=%d sims=%d neighbors=%d elapsed=%.1fs",
        family, verdict, baseline_m.opt_max, best_m.opt_max, delta_pp,
        winning_restart, total_sim_calls, total_neighbors_evaluated, elapsed,
    )
    result = FamilyResult(
        family=family,
        baseline_metrics=baseline_m,
        baseline_config=baseline,
        best_metrics=best_m,
        best_config=best_config,
        delta_pp=delta_pp,
        verdict=verdict,
        num_restarts=num_restarts,
        winning_restart=winning_restart,
        total_sim_calls=total_sim_calls,
        total_neighbors_evaluated=total_neighbors_evaluated,
        elapsed_s=elapsed,
    )
    state.mark_family(
        family,
        "done",
        verdict=verdict,
        best={"config": {k: list(v) for k, v in best_config.items()}, "opt_max": best_m.opt_max},
    )
    return result


def _configure_family_logger(family: str, logs_dir: Path, level: str) -> logging.Logger:
    logs_dir.mkdir(parents=True, exist_ok=True)
    log = logging.getLogger(family)
    log.setLevel(getattr(logging, level.upper(), logging.INFO))
    log.propagate = False
    if not log.handlers:
        fmt = logging.Formatter("%(asctime)s [%(name)s] %(levelname)s %(message)s")
        fh = logging.FileHandler(logs_dir / f"{family}.log")
        fh.setFormatter(fmt)
        log.addHandler(fh)
        sh = logging.StreamHandler(sys.stderr)
        sh.setFormatter(fmt)
        log.addHandler(sh)
    return log


def _configure_global_logger(logs_dir: Path, level: str) -> logging.Logger:
    logs_dir.mkdir(parents=True, exist_ok=True)
    log = logging.getLogger("global")
    log.setLevel(getattr(logging, level.upper(), logging.INFO))
    log.propagate = False
    if not log.handlers:
        fmt = logging.Formatter("%(asctime)s [%(name)s] %(levelname)s %(message)s")
        fh = logging.FileHandler(logs_dir / "global.log")
        fh.setFormatter(fmt)
        log.addHandler(fh)
        sh = logging.StreamHandler(sys.stderr)
        sh.setFormatter(fmt)
        log.addHandler(sh)
    return log


def _family_worker(kwargs: dict) -> FamilyResult:
    """multiprocessing entrypoint — kwargs must be picklable."""

    def _sigterm(_signum, _frame):
        sys.exit(0)

    signal.signal(signal.SIGTERM, _sigterm)
    signal.signal(signal.SIGINT, _sigterm)
    return _search_family(**kwargs)


def _write_family_report(reports_dir: Path, r: FamilyResult, defs: list[dict]) -> None:
    reports_dir.mkdir(parents=True, exist_ok=True)
    path = reports_dir / f"{r.family}.md"
    if r.skipped_reason:
        path.write_text(
            f"# Fleet: {r.family}\n\n"
            f"## Skipped\n\nreason: {r.skipped_reason}\n\nno data available for this family in the current window.\n"
        )
        return
    baseline_insts = sorted({inst for inst, _ in r.baseline_config.values()})
    best_insts = sorted({inst for inst, _ in r.best_config.values()})
    lines = [
        f"# Fleet: {r.family}",
        "",
        "## Baseline (current config)",
        f"  fleet = {baseline_insts}",
        f"  opt_max = {r.baseline_metrics.opt_max * 100:.1f}% "
        f"(cpu {r.baseline_metrics.opt_cpu * 100:.1f}%, mem {r.baseline_metrics.opt_mem * 100:.1f}%)",
        f"  cal_cpu = {r.baseline_metrics.cal_cpu * 100:.1f}%, cal_mem = {r.baseline_metrics.cal_mem * 100:.1f}%",
        f"  node_hours ~ {r.baseline_metrics.node_hours:.1f}",
        "",
        "## Recommendation",
        f"  fleet = {best_insts}",
        f"  opt_max = {r.best_metrics.opt_max * 100:.1f}% "
        f"(cpu {r.best_metrics.opt_cpu * 100:.1f}%, mem {r.best_metrics.opt_mem * 100:.1f}%)",
        f"  cal_cpu = {r.best_metrics.cal_cpu * 100:.1f}%, cal_mem = {r.best_metrics.cal_mem * 100:.1f}%",
        f"  node_hours ~ {r.best_metrics.node_hours:.1f}",
        f"  delta vs baseline = {r.delta_pp:+.2f}pp",
        f"  verdict = {r.verdict}",
        "",
        "## Def re-mappings",
    ]
    for d in defs:
        name = d["name"]
        base = r.baseline_config.get(name)
        best = r.best_config.get(name)
        if base is None or best is None:
            continue
        marker = "" if base == best else "  <-- CHANGED"
        lines.append(f"  {name}: {base[0]} N={base[1]} -> {best[0]} N={best[1]}{marker}")
    lines.append("")
    lines.append("## Convergence")
    lines.append(
        f"  restarts={r.num_restarts}, winning_restart={r.winning_restart}, "
        f"sims={r.total_sim_calls}, neighbors_evaluated={r.total_neighbors_evaluated}, "
        f"elapsed={r.elapsed_s:.1f}s",
    )
    lines.append("")
    path.write_text("\n".join(lines) + "\n")


def _write_family_patch_stub(reports_dir: Path, r: FamilyResult, defs: list[dict]) -> None:
    """Placeholder patch — real diff generation is Phase 4."""
    reports_dir.mkdir(parents=True, exist_ok=True)
    path = reports_dir / f"{r.family}.patch"
    if r.skipped_reason:
        path.write_text(f"# SKIPPED family {r.family}: {r.skipped_reason}\n")
        return
    lines = [
        f"# STUB: would-be changes for family {r.family}.",
        f"# Verdict: {r.verdict} (delta {r.delta_pp:+.2f}pp)",
        "# Real unified diff generation is deferred to Phase 4.",
        "",
    ]
    baseline_insts = sorted({inst for inst, _ in r.baseline_config.values()})
    best_insts = sorted({inst for inst, _ in r.best_config.values()})
    lines.append(f"# modules/nodepools/defs/{r.family}.yaml instances: {baseline_insts} -> {best_insts}")
    lines.append("")
    for d in defs:
        name = d["name"]
        base = r.baseline_config.get(name)
        best = r.best_config.get(name)
        if base is None or best is None or base == best:
            continue
        lines.append(
            f"# modules/arc-runners/defs/{name}.yaml: instance_type {base[0]} N={base[1]} -> {best[0]} N={best[1]}"
        )
    path.write_text("\n".join(lines) + "\n")


def _write_global_report(reports_dir: Path, results: list[FamilyResult]) -> None:
    reports_dir.mkdir(parents=True, exist_ok=True)
    path = reports_dir / "global.md"
    lines = [
        "# Global search summary",
        "",
        "| family | baseline opt_max | rec opt_max | delta (pp) | verdict | sims | wall (s) |",
        "|--------|-----------------:|------------:|-----------:|:--------|-----:|---------:|",
    ]
    for r in sorted(results, key=lambda x: x.family):
        if r.skipped_reason:
            lines.append(
                f"| {r.family} | n/a | n/a | n/a | skipped ({r.skipped_reason}) | 0 | {r.elapsed_s:.0f} |"
            )
            continue
        lines.append(
            f"| {r.family} | {r.baseline_metrics.opt_max * 100:.1f}% | "
            f"{r.best_metrics.opt_max * 100:.1f}% | {r.delta_pp:+.2f} | "
            f"{r.verdict} | {r.total_sim_calls} | {r.elapsed_s:.0f} |"
        )
    lines.append("")
    path.write_text("\n".join(lines) + "\n")


def _install_shutdown_handler(output_dir: Path, log: logging.Logger) -> None:
    def _handler(signum, _frame):
        log.warning(
            "signal %s received — state is checkpointed; resume with: --resume %s",
            signum, output_dir,
        )
        sys.exit(0)

    signal.signal(signal.SIGINT, _handler)
    signal.signal(signal.SIGTERM, _handler)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--csv", default=str(HERE / "pytorch_60d.csv"))
    ap.add_argument("--last-days", type=int, default=DEFAULT_LAST_DAYS)
    ap.add_argument("--output-dir", default=None)
    ap.add_argument("--resume", default=None)
    ap.add_argument("--family", action="append", default=None)
    ap.add_argument("--num-workers", type=int, default=None)
    ap.add_argument("--num-restarts", type=int, default=DEFAULT_NUM_RESTARTS)
    ap.add_argument("--improvement-threshold-pp", type=float, default=DEFAULT_IMPROVEMENT_THRESHOLD_PP)
    ap.add_argument("--neutral-move-limit", type=int, default=DEFAULT_NEUTRAL_MOVE_LIMIT)
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument(
        "--force",
        action="store_true",
        help="ignore per-family 'done' status on resume and re-run the family",
    )
    ap.add_argument("--log-level", choices=["debug", "info", "warning"], default="info")
    ap.add_argument("--drop-provider", action="append", default=None)
    ap.add_argument("--keep-fraction", type=float, default=1.0)
    ap.add_argument("--keep-seed", type=int, default=12345)
    ap.add_argument("--no-runner-pods", action="store_true")
    ap.add_argument("--runner-pool", default=RUNNER_POD_POOL)
    args = ap.parse_args(argv)

    output_dir = Path(args.resume) if args.resume else (Path(args.output_dir) if args.output_dir else _default_output_dir())
    output_dir.mkdir(parents=True, exist_ok=True)
    logs_dir = output_dir / "logs"
    reports_dir = output_dir / "reports"
    cache_path = output_dir / "cache.sqlite"
    state_path = output_dir / "state.sqlite"

    global_log = _configure_global_logger(logs_dir, args.log_level)
    _install_shutdown_handler(output_dir, global_log)
    global_log.info("output_dir=%s", output_dir)

    families = tuple(args.family) if args.family else IN_SCOPE_FAMILIES
    for f in families:
        if f not in IN_SCOPE_FAMILIES:
            global_log.error("family %r not in scope %s", f, IN_SCOPE_FAMILIES)
            return 2

    defs_by_family = _defs_by_family()
    families = tuple(f for f in families if defs_by_family.get(f))

    if args.dry_run:
        global_log.info("dry-run: invoking optimize_catalog for %s", families)
        rc = optimize_catalog.main([f"--family={f}" for f in families] + [f"--output={output_dir / 'catalog.json'}"])
        return rc

    csv_path = Path(args.csv)
    if not csv_path.is_file():
        global_log.error("CSV not found: %s", csv_path)
        return 1

    global_log.info("hashing sim source files")
    src_shas = _sim_source_shas()
    global_log.info("hashing CSV (may take a moment for 60d file)")
    csv_sha = _sha256_file(csv_path)

    global_log.info("loading CSV %s (last_days=%s)", csv_path, args.last_days)
    drop_providers = set(args.drop_provider) if args.drop_provider else set()
    add_runner_pods = not args.no_runner_pods
    all_jobs = sim_load.load_jobs(
        csv_path,
        add_runner_pods=add_runner_pods,
        runner_pool=args.runner_pool,
        drop_providers=drop_providers,
        keep_fraction=args.keep_fraction,
        keep_seed=args.keep_seed,
        last_days=args.last_days,
    )
    global_log.info("loaded %d jobs", len(all_jobs))

    daemonsets = discover_daemonsets(REPO_ROOT)
    runner_fleet = _real_runner_fleet()

    # Base sim flags. PROD_PARITY_SIM_FLAGS (single source of truth shared with
    # benchmark's calibration run) overrides the sim's defaults for the three
    # prod-parity keys so the search optimizes under the same accounting the
    # sim-vs-prod calibration validates.
    sim_flags = {
        "seed": 42,
        "placeholder_max_age": 2,
        "warmup_default": 1,
        "warmup_gpu": 2,
        "warmup_baremetal": 3,
        "placeholders_enabled": True,
        # load_jobs args — must be part of the cache key so a run with different job
        # filtering does not collide with a prior run's cached sims.
        "last_days": args.last_days,
        "drop_providers": sorted(drop_providers),
        "keep_fraction": args.keep_fraction,
        "keep_seed": args.keep_seed,
        "add_runner_pods": add_runner_pods,
        "runner_pool": args.runner_pool,
    }
    sim_flags.update(PROD_PARITY_SIM_FLAGS)
    global_log.info(
        "sim_flags prod-parity: daemonsets_in_metric=%s phantom_pods_enabled=%s empty_ttl_buckets=%s",
        sim_flags["daemonsets_in_metric"],
        sim_flags["phantom_pods_enabled"],
        sim_flags["empty_ttl_buckets"],
    )

    state = StateStore(state_path)
    worker_kwargs = []
    for family in families:
        defs = defs_by_family.get(family) or []
        if not defs:
            global_log.warning("family %s has no in-scope defs — skipping", family)
            continue
        prior_family_status = state.family_status(family)
        if prior_family_status == "done" and not args.force:
            global_log.info("skipping family %s (already done)", family)
            continue
        worker_kwargs.append(
            {
                "family": family,
                "defs": defs,
                "all_jobs": all_jobs,
                "daemonsets": daemonsets,
                "runner_fleet": runner_fleet,
                "sim_flags": sim_flags,
                "csv_sha": csv_sha,
                "src_shas": src_shas,
                "cache_path": cache_path,
                "state_path": state_path,
                "logs_dir": logs_dir,
                "num_restarts": args.num_restarts,
                "improvement_threshold_pp": args.improvement_threshold_pp,
                "neutral_move_limit": args.neutral_move_limit,
                "log_level": args.log_level,
            }
        )

    if not worker_kwargs:
        global_log.warning("no families to run")
        return 0

    cpu = os.cpu_count() or 2
    workers = args.num_workers if args.num_workers else max(1, min(len(worker_kwargs), cpu - 2))
    global_log.info("dispatching %d families across %d workers", len(worker_kwargs), workers)

    results: list[FamilyResult] = []
    t0 = time.perf_counter()

    if workers == 1:
        for kw in worker_kwargs:
            results.append(_family_worker(kw))
    else:
        ctx = mp.get_context("spawn")
        with ctx.Pool(processes=workers, maxtasksperchild=1) as pool:
            for r in pool.imap_unordered(_family_worker, worker_kwargs):
                results.append(r)
                elapsed = time.perf_counter() - t0
                done = len(results)
                total = len(worker_kwargs)
                remaining = total - done
                eta = (elapsed / done) * remaining if done else 0.0
                global_log.info("heartbeat: %d/%d families done, elapsed=%.0fs, eta=%.0fs", done, total, elapsed, eta)

    for r in results:
        defs = defs_by_family[r.family]
        eligible_defs = [d for d in defs if d["name"] in r.baseline_config]
        _write_family_report(reports_dir, r, eligible_defs)
        _write_family_patch_stub(reports_dir, r, eligible_defs)
    _write_global_report(reports_dir, results)

    global_log.info("all done. Reports in %s", reports_dir)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
