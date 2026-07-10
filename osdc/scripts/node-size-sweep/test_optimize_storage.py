"""Unit tests for optimize_storage (SQLite sim-result cache and resume state).

Every store is exercised against a real on-disk SQLite file under pytest's
``tmp_path`` — no mocking, no network, no clusters. Roundtrips assert that the
frozen ``SimMetrics`` dataclass survives a put/get cycle byte-for-byte, that
misses return ``None``, and that re-opening an existing DB with the same code is
safe (the ``CREATE TABLE IF NOT EXISTS`` path).
"""

from __future__ import annotations

import dataclasses
import json
from typing import TYPE_CHECKING

import optimize_storage as st
import pytest

if TYPE_CHECKING:
    from pathlib import Path


def _metrics(opt_max: float = 1.0, empty: bool = False, elapsed_s: float = 2.5) -> st.SimMetrics:
    return st.SimMetrics(
        opt_max=opt_max,
        opt_cpu=0.11,
        opt_mem=0.22,
        cal_cpu=0.33,
        cal_mem=0.44,
        vcpu_hours=123.5,
        empty=empty,
        elapsed_s=elapsed_s,
    )


def test_sim_metrics_defaults() -> None:
    m = st.SimMetrics(opt_max=1.0, opt_cpu=2.0, opt_mem=3.0, cal_cpu=4.0, cal_mem=5.0, vcpu_hours=6.0)
    assert m.empty is False
    assert m.elapsed_s == 0.0


def test_sim_metrics_frozen_and_hashable() -> None:
    m = _metrics()
    assert m == _metrics()
    assert hash(m) == hash(_metrics())
    with pytest.raises(dataclasses.FrozenInstanceError):
        m.opt_max = 9.0  # type: ignore[misc]


def test_cluster_validation_result_defaults() -> None:
    r = st.ClusterValidationResult(
        baseline_metrics=_metrics(opt_max=0.9),
        recommendation_metrics=_metrics(opt_max=0.5),
        days=None,
        elapsed_sec=12.0,
        per_family_contrib={},
    )
    assert r.baseline_cost is None
    assert r.recommendation_cost is None
    assert r.days is None
    assert r.per_family_contrib == {}


def test_cluster_validation_result_all_fields() -> None:
    base = _metrics(opt_max=0.9)
    rec = _metrics(opt_max=0.4)
    contrib: dict[str, tuple[st.SimMetrics | None, st.SimMetrics | None]] = {
        "c7i": (base, rec),
        "g5": (None, None),
    }
    r = st.ClusterValidationResult(
        baseline_metrics=base,
        recommendation_metrics=rec,
        days=7,
        elapsed_sec=30.0,
        per_family_contrib=contrib,
        baseline_cost={"usd": 100.0},
        recommendation_cost={"usd": 60.0},
    )
    assert r.days == 7
    assert r.per_family_contrib["c7i"] == (base, rec)
    assert r.per_family_contrib["g5"] == (None, None)
    assert r.baseline_cost == {"usd": 100.0}
    assert r.recommendation_cost == {"usd": 60.0}


def test_sim_cache_creates_parent_dirs(tmp_path: Path) -> None:
    db = tmp_path / "nested" / "deeper" / "sim.db"
    st.SimCache(db)
    assert db.exists()
    assert db.parent.is_dir()


def test_sim_cache_put_get_roundtrip(tmp_path: Path) -> None:
    cache = st.SimCache(tmp_path / "sim.db")
    m = _metrics(opt_max=0.75, empty=False, elapsed_s=4.0)
    cache.put("k1", json.dumps({"family": "c7i"}), m)
    assert cache.get("k1") == m


def test_sim_cache_roundtrip_empty_true(tmp_path: Path) -> None:
    cache = st.SimCache(tmp_path / "sim.db")
    m = _metrics(empty=True)
    cache.put("empty-key", "{}", m)
    got = cache.get("empty-key")
    assert got is not None
    assert got.empty is True


def test_sim_cache_get_miss_returns_none(tmp_path: Path) -> None:
    cache = st.SimCache(tmp_path / "sim.db")
    assert cache.get("does-not-exist") is None


def test_sim_cache_put_idempotent_replace(tmp_path: Path) -> None:
    cache = st.SimCache(tmp_path / "sim.db")
    cache.put("dup", "{}", _metrics(opt_max=1.0))
    cache.put("dup", "{}", _metrics(opt_max=2.0))
    got = cache.get("dup")
    assert got is not None
    assert got.opt_max == 2.0


def test_sim_cache_reopen_existing_db(tmp_path: Path) -> None:
    db = tmp_path / "sim.db"
    st.SimCache(db).put("persist", "{}", _metrics(opt_max=3.0))
    reopened = st.SimCache(db)
    got = reopened.get("persist")
    assert got is not None
    assert got.opt_max == 3.0


def test_sim_cache_concurrent_writers(tmp_path: Path) -> None:
    db = tmp_path / "sim.db"
    a = st.SimCache(db)
    b = st.SimCache(db)
    a.put("from-a", "{}", _metrics(opt_max=1.0))
    b.put("from-b", "{}", _metrics(opt_max=2.0))
    assert a.get("from-b") is not None
    assert b.get("from-a") is not None


def test_state_store_creates_parent_dirs(tmp_path: Path) -> None:
    db = tmp_path / "nested" / "state.db"
    st.StateStore(db)
    assert db.exists()


def test_family_status_miss_returns_none(tmp_path: Path) -> None:
    store = st.StateStore(tmp_path / "state.db")
    assert store.family_status("c7i") is None


def test_mark_family_roundtrip(tmp_path: Path) -> None:
    store = st.StateStore(tmp_path / "state.db")
    best = {"instances": ["c7i.48xlarge"], "opt_max": 0.9}
    store.mark_family("c7i", "done", verdict="accept", best=best)
    assert store.family_status("c7i") == "done"
    assert store.family_verdict("c7i") == "accept"
    stored = store.family_best_json("c7i")
    assert stored is not None
    assert json.loads(stored) == best


def test_mark_family_without_best_stores_null(tmp_path: Path) -> None:
    store = st.StateStore(tmp_path / "state.db")
    store.mark_family("g5", "running")
    assert store.family_status("g5") == "running"
    assert store.family_best_json("g5") is None
    assert store.family_verdict("g5") is None


def test_family_best_json_miss_returns_none(tmp_path: Path) -> None:
    store = st.StateStore(tmp_path / "state.db")
    assert store.family_best_json("absent") is None


def test_family_verdict_miss_returns_none(tmp_path: Path) -> None:
    store = st.StateStore(tmp_path / "state.db")
    assert store.family_verdict("absent") is None


def test_mark_family_replace_updates_status(tmp_path: Path) -> None:
    store = st.StateStore(tmp_path / "state.db")
    store.mark_family("c7i", "running")
    store.mark_family("c7i", "done", verdict="reject")
    assert store.family_status("c7i") == "done"
    assert store.family_verdict("c7i") == "reject"


def test_restart_status_miss_returns_none(tmp_path: Path) -> None:
    store = st.StateStore(tmp_path / "state.db")
    assert store.restart_status("c7i", 0) is None


def test_update_restart_and_status(tmp_path: Path) -> None:
    store = st.StateStore(tmp_path / "state.db")
    store.update_restart("c7i", 0, json.dumps({"x": 1}), 0.87, 5, "done")
    assert store.restart_status("c7i", 0) == "done"


def test_update_restart_replace(tmp_path: Path) -> None:
    store = st.StateStore(tmp_path / "state.db")
    store.update_restart("c7i", 0, "{}", 0.5, 1, "running")
    store.update_restart("c7i", 0, "{}", 0.4, 9, "done")
    rows = store.restart_rows("c7i")
    assert len(rows) == 1
    assert rows[0][3] == "done"


def test_restart_rows_empty(tmp_path: Path) -> None:
    store = st.StateStore(tmp_path / "state.db")
    assert store.restart_rows("nobody") == []


def test_restart_rows_ordered_by_id(tmp_path: Path) -> None:
    store = st.StateStore(tmp_path / "state.db")
    store.update_restart("c7i", 2, json.dumps({"r": 2}), 0.2, 3, "done")
    store.update_restart("c7i", 0, json.dumps({"r": 0}), 0.9, 1, "done")
    store.update_restart("c7i", 1, json.dumps({"r": 1}), None, 2, "running")
    rows = store.restart_rows("c7i")
    assert [r[0] for r in rows] == [0, 1, 2]
    assert rows[0] == (0, json.dumps({"r": 0}), 0.9, "done")
    assert rows[1][2] is None


def test_state_store_reopen_existing_db(tmp_path: Path) -> None:
    db = tmp_path / "state.db"
    st.StateStore(db).mark_family("c7i", "done", best={"k": "v"})
    reopened = st.StateStore(db)
    assert reopened.family_status("c7i") == "done"
    assert json.loads(reopened.family_best_json("c7i") or "{}") == {"k": "v"}
