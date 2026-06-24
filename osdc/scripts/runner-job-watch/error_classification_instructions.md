# LF Runner Failure Classifier — Instructions

You will be given a slice of a CI job log from a failed PyTorch GitHub Actions
job that ran on an `lf-*` runner (the new OSDC LF cluster, ARC-managed).

Classify the root cause of the failure. Your output MUST be a single JSON
object on one line, no prose, no markdown fences, no explanation outside the
JSON. Schema:

```
{
  "category":          "infra" | "test" | "user_code" | "flaky" | "unknown",
  "confidence":        "high"  | "medium" | "low",
  "summary":           "<= 200 chars, one-line root cause">,
  "suggested_action":  "<= 200 chars, what a human reviewer should do next"
}
```

## Category definitions

- **infra**: The job failed for a reason that has nothing to do with the code
  under test. The LF/ARC cluster, runner host, container runtime, networking,
  storage, DNS, image pulls, IAM/auth to AWS/GitHub, Kubernetes scheduling,
  EBS/EFS mounts, GPU driver, or any other piece of CI infrastructure broke.
  These are the failures we WANT to surface — a human should look at them.

  Examples that qualify as infra:
  - ECR pull failures, image pull timeouts, ImagePullBackOff
  - DNS resolution errors (`getaddrinfo`, `Temporary failure in name resolution`)
  - Networking timeouts to AWS services (S3, ECR, STS, EC2 metadata)
  - IAM/credential errors (`Unable to locate credentials`, `AccessDenied`,
    `ExpiredToken`, IRSA failures)
  - Pod evicted, OOMKilled (when the killer is the kubelet/cgroup, not the
    test process), node NotReady, taint/toleration mismatches
  - Disk full on the runner, no space left on device, EBS mount failures
  - GitHub Actions runner registration / heartbeat failures
  - Docker daemon errors, container runtime errors, containerd issues
  - GPU not visible (`nvidia-smi` failing, CUDA init errors when the host
    clearly doesn't have a usable GPU)
  - apt/pip/conda mirror outages on the runner side
  - Upstream PyPI / pythonhosted / download.pytorch.org refusals, throttling,
    or connection errors (HTTP 429, 403, 5xx, `Connection refused`,
    `Connection reset`, TLS handshake failures, DNS failures targeting
    `pypi.org`, `files.pythonhosted.org`, `download.pytorch.org`). Two
    deployment shapes exist and both are **infra**:
      - **Cache present** (most OSDC runners): pip is pointed at the
        in-cluster `pypi-cache-{slug}` proxy via
        `PIP_INDEX_URL=http://pypi-cache-{slug}.pypi-cache.svc.cluster.local:8080/simple/`,
        which itself falls back to upstream PyPI. The failure can be
        runner→cache (cache pod unreachable, DNS for `pypi-cache.svc...`
        failing, cache returning 5xx) OR cache→upstream (the proxy's
        `@pypi_fallback` couldn't reach `pypi.org`/`files.pythonhosted.org`).
      - **No cache** (some configurations): the runner reaches PyPI directly,
        log shows the public hostname in the failing URL.
    Read the failing URL/host in the log to tell which path was hit.
  - GHA artifact upload/download failures due to GHA service issues
  - RPC errors, timeouts, etc

- **test**: A specific PyTorch test failed in a way that points at the code
  being tested (assertion failures, numerical mismatches, real Python
  exceptions inside the test body). Do not log these prominently.

- **user_code**: Build errors, lint errors, type errors, import errors that
  are clearly the PR author's fault (syntax errors, missing symbols,
  undeclared dependencies in setup.py, etc.). Do not log prominently.

- **flaky**: A known-flaky test that retried/failed in a way that matches the
  usual flaky-test fingerprint (e.g. timeout in distributed test, NCCL hang,
  random seed sensitivity). Distinguish from infra: flaky tests are
  intermittent test bugs, not cluster bugs.

- **unknown**: You genuinely cannot tell from the log slice. Use this
  sparingly — prefer guessing with low confidence over `unknown`.

## Confidence guidance

- **high**: One unambiguous error message in the log directly maps to a
  category. Smoking gun.
- **medium**: The error pattern is suggestive but the log could be read more
  than one way.
- **low**: You are guessing from indirect signals. Use `unknown` instead
  unless you have a real hypothesis.

## Edge cases

- A test process being OOM-killed by the kernel cgroup is **infra** if the
  test memory budget should have fit (i.e., the runner gave us less RAM
  than expected). It is **test** if the test itself genuinely allocates
  more than the runner type advertises.
- Network errors during `git fetch` / `pip install` at job-setup time are
  **infra**. The same errors mid-test could be either; lean infra if the
  endpoint is an AWS/GitHub service and test if the test was hitting an
  external endpoint as part of its own logic.
- A failing CUDA kernel with a clear Python stack inside a test is **test**.
  A failing CUDA init at job startup (`no CUDA-capable device is detected`)
  on a runner that's supposed to have a GPU is **infra**.
- "Cancelled" jobs that show up as failure because GHA killed them due to
  another job in the workflow failing: **infra** if the cancel reason is
  visible and points at GHA itself, otherwise **unknown** (we'll skip those).
- Timeouts in distributed/NCCL tests are **flaky** unless the log clearly
  shows the underlying transport/network was the cause (then **infra**).

## Output discipline

- One line. Valid JSON. Nothing else.
- Keep `summary` and `suggested_action` short and concrete. Name the
  service/component when you can (`ECR`, `STS`, `containerd`, `kubelet`).
- If `category` is `infra` and `confidence` is `high`, `suggested_action`
  should name a place to look (e.g. "check ARC node DNS / VPC endpoint for
  ecr.us-east-1.amazonaws.com").

Load all osdc-* skills on (../.claude/skills/osdc-*) for understanding the 
scope of errors and better get a grasp of what is infra, and what is not. Specifically:

- osdc-cli-debugging/SKILL.md
- osdc-deployment/SKILL.md
- osdc-harbor/SKILL.md
- osdc-nodelocaldns/SKILL.md
- osdc-observability/SKILL.md
- osdc-project-structure/SKILL.md
- osdc-pypi-cache/SKILL.md
- osdc-runners-nodepools/SKILL.md
- osdc-tooling-and-quality/SKILL.md

It might be helpful in some situation to load pytorch-runners-routing/SKILL.md
to understand a bit how runners are routed in pytorch/pytorch repo (if necessary).

Paths above are relative to the lf-runner-watch project directory
(`~/meta/agent_space/lf-runner-watch`). Read them with the Read tool.


