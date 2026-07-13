---
name: osdc-runners-nodepools
description: >
  OSDC runners, NodePools, BuildKit, GitHub Actions constraints, EKS node taints,
  image mirroring, and the runner/nodepool change checklist.
  Applies to ~/meta/ci-infra/osdc.
  Load when modifying runners, nodepools, BuildKit, or node configurations.
---

# OSDC Runners, NodePools & BuildKit

## Quality Gates

Before declaring runner/nodepool work complete: run `just lint` and `just test` from `osdc/`. All 13 linters and unit tests must pass — these gates are mandatory per the project CLAUDE.md.

## Runner & NodePool Change Checklist (MANDATORY)

When changing runner definitions (`modules/arc-runners/defs/`, `modules/arc-runners-h100/defs/`, `modules/arc-runners-b200/defs/`) or NodePool definitions (`modules/nodepools/defs/`, `modules/nodepools-h100/defs/`, `modules/nodepools-b200/defs/`), you MUST update the following `scripts/python/` files to stay in sync:

| File | What to update |
|------|---------------|
| `scripts/python/instance_specs.py` | `INSTANCE_SPECS` (vcpu, memory_gib, memory_mi, gpu, arch — used by `analyze_node_utilization.py`, `generate_buildkit.py`, `collect_instance_memory.py`, `simulate_cluster.py`), `ENI_MAX_PODS` (AWS-stock max-pods used by simulators and BuildKit sizing) |
| `scripts/python/pytorch_workload_data.py` | `OLD_TO_NEW_LABEL` (update old->new runner name mappings when names change) |
| `scripts/python/simulate_cluster.py` | Uses `analyze_node_utilization` functions — verify simulation still works |
| `scripts/python/simulate_cluster_cli.py` | CLI entry point for simulation — re-run to validate packing |
| `integration-tests/workflows/integration-test.yaml.tpl` | Update `runs-on` labels if runner names changed. Jobs are wrapped in `# BEGIN_<TAG>` / `# END_<TAG>` markers; if you add a new gate, register it in `TAG_REQUIREMENTS` in `integration-tests/scripts/python/phases.py` so the orchestrator strips jobs on clusters missing the required modules. |
| `docs/runner_naming_convention.md` | Update runner name examples and mapping tables |

**Verification**: After any runner/nodepool change, run `just analyze-utilization` to confirm packing efficiency and `just test` to verify all scripts agree on the new values.

**DaemonSet impact**: When changing runner/job pod resources or adding new instance types, account for per-node DaemonSet overhead. The BuildKit pod-sizing algorithm subtracts a fixed 300m CPU / 440Mi memory budget (`DAEMONSET_OVERHEAD_CPU_M` / `DAEMONSET_OVERHEAD_MEM_MI` in `modules/buildkit/scripts/python/generate_buildkit.py`) before dividing per-node capacity. Cluster-wide base DaemonSets that fit inside this budget (manifests under `base/kubernetes/`): `registry-mirror-config`, `node-performance-tuning`, `algif-mitigation`, `dirtyfrag-mitigation`, `image-cache-janitor`, `nvidia-device-plugin`, plus DCGM on GPU nodes. `nodelocaldns` (25m CPU / 100Mi memory) is under `base/kubernetes/nodelocaldns/` and deployed via its own deploy.sh. Per-module DaemonSets include `cache-enforcer` (module) and `runner-hooks-warmer` (declared in `modules/arc/kubernetes/hooks-warmer.yaml`, runs in the `arc-runners` namespace, but pinned to `node-fleet=c7i-runner` via nodeSelector). Helm/EKS-addon overhead (kube-prometheus-stack node-exporter, Alloy logging, kube-proxy, vpc-cni, ebs-csi-node) is enumerated in `scripts/python/daemonset_overhead.py` (`HELM_DAEMONSETS` + `EKS_ADDON_DAEMONSETS`). The live computed total is available via `uv run scripts/python/daemonset_overhead.py`. New cluster-wide DaemonSets MUST tolerate the standard taints listed in the EKS Node Taints section below.

## GitHub Actions Workflow Constraints

All self-hosted runner pods run in `containerMode: kubernetes-novolume`. Workflow jobs typically need a `container:` image — containerless jobs may be rejected by the runner-container-hooks. Runner image is digest-pinned per OSDC commit by `modules/arc-runners/scripts/python/resolve_runner_version.py` — never use `:latest` and there is no Renovate bot. Runner-container-hooks fork: the `runner-hooks-warmer` DaemonSet (`modules/arc/kubernetes/hooks-warmer.yaml`, deployed by the `arc` module into the `arc-runners` namespace) pins **`v0.8.15`** (https://github.com/jeanschmidt/runner-container-hooks/releases/tag/v0.8.15) — this is the version actually placed on each node. A stale comment in `runner.yaml.tpl` still references `v0.8.13`; the warmer DaemonSet wins. The `wait-for-hooks` init container (Alpine, polls `/mnt/host-hooks/dist/index.js` for up to 300s) snapshots the patched hooks into an emptyDir consumed by every runner pod.

### Runner image resolution (per-commit digest pinning)

`modules/arc-runners/deploy.sh` resolves the runner image in this order:

1. **Operator override**: if `arc.runner_image_tag` is set in `clusters.yaml` (under the cluster's `arc:` block), the deploy uses `ghcr.io/actions/actions-runner:<tag>` (tag-only, no digest). The resolver is bypassed entirely — no GitHub API call, no `crane` call, ConfigMap untouched. Use this as the rollback escape hatch.
2. **Auto-resolve**: otherwise `resolve_runner_version.py` is invoked. It uses the SHA of the most recent commit touching anything under `osdc/` as a lookup key into the `arc-runner-version-lock` ConfigMap (namespace `osdc-system`, key `history.json`, capped at 20 entries newest-first). Cache hit returns the locked `tag@digest`. Cache miss calls GitHub `/releases/latest`, resolves the digest via `crane digest`, prepends the entry, and writes back with optimistic-concurrency (resourceVersion); 409 retries up to 5 times.
3. **Final fallback**: `generate_runners.py` line ~275 contains `cluster_config.get("runner_image", "ghcr.io/actions/actions-runner:2.333.1")` — but in practice `deploy.sh` always exports `RUNNER_IMAGE`, so the literal `2.333.1` only ever appears via unit tests, never via deploy.

The three `arc-runners*` modules (`arc-runners`, `arc-runners-h100`, `arc-runners-b200`) deploy concurrently and share the same lock ConfigMap. The H100 and B200 modules `exec` into the base `arc-runners/deploy.sh` with `ARC_RUNNERS_DEFS_DIR` / `ARC_RUNNERS_OUTPUT_DIR` / `ARC_RUNNERS_MODULE_NAME` overrides, so they inherit the resolver result. See `docs/runner-image-autoresolve.md` for the full ConfigMap shape, failure modes, and rollback paths.

There is no longer a `.github/workflows/osdc-auto-update-deploy-prod.yml` — deploy-time resolution replaced the Renovate flow. The smoke test `modules/arc-runners/tests/smoke/test_runner_version_lock.py` validates the ConfigMap shape and cross-checks the locked entry against the deployed `AutoscalingRunnerSet` pod templates.

## EKS Node Taints

**Base nodes**: `CriticalAddonsOnly=true:NoSchedule`. All base workloads (Harbor, DaemonSets, Karpenter, control plane) must tolerate this.

**Workload (Karpenter) nodes** — every NodePool emits these taints:
- `node-fleet=<fleet-name>:NoSchedule` — fleet isolation (e.g. `c7i-runner`, `g5`, `m8g`)
- `instance-type=<instance>:NoSchedule` — per-instance-type taint
- `nvidia.com/gpu=true:NoSchedule` — GPU pools only

**IPv6-only EKS**: the cluster runs on IPv6-only pod networking (`ip_family = "ipv6"`, commit `a6b4c8c` / PR #576). Pod IPs are allocated from a /80 IPv6 prefix per node via VPC CNI prefix delegation; the service CIDR is auto-assigned by EKS in `fd00:ec2::/108` (ULA). There is no per-AZ NodePool fan-out, no `bucket` label, no ENIConfig CR, and no `pod_cidr_buckets` map in `clusters.yaml` — the IPv4 + Custom Networking bucket scheme described in the abandoned `INCREASE_IPV4.md` plan was superseded by the IPv6 migration and is NOT in the current generator.

**Startup taints** (cleared by per-DaemonSet init containers via the shared `taint_remover.py` at end-of-init; registry in `modules/nodepools/scripts/python/generate_nodepools.py:STARTUP_TAINTS`):
- `node-init.osdc.io/registry-mirror=true:NoSchedule` — emitted on every cluster; cleared by `registry-mirror-config`
- `node-init.osdc.io/perf-tuning=true:NoSchedule` — emitted on every cluster; cleared by `node-performance-tuning`
- `node-init.osdc.io/algif-mitigation=true:NoSchedule` — emitted on every cluster (TEMPORARY, removed in lockstep with the DaemonSet once AL2023 kernel 6.12.85+ is rolled out); cleared by `algif-mitigation`
- `node-init.osdc.io/dirtyfrag-mitigation=true:NoSchedule` — emitted on every cluster (TEMPORARY, removed in lockstep with the DaemonSet once AL2023 kernel 6.1.170+ or 6.12.83+ is rolled out); cleared by `dirtyfrag-mitigation`
- `node-init.osdc.io/cache-enforcer=true:NoSchedule` — emitted only on clusters with `cache-enforcer` in `NODEPOOLS_ENABLED_MODULES` AND on nodepool defs that pass the per-def `applies_when` predicate. The predicate (`generate_nodepools.py:62-65`) skips release-runner pools (`extra_labels.osdc.io/runner-class == "release"`) because the cache-enforcer DaemonSet has matching `DoesNotExist` nodeAffinity and never schedules there — emitting the taint would deadlock those nodes. Cleared by `cache-enforcer`.
- `node-init.osdc.io/hf-cache=true:NoSchedule` — emitted only on clusters with `hf-cache` in `NODEPOOLS_ENABLED_MODULES` AND on GPU nodepool defs (per-def `applies_when: d.get("gpu")`). The hf-cache mount DaemonSet is GPU-only (`nvidia.com/gpu` nodeSelector) — CPU runners don't pull models — so emitting the taint on CPU nodepools would strand them (nothing to clear it). Cleared by `hf-cache` (the rclone mount's `taint-remover` sidecar, once the FUSE is up).

`STARTUP_TAINTS` registry is validated at generator startup (`generate_nodepools.py:_validate_startup_taints_registry`) — each entry's `module` field must match a sibling directory under `modules/`, catching typos before they ship.

Workload pods do NOT tolerate `node-init.osdc.io/*` — they wait for every applicable taint to be removed. Runner pod tolerations are `node-fleet`, `instance-type`, plus `nvidia.com/gpu` on GPU job pods.

## Image Mirroring

For image mirroring details (bootstrap images, ECR, Harbor proxy cache), see the `osdc-harbor` skill. Cluster-wide DaemonSets that pull from already-mirrored upstreams (e.g. `nodelocaldns` from `registry.k8s.io`) do NOT need a per-image pre-mirror entry — the existing `registry-mirror-config` proxy cache handles them lazily on first pull (~30-60s ImagePullBackOff possible on a fresh Karpenter node before containerd `hosts.toml` is written).

## BuildKit Build Service

BuildKit (`moby/buildkit:v0.29.0`) runs as two Deployments in the `buildkit` namespace — one per architecture. Runner job pods invoke `buildctl` — no Kubernetes API access required.

- **Architecture**: Dual-arch fleet with per-arch Deployments and Services
  - `buildkitd-arm64` — Graviton (script default `m8gd.24xlarge`; real clusters override — both staging clusters and the prod fleets use `m7gd.16xlarge`), Service: `buildkitd-arm64.buildkit:1234`
  - `buildkitd-amd64` — Intel (default `m6id.24xlarge`), Service: `buildkitd-amd64.buildkit:1234`
  - `buildkitd` — combined Service (round-robin across both arches, for arch-agnostic builds)
- **Sizing**: Dynamically computed by `modules/buildkit/scripts/python/generate_buildkit.py` from instance specs. Guaranteed QoS (requests == limits), static CPU pinning, `max-parallelism=1` (one build at a time per pod), 2 pods per node by default (overridable per-arch via `buildkit.amd64_pods_per_node` / `buildkit.arm64_pods_per_node`)
- **NUMA**: BuildKit EC2NodeClasses use `topologyManagerPolicy: restricted` + `topologyManagerScope: container` + `prefer-closest-numa-nodes: true` — stricter than the nodepool generator's `best-effort` default. Relevant when debugging BuildKit scheduling failures on multi-NUMA hosts.
- **Startup taint**: BuildKit nodepools also carry their own `git-cache-not-ready=true:NoSchedule` startup taint (in addition to the cluster-wide `node-init.osdc.io/*` ones). The git-cache rsync init clears it before pods can schedule.
- **Instance types**: Configurable via `clusters.yaml` (`buildkit.arm64_instance_type`, `buildkit.amd64_instance_type`)
- **Scaling**: Configurable via `clusters.yaml` (`buildkit.replicas_per_arch`, default 12 in `defaults:`; the bash fallback in `deploy.sh` is 4, used only if neither `defaults:` nor the cluster sets a value)
- **Autoscaling (KEDA)**: Enabled via `buildkit.autoscaling.enabled: true` (currently true on `arc-cbr-production` and both `lf-prod-aws-ue1`/`ue2` and both staging clusters; false elsewhere). Requires the `keda` module to be in the cluster's module list. Per-arch `_min`/`_max`/`_fallback` knobs live under `buildkit.autoscaling.*` in `clusters.yaml`. ScaledObjects are generated by `generate_buildkit.generate_autoscaling_yaml` and applied from `generated/autoscaling.yaml`. With autoscaling on, Deployments omit the static `replicas:` line and add `terminationGracePeriodSeconds: 8100` for in-flight builds.
- **Storage**: NVMe instance storage (RAID0) for build cache + git object cache
- **Registry mirrors**: `buildkitd.toml` routes `FROM` image pulls through Harbor for docker.io, ghcr.io, nvcr.io, registry.k8s.io, quay.io (public.ecr.aws not mirrored — no rate limits)
- **Network access**: NetworkPolicy restricts ingress to pods from `arc-runners` namespace only
- **Load balancing**: HAProxy `buildkitd-lb` (least-connections) distributes `buildctl` connections across buildkitd pods per architecture. Backends are discovered via headless Service DNS (`buildkitd-arm64-pods`, `buildkitd-amd64-pods`, `buildkitd-pods` — `clusterIP: None`) with `resolve-prefer ipv6` on each `server-template` line because under IPv6-only EKS the pod IPs only emit AAAA records.

### Targeting an architecture

```bash
# Build an ARM64 image
buildctl --addr tcp://buildkitd-arm64.buildkit:1234 build --output type=image,name=$IMAGE,push=true ...

# Build an x86_64 image
buildctl --addr tcp://buildkitd-amd64.buildkit:1234 build --output type=image,name=$IMAGE,push=true ...

# Multi-arch: build both, then combine with crane
crane index append -t $IMAGE -m $IMAGE-arm64 -m $IMAGE-amd64
```

### Using the git cache in Dockerfiles

The git-cache rsync runs on BuildKit nodes (same as runner nodes). The buildkitd pod mounts the cache at `/opt/git-cache`. Pass it as a named build context, then bind-mount in the Dockerfile and set `GIT_ALTERNATE_OBJECT_DIRECTORIES`:

```bash
buildctl ... --opt context:gitcache=local:gitcache --local gitcache=/opt/git-cache ...
```

```dockerfile
RUN --mount=type=bind,from=gitcache,source=pytorch/pytorch.git/objects,target=/tmp/git-objects \
    GIT_ALTERNATE_OBJECT_DIRECTORIES=/tmp/git-objects \
    git clone https://github.com/pytorch/pytorch /workspace
```

## Module-Specific Operational Knowledge

### BuildKit Pod Sizing Algorithm

Pod resource requests are computed by `modules/buildkit/scripts/python/generate_buildkit.py` from a static instance spec table:
1. Look up total vCPU + memory for the instance type
2. Subtract kubelet reserved resources
3. Subtract DaemonSet overhead (300m CPU, 440Mi memory)
4. Apply 10% margin
5. Divide by `pods_per_node` (default: 2)

This ensures exactly N pods fit per node with Guaranteed QoS (requests == limits -> static CPU pinning). NVMe instance storage uses `instanceStorePolicy: RAID0` on EC2NodeClass (nodeadm handles formatting/mounting) — instance types must have `d` suffix.

When adding a new instance type: update BOTH `INSTANCE_SPECS` (vcpu/memory/gpu/arch) and `ENI_MAX_PODS` (AWS-stock max-pods) in `scripts/python/instance_specs.py`.

### EC2NodeClass userData

Generated EC2NodeClass `userData` is multipart MIME. The `application/node.eks.aws` part always carries kubelet config (cpuManagerPolicy, topologyManagerPolicy/Scope, log size/files). Defaults: `cpuManagerPolicy: static`, `topologyManagerPolicy: best-effort`, `topologyManagerScope: container`. BuildKit overrides this to `topologyManagerPolicy: restricted` + `prefer-closest-numa-nodes: true` for tight NUMA placement — it is the only generator that doesn't use `best-effort`. Most pools stop at the kubelet block — registry mirrors, CPU governor, GPU persistence, and the algif_aead / DirtyFrag CVE blacklists are handled by base DaemonSets (`registry-mirror-config`, `node-performance-tuning`, `algif-mitigation`, `dirtyfrag-mitigation`).

### EC2NodeClass metadataOptions — IPv6 IMDS

Generated EC2NodeClass templates set `spec.metadataOptions.httpProtocolIPv6: enabled`. Under IPv6-only EKS the instance metadata service (IMDS) is reachable at `[fd00:ec2::254]` instead of the IPv4 link-local `169.254.169.254`. Workloads, kubelet, and AWS SDKs that talk to IMDS must do so over IPv6; enabling `httpProtocolIPv6` is what makes the IMDS endpoint listen on the IPv6 link-local address. Applied to: every nodepool generator output (`modules/nodepools/scripts/python/generate_nodepools.py`) and the pypi-cache nodepool template (`modules/pypi-cache/kubernetes/ec2nodeclass.yaml.tpl`).

GPU pools (H100, B200) optionally append a `text/x-shellscript` MIME part via the per-def `user_data_script` field — used today for one-shot containerd registry mirror config that must be in place before any image pull. Prefer DaemonSets where possible; reserve `user_data_script` for boot-critical setup only.

**TEMPORARY mitigations** — two kernel-mod blacklist DaemonSets with identical shape and the same lifecycle:
- `base/kubernetes/algif-mitigation.yaml` — modprobe blacklist for CVE-2026-31431 ("Copy Fail" algif_aead LPE). Remove once nodes are on a kernel 6.12.85+ AL2023 AMI. AMI pinnings tagged with `TODO(CVE-2026-31431)` markers are the trigger points: `clusters.yaml`, `modules/nodepools/scripts/python/generate_nodepools.py`, `modules/buildkit/scripts/python/generate_buildkit.py`, `modules/pypi-cache/kubernetes/ec2nodeclass.yaml.tpl`. Watch https://explore.alas.aws.amazon.com/CVE-2026-31431.html.
- `base/kubernetes/dirtyfrag-mitigation.yaml` — modprobe blacklist for CVE-2026-43284 + CVE-2026-43500 ("DirtyFrag" page-cache write LPEs in xfrm-ESP / RxRPC). Also drops the page cache to evict any DirtyFrag-poisoned pages. Same TEMPORARY lifecycle as algif-mitigation.

### NodePool Def File Schema

`modules/nodepools/defs/*.yaml` supports three top-level forms (detected by the generator):

**`nodepool:`** — single instance type (one NodePool from one file). Currently unused; most "single-instance" pools are written as a `fleet:` with one entry instead.
```yaml
nodepool:
  name: example
  instance_type: c7i.16xlarge
  arch: amd64                  # auto-detected if omitted
  node_disk_size: 100
  gpu: true
  has_nvme: true
  topology_manager_policy: single-numa-node   # default best-effort
  topology_manager_scope: pod                 # default container
  baremetal: true                             # uses NODEPOOLS_BAREMETAL_CONSOLIDATE_AFTER (1h)
  exclude_regions: [us-west-1]
  capacity_type: on-demand                    # or "reserved" for Capacity Blocks
  capacity_reservation_ids: [cr-xxxx]         # only with capacity_type: reserved
  user_data_script: scripts/h100-node-setup.sh # optional shell MIME part
  node_compactor: true                        # per-def override of cluster default
```

**`fleet:`** — multi-instance fleet (one NodePool per instance, all sharing a `node-fleet` label/taint). Used by every current def — including single-instance GPU pools (`p4d.yaml`, `nodepools-h100/defs/p5.yaml`, `nodepools-b200/defs/p6.yaml`), which are fleets with one instance entry.
```yaml
fleet:
  name: g5
  arch: amd64
  gpu: true
  exclude_regions: [us-west-1]
  instances:
    - type: g5.48xlarge
      weight: 100              # Karpenter prefers higher weights
      node_disk_size: 600
      has_nvme: true
      baremetal: true          # per-instance optional
```

**`fleets:`** — multi-fleet file (several fleets in one YAML). Detected by the generator but unused in the current defs.

**`-large` companion fleets** (dual-fleet pattern): full-node 8-GPU runners (`l-bx86iavx512-88-1000-a100-8`, `l-bx86iamx-176-1800-h100-8`, `l-bx86iamx-176-1800-b200-8`) override `node_fleet` to point at dedicated `-large` companion pools: `p4d-large`, `p5-large`, `p6-b200-large` (plus CPU equivalents `c7i-large`, `c7a-large`, `r7a-large`, `m7g-metal`, `m8g-large`, `g4dn-metal`, `g5-large`). The companion fleets deliberately do **NOT** set `single-numa-node` because an 8-GPU pod spans both NUMA nodes and would hit `TopologyAffinityError` under `single-numa-node`. The packed multi-tenant pool keeps the strict NUMA policy; the `-large` companion stays at `best-effort`.

NUMA defaults: `topologyManagerPolicy: best-effort` for everything by default. Only the packed GPU pools `p4d`, `p5`, `p6` pin `single-numa-node` with `pod` scope. Their `-large` companions stay at `best-effort` so the whole-node 8-GPU runner can span both NUMA nodes.

**Not in the schema**: there is no `bucket`, `max_pods`, `pod_cidr_buckets`, or per-AZ fan-out. EC2NodeClasses are emitted without `spec.kubelet.maxPods` (kubelet uses the AWS-stock default per `ENI_MAX_PODS`). An IPv4 + Custom Networking bucket scheme was considered (`INCREASE_IPV4.md`) but the codebase moved to IPv6-only EKS in PR #576; do not add these fields — they will be silently ignored by the generator.

Generated YAMLs contain `CLUSTER_NAME_PLACEHOLDER` — `deploy.sh` does `sed` replacement at apply time with the actual cluster name.

### Runner Def File Schema

`modules/arc-runners/defs/*.yaml` (and `arc-runners-h100/defs`, `arc-runners-b200/defs`):
```yaml
runner:
  name: l-x86iamx-22-225-h100   # ~42 char limit (see docs/runner_naming_convention.md)
  instance_type: p5.48xlarge    # determines node-fleet name (split on ".")
  node_fleet: g5-48xlarge       # optional explicit fleet name; defaults to instance_type.split(".")[0]
  vcpu: 22                      # CAPACITY_AWARE_WORKFLOW_CPU on listener
  memory: 225Gi                 # CAPACITY_AWARE_WORKFLOW_MEMORY
  disk_size: 200                # Gi (per workflow pod)
  gpu: 1                        # 0 for CPU runners
  proactive_capacity: 1         # optional warm pool size (default 0). Reduced fleet-wide in PR #772; most CPU defs now sit at 0-5.
  max_burst_capacity: 250       # optional cap on listener burst (default 0 = unlimited)
  hud_failure_base_capacity: 30 # optional additive floor for HUD-failure fallback (default 0, clamped [0,1000], warn >100). Surge formula: ProactiveCapacity*HUDFailureMultiplier + HUDFailureBaseCapacity. Not capped by clusters.yaml; def value applies in all clusters.
  max_runners: 8                # optional concurrency cap; omit for unlimited (Karpenter scales). May also be a dict: { default: 8, arc-cbr-production-uw1: 48 } — `default` key is mandatory; per-cluster keys override.
  runner_group: default         # GitHub runner group; forced to default for repo-scoped URLs. Cluster-level `arc-runners.runner_group` in clusters.yaml overrides per-def values.
  runner_class: ""              # optional isolation label (e.g. "release")
```

Use `node_fleet` when a runner needs its own dedicated fleet name distinct from the instance family (e.g. to pin the 8-GPU runner to a `-large` companion pool, or to isolate release-class runners). The reserved name `c7i-runner` is rejected — it's the legacy default fleet for plain `c7i.*` runners and cannot be reused as an override (`scripts/python/fleet_naming.py:RESERVED_NODE_FLEET_NAMES`). `derive_fleet_name(instance_type, override)` enforces the DNS-1123 label format on overrides.

Validation: `max_runners` must be a positive int OR a dict containing a `default` key with positive-int values per cluster id (`generate_runners.py:resolve_max_runners`); `max_burst_capacity` must be non-negative; `max_burst_capacity < proactive_capacity` (or `< hud_failure_base_capacity`) is an error, not a warning, and aborts the generate step. `node_fleet`, if present, must be a non-empty string with no leading/trailing whitespace.

**Cluster-wide overrides** (`generate_runners.py`):
- `proactive_capacity_max: <int>` at the cluster level clamps every def's `proactive_capacity` down to that value. Currently only the two staging clusters (`meta-staging-aws-uw1`, `meta-staging-aws-ue1`) set it (to `0`); all prod clusters dropped the override in PR #771 and rely on per-def values reduced in PR #772.
- `pause_runners: true` at the cluster level forces `max_runners=0` and `hud_failure_base_capacity=0` on every scale set — cluster-wide drain switch.
- Region-exclusion auto-zeroing: if a runner's `instance_type` is in the cluster's `excluded_instance_types` (derived from the backing nodepool/fleet's `exclude_regions`, e.g. `us-west-1` for A100/g5), `max_runners`, `proactive_capacity`, and `hud_failure_base_capacity` are forced to 0 so GitHub does not route jobs that would pend forever.
- `arc-runners.runner_group: <name>` at the cluster level pins every scale set's GitHub runner group, unless the cluster's `github_config_url` is repo-scoped — repo-scoped URLs force `default` regardless. Per-cluster groups: `arc-cbr-production-uw1` -> `arc-cbr-prod-uw1`, `meta-prod-aws-ue1` -> `meta-prod-aws-ue1`, `lf-prod-aws-ue1`/`ue2` -> `lf-prod-aws-ue1`/`ue2`, staging -> `meta-staging-aws-uw1`/`ue1`. `arc-cbr-production` (us-east-2) does NOT set `runner_group` and runners land in `default`.

### Cache-Enforcer (DaemonSet)

Uses `xt_string` kernel module to match domain names in TLS ClientHello SNI (port 443) and HTTP Host header (port 80). Rules in CACHE_ENFORCER iptables chain, jumped from OUTPUT and FORWARD. REJECT with tcp-reset (fast "Connection refused").

**Blocked registries** (forced through Harbor at harbor:30002): docker.io, registry-1.docker.io, auth.docker.io, production.cloudflare.docker.com, ghcr.io, nvcr.io, quay.io, registry.k8s.io

**Blocked PyPI** (REJECTed at iptables OUTPUT/FORWARD — the runner pod's pip env vars redirect to pypi-cache instead): pypi.org, files.pythonhosted.org. `download.pytorch.org` is NOT in `PYPI_DOMAINS` — workflows can still reach it directly. Runner pod `PIP_INDEX_URL` / `UV_DEFAULT_INDEX` env vars point at `http://pypi-cache-cpu.pypi-cache.svc.cluster.local:8080/simple/` (Service DNS, not localhost).

**NOT blocked**: public.ecr.aws (no rate limits)

To add/remove blocked domains: edit `REGISTRY_DOMAINS` or `PYPI_DOMAINS` in `modules/cache-enforcer/kubernetes/configmap.yaml`, then `just deploy-module <cluster> cache-enforcer`.

**Limitation**: TLS Encrypted Client Hello (ECH) encrypts SNI, bypassing xt_string matching. No blocked domains currently use ECH. Migration path: Cilium CNI with toFQDNs DNS-based policies.

**Dependency**: cache-enforcer depends on Harbor (base) and pypi-cache module. Without pypi-cache deployed, pip installs fail entirely (traffic blocked but no cache to serve it).

### Node Compactor — Karpenter Integration

Lives at `base/node-compactor/` (NOT under `modules/`). Cluster-level default `node_compactor.enabled: true` in `clusters.yaml`. Per-def override via `node_compactor: true|false` in a NodePool def.

- NodePools labeled `osdc.io/node-compactor: "true"` are auto-discovered
- Compactor taints nodes `NoSchedule` (no eviction of running pods)
- Relies on Karpenter's `WhenEmpty` consolidation policy with `consolidateAfter: 2m` to delete empty tainted nodes
- On SIGTERM, compactor removes all its taints before exiting — redeploying causes temporary burst of untainted nodes
- Burst absorption: when pending pods match tainted nodes (checks tolerations, nodeSelector, affinity, resource fit), compactor temporarily removes taints. Fleet cooldown (default 900s) blocks new taints after burst untaint
- Anti-flap: per-cluster `node_compactor.min_node_age_seconds` (e.g. `arc-cbr-production` sets 900) — newly-launched nodes are exempt from tainting for this many seconds, avoiding a churn loop when Karpenter has just provisioned them for incoming workload

### Runner Pod Two-Tier Resource Model

- **Runner pod** (750m CPU, **1Gi memory**) — lightweight ARC orchestrator; mounts hook ConfigMap. Bumped from 512Mi to 1Gi to give native Node.js stdio buffers (held open by slow CRI exec during pod-density bursts) headroom — observed OOMs traced back to native buffers, not V8 heap. `.NET` runner caps via `DOTNET_GCHeapHardLimit=C800000` (200 MiB), Node hooks via `NODE_OPTIONS=--max-old-space-size=128`. Resources are fixed in `templates/runner.yaml.tpl`, NOT in def files. Listener `CAPACITY_AWARE_RUNNER_CPU/MEMORY` env vars MUST stay in sync with these.
- **Job pod** (resources from def file `vcpu`/`memory`/`disk_size`/`gpu`) — runs actual workflow containers, gets git cache volume.
- Min runners 0; runner scaling is unlimited unless `max_runners` is set in the def. Prefer overspend over outage — only cap fixed-capacity reserved pools (e.g. H100/B200 Capacity Blocks).

**Init container `wait-for-hooks`**: every runner pod runs an Alpine init container that polls `/mnt/host-hooks/dist/index.js` (placed by the `runner-hooks-warmer` DaemonSet in `arc-runners` namespace) for up to 300s, then snapshots `dist/` + `.version` into an emptyDir consumed by the main runner. This is a hard scheduling-gate dependency: nodes without the warmer DaemonSet ready cannot run jobs. Remove when upstream merges the patched hooks.

**Four-tier PriorityClass ladder** (in `modules/arc/kubernetes/priority-classes.yaml`):
- `-10` `placeholder-runner` — proactive runner-capacity placeholders; `preemptionPolicy: Never`
- `0` `arc-runner` — actual runner pods; preempt `placeholder-runner` only
- `10` `placeholder-workflow` — proactive workflow-capacity placeholders; `preemptionPolicy: Never`
- `20` `arc-workflow` — actual workflow (job) pods; preempt `placeholder-workflow`

The pairing ensures workflow pods can claim the capacity reserved by the placeholders they replace, without runner pods clobbering workflow placeholders. Changing any value breaks the proactive-capacity preemption ladder.

**Listener metrics cardinality**: `listenerMetrics` block in the runner template enumerates an allowlist of labels per metric (counters, gauges, histograms) — strips `job_name`, `event_name`, `job_workflow_ref`, `job_workflow_target` which create unbounded series. Keeps `repository`, `organization`, `enterprise`, `job_workflow_name`, `name`, `namespace`, `result`. Required for Grafana Cloud billing predictability; do not add labels without a billing review.

### ARC Module Split

- `modules/arc/` = controller + namespaces (`arc-systems`, `arc-runners`) + runner ServiceAccount + LimitRange + hooks ConfigMap + **`runner-hooks-warmer` DaemonSet** (moved here from `arc-runners` in PR #746 — still runs in the `arc-runners` namespace but lifecycle is now owned by the `arc` module) + four-tier PriorityClass ladder + capacity-monitor RBAC. **No terraform** — pure k8s/helm. Uses fork chart `oci://ghcr.io/jeanschmidt/actions-runner-controller-charts/gha-runner-scale-set-controller`, version pinned in `clusters.yaml` `arc.chart_version` (currently `0.14.1-jeanschmidt.16`). Controller and runner chart versions MUST match (minor mismatch deletes ARS). The cluster-wide containerd registry mirror lives at `base/kubernetes/registry-mirror-config.yaml` — NOT inside the arc module.
- `modules/arc-runners/` = runner scale sets (the actual runners). Separate deploy cycle. Reads `arc-runners.{github_config_url,github_secret_name,runner_name_prefix,runner_group}` from `clusters.yaml`.
- `modules/keda/` = KEDA operator chart (`kedacore/keda`, version pinned via `keda.chart_version`, currently `2.16.1`). Required for BuildKit autoscaling. Deployed on every cluster that enables `buildkit.autoscaling.enabled: true` — currently `meta-staging-aws-uw1`, `meta-staging-aws-ue1`, `arc-cbr-production`, `lf-prod-aws-ue1`, `lf-prod-aws-ue2` (added in PR #775). Does not directly affect runners/nodepools, but the KEDA ScaledObjects in `modules/buildkit/generated/autoscaling.yaml` require this module to be deployed first.

## GPU Nodepools

Three NVIDIA GPU node families are supported, each with a unified single-fleet runner family providing 1/2/4/8-GPU splits via `nvidia.com/gpu` resource requests (NUMA single-numa-node policy).

| GPU | Instance | Module | Capacity model | Notes |
|-----|----------|--------|----------------|-------|
| **A10G** | g5.* (multi-instance fleet: 48xl/12xl/24xl/8xl/16xl, weight-ordered) | `nodepools` | on-demand spot/OD | excludes us-west-1 |
| **L4** / T4 / older | g6, g4dn fleets | `nodepools` | on-demand | shared `nodepools` defs |
| **A100 40GB SXM4** | p4d.24xlarge | `nodepools` (`p4d.yaml` `fleet:` + companion `p4d-large.yaml`) | **on-demand** (no Capacity Block) | excludes us-west-1; packed runners `l-x86iavx512-{11-125-a100, 22-250-a100-2, 44-500-a100-4}` on the `p4d` fleet (single-numa-node), `l-bx86iavx512-88-1000-a100-8` pinned via `node_fleet: p4d-large` (best-effort) |
| **H100 80GB SXM5** | p5.48xlarge | **`nodepools-h100`** (`p5.yaml` + `p5-large.yaml`) + **`arc-runners-h100`** | **AWS Capacity Blocks** (`reserved`; reservation IDs are per-cluster under `clusters.<id>.nodepools-h100.capacity_reservation_ids` — e.g. us-east-2 single `cr-0c3f05dffb85ed832`, us-west-1 dual `cr-04d3d1d84e127a562` + `cr-09a53051589034fb8` for 48 H100s total) | All H100 defs use the per-cluster max_runners dict form. Default values (sized for one 8-GPU node): 1-GPU=8, 2-GPU=4, 4-GPU=2, 8-GPU=1. `arc-cbr-production-uw1` overrides: 1-GPU=48, 2-GPU=24, 4-GPU=12, 8-GPU=6 (sized for the 6 reserved nodes). 8-GPU def pins `node_fleet: p5-large`. Packed `p5.yaml` uses `user_data_script: scripts/h100-node-setup.sh` |
| **B200** | p6-b200.48xlarge | **`nodepools-b200`** (`p6.yaml` + `p6-b200-large.yaml`) + **`arc-runners-b200`** | **AWS Capacity Blocks** (`reserved`, single ID `cr-0b15c0b3163f09d26` in us-east-2) | All B200 defs use plain-int max_runners (no per-cluster dict): 1-GPU=8, 2-GPU=4, 4-GPU=2, 8-GPU=1. 8-GPU def pins `node_fleet: p6-b200-large`. Packed `p6.yaml` uses `user_data_script: scripts/b200-node-setup.sh` |

A100 nodes scale fully with workload (no warm floor — first job after consolidation pays a 5-10 min cold start). H100/B200 nodes are reserved capacity, so concurrency is capped to the reservation.

## Modular Submodule Pattern (GPU pools)

`nodepools-h100`, `nodepools-b200`, `arc-runners-h100`, `arc-runners-b200` are thin shims that **delegate to the base modules** (`nodepools`, `arc-runners`) with overridden defs/output paths. New GPU SKUs can be added as new sibling modules without touching the base.

`modules/nodepools-h100/deploy.sh`:
```bash
UPSTREAM_ROOT="${OSDC_UPSTREAM:-$(cd "$MODULE_DIR/../.." && pwd)}"
export NODEPOOLS_DEFS_DIR="$MODULE_DIR/defs"
export NODEPOOLS_OUTPUT_DIR="$MODULE_DIR/generated"
export NODEPOOLS_MODULE_NAME="nodepools-h100"
exec "$UPSTREAM_ROOT/modules/nodepools/deploy.sh" "$@"
```

`modules/arc-runners-h100/deploy.sh` does the same with `ARC_RUNNERS_DEFS_DIR`, `ARC_RUNNERS_OUTPUT_DIR`, `ARC_RUNNERS_MODULE_NAME`. The base `nodepools/deploy.sh` and `arc-runners/deploy.sh` honor these env vars (with fallback defaults to `$MODULE_DIR/defs`, etc.).

When adding a new GPU SKU, copy one of the `*-h100` modules, swap the def files and reservation ID, register it in `clusters.yaml` `modules:`. Do NOT modify the base `nodepools` or `arc-runners` defs/templates.

### Monitoring Component Placement

- node-exporter tolerates ALL taints (runs on every node)
- DCGM exporter: only on GPU nodes (nodeAffinity on `nvidia.com/gpu.present`)
- All other monitoring components: base infrastructure nodes (tolerate `CriticalAddonsOnly`)

### Karpenter Module = Controller Only

The `modules/karpenter/` module handles the controller only. NodePools are managed by `modules/nodepools/` (and the GPU shims). Karpenter terraform creates: IAM Role (IRSA), SQS Queue (spot/rebalance/health events), four EventBridge Rules (`spot_interruption`, `rebalance`, `instance_state_change`, `scheduled_change` — the last from AWS Health), and discovery tags (`karpenter.sh/discovery`) on cluster SG and private subnets.

---

Verified against commit `c1c884b` (IPv6-only EKS, runner-image per-commit digest pinning via `resolve_runner_version.py`, runner-container-hooks v0.8.15 placed by warmer DaemonSet owned by `arc` module, ARC fork chart `0.14.1-jeanschmidt.16`, keda `2.16.1` enabling BuildKit autoscaling on staging + arc-cbr-production + lf-prod-aws-ue1/ue2, proactive_capacity reductions PR #772 and removal of `proactive_capacity_max` from prod PR #771, integration-test job filtering by cluster module availability PR #774 with cluster-specific runner groups PR #773).
