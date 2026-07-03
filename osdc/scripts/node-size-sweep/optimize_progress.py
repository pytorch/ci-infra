"""Live terminal progress display for the optimizer search.

Two rendering paths:
- Single-family: `ProgressDisplay` renders one live line/panel via `rich.live.Live`
  when a TTY is present, or CR-updated plain text otherwise.
- Multi-family (worker Pool): `MultiFamilyProgressDisplay` runs in the parent
  and drains state updates from a `multiprocessing.Queue`. Workers push updates
  via `QueueProgressDisplay`, which mirrors `ProgressDisplay`'s API.

Rendering is optional throughout: `enabled=False` yields no-op stubs so callers
never need to branch on TTY-ness.
"""

from __future__ import annotations

import itertools
import logging
import multiprocessing as mp
import sys
import threading
import time
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from optimize_engine import FamilyResult


_SPINNER_UNICODE = "⠋⠙⠹⠸⠼⠴⠶⠧⠇⠏"
_SPINNER_ASCII = "|/-\\"


def _supports_unicode() -> bool:
    enc = (sys.stderr.encoding or "").lower()
    return "utf" in enc


def _spinner_cycle() -> itertools.cycle:
    return itertools.cycle(_SPINNER_UNICODE if _supports_unicode() else _SPINNER_ASCII)


def _bar(done: int, total: int, width: int) -> str:
    if total <= 0:
        return " " * width
    filled = int(width * done / total)
    if filled >= width:
        return "=" * width
    return "=" * filled + ">" + " " * (width - filled - 1)


class ProgressDisplay:
    """Single-family live terminal progress. No-op when disabled."""

    def __init__(
        self,
        *,
        enabled: bool,
        use_rich: bool,
        loggers: list[logging.Logger] | None = None,
    ) -> None:
        self.enabled = enabled
        self.use_rich = use_rich and enabled
        self._loggers = loggers or []
        self._lock = threading.Lock()
        self._family: str | None = None
        self._mode: str = ""
        self._num_restarts: int = 0
        self._restart_idx: int = 0
        self._phase: str = ""
        self._step: int = 0
        self._evaluated: int = 0
        self._total: int = 0
        self._best_opt_max: float = 0.0
        self._spinner_cycle = _spinner_cycle()
        self._spinner_char = next(self._spinner_cycle)

        self._live = None
        self._console = None
        self._rich_handler = None
        self._saved_stream_handlers: list[tuple[logging.Logger, logging.Handler, int]] = []

        if not self.enabled:
            return
        if self.use_rich:
            self._init_rich()
        else:
            self._init_plain()

    def _init_rich(self) -> None:
        try:
            from rich.console import Console
            from rich.live import Live
            from rich.logging import RichHandler
        except ImportError:
            self.use_rich = False
            self._init_plain()
            return

        self._console = Console(file=sys.stderr, force_terminal=True)
        self._live = Live(
            self._render_rich(),
            console=self._console,
            refresh_per_second=10,
            transient=False,
        )
        self._live.start()

        self._rich_handler = RichHandler(
            console=self._console,
            show_time=True,
            show_level=True,
            show_path=False,
            markup=False,
            rich_tracebacks=False,
        )
        self._rich_handler.setFormatter(logging.Formatter("%(name)s %(message)s"))
        for lg in self._loggers:
            for h in list(lg.handlers):
                if isinstance(h, logging.StreamHandler) and not isinstance(h, logging.FileHandler):
                    self._saved_stream_handlers.append((lg, h, h.level))
                    lg.removeHandler(h)
            lg.addHandler(self._rich_handler)

    def _init_plain(self) -> None:
        for lg in self._loggers:
            for h in lg.handlers:
                if isinstance(h, logging.StreamHandler) and not isinstance(h, logging.FileHandler):
                    self._saved_stream_handlers.append((lg, h, h.level))
                    h.setLevel(logging.WARNING)

    def _render_rich(self):
        from rich.text import Text

        t = Text()
        if not self._family:
            t.append("(starting)")
            return t
        t.append(f"{self._family}  ", style="bold cyan")
        if self._mode:
            t.append(f"{self._mode}  ", style="magenta")
        if self._num_restarts:
            t.append(f"restart {self._restart_idx}/{self._num_restarts}  ")
        if self._phase:
            t.append(f"{self._phase}  ", style="yellow")
        if self._total:
            bar = _bar(self._evaluated, self._total, width=12)
            t.append(f"[{bar}]  ", style="green")
            t.append(f"{self._evaluated}/{self._total}  ")
        t.append(f"best {self._best_opt_max:.4f}  ", style="bold")
        t.append(self._spinner_char, style="cyan")
        return t

    def _render_plain_line(self) -> str:
        parts = [self._family or ""]
        if self._mode:
            parts.append(self._mode)
        if self._num_restarts:
            parts.append(f"restart {self._restart_idx}/{self._num_restarts}")
        if self._phase:
            parts.append(self._phase)
        if self._total:
            bar = _bar(self._evaluated, self._total, width=12)
            parts.append(f"[{bar}] {self._evaluated}/{self._total}")
        parts.append(f"best {self._best_opt_max:.4f}")
        parts.append(self._spinner_char)
        return "  ".join(p for p in parts if p)

    def _refresh(self) -> None:
        if not self.enabled:
            return
        if self.use_rich and self._live is not None:
            self._live.update(self._render_rich())
        else:
            sys.stderr.write("\r\x1b[K" + self._render_plain_line())
            sys.stderr.flush()

    def start_family(self, family: str, mode: str, num_restarts: int, best_opt_max: float = 0.0) -> None:
        if not self.enabled:
            return
        with self._lock:
            self._family = family
            self._mode = mode
            self._num_restarts = num_restarts
            self._restart_idx = 0
            self._phase = "baseline"
            self._step = 0
            self._evaluated = 0
            self._total = 0
            self._best_opt_max = best_opt_max
            self._spinner_char = next(self._spinner_cycle)
        self._refresh()

    def update_best(self, best_opt_max: float) -> None:
        if not self.enabled:
            return
        with self._lock:
            if best_opt_max > self._best_opt_max:
                self._best_opt_max = best_opt_max
        self._refresh()

    def start_restart(self, restart_idx: int, phase: str) -> None:
        if not self.enabled:
            return
        with self._lock:
            self._restart_idx = restart_idx + 1
            self._phase = phase
            self._step = 0
            self._evaluated = 0
            self._total = 0
        self._refresh()

    def start_phase(self, phase: str, total: int) -> None:
        if not self.enabled:
            return
        with self._lock:
            self._phase = phase
            self._evaluated = 0
            self._total = total
        self._refresh()

    def advance(self, current_best_opt_max: float | None = None) -> None:
        if not self.enabled:
            return
        with self._lock:
            self._evaluated += 1
            if current_best_opt_max is not None and current_best_opt_max > self._best_opt_max:
                self._best_opt_max = current_best_opt_max
            self._spinner_char = next(self._spinner_cycle)
        self._refresh()

    def end_phase(self, result: str) -> None:
        if not self.enabled:
            return
        with self._lock:
            self._phase = f"{self._phase} {result}"
        self._refresh()

    def end_family(self) -> None:
        if not self.enabled:
            return
        with self._lock:
            self._phase = "done"
        self._refresh()

    def close(self) -> None:
        if not self.enabled:
            return
        try:
            if self.use_rich and self._live is not None:
                self._live.stop()
        finally:
            for lg, handler, level in self._saved_stream_handlers:
                if self._rich_handler is not None and self._rich_handler in lg.handlers:
                    lg.removeHandler(self._rich_handler)
                handler.setLevel(level)
                if handler not in lg.handlers:
                    lg.addHandler(handler)
            self._saved_stream_handlers.clear()
            if not self.use_rich:
                sys.stderr.write("\n")
                sys.stderr.flush()


class QueueProgressDisplay:
    """Worker-side stand-in that forwards state changes to the parent over a queue.

    Mirrors `ProgressDisplay`'s API so worker code doesn't branch. Non-terminal
    updates use `put_nowait` (drop rather than block); the terminal `family_end`
    uses blocking `put` so the parent can render DONE.
    """

    def __init__(self, family: str, queue) -> None:
        self._family = family
        self._queue = queue
        self._num_restarts = 0
        self._restart_idx = 0
        self._best_opt_max = 0.0

    def _try_put(self, msg: dict) -> None:
        try:
            self._queue.put_nowait(msg)
        except Exception:
            pass

    def start_family(self, family: str, mode: str, num_restarts: int, best_opt_max: float = 0.0) -> None:
        self._num_restarts = num_restarts
        self._best_opt_max = best_opt_max
        self._try_put(
            {
                "kind": "family_start",
                "family": family,
                "mode": mode,
                "num_restarts": num_restarts,
                "best_opt_max": best_opt_max,
            }
        )

    def update_best(self, best_opt_max: float) -> None:
        if best_opt_max > self._best_opt_max:
            self._best_opt_max = best_opt_max
        self._try_put(
            {
                "kind": "best_update",
                "family": self._family,
                "best_opt_max": self._best_opt_max,
            }
        )

    def start_restart(self, restart_idx: int, phase: str) -> None:
        self._restart_idx = restart_idx + 1
        self._try_put(
            {
                "kind": "restart_start",
                "family": self._family,
                "restart": restart_idx + 1,
                "total_restarts": self._num_restarts,
                "phase": phase,
            }
        )

    def start_phase(self, phase: str, total: int) -> None:
        self._try_put(
            {
                "kind": "phase_start",
                "family": self._family,
                "phase": phase,
                "total": total,
            }
        )

    def advance(self, current_best_opt_max: float | None = None) -> None:
        if current_best_opt_max is not None and current_best_opt_max > self._best_opt_max:
            self._best_opt_max = current_best_opt_max
        self._try_put(
            {
                "kind": "phase_advance",
                "family": self._family,
                "current_best": self._best_opt_max,
            }
        )

    def end_phase(self, result: str) -> None:
        self._try_put(
            {
                "kind": "phase_end",
                "family": self._family,
                "result": result,
            }
        )

    def end_family(self) -> None:
        pass

    def close(self) -> None:
        pass

    def report_final(self, result: "FamilyResult") -> None:
        msg = {
            "kind": "family_end",
            "family": result.family,
            "opt_max": result.best_metrics.opt_max if result.best_metrics else 0.0,
            "baseline_opt_max": result.baseline_metrics.opt_max if result.baseline_metrics else 0.0,
            "delta_pp": (
                (result.best_metrics.opt_max - result.baseline_metrics.opt_max) * 100.0
                if result.best_metrics and result.baseline_metrics
                else 0.0
            ),
            "verdict": result.verdict,
            "skipped_reason": result.skipped_reason,
        }
        try:
            self._queue.put(msg, timeout=5.0)
        except Exception:
            pass


class MultiFamilyProgressDisplay:
    """Parent-side aggregator: one row per family, updated from worker messages."""

    STALL_TIMEOUT_S = 900.0

    def __init__(
        self,
        *,
        families: list[str],
        enabled: bool,
        use_rich: bool,
        loggers: list[logging.Logger] | None = None,
    ) -> None:
        self.enabled = enabled
        self.use_rich = use_rich and enabled
        self._loggers = loggers or []
        self._lock = threading.Lock()
        self._stop = threading.Event()

        ctx = mp.get_context("spawn")
        self._manager = ctx.Manager()
        # Manager-backed queue is proxy-based and picklable across a spawn Pool;
        # a plain ctx.Queue() cannot be passed as a Pool task argument.
        self.queue = self._manager.Queue()

        self._rows: dict[str, dict] = {}
        for f in families:
            self._rows[f] = self._new_row(f, "queued")

        self._spinner_cycle = _spinner_cycle()
        self._spinner_char = next(self._spinner_cycle)

        self._live = None
        self._console = None
        self._rich_handler = None
        self._saved_stream_handlers: list[tuple[logging.Logger, logging.Handler, int]] = []
        self._consumer_thread: threading.Thread | None = None

        if not self.enabled:
            return
        if self.use_rich:
            self._init_rich()
        else:
            self._init_plain()

    @staticmethod
    def _new_row(family: str, status: str) -> dict:
        return {
            "family": family,
            "status": status,
            "mode": "",
            "restart": 0,
            "total_restarts": 0,
            "phase": "",
            "evaluated": 0,
            "total": 0,
            "best": 0.0,
            "baseline": 0.0,
            "delta_pp": 0.0,
            "verdict": "",
            "skipped_reason": None,
            "last_update": time.monotonic(),
        }

    def _init_rich(self) -> None:
        try:
            from rich.console import Console
            from rich.live import Live
            from rich.logging import RichHandler
        except ImportError:
            self.use_rich = False
            self._init_plain()
            return

        self._console = Console(file=sys.stderr, force_terminal=True)
        self._live = Live(
            self._render(),
            console=self._console,
            refresh_per_second=10,
            transient=False,
        )
        self._live.start()

        self._rich_handler = RichHandler(
            console=self._console,
            show_time=True,
            show_level=True,
            show_path=False,
            markup=False,
            rich_tracebacks=False,
        )
        self._rich_handler.setFormatter(logging.Formatter("%(name)s %(message)s"))
        for lg in self._loggers:
            for h in list(lg.handlers):
                if isinstance(h, logging.StreamHandler) and not isinstance(h, logging.FileHandler):
                    self._saved_stream_handlers.append((lg, h, h.level))
                    lg.removeHandler(h)
            lg.addHandler(self._rich_handler)

    def _init_plain(self) -> None:
        for lg in self._loggers:
            for h in lg.handlers:
                if isinstance(h, logging.StreamHandler) and not isinstance(h, logging.FileHandler):
                    self._saved_stream_handlers.append((lg, h, h.level))
                    h.setLevel(logging.WARNING)

    def start(self) -> None:
        if not self.enabled:
            return
        self._consumer_thread = threading.Thread(target=self._consume_loop, daemon=True)
        self._consumer_thread.start()

    def _consume_loop(self) -> None:
        while not self._stop.is_set():
            try:
                msg = self.queue.get(timeout=0.2)
            except Exception:
                self._check_stalled()
                if self.use_rich and self._live is not None:
                    with self._lock:
                        self._spinner_char = next(self._spinner_cycle)
                        self._live.update(self._render())
                continue
            self._apply(msg)
            if self.use_rich and self._live is not None:
                with self._lock:
                    self._live.update(self._render())

    def _apply(self, msg: dict) -> None:
        kind = msg.get("kind")
        family = msg.get("family")
        if not family:
            return
        with self._lock:
            row = self._rows.get(family)
            if row is None:
                row = self._new_row(family, "running")
                self._rows[family] = row
            row["last_update"] = time.monotonic()
            if kind == "family_start":
                row["status"] = "running"
                row["mode"] = str(msg.get("mode", ""))
                row["total_restarts"] = int(msg.get("num_restarts", 0))
                row["best"] = float(msg.get("best_opt_max", 0.0))
                row["phase"] = "baseline"
            elif kind == "best_update":
                row["best"] = max(row["best"], float(msg.get("best_opt_max", 0.0)))
            elif kind == "restart_start":
                row["status"] = "running"
                row["restart"] = int(msg.get("restart", 0))
                row["total_restarts"] = int(msg.get("total_restarts", row["total_restarts"]))
                row["phase"] = str(msg.get("phase", ""))
                row["evaluated"] = 0
                row["total"] = 0
            elif kind == "phase_start":
                row["phase"] = str(msg.get("phase", ""))
                row["evaluated"] = 0
                row["total"] = int(msg.get("total", 0))
            elif kind == "phase_advance":
                row["evaluated"] += 1
                row["best"] = max(row["best"], float(msg.get("current_best", row["best"])))
            elif kind == "phase_end":
                row["phase"] = f"{row['phase']} {msg.get('result', '')}"
            elif kind == "family_end":
                row["status"] = "done"
                row["best"] = float(msg.get("opt_max", row["best"]))
                row["baseline"] = float(msg.get("baseline_opt_max", 0.0))
                row["delta_pp"] = float(msg.get("delta_pp", 0.0))
                row["verdict"] = str(msg.get("verdict", ""))
                row["skipped_reason"] = msg.get("skipped_reason")

    def _check_stalled(self) -> None:
        now = time.monotonic()
        with self._lock:
            for row in self._rows.values():
                if row["status"] == "running" and now - row["last_update"] > self.STALL_TIMEOUT_S:
                    row["status"] = "stalled"

    def _render(self):
        from rich.table import Table

        table = Table.grid(padding=(0, 1))
        table.add_column()

        def sort_key(item):
            status = item["status"]
            order = {"running": 0, "queued": 1, "stalled": 2, "done": 3}.get(status, 4)
            return (order, item["family"])

        rows = sorted(self._rows.values(), key=sort_key)
        prev_status = None
        for row in rows:
            if prev_status is not None and row["status"] != prev_status:
                table.add_row("")
            prev_status = row["status"]
            table.add_row(self._render_row(row))
        return table

    def _render_row(self, row: dict):
        from rich.text import Text

        t = Text()
        t.append(f"{row['family']:<8}", style="bold cyan")
        status = row["status"]
        if status == "queued":
            t.append("QUEUED", style="dim")
            return t
        if status == "stalled":
            t.append("STALLED (no updates)", style="bold red")
            return t
        if status == "done":
            if row["skipped_reason"]:
                t.append(f"DONE     skipped ({row['skipped_reason']})", style="dim")
                return t
            t.append("DONE     ", style="bold green")
            t.append(f"opt_max {row['best']:.4f} ")
            if row["baseline"] > 0:
                t.append(
                    f"(baseline {row['baseline']:.4f}, {row['delta_pp']:+.1f}pp)",
                    style="green" if row["delta_pp"] > 0 else "yellow",
                )
            t.append(f"  [{row['verdict']}]", style="dim")
            return t
        # running
        if row["mode"]:
            t.append(f"{row['mode']:<10} ", style="magenta")
        if row["total_restarts"]:
            t.append(f"restart {max(row['restart'] - 1, 0)}/{row['total_restarts']}  ")
        if row["phase"]:
            t.append(f"{row['phase']:<12} ", style="yellow")
        if row["total"]:
            bar = _bar(row["evaluated"], row["total"], width=12)
            t.append(f"[{bar}] ", style="green")
            t.append(f"{row['evaluated']:>4}/{row['total']:<4}  ")
        else:
            t.append(f"[{' ' * 12}]  0/0     ", style="dim")
        t.append(f"best {row['best']:.4f}  ", style="bold")
        t.append(self._spinner_char, style="cyan")
        return t

    def close(self) -> None:
        if not self.enabled:
            return
        self._stop.set()
        if self._consumer_thread is not None:
            self._consumer_thread.join(timeout=2.0)
        try:
            while True:
                msg = self.queue.get_nowait()
                self._apply(msg)
        except Exception:
            pass
        try:
            if self.use_rich and self._live is not None:
                with self._lock:
                    self._live.update(self._render())
                self._live.stop()
        finally:
            for lg, handler, level in self._saved_stream_handlers:
                if self._rich_handler is not None and self._rich_handler in lg.handlers:
                    lg.removeHandler(self._rich_handler)
                handler.setLevel(level)
                if handler not in lg.handlers:
                    lg.addHandler(handler)
            self._saved_stream_handlers.clear()
            try:
                self._manager.shutdown()
            except Exception:
                pass
