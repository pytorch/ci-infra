# CLAUDE.md — OSDC (Open Source Dev Cloud)

project-doc: enabled

## What This Is

Modular Kubernetes infrastructure platform on AWS EKS. A shared `base/` provides the cluster (VPC, EKS, Harbor, git cache, GPU plugins), and optional `modules/` layer services on top (ARC, runners, BuildKit, future projects). One codebase drives multiple clusters across regions via `clusters.yaml`.

Working directory: `osdc/`. Run all commands from here.

## Clusters Configuration (IMPORTANT)

**All paths below are relative to this CLAUDE.md file.**

Two `clusters.yaml` files exist — always check both when investigating module deployment:

- **`./clusters.yaml`** — upstream cluster definitions (the platform's own clusters)
- **`../../clusters.yaml`** — consumer repo cluster definitions (consumer-specific clusters)

Modules may be deployed in upstream clusters but not consumer clusters, or vice versa. Never conclude a module is "not deployed" without checking both files.

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

## Skills Reference

Detailed instructions are broken into on-demand skills. Load the relevant skill when working on a specific topic:

| Skill | What it covers | When to load |
|-------|---------------|--------------|
| `osdc-project-structure` | Architecture, directory tree, submodule pattern, design decisions, git cache, knowledge base, key files, docs index | Always load when working on OSDC |
| `osdc-deployment` | Deploy workflow, just recipes, base/module deploy order, clusters.yaml, Terraform architecture, smoke tests | Deploying, adding modules, modifying deploy scripts |
| `osdc-tooling-and-quality` | Tools (tofu/just/mise/uv), automation hierarchy, unit tests, code style, 13 linters, indentation rules, full Don't Do list | Writing code, running linters, adding scripts/tests |
| `osdc-runners-nodepools` | Runners, NodePools, BuildKit, GitHub Actions constraints, node taints, image mirroring, change checklist | Modifying runners, nodepools, BuildKit, node configs |
| `osdc-observability` | Monitoring + logging pipelines, three-Alloy architecture, Loki log queries, label strategy, module pipelines, credentials | Working on monitoring, logging, Alloy, querying logs |
| `osdc-cli-debugging` | Read-only kubectl, aws, helm, tofu commands and safety boundaries | Investigating cluster state, debugging pods |
| `osdc-harbor` | Harbor Helm chart gotchas, image mirroring, proxy cache configuration | Working on Harbor or container registry config |

Load the relevant `osdc-*` skill when you need detailed instructions on any specific topic.

## Docs Index

Reference documentation in `docs/`:

| Doc | What it covers |
|-----|---------------|
| `docs/architecture.md` | Platform design — base vs modules separation, cluster lifecycle on AWS EKS |
| `docs/modules.md` | Module contract — what a module is, directory structure, required files |
| `docs/observability.md` | Three-Alloy observability architecture — monitoring + logging pipelines to Grafana Cloud |
| `docs/observability-estimates.md` | Per-unit cost estimates for metrics cardinality and log volume (Grafana Cloud billing) |
| `docs/operations.md` | Operational prerequisites — AWS CLI, mise, working directory setup for cluster management |
| `docs/loki_query.md` | CLI queries against Grafana Cloud Loki when kubectl logs is unavailable |
| `docs/mimir_query.md` | CLI queries against Grafana Cloud Mimir (Prometheus metrics, no in-cluster Prometheus) |
| `docs/runner_naming_convention.md` | Runner label format and the ~42 character name limit (ARC/K8s/Cilium constraints) |
| `docs/current_runner_load_distribution.md` | Job counts and peak concurrency by runner type (pytorch/pytorch, from ClickHouse) |
| `docs/node-utilization-optimization.md` | Runner-to-node packing efficiency analysis and instance type recommendations |
| `docs/node-warmup-and-scheduling-gates.md` | Full node initialization sequence — taints, DaemonSets, init containers before job scheduling |
| `docs/initialize-containers-slowness.md` | Root-cause analysis of "Initialize containers" delays (ARC workspace copy + CPU starvation) |
| `docs/pr-migration.md` | Migration plan from monolithic `arc/` to modular `osdc/` layout |
