"""SQLite-backed sim-result cache and per-family search state for the optimizer.

Both stores use WAL mode so multiple worker processes can read/write concurrently
without stomping on each other. Schemas are self-describing; opening an existing
database with new code is safe as long as columns are added, not removed.
"""

from __future__ import annotations

import json
import sqlite3
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from pathlib import Path


@dataclass(frozen=True)
class SimMetrics:
    opt_max: float
    opt_cpu: float
    opt_mem: float
    cal_cpu: float
    cal_mem: float
    # vCPU-hours = allocatable-vcpu-millicores summed across buckets / 1000 / 12
    # (each bucket is 5min = 1/12 hour). Instance-size-invariant WITHIN a family
    # and proportional to $/hr — 1h x 192 vCPU = 12h x 16 vCPU in raw compute.
    vcpu_hours: float
    empty: bool = False
    elapsed_s: float = 0.0
    # Physical vCPU-minutes: sum over live nodes of INSTANCE_SPECS[itype].vcpu x
    # minutes alive. This is real hardware provisioned, distinct from vcpu_hours
    # above (which counts post-kubelet/post-DS allocatable millicores).
    total_vcpu_minutes: float = 0.0
    peak_nodes: int = 0


@dataclass
class ClusterValidationResult:
    """Cluster-wide before/after result from two full-dataset sims (baseline vs
    combined recommendation). See optimize_search._run_cluster_validation_phase.

    `per_family_contrib` maps family -> (baseline_metrics, recommendation_metrics)
    computed by prefix-filtering the same two sim outputs; this lets per-family
    reports show the family's actual contribution to the full-cluster sim without
    running extra sims.
    """

    baseline_metrics: SimMetrics
    recommendation_metrics: SimMetrics
    days: int | None
    elapsed_sec: float
    per_family_contrib: dict[str, tuple[SimMetrics | None, SimMetrics | None]]
    baseline_cost: dict | None = None
    recommendation_cost: dict | None = None


class SimCache:
    """(cache_key) -> SimMetrics. Insert-idempotent; safe under concurrent writers."""

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
                    cal_cpu     REAL NOT NULL,
                    cal_mem     REAL NOT NULL,
                    vcpu_hours  REAL NOT NULL,
                    total_vcpu_minutes REAL NOT NULL,
                    peak_nodes  REAL NOT NULL,
                    empty       INTEGER NOT NULL,
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
                "SELECT opt_max, opt_cpu, opt_mem, cal_cpu, cal_mem, vcpu_hours, "
                "total_vcpu_minutes, peak_nodes, empty, elapsed_s "
                "FROM sim_cache WHERE key=?",
                (key,),
            ).fetchone()
        if row is None:
            return None
        return SimMetrics(
            opt_max=row[0],
            opt_cpu=row[1],
            opt_mem=row[2],
            cal_cpu=row[3],
            cal_mem=row[4],
            vcpu_hours=row[5],
            total_vcpu_minutes=row[6],
            peak_nodes=int(row[7]),
            empty=bool(row[8]),
            elapsed_s=row[9],
        )

    def put(self, key: str, config_json: str, m: SimMetrics) -> None:
        with self._conn() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO sim_cache "
                "(key, config_json, opt_max, opt_cpu, opt_mem, cal_cpu, cal_mem, vcpu_hours, "
                " total_vcpu_minutes, peak_nodes, empty, elapsed_s, computed_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    key,
                    config_json,
                    m.opt_max,
                    m.opt_cpu,
                    m.opt_mem,
                    m.cal_cpu,
                    m.cal_mem,
                    m.vcpu_hours,
                    m.total_vcpu_minutes,
                    m.peak_nodes,
                    int(m.empty),
                    m.elapsed_s,
                    int(time.time()),
                ),
            )


class StateStore:
    """Per-family + per-restart search state, so a resume can skip completed work."""

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
                    restart_best_json   TEXT NOT NULL,
                    best_objective      REAL,
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

    def family_best_json(self, family: str) -> str | None:
        with self._conn() as conn:
            row = conn.execute("SELECT best_json FROM family_state WHERE family=?", (family,)).fetchone()
        return row[0] if row and row[0] else None

    def mark_family(
        self,
        family: str,
        status: str,
        verdict: str | None = None,
        best: dict | None = None,
    ) -> None:
        with self._conn() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO family_state VALUES (?, ?, ?, ?, ?)",
                (family, status, verdict, json.dumps(best) if best else None, int(time.time())),
            )

    def family_verdict(self, family: str) -> str | None:
        with self._conn() as conn:
            row = conn.execute("SELECT verdict FROM family_state WHERE family=?", (family,)).fetchone()
        return row[0] if row else None

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
        restart_best_json: str,
        best_objective: float,
        neighbors_evaluated: int,
        status: str,
    ) -> None:
        with self._conn() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO restart_state VALUES (?, ?, ?, ?, ?, ?, ?)",
                (
                    family,
                    restart_id,
                    restart_best_json,
                    best_objective,
                    neighbors_evaluated,
                    status,
                    int(time.time()),
                ),
            )

    def restart_rows(self, family: str) -> list[tuple[int, str, float | None, str]]:
        """(restart_id, restart_best_json, best_objective, status) rows for a family.

        best_objective is total physical vCPU-minutes (the search objective,
        lower is better)."""
        with self._conn() as conn:
            return list(
                conn.execute(
                    "SELECT restart_id, restart_best_json, best_objective, status FROM restart_state "
                    "WHERE family=? ORDER BY restart_id",
                    (family,),
                ).fetchall()
            )
