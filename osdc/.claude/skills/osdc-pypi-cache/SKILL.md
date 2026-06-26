---
name: osdc-pypi-cache
description: >
  OSDC PyPI wheel cache module — per-CUDA-slug nginx+pypiserver fanout backed by shared EFS
  wheelhouse, fed by an external wheel-build pipeline via S3. Covers architecture (4 components),
  slug naming, EFS PVC, NVMe nginx cache, S3 bucket layout, needbuild override,
  prebuilt-cache.txt matrix invalidation, njs merge handler, /whl/ rewrites, NetworkPolicy,
  IRSA roles, log rotation, pod resource computation, cache-enforcer SNI matching.
  Applies to ~/meta/ci-infra/osdc.
  Load when developing, debugging, or maintaining the pypi-cache module, investigating pip
  install failures on runners, adding CUDA versions, or working with the wants/wheel-syncer
  pipeline.
---

# OSDC PyPI Cache Module

## Architecture (4 components + shared EFS)

The module deploys per-cluster:

1. **`pypi-cache-{slug}` Deployments** — one per CUDA slug (`cpu`, `cu126`, `cu128`, `cu130`).
   Each pod runs 3 containers: `nginx` (proxy + cache, `docker.io/nginxinc/nginx-unprivileged:1.27-alpine`),
   `pypiserver` (`pypiserver/pypiserver:v2.4.1`, local wheel index), and
   `nginx-exporter` (`docker.io/nginx/nginx-prometheus-exporter:1.4.1` on `:9113/metrics`).
   Pods run as `serviceAccountName: pypi-cache` with `runAsNonRoot: true, runAsUser: 65534`,
   `readOnlyRootFilesystem: true`. Behind Service `pypi-cache-{slug}` on ports `8080` (http) and
   `9113` (metrics). Replicas: default 2; per-cluster overrides
   (`arc-staging: 1`, `meta-staging-aws-uw1: 1`, `arc-cbr-production: 10`,
   `arc-cbr-production-uw1: 10`, `meta-prod-aws-ue1: 10`, `lf-prod-aws-ue1: 6`,
   `lf-prod-aws-ue2: 6`). pypiserver runs with `--disable-fallback` — nginx, not pypiserver, owns
   the upstream-fallback path.

2. **`pypi-wants-collector` Deployment** (1 replica) — runs `wants_collector.py`. Tails
   `/data/logs/upstream/fallback.YYYY-MM-DD.log` on EFS, filters with PyPI JSON API,
   uploads `wants/{cluster}.txt` and updates `prebuilt-cache.txt`. **Writer to S3.**

3. **`pypi-wheel-syncer` Deployment** (1 replica) — runs `wheel_syncer.py`. Pulls
   `s3://pytorch-pypi-wheel-cache/{slug}/*.whl` → `/data/wheelhouse/{slug}/` on EFS via
   atomic rename. **Reader from S3.**

4. **External wheel-build pipeline** — NOT in this repo. No GitHub workflow, no builder
   pod, no `.github/workflows/*` for it. Reads `wants/{cluster}.txt` + `needbuild.txt` from
   S3, builds wheels, pushes to `s3://pytorch-pypi-wheel-cache/{slug}/`. The wheel-syncer
   then surfaces them on EFS.

**Shared EFS PVC `pypi-cache-data`** (ReadWriteMany, StorageClass `efs-pypi-cache` with
provisioner `efs.csi.aws.com`, `basePath: /pypi-cache`, `reclaimPolicy: Retain`) is
mounted by all per-slug Deployments + wants-collector + wheel-syncer. Holds the
wheelhouse and fallback logs. **Persistent across pod rescheduling.** EFS CSI driver is
installed via `aws_eks_addon` in this module's Terraform.

Runners pick the correct backend via env (FQDN form is what generated runner manifests
actually inject — the short name also resolves inside `arc-runners` via search domains):
```
PIP_INDEX_URL=http://pypi-cache-{slug}.pypi-cache.svc.cluster.local:8080/simple/
PIP_EXTRA_INDEX_URL=http://pypi-cache-{slug}.pypi-cache.svc.cluster.local:8080/whl/{cuda}/
```

The per-slug Service exposes two ports: `8080` (http) and `9113` (metrics, from the
nginx-exporter sidecar). Use the metrics port when wiring up a ServiceMonitor.

Per-slug PodDisruptionBudget (`pypi-cache-{slug}`, `minAvailable: 1`) guards rollouts and
node drains. The Karpenter NodePool sets `disruption.budgets[].nodes: "1"` — Karpenter
consolidates at most one node at a time.

## CUDA Slug Convention

`cuda_slug()` in `generate_manifests.py` strips patch version: `12.8.1` → `cu128`,
`13.0.2` → `cu130`. Matches PyTorch's `download.pytorch.org/whl/cu128/` URL convention.
**Patch version is intentionally dropped.** `get_slugs()` always prepends `cpu`.

Configured in `clusters.yaml`:
```yaml
pypi_cache:
  instance_type: r5d.12xlarge
  cuda_versions: ["12.6.3", "12.8.1", "13.0.2"]
  # python_versions, target_architectures, target_manylinux are read by
  # wants-collector for the matrix header, but not yet by manifest generation.
```

Adding a CUDA version → new Deployment, new Service, new pod-per-node, smaller per-pod
NVMe slice. Re-run manifest generation after editing `clusters.yaml`.

## Storage: Two Distinct Layers

| Layer | Type | Lifetime | What lives here |
|-------|------|----------|----------------|
| **Wheelhouse** | EFS PVC `pypi-cache-data` | Persistent across rescheduling | Built `.whl` files synced from S3 by wheel-syncer; fallback logs |
| **Nginx cache** | NVMe hostPath `/mnt/k8s-disks/0/nginx-cache-{slug}` (or emptyDir) | Ephemeral (gone on pod rescheduling) | Cached PEP 503/691 index responses, cached wheel downloads from PyPI/PyTorch fallback |

NVMe size per pod is **computed** by `compute_nginx_cache_size()` as
`floor(nvme_gib * 0.95 / pods_per_node)`. For r5d.12xlarge (~1,800 GiB NVMe RAID0) with
4 slugs (`cpu, cu126, cu128, cu130`) that's ~427 GiB per pod. Adding CUDA versions
shrinks this. Karpenter auto-formats NVMe as RAID0; init container `chown`s the
hostPath. The whole `else` branch in `generate_manifests.py` (`instance_type` empty →
emptyDir `30Gi` cache + `CriticalAddonsOnly Exists` toleration + manual `server.cpu/memory`
from `DEFAULTS["server"]`) is **unreachable today** because `DEFAULTS` sets `r5d.12xlarge`.

The nginx `max_size=` on `proxy_cache_path` is **computed at deploy time** by running
`generate_manifests.py --print-nginx-max-cache-size`, then `sed`-substituted into the
`__NGINX_MAX_CACHE_SIZE__` placeholder when `deploy.sh` materializes the
`pypi-cache-nginx-config` ConfigMap. ConfigMap changes don't propagate to running pods,
so `deploy.sh` does an explicit `kubectl rollout restart` on every per-slug Deployment.

`inactive=7d` on the nginx cache key zone (`nginx.conf:53`) — entries unused for 7 days
are evicted regardless of TTL.

`deploy.sh` also wipes the per-pod nginx cache on every deploy by default. Behavior is
controlled by `PYPI_CACHE_CLEAR`: `yes` clears without prompt, `no` skips, unset on a
TTY prompts the user (defaults to clear after a 30 s timeout), unset with `CI=true`
skips. Expect a post-deploy latency hit on the first few requests per slug.

## S3 Bucket Layout

Bucket: `s3://pytorch-pypi-wheel-cache/`

| Path | Scope | Writer | Reader |
|------|-------|--------|--------|
| `wants/{cluster}.txt` | per-cluster, 7-day expiry | wants-collector | external builder |
| `prebuilt-cache.txt` | shared | wants-collector | external builder, wants-collector |
| `needbuild.txt` | shared, manual | human (via `aws s3 cp`) | external builder, wants-collector |
| `{slug}/*.whl` | shared per-slug | external builder | wheel-syncer (S3 RO) |

`wants/*`, `prebuilt-cache.txt`, `needbuild.txt` are public-read.

## prebuilt-cache.txt — Header Invalidation Rule

First line is a matrix marker:
```
# matrix: py3.10,py3.11,py3.12,py3.13,py3.13t,py3.14,py3.14t x86_64,aarch64 manylinux_2_28
```

The header is built by `build_matrix()` (`wants_collector.py:114-129`) as
`{','.join('py'+v)} {','.join(archs)} manylinux_{N}` — versions are concatenated
verbatim, so freethreaded variants survive as `py3.13t` / `py3.14t`. Defaults in
`clusters.yaml` give 7 python entries × 2 architectures.

`parse_prebuilt_cache()` checks the header. **Mismatch invalidates the entire cache**
(returns empty set, all packages get re-checked). Bumping `python_versions`,
`target_architectures`, or `target_manylinux` in `clusters.yaml` triggers full re-walk.
Cold start (no key in S3 → `NoSuchKey`) returns None → empty set; works on first run.

## needbuild.txt Format

Force-build override list. Bypasses both prebuilt-cache check and PyPI availability check.
- One package per line
- `#` lines and blank lines allowed
- Names PEP 503-normalized via `_normalize_name()` (lowercase, `_`/`.`/`-` collapsed)
- Collector reads but never writes — only humans edit (`aws s3 cp needbuild.txt s3://…`)

## nginx Internals (the gotchas)

### merge_indexes.js (njs handler)

Loaded at `nginx.conf:71`, serves `/simple/{pkg}/`. Issues two subrequests against
`/_internal/local/simple/{pkg}/` and `/_internal/upstream/simple/{pkg}/` (the two backend
locations are cached under `local:` and `upstream:` cache-key prefixes to prevent collision),
merges responses. **Solves the BY/BZ shadowing problem**: pypiserver returned 200 for some
packages with wrong-variant wheels, preventing fallback to PyPI. Merging guarantees both
sources are considered. Local wins on filename collision; upstream URLs are rewritten to
relative paths (the nginx `sub_filter` directive does NOT apply to njs subrequest bodies, so
URL rewriting is duplicated in JS). Supports PEP 503 (HTML) and PEP 691 (JSON) via the client
`Accept` header; since pypiserver v2.x always returns HTML, `parseFilesWithFallback()` parses
JSON first and falls back to HTML-parsing on `JSON.parse` failure. Root `/simple/` listing
prefers upstream (pypiserver's root is intentionally incomplete) and falls back to local.

`subrequest_output_buffer_size 100m` (line 77) — required for grpcio (~6 MB), aiohttp
(~7 MB), full pypi `/simple/` response (~40 MB). Don't shrink it.

### Dual-stack listen + IPv6 resolver (IPv6-only EKS)

Under IPv6-only EKS the nginx proxy listens on **both `0.0.0.0:8080` and `[::]:8080`** — `nginx.conf` declares `listen 8080;` AND `listen [::]:8080;` so the same Service can serve clients on either address family. The `resolver` directive no longer carries `ipv6=off`; nginx must resolve AAAA records to reach IPv6-only upstreams.

`deploy.sh` brackets IPv6 ClusterIPs when substituting the `__DNS_RESOLVER__` placeholder in `nginx.conf`:

```bash
case "$KUBE_DNS_IP" in
  *:*) DNS_RESOLVER="[$KUBE_DNS_IP]" ;;   # IPv6 ULA from fd00:ec2::/108 — bracketed
  *)   DNS_RESOLVER="$KUBE_DNS_IP"   ;;   # IPv4 — bare
esac
```

Under IPv6-only EKS the kube-dns ClusterIP is from the AWS-assigned `fd00:ec2::/108` cluster service CIDR and arrives bracketed in the rendered `nginx.conf`.

The **nginx-prometheus-exporter sidecar scrapes via `[::1]:8080`** (IPv6 loopback) — see `--nginx.scrape-uri=http://[::1]:8080/stub_status` in the deployment template. Even with the dual-stack listen, the exporter intentionally uses IPv6 loopback to validate end-to-end IPv6 reachability inside the pod.

### sub_filter URL rewriting

For `text/html` (implicit), `application/vnd.pypi.simple.v1+json`, and
`application/vnd.pypi.simple.v1+html`:
- `https://files.pythonhosted.org` → `` (relative)
- `https://download.pytorch.org` → `` (relative)

Forces clients through the proxy so cache-enforcer doesn't block them. Note: `sub_filter`
only affects the proxied response body — it does NOT touch njs subrequest bodies, so
`merge_indexes.js` re-implements the same rewrite for merged `/simple/` responses.

### Hash-path vs flat-path /packages/ routing

`files.pythonhosted.org` uses hash-based paths
(`/packages/<2-hex>/<64-hex>/<filename>.whl`) while pypiserver returns flat
`/packages/<filename>.whl` URLs. Two distinct nginx `location` blocks split the traffic:
the `^/packages/[0-9a-f][0-9a-f]/` regex proxies to `https://files.pythonhosted.org`
(1-month cache, hits pythonhosted's hash structure); the generic `\.(whl|tar\.gz|zip)$`
location goes to pypiserver with a server-side fallback to `@pypi_fallback` via
`proxy_intercept_errors on; error_page 404 500 502 503 = @pypi_fallback;`. The fallback
proxies to `https://pypi.org` and writes the access log to
`/data/logs/upstream/fallback.$log_date.log` — that's the file the wants-collector tails.

### `/whl/` 403→404 rewrite (uv compatibility gotcha)

`error_page 403 =404 @pytorch_not_found;` (and `500 502 503 504 =404`) on the PyTorch
`/whl/` path. uv treats 403 as auth error and aborts; 404 is interpreted as "fall through
to default index". Don't change this.

### Cache TTLs

| Endpoint | 200 | 404 | Other |
|---------|-----|-----|------|
| Local pypiserver index (`local:`) | 30m | 1m | — |
| Upstream pypi index (`upstream:`) | 10m | 1m | 301/302 1m |
| pythonhosted downloads | 1M (one month) | 1m | 301 1M |
| Local pypiserver wheel downloads | 30d | — | — |
| PyTorch `/whl/` index | 10m | 1m | 403 1m |

`inactive=7d` evicts unused entries regardless.

### PEP 691 cache key includes `$http_accept`

`application/vnd.pypi.simple.v1+json` (PEP 691) and `text/html` (PEP 503) responses must
be cached separately. **Removing `$http_accept` from the cache key causes silent format
collisions.**

### pypiserver backend

`--backend simple-dir` (`deployment.yaml.tpl:132`). Every `/simple/<pkg>/` request does
a fresh `os.listdir` of the wheelhouse. `cached-dir` is **not** used because its
inotify-based invalidation does not observe NFS writes from other clients
(`pypi-wheel-syncer` in a separate pod) — the in-memory index would freeze on first scan.
Per-request listdir cost is negligible because nginx caches the `/simple/<pkg>/`
response for 30 minutes upstream.

## Node Placement & Resources

**Per-slug `pypi-cache-{slug}` pods** run on dedicated `r5d.12xlarge` Karpenter
NodePool `pypi-cache`, taint `workload=pypi-cache:NoSchedule`. PodAntiAffinity is
**soft** (`preferredDuringSchedulingIgnoredDuringExecution`, weight 100,
`topologyKey: kubernetes.io/hostname`) — Karpenter prefers spreading replicas of the
same `cuda-version` across nodes but will co-schedule when no other node is available
(e.g. during rollout headroom contention). The "shared base nodes /
`CriticalAddonsOnly`" fallback path exists in code but is unreachable today
(DEFAULTS sets the instance type).

**`pypi-wants-collector` and `pypi-wheel-syncer` pods** have no nodeSelector and only
tolerate `key: CriticalAddonsOnly, operator: Equal, value: "true", effect: NoSchedule`
— matching the `CriticalAddonsOnly=true:NoSchedule` taint on base nodes. They cannot
land on the dedicated `workload=pypi-cache` NodePool. In practice they end up on
whichever shared/base node will accept them. When debugging "which node is the
pipeline on?", look at the base node group, not pypi-cache nodes.

Both pipeline pods are otherwise identical in shape:
- `initContainer` runs `python:3.12-alpine` and `pip install boto3==1.35.0 --target=/pip-packages`;
  the runtime container picks it up via `PYTHONPATH=/pip-packages`. Relevant when
  debugging container startup or supply-chain.
- `AWS_USE_DUALSTACK_ENDPOINT=true` so boto3 reaches S3/STS over `*.dualstack.<region>.amazonaws.com`
  IPv6 endpoints — required on IPv6-only EKS to avoid V4-egress NAT.
- Liveness probe checks `/tmp/last-success` mtime is < 600 s old. When a pipeline pod
  "looks stuck" but isn't restarting, check that the script is touching `/tmp/last-success`.

`compute_pod_resources()` computes Guaranteed-QoS CPU/memory per pod from instance specs:
```
allocatable = total - kubelet_reserved
usable = allocatable - daemonset_overhead (300m / 440Mi)
per_pod = floor(usable * 0.90 / pods_per_node)
```
nginx gets fixed allocation (4 vCPU / 64 GiB — sized for 100m subrequest buffers under
load). pypiserver gets the remainder.

## NetworkPolicy

`networkpolicy.yaml` restricts ingress to pods in namespace
`kubernetes.io/metadata.name: arc-runners`. The `podSelector` only matches pods with
label `app: pypi-cache` — so this policy applies to the per-slug `pypi-cache-{slug}`
pods only. The wants-collector and wheel-syncer pods (labels `app: pypi-wants-collector`
and `app: pypi-wheel-syncer`) have **no NetworkPolicy** in this module.

Cache-enforcer is a DaemonSet running in `kube-system` and is **not** a client — it
only blocks egress on runner nodes. Clients are runner pods only.

## IRSA Roles (Terraform)

| Role | Permissions (as deployed) |
|------|------------|
| `{cluster}-pypi-wants-collector-role` | `s3:PutObject`, `s3:GetObject`, `s3:ListBucket` on the **whole bucket** (`pytorch-pypi-wheel-cache` and `pytorch-pypi-wheel-cache/*`) |
| `{cluster}-pypi-wheel-syncer-role` | `s3:GetObject`, `s3:ListBucket` on the **whole bucket** |
| EFS CSI driver IRSA (in this module) | EFS access points |

Both pipeline roles are bucket-wide — the wants-collector role can technically write
to `{slug}/*.whl` paths owned by the external builder. The conceptual scopes are
`wants/*` + `prebuilt-cache.txt` (wants-collector) and bucket-RO (wheel-syncer); the
deployed policies are looser than that.

`deploy.sh` reads `wants_collector_role_arn` and `wheel_syncer_role_arn` from the
terraform output and annotates the `pypi-wants-collector` and `pypi-wheel-syncer`
ServiceAccounts at deploy time with `eks.amazonaws.com/role-arn`.

EFS CSI driver is installed by this module via `aws_eks_addon` (pinned at
`v3.2.0-eksbuild.1`) — not a base infra concern. The EFS filesystem itself uses
`throughput_mode = "elastic"` (`terraform/main.tf:52`) — relevant for capacity
planning vs bursting/provisioned modes.

## Log Rotation

nginx writes upstream-fallback access logs to `/data/logs/upstream/fallback.$log_date.log`
on EFS. `$log_date` is an nginx `map` of `$time_iso8601` to `YYYY-MM-DD`, producing one
file per day. wants-collector deletes files older than `--max-log-age-days` (default 30)
on each run. Filename pattern is exact: `fallback.YYYY-MM-DD.log`; non-date files like
`fallback.date-unknown.log` are skipped.

`scripts/python/log_rotator.py` exists with its own unit test, but is **dead code** —
nothing in `deploy.sh`, the k8s manifests, or other scripts invokes it. Production log
rotation is done by `wants_collector.cleanup_old_logs`. There's even a test
(`test_generate_manifests.py::test_command_does_not_pipe_through_log_rotator`) that
asserts `log_rotator.py` is not wired up.

## cache-enforcer Integration

`modules/cache-enforcer` is a separate DaemonSet on `workload-type: github-runner` nodes
(NOT pypi-cache nodes). Blocks egress to `pypi.org`, `files.pythonhosted.org`,
`download.pytorch.org` using **`xt_string` SNI matching on TLS ClientHello** — NOT DNS.
Requires the `xt_string` kernel module (preflight loads it; aborts if missing).

If pypi-cache is down, all pip installs on runners fail — there is no bypass.

## Key Files

- `kubernetes/namespace.yaml` — `pypi-cache` Namespace
- `kubernetes/serviceaccount.yaml` — `pypi-cache` SA used by per-slug Deployments
- `kubernetes/wants-collector-sa.yaml` — SA annotated with IRSA role at deploy time
- `kubernetes/wheel-syncer-sa.yaml` — SA annotated with IRSA role at deploy time
- `kubernetes/kustomization.yaml` — applies namespace, the 3 SAs, NetworkPolicy
- `kubernetes/nginx.conf` — proxy, cache, sub_filter rewrites, /simple/ via njs,
  `/whl/` and `/packages/<hash>/` routing, `@pypi_fallback`
- `kubernetes/merge_indexes.js` — njs handler, local + upstream merge, URL rewrite
- `kubernetes/deployment.yaml.tpl` — 3-container pod template (nginx, pypiserver, exporter)
- `kubernetes/service.yaml.tpl` — per-slug ClusterIP Service (ports 8080 + 9113)
- `kubernetes/wants-collector-deployment.yaml.tpl` — wants-collector
- `kubernetes/wheel-syncer-deployment.yaml.tpl` — wheel-syncer
- `kubernetes/storageclass.yaml.tpl` — `efs-pypi-cache`, `basePath: /pypi-cache`, Retain
- `kubernetes/pvc.yaml.tpl` — `pypi-cache-data`, RWX
- `kubernetes/networkpolicy.yaml` — arc-runners only
- `kubernetes/nodepool.yaml.tpl` — Karpenter `pypi-cache`, taint `workload=pypi-cache`,
  `disruption.budgets[].nodes: "1"`
- `kubernetes/ec2nodeclass.yaml.tpl` — EC2NodeClass for pypi-cache nodes (AL2023,
  `instanceStorePolicy: RAID0` for NVMe instances, CLUSTER_NAME_PLACEHOLDER sed-substituted
  by deploy.sh)
- `kubernetes/pdb.yaml.tpl` — per-slug PodDisruptionBudget (`minAvailable: 1`), keeps
  at least one pod per slug servable through node drains and rolling updates.
  **Gotcha**: clusters configured with `replicas: 1` (e.g. `arc-staging`,
  `meta-staging-aws-uw1`) will block voluntary disruptions entirely — drain the node
  manually or temporarily bump replicas.
- `scripts/python/generate_manifests.py` — slug fanout, resource computation, NVMe sizing
- `scripts/python/wants_collector.py` — fallback log → S3 wants/, prebuilt-cache.txt
- `scripts/python/wheel_syncer.py` — S3 → EFS atomic rename
- `scripts/python/log_rotator.py` — dead code (see Log Rotation section)
- `terraform/main.tf` — IRSA (wants-collector RW, wheel-syncer RO, EFS CSI driver), EFS FS, EFS CSI addon
- `terraform/wheel-cache-bucket/` — S3 bucket (region `us-east-2`), lifecycle (7-day expiry on `wants/*`), public-read on metadata files
- `docker/Dockerfile` — wheel-builder image (NOT deployed by this module; remnant of the
  retired in-cluster builder design — the active builder is the external pipeline).
