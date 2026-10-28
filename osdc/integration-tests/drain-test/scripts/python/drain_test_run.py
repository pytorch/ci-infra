"""Exercise real drain/resume recipes with canary jobs on the configured cluster.

Cleanup follows the existing slow-test pattern: finally, atexit and SIGINT/
SIGTERM handlers, plus an always() same-job recovery step in CI. Recovery is
still subject to GitHub cancellation deadlines. Live execution is restricted to
the serialized pre-merge Actions job; local invocation supports only --dry-run.
CI defers exception recovery to that step and leaves canary leftovers for the next run.
A runner-local intent file and process token allow that step to stop orphaned
drain writers before restoring. SIGKILL of the whole job or runner loss
cannot execute this process's cleanup; no independent recovery agent is used.
"""

from __future__ import annotations

import argparse
import atexit
import contextlib
import json
import logging
import os
import re
import signal
import subprocess
import sys
import time
from concurrent.futures import CancelledError, ThreadPoolExecutor
from pathlib import Path
from threading import Event
from uuid import uuid4

from drain_test_checks import (
    advertised_capacity,
    held_probe_number,
    listener_pods,
    probe_jobs,
    probe_scale_set,
    require_limits,
    require_probe_placement,
    require_queued,
    require_success,
    runner_limits,
)

# Use the shared integration test helpers, matching the other orchestrators.
_INTEG_SCRIPTS = Path(__file__).resolve().parents[3] / "scripts" / "python"
sys.path.insert(0, str(_INTEG_SCRIPTS))

# Import the same prefix-resolution helper used by runner_max_map.py /
# generate_runners.py so the probe label tracks the actual ARS names.
_ARC_RUNNERS_SCRIPTS = Path(__file__).resolve().parents[4] / "modules" / "arc-runners" / "scripts" / "python"
sys.path.insert(0, str(_ARC_RUNNERS_SCRIPTS))

from run import CANARY_REPO, load_cluster_config, resolve, safe_json_loads  # noqa: E402
from runner_max_map import resolve_runner_name_prefix  # noqa: E402

log = logging.getLogger("osdc-drain-test")
PROC_ROOT = Path("/proc")
DRAIN_OWNER_ENV = "OSDC_DRAIN_OWNER_TOKEN"
PR_TITLE_PREFIX = "[NO REVIEW][NO MERGE] OSDC Drain Test"
WORKFLOW_NAME_PREFIX = "OSDC Drain Test "
UPSTREAM = Path(__file__).resolve().parents[4]
RUN_TIMEOUT = 75 * 60
DRAIN_PHASE_TIMEOUT = 15 * 60
CLEANUP_TIMEOUT = 10 * 60
RESTORE_TIMEOUT = 10 * 60
# Total budget for confirming held jobs are queued and the cluster is drained.
OBSERVE_TIMEOUT = 120
# Listener metrics scrape uses a bounded wait even when no controller deadline
# is active, so cleanup/restore paths cannot hang on the thread pool.
CAPACITY_TIMEOUT = 2 * 60
WAKEUP_INTERVAL = 15
WAKEUP_ATTEMPTS = 8
# Match the unchanged operational recipes in osdc/justfile.
REFRESH_TAINT_KEY = "deploy.osdc.io/refresh-pending"
REFRESH_TAINT_VALUE = "true"
REFRESH_TAINT_EFFECT = "NoSchedule"
RUNNER_NODE_SELECTOR = "workload-type=github-runner"
HELD_RELEASE_STEP = "Wait for controller release"
# Drain-test probes deliberately use a small, non-GPU x86 runner so the probe
# jobs start quickly and cheaply. The name must match an ARC runner definition
# under modules/arc-runners/defs/ (currently l-x86iamx-8-16.yaml: c7i.12xlarge,
# 8 vCPU / 16 Gi). Centralizing the string makes the invariant explicit and
# avoids silent drift if the probe runner is ever changed.
PROBE_RUNNER_NAME = "l-x86iamx-8-16"


class RefreshTaintError(RuntimeError):
    """A surviving node lost a recorded taint; resuming limits cannot repair it."""


def require_ci_execution() -> None:
    """Guard supported CLI usage; live execution is opt-in via the CI workflow."""
    if os.environ.get("OSDC_DRAIN_TEST_LIVE") != "true":
        raise SystemExit(
            "Live drain-test (including --restore-only) is allowed only when "
            "OSDC_DRAIN_TEST_LIVE=true is set by the CI workflow. "
            "local execution supports --dry-run only."
        )


def is_canary_name(value: str | None, prefix: str) -> bool:
    """Recognize current and previous test identities without accepting a bare prefix."""
    if not isinstance(value, str) or not value.startswith(prefix):
        return False
    suffix = value[len(prefix) :]
    return suffix.isascii() and suffix.isalnum()


def is_canary_run(run: dict) -> bool:
    return is_canary_name(run.get("name"), WORKFLOW_NAME_PREFIX)


def validate_canary_resources(runs: list[dict], pulls: list[dict]) -> None:
    """Reject unrecognized cleanup targets before any cleanup writes.

    Callers select canary runs first; this guard catches accidental foreign
    targets rather than deciding which runs on the branch should be ignored.
    """
    foreign_runs = [run for run in runs if not is_canary_run(run)]
    if foreign_runs:
        details = ", ".join(f"{run.get('name')!r} (run {run.get('id')})" for run in foreign_runs[:3])
        raise RuntimeError(f"Refusing canary cleanup: unrecognized workflow run(s) on the test branch: {details}")
    foreign_prs = [pr for pr in pulls if not is_canary_name(pr.get("title"), f"{PR_TITLE_PREFIX} ")]
    if foreign_prs:
        details = ", ".join(f"{pr.get('title')!r} (PR #{pr.get('number')})" for pr in foreign_prs[:3])
        raise RuntimeError(f"Refusing canary cleanup: unrecognized PR(s) on the test branch: {details}")


def remaining(deadline: float | None, timeout: float) -> float:
    """Clamp a blocking operation to an absolute deadline."""
    if deadline is None:
        return timeout
    available = deadline - time.monotonic()
    if available <= 0:
        raise TimeoutError("Controller deadline exceeded")
    return min(timeout, available)


def command(
    args: list[str],
    *,
    deadline: float | None = None,
    timeout: float = 30,
    check: bool = True,
    cancel_event: Event | None = None,
    **kwargs,
) -> subprocess.CompletedProcess:
    """Run a subprocess bounded by an absolute deadline."""
    if cancel_event is not None and cancel_event.is_set():
        raise CancelledError("Command cancelled before launch")
    timeout = remaining(deadline, timeout)
    payload = kwargs.pop("input", None)
    proc = subprocess.Popen(
        args,
        stdin=subprocess.PIPE if payload is not None else None,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        start_new_session=True,
        **kwargs,
    )
    try:
        if cancel_event is None:
            stdout, stderr = proc.communicate(input=payload, timeout=remaining(deadline, timeout))
        else:
            call_deadline = time.monotonic() + remaining(deadline, timeout)
            stdout = stderr = None
            while True:
                if cancel_event.is_set():
                    raise CancelledError("Command cancelled")
                poll_timeout = min(0.1, max(0, call_deadline - time.monotonic()))
                try:
                    stdout, stderr = proc.communicate(input=payload, timeout=remaining(deadline, poll_timeout))
                    break
                except (subprocess.TimeoutExpired, TimeoutError) as exc:
                    # communicate() retains buffered input/output across retries.
                    payload = None
                    cause = exc
                    if isinstance(exc, TimeoutError):
                        # The absolute controller deadline was crossed between polls,
                        # before communicate() could start. Promote to the same
                        # exception type used for a subprocess-level timeout so callers
                        # only need one handler and any partial output is preserved.
                        exc = subprocess.TimeoutExpired(args, timeout, output=stdout, stderr=stderr)
                    stdout, stderr = exc.output, exc.stderr
                    if time.monotonic() >= call_deadline:
                        raise subprocess.TimeoutExpired(args, timeout, output=stdout, stderr=stderr) from cause
    except BaseException as exc:
        with contextlib.suppress(ProcessLookupError, PermissionError):
            os.killpg(proc.pid, signal.SIGKILL)
        # Allow a bounded reap even after the controller budget is exhausted.
        grace = max(1, min(2, deadline - time.monotonic())) if deadline is not None else 2
        try:
            stdout, stderr = proc.communicate(timeout=grace)
        except subprocess.TimeoutExpired as teardown_error:
            stdout, stderr = teardown_error.output, teardown_error.stderr
            proc.stdout.close()
            proc.stderr.close()
            proc.poll()
        if isinstance(exc, (TimeoutError, subprocess.TimeoutExpired)):
            # Keep the original exception type and deadline semantics, but retain
            # output collected while reaping the killed process for diagnosis.
            if isinstance(exc, subprocess.TimeoutExpired):
                exc.output = stdout or exc.output
                exc.stderr = stderr or exc.stderr
                stdout, stderr = exc.output, exc.stderr
            output = [
                value.decode(errors="replace") if isinstance(value, bytes) else value for value in (stdout, stderr)
            ]
            exc.add_note(f"Command: {args!r}\nstdout: {output[0] or ''}\nstderr: {output[1] or ''}")
        raise
    result = subprocess.CompletedProcess(args, proc.returncode, stdout, stderr)
    if check and proc.returncode != 0:
        log.warning("%s exited %d\nstdout: %s\nstderr: %s", args[0], proc.returncode, stdout.strip(), stderr.strip())
    if check:
        result.check_returncode()
    remaining(deadline, float("inf"))
    return result


def configured_probe_label(cfg: dict, cluster_id: str) -> str:
    """Return the probe label using the same prefix as the runner scale set generator.

    Delegates to runner_max_map.py's resolve_runner_name_prefix so the probe
    label tracks the same whole-block defaults fallback used to generate the
    actual AutoscalingRunnerSet names. Re-deriving the logic here would let the
    two paths drift silently if the fallback rules change.

    The runner name itself is fixed by PROBE_RUNNER_NAME: drain-test probes
    intentionally use a small, non-GPU x86 runner for fast/cheap scheduling.
    """
    clusters_yaml = {"clusters": {cluster_id: cfg["cluster"]}, "defaults": cfg.get("defaults", {})}
    prefix = resolve_runner_name_prefix(clusters_yaml, cluster_id)
    return f"{prefix}{PROBE_RUNNER_NAME}"


def configured_runner_group(cfg: dict) -> str:
    """Require organization registration and an explicit group from the same block."""
    arc_config = resolve(cfg, "arc-runners", {})
    if not isinstance(arc_config, dict):
        raise ValueError("Expected arc-runners configuration to be a mapping")
    group = arc_config.get("runner_group")
    if not isinstance(group, str) or not group.strip():
        raise ValueError(f"Expected runner group to be a nonempty string, got {group!r}")
    github_url = arc_config.get("github_config_url")
    if not isinstance(github_url, str) or not re.fullmatch(r"https://github\.com/[^/?#\s]+/?", github_url):
        raise ValueError(
            "Drain-test requires a valid arc-runners.github_config_url "
            "with organization-scoped registration (https://github.com/<org>) "
            "and an explicit runner group; repository-scoped URLs are unsupported "
            "because probes require runner-group targeting"
        )
    return group


def generate_workflow(run_key: str, probe_label: str, runner_group: str) -> str:
    """Generate uniquely identifiable probes pinned to the configured runner group."""
    if not run_key or not run_key.isascii() or not run_key.isalnum():
        raise ValueError("Run key must be nonempty and ASCII alphanumeric")
    template = Path(__file__).resolve().parents[2] / "workflows" / "drain-test.yaml.tpl"
    workflow = (
        template.read_text()
        .replace("{{RUN_KEY}}", run_key)
        .replace("{{PROBE_LABEL}}", json.dumps(probe_label))
        .replace("{{RUNNER_GROUP}}", json.dumps(runner_group))
    )
    if re.search(r"\{\{[A-Z_]+\}\}", workflow):
        raise ValueError("Unresolved drain-test workflow placeholder")
    return workflow


def api(
    endpoint: str,
    payload: dict | None = None,
    method: str = "GET",
    *,
    repository: str = CANARY_REPO,
    timeout: float = 30,
    deadline: float | None = None,
) -> dict | list:
    args = ["gh", "api", f"repos/{repository}/{endpoint}".rstrip("/"), "--method", method]
    if payload is not None:
        args.extend(["--input", "-"])
    output = command(
        args, input=json.dumps(payload) if payload is not None else None, timeout=timeout, deadline=deadline
    ).stdout
    # These endpoints legitimately return no response body. Reads and writes
    # that return resources must never turn a malformed response into success.
    if not output.strip() and (method == "DELETE" or (method == "POST" and endpoint.endswith("/cancel"))):
        return {}
    result = safe_json_loads(output, context=endpoint)
    if not isinstance(result, (dict, list)):
        raise ValueError(f"Expected a JSON object or array from gh api {endpoint}")
    return result


def kubectl(*args: str, deadline: float | None = None, cancel_event: Event | None = None) -> str:
    kwargs = {"cancel_event": cancel_event} if cancel_event is not None else {}
    return command(["kubectl", "--request-timeout=15s", *args], deadline=deadline, **kwargs).stdout


def process_group_running(pgid: int, deadline: float | None = None) -> bool:
    """Check all group members, ignoring zombies that cannot execute patches."""
    processes = command(["ps", "-eo", "pgid=,stat="], deadline=deadline, timeout=2).stdout
    malformed = False
    for line in processes.splitlines():
        try:
            group, state = line.split()
            group = int(group)
        except ValueError:
            malformed = True
            continue
        if group == pgid and not state.startswith("Z"):
            return True
    if malformed:
        # An unparsed row may hide a live writer. Only a missing process group
        # proves it is safe to resume without a complete ps snapshot.
        try:
            os.killpg(pgid, 0)
        except ProcessLookupError:
            return False
        except PermissionError as exc:
            raise ValueError(f"Malformed ps output; cannot inspect drain process group {pgid}") from exc
        raise ValueError(f"Malformed ps output; drain process group {pgid} still exists")
    return False


class DrainTest:
    def __init__(self, args: argparse.Namespace, run_key: str):
        self.args = args
        self.cluster = args.cluster_id
        self.group: str | None = None
        self.repository = CANARY_REPO
        self.run_key = run_key
        self.branch = f"osdc-drain-test-{self.cluster}"
        self.workflow_name = f"{WORKFLOW_NAME_PREFIX}{run_key}"
        self.pr: int | None = None
        self.run_id: int | None = None
        # Cleanup intent: the canary may exist even if creation was not confirmed.
        self.canary_cleanup_needed = False
        self.draining = False
        self.resume_limits_restored = False
        self.refresh_taint_failure: str | None = None
        self.refresh_nodes: dict[str, str] = {}
        self.placements: dict[int, dict] = {}
        self.released_probes: set[int] = set()
        self.pending_held_release: str | None = None
        self.drain: subprocess.Popen | None = None
        self.desired: dict[str, int] = {}
        self.probe_ids: dict[str, set[int]] = {}
        self.assigned_phases: set[str] = set()
        self.wakeup_intents: dict[tuple[str, int], dict[str, str]] = {}
        self.cleanup_deadline: float | None = None
        self.deadline: float | None = None
        self.recovery_attempts = 0
        self.recovery_file = getattr(args, "recovery_file", None)
        self.defer_recovery = getattr(args, "defer_recovery", False)
        self.skip_canary_cleanup = False
        # Keep only test-specific overrides; inherit the environment at launch.
        self.env_overrides = {
            "OSDC_ROOT": str(args.root_dir),
            "OSDC_UPSTREAM": str(args.upstream_dir),
            "CLUSTERS_YAML": str(args.root_dir / "clusters.yaml"),
            # Taints can make capacity zero even when maxRunners=0 is broken.
            "OSDC_TAINT_NODES": "no",
            "OSDC_UNTAINT_NODES": "no",
        }

    @contextlib.contextmanager
    def deadline_scope(self, deadline: float):
        """Nested waits may shorten a budget, but must never extend it."""
        parent = self.deadline
        self.deadline = min(parent, deadline) if parent is not None else deadline
        try:
            self.remaining(float("inf"))
            yield
        finally:
            self.deadline = parent

    def remaining(self, timeout: float) -> float:
        """Clamp a blocking operation to the current instance deadline."""
        return remaining(self.deadline, timeout)

    def wait_for(self, description: str, check, timeout: float, poll: float = 10) -> None:
        """Retry convergence/API failures, but let definitive test failures escape."""
        deadline = time.monotonic() + timeout
        last_error = "condition not reached"
        try:
            with self.deadline_scope(deadline):
                while True:
                    self.remaining(float("inf"))
                    try:
                        passed = check()
                        self.remaining(float("inf"))
                        if passed:
                            log.info("PASS: %s", description)
                            return
                    except (ValueError, subprocess.SubprocessError) as exc:
                        last_error = str(exc)
                    time.sleep(self.remaining(poll))
        except TimeoutError as exc:
            raise TimeoutError(f"{description}: deadline exceeded ({last_error})") from exc

    def record_recovery(self) -> dict[str, str]:
        """Persist intent before launching any drain writer; never tag the controller."""
        if self.recovery_file is None:
            return {}
        if not PROC_ROOT.is_dir():
            raise RuntimeError("Runner-local recovery requires Linux /proc")
        token = uuid4().hex
        # Exclusive creation prevents overwriting an earlier writer's identity.
        with self.recovery_file.open("x") as state:
            json.dump({"cluster": self.cluster, "token": token}, state)
            state.flush()
            os.fsync(state.fileno())
        return {DRAIN_OWNER_ENV: token}

    def recorded_drain_groups(self, token: str) -> set[int]:
        """Match inherited ownership tokens, including descendants of an exited leader."""
        groups = set()
        for process in PROC_ROOT.glob("[0-9]*"):
            self.remaining(float("inf"))
            try:
                if process.stat().st_uid != os.getuid():
                    continue
                try:
                    environment = (process / "environ").read_bytes().split(b"\0")
                except PermissionError:
                    # Same-UID processes may deny environ access (e.g. non-dumpable).
                    # Keep failures after a matching ownership token fatal.
                    continue
                if f"{DRAIN_OWNER_ENV}={token}".encode() not in environment:
                    continue
                _, separator, tail = (process / "stat").read_text().rpartition(")")
                fields = tail.split()
                if not separator or len(fields) < 3:
                    raise ValueError(
                        f"Invalid stat for drain writer PID {process.name}: "
                        "expected state, parent PID and process group after ')'"
                    )
                if fields[0] == "Z":
                    continue
                try:
                    group = int(fields[2])
                except ValueError as exc:
                    raise ValueError(
                        f"Invalid process group for drain writer PID {process.name}: {fields[2]!r}"
                    ) from exc
                if group <= 1 or group == os.getpgrp():
                    raise RuntimeError("Refusing to stop a drain writer in an unsafe process group")
                groups.add(group)
            except (FileNotFoundError, ProcessLookupError):
                # Processes may exit between enumeration and inspection.
                continue
        return groups

    def stop_recorded_drain(self) -> None:
        """Fence old step writers before a new restore-only process changes limits."""
        if self.recovery_file is None:
            return
        if not PROC_ROOT.is_dir():
            raise RuntimeError("Runner-local recovery requires Linux /proc")
        try:
            state = json.loads(self.recovery_file.read_text())
            if not isinstance(state, dict):
                raise ValueError("Drain recovery state must be a JSON object")
        except (OSError, ValueError):
            log.exception(
                "Drain recovery state is unreadable or corrupt; "
                "cannot fence previous writers, but will still attempt resume"
            )
            return
        token = state.get("token", "")
        if (
            state.get("cluster") != self.cluster
            or not isinstance(token, str)
            or len(token) != 32
            or any(c not in "0123456789abcdef" for c in token)
        ):
            raise ValueError("Invalid runner-local drain recovery state")
        for sig in (signal.SIGTERM, signal.SIGKILL):
            for group in self.recorded_drain_groups(token):
                with contextlib.suppress(ProcessLookupError):
                    os.killpg(group, sig)
            try:
                self.wait_for(
                    "recorded drain writers stopped", lambda: not self.recorded_drain_groups(token), 2, poll=0.5
                )
                return
            except TimeoutError:
                if sig == signal.SIGKILL:
                    raise

    def api(self, endpoint, payload=None, method="GET", *, deadline: float | None = None, **kwargs):
        return api(endpoint, payload, method, deadline=deadline if deadline is not None else self.deadline, **kwargs)

    def kube(self, *args, deadline: float | None = None, cancel_event: Event | None = None):
        kwargs = {"cancel_event": cancel_event} if cancel_event is not None else {}
        return kubectl(*args, deadline=deadline if deadline is not None else self.deadline, **kwargs)

    def workflow(self):
        return generate_workflow(self.run_key, self.probe_label, self.group)

    def check_probe(self, items):
        return probe_scale_set(items, self.desired, label=self.probe_label, cluster=self.cluster)

    def recipe(self, name: str, timeout: float = 120) -> None:
        result = command(
            ["just", name, self.cluster],
            cwd=self.args.root_dir,
            env={**os.environ, **self.env_overrides},
            timeout=timeout,
            deadline=self.deadline,
        )
        log.info("%s: %s", name, result.stdout)

    def scale_sets(self) -> list[dict]:
        data = safe_json_loads(
            self.kube("get", "autoscalingrunnersets", "-n", "arc-runners", "-o", "json"),
            context="scale_sets",
        )
        return data.get("items", []) if isinstance(data, dict) else []

    def prepare(self) -> None:
        cfg = load_cluster_config(self.args.root_dir / "clusters.yaml", self.cluster)
        self.group = configured_runner_group(cfg)
        self.probe_label = configured_probe_label(cfg, self.cluster)
        self.wait_for("kubeconfig available", lambda: self.recipe("kubeconfig", timeout=90) or True, 90, poll=5)
        script = self.args.upstream_dir / "modules/arc-runners/scripts/python/runner_max_map.py"
        parsed = safe_json_loads(
            command(
                ["uv", "run", str(script), self.cluster],
                cwd=self.args.root_dir,
                env={**os.environ, **self.env_overrides},
                deadline=self.deadline,
                timeout=90,
            ).stdout,
            context="runner_max_map",
        )
        if (
            not isinstance(parsed, dict)
            or not parsed
            or any(
                not isinstance(name, str) or not name or type(limit) is not int or limit < 0
                for name, limit in parsed.items()
            )
        ):
            raise ValueError("Expected a nonempty runner_max_map object with nonnegative integer limits")
        self.desired = parsed

    def capacities(self, expected: set[str] | None = None) -> dict[str, float]:
        """Scrape all configured listeners, or only the requested scale sets."""
        pods_data = safe_json_loads(
            self.kube(
                "get",
                "pods",
                "-n",
                "arc-systems",
                "-l",
                "app.kubernetes.io/component=runner-scale-set-listener",
                "-o",
                "json",
            ),
            context="capacities/pods",
        )
        pods = pods_data.get("items", []) if isinstance(pods_data, dict) else []
        listeners = listener_pods(pods, set(self.desired) if expected is None else expected)

        deadline = self.deadline
        cancel_event = Event()

        def scrape(item):
            name, (pod, port) = item
            path = f"/api/v1/namespaces/arc-systems/pods/{pod}:{port}/proxy/metrics"
            # listener_pods() already verified via Pod label that this listener
            # belongs to scale set `name`. Trust the pod as the source of truth
            # for its own metrics, rather than coupling to the AutoscalingListener
            # CR being visible in the same kubectl list.
            # Executor threads share the same DrainTest instance. Pass the captured
            # deadline explicitly so workers never mutate self.deadline concurrently.
            return name, advertised_capacity(
                self.kube("get", "--raw", path, deadline=deadline, cancel_event=cancel_event),
                name,
            )

        pool = ThreadPoolExecutor(max_workers=8)
        try:
            return dict(
                pool.map(
                    scrape,
                    listeners.items(),
                    timeout=remaining(deadline, float("inf")) if deadline is not None else CAPACITY_TIMEOUT,
                )
            )
        finally:
            # Running workers stop their own subprocess groups; pending tasks
            # cannot launch commands after the batch has been abandoned.
            cancel_event.set()
            pool.shutdown(wait=False, cancel_futures=True)

    def create_pr(self) -> None:
        """Replace workflows only on the cluster's temporary canary branch, never main."""
        base = self.api("git/ref/heads/main")["object"]["sha"]
        base_tree = self.api(f"git/commits/{base}")["tree"]["sha"]
        workflows = self.api(
            "git/trees",
            {
                "tree": [
                    {
                        "path": "drain-test.yaml",
                        "mode": "100644",
                        "type": "blob",
                        "content": self.workflow(),
                    }
                ]
            },
            "POST",
        )["sha"]
        tree = self.api(
            "git/trees",
            {
                "base_tree": base_tree,
                "tree": [{"path": ".github/workflows", "mode": "040000", "type": "tree", "sha": workflows}],
            },
            "POST",
        )["sha"]
        commit = self.api("git/commits", {"message": self.workflow_name, "tree": tree, "parents": [base]}, "POST")[
            "sha"
        ]
        # Register cleanup intent before the write, not proof of branch creation.
        # Retain it on any failure because the remote outcome may be unknown.
        self.canary_cleanup_needed = True
        self.api("git/refs", {"ref": f"refs/heads/{self.branch}", "sha": commit}, "POST")
        self.pr = self.api(
            "pulls",
            {
                "title": f"{PR_TITLE_PREFIX} {self.run_key}",
                "head": self.branch,
                "base": "main",
                "body": "Temporary OSDC drain/undrain probes. Do not merge.",
            },
            "POST",
        )["number"]
        log.info("Canary PR: https://github.com/%s/pull/%s", self.repository, self.pr)

    def runs(self) -> list[dict]:
        return [
            run
            for run in self.api(f"actions/runs?event=pull_request&branch={self.branch}&per_page=100")["workflow_runs"]
            if run["name"] == self.workflow_name
        ]

    def find_run(self) -> bool:
        runs = self.runs()
        if not runs:
            return False
        if len(runs) != 1:
            raise RuntimeError(f"Expected one canary run, got {len(runs)}")
        self.run_id = runs[0]["id"]
        log.info("Original canary run: https://github.com/%s/actions/runs/%s", self.repository, self.run_id)
        return True

    def jobs(self) -> list[dict]:
        return self.api(f"actions/runs/{self.run_id}/jobs?per_page=100")["jobs"]

    def probes(self, phase: str, count: int) -> list[dict]:
        """Pin the original jobs; wakeup runs must never replace the acceptance probes."""
        jobs = probe_jobs(self.jobs(), phase, count)
        ids = {job.get("id") for job in jobs}
        if len(ids) != count or any(type(job_id) is not int or job_id <= 0 for job_id in ids):
            raise RuntimeError(f"Invalid {phase} probe job IDs: {ids}")
        expected = self.probe_ids.setdefault(phase, ids)
        if ids != expected:
            raise RuntimeError(f"Original {phase} probe jobs changed: {expected} -> {ids}")
        if any(job.get("runner_id") for job in jobs):
            self.assigned_phases.add(phase)
        return jobs

    def wakeup(self, phase: str, attempt: int, deadline: float) -> None:
        """New same-scale-set traffic works around ARC #4715 without rerunning old jobs."""
        if self.draining or not self.canary_cleanup_needed or self.pr is None or self.run_id is None:
            raise RuntimeError("Waking probes requires an active canary outside drain")

        def request(endpoint, payload=None, method="GET"):
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError("ARC #4715 wakeup deadline exceeded")
            return self.api(endpoint, payload, method, timeout=min(30, remaining))

        head = request(f"git/ref/heads/{self.branch}")["object"]["sha"]
        intent = self.wakeup_intents.setdefault((phase, attempt), {"parent": head})
        if intent.get("commit") == head:
            # A previous PATCH may have succeeded even though its response timed out.
            log.info("ARC #4715 wakeup %s/%s already applied: %s", phase, attempt, head)
            return
        if head != intent["parent"]:
            raise RuntimeError("Canary branch changed during an unresolved wakeup")
        if "tree" not in intent:
            tree = request(f"git/commits/{head}")["tree"]["sha"]
            intent["tree"] = request(
                "git/trees",
                {
                    "base_tree": tree,
                    "tree": [
                        {
                            "path": ".osdc-drain-wakeup",
                            "mode": "100644",
                            "type": "blob",
                            "content": f"{self.run_key}:{phase}:{attempt}\n",
                        }
                    ],
                },
                "POST",
            )["sha"]
        if "commit" not in intent:
            intent["commit"] = request(
                "git/commits",
                {"message": f"ARC #4715 wakeup {phase} {attempt}", "tree": intent["tree"], "parents": [head]},
                "POST",
            )["sha"]
        # Persist the exact target before PATCH. Retries read HEAD and reuse it,
        # rather than making a new commit for an ambiguously successful write.
        request(f"git/refs/heads/{self.branch}", {"sha": intent["commit"], "force": False}, "PATCH")

    def wait_with_wakeup(self, description: str, phase: str, check, timeout: float) -> None:
        """Send bounded, spaced traffic until original probes receive an assignment."""
        deadline = time.monotonic() + self.remaining(timeout)
        attempt = 0
        pending_attempt = None
        next_wakeup = time.monotonic()

        def poll():
            nonlocal attempt, pending_attempt, next_wakeup
            if self.draining:
                raise RuntimeError("ARC #4715 wakeup is forbidden during drain")
            if check():
                return True
            if phase in self.assigned_phases:
                return False
            if time.monotonic() >= deadline:
                return False
            if pending_attempt is not None or (attempt < WAKEUP_ATTEMPTS and time.monotonic() >= next_wakeup):
                items = self.scale_sets()
                require_limits(runner_limits(items), self.desired)
                target = self.check_probe(items)
                if self.capacities({target}).get(target, 0) <= 0:
                    raise ValueError(f"Waiting for positive advertised capacity on {target}")
                if pending_attempt is None:
                    attempt += 1
                    pending_attempt = attempt
                log.info("ARC #4715 workaround: %s wakeup %s/%s", phase, pending_attempt, WAKEUP_ATTEMPTS)
                self.wakeup(phase, pending_attempt, deadline)
                pending_attempt = None
                next_wakeup = time.monotonic() + WAKEUP_INTERVAL
            return False

        self.wait_for(description, poll, timeout, poll=5)
        log.info("ARC #4715 workaround: %s completed", phase)

    def inflight_ready(self) -> bool:
        jobs = self.probes("inflight", 1)
        job = jobs[0]
        if job["status"] == "completed":
            raise RuntimeError("In-flight probe finished before drain")
        group = job.get("runner_group_name")
        if job.get("runner_id") and group and group != self.group:
            raise RuntimeError("In-flight probe was assigned to a different runner group")
        return bool(job.get("runner_id")) and any(
            step["name"] == "Wait for drain" and step["status"] == "in_progress" for step in (job.get("steps") or [])
        )

    def drained(self, *, strict: bool = False) -> bool:
        """Allow initial convergence, but reject observed regressions immediately."""
        items = self.scale_sets()
        limits = runner_limits(items)
        try:
            require_limits(limits, dict.fromkeys(self.desired, 0))
        except ValueError as exc:
            if strict:
                raise RuntimeError(str(exc)) from exc
            raise
        values = self.capacities()
        if any(values.values()):
            error = RuntimeError if strict else ValueError
            raise error(f"Drained listeners still advertise capacity: {values}")
        return True

    def completed(self, phase: str, count: int) -> bool:
        jobs = self.probes(phase, count)
        if any(job["status"] == "completed" and job.get("conclusion") != "success" for job in jobs):
            raise RuntimeError(f"{phase} probe failed: {jobs}")
        if not all(job["status"] == "completed" for job in jobs):
            return False
        try:
            require_success(jobs, self.group)
        except ValueError as exc:
            raise RuntimeError(str(exc)) from exc
        return True

    def flush_held_release(self) -> None:
        """Retry the exact cumulative release body until its write is confirmed."""
        if self.pending_held_release is not None:
            self.api(f"pulls/{self.pr}", {"body": self.pending_held_release}, "PATCH")
            self.pending_held_release = None

    def held_completed(self) -> bool:
        """Capture each original probe's placement before allowing its pods to disappear."""
        # A failed PATCH may have applied remotely. Preserve placement evidence,
        # but retry the pending write even when no further probe needs release.
        self.flush_held_release()
        jobs = self.jobs()
        held_jobs = [job for job in jobs if job["name"].startswith("drain-held")]
        original_ids = self.probe_ids.get("held")
        if not original_ids or any(job["id"] not in original_ids for job in held_jobs):
            raise RuntimeError("Original held probe jobs changed or their job IDs were not recorded")
        if any(job.get("runner_id") for job in held_jobs):
            self.assigned_phases.add("held")
        visible_numbers = {held_probe_number(job) for job in held_jobs}
        expected = {1, 2, 3}
        unreleased = expected - self.released_probes
        if not visible_numbers.issuperset(unreleased):
            missing = unreleased - visible_numbers
            raise RuntimeError(f"Expected one original held job for each unreleased probe number; missing {missing}")
        if not visible_numbers.issubset(expected):
            raise RuntimeError(f"Unexpected held probe numbers: {visible_numbers - expected}")
        snapshot = None
        for job in held_jobs:
            number = held_probe_number(job)
            if job["status"] == "completed" and job.get("conclusion") != "success":
                raise RuntimeError(f"held probe failed: {job}")
            if not job.get("runner_id"):
                if job["status"] == "completed":
                    raise RuntimeError(f"Held job completed without a runner: {job}")
                continue
            group = job.get("runner_group_name")
            if group and group != self.group:
                raise RuntimeError(f"Held job assigned to a different runner group: {job}")
            if number not in self.placements:
                if job["status"] == "completed":
                    raise RuntimeError(f"Held job {job['id']} completed without placement verification")
                if not any(
                    step["name"] == HELD_RELEASE_STEP and step["status"] == "in_progress"
                    for step in (job.get("steps") or [])
                ):
                    continue
                if snapshot is None:
                    self.verify_refresh_taints()
                    pods_data = safe_json_loads(
                        self.kube("get", "pods", "-n", "arc-runners", "-o", "json"), context="held_completed/pods"
                    )
                    nodes_data = safe_json_loads(
                        self.kube("get", "nodes", "-o", "json"), context="held_completed/nodes"
                    )
                    if not isinstance(pods_data, dict) or not isinstance(nodes_data, dict):
                        log.warning("Skipping placement verification: failed to scrape cluster state")
                        return False
                    snapshot = pods_data.get("items", []), nodes_data.get("items", [])
                    scale_set = self.check_probe(self.scale_sets())
                evidence = require_probe_placement(job, *snapshot, set(self.refresh_nodes), scale_set=scale_set)
                self.placements[number] = {
                    "job_id": job["id"],
                    "runner_id": job["runner_id"],
                    "runner_name": job["runner_name"],
                    **evidence,
                }
                log.info("Held probe %d placement verified: %s", number, self.placements[number])
            recorded = self.placements[number]
            if recorded["job_id"] != job["id"] or any(
                recorded[key] != job.get(key) for key in ("runner_id", "runner_name")
            ):
                raise RuntimeError(f"Held job {job['id']} changed runner or job identity after placement verification")
            if number not in self.released_probes:
                # Persist evidence in memory before PATCH: an uncertain response may still release the job.
                self.released_probes.add(number)
                markers = [f"OSDC_DRAIN_RELEASE_{self.run_key}"] + [
                    f"OSDC_HELD_RELEASE_{self.run_key}_{probe}" for probe in sorted(self.released_probes)
                ]
                self.pending_held_release = "\n".join(markers)
                self.flush_held_release()
        if not visible_numbers.issubset(self.released_probes):
            return False
        if not all(job["status"] == "completed" for job in held_jobs):
            return False
        try:
            require_success(held_jobs, self.group)
        except ValueError as exc:
            raise RuntimeError(str(exc)) from exc
        log.info(
            "All original held jobs succeeded with verified placements; stale UID baseline: %s", self.refresh_nodes
        )
        return True

    def held_ready(self) -> bool:
        jobs = self.probes("held", 3)
        # Do not retry an invariant failure: even a brief assignment is a failure.
        try:
            require_queued(jobs)
        except ValueError as exc:
            raise RuntimeError(str(exc)) from exc
        return True

    def observe_held(self) -> None:
        def check():
            self.held_ready()
            return self.drained(strict=True)

        self.wait_for("held jobs queued and drain still in effect", check, timeout=OBSERVE_TIMEOUT)

    @staticmethod
    def has_refresh_taint(node: dict) -> bool:
        return any(
            t.get("key") == REFRESH_TAINT_KEY
            and t.get("value") == REFRESH_TAINT_VALUE
            and t.get("effect") == REFRESH_TAINT_EFFECT
            for t in node["spec"].get("taints", [])
        )

    def runner_nodes(self) -> list[dict]:
        data = safe_json_loads(
            self.kube("get", "nodes", "-l", RUNNER_NODE_SELECTOR, "-o", "json"),
            context="runner_nodes",
        )
        return data.get("items", []) if isinstance(data, dict) else []

    def remember_refresh_nodes(self, nodes: list[dict]) -> None:
        self.refresh_nodes.update(
            {node["metadata"]["uid"]: node["metadata"]["name"] for node in nodes if self.has_refresh_taint(node)}
        )

    def taint_runner_nodes(self) -> None:
        before = self.runner_nodes()
        self.remember_refresh_nodes(before)
        if not before:
            log.warning("No runner nodes remain; skipping live refresh-taint coverage")
            return
        self.recipe("taint-nodes")
        after = self.runner_nodes()
        self.remember_refresh_nodes(after)
        by_uid = {node["metadata"]["uid"]: node for node in after}
        # Operational recipes are unchanged; independently verify their effect.
        for node in before:
            uid = node["metadata"]["uid"]
            if uid in by_uid and not self.has_refresh_taint(by_uid[uid]):
                raise RuntimeError(
                    f"Refresh taint was not applied to surviving node {node['metadata']['name']} ({uid})"
                )
        if not self.refresh_nodes:
            log.warning("Runner nodes recycled during tainting; live refresh-taint coverage was not observed")
        log.info("Confirmed refresh taints on runner node UIDs: %s", self.refresh_nodes)

    def verify_refresh_taints(self) -> None:
        if self.refresh_taint_failure is not None:
            raise RefreshTaintError(self.refresh_taint_failure)
        if not self.refresh_nodes:
            return

        def check():
            # Query all nodes: a changed runner label must not look like node deletion.
            nodes_data = safe_json_loads(self.kube("get", "nodes", "-o", "json"), context="verify_refresh_taints")
            if not isinstance(nodes_data, dict):
                return False
            nodes = nodes_data.get("items", [])
            by_uid = {node["metadata"]["uid"]: node for node in nodes}
            surviving = self.refresh_nodes.keys() & by_uid.keys()
            for uid in surviving:
                if not self.has_refresh_taint(by_uid[uid]):
                    self.refresh_taint_failure = (
                        f"Refresh taint removed from surviving node {self.refresh_nodes[uid]} ({uid})"
                    )
                    raise RefreshTaintError(self.refresh_taint_failure)
            recycled = self.refresh_nodes.keys() - by_uid.keys()
            log.info("Refresh taint check: %d surviving nodes, recycled UIDs: %s", len(surviving), sorted(recycled))
            if not surviving:
                log.warning(
                    "All recorded refresh-tainted nodes were recycled; live taint preservation was not observed"
                )
            return True

        self.wait_for("refresh taints preserved", check, 120, poll=5)

    def resume(self) -> None:
        """Restore configured limits while preserving refresh taints on surviving nodes."""
        if not self.resume_limits_restored:
            # Also establish a baseline for standalone restore-only runs.
            self.remember_refresh_nodes(self.runner_nodes())
            self.recipe("resume-runners", timeout=210)
            require_limits(runner_limits(self.scale_sets()), self.desired)
            self.resume_limits_restored = True
        self.verify_refresh_taints()
        self.draining = False

    def cancel_run(self, run: dict) -> None:
        """Allow a run to finish between listing and cancellation."""
        try:
            self.api(f"actions/runs/{run['id']}/cancel", method="POST")
        except subprocess.SubprocessError:
            try:
                status = self.api(f"actions/runs/{run['id']}")["status"]
            except subprocess.SubprocessError:
                log.warning(
                    "Status recheck failed after cancellation failed for run %s",
                    run["id"],
                    exc_info=True,
                )
            else:
                if status == "completed":
                    return
            raise

    def delete_canary_branch(self) -> None:
        """Accept a failed delete only when the exact ref is confirmed absent."""
        try:
            self.api(f"git/refs/heads/{self.branch}", method="DELETE")
        except (subprocess.CalledProcessError, subprocess.TimeoutExpired):
            # A missing ref and other failures can both return 422. Recheck after
            # deletion so another cleaner removing the branch cannot race us.
            # A timeout can also leave the DELETE already applied server-side.
            refs = self.api(f"git/matching-refs/heads/{self.branch}")
            if any(item["ref"] == f"refs/heads/{self.branch}" for item in refs):
                raise

    def cleanup_stale_canary(self) -> None:
        """Select canary runs and validate all open PRs before reclaiming the branch.

        Unrelated workflow runs are logged and ignored regardless of status.
        An unrecognized PR blocks reclamation before any cleanup writes.
        """
        page = 1
        candidates = []
        while True:
            runs = self.api(f"actions/runs?event=pull_request&branch={self.branch}&per_page=100&page={page}")[
                "workflow_runs"
            ]
            candidates.extend(runs)
            if len(runs) < 100:
                break
            page += 1
        pulls = self.api(f"pulls?head={self.repository.split('/')[0]}:{self.branch}&state=open&per_page=100")

        # Ignore runs from unrelated workflows that happen to share the branch.
        canary_runs = [run for run in candidates if is_canary_run(run)]
        foreign = [run for run in candidates if not is_canary_run(run)]
        if foreign:
            log.warning(
                "Ignoring %d foreign workflow run(s) on %s: %s",
                len(foreign),
                self.branch,
                sorted({f"{run.get('name')} ({run.get('id')})" for run in foreign}),
            )

        self._reclaim(canary_runs, pulls)

    def run(self) -> None:
        self.prepare()
        self.cleanup_stale_canary()
        items = []

        def read_initial_scale_sets():
            items[:] = self.scale_sets()
            runner_limits(items)
            return True

        self.wait_for("initial runner scale sets available", read_initial_scale_sets, self.args.drain_timeout)
        actual = runner_limits(items)
        drain_owner = None
        if actual != self.desired:
            log.warning("Restoring initial runner limits: actual=%s, expected=%s", actual, self.desired)
            # Persist intent before repair so failed/cancelled CI startup can hand off recovery too.
            drain_owner = self.record_recovery()
            self.draining = True
            self.resume_limits_restored = False
            self.resume()
            items = self.scale_sets()
        require_limits(runner_limits(items), self.desired)
        self.check_probe(items)
        # Establish metric availability before mutating anything.
        self.wait_for("listener metrics available", lambda: bool(self.capacities()), self.args.drain_timeout)
        self.create_pr()
        self.wait_for("canary workflow created", self.find_run, 120)
        self.wait_for(
            f"in-flight job executing its wait step on {self.cluster}",
            self.inflight_ready,
            self.args.start_timeout,
        )
        if drain_owner is None:
            drain_owner = self.record_recovery()
        self.draining = True
        self.resume_limits_restored = False
        # Convergence, probe release/completion and recipe exit share one budget.
        with self.deadline_scope(time.monotonic() + DRAIN_PHASE_TIMEOUT):
            self.drain = subprocess.Popen(
                ["just", "drain-runners", self.cluster],
                cwd=self.args.root_dir,
                env={
                    **os.environ,
                    **self.env_overrides,
                    **drain_owner,
                    "OSDC_DRAIN_TIMEOUT_SECS": str(DRAIN_PHASE_TIMEOUT),
                },
                start_new_session=True,
            )
            self.wait_for("zero runner limits and advertised capacity", self.drained, self.args.drain_timeout)

            def release_inflight() -> bool:
                # Repeating the same PR body is safe when a response was lost.
                self.api(f"pulls/{self.pr}", {"body": f"OSDC_DRAIN_RELEASE_{self.run_key}"}, "PATCH")
                return True

            # Release retries and probe completion share the existing phase budget.
            with self.deadline_scope(time.monotonic() + 180):
                self.wait_for("in-flight job released after drain", release_inflight, 180)
                self.wait_for("in-flight job succeeds after drain", lambda: self.completed("inflight", 1), 180)
            if self.drain.wait(timeout=self.remaining(DRAIN_PHASE_TIMEOUT)) != 0:
                raise RuntimeError("drain-runners failed")
        # Reap remaining group members before observation/resume, and retire the
        # group ID promptly rather than retaining it through the completion wait.
        self.stop_drain()
        self.observe_held()
        self.taint_runner_nodes()
        self.resume()
        self.wait_with_wakeup("held jobs succeed after undrain", "held", self.held_completed, self.args.finish_timeout)
        self.verify_refresh_taints()

    def stop_drain(self) -> None:
        """Stop all owned recipe children before restoring; avoid late zero patches."""
        if self.drain is None:
            return
        # start_new_session=True makes the original PID the process group ID.
        # The group can outlive its leader, so poll()/wait() alone are insufficient.
        pgid = self.drain.pid
        with contextlib.suppress(ProcessLookupError):
            os.killpg(pgid, signal.SIGTERM)
        try:
            self.wait_for(
                "drain process group stopped", lambda: not process_group_running(pgid, self.deadline), 2, poll=0.5
            )
        except TimeoutError:
            with contextlib.suppress(ProcessLookupError):
                os.killpg(pgid, signal.SIGKILL)
            self.wait_for(
                "drain process group killed", lambda: not process_group_running(pgid, self.deadline), 2, poll=0.5
            )
        self.drain.wait(timeout=self.remaining(2))
        self.drain = None

    def recovery_command(self) -> str:
        """Keep the manual limits-recovery instruction consistent across failures."""
        return f"cd {self.args.root_dir} && OSDC_UNTAINT_NODES=no just resume-runners {self.cluster}"

    def cleanup(self) -> bool:
        """Share one cleanup deadline and retry count with the atexit fallback."""
        if self.cleanup_deadline is None:
            self.cleanup_deadline = time.monotonic() + self.remaining(self.args.cleanup_timeout)
        # Deferring recovery is a contract between the test step and the
        # workflow's restore-only step: decide here, keep _cleanup() linear.
        skip_recovery = self.defer_recovery and (self.draining or self.skip_canary_cleanup)
        skip_canary_cleanup = self.defer_recovery and self.skip_canary_cleanup
        try:
            with self.deadline_scope(self.cleanup_deadline):
                return self._cleanup(
                    skip_recovery=skip_recovery,
                    skip_canary_cleanup=skip_canary_cleanup,
                )
        except TimeoutError:
            log.exception("Cleanup deadline exceeded")
            if self.draining and not self.resume_limits_restored:
                log.error("RECOVERY REQUIRED: %s", self.recovery_command())
            elif self.draining:
                log.error("Runner limits restored; refresh-taint verification incomplete")
            return False
        except Exception:
            log.exception("Cleanup failed unexpectedly")
            return False

    def _stop_owned_writers(self) -> bool:
        """Stop all owned recipe children before restoring; avoid late zero patches."""
        try:
            self.stop_drain()
            if self.recovery_file is not None and self.recovery_file.exists():
                self.stop_recorded_drain()
        except (OSError, subprocess.SubprocessError, ValueError, RuntimeError):
            log.exception("Could not stop drain process; refusing to race it with resume")
            return False
        return True

    def _recover_cluster(self) -> bool:
        """Attempt to resume runner limits up to three times."""
        while self.recovery_attempts < 3 and self.refresh_taint_failure is None:
            try:
                self.remaining(float("inf"))
                self.recovery_attempts += 1
                self.resume()
                return True
            except TimeoutError:
                raise
            except RefreshTaintError:
                break
            except Exception:
                log.exception("Recovery attempt %d/3 failed", self.recovery_attempts)
        return False

    def _reclaim(self, runs: list[dict], pulls: list[dict]) -> None:
        """Cancel active runs, close PRs and delete the temporary branch.

        Runs must already be selected as canary cleanup targets. Validate every
        supplied run and PR before writing, so caller mistakes cannot cancel
        unrelated runs, close their PRs or delete the branch.
        Caller decides whether failure is fatal (startup) or logged (cleanup).
        """
        validate_canary_resources(runs, pulls)
        for run in runs:
            if run["status"] != "completed":
                self.cancel_run(run)
        for pr in pulls:
            self.api(f"pulls/{pr['number']}", {"state": "closed"}, "PATCH")
        self.delete_canary_branch()

    def _cleanup_canary(self) -> bool:
        """Cancel active runs, close the PR and delete the temporary branch."""
        try:
            runs = self.runs()
            # Resolve by head branch even if PR creation timed out before returning its number.
            pulls = self.api(f"pulls?head={self.repository.split('/')[0]}:{self.branch}&state=open&per_page=100")
            self._reclaim(runs, pulls)
            # Clear intent only after all canary cleanup has succeeded.
            self.canary_cleanup_needed = False
        except Exception:
            log.exception("Canary cleanup failed for %s", self.branch)
            return False
        return True

    def _cleanup(self, *, skip_recovery: bool, skip_canary_cleanup: bool) -> bool:
        ok = True

        if not self._stop_owned_writers():
            return False

        if skip_recovery:
            log.warning("Deferring cluster recovery to the workflow restore step; leaving canary cleanup for next run")
            return True

        # Local runs restore first. GitHub API failure must never prevent recovery.
        if self.draining and not self._recover_cluster():
            if not self.resume_limits_restored:
                log.error("RECOVERY REQUIRED: %s", self.recovery_command())
            elif self.refresh_taint_failure is not None:
                log.error("Runner limits restored; taint preservation failed: %s", self.refresh_taint_failure)
            else:
                log.error("Runner limits restored; refresh-taint verification incomplete")
            ok = False

        if not skip_canary_cleanup and self.canary_cleanup_needed:
            ok = self._cleanup_canary() and ok

        if ok and self.defer_recovery and self.recovery_file is not None:
            try:
                self.recovery_file.unlink(missing_ok=True)
            except OSError:
                log.exception("Failed to remove completed recovery intent")
                ok = False

        return ok


def positive(value: str) -> int:
    result = int(value)
    if result <= 0:
        raise argparse.ArgumentTypeError("Must be positive")
    return result


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cluster-id", required=True, help="Cluster ID from clusters.yaml")
    parser.add_argument(
        "--root-dir", type=Path, default=UPSTREAM, help="OSDC root containing clusters.yaml and justfile"
    )
    parser.add_argument("--upstream-dir", type=Path, default=UPSTREAM)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--dry-run", action="store_true", help="Print probe workflow without API calls or cluster access")
    mode.add_argument(
        "--restore-only",
        action="store_true",
        help="Restore configured runner limits and preserve refresh taints on surviving nodes",
    )
    mode.add_argument(
        "--defer-recovery",
        action="store_true",
        help="CI only: leave exception recovery to the subsequent workflow step; skip canary cleanup on failure",
    )
    parser.add_argument(
        "--recovery-file",
        type=Path,
        default=os.environ.get("OSDC_DRAIN_RECOVERY_FILE"),
        help="Linux runner-local intent file shared with the subsequent restore-only step",
    )
    parser.add_argument("--run-timeout", type=positive, default=RUN_TIMEOUT, help="Main flow budget in seconds")
    parser.add_argument("--cleanup-timeout", type=positive, default=CLEANUP_TIMEOUT, help="Cleanup budget in seconds")
    parser.add_argument(
        "--restore-timeout", type=positive, default=RESTORE_TIMEOUT, help="Restore-only total budget in seconds"
    )
    parser.add_argument("--start-timeout", type=positive, default=1800, help="Queue/start deadline in seconds")
    parser.add_argument("--finish-timeout", type=positive, default=1800, help="Queue/completion deadline in seconds")
    parser.add_argument("--drain-timeout", type=positive, default=300, help="Drain convergence deadline in seconds")
    args = parser.parse_args()
    if args.defer_recovery and args.recovery_file is None:
        parser.error("--defer-recovery requires --recovery-file or OSDC_DRAIN_RECOVERY_FILE")
    return args


def on_signal(signum: int, frame: object) -> None:
    raise SystemExit(128 + signum)


def run_controller(test: DrainTest, *, restore_only: bool = False) -> None:
    passed = False

    def handle_signal(signum: int, frame: object) -> None:
        nonlocal passed
        passed = False
        if test.defer_recovery:
            test.skip_canary_cleanup = True
        on_signal(signum, frame)

    previous = {sig: signal.signal(sig, handle_signal) for sig in (signal.SIGINT, signal.SIGTERM)}
    atexit.register(test.cleanup)
    restore_deadline = time.monotonic() + test.args.restore_timeout if restore_only else None
    if restore_only:
        test.cleanup_deadline = restore_deadline
    try:
        if restore_only:
            with test.deadline_scope(restore_deadline):
                test.stop_recorded_drain()
                test.prepare()
                test.draining = True
                test.resume_limits_restored = False
                # Count the initial attempt against cleanup/atexit's shared limit.
                test.recovery_attempts += 1
                test.resume()
        else:
            with test.deadline_scope(time.monotonic() + test.remaining(test.args.run_timeout)):
                test.run()
        passed = True
    finally:
        # A second signal must not interrupt this process's recovery attempt.
        for sig in previous:
            signal.signal(sig, signal.SIG_IGN)
        # Ignore signals before permitting successful CI canary cleanup. A signal
        # during this handoff keeps the atexit fallback on the cancellation path.
        if test.defer_recovery:
            test.skip_canary_cleanup = not passed
        cleanup_error: Exception | None = None
        try:
            cleaned = test.cleanup()
        except Exception as exc:
            cleanup_error = exc
            log.exception("Cleanup raised an unexpected exception")
            cleaned = False
        finally:
            atexit.unregister(test.cleanup)
            for sig, handler in previous.items():
                signal.signal(sig, handler)
        # If the test body itself failed, preserve that failure instead of masking
        # it with a cleanup error. Otherwise surface the cleanup failure.
        if cleanup_error is not None and passed:
            raise cleanup_error
        if passed and not cleaned:
            raise RuntimeError("Test passed but cleanup failed")


def main() -> None:
    args = parse_args()
    if not args.dry_run:
        require_ci_execution()
    # Validate before installing cleanup handlers or fencing old drain writers,
    # including in restore-only mode, which stops writers before prepare().
    cfg = load_cluster_config(args.root_dir / "clusters.yaml", args.cluster_id)
    group = configured_runner_group(cfg)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    key = uuid4().hex
    if args.dry_run:
        print(generate_workflow(key, configured_probe_label(cfg, args.cluster_id), group))
        return
    run_controller(DrainTest(args, key), restore_only=args.restore_only)


if __name__ == "__main__":
    main()
