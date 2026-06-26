---
name: pytorch-runners-routing
description: >
  How PyTorch CI runners and jobs are defined and resolved across .github/workflows/ —
  test-matrix runner labels, the runner determinator (lf / arc / mt- prefixes and the
  PR #186663 lf+arc combination), the test-infra #5132 experiment config, EC2 -> ARC
  label translation via .github/arc.yaml and .github/scripts/map_ec2_to_arc.py
  (including the meta_only_runners override for H100/B200), build vs test runner
  decoupling, the _runner-determinator.yml / _linux-build.yml / _linux-test.yml flow,
  and per-workflow opt-in / opt-out mechanisms. Applies to ~/meta/pytorch.
  Load this skill whenever working on, researching, debugging, reviewing, or answering
  ANY question about PyTorch CI runners, runner labels, test matrices, runner prefixes
  (lf, mt-, lf-, amd-do-), experiments (lf / arc / amd-do), the runner determinator,
  ARC fleet routing, H100/B200/A100 runner placement, GitHub Actions runs-on values
  in .github/workflows/, or how jobs get assigned to specific runner pools.
---

# PyTorch CI Runner Routing & Resolution

## Scope

Everything about how `.github/workflows/*.yml` jobs in pytorch/pytorch land on a
specific runner — label definition, prefix resolution, fleet routing, the experiment
system, and the EC2-to-ARC translation pipeline. Read this BEFORE touching any
`runs-on:` value, any `test-matrix:` entry, any `runner_prefix:` interpolation, any
`check_experiments:` / `opt_out_experiments:` input, `arc.yaml`, or
`runner_determinator.py` / `map_ec2_to_arc.py`.

## Mental Model: Four Inputs, One Final Label

Every PyTorch CI job runs on a `runs-on:` value that GitHub uses to pick a runner.
That label is **assembled at runtime** from four things:

1. **Bare label** in the workflow YAML — `linux.c7i.2xlarge`, `linux.aws.h100`, etc.
2. **`runner_prefix:` input** to the reusable workflow — interpolated from the
   determinator's `label-type` output (`""`, `"lf."`, `"mt-"`, `"lf-"`, optionally
   plus `"amd-do-"`).
3. **`use-arc:` flag** — triggers the EC2 -> ARC label translation step.
4. **`arc.yaml` mapping** — the EC2 -> ARC translation table, plus the
   `meta_only_runners` override list.

A label like `linux.aws.h100` can become any of:
- `linux.aws.h100` (default fleet, no experiments)
- `lf.linux.aws.h100` (LF experiment on, no ARC)
- `mt-l-x86iamx-22-225-h100` (ARC experiment on — translated via `arc.yaml`)
- `lf-l-x86iamx-22-225-h100` (ARC + LF both on, post PR #186663)

H100/B200 are special-cased: regardless of the four inputs above, they are pinned
to `mt-<arc-label>` via the `meta_only_runners` override (see [Meta-Only Override
for H100/B200](#meta-only-override-for-h100b200) below).

## The 5 Files That Define Runner Routing

| File | Purpose |
|------|---------|
| `.github/workflows/_runner-determinator.yml` | Reusable workflow. Caller passes `check_experiments` / `opt_out_experiments`. Emits `label-type` (the prefix), `use-arc`, `amd-do-label-type`, `runner-config`, `runner-type`, `runner-label`, `ci-docker-hash`. |
| `.github/scripts/runner_determinator.py` | The script. Fetches the rollout config from `pytorch/test-infra#5132` (first comment), evaluates per-user opt-in/out + per-workflow allowlist + rollout %, and emits the prefix. |
| `.github/scripts/test_runner_determinator.py` | Tests for the determinator. Run on PR changes to the script. |
| `.github/arc.yaml` | EC2 -> ARC label mapping (`runner_mapping`) AND `meta_only_runners` override list for H100/B200. |
| `.github/scripts/map_ec2_to_arc.py` + `test_map_ec2_to_arc.py` | Script that rewrites a test-matrix's `runner:` field from EC2 labels to ARC labels using `arc.yaml`. Invoked from `_linux-build.yml`. |

## The Two Reusable Workflows That Consume the Determinator Output

| File | Role |
|------|------|
| `.github/workflows/_linux-build.yml` | Build job. `runs-on: ${{ inputs.runner_prefix }}${{ inputs.runner }}`. Also runs `map_ec2_to_arc.py --prefix ${{ inputs.runner_prefix }}` over the test-matrix and re-emits it as the build job's `test-matrix` output. The translated matrix is what the test job consumes. |
| `.github/workflows/_linux-test.yml` | Test job. Has two job branches gated on `inputs.use-arc`: `test` (EC2 path, EBS volumes) and `test-osdc` (ARC path, IAM role `arn:aws:iam::308535385114:role/arc`, container `--gpus all`). Both use `runs-on: ${{ matrix.runner }}` from the translated test-matrix. |

There are sibling workflows for other platforms (`_mac-build.yml`, `_mac-test.yml`,
`_win-build.yml`, `_win-test.yml`, `_rocm-test.yml`, `_xpu-test.yml`, `_vllm-build.yml`,
`_vllm-benchmark.yml`, `_linux-test-stable-fa3.yml`). Not all of them participate in
the determinator/ARC flow — Linux build + test are the primary path.

## Caller Pattern (How a Workflow Plugs Into the Determinator)

Every workflow that wants experiment-driven runner placement has a `get-label-type`
job at the top calling `_runner-determinator.yml`, then passes its outputs into the
build/test jobs:

```yaml
jobs:
  get-label-type:
    name: get-label-type
    uses: pytorch/pytorch/.github/workflows/_runner-determinator.yml@main
    with:
      triggering_actor: ${{ github.triggering_actor }}
      issue_owner: ${{ github.event.pull_request.user.login || github.event.issue.user.login }}
      curr_branch: ${{ github.head_ref || github.ref_name }}
      curr_ref_type: ${{ github.ref_type }}
      check_experiments: arc,lf       # which non-default experiments to consider for THIS workflow
      opt_out_experiments: lf         # which experiments to explicitly skip for THIS workflow

  some-build:
    uses: ./.github/workflows/_linux-build.yml
    needs: get-label-type
    with:
      runner_prefix: "${{ needs.get-label-type.outputs.label-type }}"   # build CPU runner gets the prefix
      runner: linux.c7i.2xlarge                                          # base label
      use-arc: ${{ needs.get-label-type.outputs.use-arc == 'true' }}     # forwards ARC routing to test job
      test-matrix: |
        { include: [
          { config: "default", shard: 1, num_shards: 1, runner: "linux.aws.h100" },
        ]}
```

`runner_prefix` is empty by default — without the determinator interpolation, the
build lands on the default fleet (AWS EC2).

## Experiment System

### Config Source

The rollout config lives in the **first comment** of GitHub issue
`pytorch/test-infra#5132`. The script fetches it at runtime via the GitHub API.
The issue body has two `---`-separated sections:

1. **Settings YAML** — defines available experiments and their rollout %.
2. **User opt-in list** — `@username,experiment[:percent],-experiment_to_opt_out`.

The issue number is overridable via the `issue_number` input on
`_runner-determinator.yml` (default `"5132"`).

### Experiment Settings (per the docstring example)

```yaml
experiments:
  lf:
    rollout_percent: 25     # NB: the code uses `rollout_perc` (5-char), the docstring example has a typo
    all_branches: false
    default: true
  arc:
    rollout_perc: 50
    all_branches: true
    default: false
    workflows: pull,trunk   # ALL or empty = every workflow; "-name" excludes a workflow even under ALL
```

Per-experiment fields (`Experiment` NamedTuple in `runner_determinator.py`):

- `rollout_perc: float` — % of workflows that get this experiment when no user opted in.
- `all_branches: bool` — if False, exception branches (release/main/etc.) skip the experiment.
- `default: bool` — if False, the experiment only runs when the caller passes it in `check_experiments`.
- `workflows: str` — comma-separated allowlist of `github.workflow` names. `"ALL"` or empty = every workflow. `"-Name"` prefix excludes that workflow even when `"ALL"` is present. Excludes win over includes.

### User Opt-in / Opt-out

After the `---` separator: each line `@user,experiment1,experiment2:N,-experiment3`:

- Plain entry → opt-in 100%.
- `experiment:N` → per-user N% rollout (0-100).
- `-experiment` → explicit opt-out.
- `#@user,...` → user opts out of ALL experiments.
- Triggering actor and PR author are both checked.

### Per-Workflow Opt-In / Opt-Out (in the workflow YAML)

| Input | Effect |
|-------|--------|
| `check_experiments: arc,lf` | Only consider these experiments (overrides `default: true` for non-listed ones — they will NOT run unless listed). |
| `check_experiments` unset | Use the experiment's `default:` flag from the config. |
| `opt_out_experiments: lf` | Explicitly skip the named experiment regardless of `default:` / `check_experiments`. Higher priority than `check_experiments`. |

### PR-Level Kill Switch

Apply the `no-runner-experiments` label on a PR → determinator skips everything
and returns the default-fleet prefix (empty). See `OPT_OUT_LABEL` in
`runner_determinator.py`.

## The Four Prefix States

| ARC | LF | `label-type` output | What lands on the build CPU runner |
|:---:|:---:|---|---|
| off | off | `""` | `linux.c7i.2xlarge` |
| off | on | `"lf."` | `lf.linux.c7i.2xlarge` |
| on | off | `"mt-"` (= `ARC_LABEL_PREFIX`) | `mt-linux.c7i.2xlarge` -> via `map_ec2_to_arc.py` -> `mt-l-x86iavx512-8-64` |
| on | on | `"lf-"` (after [PR #186663](https://github.com/pytorch/pytorch/pull/186663)) | `lf-linux.c7i.2xlarge` -> via `map_ec2_to_arc.py` -> `lf-l-x86iavx512-8-64` |

Notes:

- ARC takes precedence over LF when both are enabled (returns `RunnerPrefixResult(use_arc=True)`).
- Before PR #186663: ARC+LF returned `"mt-"`. After: ARC+LF returns `"lf-"`, but still
  sets `use_arc=True`. This routes jobs to LF-fleet ARC runners while keeping ARC's
  scheduling semantics.
- The canary repo (`pytorch/pytorch-canary`) appends `.c` to the LF prefix:
  `"lf.c."` standalone, `"c-mt-"` for ARC standalone.
- `amd-do` experiment is **exposed separately** via the `amd-do-label-type` output
  (`"amd-do-"`), not folded into `label-type`. Consumers wire it per-job.
- Only one non-fleet experiment can stack with one fleet at a time (the rest are
  dropped with a warning).

## EC2 -> ARC Label Translation

When ARC is enabled (`use-arc: true`), the test job runs `test-osdc` (the OSDC branch
in `_linux-test.yml`) which needs ARC fleet labels, not EC2 labels. The translation
happens in the **build job**, not the test job:

1. `_linux-build.yml` runs `map_ec2_to_arc.py --prefix "${RUNNER_PREFIX}" "${FILTERED_TEST_MATRIX}"`.
2. The script:
   - Loads `runner_mapping` from `.github/arc.yaml`.
   - For each `include[].runner`: strips the prefix, looks the bare label up in
     `runner_mapping`, prepends the prefix back (with passthrough rules).
   - Drops entries with config in `excluded_configs` (currently `{"onnx"}` — see
     the comment in the script for why).
3. The translated test-matrix becomes the build job's `test-matrix` output.
4. The test job consumes `needs.<build>.outputs.test-matrix` as its input, so
   `matrix.runner` is the already-translated ARC label.

### `arc.yaml` — The Mapping Table

```yaml
runner_mapping:
  linux.c7i.2xlarge: l-x86iavx512-8-64
  linux.12xlarge.memory: l-x86iavx512-48-384
  linux.aws.h100: l-x86iamx-22-225-h100
  linux.aws.h100.4: l-x86iamx-88-900-h100-4
  linux.aws.h100.8: l-bx86iamx-176-1800-h100-8
  linux.dgx.b200: l-x86iamx-22-225-b200
  # ... CPU, A100, H100, B200, A10G, T4, L4, ARM64, ROCm, XPU, TPU
  linux.rocm.gpu.2: linux.rocm.gpu.2                    # passthrough — identity mapping
  linux.idc.xpu: linux.idc.xpu                          # passthrough
```

**Passthrough rule** (identity mapping in the table): `mapped == clean` means
the runner is not OSDC-managed (e.g. ROCm, XPU, TPU). The script keeps the
original label without prefixing.

**ARC label naming convention** is documented as a comment at the top of `arc.yaml`:

```
{os}-[b]{arch}{vendor}{features}-{vcpu}-{memory}[-{gpu_type}[-{gpu_count}]]
```

Examples: `l-x86iavx512-8-64` (Linux, x86, Intel AVX-512, 8 vCPU, 64 GiB),
`l-bx86iamx-176-1800-h100-8` (Linux, bare-metal, x86, Intel AMX, 176 vCPU,
1800 GiB, 8x H100).

### Meta-Only Override for H100/B200

H100 and B200 hardware only exists on the Meta ARC fleet — LF and AWS EC2 don't carry
those machines. Routing an H100/B200 job to `lf-l-...-h100` would queue forever.

The fix: `meta_only_runners` list in `.github/arc.yaml`:

```yaml
meta_only_runners:
  - linux.aws.h100
  - linux.aws.h100.4
  - linux.aws.h100.8
  - linux.dgx.b200
```

In `map_ec2_to_arc.py`, the per-entry loop has an early-return for any label in this
set: `entry["runner"] = "mt-" + mapped` (forces Meta ARC fleet, overrides whatever
`--prefix` was passed). This decouples the H100/B200 test runner from the build
runner's fleet — the build CPU is free to land anywhere the experiment routes it,
but the GPU test job is always pinned to Meta ARC.

**Important**: `linux.dgx.b200.8` (8-GPU B200, used by `b200-distributed.yml` and
`b200-symm-mem.yml`) is **NOT** in `runner_mapping` today. Those workflows do NOT set
`use-arc: true`, so `map_ec2_to_arc.py` is never called for them — they land on AWS
EC2 directly. If you ever migrate them to ARC, the correct ARC label must be added
to BOTH `runner_mapping` AND `meta_only_runners`.

## Build vs Test Runner Decoupling — Why It Matters

A single `_runner-determinator.yml` call typically feeds BOTH the build job's
`runner_prefix` AND the build job's `use-arc` (forwarded to the test job). They are
coupled by default. There are three patterns in the wild:

1. **Fully coupled (default)** — `runner_prefix:` and `use-arc:` both come from the
   same `get-label-type`. Build CPU runner and test GPU runner travel together
   through the experiment combinations.
2. **Hardcoded `mt-`** (`runner_prefix: "mt-"`) — bypasses the determinator's output
   for that specific build job. Used by `attention_op_microbenchmark.yml` and
   `operator_microbenchmark.yml` for their B200 build paths. Comment in those files:
   "always use OSDC runner to test this workflow".
3. **Per-workflow opt-out** (`opt_out_experiments: lf`) — used by
   `inductor-perf-test-nightly-h100.yml`, `inductor-perf-test-b200.yml`,
   `inductor-periodic.yml`. Removes a single experiment from consideration for that
   workflow.

The `meta_only_runners` mechanism is the **only** decoupling that targets a specific
runner label rather than a whole workflow — it's the cleanest tool when "the build
can land anywhere but this specific test runner must always be Meta ARC".

## GPU Workflows: Inventory and Mechanics

The workflows that touch high-end NVIDIA GPUs in this repo (H100, B200, A100). Read
each before editing — runner conventions differ.

| File | GPU test runners | `check_experiments` | `opt_out_experiments` |
|------|------------------|---------------------|----------------------|
| `.github/workflows/test-h100.yml` | `linux.aws.h100` | `arc,lf` | — |
| `.github/workflows/h100-cutlass-backend.yml` | `linux.aws.h100` | `arc,lf` | — |
| `.github/workflows/h100-distributed.yml` | `linux.aws.h100.8` | `arc,lf` | — |
| `.github/workflows/h100-symm-mem.yml` | `linux.aws.h100.4` | `arc,lf` | — |
| `.github/workflows/test-b200.yml` | `linux.dgx.b200` | `arc,lf` | — |
| `.github/workflows/b200-distributed.yml` | `linux.dgx.b200.8` | (none) | — |
| `.github/workflows/b200-symm-mem.yml` | `linux.dgx.b200.8` | (none) | — |
| `.github/workflows/inductor-perf-test-nightly-h100.yml` | `linux.aws.h100` | `arc` | `lf` |
| `.github/workflows/inductor-perf-test-b200.yml` | `linux.dgx.b200` | (none) | `lf` |
| `.github/workflows/inductor-pallas.yml` | `linux.aws.h100` | `arc` | — |
| `.github/workflows/inductor-periodic.yml` | `linux.aws.h100` (1 entry) + many g5/a100 | `arc,amd-do` | `lf` |
| `.github/workflows/attention_op_microbenchmark.yml` | `linux.aws.a100`, `linux.aws.h100`, `linux.dgx.b200` (B200 path: `runner_prefix: "mt-"` hardcoded) | `arc,lf` | — |
| `.github/workflows/operator_microbenchmark.yml` | `linux.aws.h100`, `linux.aws.a100`, `linux.dgx.b200` (B200 path: `runner_prefix: "mt-"` hardcoded) | `arc,lf` | — |
| `.github/workflows/operator_microbenchmark_compare.yml` | conditional H100/A100/B200 via `${{ inputs.gpu }}` | (none, on the get-label-type job) | — |
| `.github/workflows/vllm.yml` | `linux.dgx.b200` (1 entry) + many g6 | NO `get-label-type` job | — |
| `.github/workflows/vllm-benchmark.yml` | `inputs.runners` default `"h100,b200"` → external matrix gen | NO `get-label-type` job | — |

## Common Runner Label Families

### CPU build runners (default fleet, prefixed by experiment)

`linux.c7i.2xlarge`, `linux.4xlarge`, `linux.12xlarge`, `linux.12xlarge.memory`,
`linux.r7i.4xlarge`, `linux.24xlarge.memory`, `linux.24xl.spr-metal`. These flow
through `arc.yaml` -> `l-x86iavx512-*` family.

### NVIDIA GPU test runners

| Label | GPU |
|-------|-----|
| `linux.g4dn.4xlarge.nvidia.gpu`, `linux.g4dn.12xlarge.nvidia.gpu`, `linux.g4dn.metal.nvidia.gpu` | T4 |
| `linux.g5.4xlarge.nvidia.gpu`, `linux.g5.12xlarge.nvidia.gpu`, `linux.g5.48xlarge.nvidia.gpu` | A10G |
| `linux.g6.4xlarge.experimental.nvidia.gpu`, `linux.g6.12xlarge.nvidia.gpu` | L4 |
| `linux.aws.a100` | A100 (p4de) |
| `linux.aws.h100`, `linux.aws.h100.4`, `linux.aws.h100.8` | H100 (p5) |
| `linux.dgx.b200`, `linux.dgx.b200.8` | B200 (p6) |

### ARM64

`linux.arm64.2xlarge`, `linux.arm64.m7g.4xlarge`, `linux.arm64.m8g.4xlarge`,
`linux.arm64.r7g.12xlarge.memory`, `linux.arm64.m7g.metal`.

### Partner hardware (passthrough — never prefixed/translated)

`linux.rocm.gpu.*`, `linux.idc.xpu`, `linux.google.tpuv7x.1`.

## `_runner-determinator.yml` Outputs (Full Reference)

| Output | What it is | Typical consumer |
|--------|-----------|-----------------|
| `label-type` | The runner prefix (`""`, `"lf."`, `"mt-"`, `"lf-"`) | `runner_prefix:` input on `_linux-build.yml` |
| `use-arc` | String `"true"` / `"false"` | `use-arc:` input on `_linux-build.yml` / `_linux-test.yml`, often compared with `== 'true'` |
| `amd-do-label-type` | `"amd-do-"` if amd-do experiment enabled, else `""` | Per-job AMD pinning |
| `ci-docker-hash` | `git rev-parse HEAD:.ci/docker` (the tree hash) | `ci-docker-hash:` input on `_linux-build.yml` for image tag |
| `runner-config` | Normalized runner config (`m7g` / `m8g`) | ARM64 metal runner selection in some workflows |
| `runner-type` | Runner suffix (`metal` / `metal-24xl`) | ARM64 metal runner selection |
| `runner-label` | Fully qualified ARM label `linux.arm64.<config>.<type>` | Direct `runs-on:` on some ARM workflows |

## Decision Trees / Recipes

### "I want this workflow to never use LF runners"

Add `opt_out_experiments: lf` to the `_runner-determinator.yml` caller. The build's
`runner_prefix` will never include `lf.` / `lf-`. Existing examples:
`inductor-perf-test-nightly-h100.yml`, `inductor-perf-test-b200.yml`,
`inductor-periodic.yml`.

### "I want this specific GPU test runner to always go to Meta ARC, but let the build follow the experiment"

Add the EC2 label to `meta_only_runners` in `.github/arc.yaml`. No workflow changes
needed. Already done for `linux.aws.h100`, `linux.aws.h100.4`, `linux.aws.h100.8`,
`linux.dgx.b200`.

### "I want this build job to always go to Meta ARC (skip the determinator)"

Hardcode `runner_prefix: "mt-"` on the build job. Skips the experiment system for
that specific job. Already done for B200 build paths in
`attention_op_microbenchmark.yml` and `operator_microbenchmark.yml`.

### "I want to know what label my job will actually run on"

Trace through:
1. Read the workflow YAML — find the `get-label-type` job and the build job.
2. Note the `check_experiments` / `opt_out_experiments` settings.
3. Pull the current test-infra #5132 config (first comment) to see what the
   experiments are currently rolling.
4. Apply the prefix to the bare `runner` label.
5. If `use-arc=true`, translate via `arc.yaml`.
6. If the bare label is in `meta_only_runners`, the result is `mt-<arc-label>`
   regardless of step 4.

### "I added a new EC2 runner type — what do I need to update?"

If it will EVER be used with `use-arc: true`: add the EC2 -> ARC mapping to
`runner_mapping` in `.github/arc.yaml`. If it's a passthrough (not OSDC-managed),
add an identity mapping (`linux.foo: linux.foo`). Otherwise the script errors out
with `error: no ARC runner found for '<label>'`.

If it's a new H100/B200 variant: also add the EC2 label to `meta_only_runners`.

### "How do I add a new experiment?"

The experiment is defined entirely in the test-infra #5132 issue body. No code
change to `runner_determinator.py` is needed unless the experiment requires a
special handling (like `arc` returning `use_arc=True`, or `amd-do` being exposed
separately). Workflows opt in via `check_experiments: <name>` on their
`get-label-type` job.

## Common Failure Modes

| Symptom | Likely cause |
|---------|-------------|
| Job stuck queued on `lf-l-...-h100` or `lf-l-...-b200` | H100/B200 not in `meta_only_runners`. Add it. |
| `map_ec2_to_arc.py` error `error: no ARC runner found for '<label>'` | Missing entry in `runner_mapping` in `.github/arc.yaml`. Add a mapping (or an identity passthrough if not OSDC-managed). |
| Job lands on `linux.aws.h100` (EC2) instead of `mt-l-x86iamx-22-225-h100` (ARC) | The build job didn't pass `use-arc: true` to the test job, so the test-matrix wasn't translated. Check the `use-arc:` line on the test job. |
| Job lands on the default fleet (no prefix) despite `check_experiments: arc,lf` | Either the user is opted out via `#@user` in test-infra #5132, the PR has the `no-runner-experiments` label, or both experiments are below their rollout %. Set the user to 100% opt-in to debug. |
| Workflow newly inherits `lf-` prefix after PR #186663 | Expected: `arc,lf` enabled together now yields `lf-` (was `mt-` before). If LF doesn't carry that hardware, opt out of LF or add the bare label to `meta_only_runners`. |
| New runner added to a test-matrix breaks the build job | Probably missing from `arc.yaml`. The script's per-entry lookup is strict — there is no implicit passthrough for unknown labels. |
| Onnx tests get silently dropped on the ARC path | Intentional. `map_ec2_to_arc.py` excludes `config: onnx` because onnxruntime's `hardware_concurrency()` sees all host CPUs on ARC k8s instead of the container cpuset. See the comment in the script. |
| Determinator script fails or times out | The wrapper catches the exception and falls back to Meta runners (empty prefix) + no experiments. Look in the `get-label-type` step logs for the actual error. |
| `check_experiments` typo / unknown experiment name | The experiment is silently ignored (not in the eligible set). Check spelling against the keys in the test-infra #5132 settings YAML. |
| `--workflow-name` not recognized by an older script | `_runner-determinator.yml` probes `--help` and skips the flag if the checked-out script (from PR head) doesn't support it. Workflow allowlist filtering won't run. Rebase the PR onto main. |

## Things to NEVER Do

- **NEVER** invent a new ARC label format — use the existing `{os}-[b]{arch}{vendor}{features}-{vcpu}-{memory}[-{gpu_type}[-{gpu_count}]]` scheme in `arc.yaml`. The convention is reviewed across CI infra.
- **NEVER** add an EC2 label to `meta_only_runners` without first confirming it has a `runner_mapping` entry. The script will error.
- **NEVER** remove `use_arc=True` from the ARC branch in `runner_determinator.py` — it's load-bearing for routing the test job to the `test-osdc` branch in `_linux-test.yml`.
- **NEVER** hardcode an ARC label (e.g. `l-x86iamx-22-225-h100`) in a workflow's `test-matrix` `runner:` field. Use the EC2 label and let `map_ec2_to_arc.py` translate. `map_ec2_to_arc.py` does not recognize ARC labels as inputs.
- **NEVER** add the `lf-` prefix or `mt-` prefix directly to `runner_mapping` entry values. The prefix is added separately by the script.
- **NEVER** edit `.ci/docker/` to change runner behavior — that's a Docker image rebuild trigger, unrelated to runner routing.

## Key Cross-References

- `pytorch/test-infra#5132` — first comment is the live rollout config. Edit there to roll experiments, not in code.
- [PR #186663](https://github.com/pytorch/pytorch/pull/186663) — changes ARC+LF combination to return `"lf-"` instead of `"mt-"`. Read this when debugging why a workflow's prefix flipped after merging.
- `docs/runner_naming_convention.md` (in `~/meta/ci-infra/osdc/`) — the source of truth for the ARC label naming scheme. The `osdc-runners-nodepools` skill covers the runner definition side (what runners exist, what hardware they map to).
- `~/meta/ci-infra/osdc/modules/arc-runners/defs/*.yaml`, `modules/arc-runners-h100/defs/`, `modules/arc-runners-b200/defs/` — actual ARC runner scale set definitions. Each `runner.name` field there is what appears as the bare label in `arc.yaml`'s mapping target.

## Verification Commands

```bash
# See the current test-infra #5132 config
gh issue view 5132 --repo pytorch/test-infra --json comments --jq '.comments[0].body'

# Run the determinator locally (read-only, hits the GitHub API)
cd ~/meta/pytorch
python .github/scripts/runner_determinator.py --help

# Run the ARC mapping for a test-matrix
cd ~/meta/pytorch/.github/scripts
python map_ec2_to_arc.py --prefix "lf-" '{include:[{config:"x",runner:"linux.aws.h100"}]}'
# Expected: runner becomes "mt-l-x86iamx-22-225-h100" (meta_only override forces mt-)

# Run the test suites
python .github/scripts/test_runner_determinator.py
python .github/scripts/test_map_ec2_to_arc.py
```
