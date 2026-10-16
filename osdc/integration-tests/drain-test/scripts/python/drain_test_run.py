"""Exercise real drain/resume recipes with canary jobs on meta-staging-aws-uw1.

Cleanup follows the existing slow-test pattern: finally, atexit and SIGINT/
SIGTERM handlers, plus a same-job recovery step in CI. SIGKILL/runner loss
cannot execute this process's cleanup; no independent recovery agent is used.
"""

from __future__ import annotations

import argparse
import atexit
import contextlib
import json
import logging
import os
import signal
import subprocess
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from uuid import uuid4

import yaml
from drain_test_checks import (
    CLUSTER,
    advertised_capacity,
    listener_pods,
    probe_jobs,
    probe_scale_set,
    require_limits,
    require_queued,
    require_success,
    runner_limits,
)

log = logging.getLogger("osdc-drain-test")
CANARY_REPO = "pytorch/pytorch-canary"
UPSTREAM = Path(__file__).resolve().parents[4]


def generate_workflow(run_key: str) -> str:
    """Generate uniquely identifiable canary probes pinned to the uw1 runner group."""
    if not run_key or not run_key.isalnum():
        raise ValueError("Run key must be nonempty and alphanumeric")
    template = Path(__file__).resolve().parents[2] / "workflows" / "drain-test.yaml.tpl"
    return template.read_text().replace("{{RUN_KEY}}", run_key)


def command(args: list[str], *, timeout: float = 30, **kwargs) -> subprocess.CompletedProcess:
    """Bound subprocess calls, including GitHub API calls for queued jobs."""
    return subprocess.run(args, check=True, capture_output=True, text=True, timeout=timeout, **kwargs)


def api(endpoint: str, payload: dict | None = None, method: str = "GET", *, repository: str = CANARY_REPO) -> dict:
    args = ["gh", "api", f"repos/{repository}/{endpoint}".rstrip("/"), "--method", method]
    if payload is not None:
        args.extend(["--input", "-"])
    output = command(args, input=json.dumps(payload) if payload is not None else None).stdout
    return json.loads(output) if output.strip() else {}


def kubectl(*args: str) -> str:
    return command(["kubectl", "--request-timeout=15s", *args]).stdout


def process_group_running(pgid: int) -> bool:
    """Check all group members, ignoring zombies that cannot execute patches."""
    processes = command(["ps", "-eo", "pgid=,stat="], timeout=2).stdout
    return any(
        int(group) == pgid and not state.startswith("Z")
        for group, state in (line.split() for line in processes.splitlines())
    )


def wait_for(description: str, check, timeout: float, poll: float = 10) -> None:
    """Retry convergence/API failures, but let definitive test failures escape."""
    deadline = time.monotonic() + timeout
    last_error = "condition not reached"
    while True:
        try:
            if check():
                log.info("PASS: %s", description)
                return
        except (ValueError, subprocess.SubprocessError) as exc:
            last_error = str(exc)
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError(f"{description}: deadline exceeded ({last_error})")
        time.sleep(min(poll, remaining))


class DrainTest:
    def __init__(self, args: argparse.Namespace, run_key: str):
        self.args = args
        self.cluster = CLUSTER
        self.group = CLUSTER
        self.repository = CANARY_REPO
        self.run_key = run_key
        self.branch = f"osdc-drain-test-{CLUSTER}-{run_key}"
        self.workflow_name = f"OSDC Drain Test {run_key}"
        self.pr: int | None = None
        self.run_id: int | None = None
        self.branch_created = False
        self.draining = False
        self.drain: subprocess.Popen | None = None
        self.desired: dict[str, int] = {}
        self.env = {
            **os.environ,
            "OSDC_ROOT": str(args.root_dir),
            "OSDC_UPSTREAM": str(args.upstream_dir),
            "CLUSTERS_YAML": str(args.clusters_yaml),
            # Taints can make capacity zero even when maxRunners=0 is broken.
            "OSDC_TAINT_NODES": "no",
        }

    def api(self, endpoint, payload=None, method="GET"):
        return api(endpoint, payload, method)

    def kube(self, *args):
        return kubectl(*args)

    def workflow(self):
        return generate_workflow(self.run_key)

    def check_probe(self, items):
        probe_scale_set(items, self.desired)

    def recipe(self, name: str, timeout: float = 120) -> None:
        result = command(["just", name, self.cluster], cwd=self.args.root_dir, env=self.env, timeout=timeout)
        log.info("%s: %s", name, result.stdout)

    def scale_sets(self) -> list[dict]:
        return json.loads(self.kube("get", "autoscalingrunnersets", "-n", "arc-runners", "-o", "json"))["items"]

    def prepare(self) -> None:
        config = yaml.safe_load(self.args.clusters_yaml.read_text())
        group = config["clusters"][CLUSTER].get("arc-runners", {}).get("runner_group")
        if group != CLUSTER:
            raise ValueError(f"Expected runner group {CLUSTER}, got {group!r}")
        self.recipe("kubeconfig")
        script = self.args.upstream_dir / "modules/arc-runners/scripts/python/runner_max_map.py"
        self.desired = json.loads(
            command(["uv", "run", str(script), CLUSTER], cwd=self.args.root_dir, env=self.env).stdout
        )

    def capacities(self) -> dict[str, float]:
        pods = json.loads(
            self.kube(
                "get",
                "pods",
                "-n",
                "arc-systems",
                "-l",
                "app.kubernetes.io/component=runner-scale-set-listener",
                "-o",
                "json",
            )
        )["items"]
        listeners = listener_pods(pods, set(self.desired))
        # Resolve the deployed fork's ERS metric label from its owning listener.
        objects = json.loads(self.kube("get", "autoscalinglisteners", "-n", "arc-systems", "-o", "json"))["items"]
        metric_names = {
            obj["metadata"]["name"]: {obj["spec"]["autoscalingRunnerSetName"], obj["spec"]["ephemeralRunnerSetName"]}
            for obj in objects
        }

        def scrape(item):
            name, (pod, port) = item
            path = f"/api/v1/namespaces/arc-systems/pods/{pod}:{port}/proxy/metrics"
            allowed = metric_names.get(pod, {name})
            if name not in allowed:
                raise ValueError(f"Listener {pod} does not belong to scale set {name}")
            return name, advertised_capacity(self.kube("get", "--raw", path), name, metric_names=allowed)

        pool = ThreadPoolExecutor(max_workers=8)
        try:
            return dict(pool.map(scrape, listeners.items()))
        finally:
            # Cancellation must reach cluster cleanup without waiting for scrapes.
            pool.shutdown(wait=False, cancel_futures=True)

    def create_pr(self) -> None:
        """Replace workflows only on a unique temporary canary branch, never main."""
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
        # Register intent before the write: a timed-out API call may have succeeded.
        self.branch_created = True
        self.api("git/refs", {"ref": f"refs/heads/{self.branch}", "sha": commit}, "POST")
        self.pr = self.api(
            "pulls",
            {
                "title": f"[NO REVIEW][NO MERGE] {self.workflow_name}",
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
        return True

    def jobs(self) -> list[dict]:
        return self.api(f"actions/runs/{self.run_id}/jobs?per_page=100")["jobs"]

    def inflight_ready(self) -> bool:
        jobs = probe_jobs(self.jobs(), "inflight", 1)
        job = jobs[0]
        if job["status"] == "completed":
            raise RuntimeError("In-flight probe finished before drain")
        if job.get("runner_id") and job.get("runner_group_name") != self.group:
            raise RuntimeError("In-flight probe was assigned to a different runner group")
        return bool(job.get("runner_id")) and any(
            step["name"] == "Wait for drain" and step["status"] == "in_progress" for step in job.get("steps", [])
        )

    def drained(self) -> bool:
        require_limits(runner_limits(self.scale_sets()), dict.fromkeys(self.desired, 0))
        values = self.capacities()
        if any(values.values()):
            raise ValueError(f"Drained listeners still advertise capacity: {values}")
        return True

    def completed(self, phase: str, count: int) -> bool:
        jobs = probe_jobs(self.jobs(), phase, count)
        if any(job["status"] == "completed" and job.get("conclusion") != "success" for job in jobs):
            raise RuntimeError(f"{phase} probe failed: {jobs}")
        if not all(job["status"] == "completed" for job in jobs):
            return False
        try:
            require_success(jobs, self.group)
        except ValueError as exc:
            raise RuntimeError(str(exc)) from exc
        return True

    def held_ready(self) -> bool:
        jobs = probe_jobs(self.jobs(), "held", 3)
        # Do not retry an invariant failure: even a brief assignment is a failure.
        try:
            require_queued(jobs)
        except ValueError as exc:
            raise RuntimeError(str(exc)) from exc
        return True

    def observe_held(self) -> None:
        deadline = time.monotonic() + self.args.observe_seconds
        while True:
            self.held_ready()
            if not self.drained():  # drained() raises on missing/nonzero evidence
                raise RuntimeError("Drain did not remain in effect")
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return
            time.sleep(min(10, remaining))

    def resume(self) -> None:
        """Restore configured limits, not a potentially already-drained snapshot."""
        self.recipe("resume-runners")
        require_limits(runner_limits(self.scale_sets()), self.desired)
        nodes = json.loads(self.kube("get", "nodes", "-l", "workload-type=github-runner", "-o", "json"))["items"]
        if any(t["key"] == "deploy.osdc.io/refresh-pending" for node in nodes for t in node["spec"].get("taints", [])):
            raise ValueError("Runner nodes still have the refresh-pending taint after resume")
        self.draining = False

    def run(self) -> None:
        self.prepare()
        items = self.scale_sets()
        require_limits(runner_limits(items), self.desired)
        self.check_probe(items)
        # Establish metric availability before mutating anything.
        wait_for("listener metrics available", lambda: bool(self.capacities()), self.args.drain_timeout)
        self.create_pr()
        wait_for("canary workflow created", self.find_run, 120)
        wait_for(
            f"in-flight job executing its wait step on {self.cluster}", self.inflight_ready, self.args.start_timeout
        )
        self.draining = True
        self.drain = subprocess.Popen(
            ["just", "drain-runners", self.cluster],
            cwd=self.args.root_dir,
            env={**self.env, "OSDC_DRAIN_TIMEOUT_SECS": "300"},
            start_new_session=True,
        )
        wait_for("zero runner limits and advertised capacity", self.drained, self.args.drain_timeout)
        self.api(f"pulls/{self.pr}", {"body": f"OSDC_DRAIN_RELEASE_{self.run_key}"}, "PATCH")
        wait_for("in-flight job succeeds after drain", lambda: self.completed("inflight", 1), 180)
        if self.drain.wait(timeout=180) != 0:
            raise RuntimeError("drain-runners failed")
        # Reap remaining group members before observation/resume, and retire the
        # group ID promptly rather than retaining it through the completion wait.
        self.stop_drain()
        wait_for("held jobs queued", self.held_ready, 120)
        self.observe_held()
        self.resume()
        wait_for("held jobs succeed after undrain", lambda: self.completed("held", 3), self.args.finish_timeout)

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
            wait_for("drain process group stopped", lambda: not process_group_running(pgid), 2, poll=0.05)
        except TimeoutError:
            with contextlib.suppress(ProcessLookupError):
                os.killpg(pgid, signal.SIGKILL)
            wait_for("drain process group killed", lambda: not process_group_running(pgid), 2, poll=0.05)
        self.drain.wait(timeout=2)
        self.drain = None

    def cleanup(self) -> bool:
        """Idempotent best-effort cleanup; keep failed work eligible for atexit retry."""
        ok = True
        try:
            self.stop_drain()
        except (OSError, subprocess.SubprocessError):
            log.exception("Could not stop drain process; refusing to race it with resume")
            return False
        # Restore first. GitHub API failure must never prevent cluster recovery.
        if self.draining:
            for attempt in range(3):
                try:
                    self.resume()
                    break
                except Exception:
                    log.exception("Recovery attempt %d/3 failed", attempt + 1)
            if self.draining:
                log.error("RECOVERY REQUIRED: cd %s && just resume-runners %s", self.args.root_dir, self.cluster)
                ok = False
        if self.branch_created:
            try:
                for run in self.runs():
                    if run["status"] != "completed":
                        self.api(f"actions/runs/{run['id']}/cancel", method="POST")
                # Resolve by unique head even if PR creation timed out before returning its number.
                pulls = json.loads(
                    command(
                        [
                            "gh",
                            "api",
                            f"repos/{self.repository}/pulls?head={self.repository.split('/')[0]}:{self.branch}&state=open",
                        ]
                    ).stdout
                )
                for pr in pulls:
                    self.api(f"pulls/{pr['number']}", {"state": "closed"}, "PATCH")
                self.api(f"git/refs/heads/{self.branch}", method="DELETE")
                self.branch_created = False
            except Exception:
                log.exception("Canary cleanup failed for %s", self.branch)
                ok = False
        return ok


def positive(value: str) -> int:
    result = int(value)
    if result <= 0:
        raise argparse.ArgumentTypeError("Must be positive")
    return result


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cluster-id", choices=[CLUSTER], required=True)
    parser.add_argument("--root-dir", type=Path, default=UPSTREAM)
    parser.add_argument("--upstream-dir", type=Path, default=UPSTREAM)
    parser.add_argument("--clusters-yaml", type=Path, default=UPSTREAM / "clusters.yaml")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--dry-run", action="store_true", help="Print probe workflow without API calls or cluster access")
    mode.add_argument(
        "--restore-only", action="store_true", help="Resume and verify configured runner limits and taints"
    )
    parser.add_argument("--start-timeout", type=positive, default=1800, help="Queue/start deadline in seconds")
    parser.add_argument("--finish-timeout", type=positive, default=1800, help="Queue/completion deadline in seconds")
    parser.add_argument("--drain-timeout", type=positive, default=600, help="Drain convergence deadline in seconds")
    parser.add_argument("--observe-seconds", type=positive, default=60)
    return parser.parse_args()


def on_signal(signum: int, frame: object) -> None:
    raise SystemExit(128 + signum)


def run_controller(test: DrainTest, *, restore_only: bool = False) -> None:
    previous = {sig: signal.signal(sig, on_signal) for sig in (signal.SIGINT, signal.SIGTERM)}
    atexit.register(test.cleanup)
    passed = False
    try:
        if restore_only:
            test.prepare()
            test.draining = True
            test.resume()
        else:
            test.run()
        passed = True
    finally:
        # A second signal must not interrupt this process's recovery attempt.
        for sig in previous:
            signal.signal(sig, signal.SIG_IGN)
        cleaned = test.cleanup()
        if cleaned:
            atexit.unregister(test.cleanup)
        for sig, handler in previous.items():
            signal.signal(sig, handler)
        if passed and not cleaned:
            raise RuntimeError("Test passed but cleanup failed")


def main() -> None:
    args = parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    key = uuid4().hex
    if args.dry_run:
        print(generate_workflow(key))
        return
    run_controller(DrainTest(args, key), restore_only=args.restore_only)


if __name__ == "__main__":
    main()
