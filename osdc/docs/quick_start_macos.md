# Quick Start (macOS)

## Prerequisites

### 1. Homebrew

```bash
/bin/bash -c "$(curl -fsSL https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)"
```

### 2. Install tools

```bash
brew install mise just git
```

### 3. AWS credentials
reach out to pytorch dev infra team to get access to the AWS credential

### 4. Clone and init submodules

```bash
git clone <repo-url> ciforge
cd ciforge/osdc
git submodule update --init --recursive
```

### 5. Install project dependencies

```bash
mise install    # installs tofu, kubectl, helm, crane, etc.
just setup      # installs Python dependencies via uv
```

## Verify setup

```bash
just lint       # all linters should pass
just test       # all tests should pass
```

## Clusters

| Cluster ID | Name | Region | Purpose |
|---|---|---|---|
| `arc-cbr-production` | pytorch-arc-cbr-production | us-east-2 | CI runners (includes B200 GPU) |
| `re-prod` | pytorch-re-prod-production | us-east-2 | Release Engineering |

```bash
just list               # show all clusters and modules
just show <cluster>     # show cluster config details
```

## Connect to a cluster

```bash
just kubeconfig <cluster>
# e.g.
just kubeconfig re-prod
```

Or manually:

```bash
aws eks update-kubeconfig --name pytorch-re-prod-production --region us-east-2 --profile ossadmin
```

## Deploy

```bash
# Full deploy (base + all modules)
just deploy <cluster>

# Base infra only
just deploy-base <cluster>

# Single module
just deploy-module <cluster> <module>
```

## Useful commands

```bash
just --list                          # all available recipes
kubectl get pods -A                  # all pods
kubectl get nodes                    # all nodes
kubectl get nodepools                # Karpenter NodePools
k9s                                  # interactive cluster UI
```

## Lint and test

Always run before submitting changes:

```bash
just lint
just test
```

## More docs

- [operations.md](operations.md) — day-to-day operations, adding clusters/modules/runners
- [architecture.md](architecture.md) — platform design
- [modules.md](modules.md) — module contract and structure
- [observability.md](observability.md) — monitoring and logging
