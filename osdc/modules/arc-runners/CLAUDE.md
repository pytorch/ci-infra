# modules/arc-runners/ — ARC Runner Scale Sets

GitHub Actions self-hosted runners via Actions Runner Controller. Requires the `arc` module (controller) and `nodepools` module (compute).

## What's here

| Path | Purpose |
|------|---------|
| `defs/*.yaml` | **Source of truth** — runner definitions (instance type, CPU, memory, GPU) |
| `templates/runner.yaml.tpl` | Multi-doc template: Helm values + job pod hook ConfigMap |
| `clusters.yaml` (repo root) | Per-cluster GitHub config (org URL, secret name, runner prefix) |
| `generated/` | Auto-generated ARC runner configs (from defs + template) |
| `scripts/python/generate_runners.py` | Reads `defs/` + template → writes `generated/` |
| `deploy.sh` | Generates configs, applies ConfigMaps + Helm releases |

## Dependencies

- **arc** module: ARC controller must be running (deploy.sh enforces this)
- **nodepools** module: NodePools must exist for the instance types referenced in defs

## Adding a runner type

1. Create `defs/<name>.yaml`:
   ```yaml
   runner:
     name: my-runner
     instance_type: c6i.12xlarge    # must have a matching NodePool
     disk_size: 100
     vcpu: 4
     memory: 8Gi
     gpu: 0
   ```
2. Ensure `nodepools` module has a def for the `instance_type`
3. Run `just deploy-module <cluster> arc-runners`

## Two-tier pod model

- **Runner pod** (750m CPU, 512Mi) — lightweight ARC orchestrator, mounts hook ConfigMap
- **Job pod** (resources from def) — runs actual workflow containers, gets git cache volume

## Configuration resolution

`generate_runners.py` reads cluster-specific config (GitHub URL, secret, prefix) from `clusters.yaml` under the `arc-runners` key. Runner scaling is unlimited -- prefer overspend over outage.

## Who uses this

- **pytorch/pytorch-canary** (staging) and **pytorch** org (production)
