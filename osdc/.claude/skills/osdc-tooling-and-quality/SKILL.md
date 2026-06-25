---
name: osdc-tooling-and-quality
description: >
  OSDC tools (tofu, just, mise, crane, uv), automation hierarchy, unit tests, code style
  and linting rules (13 linters, indentation), "Don't Do" list, and quality gates.
  Applies to the OSDC project (`osdc/`).
  Load when writing code, running linters, adding scripts/tests, or debugging and trying to understand issues.
---

# OSDC Tooling, Quality & Code Style

## Knowledge Base

The knowledge base for this project (upstream repos, reference docs) is at `~/meta/actions-knowledge-base`.

## NEVER USE TERRAFORM — USE TOFU ONLY

This project uses **OpenTofu** (`tofu`), NOT Terraform. Running `terraform` commands **will corrupt the state file** and can destroy infrastructure. There is no recovery.

- **NEVER** run `terraform init`, `terraform plan`, `terraform apply`, or any `terraform` subcommand
- **ALWAYS** use `tofu` or `just` recipes (which call `tofu` internally)
- Directories are named `terraform/` but the tool is `tofu`
- In `mise.toml`, the entry is `opentofu`, never `terraform`

## Key Tools

Versions are pinned in `osdc/mise.toml` (mise auto-installs on first run):

- **OpenTofu** (`tofu`) **1.7**: Infrastructure as code. See warning above — never use `terraform`.
- **just**: Command runner. Recipes in `osdc/justfile`. Single entry point for all operations.
- **mise**: Tool version manager. Config in `osdc/mise.toml`. Auto-installs tools on first run.
- **Python 3.12**, **kubectl 1.34**, **helm 3.14**, **awscli 2**, **packer 1.10** — pinned.
- **kube-linter v0.7.6** — pinned via the aqua backend (`aqua:stackrox/kube-linter`); rule names and severities shift across versions, so a bump requires re-running `just lint` against the full tree.
- **crane**, **uv**, **ruff** — pinned to `latest`.
- **uv**: Python package manager. Always use `uv`, never pip/conda/poetry.

`actionlint` is also installed via `mise.toml` but is **not** wired into `just lint` (currently unused by the lint pipeline). GitHub Actions workflow YAML in `.github/workflows/` is therefore not linted as part of `just lint` — only ad-hoc invocation of `actionlint` covers it.

## Automation Hierarchy

**Required order of preference for new automation:**

1. **just recipes** — use existing just recipes for all tasks; check justfile first before creating anything
2. **Python scripts** — for new automation requiring logic/complexity
3. **Bash scripts** — ONLY when Python is unsuitable OR trivial (< 20 lines)

**DO NOT create bash scripts if a Python solution is reasonable.** Python provides better error handling, testability, and maintainability.

**ALWAYS use `uv` for Python dependencies** (`uv pip install`, `uv venv`, `uv run`). NEVER use pip, conda, poetry, or other package managers.

## Unit Tests (MANDATORY)

**Every new Python script with testable logic MUST have co-located unit tests.** When adding or modifying functionality in ANY script, you MUST check for existing tests and update them to cover the changes. When adding new functionality, add corresponding tests. No code change is complete without verifying its test coverage.

- **Co-located tests**: Test files live next to the script they test (e.g., `scripts/python/foo.py` -> `scripts/python/test_foo.py`). Test discovery in `just test` is `find`-driven (justfile `test` recipe) — it walks the tree for `test_*.py` and prunes `.scratch`, `.venv`, `tests/e2e`, and `base|modules/*/tests/smoke`. (The `testpaths` list in `pyproject.toml` is only consulted when invoking bare `pytest`, and it notably does NOT include `tests/smoke/` even though `just test` does.)
- **Pure-logic extraction**: Separate testable logic (data transforms, validation, comparisons) from side effects (subprocess calls, kubectl, helm). Test the pure logic; mock the side effects only when necessary.
- **Run before declaring done**: `just test` must pass clean. No skipped tests, no xfails for new code.
- **Coverage gate (95% per file)**: `just test` runs pytest with `-n auto --cov`, then enforces a **per-file coverage threshold of 95%** (hard-coded in the `test` recipe). `pyproject.toml` also sets `[tool.coverage.report] fail_under = 95`. If `just test` fails on otherwise-passing tests, check the per-file coverage report — any file under 95% will block.
- **Coverage expectation**: Test happy paths, edge cases (empty inputs, missing keys), and any cross-module interaction scenarios (e.g., multi-module namespace sharing).
- **Test directory exclusions**: `just test` discovers `test_*.py` files everywhere EXCEPT:
  - `tests/e2e/` (live-cluster end-to-end tests — run via `just test-compactor`, `just test-janitor`)
  - `base/<component>/tests/smoke/` and `modules/<module>/tests/smoke/` (live-cluster smoke tests — run via `just smoke <cluster>`)
  - The project-root `tests/smoke/` IS included — it holds unit tests for the smoke helpers package. If you put unit tests under a module's `tests/smoke/`, they will NOT run in `just test`.
- **Other live-cluster test recipes** (also outside `just test`): `just integration-test <cluster>`, `just load-test <cluster>`, `just workload-test <cluster>` — each drives its own Python entrypoint under `integration-tests/*/scripts/python/`.
- **Coverage exclusion for smoke helpers**: Directories matching `*/tests/smoke` or `*/tests/smoke/*` are excluded from the `--cov=` flags (justfile `test` recipe). Their unit tests run, but the 95% per-file gate doesn't apply to them — full coverage is provided by the live-cluster smoke runs.
- **Coverage `omit`**: `pyproject.toml` `[tool.coverage.run] omit = ["test_*.py", "conftest.py"]` — test files themselves don't count toward coverage of the modules under test.

## Before Declaring Work Complete (MANDATORY)

Before declaring any code change complete, you MUST run both of these and they MUST pass clean:

```bash
just lint    # All 13 linters must pass with zero errors
just test    # All unit tests must pass
```

If either fails, fix the issues before finishing. Do not defer lint or test failures — they block CI and break other contributors.

## Agent-Optimized Output (AGENT_ENVIRONMENT=true)

The `just test`, `just lint`, and `just smoke` recipes detect the `AGENT_ENVIRONMENT=true` env var (set by default in the launcher). When enabled:

- **On success**: output is suppressed to just `OK (<time>)` — no verbose logs, no per-linter/test detail.
- **On failure**: full output is returned as usual — failures, stack traces, linter errors, everything needed to diagnose the problem.

**This keeps agent context clean.** A passing `just lint` returns one line instead of hundreds. Agents should leverage this: run the recipe, check for `OK` — if you get it, move on. No need to capture output to temp files or parse results on success.

## Running Lint, Tests, and Smoke Efficiently (MANDATORY for agents)

**ALWAYS capture full output to a temp file AND tail the end in one command.** This pattern applies to `just lint`, `just test`, and `just smoke` — every time, regardless of expected outcome:

```bash
just lint  2>&1 | tee /tmp/lint-output.txt  | tail -n 10 ; wc -l /tmp/lint-output.txt
just test  2>&1 | tee /tmp/test-output.txt  | tail -n 10 ; wc -l /tmp/test-output.txt
just smoke 2>&1 | tee /tmp/smoke-output.txt | tail -n 10 ; wc -l /tmp/smoke-output.txt
```

**Why this pattern:**
- The `tail -n 10` shows you the result immediately (success `OK` or failure summary) without flooding context.
- The `wc -l` tells you how much output was captured.
- The full output is saved to `/tmp/` — if something fails, **do NOT re-run the command**. Instead, investigate the temp file:

```bash
# Read the full file
cat /tmp/lint-output.txt

# Read specific sections
head -n 50 /tmp/lint-output.txt
tail -n 30 /tmp/test-output.txt

# Search for specific errors
grep -n "ERROR\|FAILED\|error" /tmp/lint-output.txt

# Extract a specific linter's output
awk '/^━━━ shfmt ━━━$/,/^━━━/' /tmp/lint-output.txt
```

**NEVER re-run `just lint`, `just test`, or `just smoke`** just to see what failed. The temp file has everything — read it, grep it, slice it. Re-running wastes time and tokens.

### Investigating Failures

**Lint**: the last lines show which linters failed (`FAILED linters: shfmt, ruff check`). Use `awk` to extract the specific linter section from the temp file.

**Tests**: the last lines show which directories failed (`FAILED test dirs: ...`). For deeper investigation, re-run just that directory:

```bash
cd <failed-dir> && uv run pytest --tb=short -v
```

### Run Targeted Checks When Possible

If you know which files changed, run the specific linter directly. Run from the project root (`osdc/`):

```bash
# Python
uv run ruff check --config ruff.toml path/to/file.py
uv run ruff format --check --config ruff.toml path/to/file.py

# Shell
shellcheck path/to/script.sh
shfmt -d -i 2 -ci -bn path/to/script.sh

# Python tests (specific directory)
cd path/to/test/dir && uv run pytest --tb=short -q

# Terraform
tofu fmt -check -recursive modules/my-module/terraform/

# YAML
uv run yamllint -c .yamllint.yaml path/to/file.yaml
```

## Code Style & Linting

`just lint` runs **13 linters**. `just lint-fix` auto-fixes what it can — in this order: `tofu fmt`, `shfmt`, `ruff` (check `--fix` then format), `taplo fmt`. All must pass — CI blocks on any failure.

### Indentation (most common agent mistake)

| Language | Indent | Tool |
|----------|--------|------|
| Python | 4 spaces | ruff |
| Shell (.sh) | **2 spaces** | shfmt (`-i 2 -ci -bn`) |
| YAML | 2 spaces (sequences indented) | yamllint |
| HCL/Terraform | 2 spaces | tofu fmt |
| TOML | 2 spaces | taplo |
| Dockerfile | 4 spaces | .editorconfig |
| JSON / jsonnet | 2 spaces | .editorconfig |
| justfile | 4 spaces | .editorconfig |
| Makefile | tab | .editorconfig |

### Python (ruff)

- **Line length: 120** (not 80/88)
- **Target: Python 3.12**
- Imports must be sorted (isort rules enabled). `known-first-party = ["osdc"]`
- **Selected rule families** (`ruff.toml` `[lint] select`): `E`, `F`, `W`, `I`, `UP`, `B`, `SIM`, `RUF`, `S`, `T20`, `PIE`, `C4`, `PT`, `RSE`, `RET`, `TCH`, `ARG`, `ERA`
- **Globally ignored**: `E501` (line length is the formatter's job), `S108` (`/tmp` paths), `S603`/`S607` (subprocess calls), `T201` (`print()` allowed), `RET504` (assignment-before-return), `ARG001` (unused function arguments)
- **Test-file ignores** (`**/test_*.py`): `S101` (asserts), `S106` (hardcoded passwords), `ARG` (unused args), `PT009`/`PT019`/`PT027` (unittest-style patterns), `ERA001` (commented-out code in test comments)
- **Tests directories** (`**/tests/**`): same as test files plus `TCH`
- No commented-out code (`ERA001`) — except in test files
- Use comprehensions over `map()`/`filter()` (`C4`)
- Don't use `assert` outside test files (`S101`)

### Shell (shellcheck + shfmt)

- **2-space indent**, case bodies indented (`-ci`), binary ops (`&&`/`||`) start the next line (`-bn`)
- Always quote variables (shellcheck `SC2086`)
- `enable=all` is set in `.shellcheckrc`, with these noisy checks disabled: `SC2250` (prefer `${var}` over `$var`), `SC2292` (prefer `[[ ]]` over `[ ]`), `SC2310` (function in if/&& disables `set -e`), `SC2312` (consider invoking separately for exit code)
- `source-path=SCRIPTDIR` so sourced files resolve relative to the script

### YAML (yamllint)

- 2-space indent, sequences indented too
- Truthy values: only `true`, `false`, `yes`, `no` (not `on`/`off`/`True`/`False`)
- Max line length: 200 (warning only)
- No trailing whitespace, newline at EOF, max 2 consecutive blank lines
- **Scope**: yamllint runs against `base/kubernetes/`, `base/node-compactor/kubernetes/`, `base/helm/`, every `modules/<module>/kubernetes/`, every `modules/<module>/helm/`, every `modules/<module>/defs/` (relevant for runner / nodepool definition YAMLs), and `clusters.yaml`

### Kubernetes (kubeconform + kube-linter)

- Manifests validated against official schemas in strict mode (`-strict -summary -ignore-missing-schemas`); CRDs without published schemas are tolerated via `-ignore-missing-schemas` and a CRDs-catalog schema URL
- Most safety/security checks are active by default. Explicitly excluded in `.kube-linter.yaml`: resource-requirements checks (`unset-cpu-requirements`, `unset-memory-requirements` — Karpenter handles scheduling), `no-liveness-probe`, `no-read-only-root-fs`, `run-as-non-root`, `privileged-container`, `privilege-escalation-container`, `host-ipc` / `host-network` / `host-pid`, `sensitive-host-mounts`, `liveness-port`, `drop-net-raw-capability`, `dangling-service`, `latest-tag`, `pdb-min-available`, `pdb-unhealthy-pod-eviction-policy`

### Terraform (tflint + tofu fmt)

- AWS plugin enabled, pinned to **version 0.38.0** (`.tflint.hcl`); catches invalid instance types and deprecated resources
- `call_module_type = "none"` so tflint runs without a `tofu init` (module sources don't need to resolve)
- Canonical formatting enforced by `tofu fmt`

### Terraform conventions: prefer `for_each` over `count`

**All new terraform resources MUST use `for_each` keyed by a stable identifier (CIDR, AZ, name, or composite like `"${bucket}-${az}"`)**, with one narrow exception:

```hcl
# ALLOWED — gating an entire resource on a feature flag
count = var.should_this_exist ? 1 : 0
```

Otherwise: always `for_each`.

**Why**: `count` indexes resources by list position. Removing or reordering an element shifts every subsequent index, which terraform interprets as destroy+recreate at the shifted indexes — even though the underlying real-world resource hasn't changed. `for_each` keys resources by stable identifiers, so removing one entry only destroys that one resource. This matters acutely for immutable or in-use AWS resources (subnets, NAT GWs, EIPs, route tables, CIDR reservations) where a spurious destroy+recreate can drop traffic, leak allocations, or block apply.

**Existing `count`-based resources are out of scope to refactor** — leave them as-is unless you're already touching them for another reason.

### Dockerfiles (hadolint)

- Standard rules, but apt/pip version pinning not required (`DL3008`/`DL3013` ignored)

### Security (trivy)

- Scans `base/` and `modules/` for HIGH/CRITICAL IaC issues
- Known exceptions in `.trivyignore` (public EKS API, privileged DaemonSets, etc.)

### All files (.editorconfig)

- LF line endings, trailing newline required, no trailing whitespace

## Don't Do

- **NEVER run `terraform`** — use `tofu` or `just` recipes (terraform will corrupt state)
- Don't run state-changing CLI commands directly (apply, delete, install, destroy) — use `just` recipes
- Don't create bash scripts without considering Python first
- Don't use pip/conda/poetry — use `uv` for Python packages
- Don't install packages or run setup scripts without checking first
- Don't update ANY versions (tools, deps, images) without explicit approval
- Don't create documentation files unless explicitly requested
- Don't experiment with the cluster — read-only investigation is fine, but don't change anything
- Don't mix unrelated files or technologies in the same directory
