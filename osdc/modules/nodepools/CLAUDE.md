# modules/nodepools/ — Karpenter NodePool Definitions

Pure compute provisioning. Defines what EC2 instance types Karpenter can spin up. No ARC, no runner scale sets — just NodePools.

## What's here

| Path | Purpose |
|------|---------|
| `defs/*.yaml` | **Source of truth** — one file per instance type (name, type, arch, disk, gpu flag) |
| `generated/` | Auto-generated Karpenter NodePool + EC2NodeClass YAMLs |
| `scripts/python/generate_nodepools.py` | Reads `defs/` → writes `generated/` |
| `deploy.sh` | Generates then applies NodePools (with cluster name substitution) |

## Adding a new NodePool

1. Create `defs/<instance-type>.yaml`:
   ```yaml
   nodepool:
     name: m5-4xlarge
     instance_type: m5.4xlarge
     arch: amd64
     disk_size: 100
     gpu: false
   ```
2. Run `just deploy-module <cluster> nodepools`

## Cluster name placeholder

Generated YAMLs contain `CLUSTER_NAME_PLACEHOLDER`. `deploy.sh` does `sed` replacement at apply time with the actual cluster name from `clusters.yaml`.

## Who uses this

- **arc-runners** module: its runner defs reference NodePools by instance_type
- **remoteexec** (future): compute pods scheduled onto these NodePools
- **devgpu** (future): GPU pod allocation using GPU NodePools
- Any module that needs Karpenter-provisioned compute
