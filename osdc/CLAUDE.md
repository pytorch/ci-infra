# CLAUDE.md — OSDC (Open Source Dev Cloud)

project-doc: enabled

This file is the canonical project guidance for any coding agent (Claude, Codex, etc.) operating in `osdc/`. The sibling `AGENTS.md` is a thin pointer back to this file so AGENTS.md-aware tools pick up the same rules.

## What This Is

Modular Kubernetes infrastructure platform on AWS EKS. The `modules/eks/` module provisions the cluster (VPC, EKS, Harbor S3/IAM via tofu); `base/` layers cluster-wide k8s resources on top (Harbor Helm chart, NVIDIA device plugin, image-cache-janitor, node tuning, runner-base image). Optional `modules/` (ARC, runners, BuildKit, nodepools, pypi-cache, monitoring, logging, etc.) deploy on top. One codebase drives multiple clusters across regions via `clusters.yaml`.

Working directory: `osdc/`. Run all commands from here.

## Clusters Configuration

All clusters are defined in `./clusters.yaml`. This is the single source of truth for cluster definitions, module deployment, and per-cluster configuration.

## NEVER USE TERRAFORM — USE TOFU ONLY

This project uses **OpenTofu** (`tofu`), NOT Terraform. Running `terraform` commands **will corrupt the state file** and can destroy infrastructure. There is no recovery.

- **NEVER** run `terraform init`, `terraform plan`, `terraform apply`, or any `terraform` subcommand
- **ALWAYS** use `tofu` or `just` recipes (which call `tofu` internally)
- Directories are named `terraform/` but the tool is `tofu`
- In `mise.toml`, the entry is `opentofu`, never `terraform`

## Before Declaring Work Complete (MANDATORY)

```bash
just lint    # All 13 linters must pass with zero errors
just test    # All unit tests must pass
```

If either fails, fix the issues before finishing. Do not defer lint or test failures — they block CI and break other contributors.

## Don't Do (Critical Safety Rules)

- **NEVER run `terraform`** — use `tofu` or `just` recipes (terraform will corrupt state)
- Don't run state-changing CLI commands directly (apply, delete, install, destroy) — use `just` recipes
- Don't experiment with the cluster — read-only investigation is fine, but don't change anything
- Don't update ANY versions (tools, deps, images) without explicit approval

## GPU Fleet Unification & NUMA Topology

GPU families (g5, g6, g4dn) use unified single-fleet definitions (all GPU counts in one fleet). GPU allocation is handled by `nvidia.com/gpu` resource requests, not fleet-level isolation. Default `topologyManagerPolicy` is `best-effort`; multi-NUMA GPU instances (p4d, p5, p6-b200) override per-def to `single-numa-node` to prevent NUMA fragmentation and TopologyAffinityError livelocks under mixed GPU packing. See `actions-knowledge-base/docs/osdc/numa-topology-gpu-fleet-unification.md` for details.

## Skills

Skills live under `.claude/skills/`. Each one is a `SKILL.md` file with deep notes on one topic. Read the file when you start work in that area. Codex and other agents without skill auto-loading should open the path directly.

| Skill file | Read when |
|------------|-----------|
| [`.claude/skills/osdc-project-structure/SKILL.md`](.claude/skills/osdc-project-structure/SKILL.md) | Always. Project layout, key files, design choices. |
| [`.claude/skills/osdc-deployment/SKILL.md`](.claude/skills/osdc-deployment/SKILL.md) | Deploying. Editing `clusters.yaml`. Changing deploy scripts. |
| [`.claude/skills/osdc-tooling-and-quality/SKILL.md`](.claude/skills/osdc-tooling-and-quality/SKILL.md) | Writing code. Running linters. Adding tests. |
| [`.claude/skills/osdc-runners-nodepools/SKILL.md`](.claude/skills/osdc-runners-nodepools/SKILL.md) | Editing runners, nodepools, BuildKit, node configs. |
| [`.claude/skills/osdc-observability/SKILL.md`](.claude/skills/osdc-observability/SKILL.md) | Touching monitoring, logging, Alloy. Querying logs or metrics. |
| [`.claude/skills/osdc-cli-debugging/SKILL.md`](.claude/skills/osdc-cli-debugging/SKILL.md) | Debugging the cluster. Running `kubectl`, `aws`, `helm`, `tofu`. |
| [`.claude/skills/osdc-harbor/SKILL.md`](.claude/skills/osdc-harbor/SKILL.md) | Editing Harbor or container registry config. |
| [`.claude/skills/osdc-pypi-cache/SKILL.md`](.claude/skills/osdc-pypi-cache/SKILL.md) | Editing pypi-cache. Debugging pip install failures on runners. |
| [`.claude/skills/osdc-nodelocaldns/SKILL.md`](.claude/skills/osdc-nodelocaldns/SKILL.md) | Editing NodeLocal DNSCache. Debugging DNS on runner nodes. |

## Docs Index

Reference documentation in `docs/`:

| Doc | What it covers |
|-----|---------------|
| `docs/architecture.md` | Platform design — base vs modules separation, cluster lifecycle on AWS EKS |
| `docs/modules.md` | Module contract — what a module is, directory structure, required files |
| `docs/observability.md` | Three-Alloy observability architecture — monitoring + logging pipelines to Grafana Cloud |
| `docs/observability-estimates.md` | Per-unit cost estimates for metrics cardinality and log volume (Grafana Cloud billing) |
| `docs/operations.md` | Operational prerequisites — AWS CLI, mise, working directory setup for cluster management |
| `docs/ipv6-cluster-recreation.md` | Operator runbook for destroying and recreating an OSDC cluster as IPv6-only (accepted data losses: Harbor S3, EFS pypi-cache) |
| `docs/loki_query.md` | CLI queries against Grafana Cloud Loki when kubectl logs is unavailable |
| `docs/mimir_query.md` | CLI queries against Grafana Cloud Mimir (Prometheus metrics, no in-cluster Prometheus) |
| `docs/runner_naming_convention.md` | Runner label format and the ~42 character name limit (ARC/K8s/Cilium constraints) |
| `docs/current_runner_load_distribution.md` | Job counts and peak concurrency by runner type (pytorch/pytorch, from ClickHouse) |
| `docs/node-utilization-optimization.md` | Runner-to-node packing efficiency analysis and instance type recommendations |
| `docs/node-warmup-and-scheduling-gates.md` | Full node initialization sequence — taints, DaemonSets, init containers before job scheduling |
| `docs/arc-fork-build-deploy.md` | ARC fork (jeanschmidt/actions-runner-controller) build/release workflow and chart publishing |
| `docs/pypi-package-cache.md` | PyPI wheel cache architecture, slug naming, S3 layout, runner integration |
