"""End-to-end integration test for the agent-sandbox.

Unlike the standard OSDC integration test (a canary-repo workflow that runs on a
*runner*), the sandbox's integration point is host -> Job: the agent is not a
runner, so this test drives it the way the real caller does — it launches a pod
in the ai-sandbox namespace and asserts the security invariants that the whole
design rests on, all of which hold WITHOUT any real Bedrock/GitHub credentials:

  1. the pod actually runs under gVisor (runsc), proving the isolation runtime is
     installed and the RuntimeClass pins it to the sandbox fleet;
  2. the pod carries NO cloud credentials and NO IRSA/web-identity token;
  3. no Kubernetes ServiceAccount token is mounted (automount disabled).

It uses a stock busybox probe (not the agent image) so it needs nothing but the
deployed module + its gVisor fleet.
"""

from __future__ import annotations

import json
import subprocess
import time
import uuid

import pytest

NAMESPACE = "ai-sandbox"
PROBE_IMAGE = "public.ecr.aws/docker/library/busybox:1.36"
# Karpenter may need to provision a fresh gVisor node, then boot + install runsc.
SCHEDULE_TIMEOUT_S = 900
POLL_INTERVAL_S = 10

# Credential env vars that must NEVER appear in an agent pod.
FORBIDDEN_ENV = (
    "AWS_ACCESS_KEY_ID=",
    "AWS_SECRET_ACCESS_KEY=",
    "AWS_SESSION_TOKEN=",
    "AWS_WEB_IDENTITY_TOKEN_FILE=",
    "GITHUB_TOKEN=",
    "GH_TOKEN=",
)

PROBE_SCRIPT = (
    'echo "===PROCVERSION==="; cat /proc/version; '
    'echo "===ENV==="; env; '
    'echo "===SATOKEN==="; '
    "if [ -f /var/run/secrets/kubernetes.io/serviceaccount/token ]; "
    "then echo TOKEN_PRESENT; else echo NO_TOKEN; fi"
)

pytestmark = [pytest.mark.live]


def _kubectl(args: list[str], *, check: bool = True, stdin: str | None = None) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["kubectl", "-n", NAMESPACE, *args],
        capture_output=True,
        text=True,
        check=check,
        input=stdin,
    )


def _probe_manifest(name: str) -> str:
    return json.dumps(
        {
            "apiVersion": "batch/v1",
            "kind": "Job",
            "metadata": {"name": name, "namespace": NAMESPACE, "labels": {"app": "sandbox-agent"}},
            "spec": {
                "backoffLimit": 0,
                "ttlSecondsAfterFinished": 300,
                "activeDeadlineSeconds": SCHEDULE_TIMEOUT_S,
                "template": {
                    "metadata": {"labels": {"app": "sandbox-agent"}},
                    "spec": {
                        # gVisor RuntimeClass scheduling auto-adds the ai-sandbox
                        # nodeSelector + tolerations — same path the agent uses.
                        "runtimeClassName": "gvisor",
                        "restartPolicy": "Never",
                        "serviceAccountName": "sandbox-agent",
                        "automountServiceAccountToken": False,
                        "containers": [
                            {
                                "name": "probe",
                                "image": PROBE_IMAGE,
                                "command": ["sh", "-c", PROBE_SCRIPT],
                                "securityContext": {
                                    "runAsNonRoot": True,
                                    "runAsUser": 1000,
                                    "allowPrivilegeEscalation": False,
                                },
                            }
                        ],
                    },
                },
            },
        }
    )


@pytest.fixture(scope="module")
def probe_logs(cluster_id: str) -> str:
    """Launch the probe Job, wait for it to finish, return its logs. Cleans up."""
    # Skip cleanly when the module isn't deployed to this cluster.
    ns = subprocess.run(["kubectl", "get", "ns", NAMESPACE], capture_output=True, text=True, check=False)
    if ns.returncode != 0:
        pytest.skip(f"namespace {NAMESPACE} absent — agent-sandbox not deployed on {cluster_id}")

    name = f"e2e-probe-{uuid.uuid4().hex[:8]}"
    _kubectl(["apply", "-f", "-"], stdin=_probe_manifest(name))
    try:
        deadline = time.monotonic() + SCHEDULE_TIMEOUT_S
        while time.monotonic() < deadline:
            job = json.loads(_kubectl(["get", "job", name, "-o", "json"]).stdout)
            status = job.get("status", {})
            if status.get("succeeded"):
                break
            if status.get("failed"):
                logs = _kubectl(["logs", f"job/{name}"], check=False).stdout
                pytest.fail(f"probe Job failed. Logs:\n{logs}")
            time.sleep(POLL_INTERVAL_S)
        else:
            pods = _kubectl(["get", "pods", "-l", f"job-name={name}", "-o", "wide"], check=False).stdout
            pytest.fail(f"probe Job did not complete within {SCHEDULE_TIMEOUT_S}s (gVisor node not ready?).\n{pods}")

        return _kubectl(["logs", f"job/{name}"]).stdout
    finally:
        _kubectl(["delete", "job", name, "--wait=false"], check=False)


class TestSandboxIsolation:
    def test_runs_under_gvisor(self, probe_logs: str) -> None:
        """/proc/version inside runsc reports a gVisor kernel — proof the pod is
        sandboxed, not running on the host runc."""
        proc = probe_logs.split("===ENV===")[0]
        assert "gVisor" in proc, f"pod is not running under gVisor (runsc). /proc/version:\n{proc}"

    def test_no_credentials_in_env(self, probe_logs: str) -> None:
        env_section = probe_logs.split("===ENV===")[-1].split("===SATOKEN===")[0]
        leaked = [v for v in FORBIDDEN_ENV if v in env_section]
        assert not leaked, f"agent pod exposes credential env vars: {leaked}"

    def test_no_serviceaccount_token(self, probe_logs: str) -> None:
        assert "NO_TOKEN" in probe_logs, "a ServiceAccount token was mounted — automount must be disabled."
