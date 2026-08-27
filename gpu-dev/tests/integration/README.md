# Integration tests (real pods)

These reserve **real** reservations on a live cluster, exec on the pod, and cancel.
They are **off by default** — both the `integration` marker and a reachability check
must pass.

## Run

```bash
# Target env defaults to "staging" (us-west-2). Needs the staging cluster applied
# (terraform-gpu-devservers/staging) and AWS creds + your GitHub user.
GPU_DEV_TEST_ENV=staging GPU_DEV_GITHUB_USER=<your-gh-user> \
  pytest -m integration --run-integration -q
```

- `GPU_DEV_TEST_ENV` — which `Config.ENVIRONMENTS` entry to target (default `staging`).
  Selected via `GPU_DEV_ENVIRONMENT`, which drives region + resource prefix.
- `GPU_DEV_TEST_TIMEOUT_MIN` — how long to wait for a reservation to go active
  (default 15; T4 spot may scale a node up ~10 min).
- Without `--run-integration` (or `GPU_DEV_RUN_INTEGRATION=1`) every test is skipped.
- If the target env's DynamoDB table / creds aren't reachable, tests **skip** (not fail).

## What they cover

- `test_cpu_lifecycle.py` — reserve `cpu-x86` → active → `echo`/`nproc` over SSH →
  cancel; reservation is visible in `list_reservations` while active.
- `test_t4_lifecycle.py` — reserve `t4` → active → `nvidia-smi` → cancel; optional
  `torch.cuda.is_available()` when torch is staged.

Every reservation is cancelled in a `finally`, so a failing test never leaks a pod.

## Safety

CPU and T4 only (cheap). Never reserves H100/B200 here. Intended for **staging**,
not prod — point at prod only for a deliberate cheap CPU smoke.
