# CLAUDE.md — OSDC (Open Source Dev Cloud)

project-doc: enabled

## What This Is

Modular Kubernetes infrastructure platform on AWS EKS. A shared `base/` provides the cluster (VPC, EKS, Harbor, git cache, GPU plugins), and optional `modules/` layer services on top (ARC, runners, BuildKit, future projects). One codebase drives multiple clusters across regions via `clusters.yaml`.

Working directory: `osdc/`. Run all commands from here.

## NEVER USE TERRAFORM — USE TOFU ONLY

This project uses **OpenTofu** (`tofu`), NOT Terraform. Running `terraform` commands **will corrupt the state file** and can destroy infrastructure. There is no recovery.

- **NEVER** run `terraform init`, `terraform plan`, `terraform apply`, or any `terraform` subcommand
- **ALWAYS** use `tofu` or `just` recipes (which call `tofu` internally)
- Directories are named `terraform/` but the tool is `tofu`
- In `mise.toml`, the entry is `opentofu`, never `terraform`

## Before Declaring Work Complete (MANDATORY)

```bash
just lint    # All 11 linters must pass with zero errors
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
| `osdc-tooling-and-quality` | Tools (tofu/just/mise/uv), automation hierarchy, unit tests, code style, 11 linters, indentation rules, full Don't Do list | Writing code, running linters, adding scripts/tests |
| `osdc-runners-nodepools` | Runners, NodePools, BuildKit, GitHub Actions constraints, node taints, image mirroring, change checklist | Modifying runners, nodepools, BuildKit, node configs |
| `osdc-observability` | Monitoring + logging pipelines, three-Alloy architecture, Loki log queries, label strategy, module pipelines, credentials | Working on monitoring, logging, Alloy, querying logs |
| `osdc-cli-debugging` | Read-only kubectl, aws, helm, tofu commands and safety boundaries | Investigating cluster state, debugging pods |
| `osdc-harbor` | Harbor Helm chart gotchas, image mirroring, proxy cache configuration | Working on Harbor or container registry config |

Load the relevant `osdc-*` skill when you need detailed instructions on any specific topic.
