# CI Hours — Runner Categories

Reference for `ci_hours.json`. Explains how each CI job in `default.workflow_job`
(ClickHouse) is mapped to exactly one runner category for the "Monthly CI Hours"
dashboard.

**Scope:** the `pytorch` and `meta-pytorch` GitHub orgs. **CI-hours** = execution wall-clock,
`sum(dateDiff('second', started_at, completed_at))/3600` over completed jobs (includes
failed/cancelled jobs' runtime — correct for cost; excludes queue time and never-started
jobs).

## How a job is categorized

Categorization is **label-based**: a `multiIf` over the job's `labels[]` array (plus
`runner_group_name`, only for GitHub-hosted). It is **priority-ordered — first match
wins**. Order matters:

- **GitHub-hosted is checked first** so GitHub-provided macOS/Windows count as
  GitHub-hosted, not MacOS/Windows (MacOS/Windows then mean the Meta/LF self-hosted
  fleets only).
- **H100 and B200/H200 are checked before Linux autoscaled** so those GPU labels
  (which otherwise match the generic OSDC/Linux patterns) are carved out as static.

## Category summary

| # | Category | Bucket | What it is | ~Jul 2026 |
|---|----------|--------|------------|----------:|
| 1 | GitHub-hosted | 3rd-party | GitHub-provided hosted runners (GitHub bills these) | 35,705 h (1.6%) |
| 2 | MacOS | static | Meta self-hosted Mac fleet | 49,979 h (2.2%) |
| 3 | H100 | static | H100 GPU compute, any provider (Meta prepaid + GCP) | 9,241 h (0.4%) |
| 4 | B200/H200 | static | Meta DGX B200 / H200 GPU (prepaid capacity) | 7,423 h (0.3%) |
| 5 | Windows | dynamic | Meta + LF self-hosted Windows | 105,131 h (4.7%) |
| 6 | Linux autoscaled | dynamic | old Lambda + OSDC + Meta + LF autoscaled Linux | 1,783,186 h (79.4%) |
| 7 | Partner (donated) | 3rd-party | partner-donated hardware (AMD/Intel/Google/IBM) | 254,189 h (11.3%) |

Super-buckets: **Static** = {MacOS, H100, B200/H200}; **Dynamic** = {Windows, Linux
autoscaled}; **3rd-party** = {GitHub-hosted, Partner (donated)}. Total (Jul 2026,
both orgs, staging excluded) ≈ **2,244,854 h**.

> Note: category #7 is named **"Partner (donated)"** (the fine-grained partner-only
> leftover). The coarse rollup bucket is called **"3rd-party"** and includes both
> GitHub-hosted and Partner (donated) — the two are different scopes; don't conflate them.

## Per-category label expressions

Each row below is one branch of the `multiIf`, in priority order. `x` iterates the
`labels[]` array via `arrayExists(x -> …, labels)`.

### 1. GitHub-hosted
```
runner_group_name = 'GitHub Actions'
  OR arrayExists(x ->
       position(x,'ubuntu') > 0
       OR (x LIKE 'macos%' AND NOT x LIKE 'macos-m%')
       OR x LIKE 'windows-%'
       OR x LIKE 'windows.2022%'
       OR position(x,'-core-windows') > 0, labels)
```
GitHub-provided runners. `runner_group_name = 'GitHub Actions'` is the primary signal;
the label patterns catch hosted runners that register under other groups.
Examples: `ubuntu-22.04`, `ubuntu-latest`, `8-core-ubuntu`, `macos-14-xlarge`,
`macos-latest`, `windows-latest`, `windows-11-arm64-preview`, `windows.2022.small`.

### 2. MacOS (static)
```
arrayExists(x -> x LIKE 'macos-m%', labels)
```
Meta self-hosted Apple-silicon Mac fleet. Examples: `macos-m1-14`, `macos-m1-stable`,
`macos-m2-15`, `macos-m2-16`, `macos-m2-26`, `macos-m2-stable`.
(GitHub-hosted `macos-latest` / `macos-14-xlarge` are caught earlier as GitHub-hosted.)

### 3. H100 (static)
```
arrayExists(x -> position(x,'h100') > 0, labels)
```
Any label containing `h100`, from **any provider** (per decision, H100 = all H100 GPU
compute, not only Meta's). Examples: `mt-l-x86iamx-22-225-h100`,
`mt-l-x86iamx-88-900-h100-4`, `mt-l-bx86iamx-176-1800-h100-8`, `linux.aws.h100`,
`gcp-h100-runner` (GCP-hosted, meta-pytorch).

### 4. B200/H200 (static)
```
arrayExists(x -> position(x,'b200') > 0 OR position(x,'h200') > 0, labels)
```
Any label containing `b200` or `h200`. Examples: `linux.dgx.b200`, `mt-l-*-b200*`.
(H200 has no volume yet; the pattern is future-proofing.)

### 5. Windows (dynamic)
```
arrayExists(x -> x LIKE 'windows.%' OR x LIKE 'lf.windows.%'
                 OR x LIKE 'mt-windows.%' OR x LIKE 'lf-windows.%', labels)
```
Meta + LF self-hosted Windows (dotted and hyphen prefix forms). Examples:
`windows.4xlarge`, `windows.12xlarge`, `windows.g5.4xlarge.nvidia.gpu`,
`lf.windows.12xlarge`, `mt-windows.12xlarge`, `lf-windows.4xlarge`.
(GitHub-hosted `windows-latest` etc. are caught earlier as GitHub-hosted.)

### 6. Linux autoscaled (dynamic)
```
arrayExists(x ->
  ( x LIKE 'linux.%' OR x LIKE 'lf.linux.%' OR x LIKE 'mt-linux.%' OR x LIKE 'lf-linux.%'
    OR match(x, '^(mt|lf)-(rel-)?l-')
    OR x LIKE 'lf.l-%'
    OR match(x, '^(rel-)?l-')
    OR x LIKE 'arc.%' OR x LIKE 'lf.arc.%' OR x LIKE 'mt.arc.%' )
  AND NOT position(x,'rocm')>0 AND NOT position(x,'xpu')>0 AND NOT position(x,'s390x')>0
  AND NOT position(x,'gaudi')>0 AND NOT position(x,'hpu')>0 AND NOT position(x,'google')>0
  AND NOT position(x,'tpu')>0, labels)
```
All Linux we autoscale — the **union of the old Lambda fleet and the new OSDC fleet**,
for both Meta and Linux Foundation — EXCEPT partner accelerators (the `NOT` guards) and
H100/B200/H200 (carved out above). This is an **explicit positive match**: an unknown
future label falls through to `Partner (donated)` rather than silently inflating "our"
hours. Examples:
- old Lambda: `linux.2xlarge`, `linux.g5.4xlarge.nvidia.gpu`, `linux.arm64.2xlarge`
- OSDC (ARC): `mt-l-x86iavx512-8-64`, `lf-l-x86iavx512-16-128`, `mt-rel-l-*`, bare `l-x86iavx512-8-64`, `lf.l-x86iavx512-8-64`, `arc.l-*`, `arc.linux.c7i.2xlarge`
- hyphen/dot Meta+LF: `mt-linux.c7i.2xlarge`, `lf-linux.c7i.2xlarge`

### 7. Partner (donated) — the `multiIf` else branch
```
(everything not matched above)
```
Partner-donated hardware that we don't pay for and don't autoscale. In practice ~100%
partner GPUs. Examples: `linux.rocm.gpu.gfx950.1` (AMD), `linux.idc.xpu` (Intel),
`linux.s390x` (IBM), `linux.hpu.gaudi3.8` (Intel Gaudi), `linux.google.tpuv7x.1`
(Google TPU), `amd-sandbox-*`, `linux-mi355-*`. No-label jobs also land here by
definition (they contribute ~0 hours).

## Global filters (applied before categorization)

Every panel's query applies these:

| Filter | Expression |
|--------|------------|
| Org scope | `url LIKE 'https://api.github.com/repos/pytorch/%' OR url LIKE 'https://api.github.com/repos/meta-pytorch/%'` |
| Completed only | `status = 'completed' AND completed_at >= started_at` |
| Duration sanity | `started_at > toDateTime64('2020-01-01', 9)` (guards epoch/phantom rows) |
| Exclude staging | `NOT arrayExists(x -> x LIKE 'c-mt-%' OR x LIKE 'c-lf-%' OR x LIKE 'c.%' OR x LIKE 'lf.c.%' OR position(x,'canary')>0, labels)` |
| Time window | on `started_at` (minmax skip index → pruning); bucketed by `toStartOfMonth(started_at)` |

**No dedup.** The `LIMIT 1 BY id` ReplacingMergeTree dedup was intentionally dropped:
it drove ~5 GB/panel memory for a ~0.0001% accuracy difference. On this monthly cost
dashboard the tradeoff isn't worth it (query is ~4 s / 0.3 GB without it).

## Label prefix glossary

| Token | Meaning |
|-------|---------|
| `linux.` / `windows.` / `macos-` | old Lambda autoscaler (dot forms) / static Mac |
| `mt-` | Meta (provider/funder) |
| `lf-` / `lf.` | Linux Foundation |
| `l-` | OSDC Linux def name (`l` = Linux); `rel-` = release runner class |
| `arc.` | actions-runner-controller (OSDC) |
| `c-` / `c.` / `*canary*` | canary / staging (excluded) |
| `-h100` / `-b200` / `-h200` | GPU type token (carved out as static) |
| `rocm`/`xpu`/`s390x`/`gaudi`/`hpu`/`google`/`tpu` | partner accelerators → Partner (donated) |

## Dashboard layout

- Default time range: **1 year, month-aligned** (`now-1y/M` → `now`).
- **No stat panels.** Three data panels, **each in its own default-collapsed row**
  (`RowsLayout` + `collapse: true`): "Monthly CI Hours by Category", "Monthly CI Hours
  by Bucket", "CI Hours by Category" (table).
- **Lazy-load:** collapsed-row panels do not run their queries until the row is
  expanded, so opening the dashboard fires zero ClickHouse queries — the heavy >1y
  query runs only on demand.

## Maintenance notes

- **Partner-exclusion keyword list** (`rocm/xpu/s390x/gaudi/hpu/google/tpu`) must be
  kept in sync: a newly donated accelerator that carries a `linux.*`-style label but no
  matching keyword would be miscounted as Linux autoscaled. Add new partner tokens here
  and to every categorized panel.
- **`runner_group_name` is empty** for the self-hosted `mt-*`/`lf-*`/`l-*`/`arc.*`
  fleets — do not rely on it for classification (it's populated for partner HW and
  GitHub Actions only). Classification rests on the label-prefix convention above.
- **Category expression is repeated across 3 panels** (by-category, by-bucket, table).
  Grafana JSON has no cross-panel SQL sharing — if categories change, edit all three in
  lockstep and keep the by-bucket rollup consistent.
