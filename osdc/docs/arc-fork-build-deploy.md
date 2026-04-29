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
- **Chart version**: configured in `clusters.yaml` at `arc.chart_version` (currently `0.14.1-jeanschmidt.5`). Format is `<upstream-base>-jeanschmidt.<N>`; bump `<N>` for each fork publish. Valid as both Helm chart version and OCI image tag.
- **Image tags**: `ghcr.io/jeanschmidt/gha-runner-scale-set-controller:<release_tag_name>` (set during workflow dispatch — pass `release_tag_name=0.14.1-jeanschmidt.5` to match the chart)

To publish a new chart version, trigger `gha-publish-chart.yaml` from the fork's GitHub Actions UI with `publish_gha_runner_scale_set_controller_chart: true`.

## Using the Local Chart (dev workflow)

For local development, you can skip publishing the chart to GHCR and point `deploy.sh` directly at the local chart path:

In `modules/arc/deploy.sh`, replace the OCI chart reference:

```bash
# Replace this:
  oci://ghcr.io/jeanschmidt/actions-runner-controller-charts/gha-runner-scale-set-controller \
  --version "${ARC_CHART_VERSION}" \

# With the local path (no --version needed):
  /Users/jschmidt/meta/actions-runner-controller/charts/gha-runner-scale-set-controller \
```

Keep the original lines commented out with a `TODO: restore before committing` so they aren't accidentally merged.

This skips the chart publish step entirely — Helm reads the chart directly from disk. You still need to build and push the controller image to Harbor (see below).

## Building the Forked Image

The `Dockerfile` builds all binaries (manager, ghalistener, webhook server, metrics server, sleep) from Go source into a distroless image.

### Versioning

The image has two version components that serve different purposes:

- **`VERSION` build arg** (compatibility): must match the chart's `appVersion` (e.g., `0.14.1`). See the version check section below for why this is critical.
- **Docker tag** (identity): tracks fork iterations using the format `<chart_version>-capacity.<N>` (e.g., `0.14.1-capacity.1`). Bump `<N>` for every new build. This is what goes in `deploy.sh`'s `image.tag` and what Harbor stores.

When rebasing the fork onto a new upstream release (e.g., `0.15.0`), bump both the `VERSION` build arg and reset the capacity suffix (e.g., `0.15.0-capacity.1`).

### Controller Version Check (CRITICAL — read before building)

The ARC controller has a **hardcoded version reconciliation loop** that runs on every AutoscalingRunnerSet. On each reconcile, it compares:

- `buildVersion`: compiled into the controller binary via `-ldflags -X build.Version=<VERSION>` (from the `VERSION` build arg)
- `autoscalingRunnerSetVersion`: the `app.kubernetes.io/version` label on the CR, stamped by the runner scale set Helm chart from `Chart.AppVersion`

If these don't match, **the controller deletes the AutoscalingRunnerSet CR**. This happens silently — Helm shows a successful deploy, but the controller immediately nukes every runner scale set. All listeners, runners, and placeholders disappear.

**Source**: `controllers/actions.github.com/autoscalingrunnerset_controller.go:139-153`

The comparison logic (`apis/actions.github.com/v1alpha1/version.go:IsVersionAllowed()`) allows the following:

| Condition | Result | Example |
|-----------|--------|---------|
| `buildVersion == "dev"` | Always allowed | Default if `VERSION` build arg is not set |
| `buildVersion` starts with `"canary-"` | Always allowed | `canary-test` |
| Exact string match | Allowed | Both `0.14.1` |
| Semver major.minor match | Allowed | `0.14.0` controller + `0.14.1` chart |
| Any other mismatch | **CR deleted** | `v0.0.2` controller + `0.14.1` chart |

**Common pitfalls**:

- Using a `v` prefix (e.g., `v0.14.1`) — the semver parser does not strip the `v`, so parsing fails and the version is rejected
- Using an unrelated version (e.g., `v0.0.2`) — no match, all runner scale sets deleted
- There is **no flag, env var, or config option** to disable this check

### Local build and push to Harbor

```bash
cd /Users/jschmidt/meta/actions-runner-controller

# Build for linux/amd64 (what the cluster runs)
# VERSION must match the chart's appVersion — DO NOT use an unrelated version
docker buildx build \
  --platform linux/amd64 \
  --build-arg VERSION=0.14.1 \
  --build-arg COMMIT_SHA=$(git rev-parse HEAD) \
  -t localhost:30002/osdc/gha-runner-scale-set-controller:0.14.1-capacity.1 \
  -f Dockerfile \
  --load .

# Save to tarball for crane push (faster than docker push over port-forward)
docker save localhost:30002/osdc/gha-runner-scale-set-controller:0.14.1-capacity.1 -o /tmp/arc-controller.tar

# Port-forward to Harbor (if not already running)
kubectl port-forward -n harbor-system svc/harbor 30002:80 &

# Authenticate crane with Harbor
HARBOR_PASS=$(kubectl get secret -n harbor-system harbor-admin-password -o jsonpath='{.data.password}' | base64 -d)
mise exec -- crane auth login localhost:30002 -u admin -p "$HARBOR_PASS" --insecure

# Push via crane (much faster than docker push — deduplicates existing blobs)
mise exec -- crane push --insecure /tmp/arc-controller.tar localhost:30002/osdc/gha-runner-scale-set-controller:0.14.1-capacity.1
```

**Tagging**: always use the `<chart_version>-capacity.<N>` format. Bump `<N>` for every new build — `IfNotPresent` pull policy means the same tag won't be re-pulled. Never use a static mutable tag like `latest` or `proactive-capacity` in production. Never use a `v` prefix on `VERSION` — the controller's semver parser does not strip it and will fail the version check.

**Why crane instead of docker push**: `docker push` over `kubectl port-forward` is extremely slow and often times out. `crane` is a lightweight registry client that deduplicates blobs and works reliably over the port-forward. It's available via `mise` in the project.

**Note**: The Harbor `osdc` project is auto-created by `modules/arc/deploy.sh` if it does not exist.

## Deploying

```bash
just deploy-module <cluster> arc
```

This runs `modules/arc/deploy.sh`, which:

1. **Ensures Harbor project** `osdc` exists (port-forwards to Harbor, creates via API, 409 = already exists)
2. **Applies PriorityClasses** from `modules/arc/kubernetes/priority-classes.yaml` (placeholder-runner, arc-runner, placeholder-workflow, arc-workflow)
3. **Applies RBAC** from `modules/arc/kubernetes/capacity-monitor-rbac.yaml`
4. **Helm upgrade** of the fork chart:
   - Chart: `oci://ghcr.io/jeanschmidt/actions-runner-controller-charts/gha-runner-scale-set-controller`
   - Version: from `clusters.yaml` `arc.chart_version` (default `0.14.1-jeanschmidt.5`)
   - Image: defaults to `ghcr.io/jeanschmidt/gha-runner-scale-set-controller:<chart_version>`. Override with `arc.image_repository` / `arc.image_tag` in `clusters.yaml` for local Harbor builds.

Other deploy.sh config knobs (all from `clusters.yaml`):

| Key | Default | What |
|-----|---------|------|
| `arc.chart_version` | `0.14.1-jeanschmidt.5` | Helm chart version (fork) |
| `arc.image_repository` | `ghcr.io/jeanschmidt/gha-runner-scale-set-controller` | Controller image repo (override for local Harbor builds) |
| `arc.image_tag` | _(chart_version)_ | Controller image tag (override for local Harbor builds) |
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
| `CAPACITY_AWARE_ENABLED` | `true` | Enable the capacity monitor goroutine |
| `CAPACITY_AWARE_PROACTIVE_CAPACITY` | `0` | Number of placeholder pairs to maintain ahead of demand |
| `CAPACITY_AWARE_RECALCULATE_INTERVAL` | `30s` | Fallback reconciliation interval (event-driven is primary) |
| `CAPACITY_AWARE_PLACEHOLDER_TIMEOUT` | `20m` | How long a placeholder can stay Pending before being deleted |
| `CAPACITY_AWARE_WORKFLOW_CPU` | _(from runner def)_ | Workflow placeholder CPU request (template: `{{VCPU}}`) |
| `CAPACITY_AWARE_WORKFLOW_MEMORY` | _(from runner def)_ | Workflow placeholder memory request (template: `{{MEMORY}}`) |
| `CAPACITY_AWARE_WORKFLOW_GPU` | _(from runner def)_ | Workflow placeholder GPU count (template: `{{GPU_COUNT}}`) |
| `CAPACITY_AWARE_WORKFLOW_DISK` | _(from runner def)_ | Workflow placeholder disk (template: `{{DISK_SIZE}}`) |
| `CAPACITY_AWARE_RUNNER_CPU` | `750m` | Runner placeholder CPU request |
| `CAPACITY_AWARE_RUNNER_MEMORY` | `512Mi` | Runner placeholder memory request |
| `CAPACITY_AWARE_NODE_FLEET` | _(from runner def)_ | Node fleet for placeholder scheduling (template: `{{NODE_FLEET}}`) |
| `CAPACITY_AWARE_RUNNER_CLASS` | _(from runner def)_ | Runner class for placeholder node selector (template: `{{RUNNER_CLASS}}`) |
| `CAPACITY_AWARE_HUD_API_TOKEN` | _(from K8s secret)_ | PyTorch HUD API token for queued job counts |

Currently enabled for all runners (`CAPACITY_AWARE_ENABLED=true` is hardcoded in the template).

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

## Load Testing the Capacity Monitor

To verify the capacity monitor and HUD integration on arc-staging:

```bash
just load-test arc-staging --label l-x86iamx-8-16:400
```

**Why `--label l-x86iamx-8-16:400`**: arc-staging runs in us-west-1 where `c7a` instances are not available. The default distribution assigns most jobs to `l-x86iavx512-8-16` (node-fleet `c7a`), which will never schedule. `l-x86iamx-8-16` uses node-fleet `c7i`, which is available in us-west-1.

To exercise multiple runner types in parallel (e.g., CPU + GPU), repeat `--label`:

```bash
just load-test arc-staging --label l-x86iamx-8-16:400 --label l-x86iavx512-29-115-t4:200
```

**GPU labels in us-west-1**: g5 (A10G) and g6 (L4) fleets have `exclude_regions: [us-west-1]`. Only g4dn (T4) is available — pick from `l-x86iavx512-29-115-t4` (1×T4), `l-x86iavx512-45-172-t4-4` (4×T4), or `l-bx86iavx512-94-344-t4-8` (8×T4, bare-metal).

`--label` and `--jobs` are mutually exclusive. Without `--label`, `--jobs N` distributes proportionally across all available runner types.

**Verifying the capacity monitor is working**:

```bash
# Check listener startup logs for "Capacity monitor enabled" and "Starting capacity monitor"
kubectl logs -n arc-systems <listener-pod> | head -20

# Check reconciliation logs (every 30s)
kubectl logs -n arc-systems <listener-pod> | grep "capacity reconciled"

# Test HUD API directly
curl -s 'https://hud.pytorch.org/api/clickhouse/queued_jobs_aggregate?parameters=%7B%22queuedThresholdMinutes%22%3A0%2C%22maxAgeDays%22%3A3%2C%22orgs%22%3A%5B%22pytorch%22%5D%2C%22repo%22%3A%22%22%7D' | python3 -m json.tool | head -20
```

If the capacity monitor is NOT starting despite `CAPACITY_AWARE_ENABLED=true`, the controller image likely needs to be rebuilt — the running binary may predate the capacity monitor code.

## Maintenance

On ARC upgrades:
1. Check if `cmd/ghalistener/main.go` changed (the entry point wiring -- our changes are in the capacity monitor errgroup block)
2. Check if `github.com/actions/scaleset` changed (specifically `listener.SetMaxRunners()` and `listener.Config`)
3. Rebase the fork branch `jeanschmidt/placeholder_run_poc` onto upstream master
4. Rebuild and push the image to Harbor

The `capacity/` package is entirely ours -- no upstream merge conflicts possible. The fork surface is one modified file (`main.go`) plus the new `capacity/` package (4 files).
