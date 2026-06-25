---
name: osdc-harbor
description: >
  Harbor pull-through cache configuration, Helm chart gotchas (inconsistent value paths,
  per-component image overrides, taint tolerations), and image mirroring strategy.
  Applies to ~/meta/ci-infra/osdc.
  Load when working on Harbor, image mirroring, or container registry configuration.
---

# OSDC Harbor Configuration

## Harbor is Always-On

Harbor is baked into `base/`, not a module. Every cluster needs a pull-through cache. It caches docker.io, ghcr.io, nvcr.io, registry.k8s.io, quay.io, and public.ecr.aws (six proxy projects: `dockerhub-cache`, `ghcr-cache`, `ecr-public-cache`, `nvcr-cache`, `k8s-cache`, `quay-cache`). Containerd on every node is configured to route pulls through Harbor at `harbor:30002` (see `base/scripts/bootstrap/eks-base-bootstrap.sh`).

### NodePort Exposure (port 30002) and the `harbor` /etc/hosts hostname

Harbor uses `expose.type: nodePort` with `nodePort: 30002`. The kube-proxy NodePort means every node can reach Harbor on its own routable IP. This is the heart of the "every node can reach Harbor" design.

All in-cluster clients address Harbor as `harbor:30002`, NOT `localhost:30002` / `127.0.0.1:30002` / `[::1]:30002`. The `harbor` hostname is resolved per-node via `/etc/hosts` to that node's primary IPv6 address (the routable one — the same address kube-proxy listens on for NodePort traffic).

**Why not `[::1]:30002`?** Under IPv6-only EKS, kube-proxy opens NodePort listeners on `[::]:30002` (single-stack IPv6) but **explicitly excludes `::1` (loopback) from NodePort routing.** Traffic destined for `::1` never reaches the kube-proxy listener — connections fail. The node's primary IPv6 IS routed to the kube-proxy listener, so addressing the node by its own routable IP works.

**Why not `localhost`/`127.0.0.1`?** Under IPv6-only EKS, kube-proxy does not bind any IPv4 listener for NodePort. The harbor-helm chart (v1.18.2) does not render `ipFamilyPolicy` / `ipFamilies` on the NodePort branch of `templates/nginx/service.yaml` (only the ClusterIP branch supports it), so the chart-level dual-stack workaround is unavailable.

**Per-node `/etc/hosts` substitution (the `harbor` hostname)**: each node writes a line `<node primary IPv6> harbor # managed-by: osdc-harbor-mirror` to `/etc/hosts`. The write is **idempotent** (re-runs delete the marker line first, then re-add). The primary writer is a DaemonSet that runs on every node; two additional writers exist for pre-DaemonSet timing windows.

- **DaemonSet on every node (base + Karpenter)**: `base/kubernetes/registry-mirror-config.yaml` runs on ALL nodes (no nodeSelector, `tolerations: - operator: Exists`). Its init container reads node IPv6 from the Downward API (`status.hostIP`) and writes via a hostPath mount of `/etc/hosts`. Notable details: (a) `hostNetwork: true`, `hostPID: true`, `priorityClassName: system-node-critical`; (b) init container runs `public.ecr.aws/amazonlinux/amazonlinux:2023`, sleeper container runs `public.ecr.aws/docker/library/alpine:3.19` (both upstream-ECR-public — pulled directly, never through Harbor, since this DaemonSet is what makes Harbor reachable); (c) `HARBOR_PORT` env var = `"30002"` (single source of truth for the port — `/etc/containerd/certs.d/harbor:${HARBOR_PORT}/hosts.toml` is written under this exact directory name); (d) `updateStrategy.rollingUpdate.maxUnavailable: "50%"` — not the K8s default of `1`, picked so a single stuck Terminating pod (e.g. on a Karpenter-disrupted node) can't gate the rest of the fleet from receiving a new config revision; (e) writes a marker file `/var/lib/registry-mirror-config/.configured` so re-runs short-circuit when the six per-registry hosts.toml files are already present; (f) on successful init, removes the per-DS startup taint `node-init.osdc.io/registry-mirror` (declared in `MODULE_STARTUP_TAINTS` in `modules/nodepools/scripts/python/generate_nodepools.py`) so workload pods can schedule on the node — idempotent no-op when the taint isn't there.
- **Base infra bootstrap (pre-DaemonSet)**: `base/scripts/bootstrap/eks-base-bootstrap.sh` writes the same entry from IMDS (`/latest/meta-data/ipv6` after an IMDSv2 token). Runs from launch-template userData before kubelet starts, so the DaemonSet hasn't landed yet. The DS later overwrites idempotently.
- **H100/B200 GPU userData (pre-DaemonSet)**: `modules/nodepools-h100/scripts/h100-node-setup.sh` and `modules/nodepools-b200/scripts/b200-node-setup.sh` read from IMDS. EC2NodeClass userData runs BEFORE kubelet — needed because kubelet pulls the GPU device plugin image during startup and the DaemonSet hasn't started yet.

Containerd hosts.toml entries use `[host."http://harbor:30002/v2/<project>"]`. K8s pod `image:` fields baked from `IMAGE="..."` in deploy.sh scripts (zombie-cleanup, harbor-cache-recovery, node-compactor, image-cache-janitor) use `harbor:30002/osdc/<repo>:<tag>`.

**Direct `harbor:30002` plain-HTTP entry**: `registry-mirror-config.yaml` also writes `/etc/containerd/certs.d/harbor:30002/hosts.toml` with `server = "http://harbor:30002"`. Without it, containerd defaults to HTTPS for the `harbor:30002` host and fails with `http: server gave HTTP response to HTTPS client` — Harbor's NodePort only serves plaintext HTTP. This is what makes the `harbor:30002/osdc/<repo>:<tag>` references resolve cluster-internally.

**Push side (devmac via port-forward) is different**: deploy.sh scripts push to Harbor via `kubectl port-forward svc/harbor 8081:80` and `crane push localhost:8081/osdc/<repo>:<tag>`. Harbor stores by project + repo + tag — the hostname is just where to talk to Harbor. Pushes from devmac (`localhost:8081`) and pulls on nodes (`harbor:30002`) reach the same artifact.

`base/helm/harbor/values.yaml` keeps `externalURL: http://harbor:30002` because the chart only uses it for `EXT_ENDPOINT` (callback URL construction for OAuth and similar flows that the cache-only deployment does not exercise). The in-cluster `harbor` hostname is resolvable via the per-node `/etc/hosts` substitution described above.

## Harbor Helm Chart Gotchas

The Harbor chart (v1.18.2) has several gotchas:

- **No `global.imageRegistry`**: Each component's image must be overridden individually via `--set core.image.repository=...`, `--set registry.registry.image.repository=...`, etc. The deploy recipe sets nine such overrides.
- **Nested vs top-level values**: Most components use top-level paths (`core.tolerations`, `registry.tolerations`), but `redis` and `database` require values under `.internal` (`redis.internal.tolerations`, `database.internal.tolerations`). Likewise images: `redis.internal.image.repository`, `database.internal.image.repository`, `registry.registry.image.repository`, `registry.controller.image.repository`.
- **All base-infrastructure nodes are tainted** with `CriticalAddonsOnly=true:NoSchedule`. Every Harbor component (including nginx, exporter) must have matching tolerations or pods will be unschedulable. The values file defines two YAML anchors applied across every component: `*base-tolerations` (operator `Equal`, value `"true"`) and `*base-affinity` (`nodeAffinity.preferredDuringScheduling… role=base-infrastructure`). The toleration alone would also permit GPU/runner nodes — the affinity is what keeps Harbor pods sticky to base-infra.
- **`updateStrategy.type: Recreate`** is set globally because jobservice and registry are Deployments backed by RWO EBS PVCs. RollingUpdate causes Multi-Attach errors when the old and new pods land on different nodes — EBS volumes can only attach to one node at a time.
- **`harborAdminPassword` only takes effect on first install.** On upgrades, the database still holds the previous password (or the chart default `Harbor12345`). The deploy recipe handles this by running a manual API password migration after every upgrade: `PUT /api/v2.0/users/1/password` against `localhost:8090` via port-forward, using `admin:Harbor12345` as the auth. The call is idempotent — HTTP 200 = default was still active, just changed it; HTTP 401 = default no longer accepted (already migrated to the K8s-secret value, treated as success).
- **Input-hash Helm strategy**: the chart uses `randAlphaNum`/`genCA` template functions, producing non-deterministic output. The deploy uses `helm_upgrade_by_input_hash` (in `scripts/helm-upgrade.sh`) which hashes the inputs sent TO Helm rather than the rendered templates. Standard `helm template` diffing would false-positive on every deploy. Set `HELM_FORCE_UPGRADE=1` to bypass.
- **Helm upgrade retry loop**: the recipe wraps `helm_upgrade_by_input_hash` in a 3-attempt loop with 30s backoff between attempts. Failures during the first two attempts are logged and retried; only the third failure aborts. Expect to see multiple "attempt N/3" lines on transient Helm/API errors.
- **Trivy disabled**: `trivy.enabled: false` — this is a cache-only deployment, no scanning needed.
- **Built-in cache enabled**: `cache.enabled: true, expireHours: 24`. Prometheus scraping enabled via `metrics.enabled: true` (consumed by the monitoring module's harbor ServiceMonitor).
- **S3 image-pull redirect enabled** (`persistence.imageChartStorage.s3.disableredirect: false`): registry returns 307 redirects so clients pull layer blobs directly from S3, offloading bandwidth from Harbor registry pods.

## Harbor Namespace, ServiceAccount, and Secrets (created by the recipe, not the chart)

The `_deploy-harbor` justfile recipe does several things outside Helm before/around the upgrade:

- **Namespace**: `harbor-system` is created by `base/kubernetes/harbor-namespace.yaml` (referenced from `base/kubernetes/kustomization.yaml`). The recipe also runs `kubectl create namespace harbor-system 2>/dev/null || true` defensively, and Helm is invoked with `--create-namespace`. Three layers, all idempotent.
- **`harbor-registry` ServiceAccount**: created manually by the recipe before the Helm upgrade. The Harbor chart only sets `serviceAccountName` on the pod spec but does NOT create the SA itself. Helm is then passed `--set registry.serviceAccountName=harbor-registry --set registry.automountServiceAccountToken=true`. The SA carries the IRSA annotation for S3 (see static-IAM-user section).
- **`harbor-admin-password` secret**: auto-generated on first deploy (`openssl rand`), stored in `harbor-system`, passed to Helm as `--set harborAdminPassword=`. Read back on subsequent deploys.
- **`harbor-db-password` secret**: also auto-generated separately on first deploy, stored in `harbor-system`, passed to Helm as `--set database.internal.password=`. Distinct from the admin password.

## Per-cluster Harbor knobs (clusters.yaml)

The deploy reads these from `clusters.yaml` under `harbor.*` (defaults in parens):

- `harbor.core_replicas` (2)
- `harbor.registry_replicas` (2)
- `harbor.nginx_replicas` (3)
- `harbor.pdb_max_unavailable` (1) — accepts integer or `"<N>%"` string; `meta-staging-aws-uw1` and `meta-staging-aws-ue1` use `"100%"` (regex `^([1-9][0-9]*|[1-9][0-9]?%|100%)$`).

## Static IAM User for S3 (IRSA Workaround)

The Harbor S3 driver (`goharbor/distribution`) does NOT support IRSA — its AWS credential chain is hardcoded and does not understand web-identity tokens. As a workaround, `modules/eks/terraform/modules/harbor/main.tf` provisions BOTH:

1. An IAM **role** (`<cluster_name>-harbor-registry`) attached to the `harbor-registry` ServiceAccount via the `eks.amazonaws.com/role-arn` annotation (kept around in case the upstream driver gains IRSA support).
2. An IAM **user** (`<cluster_name>-harbor-s3`) with a static access key. The deploy creates a Kubernetes secret `harbor-s3-credentials` containing `REGISTRY_STORAGE_S3_ACCESSKEY` / `REGISTRY_STORAGE_S3_SECRETKEY`, and Helm is given `persistence.imageChartStorage.s3.existingSecret=harbor-s3-credentials`.

If you ever migrate off the static credentials, the IAM user, the access key, and the secret all need to go together — and the upstream driver must support IRSA first.

**S3 dualstack endpoint (IPv6-only EKS requirement)**: the deploy passes `--set persistence.imageChartStorage.s3.regionendpoint="https://s3.dualstack.${AWS_REGION}.amazonaws.com"`. The default S3 endpoint is IPv4-only; without the dualstack override, registry pods cannot reach S3 from IPv6-only EKS.

## Image Mirroring

`modules/eks/images.yaml` contains **only bootstrap images** — the Harbor components themselves, which must be mirrored to ECR because Harbor cannot cache its own images. Once Harbor is running, all other images are pulled through Harbor's proxy cache.

Images from `public.ecr.aws` are NOT mirrored to ECR (no rate limits to worry about) but ARE still proxied through Harbor via the `ecr-public-cache` project, so containerd routing remains uniform.

### NodeLocal DNSCache image (lazy proxy-cache pull)

`registry.k8s.io/dns/k8s-dns-node-cache` (the NLD DaemonSet image, currently tag `1.26.8` in `base/kubernetes/nodelocaldns/daemonset.yaml`) is **NOT pre-mirrored to ECR**. It flows through the standard Harbor `k8s-cache` proxy project on first pull. The DaemonSet is `system-node-critical` and tolerates a brief startup race on cold Karpenter nodes (~30-60s ImagePullBackOff while Harbor lazy-pulls from upstream and caches the layers). Accepted trade-off vs. building a separate runtime-image mirror pipeline.

## Harbor Project Configuration

`scripts/python/configure_harbor_projects.py` configures Harbor proxy cache projects via the Harbor API. It sets up the six proxy cache registries (docker.io, ghcr.io, ecr-public, nvcr.io, registry.k8s.io, quay.io) that Harbor uses for pull-through caching.

**Seventh project `osdc` — bootstrapped lazily by push-side deploy.sh scripts**: each of `modules/zombie-cleanup/deploy.sh`, `modules/harbor-cache-recovery/deploy.sh`, `base/node-compactor/deploy.sh`, and `base/kubernetes/image-cache-janitor/deploy.sh` creates the `osdc` project via `POST /api/v2.0/projects` (HTTP 201 = created, 409 = already exists, both treated as success). This is what allows internally-built images to be pushed to `harbor:30002/osdc/<repo>:<tag>`; `configure_harbor_projects.py` does NOT create it.

**Optional authenticated upstream credentials**: if either `harbor-dockerhub-credentials` or `harbor-github-credentials` secret exists in `harbor-system`, the recipe passes `--dockerhub-username/token` / `--github-username/token` to `configure_harbor_projects.py`, so the docker.io / ghcr.io proxy-cache endpoints use authenticated upstream pulls (avoids anonymous rate limits). Missing secrets = anonymous upstream (acceptable for low-volume clusters).

## Harbor PodDisruptionBudgets

Three PDBs cover the multi-replica components only: `harbor-core`, `harbor-registry`, `harbor-nginx`. Single-replica components (`jobservice`, `portal`, `exporter`, internal `redis`/`database`) are intentionally NOT PDB-protected — a `minAvailable: 1` PDB on a 1-replica Deployment deadlocks node drains.

- Single per-cluster knob: `harbor.pdb_max_unavailable` in `clusters.yaml`. Default `1` (conservative). `meta-staging-aws-uw1` and `meta-staging-aws-ue1` use `"100%"` (string, quoted) to effectively disable protection (staging runs 1 replica each).
- Chart has no native PDB support — standalone manifests templated from `base/kubernetes/harbor/pdb.yaml.tpl`, sed-substituted (`__MAX_UNAVAILABLE__`) and applied by the `_deploy-harbor` justfile recipe via `kubectl_apply_if_changed` after the Helm install completes.
- Selectors: `app: harbor`, `component: <core|registry|nginx>`, `release: harbor` — mirror the chart's per-component Deployment selectors.
- Known limits: (a) not active during first install (applied after Helm completes); (b) does NOT block `Recreate`-strategy rollouts of registry/jobservice (PDBs only block eviction-API calls); (c) orphaned on `helm uninstall` (Harbor is base infra, never uninstalled in practice).

## Key Files

| File | Purpose |
|------|---------|
| `base/helm/harbor/values.yaml` | Harbor Helm values (anchors, NodePort, tolerations, RWO Recreate strategy, Trivy off, cache on) |
| `base/kubernetes/harbor/pdb.yaml.tpl` | PodDisruptionBudget template for core/registry/nginx (sed-substituted by `_deploy-harbor`, no native chart PDB support) |
| `modules/eks/terraform/modules/harbor/main.tf` | S3 bucket + IAM role (IRSA) + IAM user (static-keys workaround) |
| `modules/eks/images.yaml` | Bootstrap images to mirror to ECR |
| `modules/harbor-cache-recovery/` | Scheduled CronJob: scans pod container statuses for `ImagePullBackOff`/`ErrImagePull` with cache-corruption indicator messages and purges the affected Harbor proxy-cache repositories. Defaults: `schedule: "*/5 * * * *"`, `concurrencyPolicy: Forbid`, `backoffLimit: 0`, `activeDeadlineSeconds: 300`, resources `requests: cpu 50m / memory 1Gi`, `limits: cpu 200m / memory 2Gi`. Per-cluster overrides via `harbor_cache_recovery.{enabled,schedule,min_pod_age_seconds,dry_run,harbor_url}` in `clusters.yaml` (default `harbor_url`: `http://harbor.harbor-system.svc.cluster.local:80`). Memory bumped to 1Gi/2Gi in PR #504; deadlines/scheduling tuned in PR #521 to fix `DeadlineExceeded` under cluster load. |
| `scripts/python/configure_harbor_projects.py` | Harbor proxy cache project setup |
| `scripts/helm-upgrade.sh` | `helm_upgrade_by_input_hash` helper used by the Harbor deploy |
| `base/scripts/bootstrap/eks-base-bootstrap.sh` | Base infra node bootstrap: writes `<node IPv6> harbor` to /etc/hosts (IMDS lookup) and containerd mirror configs pointing at `harbor:30002` |
| `base/kubernetes/registry-mirror-config.yaml` | DaemonSet that runs on EVERY node (base + Karpenter, `operator: Exists` tolerations, no nodeSelector): writes /etc/hosts entry (Downward API hostIP) and containerd mirror configs pointing at `harbor:30002` |
| `modules/nodepools-h100/scripts/h100-node-setup.sh`, `modules/nodepools-b200/scripts/b200-node-setup.sh` | GPU EC2NodeClass userData: writes /etc/hosts entry (IMDS lookup) and containerd mirror configs before kubelet starts (so the GPU device plugin pull succeeds) |

## Harbor Garbage Collection

Harbor GC removes orphaned metadata from the database (e.g., artifact records pointing to S3 objects that no longer exist). Triggered via the Harbor API, not kubectl.

**When to run GC:**
- Image pull failures with `MANIFEST_UNKNOWN` or `manifest unknown` errors across multiple images/registries
- Harbor registry logs show 404 for manifest digests that should exist
- Harbor core logs show `manifestcache.go: failed to push manifest` errors
- After S3 object loss (accidental deletion, storage corruption — note: lifecycle expiration is no longer a possible cause, see "S3 Storage" below)
- `harbor-cache-recovery` CronJob OOMKilling (suggests large number of orphaned entries)

**Diagnosis pattern** — Harbor proxy cache storage corruption looks like:
1. Pods stuck in `ImagePullBackOff` / `ErrImagePull` cluster-wide (not just one image)
2. Harbor registry returns `manifest unknown` for digest-based manifest GETs
3. Harbor core shows `"failed to push manifest referencing digest, tag: , digest: sha256:..."` with `MANIFEST_UNKNOWN`
4. Harbor DB has artifact metadata but S3 is missing the backing blob
5. Multiple registries affected (ghcr-cache, dockerhub-cache, quay-cache — not just one)

**Procedure (order matters):**

1. **Fix the root cause first** — restore deleted objects, etc.
2. **Run GC** — cleans orphaned DB records where S3 data is gone
3. **Rolling restart Harbor registry** — clears in-memory negative cache state
4. Pods start pulling successfully as Harbor re-fetches fresh manifests from upstream

```bash
# Get Harbor admin password
HARBOR_PASS=$(kubectl get secret harbor-admin-password -n harbor-system -o jsonpath='{.data.password}' | base64 -d)

# Port-forward to Harbor (nginx-fronted NodePort service — the same `svc/harbor`
# every deploy.sh script port-forwards). `svc/harbor-core` also works (it
# exposes the Harbor core API directly on port 80), but `svc/harbor` is the
# project-wide convention.
kubectl port-forward svc/harbor -n harbor-system 8080:80 &
PF_PID=$!
sleep 2

# Dry run first
curl -s -u "admin:$HARBOR_PASS" -X POST \
  http://localhost:8080/api/v2.0/system/gc/schedule \
  -H "Content-Type: application/json" \
  -d '{"parameters":{"dry_run":true},"schedule":{"type":"Manual"}}' | jq .

# Actual GC (after reviewing dry run)
curl -s -u "admin:$HARBOR_PASS" -X POST \
  http://localhost:8080/api/v2.0/system/gc/schedule \
  -H "Content-Type: application/json" \
  -d '{"parameters":{"dry_run":false,"delete_untagged":true},"schedule":{"type":"Manual"}}' | jq .

# Check GC status
curl -s -u "admin:$HARBOR_PASS" \
  http://localhost:8080/api/v2.0/system/gc \
  -H "Content-Type: application/json" | jq '.[0] | {id, job_status, creation_time, update_time}'

# Clean up
kill $PF_PID
```

**Rolling restart:**

```bash
kubectl rollout restart deployment/harbor-registry -n harbor-system
kubectl rollout status deployment/harbor-registry -n harbor-system --watch
```

## S3 Storage — No Lifecycle Expiration (DO NOT REINTRODUCE)

Harbor's S3 bucket (`${cluster_name}-harbor-registry`) stores cached manifests and blobs. The bucket MUST NOT have an object expiration lifecycle policy — Harbor manages its own cache via GC and proxy cache TTLs. An S3 lifecycle policy deleting objects behind Harbor's back causes DB-storage mismatches (DB has metadata, S3 has no data), which presents as cluster-wide `MANIFEST_UNKNOWN` pull failures.

**The lifecycle resource was REMOVED in PR #504 (commit 4ff8401, "Fix Harbor erratic behavior caused by S3 lifecycle expiration"). It is no longer present in `modules/eks/terraform/modules/harbor/main.tf` — the file now contains only the bucket, encryption, public-access block, IAM role, and IAM user. DO NOT reintroduce any `aws_s3_bucket_lifecycle_configuration` resource.** If you grep for one and don't find it, that is correct.

## Additional Notes

### images.yaml Location

Harbor bootstrap images (`images.yaml`) live in `modules/eks/`, NOT in `base/`. Don't look for or add them in `base/`.

### harbor-cache-recovery toleration asymmetry

The Harbor chart uses `operator: Equal, value: "true"` for the `CriticalAddonsOnly` taint. The `harbor-cache-recovery` CronJob (`modules/harbor-cache-recovery/kubernetes/cronjob.yaml`) uses `operator: Exists` (no value). Both are valid — the taint key matches either way — but the asymmetry is intentional, not a bug.
