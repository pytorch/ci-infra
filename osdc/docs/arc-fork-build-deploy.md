# ARC Fork: Build, Deploy, and Configuration

## The ARC Fork

**Repo**: https://github.com/jeanschmidt/actions-runner-controller.git
**Branch**: `jeanschmidt/placeholder_run_poc` (based on upstream `actions/actions-runner-controller` master)

**Why**: Adds capacity-aware autoscaling (proactive capacity) to the `ghalistener` binary. Stock ARC is count-based and capacity-unaware -- it scales runners without checking whether the cluster can actually fit the runner + workflow pod pair. The fork adds a CapacityMonitor goroutine that dynamically adjusts `maxRunners` reported to GitHub via the `X-ScaleSetMaxCapacity` header, backed by placeholder pod reservations.

**What's changed**:
- New package: `cmd/ghalistener/capacity/` (config.go, monitor.go, placeholder.go, hud_client.go)
- Modified: `cmd/ghalistener/main.go` -- adds CapacityMonitor to the listener's errgroup when `CAPACITY_AWARE_ENABLED=true`
- Everything else (controllers, CRDs, runner charts) runs stock

## Helm Chart

Published from the fork via the `gha-publish-chart.yaml` workflow (manual `workflow_dispatch`). The workflow builds the controller image and publishes the chart to GHCR.

- **OCI registry**: `oci://ghcr.io/jeanschmidt/actions-runner-controller-charts/gha-runner-scale-set-controller`
- **Chart version**: configured in `clusters.yaml` at `arc.chart_version` (currently `0.14.0`)
- **Image tags**: `ghcr.io/jeanschmidt/gha-runner-scale-set-controller:<release_tag_name>` (set during workflow dispatch)

To publish a new chart version, trigger `gha-publish-chart.yaml` from the fork's GitHub Actions UI with `publish_gha_runner_scale_set_controller_chart: true`.

## Building the Forked Image

The `Dockerfile` builds all binaries (manager, ghalistener, webhook server, metrics server, sleep) from Go source into a distroless image.

**Local build and push to Harbor**:

```bash
cd /Users/jschmidt/meta/actions-runner-controller

# Build for linux/amd64 (what the cluster runs)
docker buildx build \
  --platform linux/amd64 \
  --build-arg VERSION=proactive-capacity \
  --build-arg COMMIT_SHA=$(git rev-parse HEAD) \
  -t localhost:30002/osdc/gha-runner-scale-set-controller:proactive-capacity \
  -f Dockerfile \
  --load .

# Push to Harbor — localhost:30002 is the Harbor NodePort, exposed directly
# on every node. No port-forward is needed for the docker push (the NodePort
# is reachable from the host). The deploy.sh uses port-forward 8081:80 only
# for its API calls during project creation.
docker push localhost:30002/osdc/gha-runner-scale-set-controller:proactive-capacity
```

**Current tag approach**: the static tag `proactive-capacity` is used (hardcoded in `modules/arc/deploy.sh` line 104). There is no content-based tagging yet. Rebuilding with the same tag requires restarting the controller pod to pick up the new image.

**Note**: The Harbor `osdc` project is auto-created by `modules/arc/deploy.sh` if it does not exist.

## Deploying

```bash
just deploy-module <cluster> arc
```

This runs `modules/arc/deploy.sh`, which:

1. **Ensures Harbor project** `osdc` exists (port-forwards to Harbor, creates via API, 409 = already exists)
2. **Applies PriorityClasses** from `modules/arc/kubernetes/priority-classes.yaml` (placeholder-runner, arc-runner, placeholder-workflow, arc-workflow)
3. **Applies RBAC** from `modules/arc/kubernetes/capacity-monitor-rbac.yaml`
4. **Helm upgrade** with image override:
   - `--set image.repository=localhost:30002/osdc/gha-runner-scale-set-controller`
   - `--set image.tag=proactive-capacity`
   - Chart: `oci://ghcr.io/jeanschmidt/actions-runner-controller-charts/gha-runner-scale-set-controller`
   - Version: from `clusters.yaml` `arc.chart_version` (default `0.14.0`)

Other deploy.sh config knobs (all from `clusters.yaml`):

| Key | Default | What |
|-----|---------|------|
| `arc.chart_version` | `0.14.0` | Helm chart version |
| `arc.replica_count` | `2` | Controller replicas |
| `arc.log_level` | `info` | Log level |
| `arc.controller_cpu_request` | `1` | CPU request |
| `arc.controller_cpu_limit` | `4` | CPU limit |
| `arc.controller_memory_request` | `2Gi` | Memory request |
| `arc.controller_memory_limit` | `4Gi` | Memory limit |

## Configuration

The capacity monitor is configured via env vars on the listener pod, set in `modules/arc-runners/templates/runner.yaml.tpl`:

| Env Var | Default | Description |
|---------|---------|-------------|
| `CAPACITY_AWARE_ENABLED` | `false` | Enable the capacity monitor goroutine |
| `CAPACITY_AWARE_PROACTIVE_CAPACITY` | `0` | Number of placeholder pairs to maintain ahead of demand |
| `CAPACITY_AWARE_RECALCULATE_INTERVAL` | `30s` | Fallback reconciliation interval (event-driven is primary) |
| `CAPACITY_AWARE_PLACEHOLDER_TIMEOUT` | `5m` | How long a placeholder can stay Pending before being deleted |
| `CAPACITY_AWARE_WORKFLOW_CPU` | _(from runner def)_ | Workflow placeholder CPU request (template: `{{VCPU}}`) |
| `CAPACITY_AWARE_WORKFLOW_MEMORY` | _(from runner def)_ | Workflow placeholder memory request (template: `{{MEMORY}}`) |
| `CAPACITY_AWARE_WORKFLOW_GPU` | _(from runner def)_ | Workflow placeholder GPU count (template: `{{GPU_COUNT}}`) |
| `CAPACITY_AWARE_WORKFLOW_DISK` | _(from runner def)_ | Workflow placeholder disk (template: `{{DISK_SIZE}}`) |
| `CAPACITY_AWARE_RUNNER_CPU` | `750m` | Runner placeholder CPU request |
| `CAPACITY_AWARE_RUNNER_MEMORY` | `512Mi` | Runner placeholder memory request |
| `CAPACITY_AWARE_NODE_FLEET` | _(from runner def)_ | Node fleet for placeholder scheduling (template: `{{NODE_FLEET}}`) |
| `CAPACITY_AWARE_RUNNER_CLASS` | _(from runner def)_ | Runner class for placeholder node selector (template: `{{RUNNER_CLASS}}`) |
| `CAPACITY_AWARE_HUD_API_TOKEN` | _(from K8s secret)_ | PyTorch HUD API token for queued job counts |

To enable for a runner: set `CAPACITY_AWARE_ENABLED=true` in the runner definition. Currently defaults to `false` for all runners.

## Creating the HUD API Secret

```bash
kubectl create secret generic pytorch-hud-token \
  --namespace arc-systems \
  --from-literal=token='<hud-internal-bot-secret>'
```

The template mounts this as optional (`optional: true`), so missing the secret does not prevent pod startup.

## Adding maxRunners to a Runner Definition

Add `max_runners: <value>` to the runner def YAML. Example:

```yaml
runner:
  name: l-x86iavx512-8-16
  instance_type: c7a.48xlarge
  vcpu: 8
  memory: 16Gi
  gpu: 0
  disk_size: 150
  max_runners: 100
```

This flows through the template as `gha_max_runners:` in the generated Helm values. The capacity monitor reads `config.MaxRunners` (set from the listener config) and uses it as the ceiling for `X-ScaleSetMaxCapacity`. Without `max_runners`, the value is empty/unlimited.

Currently no runner definition has `max_runners` set.

## Maintenance

On ARC upgrades:
1. Check if `cmd/ghalistener/main.go` changed (the entry point wiring -- our changes are in the capacity monitor errgroup block)
2. Check if `github.com/actions/scaleset` changed (specifically `listener.SetMaxRunners()` and `listener.Config`)
3. Rebase the fork branch `jeanschmidt/placeholder_run_poc` onto upstream master
4. Rebuild and push the image to Harbor

The `capacity/` package is entirely ours -- no upstream merge conflicts possible. The fork surface is one modified file (`main.go`) plus the new `capacity/` package (4 files).
