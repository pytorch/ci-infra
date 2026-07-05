"""Unit tests for optimize_search (Phase 2 sim-driven per-family partition search).

The module is a mix of pure logic (mode selection, seeds, config (de)serialization,
persistence rehydration) and orchestration (argparse main(), multiprocessing Pool,
SIGINT/SIGTERM handlers, SQLite resume, live progress, per-family workers). Pure
logic is covered directly; orchestration is exercised through small serial
end-to-end main() runs over a tiny CSV and through direct calls to the worker /
validation entrypoints with signal.signal monkeypatched so process signal state is
never mutated.
"""

from __future__ import annotations

import argparse
import json
import logging
import signal
import subprocess
import sys
import types

import optimize_catalog
import optimize_engine
import optimize_search as osrch
import pytest
import sim_load
from daemonset_overhead import discover_daemonsets
from optimize_config import PROD_PARITY_SIM_FLAGS, load_defs_by_family
from optimize_engine import FamilyResult, baseline_config, build_family_catalog, canonical_config, enumerate_neighbors
from optimize_storage import SimCache, SimMetrics, StateStore

# ---------- shared fixtures ----------


@pytest.fixture(autouse=True)
def _reset_loggers():
    """Close/detach every non-root logger handler after each test.

    main() and the logger-setup helpers install FileHandlers on module-level
    named loggers ("global", "<family>") guarded by `if not log.handlers`, so a
    stale handler from an earlier test would keep writing into a tmp dir that a
    later test's tmp rotation may have deleted. Clearing after each test keeps
    every run pointing at its own dir.
    """
    yield
    for name in list(logging.root.manager.loggerDict):
        lg = logging.getLogger(name)
        for h in list(lg.handlers):
            h.close()
            lg.removeHandler(h)


@pytest.fixture(scope="module")
def daemonsets():
    return discover_daemonsets(osrch.REPO_ROOT)


@pytest.fixture(scope="module")
def defs_by_family():
    return load_defs_by_family()


@pytest.fixture(scope="module")
def runner_fleet():
    return osrch._real_runner_fleet()


@pytest.fixture(scope="module")
def sim_flags():
    flags = {
        "seed": 42,
        "placeholder_max_age": 2,
        "warmup_default": 1,
        "warmup_gpu": 2,
        "warmup_baremetal": 3,
        "placeholders_enabled": True,
        "empty_ttl_buckets": 1,
    }
    flags.update(PROD_PARITY_SIM_FLAGS)
    return flags


@pytest.fixture(scope="module")
def m7g_csv(tmp_path_factory):
    """Single-def (m7g) workload — small enough that exhaustive enumerates fast."""
    p = tmp_path_factory.mktemp("m7g") / "m7g.csv"
    p.write_text(
        "provider,label,nodepool,nodepool_fraction,start_time,end_time\n"
        "lf,l-arm64g3-16-62,m7g,1,2026-07-01T18:00:00+0000,2026-07-01T18:30:00+0000\n"
        "lf,l-arm64g3-16-62,m7g,1,2026-07-01T18:05:00+0000,2026-07-01T18:35:00+0000\n"
        "mt,l-arm64g3-16-62,m7g,1,2026-07-01T18:10:00+0000,2026-07-01T18:40:00+0000\n"
    )
    return p


@pytest.fixture(scope="module")
def r7i_csv(tmp_path_factory):
    """Two-def (r7i) workload — exercises hill-climb neighbor moves."""
    p = tmp_path_factory.mktemp("r7i") / "r7i.csv"
    p.write_text(
        "provider,label,nodepool,nodepool_fraction,start_time,end_time\n"
        "lf,l-x86iamx-16-128,r7i,1,2026-07-01T18:00:00+0000,2026-07-01T18:30:00+0000\n"
        "lf,l-x86iamx-8-64,r7i,1,2026-07-01T18:05:00+0000,2026-07-01T18:35:00+0000\n"
        "mt,l-x86iamx-16-128,r7i,1,2026-07-01T18:10:00+0000,2026-07-01T18:40:00+0000\n"
        "mt,l-x86iamx-8-64,r7i,1,2026-07-01T18:15:00+0000,2026-07-01T18:45:00+0000\n"
    )
    return p


@pytest.fixture(scope="module")
def m7g_jobs(m7g_csv):
    return sim_load.load_jobs(m7g_csv, last_days=0)


@pytest.fixture(scope="module")
def r7i_jobs(r7i_csv):
    return sim_load.load_jobs(r7i_csv, last_days=0)


@pytest.fixture
def capture_signals(monkeypatch):
    """Record handlers passed to signal.signal without installing them.

    Workers / handlers call signal.signal(SIGTERM/SIGINT, ...); recording rather
    than installing means the test process's own signal disposition is untouched.
    Returns the {signum: handler} dict, populated as the code under test runs.
    """
    captured: dict[int, object] = {}

    def _spy(signum, handler):
        captured[signum] = handler

    monkeypatch.setattr(signal, "signal", _spy)
    return captured


def _search_kwargs(family, defs, jobs, daemonsets, runner_fleet, sim_flags, work_dir, **over):
    kw = {
        "family": family,
        "defs": defs,
        "all_jobs": jobs,
        "daemonsets": daemonsets,
        "runner_fleet": runner_fleet,
        "sim_flags": sim_flags,
        "csv_sha": "csv-sha-test",
        "src_shas": {},
        "cache_path": work_dir / "cache.sqlite",
        "state_path": work_dir / "state.sqlite",
        "logs_dir": work_dir / "logs",
        "num_restarts": 0,
        "improvement_threshold_pp": 0.1,
        "neutral_move_limit": 3,
        "search_mode": "exhaustive",
        "exhaustive_max_configs": 10000,
        "exhaustive_k_cutoff": 5,
        "log_level": "warning",
    }
    kw.update(over)
    return kw


# ---------- pure logic: mode / seed / output dir / git ----------


def test_select_search_mode_explicit_flags():
    assert osrch._select_search_mode("exhaustive", 100, 5) == "exhaustive"
    assert osrch._select_search_mode("hillclimb", 1, 5) == "hillclimb"


def test_select_search_mode_auto_at_and_below_cutoff():
    assert osrch._select_search_mode("auto", 5, 5) == "exhaustive"
    assert osrch._select_search_mode("auto", 4, 5) == "exhaustive"


def test_select_search_mode_auto_above_cutoff():
    assert osrch._select_search_mode("auto", 6, 5) == "hillclimb"


def test_restart_seed_is_deterministic_and_family_specific():
    assert osrch._restart_seed("c7i", 0) == osrch._restart_seed("c7i", 0)
    assert osrch._restart_seed("c7i", 0) != osrch._restart_seed("c7i", 1)
    assert osrch._restart_seed("c7i", 0) != osrch._restart_seed("m7g", 0)
    assert 0 <= osrch._restart_seed("c7i", 0) < 2**32


def test_default_output_dir_shape():
    d = osrch._default_output_dir()
    assert d.parent == osrch.HERE / "output"
    assert d.name.endswith(("-" + osrch._git_sha(), "-nogit")) or "-" in d.name


def test_git_sha_returns_str():
    assert isinstance(osrch._git_sha(), str)


def test_git_sha_falls_back_to_nogit_on_failure(monkeypatch):
    def _boom(*_a, **_k):
        raise subprocess.CalledProcessError(1, "git")

    monkeypatch.setattr(subprocess, "check_output", _boom)
    assert osrch._git_sha() == "nogit"


def test_sha256_file_matches_hashlib(tmp_path):
    import hashlib

    p = tmp_path / "blob.bin"
    p.write_bytes(b"hello sweep")
    assert osrch._sha256_file(p) == hashlib.sha256(b"hello sweep").hexdigest()


def test_sim_source_shas_includes_expected_keys():
    shas = osrch._sim_source_shas()
    assert "daemonsets_discovered" in shas
    assert "simulate_py" in shas
    assert "optimize_engine_py" in shas
    assert all(isinstance(v, str) and len(v) == 64 for v in shas.values())


def test_real_runner_fleet_is_runner_pool(runner_fleet):
    assert runner_fleet.name == sim_load.RUNNER_POD_POOL
    assert len(runner_fleet.instances) > 0


def test_real_runner_fleet_missing_pool_raises(monkeypatch):
    class _FakeModel:
        def __init__(self):
            self.fleets: dict = {}

    monkeypatch.setattr(osrch, "ClusterModel", _FakeModel)
    with pytest.raises(RuntimeError, match="missing fleet"):
        osrch._real_runner_fleet()


# ---------- config (de)serialization ----------


def _catalog_pairs():
    return {("def-a", "c7i.2xlarge"): object(), ("def-b", "c7i.2xlarge"): object()}


def test_config_from_persisted_json_valid():
    catalog = _catalog_pairs()
    raw = json.dumps({"c7i__c7i.2xlarge": {"instance": "c7i.2xlarge", "pods": ["def-b", "def-a"]}})
    cfg = osrch._config_from_persisted_json(raw, catalog)
    assert cfg == {"c7i__c7i.2xlarge": {"instance": "c7i.2xlarge", "pods": ["def-a", "def-b"]}}


def test_config_from_persisted_json_pod_not_eligible():
    catalog = _catalog_pairs()
    raw = json.dumps({"s": {"instance": "c7i.2xlarge", "pods": ["def-c"]}})
    assert osrch._config_from_persisted_json(raw, catalog) is None


def test_config_from_persisted_json_malformed_and_nondict():
    catalog = _catalog_pairs()
    assert osrch._config_from_persisted_json("not json", catalog) is None
    assert osrch._config_from_persisted_json(json.dumps([1, 2]), catalog) is None


def test_config_from_persisted_json_missing_keys():
    catalog = _catalog_pairs()
    assert osrch._config_from_persisted_json(json.dumps({"s": {"instance": "x"}}), catalog) is None


def test_config_from_persisted_json_pods_not_list():
    catalog = _catalog_pairs()
    raw = json.dumps({"s": {"instance": "c7i.2xlarge", "pods": "def-a"}})
    assert osrch._config_from_persisted_json(raw, catalog) is None


def test_config_from_persisted_json_spec_not_dict():
    catalog = _catalog_pairs()
    assert osrch._config_from_persisted_json(json.dumps({"s": [1, 2]}), catalog) is None


# ---------- metrics rehydration + persist payload ----------


def _sim_metrics():
    return SimMetrics(opt_max=0.5, opt_cpu=0.4, opt_mem=0.5, cal_cpu=0.3, cal_mem=0.35, vcpu_hours=100.0)


def test_persist_best_payload_roundtrip():
    cfg = {"sid": {"instance": "c7i.2xlarge", "pods": ["a", "b"]}}
    payload = osrch._persist_best_payload(cfg, _sim_metrics())
    assert payload["opt_max"] == 0.5
    assert payload["metrics"]["vcpu_hours"] == 100.0
    assert payload["config"]["sid"]["pods"] == ["a", "b"]
    m = osrch._metrics_from_persisted(payload)
    assert m is not None
    assert m.opt_max == 0.5
    assert m.vcpu_hours == 100.0


def test_metrics_from_persisted_legacy_opt_max_only():
    m = osrch._metrics_from_persisted({"opt_max": 0.7})
    assert m is not None
    assert m.opt_max == 0.7
    assert m.opt_cpu == 0.0
    assert m.vcpu_hours == 0.0


def test_metrics_from_persisted_missing_vcpu_defaults_zero():
    md = {"opt_max": 0.1, "opt_cpu": 0.1, "opt_mem": 0.1, "cal_cpu": 0.1, "cal_mem": 0.1}
    m = osrch._metrics_from_persisted({"metrics": md})
    assert m is not None
    assert m.vcpu_hours == 0.0


def test_metrics_from_persisted_none_and_empty():
    assert osrch._metrics_from_persisted(None) is None
    assert osrch._metrics_from_persisted({}) is None


def test_metrics_from_persisted_bad_metrics_dict():
    assert osrch._metrics_from_persisted({"metrics": {"opt_cpu": 0.1}}) is None


def test_metrics_from_persisted_bad_legacy_value():
    assert osrch._metrics_from_persisted({"opt_max": "not-a-float"}) is None


# ---------- state-backed helpers ----------


def test_load_persisted_best_from_family_json(tmp_path, daemonsets):
    state = StateStore(tmp_path / "s.sqlite")
    entries = optimize_catalog.build_eligibility_catalog(families=["m7g"], daemonsets=daemonsets).get("m7g", [])
    catalog = build_family_catalog(entries)
    inst = entries[0].instance
    cfg = {f"m7g__{inst}": {"instance": inst, "pods": ["l-arm64g3-16-62"]}}
    state.mark_family("m7g", "done", verdict="improved", best=osrch._persist_best_payload(cfg, _sim_metrics()))
    got_cfg, got_m, _rid = osrch._load_persisted_best(state, "m7g", catalog)
    assert got_cfg == cfg
    assert got_m is not None
    assert got_m.opt_max == 0.5


def test_load_persisted_best_restart_rows_tiebreak(tmp_path, daemonsets):
    state = StateStore(tmp_path / "s.sqlite")
    entries = optimize_catalog.build_eligibility_catalog(families=["m7g"], daemonsets=daemonsets).get("m7g", [])
    catalog = build_family_catalog(entries)
    inst = entries[0].instance
    cfg_json = json.dumps({f"m7g__{inst}": {"instance": inst, "pods": ["l-arm64g3-16-62"]}})
    # opt_max None -> skipped; infeasible cfg -> skipped; valid high opt_max -> wins.
    state.update_restart("m7g", 0, cfg_json, None, 1, "running")
    state.update_restart("m7g", 1, json.dumps({"s": {"instance": inst, "pods": ["ghost"]}}), 0.9, 1, "done")
    state.update_restart("m7g", 2, cfg_json, 0.8, 3, "done")
    got_cfg, got_m, rid = osrch._load_persisted_best(state, "m7g", catalog)
    assert got_cfg is not None
    assert got_m is not None
    assert got_m.opt_max == pytest.approx(0.8)
    assert rid == 2


def test_load_persisted_best_empty_state(tmp_path):
    state = StateStore(tmp_path / "s.sqlite")
    catalog: dict = {}
    cfg, m, rid = osrch._load_persisted_best(state, "m7g", catalog)
    assert cfg is None
    assert m is None
    assert rid is None


def test_load_persisted_best_ignores_malformed_family_json(tmp_path):
    state = StateStore(tmp_path / "s.sqlite")
    # best_json present but not valid JSON -> family branch swallowed, no rows -> None.
    with state._conn() as conn:
        conn.execute(
            "INSERT OR REPLACE INTO family_state VALUES (?, ?, ?, ?, ?)",
            ("m7g", "done", "improved", "{not-json", 0),
        )
    cfg, m, _rid = osrch._load_persisted_best(state, "m7g", {})
    assert cfg is None
    assert m is None


def test_reconstitute_family_result_done_with_best(
    tmp_path, daemonsets, defs_by_family, m7g_jobs, runner_fleet, sim_flags
):
    work = tmp_path
    kw = _search_kwargs("m7g", defs_by_family["m7g"], m7g_jobs, daemonsets, runner_fleet, sim_flags, work)
    result = osrch._search_family(**kw)
    assert result.verdict in {"improved", "no_change"}
    state = StateStore(work / "state.sqlite")
    recon = osrch._reconstitute_family_result(state, "m7g", defs_by_family["m7g"], daemonsets)
    assert recon is not None
    assert recon.family == "m7g"
    assert recon.best_config is not None
    assert recon.baseline_metrics is not None


def test_reconstitute_family_result_no_state_returns_none(tmp_path, daemonsets, defs_by_family):
    state = StateStore(tmp_path / "s.sqlite")
    assert osrch._reconstitute_family_result(state, "m6i", defs_by_family["m6i"], daemonsets) is None


def test_reconstitute_family_result_skipped(tmp_path, daemonsets, defs_by_family):
    state = StateStore(tmp_path / "s.sqlite")
    state.mark_family("m6i", "done", verdict="skipped", best=None)
    recon = osrch._reconstitute_family_result(state, "m6i", defs_by_family["m6i"], daemonsets)
    assert recon is not None
    assert recon.verdict == "skipped"
    assert recon.best_config is None
    assert recon.skipped_reason == "resumed: no persisted metrics"


# ---------- logging setup ----------


def test_configure_family_logger(tmp_path):
    log = osrch._configure_family_logger("famX", tmp_path / "logs", "debug")
    assert log.name == "famX"
    assert log.level == logging.DEBUG
    assert log.propagate is False
    assert len(log.handlers) == 2
    assert (tmp_path / "logs" / "famX.log").is_file()


def test_configure_family_logger_idempotent(tmp_path):
    log1 = osrch._configure_family_logger("famY", tmp_path / "logs", "info")
    log2 = osrch._configure_family_logger("famY", tmp_path / "logs", "info")
    assert log1 is log2
    assert len(log2.handlers) == 2


def test_configure_family_logger_bad_level_defaults_info(tmp_path):
    log = osrch._configure_family_logger("famZ", tmp_path / "logs", "nonsense")
    assert log.level == logging.INFO


def test_configure_global_logger(tmp_path):
    log = osrch._configure_global_logger(tmp_path / "logs", "warning")
    assert log.name == "global"
    assert log.level == logging.WARNING
    assert (tmp_path / "logs" / "global.log").is_file()


# ---------- signal handlers ----------


def test_install_shutdown_handler_registers_and_exits(tmp_path, capture_signals):
    log = logging.getLogger("shutdown-probe")
    log.addHandler(logging.NullHandler())
    osrch._install_shutdown_handler(tmp_path, log)
    assert signal.SIGINT in capture_signals
    assert signal.SIGTERM in capture_signals
    with pytest.raises(SystemExit) as ei:
        capture_signals[signal.SIGINT](signal.SIGINT, None)
    assert ei.value.code == 0


# ---------- search strategies ----------


def test_search_exhaustive_capped_returns_sentinel(
    tmp_path, daemonsets, defs_by_family, m7g_jobs, runner_fleet, sim_flags
):
    entries = optimize_catalog.build_eligibility_catalog(families=["m7g"], daemonsets=daemonsets).get("m7g", [])
    log = osrch._configure_family_logger("cap-probe", tmp_path / "logs", "warning")
    cache = SimCache(tmp_path / "cache.sqlite")
    prog = osrch.ProgressDisplay(enabled=False, use_rich=False)
    best_cfg, best_m, evaluated, cache_hits = osrch._search_exhaustive(
        "m7g",
        defs_by_family["m7g"],
        entries,
        m7g_jobs,
        runner_fleet,
        sim_flags,
        "csv",
        {},
        cache,
        log,
        0,  # max_configs=0 forces the cap
        prog,
        None,
        None,
        daemonsets,
        set(),
    )
    assert best_cfg is None
    assert best_m is None
    assert evaluated == 0
    assert cache_hits == -1


def test_search_exhaustive_happy_path(tmp_path, daemonsets, defs_by_family, m7g_jobs, runner_fleet, sim_flags):
    entries = optimize_catalog.build_eligibility_catalog(families=["m7g"], daemonsets=daemonsets).get("m7g", [])
    log = osrch._configure_family_logger("exh-probe", tmp_path / "logs", "warning")
    cache = SimCache(tmp_path / "cache.sqlite")
    prog = osrch.ProgressDisplay(enabled=False, use_rich=False)
    best_cfg, best_m, evaluated, _hits = osrch._search_exhaustive(
        "m7g",
        defs_by_family["m7g"],
        entries,
        m7g_jobs,
        runner_fleet,
        sim_flags,
        "csv",
        {},
        cache,
        log,
        10000,
        prog,
        None,
        None,
        daemonsets,
        set(),
    )
    assert best_cfg is not None
    assert best_m is not None
    assert evaluated >= 1


def test_search_exhaustive_sim_exception_is_logged_and_skipped(
    tmp_path, daemonsets, defs_by_family, m7g_jobs, runner_fleet, sim_flags, monkeypatch
):
    entries = optimize_catalog.build_eligibility_catalog(families=["m7g"], daemonsets=daemonsets).get("m7g", [])
    log = osrch._configure_family_logger("exh-boom", tmp_path / "logs", "warning")
    cache = SimCache(tmp_path / "cache.sqlite")
    prog = osrch.ProgressDisplay(enabled=False, use_rich=False)

    def _boom(*_a, **_k):
        raise RuntimeError("sim boom")

    monkeypatch.setattr(osrch, "cached_sim", _boom)
    best_cfg, best_m, evaluated, _hits = osrch._search_exhaustive(
        "m7g",
        defs_by_family["m7g"],
        entries,
        m7g_jobs,
        runner_fleet,
        sim_flags,
        "csv",
        {},
        cache,
        log,
        10000,
        prog,
        None,
        None,
        daemonsets,
        set(),
    )
    # Every sim raised -> no config ever wins, seed best (None) preserved.
    assert best_cfg is None
    assert best_m is None
    assert evaluated == 0


def test_search_family_happy_path_exhaustive(tmp_path, daemonsets, defs_by_family, m7g_jobs, runner_fleet, sim_flags):
    kw = _search_kwargs("m7g", defs_by_family["m7g"], m7g_jobs, daemonsets, runner_fleet, sim_flags, tmp_path)
    r = osrch._search_family(**kw)
    assert r.family == "m7g"
    assert r.mode == "exhaustive"
    assert r.verdict in {"improved", "no_change"}
    assert r.best_config is not None
    assert r.baseline_metrics is not None
    assert r.baseline_cost is not None


def test_search_family_hillclimb_and_resume_skips_done_restart(
    tmp_path, daemonsets, defs_by_family, r7i_jobs, runner_fleet, sim_flags
):
    kw = _search_kwargs(
        "r7i",
        defs_by_family["r7i"],
        r7i_jobs,
        daemonsets,
        runner_fleet,
        sim_flags,
        tmp_path,
        search_mode="hillclimb",
        num_restarts=1,
    )
    r1 = osrch._search_family(**kw)
    assert r1.mode == "hillclimb"
    assert r1.verdict in {"improved", "no_change"}
    # Restart rows now persisted; a second run over the same state skips done restarts.
    r2 = osrch._search_family(**kw)
    assert r2.verdict in {"improved", "no_change"}
    state = StateStore(tmp_path / "state.sqlite")
    assert state.restart_status("r7i", 0) == "done"


def test_search_family_exhaustive_cap_falls_back_to_hillclimb(
    tmp_path, daemonsets, defs_by_family, m7g_jobs, runner_fleet, sim_flags
):
    kw = _search_kwargs(
        "m7g",
        defs_by_family["m7g"],
        m7g_jobs,
        daemonsets,
        runner_fleet,
        sim_flags,
        tmp_path,
        search_mode="exhaustive",
        exhaustive_max_configs=0,
    )
    r = osrch._search_family(**kw)
    assert r.mode == "hillclimb"


def test_search_family_no_eligible_defs(tmp_path, daemonsets, m7g_jobs, runner_fleet, sim_flags):
    bogus = [
        {
            "name": "BOGUS-DEF",
            "instance_type": "m7g.8xlarge",
            "nodepool": "m7g",
            "vcpu": 16.0,
            "memory_gib": 62.0,
            "gpu": 0,
        }
    ]
    kw = _search_kwargs("m7g", bogus, m7g_jobs, daemonsets, runner_fleet, sim_flags, tmp_path)
    r = osrch._search_family(**kw)
    assert r.verdict == "skipped"
    assert r.skipped_reason == "no eligible defs"


def test_search_family_no_jobs_in_window(tmp_path, daemonsets, defs_by_family, r7i_jobs, runner_fleet, sim_flags):
    # r7i_jobs contains no m7g labels -> the family has no jobs in the window.
    kw = _search_kwargs("m7g", defs_by_family["m7g"], r7i_jobs, daemonsets, runner_fleet, sim_flags, tmp_path)
    r = osrch._search_family(**kw)
    assert r.verdict == "skipped"
    assert r.skipped_reason == "no jobs in window"


def test_search_family_no_jobs_but_prior_best_preserved(
    tmp_path, daemonsets, defs_by_family, m7g_jobs, r7i_jobs, runner_fleet, sim_flags
):
    kw = _search_kwargs("m7g", defs_by_family["m7g"], m7g_jobs, daemonsets, runner_fleet, sim_flags, tmp_path)
    first = osrch._search_family(**kw)
    assert first.best_config is not None
    # Re-run same state with a window that has no m7g jobs -> prior best kept.
    kw2 = dict(kw)
    kw2["all_jobs"] = r7i_jobs
    r = osrch._search_family(**kw2)
    assert r.verdict == "kept_prior_best"
    assert r.best_config is not None


def test_search_family_no_change_reuses_baseline_cost(
    tmp_path, daemonsets, defs_by_family, m7g_jobs, runner_fleet, sim_flags, monkeypatch
):
    # Flat metrics for every config -> best never beats baseline -> verdict
    # no_change and final_cfg == baseline, so rec_cost is reused from baseline_cost.
    flat = SimMetrics(0.30, 0.30, 0.20, 0.2, 0.2, 50.0)
    monkeypatch.setattr(osrch, "cached_sim", lambda *_a, **_k: (flat, False))
    kw = _search_kwargs("m7g", defs_by_family["m7g"], m7g_jobs, daemonsets, runner_fleet, sim_flags, tmp_path)
    r = osrch._search_family(**kw)
    assert r.verdict == "no_change"
    assert r.best_config == r.baseline_config
    assert r.rec_cost == r.baseline_cost


def test_search_family_baseline_sim_failure(
    tmp_path, daemonsets, defs_by_family, m7g_jobs, runner_fleet, sim_flags, monkeypatch
):
    def _boom(*_a, **_k):
        raise RuntimeError("baseline boom")

    monkeypatch.setattr(osrch, "cached_sim", _boom)
    kw = _search_kwargs("m7g", defs_by_family["m7g"], m7g_jobs, daemonsets, runner_fleet, sim_flags, tmp_path)
    r = osrch._search_family(**kw)
    assert r.verdict == "skipped"
    assert "baseline sim failed" in r.skipped_reason


def test_search_family_cost_computation_failure_is_swallowed(
    tmp_path, daemonsets, defs_by_family, m7g_jobs, runner_fleet, sim_flags, monkeypatch
):
    def _boom(*_a, **_k):
        raise RuntimeError("cost boom")

    monkeypatch.setattr(optimize_engine, "cost_for_config", _boom)
    kw = _search_kwargs("m7g", defs_by_family["m7g"], m7g_jobs, daemonsets, runner_fleet, sim_flags, tmp_path)
    r = osrch._search_family(**kw)
    # Search still completes; cost fields just stay None.
    assert r.verdict in {"improved", "no_change"}
    assert r.baseline_cost is None
    assert r.rec_cost is None


# ---------- workers ----------


def test_family_worker_no_queue(
    tmp_path, daemonsets, defs_by_family, m7g_jobs, runner_fleet, sim_flags, capture_signals
):
    kw = _search_kwargs("m7g", defs_by_family["m7g"], m7g_jobs, daemonsets, runner_fleet, sim_flags, tmp_path)
    result = osrch._family_worker(dict(kw))
    assert isinstance(result, FamilyResult)
    assert result.family == "m7g"
    # Worker installed cooperative shutdown handlers; invoking one exits cleanly.
    with pytest.raises(SystemExit) as ei:
        capture_signals[signal.SIGTERM](signal.SIGTERM, None)
    assert ei.value.code == 0


def test_family_worker_with_queue_reports_final(
    tmp_path, daemonsets, defs_by_family, m7g_jobs, runner_fleet, sim_flags, capture_signals
):
    import multiprocessing as mp

    q = mp.Manager().Queue()
    kw = _search_kwargs("m7g", defs_by_family["m7g"], m7g_jobs, daemonsets, runner_fleet, sim_flags, tmp_path)
    kw["progress_queue"] = q
    result = osrch._family_worker(dict(kw))
    assert result.family == "m7g"
    # QueueProgressDisplay pushed at least start_family + report_final onto the queue.
    assert q.qsize() >= 1


def test_cluster_sim_worker(tmp_path, daemonsets, m7g_jobs, sim_flags, capture_signals):
    task = {
        "which": "baseline",
        "jobs": m7g_jobs,
        "fleets_extra": None,
        "family_pool_map": {"m7g": {"m7g"}, "empty": set()},
        "sim_flags": sim_flags,
        "logs_dir": tmp_path / "logs",
        "log_level": "warning",
    }
    which, cluster_m, contribs, cost = osrch._cluster_sim_worker(dict(task))
    assert which == "baseline"
    assert isinstance(cluster_m, SimMetrics)
    assert contribs["empty"] is None  # empty pool set -> None contribution
    assert contribs["m7g"] is not None
    assert "node_hours" in cost
    with pytest.raises(SystemExit) as ei:
        capture_signals[signal.SIGINT](signal.SIGINT, None)
    assert ei.value.code == 0


# ---------- cluster validation phase ----------


def _val_args():
    return argparse.Namespace(
        runner_pool=sim_load.RUNNER_POD_POOL,
        keep_fraction=1.0,
        keep_seed=12345,
        last_days=0,
        log_level="warning",
    )


def test_run_cluster_validation_phase_happy(
    tmp_path, daemonsets, defs_by_family, m7g_jobs, m7g_csv, runner_fleet, sim_flags
):
    kw = _search_kwargs("m7g", defs_by_family["m7g"], m7g_jobs, daemonsets, runner_fleet, sim_flags, tmp_path)
    r = osrch._search_family(**kw)
    glog = osrch._configure_global_logger(tmp_path / "logs", "warning")
    res = osrch._run_cluster_validation_phase(
        results=[r],
        csv_path=m7g_csv,
        drop_providers=set(),
        add_runner_pods=True,
        args=_val_args(),
        daemonsets=daemonsets,
        defs_by_family=defs_by_family,
        sim_flags=sim_flags,
        logs_dir=tmp_path / "logs",
        global_log=glog,
    )
    assert res is not None
    assert res.days is not None
    assert res.baseline_metrics is not None
    assert res.recommendation_metrics is not None
    # Per-family contribution written back onto the result.
    assert r.cluster_baseline_metrics is not None or r.cluster_rec_metrics is not None


def test_run_cluster_validation_phase_empty_jobs_returns_none(
    tmp_path, daemonsets, defs_by_family, m7g_jobs, m7g_csv, runner_fleet, sim_flags
):
    kw = _search_kwargs("m7g", defs_by_family["m7g"], m7g_jobs, daemonsets, runner_fleet, sim_flags, tmp_path)
    r = osrch._search_family(**kw)
    glog = osrch._configure_global_logger(tmp_path / "logs", "warning")
    res = osrch._run_cluster_validation_phase(
        results=[r],
        csv_path=m7g_csv,
        drop_providers={"lf", "mt"},  # drops every row -> empty dataset
        add_runner_pods=True,
        args=_val_args(),
        daemonsets=daemonsets,
        defs_by_family=defs_by_family,
        sim_flags=sim_flags,
        logs_dir=tmp_path / "logs",
        global_log=glog,
    )
    assert res is None


def test_run_cluster_validation_phase_override_missing_catalog_entry(
    tmp_path, daemonsets, defs_by_family, m7g_csv, sim_flags
):
    glog = osrch._configure_global_logger(tmp_path / "logs", "warning")
    # improved result whose best_config references a (def, instance) pair absent
    # from the catalog -> override-skip warning branch.
    bogus = FamilyResult(
        family="m7g",
        baseline_config={},
        baseline_metrics=_sim_metrics(),
        best_config={"m7g__bogus.instance": {"instance": "bogus.instance", "pods": ["l-arm64g3-16-62"]}},
        best_metrics=_sim_metrics(),
        verdict="improved",
    )
    res = osrch._run_cluster_validation_phase(
        results=[bogus],
        csv_path=m7g_csv,
        drop_providers=set(),
        add_runner_pods=True,
        args=_val_args(),
        daemonsets=daemonsets,
        defs_by_family=defs_by_family,
        sim_flags=sim_flags,
        logs_dir=tmp_path / "logs",
        global_log=glog,
    )
    assert res is not None


# ---------- main(): argparse surface ----------


def test_main_rejects_out_of_scope_family(tmp_path, m7g_csv):
    rc = osrch.main(
        ["--csv", str(m7g_csv), "--family", "not-a-family", "--output-dir", str(tmp_path), "--log-level", "warning"]
    )
    assert rc == 2


def test_main_missing_csv_returns_one(tmp_path):
    rc = osrch.main(
        [
            "--csv",
            str(tmp_path / "absent.csv"),
            "--family",
            "m7g",
            "--output-dir",
            str(tmp_path / "out"),
            "--skip-validation",
            "--skip-runner-fleet",
            "--log-level",
            "warning",
        ]
    )
    assert rc == 1


def test_main_dry_run_writes_catalog(tmp_path, m7g_csv):
    out = tmp_path / "dry"
    rc = osrch.main(
        ["--csv", str(m7g_csv), "--family", "m7g", "--dry-run", "--output-dir", str(out), "--log-level", "warning"]
    )
    assert rc == 0
    assert (out / "catalog.json").is_file()


def test_main_invalid_runner_fleet_arch_choice(tmp_path, m7g_csv):
    with pytest.raises(SystemExit) as ei:
        osrch.main(
            ["--csv", str(m7g_csv), "--family", "m7g", "--runner-fleet-arch", "x86", "--output-dir", str(tmp_path)]
        )
    assert ei.value.code == 2


def test_main_invalid_search_mode_choice(tmp_path, m7g_csv):
    with pytest.raises(SystemExit) as ei:
        osrch.main(["--csv", str(m7g_csv), "--family", "m7g", "--search-mode", "bogus", "--output-dir", str(tmp_path)])
    assert ei.value.code == 2


def test_main_invalid_log_level_choice(tmp_path, m7g_csv):
    with pytest.raises(SystemExit) as ei:
        osrch.main(["--csv", str(m7g_csv), "--log-level", "trace", "--output-dir", str(tmp_path)])
    assert ei.value.code == 2


def test_main_keep_fraction_out_of_range_raises(tmp_path, m7g_csv):
    with pytest.raises(ValueError, match="keep_fraction"):
        osrch.main(
            [
                "--csv",
                str(m7g_csv),
                "--family",
                "m7g",
                "--keep-fraction",
                "2.0",
                "--output-dir",
                str(tmp_path / "kf"),
                "--skip-validation",
                "--skip-runner-fleet",
                "--log-level",
                "warning",
            ]
        )


def test_main_no_families_to_run(tmp_path, m7g_csv, monkeypatch):
    # Family survives the in-scope check but has no defs -> filtered out -> RC 0.
    monkeypatch.setattr(osrch, "load_defs_by_family", lambda: {"m7g": []})
    rc = osrch.main(
        [
            "--csv",
            str(m7g_csv),
            "--family",
            "m7g",
            "--output-dir",
            str(tmp_path / "empty"),
            "--skip-validation",
            "--skip-runner-fleet",
            "--log-level",
            "warning",
        ]
    )
    assert rc == 0


# ---------- main(): serial end-to-end ----------


def test_main_serial_full_pipeline(tmp_path, m7g_csv):
    out = tmp_path / "full"
    rc = osrch.main(
        [
            "--csv",
            str(m7g_csv),
            "--family",
            "m7g",
            "--num-workers",
            "1",
            "--num-restarts",
            "0",
            "--output-dir",
            str(out),
            "--no-progress",
            "--last-days",
            "0",
            "--log-level",
            "warning",
            "--runner-fleet-arch",
            "arm64",
        ]
    )
    assert rc == 0
    reports = {p.name for p in (out / "reports").iterdir()}
    assert {"m7g.md", "m7g.patch", "global.md", "runner_fleet.md"} <= reports


def test_main_computed_worker_count_serial(tmp_path, m7g_csv):
    # No --num-workers -> exercises the max(1, min(len, cpu-2)) computation.
    out = tmp_path / "computed"
    rc = osrch.main(
        [
            "--csv",
            str(m7g_csv),
            "--family",
            "m7g",
            "--num-restarts",
            "0",
            "--output-dir",
            str(out),
            "--no-progress",
            "--last-days",
            "0",
            "--log-level",
            "warning",
            "--skip-validation",
            "--skip-runner-fleet",
        ]
    )
    assert rc == 0


def test_main_resume_and_force(tmp_path, m7g_csv):
    out = tmp_path / "resume"
    base = [
        "--csv",
        str(m7g_csv),
        "--family",
        "m7g",
        "--num-workers",
        "1",
        "--num-restarts",
        "0",
        "--no-progress",
        "--last-days",
        "0",
        "--log-level",
        "warning",
        "--skip-validation",
        "--skip-runner-fleet",
    ]
    assert osrch.main([*base, "--output-dir", str(out)]) == 0
    # Second invocation resumes: family is 'done' -> reconstituted, not re-run.
    assert osrch.main([*base, "--resume", str(out)]) == 0
    # --force ignores the 'done' status and re-runs the family.
    assert osrch.main([*base, "--resume", str(out), "--force"]) == 0


def test_main_multiworker_spawn_pool(tmp_path, tmp_path_factory):
    csv = tmp_path_factory.mktemp("mw") / "mw.csv"
    csv.write_text(
        "provider,label,nodepool,nodepool_fraction,start_time,end_time\n"
        "lf,l-arm64g3-16-62,m7g,1,2026-07-01T18:00:00+0000,2026-07-01T18:30:00+0000\n"
        "lf,l-x86aavx512-125-463,m6i,1,2026-07-01T18:05:00+0000,2026-07-01T18:35:00+0000\n"
        "mt,l-arm64g3-16-62,m7g,1,2026-07-01T18:10:00+0000,2026-07-01T18:40:00+0000\n"
        "mt,l-x86aavx512-125-463,m6i,1,2026-07-01T18:15:00+0000,2026-07-01T18:45:00+0000\n"
    )
    out = tmp_path / "mw"
    rc = osrch.main(
        [
            "--csv",
            str(csv),
            "--family",
            "m7g",
            "--family",
            "m6i",
            "--num-workers",
            "2",
            "--num-restarts",
            "0",
            "--output-dir",
            str(out),
            "--no-progress",
            "--last-days",
            "0",
            "--log-level",
            "warning",
            "--skip-validation",
            "--skip-runner-fleet",
        ]
    )
    assert rc == 0


def test_main_progress_enabled_single_worker(tmp_path, m7g_csv, monkeypatch):
    """Cover the TTY progress-enabled wiring (use_rich, ProgressDisplay, close)."""

    class _FakeProg:
        def __init__(self, *_a, **_k):
            self.closed = False

        def close(self):
            self.closed = True

        def __getattr__(self, _name):
            return lambda *_a, **_k: None

    class _FakeStderr:
        def isatty(self):
            return True

        def write(self, *_a):
            return None

        def flush(self):
            return None

    monkeypatch.setattr(osrch, "ProgressDisplay", _FakeProg)
    monkeypatch.setattr(osrch.sys, "stderr", _FakeStderr())
    # Fake rich so the `import rich; use_rich = True` branch is taken.
    monkeypatch.setitem(sys.modules, "rich", types.ModuleType("rich"))
    rc = osrch.main(
        [
            "--csv",
            str(m7g_csv),
            "--family",
            "m7g",
            "--num-workers",
            "1",
            "--num-restarts",
            "0",
            "--output-dir",
            str(tmp_path / "prog"),
            "--last-days",
            "0",
            "--log-level",
            "warning",
            "--skip-validation",
            "--skip-runner-fleet",
        ]
    )
    assert rc == 0


# ---------- hill-climb decision logic (stubbed sim for determinism) ----------


class _StubSim:
    """Deterministic cached_sim replacement keyed by canonical config.

    Returns (SimMetrics, was_hit=False) from a {config_key: SimMetrics} map,
    with a default for any config not explicitly listed. This drives the
    hill-climb IMPROVE / NEUTRAL / STOP branches without running real sims,
    which cannot be steered to hit exact deltas.
    """

    def __init__(self, by_key: dict[str, SimMetrics], default: SimMetrics | None):
        self.by_key = by_key
        self.default = default

    def __call__(self, family, config, *_a, **_k):
        m = self.by_key.get(canonical_config(config), self.default)
        if m is None:
            return None, False
        return m, False


def _r7i_setup(daemonsets, defs_by_family, tmp_path, name):
    entries = optimize_catalog.build_eligibility_catalog(families=["r7i"], daemonsets=daemonsets).get("r7i", [])
    defs = defs_by_family["r7i"]
    baseline = baseline_config("r7i", defs, entries)
    log = osrch._configure_family_logger(name, tmp_path / "logs", "info")
    cache = SimCache(tmp_path / "cache.sqlite")
    state = StateStore(tmp_path / "state.sqlite")
    prog = osrch.ProgressDisplay(enabled=False, use_rich=False)
    return entries, defs, baseline, log, cache, state, prog


def _run_hillclimb(defs, entries, baseline, seed_m, log, cache, state, prog, daemonsets, runner_fleet):
    return osrch._search_hillclimb(
        "r7i",
        defs,
        entries,
        [],
        runner_fleet,
        {},
        "csv",
        {},
        cache,
        state,
        log,
        0,  # num_restarts: baseline seed only
        3,  # neutral_move_limit
        0.1,  # improvement_threshold_pp
        prog,
        baseline,
        baseline,
        seed_m,
        daemonsets,
        {canonical_config(baseline)},
    )


def test_hillclimb_improve_then_stop(tmp_path, daemonsets, defs_by_family, runner_fleet, monkeypatch):
    entries, defs, baseline, log, cache, state, prog = _r7i_setup(daemonsets, defs_by_family, tmp_path, "hc-improve")
    bl_key = canonical_config(baseline)
    seed = SimMetrics(0.30, 0.30, 0.20, 0.2, 0.2, 50.0)
    # Baseline low; every neighbor a clear improvement -> IMPROVE, then STOP.
    stub = _StubSim({bl_key: seed}, default=SimMetrics(0.50, 0.50, 0.20, 0.2, 0.2, 40.0))
    monkeypatch.setattr(osrch, "cached_sim", stub)
    best_cfg, best_m, _evaluated, _hits, restarts_run = _run_hillclimb(
        defs, entries, baseline, seed, log, cache, state, prog, daemonsets, runner_fleet
    )
    assert best_m.opt_max == pytest.approx(0.50)
    assert best_cfg is not None
    assert restarts_run == 1


def test_hillclimb_random_restart_seed(tmp_path, daemonsets, defs_by_family, runner_fleet, monkeypatch):
    """num_restarts=1 -> restart_id 1 seeds from random_feasible_config (not the
    baseline), exercising the random-restart seed counter path."""
    entries, defs, baseline, log, cache, state, prog = _r7i_setup(daemonsets, defs_by_family, tmp_path, "hc-rand")
    seed = SimMetrics(0.30, 0.30, 0.20, 0.2, 0.2, 50.0)
    stub = _StubSim({}, default=seed)  # everything flat -> each restart STOPs immediately
    monkeypatch.setattr(osrch, "cached_sim", stub)
    _best_cfg, _best_m, _evaluated, _hits, restarts_run = osrch._search_hillclimb(
        "r7i",
        defs,
        entries,
        [],
        runner_fleet,
        {},
        "csv",
        {},
        cache,
        state,
        log,
        1,  # baseline restart + 1 random restart
        3,
        0.1,
        prog,
        baseline,
        baseline,
        seed,
        daemonsets,
        {canonical_config(baseline)},
    )
    assert restarts_run == 2


def test_hillclimb_neutral_move(tmp_path, daemonsets, defs_by_family, runner_fleet, monkeypatch):
    entries, defs, baseline, log, cache, state, prog = _r7i_setup(daemonsets, defs_by_family, tmp_path, "hc-neutral")
    bl_key = canonical_config(baseline)
    neighbors = enumerate_neighbors("r7i", baseline, defs, entries)
    neutral_key = canonical_config(neighbors[0])
    seed = SimMetrics(0.30, 0.30, 0.20, 0.2, 0.2, 50.0)
    # One neighbor: same opt_max but lower vcpu_hours (rank_key wins) -> NEUTRAL.
    # All other neighbors strictly worse so the walk stops after the neutral move.
    stub = _StubSim(
        {
            bl_key: seed,
            neutral_key: SimMetrics(0.30, 0.30, 0.20, 0.2, 0.2, 40.0),
        },
        default=SimMetrics(0.20, 0.20, 0.10, 0.1, 0.1, 60.0),
    )
    monkeypatch.setattr(osrch, "cached_sim", stub)
    _best_cfg, best_m, _evaluated, _hits, _restarts = _run_hillclimb(
        defs, entries, baseline, seed, log, cache, state, prog, daemonsets, runner_fleet
    )
    assert best_m.opt_max == pytest.approx(0.30)
    assert best_m.vcpu_hours == pytest.approx(40.0)


def test_hillclimb_immediate_stop(tmp_path, daemonsets, defs_by_family, runner_fleet, monkeypatch):
    entries, defs, baseline, log, cache, state, prog = _r7i_setup(daemonsets, defs_by_family, tmp_path, "hc-stop")
    bl_key = canonical_config(baseline)
    seed = SimMetrics(0.50, 0.50, 0.20, 0.2, 0.2, 50.0)
    # Every neighbor strictly worse -> STOP on the first step.
    stub = _StubSim({bl_key: seed}, default=SimMetrics(0.10, 0.10, 0.05, 0.1, 0.1, 60.0))
    monkeypatch.setattr(osrch, "cached_sim", stub)
    _best_cfg, best_m, _evaluated, _hits, _restarts = _run_hillclimb(
        defs, entries, baseline, seed, log, cache, state, prog, daemonsets, runner_fleet
    )
    assert best_m.opt_max == pytest.approx(0.50)


def test_hillclimb_seed_sim_failure_skips_restart(tmp_path, daemonsets, defs_by_family, runner_fleet, monkeypatch):
    entries, defs, baseline, log, cache, state, prog = _r7i_setup(daemonsets, defs_by_family, tmp_path, "hc-seedfail")
    # Seed sim returns None -> restart skipped, nothing run.
    stub = _StubSim({}, default=None)
    monkeypatch.setattr(osrch, "cached_sim", stub)
    best_cfg, best_m, _evaluated, _hits, restarts_run = osrch._search_hillclimb(
        "r7i",
        defs,
        entries,
        [],
        runner_fleet,
        {},
        "csv",
        {},
        cache,
        state,
        log,
        0,
        3,
        0.1,
        prog,
        baseline,
        None,
        None,
        daemonsets,
        set(),
    )
    assert best_cfg is None
    assert best_m is None
    assert restarts_run == 0


def test_hillclimb_all_neighbors_fail(tmp_path, daemonsets, defs_by_family, runner_fleet, monkeypatch):
    entries, defs, baseline, log, cache, state, prog = _r7i_setup(daemonsets, defs_by_family, tmp_path, "hc-nbrfail")
    bl_key = canonical_config(baseline)
    seed = SimMetrics(0.30, 0.30, 0.20, 0.2, 0.2, 50.0)
    # Seed OK, every neighbor sim returns None -> best_n stays None -> STOP break.
    stub = _StubSim({bl_key: seed}, default=None)
    monkeypatch.setattr(osrch, "cached_sim", stub)
    _best_cfg, best_m, _evaluated, _hits, restarts_run = _run_hillclimb(
        defs, entries, baseline, seed, log, cache, state, prog, daemonsets, runner_fleet
    )
    assert best_m.opt_max == pytest.approx(0.30)
    assert restarts_run == 1


# ---------- _search_family extra branches ----------


def test_search_family_baseline_not_feasible(
    tmp_path, daemonsets, defs_by_family, m7g_jobs, runner_fleet, sim_flags, monkeypatch
):
    monkeypatch.setattr(osrch, "is_baseline_feasible", lambda *_a, **_k: False)
    kw = _search_kwargs("m7g", defs_by_family["m7g"], m7g_jobs, daemonsets, runner_fleet, sim_flags, tmp_path)
    r = osrch._search_family(**kw)
    assert r.verdict == "skipped"
    assert r.skipped_reason == "baseline pods do not fit on instance"


def test_search_family_prior_best_sim_failure_is_swallowed(
    tmp_path, daemonsets, defs_by_family, m7g_jobs, runner_fleet, sim_flags, monkeypatch
):
    kw = _search_kwargs("m7g", defs_by_family["m7g"], m7g_jobs, daemonsets, runner_fleet, sim_flags, tmp_path)
    osrch._search_family(**kw)  # populate a persisted prior best
    real_cached_sim = osrch.cached_sim
    calls = {"n": 0}

    def _fail_on_prior(*a, **k):
        calls["n"] += 1
        # call 1 = baseline (must succeed), call 2 = prior-best resume (must raise)
        if calls["n"] == 2:
            raise RuntimeError("prior best boom")
        return real_cached_sim(*a, **k)

    monkeypatch.setattr(osrch, "cached_sim", _fail_on_prior)
    r = osrch._search_family(**kw)
    assert r.verdict in {"improved", "no_change"}


def test_reconstitute_family_result_baseline_value_error(tmp_path, daemonsets, defs_by_family, monkeypatch):
    state = StateStore(tmp_path / "s.sqlite")
    entries = optimize_catalog.build_eligibility_catalog(families=["m7g"], daemonsets=daemonsets).get("m7g", [])
    inst = entries[0].instance
    cfg = {f"m7g__{inst}": {"instance": inst, "pods": ["l-arm64g3-16-62"]}}
    state.mark_family("m7g", "done", verdict="improved", best=osrch._persist_best_payload(cfg, _sim_metrics()))

    def _boom(*_a, **_k):
        raise ValueError("no baseline")

    monkeypatch.setattr(osrch, "baseline_config", _boom)
    recon = osrch._reconstitute_family_result(state, "m7g", defs_by_family["m7g"], daemonsets)
    assert recon is not None
    assert recon.baseline_config == {}


# ---------- validation phase: missing sim result ----------


def test_run_cluster_validation_phase_missing_result(
    tmp_path, daemonsets, defs_by_family, m7g_csv, sim_flags, monkeypatch
):
    glog = osrch._configure_global_logger(tmp_path / "logs", "warning")
    r = FamilyResult(
        family="m7g",
        baseline_config={},
        baseline_metrics=_sim_metrics(),
        best_config=None,
        best_metrics=_sim_metrics(),
        verdict="no_change",
    )

    class _FakePool:
        def __init__(self, *_a, **_k):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *_a):
            return False

        def imap_unordered(self, _fn, _tasks):
            # Only 'baseline' comes back — 'recommendation' is missing.
            yield ("baseline", _sim_metrics(), {"m7g": None}, {"node_hours": 1.0})

    class _FakeCtx:
        def Pool(self, *_a, **_k):
            return _FakePool()

    monkeypatch.setattr(osrch.mp, "get_context", lambda _name: _FakeCtx())
    res = osrch._run_cluster_validation_phase(
        results=[r],
        csv_path=m7g_csv,
        drop_providers=set(),
        add_runner_pods=True,
        args=_val_args(),
        daemonsets=daemonsets,
        defs_by_family=defs_by_family,
        sim_flags=sim_flags,
        logs_dir=tmp_path / "logs",
        global_log=glog,
    )
    assert res is None


# ---------- main(): resumed-family reconstitution warning branch ----------


def test_main_resume_reconstitute_omit_warns(tmp_path, m7g_csv, monkeypatch):
    out = tmp_path / "recon"
    base = [
        "--csv",
        str(m7g_csv),
        "--family",
        "m7g",
        "--num-workers",
        "1",
        "--num-restarts",
        "0",
        "--no-progress",
        "--last-days",
        "0",
        "--log-level",
        "warning",
        "--skip-validation",
        "--skip-runner-fleet",
    ]
    assert osrch.main([*base, "--output-dir", str(out)]) == 0
    # Force reconstitution to fail so the "no persistable result" branch runs;
    # with no families to run and no resumed results, main returns 0.
    monkeypatch.setattr(osrch, "_reconstitute_family_result", lambda *_a, **_k: None)
    assert osrch.main([*base, "--resume", str(out)]) == 0


def test_main_multiworker_progress_enabled(tmp_path, tmp_path_factory, monkeypatch):
    csv = tmp_path_factory.mktemp("mwp") / "mwp.csv"
    csv.write_text(
        "provider,label,nodepool,nodepool_fraction,start_time,end_time\n"
        "lf,l-arm64g3-16-62,m7g,1,2026-07-01T18:00:00+0000,2026-07-01T18:30:00+0000\n"
        "lf,l-x86aavx512-125-463,m6i,1,2026-07-01T18:05:00+0000,2026-07-01T18:35:00+0000\n"
        "mt,l-arm64g3-16-62,m7g,1,2026-07-01T18:10:00+0000,2026-07-01T18:40:00+0000\n"
        "mt,l-x86aavx512-125-463,m6i,1,2026-07-01T18:15:00+0000,2026-07-01T18:45:00+0000\n"
    )

    import multiprocessing as mp

    class _FakeMulti:
        def __init__(self, *_a, **_k):
            self.queue = mp.Manager().Queue()
            self.started = False
            self.closed = False

        def start(self):
            self.started = True

        def close(self):
            self.closed = True

    class _FakeStderr:
        def isatty(self):
            return True

        def write(self, *_a):
            return None

        def flush(self):
            return None

    monkeypatch.setattr(osrch, "MultiFamilyProgressDisplay", _FakeMulti)
    monkeypatch.setattr(osrch.sys, "stderr", _FakeStderr())
    monkeypatch.setitem(sys.modules, "rich", types.ModuleType("rich"))
    rc = osrch.main(
        [
            "--csv",
            str(csv),
            "--family",
            "m7g",
            "--family",
            "m6i",
            "--num-workers",
            "2",
            "--num-restarts",
            "0",
            "--output-dir",
            str(tmp_path / "mwp"),
            "--last-days",
            "0",
            "--log-level",
            "warning",
            "--skip-validation",
            "--skip-runner-fleet",
        ]
    )
    assert rc == 0


# ---------- final gap-closers ----------


def test_hillclimb_neighbor_sim_raises(tmp_path, daemonsets, defs_by_family, runner_fleet, monkeypatch):
    """A neighbor sim that raises is caught in _sim and treated as a failed move."""
    entries, defs, baseline, log, cache, state, prog = _r7i_setup(daemonsets, defs_by_family, tmp_path, "hc-raise")
    bl_key = canonical_config(baseline)
    seed = SimMetrics(0.30, 0.30, 0.20, 0.2, 0.2, 50.0)

    def _raise_on_neighbor(_family, config, *_a, **_k):
        if canonical_config(config) == bl_key:
            return seed, False
        raise RuntimeError("neighbor sim boom")

    monkeypatch.setattr(osrch, "cached_sim", _raise_on_neighbor)
    _best_cfg, best_m, _evaluated, _hits, restarts_run = _run_hillclimb(
        defs, entries, baseline, seed, log, cache, state, prog, daemonsets, runner_fleet
    )
    assert best_m.opt_max == pytest.approx(0.30)
    assert restarts_run == 1


def test_main_progress_requested_but_not_tty(tmp_path, m7g_csv):
    """Under pytest stderr is not a TTY; omitting --no-progress hits the
    'live progress disabled' info branch and keeps progress off."""
    rc = osrch.main(
        [
            "--csv",
            str(m7g_csv),
            "--family",
            "m7g",
            "--num-workers",
            "1",
            "--num-restarts",
            "0",
            "--output-dir",
            str(tmp_path / "notty"),
            "--last-days",
            "0",
            "--log-level",
            "warning",
            "--skip-validation",
            "--skip-runner-fleet",
        ]
    )
    assert rc == 0


def test_main_tty_without_rich_falls_back(tmp_path, m7g_csv, monkeypatch):
    """TTY progress requested but rich not importable -> use_rich=False branch."""

    class _FakeProg:
        def __init__(self, *_a, **_k):
            pass

        def close(self):
            return None

        def __getattr__(self, _name):
            return lambda *_a, **_k: None

    class _FakeStderr:
        def isatty(self):
            return True

        def write(self, *_a):
            return None

        def flush(self):
            return None

    monkeypatch.setattr(osrch, "ProgressDisplay", _FakeProg)
    monkeypatch.setattr(osrch.sys, "stderr", _FakeStderr())
    monkeypatch.delitem(sys.modules, "rich", raising=False)
    monkeypatch.setattr("builtins.__import__", _no_rich_import(__import__))
    rc = osrch.main(
        [
            "--csv",
            str(m7g_csv),
            "--family",
            "m7g",
            "--num-workers",
            "1",
            "--num-restarts",
            "0",
            "--output-dir",
            str(tmp_path / "norich"),
            "--last-days",
            "0",
            "--log-level",
            "warning",
            "--skip-validation",
            "--skip-runner-fleet",
        ]
    )
    assert rc == 0


def _no_rich_import(real_import):
    def _imp(name, *a, **k):
        if name == "rich":
            raise ImportError("no rich for this test")
        return real_import(name, *a, **k)

    return _imp


def test_main_full_pipeline_amd64_arm64(tmp_path, m7g_csv):
    """Default (both arch) runner-fleet search surfaces an amd64 winner ->
    exercises the Phase 2.5 amd64-logging branch."""
    out = tmp_path / "amd"
    rc = osrch.main(
        [
            "--csv",
            str(m7g_csv),
            "--family",
            "m7g",
            "--num-workers",
            "1",
            "--num-restarts",
            "0",
            "--output-dir",
            str(out),
            "--no-progress",
            "--last-days",
            "0",
            "--log-level",
            "warning",
            "--skip-validation",
        ]
    )
    assert rc == 0
    assert (out / "reports" / "runner_fleet.md").is_file()
