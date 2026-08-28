---
name: pytorch-runners-routing
description: >
  How PyTorch CI jobs in .github/workflows/ resolve to a specific runner, AND which
  infrastructure that runner lives on: the two infras (legacy ALI Lambda-autoscaled EC2
  vs current OSDC ARC-on-EKS) and the dot-vs-dash prefix split (linux./lf./c. = old EC2;
  mt-/lf-/c-mt- = OSDC ARC). Covers the runner determinator (fleet prefixes mt- default /
  lf- / c-mt- plus the separate amd-do-), the test-infra #5132 experiment config (lf,
  amd-do), the now-unconditional EC2->ARC label translation via .github/arc.yaml and
  map_ec2_to_arc.py (the meta_only_runners H100/B200 override and the onnx exclusion),
  build-vs-test runner decoupling, and the _runner-determinator.yml / _linux-build.yml /
  _linux-test.yml flow. Old-infra ownership spans test-infra/terraform-aws-github-runner,
  test-infra scale-config.yml / lf-scale-config.yml, and pytorch-gha-infra/runners; OSDC
  ownership lives in the osdc repo (clusters.yaml + modules/arc-runners*). Applies to
  ~/meta/pytorch. LOAD THIS SKILL for ANY question about PyTorch CI runners, runner
  labels, test matrices, runs-on values, runner prefixes (mt-, lf-, c-mt-, amd-do-, or
  legacy linux./lf./c.), WHICH infra a label runs on ("does lf.linux.2xlarge run on ARC
  or EC2?", "mt- vs lf- vs lf.?", "old ALI vs OSDC runners", "where is this runner
  defined?"), experiments (lf / amd-do), the determinator, ARC fleet routing, H100/B200/
  A100 placement, or how a job gets assigned to a runner pool.
---

# PyTorch CI Runner Routing & Resolution

## Scope

Everything about how `.github/workflows/*.yml` jobs in pytorch/pytorch land on a
specific runner — label definition, which infrastructure serves it, fleet-prefix
resolution, the experiment system, and the EC2-to-ARC translation pipeline. Read this
BEFORE touching any `runs-on:` value, any `test-matrix:` entry, any `runner_prefix:`
interpolation, any `check_experiments:` / `opt_out_experiments:` input, `arc.yaml`, or
`runner_determinator.py` / `map_ec2_to_arc.py`.

For the legacy EC2 world in depth (the Lambda autoscaler, scale-config, dot prefixes,
who owns what), see [`references/old-ali-infra.md`](references/old-ali-infra.md).

## The Two Runner Infrastructures

Two completely different infrastructures serve PyTorch CI. Which one a job lands on is
encoded in its runner label's prefix.

- **Old ALI — Lambda-autoscaled EC2** (legacy). A `workflow_job` webhook drives a
  scale-up Lambda that launches ONE ephemeral EC2 instance per job; the runner
  self-registers with an exact-match label set from `scale-config.yml`, runs the job,
  then is torn down. Owned by `test-infra/terraform-aws-github-runner` (the module),
  `test-infra:.github/scale-config.yml` + `lf-scale-config.yml` (labels), and
  `pytorch-gha-infra/runners` (Meta deploy). **Being decommissioned**; still serves
  passthrough/identity labels. Depth in `references/old-ali-infra.md`.
- **OSDC — ARC on EKS** (current default). Actions Runner Controller schedules the
  runner as a POD on a pre-existing EKS nodepool — no per-job VM boot. Owned by the
  osdc repo: `clusters.yaml` sets each cluster's `runner_name_prefix`, runner defs live
  in `modules/arc-runners*/defs/`. Essentially every routed job lands here now.

**The prefix tells you the infra.** Dot/bare prefixes = old ALI EC2; dash prefixes =
OSDC ARC. The dot->dash shift *is* the ALI->OSDC migration (completed in pytorch/pytorch
PR #189219, which also removed the `arc` experiment and the old `use-arc` output).

### Master prefix -> infrastructure map

| Prefix in `runs-on` | Style | Infrastructure | Operator / funding | Defined / managed in |
|---|---|---|---|---|
| `mt-` | dash | **OSDC — ARC on EKS** | Meta | osdc `clusters.yaml` `meta-prod-aws-uw1/ue1/ue2`; defs `modules/arc-runners*/defs/`. Determinator **default** (`META_LABEL_PREFIX`); also the error fallback. |
| `c-mt-` | dash | OSDC — ARC on EKS | Meta (staging/canary) | osdc `meta-staging-aws-*`; `META_CANARY_LABEL_PREFIX`, emitted only on the `pytorch/pytorch-canary` repo. |
| `lf-` | dash | **OSDC — ARC on EKS** | Linux Foundation | osdc `clusters.yaml` `lf-prod-aws-ue1/ue2`; `LF_LABEL_PREFIX`, emitted when the `lf` experiment is on. Same `github.com/pytorch` org — distinguished only by the scale-set-name prefix. |
| `amd-do-` | dash | Partner ROCm (AMD dedicated) | AMD | separate `amd-do-label-type` output; identity-mapped in `arc.yaml`. |
| `linux.` and other bare | bare | **Old ALI — EC2** | Meta (`gh-ci`, `AWS_PROFILE=fbossci`) | `test-infra:.github/scale-config.yml`, deployed by `pytorch-gha-infra`. **Legacy** — determinator no longer emits it. `map_ec2_to_arc.py` translates ordinary bare `linux.*` -> `mt-…` (OSDC ARC); only `arc.yaml` identity/passthrough entries stay bare and land on literal old-infra/partner runners. |
| `lf.` | dot | Old ALI — EC2 | Linux Foundation (parallel deploy) | `test-infra:.github/lf-scale-config.yml`. **Dead** in the determinator. |
| `c.` / `lf.c.` | dot | Old ALI — EC2 canary | Meta / LF | `test-infra` generated `canary-scale-config.yml` / `lf-canary-scale-config.yml` (prefix-substituted from `scale-config.yml` by `validate_scale_config.py --generate`). |
| identity passthrough (`linux.rocm.gpu.*`, `linux.idc.xpu`, `linux.client.xpu`, `linux.google.tpuv7x.1`, `linux.dgx.b200.8`) | bare | Literal self-hosted (old-infra / partner HW) | `arc.yaml` identity entries — no prefix; land on a runner registering that exact string. |

## Mental Model: Bare Label + Fleet Prefix + Unconditional Translation

For the Linux build/test path, the `runs-on:` value is **assembled at runtime** from:

1. **Bare EC2-style label** in the workflow YAML (`linux.c7i.2xlarge`, `linux.aws.h100`)
   — what a human writes in the `test-matrix`.
2. **Fleet prefix** from the determinator's `label-type` output: `mt-` (default, Meta
   OSDC), `lf-` (LF OSDC, when the `lf` experiment is on), or `c-mt-` (canary repo).
   `amd-do-` is exposed *separately* via `amd-do-label-type` and wired per-job.
3. **`arc.yaml` translation** via `map_ec2_to_arc.py` — runs **unconditionally** on every
   test-matrix, rewriting each EC2 label to its ARC equivalent (or leaving identity-
   passthrough labels bare), and forcing `mt-` for `meta_only_runners`. There is no
   `use-arc` toggle anymore; ARC is the destination for everything the determinator routes.

Examples: `linux.c7i.2xlarge` -> `mt-l-x86iavx512-8-64` (default) or
`lf-l-x86iavx512-8-64` (`lf` on). `linux.aws.h100` -> `mt-l-x86iamx-22-225-h100`
regardless of prefix (H100/B200 pinned to Meta via `meta_only_runners`, see
[Meta-Only Override for H100/B200](#meta-only-override-for-h100b200)).
`linux.dgx.b200.8` stays `linux.dgx.b200.8` (identity passthrough — old runner until
OSDC has capacity).

## The 5 Files That Define Runner Routing

| File | Purpose |
|------|---------|
| `.github/workflows/_runner-determinator.yml` | Reusable workflow. Caller passes `check_experiments` / `opt_out_experiments`. Emits `label-type` (the fleet prefix), `amd-do-label-type`, `runner-config`, `runner-type`, `runner-label`, `ci-docker-hash`. |
| `.github/scripts/runner_determinator.py` | The script. Fetches the rollout config from `pytorch/test-infra#5132` (first comment), evaluates per-user opt-in/out + per-workflow allowlist + rollout %, and emits the fleet prefix. |
| `.github/scripts/test_runner_determinator.py` | Tests for the determinator. Run on PR changes to the script. |
| `.github/arc.yaml` | EC2 -> ARC label mapping (`runner_mapping`) AND `meta_only_runners` override list for H100/B200. |
| `.github/scripts/map_ec2_to_arc.py` + `test_map_ec2_to_arc.py` | Script that rewrites a test-matrix's `runner:` field from EC2 labels to ARC labels using `arc.yaml`. Invoked unconditionally from `_linux-build.yml`. |

## The Two Reusable Workflows That Consume the Determinator Output

| File | Role |
|------|------|
| `.github/workflows/_linux-build.yml` | Build job. `runs-on: ${{ inputs.runner_prefix }}${{ startsWith(inputs.runner, 'l-') && inputs.runner || contains(inputs.runner, 'arm64') && 'l-arm64g4-16-62' || 'l-x86iavx512-8-64' }}` — an inline ternary: the build lands on a fixed small ARC CPU (x86 AVX-512 8/64, or arm64 g4 16/62), unless `inputs.runner` is already an `l-`-prefixed ARC label (then it passes through). Then it runs `map_ec2_to_arc.py --prefix "${RUNNER_PREFIX}"` over the test-matrix (via `uv run` so pyyaml is present even in build images that lack it) and re-emits it as the build job's `test-matrix` output. That translated matrix is what the test job consumes. |
| `.github/workflows/_linux-test.yml` | Test job. A single job — `runs-on: ${{ matrix.runner }}` from the already-translated test-matrix, always in a container with `options: "--gpus all"`, always assuming ARC IAM role `arn:aws:iam::308535385114:role/arc`. There is no longer a `test` vs `test-osdc` branch and no `use-arc` gate; the ARC path is unconditional. (`setup-linux` is called with a hardcoded `use-arc: true` input — that is an action input meaning "we are on ARC", unrelated to the deleted determinator output.) |

Sibling workflows exist for other platforms (`_mac-build.yml`, `_mac-test.yml`,
`_win-build.yml`, `_win-test.yml`, `_rocm-test.yml`, `_xpu-test.yml`, `_vllm-build.yml`,
`_vllm-benchmark.yml`, `_linux-test-stable-fa3.yml`). Not all participate in the
determinator/ARC flow — Linux build + test are the primary path.

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
      check_experiments: lf       # non-default experiments to consider for THIS workflow (optional)
      # opt_out_experiments: lf   # experiments to explicitly skip for THIS workflow (optional)

  some-build:
    uses: ./.github/workflows/_linux-build.yml
    needs: get-label-type
    with:
      runner_prefix: "${{ needs.get-label-type.outputs.label-type }}"   # fleet prefix
      ci-docker-hash: ${{ needs.get-label-type.outputs.ci-docker-hash }} # image tag suffix
      runner: linux.c7i.2xlarge                                          # base build label
      test-matrix: |
        { include: [
          { config: "default", shard: 1, num_shards: 1, runner: "linux.aws.h100" },
        ]}
```

The default fleet prefix is `mt-` (Meta OSDC) — the determinator never emits an empty
prefix. The build translates the test-matrix on every run and re-emits it; the test job
consumes `needs.<build>.outputs.test-matrix`, so `matrix.runner` is already the
translated ARC label. There is no `use-arc:` input to forward.

## Experiment System

### Config Source

The rollout config lives in the **first comment** of GitHub issue
`pytorch/test-infra#5132`. The script fetches it at runtime via the GitHub API.
The issue body has two `---`-separated sections:

1. **Settings YAML** — defines available experiments and their rollout %.
2. **User opt-in list** — `@username,experiment[:percent],-experiment_to_opt_out`.

The issue number is overridable via the `issue_number` input on
`_runner-determinator.yml` (default `"5132"`).

The live experiments are **`lf`** (switches the fleet to Linux Foundation OSDC) and
**`amd-do`** (routes ROCm mi350 tests to AMD's dedicated runners, exposed via the
separate `amd-do-label-type` output). Any other experiment name is still parsed, but if
it is neither `lf` nor `amd-do` the determinator logs "enabled but no longer affects the
runner label prefix; ignoring" and it changes nothing — there is no `arc` experiment
anymore.

### Experiment Settings

```yaml
experiments:
  lf:
    rollout_perc: 25
    all_branches: false
    default: true
```

**Field-name caveat**: the working field is `rollout_perc` (the `Experiment` NamedTuple
field). The docstring example inside `runner_determinator.py` writes `rollout_percent` —
a typo. `parse_settings_from_text` logs any unknown key as an "Unexpected setting" and
never applies it, so a config copied from that docstring gets `rollout_perc = 0` (no
percentage rollout).

Per-experiment fields (`Experiment` NamedTuple in `runner_determinator.py`):

- `rollout_perc: float` — % of workflows that get this experiment when no user opted in.
- `all_branches: bool` — if False, exception branches (`main`, `nightly`, `release`,
  `landchecks`) skip the experiment.
- `default: bool` — if False, the experiment only runs when the caller passes it in
  `check_experiments`.
- `workflows: str` — comma-separated allowlist of `github.workflow` names. `"ALL"` or
  empty = every workflow. `"-Name"` prefix excludes that workflow even when `"ALL"` is
  present. Exclusions win over inclusions.

### User Opt-in / Opt-out

After the `---` separator: each line `@user,experiment1,experiment2:N,-experiment3`:

- Plain entry -> opt-in 100%.
- `experiment:N` -> per-user N% rollout (0-100).
- `-experiment` -> explicit opt-out.
- `#@user,...` -> user opts out of ALL experiments.
- Triggering actor and PR author are both checked (the minimum per-user % among
  opted-in requestors wins, so a conservative author % is respected).

### Per-Workflow Opt-In / Opt-Out (in the workflow YAML)

| Input | Effect |
|-------|--------|
| `check_experiments: lf` | Only consider these experiments (overrides `default: true` for non-listed ones — they will NOT run unless listed). |
| `check_experiments` unset | Use each experiment's `default:` flag from the config (so `lf` is eligible by default). |
| `opt_out_experiments: lf` | Explicitly skip the named experiment regardless of `default:` / `check_experiments`. Higher priority than `check_experiments`. |

### PR-Level Kill Switch

Apply the `no-runner-experiments` label on a PR -> the determinator adds `lf` to the
opt-outs, so the run stays on the **default Meta fleet (`mt-`)** rather than LF. See
`OPT_OUT_LABEL` in `runner_determinator.py`.

## Fleet Prefixes the Determinator Emits

| Condition | `label-type` output | Fleet / infra | Example test label (`linux.c7i.2xlarge`) |
|---|---|---|---|
| Default (no `lf`) | `mt-` (`META_LABEL_PREFIX`) | Meta OSDC ARC | `mt-l-x86iavx512-8-64` |
| `lf` experiment on | `lf-` (`LF_LABEL_PREFIX`) | LF OSDC ARC | `lf-l-x86iavx512-8-64` |
| Canary repo (`pytorch/pytorch-canary`), no `lf` | `c-mt-` (`META_CANARY_LABEL_PREFIX`) | Meta OSDC ARC (staging) | `c-mt-l-x86iavx512-8-64` |
| Determinator error / timeout | `mt-` (fallback) | Meta OSDC ARC | — |
| PR has `no-runner-experiments` | `mt-` (opts out of `lf`) | Meta OSDC ARC | — |

Notes:

- The determinator can **NEVER** emit `""` (bare/EC2) or `lf.` (dotted). Those are
  old-ALI prefixes — see `references/old-ali-infra.md`.
- `lf` takes precedence over canary: on the canary repo with `lf` on, you get `lf-`,
  not `c-mt-`.
- `amd-do` is exposed via its own `amd-do-label-type` output (`"amd-do-"` when enabled,
  else `""`), NOT folded into `label-type`. Consumers wire it per-job.

## EC2 -> ARC Label Translation

The translation happens in the **build job**, unconditionally, so the test job receives
ARC fleet labels instead of EC2 labels:

1. `_linux-build.yml` runs `map_ec2_to_arc.py --prefix "${RUNNER_PREFIX}" "${FILTERED_TEST_MATRIX}"`.
2. The script:
   - Loads `runner_mapping` and `meta_only_runners` from `.github/arc.yaml`.
   - For each `include[].runner`: strips the prefix, looks the bare label up in
     `runner_mapping`, then re-prepends the prefix (with the passthrough and meta-only
     rules below).
   - Drops entries whose `config` is in `excluded_configs` (currently `{"onnx"}` — see
     [Common Failure Modes](#common-failure-modes)).
3. The translated test-matrix becomes the build job's `test-matrix` output.
4. The test job consumes `needs.<build>.outputs.test-matrix`, so `matrix.runner` is the
   already-translated ARC label.

### `arc.yaml` — The Mapping Table

```yaml
runner_mapping:
  linux.c7i.2xlarge: l-x86iavx512-8-64
  linux.12xlarge.memory: l-x86iavx512-48-384
  linux.aws.h100: l-x86iamx-22-225-h100
  linux.aws.h100.4: l-x86iamx-88-900-h100-4
  linux.aws.h100.8: l-bx86iamx-176-1800-h100-8
  linux.dgx.b200: l-x86iamx-22-225-b200
  # ... CPU, A100, H100, B200, A10G, T4, L4, ARM64
  linux.dgx.b200.8: linux.dgx.b200.8                   # passthrough — identity mapping
  linux.rocm.gpu.2: linux.rocm.gpu.2                   # passthrough
  linux.idc.xpu: linux.idc.xpu                         # passthrough
```

**Passthrough rule** (identity mapping in the table): `mapped == clean` means the runner
is not OSDC-managed (ROCm, XPU, TPU, and `linux.dgx.b200.8` until OSDC has 8-GPU B200
capacity). The script keeps the original label WITHOUT prefixing — so it lands on the
existing self-hosted / old-infra runner.

**ARC label naming convention** is documented as a comment at the top of `arc.yaml`:

```
{os}-[b]{arch}{vendor}{features}-{vcpu}-{memory}[-{gpu_type}[-{gpu_count}]]
```

Examples: `l-x86iavx512-8-64` (Linux, x86, Intel AVX-512, 8 vCPU, 64 GiB),
`l-bx86iamx-176-1800-h100-8` (Linux, bare-metal, x86, Intel AMX, 176 vCPU, 1800 GiB,
8x H100).

### Meta-Only Override for H100/B200

H100 and B200 hardware exists only on the Meta OSDC fleet — LF and AWS EC2 don't carry
those machines. Routing an H100/B200 job to `lf-l-...-h100` would queue forever.

The fix: the `meta_only_runners` list in `.github/arc.yaml`:

```yaml
meta_only_runners:
  - linux.aws.h100
  - linux.aws.h100.4
  - linux.aws.h100.8
  - linux.dgx.b200
```

In `map_ec2_to_arc.py`, the per-entry loop has an early branch for any label in this
set: `entry["runner"] = "mt-" + mapped` (forces Meta OSDC, overriding whatever
`--prefix` was passed). This decouples the H100/B200 test runner from the build runner's
fleet — the build CPU is free to land wherever the experiment routes it, but the GPU
test job is always pinned to Meta OSDC.

**Important**: `linux.dgx.b200.8` (8-GPU B200, used by `b200-distributed.yml` and
`b200-symm-mem.yml`) is an **identity passthrough** in `runner_mapping` and is **NOT** in
`meta_only_runners` — OSDC has 8-GPU B200 runners but not enough capacity yet, so tests
stay on the existing `linux.dgx.b200.8` runner while the OSDC build runs. When OSDC gains
capacity, replace the identity mapping with a real ARC label AND add the EC2 label to
`meta_only_runners`.

## Build vs Test Runner Decoupling — Why It Matters

A single `_runner-determinator.yml` call typically feeds the build job's `runner_prefix`
(and its `ci-docker-hash`); the test runners come from the translated test-matrix. There
are four patterns in the wild:

1. **Fully coupled (default)** — `runner_prefix: "${{ ...label-type }}"`. Build CPU and
   the (translatable) test runners travel together through the experiment.
2. **Hardcoded `mt-`** (`runner_prefix: "mt-"`) — pins that build job to Meta OSDC,
   bypassing the determinator's fleet choice. Widely used for B200 builds, all ROCm
   (`rocm-*`, `periodic-rocm-*`), XPU (`xpu.yml`), and several inductor perf builds.
3. **Per-workflow opt-out** (`opt_out_experiments: lf`) — removes `lf` from
   consideration for that workflow. Used across the inductor family
   (`inductor.yml`, `inductor-unittest.yml`, `inductor-periodic.yml`,
   `inductor-nightly.yml`, `inductor-perf-test-*`), `dynamo-unittest.yml`, etc.
4. **`meta_only_runners`** — targets a specific runner *label* (H100/B200) rather than a
   whole workflow. The cleanest tool when "the build can land anywhere but this specific
   test runner must always be Meta OSDC".

## GPU Workflows: Inventory and Mechanics

Workflows touching high-end NVIDIA GPUs (H100, B200, A100). `check_experiments` /
`opt_out_experiments` are re-derived from live source. `(none)` = the `get-label-type`
job passes no experiment inputs, so defaults apply (`lf` eligible unless opted out).

| File | GPU test runner(s) | `check_experiments` | `opt_out_experiments` | Notes |
|------|--------------------|---------------------|-----------------------|-------|
| `test-h100.yml` | `linux.aws.h100` | `lf` | — | H100 forced `mt-` via `meta_only_runners` |
| `h100-cutlass-backend.yml` | `linux.aws.h100` | `lf` | — | |
| `h100-distributed.yml` | `linux.aws.h100.8` | `lf` | — | |
| `h100-symm-mem.yml` | `linux.aws.h100.4` | `lf` | — | |
| `test-b200.yml` | `linux.dgx.b200` | `lf` | — | B200 forced `mt-` via `meta_only_runners` |
| `b200-distributed.yml` | `linux.dgx.b200.8` | (none) | — | build pinned `runner_prefix: "mt-"`; `get-label-type` used only for `ci-docker-hash`; b200.8 test = identity passthrough -> old bare runner |
| `b200-symm-mem.yml` | `linux.dgx.b200.8` | (none) | — | same as `b200-distributed.yml` |
| `inductor-perf-test-nightly-h100.yml` | `linux.aws.h100` | (none) | `lf` | |
| `inductor-perf-test-b200.yml` | `linux.dgx.b200` | (none) | `lf` | build pinned `runner_prefix: "mt-"` |
| `inductor-pallas.yml` | `linux.aws.h100` | (none) | — | |
| `inductor-periodic.yml` | `linux.aws.h100` + many `g5`/`a100` | `amd-do` | `lf` | |
| `attention_op_microbenchmark.yml` | `linux.aws.a100`, `linux.aws.h100`, `linux.dgx.b200` | `lf` | — | B200 build path pins `runner_prefix: "mt-"` |
| `operator_microbenchmark.yml` | `linux.aws.h100`, `linux.aws.a100`, `linux.dgx.b200` | `lf` | — | B200 build path pins `runner_prefix: "mt-"` |
| `operator_microbenchmark_compare.yml` | conditional H100/A100/B200 via `${{ inputs.gpu }}` | (none) | — | B200 path pins `runner_prefix: "mt-"` |
| `vllm-benchmark.yml` | external matrix gen | NO `get-label-type` job | — | does not participate in the determinator |

## Common Runner Label Families

### CPU build runners (translated to the `l-x86i*` / `l-arm64g*` ARC families)

`linux.c7i.2xlarge`, `linux.4xlarge`, `linux.12xlarge`, `linux.12xlarge.memory`,
`linux.r7i.4xlarge`, `linux.24xlarge.memory`, `linux.24xl.spr-metal`, `*.amx`, `*.avx2`,
`*.amd`. The build job itself always lands on a fixed small ARC CPU (see
`_linux-build.yml`); these labels matter for the *test* matrix.

### NVIDIA GPU test runners

| Label | GPU |
|-------|-----|
| `linux.g4dn.4xlarge.nvidia.gpu`, `linux.g4dn.12xlarge.nvidia.gpu`, `linux.g4dn.metal.nvidia.gpu` | T4 |
| `linux.g5.4xlarge.nvidia.gpu`, `linux.g5.12xlarge.nvidia.gpu`, `linux.g5.48xlarge.nvidia.gpu` | A10G |
| `linux.g6.4xlarge.experimental.nvidia.gpu`, `linux.g6.12xlarge.nvidia.gpu` | L4 |
| `linux.aws.a100` | A100 (p4de) |
| `linux.aws.h100`, `linux.aws.h100.4`, `linux.aws.h100.8` | H100 (p5) — Meta-only |
| `linux.dgx.b200` | B200 (p6) — Meta-only |
| `linux.dgx.b200.8` | B200 8-GPU — identity passthrough (old runner) |

### ARM64

`linux.arm64.2xlarge`, `linux.arm64.m7g.4xlarge`, `linux.arm64.m8g.4xlarge`,
`linux.arm64.r7g.12xlarge.memory`, `linux.arm64.m7g.metal`, `linux.arm64.m8g.metal-24xl`.

### Partner hardware (identity passthrough — never prefixed/translated)

`linux.rocm.gpu.2`, `linux.rocm.gpu.mi210.1/.2`, `linux.rocm.gpu.gfx942.1/.4`,
`linux.rocm.gpu.gfx950.1/.2`, `linux.rocm.gpu.gfx1100`, `linux.idc.xpu`,
`linux.client.xpu`, `linux.google.tpuv7x.1`. The `amd-do` experiment routes mi350 tests
through the `amd-do-`-prefixed identity entries (`amd-do-linux.rocm.gpu.gfx950.1/.2`).

## `_runner-determinator.yml` Outputs (Full Reference)

| Output | What it is | Typical consumer |
|--------|-----------|-----------------|
| `label-type` | The fleet prefix (`"mt-"`, `"c-mt-"`, `"lf-"`) | `runner_prefix:` input on `_linux-build.yml` |
| `amd-do-label-type` | `"amd-do-"` if the amd-do experiment is enabled, else `""` | Per-job AMD pinning |
| `ci-docker-hash` | `git rev-parse HEAD:.ci/docker` (the tree hash, from PR head) | `ci-docker-hash:` input on `_linux-build.yml` for the image tag |
| `runner-config` | Normalized runner config (`m7g` / `m8g`) | ARM64 metal runner selection |
| `runner-type` | Runner suffix (`metal` / `metal-24xl`) | ARM64 metal runner selection |
| `runner-label` | Fully qualified ARM label `linux.arm64.<config>.<type>` | Direct `runs-on:` on some ARM workflows |

There is no `use-arc` output (removed with the `arc` experiment in PR #189219).

## Decision Trees / Recipes

### "I want this workflow to never use LF runners"

Add `opt_out_experiments: lf` to the `_runner-determinator.yml` caller. The prefix will
never be `lf-` — it stays `mt-`. Examples: the whole inductor family
(`inductor.yml`, `inductor-unittest.yml`, `inductor-periodic.yml`, `inductor-nightly.yml`,
`inductor-perf-test-*`), `dynamo-unittest.yml`.

### "I want this specific GPU test runner to always go to Meta OSDC, but let the build follow the experiment"

Add the EC2 label to `meta_only_runners` in `.github/arc.yaml`. No workflow changes
needed. Already done for `linux.aws.h100`, `linux.aws.h100.4`, `linux.aws.h100.8`,
`linux.dgx.b200`.

### "I want this build job to always go to Meta OSDC (skip the determinator's fleet choice)"

Hardcode `runner_prefix: "mt-"` on the build job. Skips the fleet experiment for that
job. Widely used for B200 builds, all ROCm, XPU, and some inductor perf builds.

### "I want to know what infra + label my job will actually run on"

Trace through:
1. Read the workflow YAML — find the `get-label-type` job and the build/test jobs.
2. Note `check_experiments` / `opt_out_experiments`.
3. Pull the current test-infra #5132 config (first comment) to see what's rolling.
4. Determine the fleet prefix: `mt-` unless `lf` is enabled (`lf-`) or it's the canary
   repo (`c-mt-`).
5. Apply `map_ec2_to_arc.py` (it runs on EVERY matrix): EC2 label -> ARC label with the
   prefix; identity-passthrough labels stay bare (old infra); `meta_only_runners` force
   `mt-`.
6. Dash prefix -> OSDC ARC; bare/dotted -> old ALI EC2 (see `references/old-ali-infra.md`).

### "I added a new EC2 runner type — what do I need to update?"

Add the EC2 -> ARC mapping to `runner_mapping` in `.github/arc.yaml`. If it's a
passthrough (not OSDC-managed), add an identity mapping (`linux.foo: linux.foo`).
Otherwise `map_ec2_to_arc.py` errors with `error: no ARC runner found for '<label>'` —
the lookup is strict, there is no implicit passthrough. If it's a new H100/B200 variant,
also add the EC2 label to `meta_only_runners`.

### "How do I add a new experiment?"

Define it in the test-infra #5132 issue body. No code change to `runner_determinator.py`
is needed UNLESS the experiment must change the label prefix — the script only special-
cases `lf` (sets the fleet) and `amd-do` (exposed via `amd-do-label-type`). Any other
experiment is parsed but "no longer affects the runner label prefix" and does nothing to
routing. Workflows opt in via `check_experiments: <name>`.

## Common Failure Modes

| Symptom | Likely cause |
|---------|-------------|
| Job stuck queued on `lf-l-...-h100` or `lf-l-...-b200` | H100/B200 not in `meta_only_runners`. Add it. |
| `map_ec2_to_arc.py` error `error: no ARC runner found for '<label>'` | Missing entry in `runner_mapping`. Add a mapping (or an identity passthrough if not OSDC-managed). |
| GPU test lands on a bare/old label instead of `mt-l-...` | The bare label is an identity passthrough in `runner_mapping` (e.g. `linux.dgx.b200.8`) — intentional until OSDC capacity lands. To move it to OSDC, replace the identity mapping and add a `meta_only_runners` entry. |
| Job lands on the default Meta fleet despite expecting `lf-` | User opted out via `#@user` in #5132, PR has `no-runner-experiments` (opts out of `lf`), `lf` is below rollout %, or the workflow sets `opt_out_experiments: lf`. Set the user to 100% opt-in to debug. |
| New runner added to a test-matrix breaks the build's map step | Missing from `arc.yaml`. The per-entry lookup is strict — no implicit passthrough for unknown labels. |
| Onnx tests silently dropped on the ARC path | Intentional. `map_ec2_to_arc.py` excludes `config: onnx` because onnxruntime's `hardware_concurrency()` sees all host CPUs on ARC k8s instead of the container cpuset. See the comment in the script. |
| Determinator script fails or times out | The wrapper falls back to Meta runners (`mt-`) + no experiments. Look in the `get-label-type` step logs for the actual error. |
| `check_experiments` typo / unknown experiment name | Silently ignored. Check spelling against the keys in the #5132 settings YAML (`lf`, `amd-do`). |
| A stale determinator checked out from a PR emits `lf.` | `_runner-determinator.yml` checks the script out from the merge commit (base), not PR head, precisely so a stale prefix like `lf.` can't reach main's build job (which has no EC2 fallback). See the checkout comment citing #189113/#189171. |

## Things to NEVER Do

- **NEVER** invent a new ARC label format — use the existing
  `{os}-[b]{arch}{vendor}{features}-{vcpu}-{memory}[-{gpu_type}[-{gpu_count}]]` scheme in
  `arc.yaml`. The convention is reviewed across CI infra.
- **NEVER** add an EC2 label to `meta_only_runners` without first confirming it has a
  `runner_mapping` entry. The script will error.
- **NEVER** hardcode an ARC label (e.g. `l-x86iamx-22-225-h100`) in a workflow's
  `test-matrix` `runner:` field. Use the EC2 label and let `map_ec2_to_arc.py` translate
  it — the script only recognizes EC2/bare labels as input.
- **NEVER** add the `lf-` / `mt-` prefix directly to `runner_mapping` entry values. The
  prefix is added separately by the script.
- **NEVER** make the determinator emit a bare (`""`) or dotted (`lf.`) prefix — main's
  build/test path has no EC2 fallback, so an old-style prefix breaks routing (see
  #189113/#189171).
- **NEVER** edit `.ci/docker/` to change runner behavior — that's a Docker image rebuild
  trigger, unrelated to runner routing.

## Key Cross-References

- `pytorch/test-infra#5132` — first comment is the live rollout config. Edit there to
  roll experiments, not in code.
- `references/old-ali-infra.md` — the legacy Lambda-autoscaled EC2 world: scale-config,
  dot prefixes, ownership, and how to recognize an old-ALI job today.
- PR #189219 — removed the `arc` experiment and the `use-arc` output; completed the
  dot->dash (ALI->OSDC) migration. Read when confused why old docs mention `use-arc`.
- `docs/runner_naming_convention.md` (in `~/meta/ci-infra/osdc/`) — source of truth for
  the ARC label naming scheme. The `osdc-runners-nodepools` skill covers the runner
  definition side (what runners exist, what hardware they map to).
- `~/meta/ci-infra/osdc/modules/arc-runners*/defs/*.yaml` — the actual ARC runner scale
  set definitions. Each `runner.name` there is what appears as the mapping *target* in
  `arc.yaml`; `runnerScaleSetName = {prefix}{runner.name}` is the literal `runs-on`.

## Verification Commands

```bash
# See the current test-infra #5132 config
gh issue view 5132 --repo pytorch/test-infra --json comments --jq '.comments[0].body'

# Run the determinator locally (read-only, hits the GitHub API)
cd ~/meta/pytorch
python .github/scripts/runner_determinator.py --help

# Run the ARC mapping for a test-matrix (matches the _linux-build.yml uv invocation)
cd ~/meta/pytorch/.github/scripts
uv run --no-project --with pyyaml==6.0.2 \
  python map_ec2_to_arc.py --prefix "lf-" '{ include: [ { config: "x", runner: "linux.aws.h100" } ] }'
# Expected: runner becomes "mt-l-x86iamx-22-225-h100" (meta_only override forces mt-)

# Run the test suites
python .github/scripts/test_runner_determinator.py
python .github/scripts/test_map_ec2_to_arc.py
```
