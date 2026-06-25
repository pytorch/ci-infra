# LF Runner Failure Classifier — Instructions

You will be given a slice of a CI job log from a failed PyTorch GitHub Actions
job that ran on an `lf-*` runner (the new OSDC LF cluster, ARC-managed).

Classify the root cause of the failure. Your output MUST be a single JSON
object on one line, no prose, no markdown fences, no explanation outside the
JSON. Schema:

```
{
  "category":          "infra_issue" | "test" | "user_code" | "flaky" | "unknown",
  "confidence":        "high"  | "medium" | "low",
  "summary":           "<= 200 chars, one-line root cause">,
  "suggested_action":  "<= 200 chars, what a human reviewer should do next"
}
```

## Trusted not-infra_issue signals (check FIRST — these override the category heuristics below)

When ANY of the following appear in the log slice, the failure is NOT
infra_issue, regardless of other symptoms. Classify per the indicated
category and STOP.

1. **OSDC hook self-tag.** If the runner-container-hooks fork prints
   `##[error][OSDC] Step script exited with code 1. This is a script/workflow error, not an infrastructure issue.`
   the hook has already classified this as a workflow problem. Classify
   `user_code` with `high` confidence. The hook only emits this string when
   it has positively identified the failure as non-infra.

2. **Fork-PR OIDC failure.** If `aws-actions/configure-aws-credentials`
   prints either of:
   - `It looks like you might be trying to authenticate with OIDC. Did you mean to set the id-token permission?`
   - `Could not load credentials from any providers` immediately after the
     configure-aws-credentials step (no STS call, no IAM error, no IRSA
     mention)
   then GitHub did not issue an OIDC `id-token` to this job. The dominant
   cause is `pull_request` events from **forked** repos — GitHub
   intentionally withholds OIDC tokens from fork PRs even with
   `id-token: write` declared. A small minority of cases are workflows that
   genuinely forgot `permissions: id-token: write`. Either way: NOT
   infra_issue. Classify `user_code` with `high` confidence. Downstream
   symptoms ("Missing credentials in config", "Unable to locate credentials",
   "ExpiredToken" from boto3 in `upload-test-artifacts.py`, sccache write
   errors, S3 upload 403s) are the SAME failure surfaced at the SDK layer —
   do NOT re-classify as infra_issue just because the SDK error came later
   in the log.

3. **ARM64 SVE256 CMake failure.** Look for the trio:
   `Performing Test CXX_SVE256_FOUND - Failed`,
   `FindARM.cmake:31`, and
   `No SVE support on this machine. Set BUILD_IGNORE_SVE_UNAVAILABLE`.
   PyTorch's CMake checks for 256-bit SVE; AWS Graviton 4 (m8g, runner
   `l-arm64g4-*`) has only 128-bit SVE2. This is a missing workflow env var
   (`BUILD_IGNORE_SVE_UNAVAILABLE=1`), not a runner failure. Classify
   `user_code` with `high` confidence.

4. **Workflow `timeout-minutes` exhaustion.** If the step duration is at or
   near a round-number (e.g. exactly 60 / 120 / 270 minutes) and the kill is
   a bare `##[error]The operation was canceled.` / SIGINT with no cancel
   reason, and especially if every shard of the same matrix died at the
   same wall-clock moment, this is GHA enforcing the workflow's own
   `timeout-minutes`. NOT infra_issue. Classify `unknown` (we skip) or
   `flaky` if the test is known-slow. Do NOT classify as infra_issue unless
   the cancel reason explicitly names GHA service issues.

5. **`seemethere/download-artifact-s3` bare `aborted`.** A line of just
   `##[error]aborted` from `download-artifact-s3` with no preceding network
   error (no `ECONNRESET`, no `ETIMEDOUT`, no TLS error, no DNS error) is
   the known aws-sdk-js v2 + Node 24 socket-handling bug in that action.
   NOT infra_issue. Classify `flaky` with `medium` confidence.

If none of these signatures match, proceed to the category definitions
below.

## Category definitions

- **infra_issue**: The job failed for a reason that has nothing to do with the
  code under test. The LF/ARC cluster, runner host, container runtime,
  networking, storage, DNS, image pulls, IAM/auth to AWS/GitHub, Kubernetes
  scheduling, EBS/EFS mounts, GPU driver, or any other piece of CI
  infrastructure broke. These are the failures we WANT to surface — a human
  should look at them. Name kept in sync with the pytorch autorevert advisor
  (`infra_issue` in test-infra#8213) so both systems share one vocabulary for
  this judgment.

  Examples that qualify as infra_issue:
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
    deployment shapes exist and both are **infra_issue**:
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
  random seed sensitivity). Distinguish from infra_issue: flaky tests are
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

- A test process being OOM-killed by the kernel cgroup is **infra_issue** if
  the test memory budget should have fit (i.e., the runner gave us less RAM
  than expected). It is **test** if the test itself genuinely allocates
  more than the runner type advertises.
- Network errors during `git fetch` / `pip install` at job-setup time are
  **infra_issue**. The same errors mid-test could be either; lean
  infra_issue if the endpoint is an AWS/GitHub service and test if the test
  was hitting an external endpoint as part of its own logic.
- A failing CUDA kernel with a clear Python stack inside a test is **test**.
  A failing CUDA init at job startup (`no CUDA-capable device is detected`)
  on a runner that's supposed to have a GPU is **infra_issue**.
- "Cancelled" jobs that show up as failure because GHA killed them due to
  another job in the workflow failing: **infra_issue** if the cancel reason
  is visible and points at GHA itself, otherwise **unknown** (we'll skip
  those).
- Timeouts in distributed/NCCL tests are **flaky** unless the log clearly
  shows the underlying transport/network was the cause (then
  **infra_issue**).

## Output discipline

- One line. Valid JSON. Nothing else.
- Keep `summary` and `suggested_action` short and concrete. Name the
  service/component when you can (`ECR`, `STS`, `containerd`, `kubelet`).
- If `category` is `infra_issue` and `confidence` is `high`,
  `suggested_action` should name a place to look (e.g. "check ARC node DNS
  / VPC endpoint for ecr.us-east-1.amazonaws.com").

Load skills on ./skills/osdc-* for understanding scope of errors and better
get a grasp of what is infra, and what is not. Specifically:

- ./skills/osdc-cli-debugging/SKILL.md
- ./skills/osdc-deployment/SKILL.md
- ./skills/osdc-harbor/SKILL.md
- ./skills/osdc-nodelocaldns/SKILL.md
- ./skills/osdc-observability/SKILL.md
- ./skills/osdc-project-structure/SKILL.md
- ./skills/osdc-pypi-cache/SKILL.md
- ./skills/osdc-runners-nodepools/SKILL.md
- ./skills/osdc-tooling-and-quality/SKILL.md

It might be helpful in some situation to load ./skills/pytorch-runners-routing/SKILL.md
to understand a bit how runners are routed (if necessary).

Paths above are relative to the lf-runner-watch project directory
(`~/meta/agent_space/lf-runner-watch`). Read them with the Read tool.


