#!/usr/bin/env python3
# /// script
# requires-python = ">=3.12"
# dependencies = ["pyyaml>=6.0", "rich>=13"]
# ///
"""Phase 2 of the node-size optimizer: sim-driven per-family partition search.

For each in-scope fleet family, run an independent search over partitions of
the family's runner defs into sub-nodepools, plus per-partition instance
assignment. Pod (cpu_m, mem_mi) requests are derived deterministically from the
(def, instance) pair via the D4 tight-fit rule — never a search variable.

Ranking metric is `max(opt_cpu, opt_mem)` on the family's virtual sub-fleets.
Sim results are memoized in SQLite; per-family / per-restart search state is
checkpointed so crashes and SIGINT resume without redoing work.

Family independence (D3) means each family runs as its own subprocess via
multiprocessing.Pool. c7i-runner is kept in the injected fleets so runner-pod
entries schedule as they do in prod.
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
import subprocess
import sys
import time
from datetime import UTC, datetime
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(REPO_ROOT / "scripts" / "python"))

from daemonset_overhead import discover_daemonsets  # noqa: E402

import optimize_catalog  # noqa: E402
import optimize_engine  # noqa: E402
import optimize_report  # noqa: E402
import sim_load  # noqa: E402
from optimize_config import (  # noqa: E402
    IN_SCOPE_FAMILIES,
    PROD_PARITY_SIM_FLAGS,
    load_defs_by_family,
)
from optimize_engine import (  # noqa: E402
    Config,
    FamilyResult,
    baseline_config,
    build_family_catalog,
    canonical_config,
    cached_sim,
    enumerate_feasible_configs,
    enumerate_neighbors,
    is_baseline_feasible,
    is_config_feasible,
    random_feasible_config,
    rank_key,
)
from optimize_progress import (  # noqa: E402
    MultiFamilyProgressDisplay,
    ProgressDisplay,
    QueueProgressDisplay,
)
from optimize_storage import ClusterValidationResult, SimCache, SimMetrics, StateStore  # noqa: E402
from sim_load import RUNNER_POD_POOL  # noqa: E402
from sim_nodes import ClusterModel, FleetSpec, Job  # noqa: E402

DEFAULT_IMPROVEMENT_THRESHOLD_PP = 0.1
DEFAULT_NEUTRAL_MOVE_LIMIT = 3
DEFAULT_NUM_RESTARTS = 20
DEFAULT_LAST_DAYS = 21
DEFAULT_EXHAUSTIVE_K_CUTOFF = 5
DEFAULT_EXHAUSTIVE_MAX_CONFIGS = 10000
DEFAULT_RENAME_THRESHOLD_PCT = 10.0


# ---------- infra helpers ----------


def _sha256_file(p: Path) -> str:
    h = hashlib.sha256()
    with p.open("rb") as fh:
        for chunk in iter(lambda: fh.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def _sim_source_shas() -> dict[str, str]:
    """Hashes of the sim files that materially affect results — pinned into the cache key.

    Includes the sim/loader/analyzer pipeline plus the discovered DaemonSet YAML
    set (as a canonical JSON blob) so bumping a DS request invalidates all
    cached entries.
    """
    scripts_python = REPO_ROOT / "scripts" / "python"
    file_list: list[tuple[str, Path]] = [
        ("simulate_py", HERE / "simulate.py"),
        ("sim_nodes_py", HERE / "sim_nodes.py"),
        ("sim_load_py", HERE / "sim_load.py"),
        ("optimize_catalog_py", HERE / "optimize_catalog.py"),
        ("optimize_config_py", HERE / "optimize_config.py"),
        ("optimize_engine_py", HERE / "optimize_engine.py"),
        ("runner_hooks_py", HERE / "runner_hooks.py"),
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
    # from a differently-located checkout still hits the same cache entry.
    ds_canonical = sorted(
        (ds.name, ds.cpu_millicores, ds.memory_mib, ds.gpu_only) for ds in discover_daemonsets(REPO_ROOT)
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


def _real_runner_fleet() -> FleetSpec:
    """Steal c7i-runner FleetSpec from a real ClusterModel so runner-pods schedule."""
    real = ClusterModel()
    fs = real.fleets.get(RUNNER_POD_POOL)
    if fs is None:
        raise RuntimeError(f"real cluster model missing fleet {RUNNER_POD_POOL!r}")
    return fs


def _restart_seed(family: str, restart_id: int) -> int:
    """Deterministic per-(family, restart_id) seed. SHA-256 so cross-process
    reproducibility is not broken by PYTHONHASHSEED."""
    seed_bytes = hashlib.sha256(f"{family}-{restart_id}-seed".encode()).digest()
    return int.from_bytes(seed_bytes[:4], "big")


# ---------- state persistence helpers ----------


def _config_from_persisted_json(raw: str, catalog) -> Config | None:
    """Rehydrate a persisted config JSON. Returns None if any pair is not eligible now."""
    try:
        data = json.loads(raw)
    except (TypeError, ValueError):
        return None
    if not isinstance(data, dict):
        return None
    out: Config = {}
    for sub_id, spec in data.items():
        if not isinstance(spec, dict) or "instance" not in spec or "pods" not in spec:
            return None
        inst = spec["instance"]
        pods = spec["pods"]
        if not isinstance(pods, list):
            return None
        for pod in pods:
            if (pod, inst) not in catalog:
                return None
        out[sub_id] = {"instance": inst, "pods": sorted(pods)}
    return out


def _metrics_from_persisted(payload: dict | None) -> SimMetrics | None:
    """Rehydrate a full SimMetrics from a persisted best payload.

    Older state DBs (pre-vcpu_hours rename) are not migrated — the schema
    break invalidates cached sims anyway. Fall back to zero for missing keys
    so resume does not crash; a stale prior best loses tie-breaks (vcpu_hours=0
    reads as "unknown / worst") until the next sim recomputes it.
    """
    if not isinstance(payload, dict):
        return None
    if "metrics" in payload and isinstance(payload["metrics"], dict):
        md = payload["metrics"]
        try:
            return SimMetrics(
                opt_max=float(md["opt_max"]),
                opt_cpu=float(md["opt_cpu"]),
                opt_mem=float(md["opt_mem"]),
                cal_cpu=float(md["cal_cpu"]),
                cal_mem=float(md["cal_mem"]),
                vcpu_hours=float(md.get("vcpu_hours", 0.0)),
            )
        except (TypeError, ValueError, KeyError):
            return None
    if "opt_max" in payload:
        try:
            return SimMetrics(
                opt_max=float(payload["opt_max"]),
                opt_cpu=0.0,
                opt_mem=0.0,
                cal_cpu=0.0,
                cal_mem=0.0,
                vcpu_hours=0.0,
            )
        except (TypeError, ValueError):
            return None
    return None


def _persist_best_payload(cfg: Config, m: SimMetrics) -> dict:
    return {
        "config": {sid: {"instance": spec["instance"], "pods": list(spec["pods"])} for sid, spec in cfg.items()},
        "opt_max": m.opt_max,
        "metrics": {
            "opt_max": m.opt_max,
            "opt_cpu": m.opt_cpu,
            "opt_mem": m.opt_mem,
            "cal_cpu": m.cal_cpu,
            "cal_mem": m.cal_mem,
            "vcpu_hours": m.vcpu_hours,
        },
    }


def _load_persisted_best(
    state: StateStore,
    family: str,
    catalog,
) -> tuple[Config | None, SimMetrics | None, int | None]:
    """Recover the best (config, full metrics) across family_state and restart_state rows.

    Uses rank_key ordering (opt_max primary, -vcpu_hours tie-breaker) so the
    resume-time choice matches the live search's ordering.
    """
    best_cfg: Config | None = None
    best_m: SimMetrics | None = None
    best_rid: int | None = None
    fam_json = state.family_best_json(family)
    if fam_json:
        try:
            fam_best = json.loads(fam_json)
        except (TypeError, ValueError):
            fam_best = None
        if isinstance(fam_best, dict) and "config" in fam_best:
            cfg = _config_from_persisted_json(json.dumps(fam_best["config"]), catalog)
            m = _metrics_from_persisted(fam_best)
            if cfg is not None and m is not None:
                best_cfg = cfg
                best_m = m
    for restart_id, cfg_json, opt_max, _status in state.restart_rows(family):
        if opt_max is None:
            continue
        cfg = _config_from_persisted_json(cfg_json, catalog)
        if cfg is None:
            continue
        # restart_state only persists opt_max, so tie-break via family-level
        # metrics when the restart row wins; otherwise treat vcpu_hours=0
        # (unknown), which just makes the restart row lose ties.
        rm = SimMetrics(
            opt_max=float(opt_max),
            opt_cpu=0.0,
            opt_mem=0.0,
            cal_cpu=0.0,
            cal_mem=0.0,
            vcpu_hours=0.0,
        )
        if best_m is None or rank_key(rm) > rank_key(best_m):
            best_m = rm
            best_cfg = cfg
            best_rid = restart_id
    return best_cfg, best_m, best_rid


def _reconstitute_family_result(
    state: StateStore,
    family: str,
    defs: list[dict],
    daemonsets,
) -> FamilyResult | None:
    """Rebuild a FamilyResult for a family the state store marks 'done'.

    Ensures the global report includes every requested family on resume,
    not just those re-run in the current invocation.
    """
    catalog_full = optimize_catalog.build_eligibility_catalog(families=[family], daemonsets=daemonsets)
    entries = catalog_full.get(family, [])
    catalog = build_family_catalog(entries)
    verdict = state.family_verdict(family) or "unknown"
    eligible_names = {e.def_label for e in entries}
    eligible_defs = [d for d in defs if d["name"] in eligible_names]
    try:
        baseline = baseline_config(family, eligible_defs, entries) if eligible_defs else {}
    except ValueError:
        baseline = {}
    prior_cfg, prior_m, _ = _load_persisted_best(state, family, catalog)
    if prior_cfg is None or prior_m is None:
        # Nothing to reconstitute (e.g. skipped families with no best).
        if verdict == "skipped":
            return FamilyResult(
                family=family,
                baseline_config=baseline,
                baseline_metrics=None,
                best_config=None,
                best_metrics=None,
                verdict="skipped",
                skipped_reason="resumed: no persisted metrics",
            )
        return None
    return FamilyResult(
        family=family,
        baseline_config=baseline,
        baseline_metrics=prior_m,
        best_config=prior_cfg,
        best_metrics=prior_m,
        verdict=verdict,
    )


# ---------- search strategies ----------


def _search_exhaustive(
    family: str,
    defs: list[dict],
    catalog_entries,
    all_jobs: list[Job],
    runner_fleet: FleetSpec,
    sim_flags: dict,
    csv_sha: str,
    src_shas: dict[str, str],
    cache: SimCache,
    log: logging.Logger,
    max_configs: int,
    prog,
    best_seed_cfg: Config | None,
    best_seed_m: SimMetrics | None,
    daemonsets,
    seen_configs: set[str],
) -> tuple[Config | None, SimMetrics | None, int, int]:
    """Enumerate every feasible config, return (best_cfg, best_m, evaluated, cache_hits).

    Falls through to hill-climb (returns None,None,...,-1 cache_hits sentinel) when
    the feasible set exceeds `max_configs` — caller re-dispatches.
    """
    log.info("family=%s: enumerating configs (max_configs=%d)", family, max_configs)
    configs, capped = enumerate_feasible_configs(family, defs, catalog_entries, limit=max_configs)
    if capped:
        log.warning(
            "family=%s: exhaustive enumeration exceeded %d configs — falling back to hillclimb",
            family,
            max_configs,
        )
        return None, None, 0, -1
    log.info("family=%s: %d feasible configs to sim", family, len(configs))
    prog.start_phase("exhaustive", len(configs))
    family_def_names = {d["name"] for d in defs}
    catalog = build_family_catalog(catalog_entries)
    best_cfg = best_seed_cfg
    best_m = best_seed_m
    evaluated = 0
    cache_hits = 0
    for cfg in configs:
        cfg_key = canonical_config(cfg)
        try:
            m, was_hit = cached_sim(
                family,
                cfg,
                all_jobs,
                catalog,
                family_def_names,
                runner_fleet,
                sim_flags,
                csv_sha,
                src_shas,
                cache,
                log,
                daemonsets=daemonsets,
            )
        except Exception as e:
            log.warning("family=%s: sim failed for config %s: %s", family, cfg_key[:60], e, exc_info=True)
            prog.advance(best_m.opt_max if best_m is not None else None)
            continue
        # Dedupe evaluation counter across baseline / prior-best / enumeration.
        if cfg_key not in seen_configs:
            seen_configs.add(cfg_key)
            evaluated += 1
        if was_hit:
            cache_hits += 1
        if best_m is None or rank_key(m) > rank_key(best_m):
            best_cfg = cfg
            best_m = m
            log.info("family=%s: new best opt_max=%.4f (%d/%d)", family, m.opt_max, evaluated, len(configs))
        prog.advance(best_m.opt_max if best_m is not None else None)
    prog.end_phase("done")
    return best_cfg, best_m, evaluated, cache_hits


def _search_hillclimb(
    family: str,
    defs: list[dict],
    catalog_entries,
    all_jobs: list[Job],
    runner_fleet: FleetSpec,
    sim_flags: dict,
    csv_sha: str,
    src_shas: dict[str, str],
    cache: SimCache,
    state: StateStore,
    log: logging.Logger,
    num_restarts: int,
    neutral_move_limit: int,
    improvement_threshold_pp: float,
    prog,
    baseline: Config,
    best_seed_cfg: Config | None,
    best_seed_m: SimMetrics | None,
    daemonsets,
    seen_configs: set[str],
) -> tuple[Config | None, SimMetrics | None, int, int, int]:
    """Return (best_cfg, best_m, evaluated, cache_hits, restarts_run).

    Restart semantics: restart_id 0 = baseline seed. restart_ids 1..N =
    random feasible seeds. `num_restarts` is the number of RANDOM restarts
    on top of the baseline (total sim rounds = 1 + num_restarts).
    """
    family_def_names = {d["name"] for d in defs}
    catalog = build_family_catalog(catalog_entries)
    best_cfg = best_seed_cfg
    best_m = best_seed_m
    evaluated = 0
    cache_hits = 0
    restarts_run = 0
    improvement_threshold = improvement_threshold_pp / 100.0

    def _sim(cfg: Config) -> tuple[SimMetrics | None, bool]:
        cfg_key = canonical_config(cfg)
        try:
            m, was_hit = cached_sim(
                family,
                cfg,
                all_jobs,
                catalog,
                family_def_names,
                runner_fleet,
                sim_flags,
                csv_sha,
                src_shas,
                cache,
                log,
                daemonsets=daemonsets,
            )
        except Exception as e:
            log.warning("family=%s: sim failed for config %s: %s", family, cfg_key[:60], e, exc_info=True)
            return None, False
        return m, was_hit

    for restart_id in range(num_restarts + 1):
        prior_status = state.restart_status(family, restart_id)
        if prior_status == "done":
            log.info("family=%s: restart %d already done, skipping", family, restart_id)
            continue

        rng = random.Random(_restart_seed(family, restart_id))  # noqa: S311
        prog.start_restart(restart_id, phase="seed")
        if restart_id == 0:
            cur = optimize_engine._config_copy(baseline)
        else:
            cur = random_feasible_config(family, defs, catalog_entries, rng)

        cur_m, was_hit = _sim(cur)
        if cur_m is None:
            log.warning("family=%s: restart %d seed sim failed — skipping restart", family, restart_id)
            continue
        cur_key = canonical_config(cur)
        if cur_key not in seen_configs:
            seen_configs.add(cur_key)
            evaluated += 1
        if was_hit:
            cache_hits += 1
        restart_best_m = cur_m
        restart_best_cfg = cur
        neutral_budget = neutral_move_limit
        step = 0

        while True:
            neighbors = enumerate_neighbors(family, cur, defs, catalog_entries)
            if not neighbors:
                break
            prog.start_phase(f"step {step}", len(neighbors))
            best_n_m: SimMetrics | None = None
            best_n_cfg: Config | None = None
            for n_cfg in neighbors:
                n_m, n_was_hit = _sim(n_cfg)
                if n_m is None:
                    prog.advance(best_m.opt_max if best_m else 0.0)
                    continue
                n_key = canonical_config(n_cfg)
                if n_key not in seen_configs:
                    seen_configs.add(n_key)
                    evaluated += 1
                if n_was_hit:
                    cache_hits += 1
                if best_n_m is None or rank_key(n_m) > rank_key(best_n_m):
                    best_n_m = n_m
                    best_n_cfg = n_cfg
                prog.advance(max(n_m.opt_max, best_m.opt_max if best_m else 0.0))
            if best_n_m is None or best_n_cfg is None:
                # All neighbors failed to sim — cannot make progress on this restart.
                prog.end_phase("STOP")
                break

            delta_pp = (best_n_m.opt_max - cur_m.opt_max) * 100.0
            if delta_pp > improvement_threshold_pp:
                log.info(
                    "family=%s restart=%d step=%d: IMPROVE %.4f -> %.4f (+%.3fpp)",
                    family,
                    restart_id,
                    step,
                    cur_m.opt_max,
                    best_n_m.opt_max,
                    delta_pp,
                )
                cur = best_n_cfg
                cur_m = best_n_m
                if rank_key(cur_m) > rank_key(restart_best_m):
                    restart_best_m = cur_m
                    restart_best_cfg = cur
                state.update_restart(
                    family,
                    restart_id,
                    canonical_config(restart_best_cfg),
                    restart_best_m.opt_max,
                    evaluated,
                    "running",
                )
                prog.end_phase("IMPROVE")
            elif (
                abs(delta_pp) < improvement_threshold_pp and neutral_budget > 0 and rank_key(best_n_m) > rank_key(cur_m)
            ):
                log.info(
                    "family=%s restart=%d step=%d: NEUTRAL %.4f -> %.4f (%.3fpp; budget=%d)",
                    family,
                    restart_id,
                    step,
                    cur_m.opt_max,
                    best_n_m.opt_max,
                    delta_pp,
                    neutral_budget - 1,
                )
                cur = best_n_cfg
                cur_m = best_n_m
                neutral_budget -= 1
                if rank_key(cur_m) > rank_key(restart_best_m):
                    restart_best_m = cur_m
                    restart_best_cfg = cur
                state.update_restart(
                    family,
                    restart_id,
                    canonical_config(restart_best_cfg),
                    restart_best_m.opt_max,
                    evaluated,
                    "running",
                )
                prog.end_phase("NEUTRAL")
            else:
                log.info(
                    "family=%s restart=%d step=%d: STOP delta=%.3fpp",
                    family,
                    restart_id,
                    step,
                    delta_pp,
                )
                prog.end_phase("STOP")
                break
            step += 1

        # Update global best from the restart PEAK, not from the walked end-state.
        if best_m is None or rank_key(restart_best_m) > rank_key(best_m):
            improved = best_m is None or restart_best_m.opt_max > best_m.opt_max + improvement_threshold
            log.info(
                "family=%s restart=%d: NEW BEST opt_max=%.4f (improved=%s)",
                family,
                restart_id,
                restart_best_m.opt_max,
                improved,
            )
            best_m = restart_best_m
            best_cfg = restart_best_cfg
            prog.update_best(best_m.opt_max)
        state.update_restart(
            family,
            restart_id,
            canonical_config(restart_best_cfg),
            restart_best_m.opt_max,
            evaluated,
            "done",
        )
        restarts_run += 1

    return best_cfg, best_m, evaluated, cache_hits, restarts_run


# ---------- family orchestrator ----------


def _select_search_mode(mode_flag: str, k: int, exhaustive_cutoff: int) -> str:
    if mode_flag == "exhaustive":
        return "exhaustive"
    if mode_flag == "hillclimb":
        return "hillclimb"
    return "exhaustive" if k <= exhaustive_cutoff else "hillclimb"


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
    search_mode: str,
    exhaustive_max_configs: int,
    exhaustive_k_cutoff: int,
    log_level: str,
    progress: ProgressDisplay | QueueProgressDisplay | None = None,
) -> FamilyResult:
    log = _configure_family_logger(family, logs_dir, log_level)
    cache = SimCache(cache_path)
    state = StateStore(state_path)
    prog = progress or ProgressDisplay(enabled=False, use_rich=False)

    t0 = time.perf_counter()
    log.info("family=%s: building eligibility catalog", family)
    catalog_full = optimize_catalog.build_eligibility_catalog(families=[family], daemonsets=daemonsets)
    entries = catalog_full.get(family, [])
    catalog = build_family_catalog(entries)

    eligible_names = {e.def_label for e in entries}
    eligible_defs = [d for d in defs if d["name"] in eligible_names]
    for d in defs:
        if d["name"] not in eligible_names:
            log.warning("def %s has zero eligible instances — dropped from family search", d["name"])
    if not eligible_defs:
        log.error("family=%s: no defs with eligible shapes — aborting family", family)
        elapsed = time.perf_counter() - t0
        state.mark_family(family, "done", verdict="skipped", best=None)
        return FamilyResult(
            family=family,
            baseline_config={},
            baseline_metrics=None,
            best_config=None,
            best_metrics=None,
            verdict="skipped",
            skipped_reason="no eligible defs",
            elapsed_sec=elapsed,
        )

    baseline = baseline_config(family, eligible_defs, entries)
    # Baseline uses PROD-REALITY pod shapes (no D4 adjustment) so gating it
    # against the recommendation catalog (which enforces D4 bounds) is wrong:
    # a baseline can be perfectly-fine-in-prod yet catalog-infeasible because
    # its tight-fit adjustment on the family's largest instance overshoots
    # the D4 upper bound. The correct check is "does the original pod fit?".
    from sim_nodes import ClusterModel, _daemonsets_for_fleet

    scoped_ds = _daemonsets_for_fleet(daemonsets, family)
    real_fleets = ClusterModel().fleets
    if not is_baseline_feasible(baseline, eligible_defs, scoped_ds, real_fleets):
        log.error(
            "family=%s: baseline pods do not physically fit on the biggest in-family "
            "instance — skipping family (check def yaml vs INSTANCE_SPECS)",
            family,
        )
        elapsed = time.perf_counter() - t0
        state.mark_family(family, "done", verdict="skipped", best=None)
        return FamilyResult(
            family=family,
            baseline_config=baseline,
            baseline_metrics=None,
            best_config=None,
            best_metrics=None,
            verdict="skipped",
            skipped_reason="baseline pods do not fit on instance",
            elapsed_sec=elapsed,
        )

    family_def_names = {d["name"] for d in eligible_defs}
    has_family_jobs = any(j.label in family_def_names for j in all_jobs)
    if not has_family_jobs:
        prior_cfg, prior_m, _ = _load_persisted_best(state, family, catalog)
        if prior_cfg is not None and prior_m is not None:
            log.warning(
                "family=%s: no jobs in window but prior best exists — preserving",
                family,
            )
            elapsed = time.perf_counter() - t0
            result = FamilyResult(
                family=family,
                baseline_config=baseline,
                baseline_metrics=prior_m,
                best_config=prior_cfg,
                best_metrics=prior_m,
                verdict="kept_prior_best",
                skipped_reason="no jobs in window (prior best preserved)",
                elapsed_sec=elapsed,
            )
            state.mark_family(
                family,
                "done",
                verdict="kept_prior_best",
                best=_persist_best_payload(prior_cfg, prior_m),
            )
            return result
        log.warning("family=%s: no jobs in window — skipping search", family)
        elapsed = time.perf_counter() - t0
        state.mark_family(family, "done", verdict="skipped", best=None)
        return FamilyResult(
            family=family,
            baseline_config=baseline,
            baseline_metrics=None,
            best_config=None,
            best_metrics=None,
            verdict="skipped",
            skipped_reason="no jobs in window",
            elapsed_sec=elapsed,
        )

    mode = _select_search_mode(search_mode, len(eligible_defs), exhaustive_k_cutoff)
    log.info("family=%s: mode=%s (K=%d defs)", family, mode, len(eligible_defs))
    prog.start_family(family, mode, num_restarts)

    log.info("family=%s: running baseline sim", family)
    seen_configs: set[str] = set()
    try:
        baseline_m, baseline_hit = cached_sim(
            family,
            baseline,
            all_jobs,
            catalog,
            family_def_names,
            runner_fleet,
            sim_flags,
            csv_sha,
            src_shas,
            cache,
            log,
            daemonsets=daemonsets,
            baseline_defs=eligible_defs,
        )
    except Exception as e:
        log.error("family=%s: baseline sim failed: %s", family, e, exc_info=True)
        elapsed = time.perf_counter() - t0
        state.mark_family(family, "done", verdict="skipped", best=None)
        return FamilyResult(
            family=family,
            baseline_config=baseline,
            baseline_metrics=None,
            best_config=None,
            best_metrics=None,
            verdict="skipped",
            skipped_reason=f"baseline sim failed: {e}",
            elapsed_sec=elapsed,
        )
    prog.update_best(baseline_m.opt_max)
    log.info(
        "family=%s: baseline opt_max=%.4f (cpu=%.4f mem=%.4f) vcpu_hours=%.1f",
        family,
        baseline_m.opt_max,
        baseline_m.opt_cpu,
        baseline_m.opt_mem,
        baseline_m.vcpu_hours,
    )
    seen_configs.add(canonical_config(baseline))

    best_cfg = baseline
    best_m = baseline_m
    evaluated_total = 1
    cache_hits_total = 1 if baseline_hit else 0
    restarts_run = 0

    # Resume: pull the strongest previously-persisted config as a warm seed so
    # a prior improvement survives the next launch.
    prior_cfg, prior_m_loaded, _prior_rid = _load_persisted_best(state, family, catalog)
    if prior_cfg is not None and prior_m_loaded is not None:
        prior_key = canonical_config(prior_cfg)
        try:
            prior_m, prior_hit = cached_sim(
                family,
                prior_cfg,
                all_jobs,
                catalog,
                family_def_names,
                runner_fleet,
                sim_flags,
                csv_sha,
                src_shas,
                cache,
                log,
                daemonsets=daemonsets,
            )
            if prior_key not in seen_configs:
                seen_configs.add(prior_key)
                evaluated_total += 1
            if prior_hit:
                cache_hits_total += 1
            if rank_key(prior_m) > rank_key(best_m):
                log.info("family=%s: resumed with prior best opt_max=%.4f", family, prior_m.opt_max)
                best_cfg = prior_cfg
                best_m = prior_m
                prog.update_best(best_m.opt_max)
        except Exception as e:
            log.warning("family=%s: prior best sim failed: %s", family, e, exc_info=True)

    if mode == "exhaustive":
        cfg, m, evaluated, cache_hits = _search_exhaustive(
            family,
            eligible_defs,
            entries,
            all_jobs,
            runner_fleet,
            sim_flags,
            csv_sha,
            src_shas,
            cache,
            log,
            exhaustive_max_configs,
            prog,
            best_cfg,
            best_m,
            daemonsets,
            seen_configs,
        )
        if cfg is None and m is None:
            # Exhaustive capped out — fall back to hillclimb.
            mode = "hillclimb"
            prog.start_family(family, mode, num_restarts, best_m.opt_max if best_m else 0.0)
            cfg, m, evaluated, cache_hits, restarts_run = _search_hillclimb(
                family,
                eligible_defs,
                entries,
                all_jobs,
                runner_fleet,
                sim_flags,
                csv_sha,
                src_shas,
                cache,
                state,
                log,
                num_restarts,
                neutral_move_limit,
                improvement_threshold_pp,
                prog,
                baseline,
                best_cfg,
                best_m,
                daemonsets,
                seen_configs,
            )
        evaluated_total += evaluated
        if cache_hits > 0:
            cache_hits_total += cache_hits
        if cfg is not None and m is not None:
            best_cfg = cfg
            best_m = m
    else:
        cfg, m, evaluated, cache_hits, restarts_run = _search_hillclimb(
            family,
            eligible_defs,
            entries,
            all_jobs,
            runner_fleet,
            sim_flags,
            csv_sha,
            src_shas,
            cache,
            state,
            log,
            num_restarts,
            neutral_move_limit,
            improvement_threshold_pp,
            prog,
            baseline,
            best_cfg,
            best_m,
            daemonsets,
            seen_configs,
        )
        evaluated_total += evaluated
        cache_hits_total += cache_hits
        if cfg is not None and m is not None:
            best_cfg = cfg
            best_m = m

    improvement_threshold = improvement_threshold_pp / 100.0
    if best_m is None or baseline_m is None:
        verdict = "no_change"
        delta_pp = 0.0
    else:
        delta = best_m.opt_max - baseline_m.opt_max
        delta_pp = delta * 100.0
        verdict = "improved" if delta > improvement_threshold else "no_change"

    elapsed = time.perf_counter() - t0
    cache_hit_rate = (cache_hits_total / evaluated_total) if evaluated_total > 0 else 0.0
    log.info(
        "family=%s: DONE verdict=%s baseline=%.4f best=%.4f delta=%.3fpp evaluated=%d restarts=%d elapsed=%.1fs",
        family,
        verdict,
        baseline_m.opt_max if baseline_m else 0.0,
        best_m.opt_max if best_m else 0.0,
        delta_pp,
        evaluated_total,
        restarts_run,
        elapsed,
    )
    prog.end_family()
    result = FamilyResult(
        family=family,
        baseline_config=baseline,
        baseline_metrics=baseline_m,
        best_config=best_cfg if best_cfg is not None else baseline,
        best_metrics=best_m if best_m is not None else baseline_m,
        verdict=verdict,
        configs_evaluated=evaluated_total,
        elapsed_sec=elapsed,
        restarts_run=restarts_run,
        cache_hit_rate=cache_hit_rate,
        mode=mode,
    )
    persisted_best = None
    if result.best_config is not None and result.best_metrics is not None:
        persisted_best = _persist_best_payload(result.best_config, result.best_metrics)
    state.mark_family(family, "done", verdict=verdict, best=persisted_best)
    return result


# ---------- logging setup ----------


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


# ---------- worker entry (spawn) ----------


def _family_worker(kwargs: dict) -> FamilyResult:
    """Pool entry point — kwargs must be picklable."""

    def _sigterm(_signum, _frame):
        # Cooperative shutdown: workers exit cleanly; SqliteCache/StateStore
        # writes are atomic per-commit so no state loss beyond in-flight sim.
        sys.exit(0)

    signal.signal(signal.SIGTERM, _sigterm)
    signal.signal(signal.SIGINT, _sigterm)

    queue = kwargs.pop("progress_queue", None)
    family = kwargs["family"]
    prog: QueueProgressDisplay | None = None
    if queue is not None:
        prog = QueueProgressDisplay(family, queue)
        _configure_family_logger(family, kwargs["logs_dir"], kwargs["log_level"])
        for name in ("global", family):
            lg = logging.getLogger(name)
            for h in lg.handlers:
                if isinstance(h, logging.StreamHandler) and not isinstance(h, logging.FileHandler):
                    h.setLevel(logging.WARNING)

    kwargs["progress"] = prog
    result = _search_family(**kwargs)
    if prog is not None:
        prog.report_final(result)
    return result


def _cluster_sim_worker(
    kwargs: dict,
) -> tuple[str, "SimMetrics", dict[str, "SimMetrics | None"]]:
    """Run one full-cluster sim (baseline or recommendation) and extract
    cluster-wide + per-family contribution metrics in-process.

    sim_out is potentially very large; extract in the worker so only the
    distilled metrics cross the process boundary.
    """

    def _sigterm(_signum, _frame):
        sys.exit(0)

    signal.signal(signal.SIGTERM, _sigterm)
    signal.signal(signal.SIGINT, _sigterm)

    which: str = kwargs["which"]
    jobs: list[Job] = kwargs["jobs"]
    fleets_extra: dict[str, FleetSpec] | None = kwargs["fleets_extra"]
    family_pool_map: dict[str, set[str]] = kwargs["family_pool_map"]
    sim_flags: dict = kwargs["sim_flags"]
    logs_dir: Path = kwargs["logs_dir"]
    log_level: str = kwargs["log_level"]

    log = _configure_global_logger(logs_dir, log_level)
    log.info("cluster sim %s: starting (%d jobs, %d extra fleets)", which, len(jobs), len(fleets_extra or {}))
    t0 = time.perf_counter()
    sim_out = optimize_engine.run_cluster_sim(jobs, fleets_extra, sim_flags)
    cluster_m = optimize_engine.extract_cluster_metrics(sim_out)
    contribs: dict[str, SimMetrics | None] = {}
    for family, pools in family_pool_map.items():
        if not pools:
            contribs[family] = None
            continue
        contribs[family] = optimize_engine.extract_family_contribution_metrics(sim_out, pools)
    elapsed = time.perf_counter() - t0
    log.info(
        "cluster sim %s: done opt_max=%.4f vcpu_hours=%.0f elapsed=%.1fs",
        which,
        cluster_m.opt_max,
        cluster_m.vcpu_hours,
        elapsed,
    )
    return which, cluster_m, contribs


# ---------- signal handling ----------


def _install_shutdown_handler(output_dir: Path, log: logging.Logger) -> None:
    def _handler(signum, _frame):
        log.warning(
            "signal %s received — state is checkpointed; resume with: --resume %s",
            signum,
            output_dir,
        )
        sys.exit(0)

    signal.signal(signal.SIGINT, _handler)
    signal.signal(signal.SIGTERM, _handler)


# ---------- validation phase ----------


def _run_cluster_validation_phase(
    *,
    results: list[FamilyResult],
    csv_path: Path,
    drop_providers: set[str],
    add_runner_pods: bool,
    args,
    daemonsets,
    defs_by_family: dict[str, list[dict]],
    sim_flags: dict,
    logs_dir: Path,
    global_log: logging.Logger,
) -> "ClusterValidationResult | None":
    """One full-cluster before/after: baseline (as-is) + recommendation (all
    improved families' best_configs merged into the shared fleet set).

    Runs two sims in parallel via a spawn Pool (2 workers). Each worker
    extracts cluster-wide + per-family contribution metrics IN-PROCESS so the
    large sim_out dict never crosses the queue.
    """
    global_log.info("cluster validation: loading full dataset (no --last-days filter) from %s", csv_path)
    full_jobs = sim_load.load_jobs(
        csv_path,
        add_runner_pods=add_runner_pods,
        runner_pool=args.runner_pool,
        drop_providers=drop_providers,
        keep_fraction=args.keep_fraction,
        keep_seed=args.keep_seed,
        last_days=None,
    )
    global_log.info(
        "cluster validation: loaded %d jobs (vs %s in search window)",
        len(full_jobs),
        args.last_days,
    )
    if not full_jobs:
        global_log.warning("cluster validation: no jobs in full dataset — skipping")
        return None

    validation_days: int | None = None
    min_s = min(j.start_bucket for j in full_jobs)
    max_s = max(j.start_bucket for j in full_jobs)
    span_sec = max(0, max_s - min_s)
    validation_days = max(1, int(round(span_sec / 86400)))

    overrides: dict[str, dict] = {}
    for r in results:
        if r.verdict != "improved" or r.best_config is None:
            continue
        catalog_full = optimize_catalog.build_eligibility_catalog(
            families=[r.family],
            daemonsets=daemonsets,
        )
        entries = catalog_full.get(r.family, [])
        by_pair = {(e.def_label, e.instance): e for e in entries}
        for sub_id, spec in r.best_config.items():
            inst = spec["instance"]
            for def_label in spec["pods"]:
                entry = by_pair.get((def_label, inst))
                if entry is None:
                    global_log.warning(
                        "cluster validation: family=%s def=%s inst=%s not in catalog — skipping override",
                        r.family,
                        def_label,
                        inst,
                    )
                    continue
                overrides[def_label] = {
                    "pool": sub_id,
                    "cpu_m": entry.slot_cpu_m,
                    "mem_mi": entry.slot_mem_mi,
                    "gpu": entry.slot_gpu,
                }

    fleets_extra = optimize_engine.build_cluster_fleets_extra(results)
    rec_jobs = optimize_engine.apply_recommendations_to_jobs(full_jobs, overrides)

    family_pool_map_baseline: dict[str, set[str]] = {}
    family_pool_map_rec: dict[str, set[str]] = {}
    for r in results:
        defs = defs_by_family.get(r.family) or []
        baseline_pools = {d.get("nodepool") for d in defs if d.get("nodepool")}
        family_pool_map_baseline[r.family] = baseline_pools
        if r.verdict == "improved" and r.best_config is not None:
            family_pool_map_rec[r.family] = set(r.best_config.keys())
        else:
            family_pool_map_rec[r.family] = baseline_pools

    tasks: list[dict] = [
        {
            "which": "baseline",
            "jobs": full_jobs,
            "fleets_extra": None,
            "family_pool_map": family_pool_map_baseline,
            "sim_flags": sim_flags,
            "logs_dir": logs_dir,
            "log_level": args.log_level,
        },
        {
            "which": "recommendation",
            "jobs": rec_jobs,
            "fleets_extra": fleets_extra,
            "family_pool_map": family_pool_map_rec,
            "sim_flags": sim_flags,
            "logs_dir": logs_dir,
            "log_level": args.log_level,
        },
    ]

    global_log.info(
        "cluster validation: dispatching 2 sims (baseline + recommendation) across 2 workers",
    )

    by_which: dict[str, tuple[SimMetrics, dict[str, SimMetrics | None]]] = {}
    t0 = time.perf_counter()
    ctx = mp.get_context("spawn")
    with ctx.Pool(processes=2, maxtasksperchild=1) as pool:
        for which, cluster_m, contribs in pool.imap_unordered(_cluster_sim_worker, tasks):
            by_which[which] = (cluster_m, contribs)
    elapsed = time.perf_counter() - t0

    if "baseline" not in by_which or "recommendation" not in by_which:
        global_log.error(
            "cluster validation: missing sim result(s) — got keys=%s",
            sorted(by_which),
        )
        return None

    base_m, base_contribs = by_which["baseline"]
    rec_m, rec_contribs = by_which["recommendation"]

    for r in results:
        r.cluster_baseline_metrics = base_contribs.get(r.family)
        r.cluster_rec_metrics = rec_contribs.get(r.family)

    delta_pp = (rec_m.opt_max - base_m.opt_max) * 100.0
    global_log.info(
        "cluster validation: baseline opt_max=%.1f%% rec opt_max=%.1f%% delta=%+.2fpp elapsed=%.1fs",
        base_m.opt_max * 100.0,
        rec_m.opt_max * 100.0,
        delta_pp,
        elapsed,
    )

    per_family_contrib: dict[str, tuple[SimMetrics | None, SimMetrics | None]] = {}
    for r in results:
        per_family_contrib[r.family] = (
            base_contribs.get(r.family),
            rec_contribs.get(r.family),
        )

    return ClusterValidationResult(
        baseline_metrics=base_m,
        recommendation_metrics=rec_m,
        days=validation_days,
        elapsed_sec=elapsed,
        per_family_contrib=per_family_contrib,
    )


# ---------- main ----------


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--csv", default=str(HERE / "pytorch_60d.csv"))
    ap.add_argument("--last-days", type=int, default=DEFAULT_LAST_DAYS)
    ap.add_argument("--output-dir", default=None)
    ap.add_argument("--resume", default=None)
    ap.add_argument("--family", action="append", default=None)
    ap.add_argument("--num-workers", type=int, default=None)
    ap.add_argument(
        "--num-restarts",
        type=int,
        default=DEFAULT_NUM_RESTARTS,
        help="number of RANDOM restarts on top of baseline; "
        f"default {DEFAULT_NUM_RESTARTS} = baseline + {DEFAULT_NUM_RESTARTS} random seeds",
    )
    ap.add_argument("--improvement-threshold-pp", type=float, default=DEFAULT_IMPROVEMENT_THRESHOLD_PP)
    ap.add_argument("--neutral-move-limit", type=int, default=DEFAULT_NEUTRAL_MOVE_LIMIT)
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--force", action="store_true", help="ignore per-family 'done' status on resume and re-run")
    ap.add_argument("--log-level", choices=["debug", "info", "warning"], default="info")
    ap.add_argument("--drop-provider", action="append", default=None)
    ap.add_argument("--keep-fraction", type=float, default=1.0)
    ap.add_argument("--keep-seed", type=int, default=12345)
    ap.add_argument("--no-runner-pods", action="store_true")
    ap.add_argument("--runner-pool", default=RUNNER_POD_POOL)
    ap.add_argument("--no-progress", action="store_true")
    ap.add_argument(
        "--search-mode",
        choices=["exhaustive", "hillclimb", "auto"],
        default="auto",
        help="auto: exhaustive for K<=%d, else hillclimb" % DEFAULT_EXHAUSTIVE_K_CUTOFF,
    )
    ap.add_argument(
        "--exhaustive-max-configs",
        type=int,
        default=DEFAULT_EXHAUSTIVE_MAX_CONFIGS,
        help="cap on enumerated configs before exhaustive falls back to hillclimb",
    )
    ap.add_argument(
        "--rename-threshold-pct",
        type=float,
        default=DEFAULT_RENAME_THRESHOLD_PCT,
        help="cpu/mem adjustment > this %% flags def rename required in patch",
    )
    ap.add_argument(
        "--skip-validation",
        action="store_true",
        help="skip the cluster-wide validation phase (two full-dataset sims: baseline "
        "and combined recommendation) — set to speed up iterative dev runs",
    )
    args = ap.parse_args(argv)

    output_dir = (
        Path(args.resume) if args.resume else (Path(args.output_dir) if args.output_dir else _default_output_dir())
    )
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

    defs_by_family = load_defs_by_family()
    families = tuple(f for f in families if defs_by_family.get(f))

    if args.dry_run:
        global_log.info("dry-run: invoking optimize_catalog for %s", families)
        return optimize_catalog.main([f"--family={f}" for f in families] + [f"--output={output_dir / 'catalog.json'}"])

    csv_path = Path(args.csv)
    if not csv_path.is_file():
        global_log.error("CSV not found: %s", csv_path)
        return 1

    global_log.info("hashing sim source files")
    src_shas = _sim_source_shas()
    global_log.info("hashing CSV")
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

    sim_flags = {
        "seed": 42,
        "placeholder_max_age": 2,
        "warmup_default": 1,
        "warmup_gpu": 2,
        "warmup_baremetal": 3,
        "placeholders_enabled": True,
        # load_jobs args — must be part of the cache key so different job
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
    worker_kwargs: list[dict] = []
    resumed_results: list[FamilyResult] = []
    for family in families:
        defs = defs_by_family.get(family) or []
        if not defs:
            global_log.warning("family %s has no in-scope defs — skipping", family)
            continue
        prior_family_status = state.family_status(family)
        if prior_family_status == "done" and not args.force:
            global_log.info("skipping family %s (already done) — loading persisted result", family)
            resumed = _reconstitute_family_result(state, family, defs, daemonsets)
            if resumed is not None:
                resumed_results.append(resumed)
            else:
                global_log.warning(
                    "family %s: state marked done but no persistable result — global report will omit",
                    family,
                )
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
                "search_mode": args.search_mode,
                "exhaustive_max_configs": args.exhaustive_max_configs,
                "exhaustive_k_cutoff": DEFAULT_EXHAUSTIVE_K_CUTOFF,
                "log_level": args.log_level,
            }
        )

    if not worker_kwargs and not resumed_results:
        global_log.warning("no families to run")
        return 0

    cpu = os.cpu_count() or 2
    workers = args.num_workers if args.num_workers else max(1, min(len(worker_kwargs), cpu - 2))
    global_log.info("dispatching %d families across %d workers", len(worker_kwargs), workers)

    is_tty = sys.stderr.isatty()
    progress_requested = not args.no_progress
    single_worker_progress = progress_requested and is_tty and workers == 1
    multi_worker_progress = progress_requested and is_tty and workers > 1
    if progress_requested and not is_tty:
        global_log.info("live progress disabled: stderr is not a TTY")

    use_rich = False
    if single_worker_progress or multi_worker_progress:
        try:
            import rich  # noqa: F401

            use_rich = True
        except ImportError:
            use_rich = False

    results: list[FamilyResult] = list(resumed_results)
    t0 = time.perf_counter()

    if workers == 1:
        for kw in worker_kwargs:
            fam_log = _configure_family_logger(kw["family"], logs_dir, args.log_level)
            prog = ProgressDisplay(
                enabled=single_worker_progress,
                use_rich=use_rich,
                loggers=[global_log, fam_log],
            )
            try:
                results.append(_search_family(progress=prog, **kw))
            finally:
                prog.close()
    else:
        multi_display: MultiFamilyProgressDisplay | None = None
        if multi_worker_progress:
            multi_display = MultiFamilyProgressDisplay(
                families=[kw["family"] for kw in worker_kwargs],
                enabled=True,
                use_rich=use_rich,
                loggers=[global_log],
            )
            multi_display.start()
            for kw in worker_kwargs:
                kw["progress_queue"] = multi_display.queue

        ctx = mp.get_context("spawn")
        try:
            with ctx.Pool(processes=workers, maxtasksperchild=1) as pool:
                for r in pool.imap_unordered(_family_worker, worker_kwargs):
                    results.append(r)
                    elapsed = time.perf_counter() - t0
                    done = len(results)
                    total = len(worker_kwargs)
                    remaining = total - done
                    eta = (elapsed / done) * remaining if done else 0.0
                    global_log.info(
                        "heartbeat: %d/%d families done, elapsed=%.0fs, eta=%.0fs",
                        done,
                        total,
                        elapsed,
                        eta,
                    )
        finally:
            if multi_display is not None:
                multi_display.close()

    cluster_val: ClusterValidationResult | None = None
    if not args.skip_validation:
        cluster_val = _run_cluster_validation_phase(
            results=results,
            csv_path=csv_path,
            drop_providers=drop_providers,
            add_runner_pods=add_runner_pods,
            args=args,
            daemonsets=daemonsets,
            defs_by_family=defs_by_family,
            sim_flags=sim_flags,
            logs_dir=logs_dir,
            global_log=global_log,
        )
    else:
        global_log.info("--skip-validation set — skipping cluster-wide validation phase")

    for r in results:
        defs = defs_by_family[r.family]
        entries = optimize_catalog.build_eligibility_catalog(families=[r.family], daemonsets=daemonsets).get(
            r.family, []
        )
        optimize_report.write_family_report(reports_dir, r, defs, entries)
        optimize_report.write_family_patch(
            reports_dir,
            r,
            defs,
            entries,
            rename_threshold_pct=args.rename_threshold_pct,
        )
    optimize_report.write_global_report(reports_dir, results, cluster_validation=cluster_val)

    global_log.info("all done. Reports in %s", reports_dir)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
