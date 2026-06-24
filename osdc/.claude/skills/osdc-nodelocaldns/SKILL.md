---
name: osdc-nodelocaldns
description: >
  OSDC NodeLocal DNSCache (NLD) base component — per-node CoreDNS DaemonSet under
  base/kubernetes/nodelocaldns/. Covers iptables-mode rationale (vs IPVS-mode), dynamic
  kube-dns ClusterIP substitution via deploy.sh (validates the kube-dns ClusterIP is IPv6
  and fails fast on IPv4 — OSDC EKS is IPv6-only by design and NLD is single-family by
  deploy-script enforcement; the IPv6-only cluster recreate has already shipped in
  commit a6b4c8c),
  Service-before-DaemonSet apply ordering (KUBE_DNS_UPSTREAM env-var injection),
  kube-dns-upstream auxiliary Service, two metrics ports (9253 CoreDNS plugin / 9353
  binary setup_errors_total), the coredns_nodecache_* vs nodelocaldns_* metric-name
  confusion, no preStop / no memory limit / no startup taint design decisions, Harbor
  proxy-cache image pull (no pre-mirror), IPv6 ULA bind address (fd00::10), and the
  ≥3-day soak gate (applies to any FUTURE cluster-wide NLD cutover).
  Applies to ~/meta/ci-infra/osdc.
  Load when developing, debugging, or maintaining the nodelocaldns base component,
  investigating DNS issues on runner nodes, modifying its Corefile / DaemonSet manifest,
  or preparing a future cutover that touches NLD.
---

# OSDC NodeLocal DNSCache (NLD)

## What This Is

Per-node CoreDNS DaemonSet that absorbs DNS queries from local pods to cut load on the cluster CoreDNS Deployment. Lives under `base/kubernetes/nodelocaldns/` (always-on, every cluster). Wired into `deploy-base` justfile recipe as the LAST base step (after image-cache-janitor). The IPv6-only cluster recreate has already happened (commit `a6b4c8c` — IPv6-only EKS) and NLD is IPv6-only by design today; the soak-gate guidance below applies to any FUTURE cluster-wide cutover that puts NLD on a new code path.

## Architecture (5 manifests + 1 deploy.sh + monitoring)

The base component deploys per-cluster:

1. **`node-local-dns` DaemonSet** (`kube-system`) — one pod per node, container `node-cache` running `registry.k8s.io/dns/k8s-dns-node-cache:1.26.8` (`imagePullPolicy: IfNotPresent`). `priorityClassName: system-node-critical`, `hostNetwork: true`, `dnsPolicy: Default`, `securityContext: privileged: true`. Requests `cpu: 25m, memory: 100Mi`, **NO limits**. `terminationGracePeriodSeconds: 30`. Rollout `maxUnavailable: 1%` (~7 nodes simultaneously on a 700-node cluster).
2. **`node-local-dns` ConfigMap** — Corefile with 4 server blocks (`cluster.local:53`, `in-addr.arpa:53`, `ip6.arpa:53`, `.:53`).
3. **`kube-dns-upstream` Service** (`kube-system`) — auxiliary Service in front of cluster CoreDNS so NLD has a separate ClusterIP to forward to without iptables-redirecting through itself. Selector `k8s-app: kube-dns`. Upstream NLD requires this.
4. **`node-local-dns-metrics` Service** (`kube-system`) — headless (`clusterIP: None`) for PodMonitor pod-level discovery. Two ports: `metrics/9253` and `metrics-binary/9353`.
5. **`node-local-dns` ServiceAccount** — no RBAC, and `automountServiceAccountToken: false`. The binary doesn't talk to the API server; the upstream Service IP is resolved via the `KUBE_DNS_UPSTREAM_SERVICE_HOST` env var that kubelet injects from the `kube-dns-upstream` Service's ClusterIP. Disabling the token is a hardening detail — the privileged container can't leak an SA token it never mounts. A future edit that introduces an init container running `kubectl` will silently fail and needs to either re-enable token mounting or use a different mechanism.
6. **`deploy.sh`** — orchestrates the apply (see below — Service ordering matters).
7. **PodMonitor `nodelocaldns`** (`modules/monitoring/`) — TWO podMetricsEndpoints (9253 + 9353).
8. **PrometheusRule `nodelocaldns-alerts`** (`modules/monitoring/`) — three alerts.

All NLD manifests (DaemonSet, ConfigMap, both Services, ServiceAccount) carry the label `app.kubernetes.io/managed-by: osdc-base` alongside the standard `k8s-app` label. Use it for label-based discovery / debugging when filtering resources by ownership.

## Iptables-mode (NOT IPVS-mode) — Critical Design Decision

NLD launches with `-localip fd00::10,${KUBE_DNS_CLUSTER_IP}`. The binary creates a dummy `nodelocaldns` interface, binds BOTH addresses, and inserts NOTRACK rules in the `raw` table for traffic destined to either address. Under OSDC's IPv6-only EKS these rules are programmed via **ip6tables** (the IPv6-family equivalent of iptables); `deploy.sh` rejects an IPv4 kube-dns ClusterIP outright, so the IPv4 path is not a runtime fallback in this project — the binary's per-family logic still exists upstream but OSDC is single-family by deploy-script enforcement.

**Pods continue resolving via the cluster's `kube-dns` Service ClusterIP** — kubelet's `--cluster-dns` is unchanged. NLD intercepts via ip6tables/iptables.

**Why this matters**: if NLD pod dies, traffic to the kube-dns ClusterIP **falls through** to cluster CoreDNS via the unchanged kube-dns Service Endpoints. With IPVS-mode (kubelet pointing at the local NLD address `fd00::10`), an NLD failure would be a total per-node DNS outage with no fallback. Iptables-mode gives graceful degradation — every per-node failure is recoverable without operator intervention.

This is also why **no startup taint** is needed for NLD readiness on fresh Karpenter nodes. The 10-30s race where NLD's NOTRACK rules aren't yet programmed resolves itself: pods can still resolve via the unchanged kube-dns ClusterIP.

The DaemonSet mounts `/run/xtables.lock` from the host (hostPath, `FileOrCreate`). This is the standard iptables advisory lock — kube-proxy, aws-node, cache-enforcer, and NLD all coordinate through it so concurrent rule mutations don't trample each other. The mount is what prevents iptables-table deadlocks between NLD and the other privileged DaemonSets writing rules on the same node. The pod also mounts the optional `kube-dns` ConfigMap at `/etc/kube-dns` (upstream NLD convention for stub-domains / upstream config — we don't ship one, but the mount stays in case future config is added) and the `node-local-dns` ConfigMap at `/etc/coredns` (with the `Corefile` key mapped to `Corefile.base`).

## kube-dns ClusterIP is Dynamic — deploy.sh Substitution Pattern

The cluster service CIDR is dynamic. On IPv4 EKS it is configurable per cluster (defaults to `10.100.0.0/16`, but can be overridden). On IPv6-only EKS it is **AWS-assigned and not configurable** — AWS allocates a `/108` from `fd00:ec2::/64` and the kube-dns ClusterIP is the 10th address in that range. In both cases hardcoding the value would break any cluster with a different service CIDR, so the value is resolved at deploy time.

`deploy.sh` resolves the live ClusterIP and substitutes it into a placeholder. The script is organized as 7 numbered steps:

1. **Step 1 (precondition)** — verify `kube-dns-upstream` Service either doesn't exist OR has `selector.k8s-app=kube-dns`. If it exists with a different selector, fail loudly (avoids silent overwrite from a stale prior install). Sets two booleans: `UPSTREAM_FIRST_DEPLOY` (never had this Service in this cluster before) and `UPSTREAM_SERVICE_FRESHLY_CREATED` (Service did not exist at the start of THIS run).
2. **Step 2** — resolve `kube-dns` Service ClusterIP via `kubectl get svc kube-dns -n kube-system -o jsonpath='{.spec.clusterIP}'`. Validates IPv6 family and fails fast on IPv4.
3. **Step 3** — render via `kubectl kustomize`, then `sed`-substitute `__KUBE_DNS_CLUSTER_IP__` in the Corefile and DaemonSet args.
4. **Step 4** — split the rendered manifests by `kind` into a Services file and a rest file. Uses an inline `uv run --with pyyaml python3` heredoc — the only non-trivial `uv` dependency in the script. This is a deliberate YAML splitter, not a place to inline `kubectl apply` with label selectors.
5. **Step 5** — apply Services FIRST (`kube-dns-upstream` + `node-local-dns-metrics`) via `kubectl_apply_if_changed`, then a brief 3s `sleep` so the ClusterIP is allocated before any DaemonSet pod is created. This ordering is non-negotiable (see next section).
6. **Step 6** — apply the rest (ConfigMap, ServiceAccount, DaemonSet) via `kubectl_apply_if_changed`.
7. **Step 7 (idempotency safety net)** — if **either** `UPSTREAM_FIRST_DEPLOY` OR `UPSTREAM_SERVICE_FRESHLY_CREATED` is true, `kubectl rollout restart ds node-local-dns`. New pods need to start AFTER the Service exists so kubelet can inject the env var. The two-trigger design covers both first install AND out-of-band Service deletion followed by redeploy.

**Why NLD is NOT in `base/kubernetes/kustomization.yaml`**: the `__KUBE_DNS_CLUSTER_IP__` placeholder cannot survive a static `kubectl apply -k`. NLD follows the same pattern as `image-cache-janitor` — a comment in `kustomization.yaml` notes NLD is deployed via its own deploy.sh because the ClusterIP is resolved at apply time, and `deploy.sh` is invoked separately by the `deploy-base` justfile recipe. Adding NLD to the kustomization would either fail kubeconform lint on the literal placeholder OR apply garbage manifests on cluster bootstrap.

## Service-Before-DaemonSet Apply Ordering (Non-Obvious)

NLD reads its upstream Service IP via the `KUBE_DNS_UPSTREAM_SERVICE_HOST` environment variable that kubelet **automatically injects from the `kube-dns-upstream` Service** (`-upstreamsvc kube-dns-upstream` flag tells the binary which Service name to look for). 

**But kubelet only injects env vars for Services that exist BEFORE the pod is created.** If the DaemonSet rolls out before the Service exists, NLD pods come up without the env var, and the Corefile's `__PILLAR__CLUSTER__DNS__` placeholder never resolves — pods crashloop or silently misbehave.

`deploy.sh` mitigates this in two ways:
1. **Apply Services first** with a short wait, then apply the DaemonSet
2. **Conditional rollout-restart** if the Services were freshly created (means existing pods don't have the env var)

This is why a naive `kubectl apply -k` doesn't work for NLD even setting aside the placeholder substitution — it doesn't enforce the Service-before-DaemonSet ordering.

## Two Metrics Ports — 9253 vs 9353 (Common Confusion Source)

NLD exposes metrics on TWO ports for distinct reasons:

| Port | Source | What's there | Notable metrics |
|------|--------|--------------|----------------|
| `9253` | CoreDNS `prometheus` plugin (configured in Corefile) | All CoreDNS plugin metrics | `coredns_dns_*`, `coredns_cache_*`, `coredns_forward_*`, `coredns_health_*` |
| `9353` | NLD binary itself (default upstream port; NOT a CoreDNS plugin) | Binary-emitted setup/error counters | `coredns_nodecache_setup_errors_total`, `coredns_nodecache_dummy_count` |

**The setup-errors metric — used by the `NodeLocalDNSSetupErrors` critical alert — lives ONLY on `:9353`.** A PodMonitor that scrapes only `:9253` would silently miss it. The PodMonitor MUST have two `podMetricsEndpoints` entries (one per port).

The container manifest must declare `containerPort: 9353 protocol: TCP name: metrics-binary` even though upstream's reference manifest doesn't expose it (their `:9253` example bundles the binary's metrics into the same plugin endpoint — but only when the binary is configured that way, which we are not).

## `coredns_nodecache_*` vs `nodelocaldns_*` (Metric Name Confusion)

Many references — including the original PR13b spec — use `nodelocaldns_setup_errors_total`. **That metric does not exist.** The actual upstream metric (verified in `kubernetes/dns` source) is `coredns_nodecache_setup_errors_total`. The `coredns_nodecache_` prefix is the canonical one for binary-emitted metrics.

If you write a query against `nodelocaldns_*` it will silently return nothing.

## Other Design Decisions (Don't Relitigate)

### No `preStop` hook
The original plan called for a preStop iptables flush addressing upstream issue [kubernetes/dns#394](https://github.com/kubernetes/dns/issues/394). Rejected because:
- Bare `iptables -F` flushes the entire `filter` table, breaking kube-proxy / aws-node / cache-enforcer rules cluster-wide. Catastrophic on rolling restart.
- Targeted flush (`iptables -t raw -F NODELOCAL_DNS`) is fragile (chain names depend on NLD version) and the binary already re-asserts rules on startup.
- `terminationGracePeriodSeconds: 30` (bumped from upstream's 5s) gives enough time for the binary's own SIGTERM cleanup loop. OOM is unlikely given `system-node-critical` priority.

### No CPU or memory limit
Strong upstream consensus: NEVER set memory limit on NLD. OOMKill orphans iptables rules → cluster-wide DNS degradation until pod restart. `system-node-critical` PriorityClass already prevents eviction. CPU throttling = DNS latency.

Set requests only: `cpu: 25m, memory: 100Mi` (request memory generously above upstream's 5Mi default to give cache headroom; the request is not a cap).

### Explicit tolerations (NOT `[{operator: Exists}]`)
`operator: Exists` would tolerate everything including `karpenter.sh/unschedulable` (blocks Karpenter consolidation) and node-not-ready taints (NLD scheduled before kubelet ready). Use the explicit list:
- `CriticalAddonsOnly:Equal:true:NoSchedule` (base nodes)
- `nvidia.com/gpu:Exists:NoSchedule` (GPU pools)
- `node-fleet:Exists:NoSchedule` (Karpenter fleets)
- `instance-type:Exists:NoSchedule` (per-instance taints)

If a smoke test discovers nodes with taints not in this list, ADD them — don't fall back to `Exists`.

### Liveness probe tuned for setup latency + reload windows
`initialDelaySeconds: 60, periodSeconds: 15, timeoutSeconds: 5, failureThreshold: 5` (= 75s of failed probes before kubelet kills the pod). Default `failureThreshold: 3` × `periodSeconds: 10` × `timeoutSeconds: 1` = 30s, too tight for ConfigMap reload windows.

Liveness is on `[fd00::10]:8080/health` (IPv6 literal requires brackets in the URL form; the DaemonSet manifest uses `host: fd00::10` in the httpGet probe — kubelet handles bracketing). Proves the dummy interface AND the binary are both up. Kubelet probes from host network; the `:8080` port is allowed through NLD's NOTRACK rules.

### Health endpoint in `cluster.local:53` ONLY (NOT in `.:53`)
The `health [fd00::10]:8080` directive is declared in the `cluster.local:53` block ONLY (matches upstream `nodelocaldns.yaml`). Per CoreDNS plugin/health docs, the health listener is a singleton: declaring the same address in a second server block (originally tried as "defense in depth" against a `cluster.local` parse failure) risks "address already in use" at NLD startup. (CoreDNS `health` directive REQUIRES IPv6 brackets in the address spec; `bind` directives further down do NOT use brackets — see IPv6 Addressing section.)

**Implication if `cluster.local` fails to load** (e.g., PILLAR substitution race, malformed Corefile after a future edit): the `:8080` health endpoint won't bind, kubelet's liveness probe fails, the pod restarts, and `deploy.sh`'s substitution + apply re-runs on the next reconcile. This is the desired behavior — a Corefile that won't parse is a serious problem worth restarting on, NOT something to mask with a fallback health endpoint.

### `force_tcp` to upstream on cluster.local/in-addr/ip6
Persistent TCP connection back to cluster CoreDNS reduces conntrack churn and improves head-of-line behavior. Each node holds 1 TCP conn per upstream IP (shared across server blocks). On the IPv4-cutover-target ~700-node cluster with the existing 2 CoreDNS replicas, that's ~350 conns/replica — within the `max_concurrent: 1000` default but tight. PR 13a (CoreDNS topology pin, scales to 6 replicas) fixes this independently.

### `.:53` (catch-all) forwards to `/etc/resolv.conf`
External lookups bypass cluster CoreDNS entirely (host's resolver = VPC resolver). Upstream default.

## Image Pull Strategy — Harbor Proxy Cache (NO Pre-Mirror)

`registry.k8s.io/dns/k8s-dns-node-cache:1.26.8` is **NOT pre-mirrored** to ECR via `modules/eks/images.yaml`. It flows through the standard Harbor `k8s-cache` proxy project (lazy-pull on first request). This is intentional: `mirror-images.sh` is bootstrap-only (Harbor components themselves), not a runtime image mirror pipeline.

**Trade-off**: cold Karpenter nodes hit ImagePullBackOff for ~30-60s on the first NLD pod start while Harbor lazy-pulls from upstream. Accepted because:
- NLD is `system-node-critical` (race resolves in seconds, no eviction risk)
- iptables-mode means pods can still resolve via the unchanged kube-dns ClusterIP during the race
- Building a runtime image mirror pipeline is much bigger blast radius

## Smoke Tests

Lives in `base/kubernetes/tests/smoke/test_base_kubernetes.py`, split across two classes:

- **`TestBaseDaemonSets`** owns the DaemonSet test:
  - `test_nodelocaldns_daemonset` — uses `assert_daemonset_healthy` (tolerates nodes in transition)
- **`TestNodeLocalDNSServices`** owns the Service tests:
  - `test_nodelocaldns_metrics_service_headless` — asserts `clusterIP == "None"` (PodMonitor depends on this)
  - `test_nodelocaldns_upstream_service_selects_kube_dns` — asserts `selector.k8s-app == "kube-dns"` (catches accidental selector drift)

If you add tolerations or change the DaemonSet shape, update the test under `TestBaseDaemonSets`. If you change the Services, update the tests under `TestNodeLocalDNSServices` — don't add Service assertions to `TestBaseDaemonSets`.

## IPv6 Addressing on Node-Local DNSCache

Under IPv6-only EKS, NLD binds the IPv6 ULA `fd00::10` (chosen because IPv6 link-local `fe80::/10` requires a zone-id suffix when binding — e.g. `fe80::10%eth0` — and CoreDNS does not support zone-scoped bindings). The ULA is non-routable and lives only on the per-node dummy `nodelocaldns` interface, mirroring the same intent as the IPv4 link-local `169.254.20.10` traditionally used by upstream's manifests.

- **Pods continue to resolve via the kube-dns Service ClusterIP** (now an IPv6 ULA from the AWS-assigned `fd00:ec2::/108` cluster service CIDR).
- **ip6tables NOTRACK** rules intercept traffic destined for both the kube-dns ClusterIP AND `fd00::10` and divert it to the local NLD listener.
- The iptables-mode rationale (graceful degradation if NLD dies, no startup taint, kubelet `--cluster-dns` unchanged) is unchanged from IPv4. Only the address family and binding syntax differ.
- **Both Services declare IPv6 SingleStack explicitly** — `metrics-service.yaml` (headless, `clusterIP: None`) and `upstream-service.yaml` both set `ipFamilyPolicy: SingleStack` with `ipFamilies: [IPv6]`. `clusterIP: None` does not relieve the headless Service from declaring family preference; the explicit declarations make the IPv6-only intent code-level reviewable and portable across clusters.

### Bracket / no-bracket conventions

| Where the address appears | Form | Reason |
|---------------------------|------|--------|
| CoreDNS `bind` directive in Corefile | `bind fd00::10` (no brackets) | Bind takes a bare address |
| CoreDNS `health` directive | `health [fd00::10]:8080` (brackets) | Address-with-port form requires brackets in Corefile |
| DaemonSet `-localip` flag | `-localip fd00::10,${KUBE_DNS_CLUSTER_IP}` (no brackets) | Comma-separated bare addresses |
| Kubelet httpGet probe | `host: fd00::10` (no brackets in YAML) | Kubelet brackets the host on its own when constructing the URL |
| `dig`/`nslookup` query target | `dig @fd00::10 ...` (no brackets) | Tools accept bare IPv6 in `@server` form |
| `curl`/HTTP URLs | `curl http://[fd00::10]:8080/health` (brackets) | RFC 3986 — IPv6 in URL authority must be bracketed |

## Soak Gate Before Cutover

The original IPv6-only cutover has already shipped (see commit `a6b4c8c`); the gate below now applies to any FUTURE cluster-wide cutover that puts NLD on a new code path (binary image bump, kernel/AL2023 base change, dual-stack migration, etc.).

NLD must run on the existing fleet for **≥3 days (preferably 7)** of stable operation, including a planned mid-soak DaemonSet rollout, before any cluster-wide cutover is allowed to schedule. The soak gate exists because NLD is a per-node DNS layer — failure modes only manifest under real production load, and the cutover removes the safety margin of pre-cutover capacity.

Soak signals to watch:
- `coredns_nodecache_setup_errors_total` should be flat at 0
- `kube_daemonset_status_number_unavailable{daemonset="node-local-dns"}` should be 0 except during deliberate rollouts
- Cluster CoreDNS QPS (visible on the `coredns` PodMonitor) should drop dramatically vs pre-NLD baseline
- `kube_pod_container_status_restarts_total{container="node-cache"}` should be flat (pod restarts indicate iptables/binary trouble)

## Verification — Confirming NLD is Actually Answering

```bash
# From inside any pod with `dig`/`nslookup` on a node where NLD runs:
dig @fd00::10 kubernetes.default.svc.cluster.local +short    # should return the kubernetes Service ClusterIP
dig @fd00::10 google.com +short                              # should return external AAAA/A records (catch-all .:53 → /etc/resolv.conf)

# Check NLD is intercepting (not falling through)
kubectl exec -n kube-system <nld-pod> -- wget -qO- http://localhost:9253/metrics | grep '^coredns_dns_request_count_total'
kubectl exec -n kube-system <nld-pod> -- wget -qO- http://localhost:9353/metrics | grep '^coredns_nodecache_'

# Check ip6tables rules (privileged exec) — IPv6-only EKS uses the IPv6 family
kubectl exec -n kube-system <nld-pod> -- ip6tables -t raw -L NODELOCAL_DNS_NOTRACK -n
```

## Key Files

- `base/kubernetes/nodelocaldns/kustomization.yaml` — references all 5 manifests (namespace `kube-system`)
- `base/kubernetes/nodelocaldns/serviceaccount.yaml` — `node-local-dns` SA (no RBAC)
- `base/kubernetes/nodelocaldns/daemonset.yaml` — DaemonSet manifest
- `base/kubernetes/nodelocaldns/configmap.yaml` — Corefile (4 server blocks, two placeholders)
- `base/kubernetes/nodelocaldns/upstream-service.yaml` — `kube-dns-upstream` (selector `k8s-app: kube-dns`)
- `base/kubernetes/nodelocaldns/metrics-service.yaml` — headless metrics Service (ports 9253 + 9353)
- `base/kubernetes/nodelocaldns/deploy.sh` — orchestration (Service ordering, ClusterIP substitution, conditional rollout-restart)
- `base/kubernetes/kustomization.yaml` — comment documenting why NLD is NOT in resources list
- `justfile` `deploy-base` recipe — invokes NLD deploy.sh as the LAST base step (after image-cache-janitor)
- `modules/monitoring/kubernetes/monitors/podmonitors/nodelocaldns.yaml` — PodMonitor (TWO endpoints: 9253 + 9353)
- `modules/monitoring/kubernetes/alerts/nodelocaldns-alerts.yaml` — `NodeLocalDNSSetupErrors` (critical), `NodeLocalDNSPodRestarting`, `NodeLocalDNSDaemonSetDegraded`
- `base/kubernetes/tests/smoke/test_base_kubernetes.py` — smoke tests on `TestBaseDaemonSets`

## Common Failure Modes

| Symptom | Likely cause | Diagnosis |
|---------|-------------|-----------|
| Pod crashloop with "Corefile parse error" | `__KUBE_DNS_CLUSTER_IP__` placeholder not substituted | `kubectl get cm node-local-dns -n kube-system -o yaml` — check the Corefile for the literal placeholder. Re-run `deploy.sh`. |
| Pod up but no DNS interception | DaemonSet rolled out before `kube-dns-upstream` Service existed (env var missing) | `kubectl rollout restart ds node-local-dns -n kube-system`. `deploy.sh` Step 7 should auto-fix this on subsequent runs. |
| Alert `NodeLocalDNSSetupErrors` firing | iptables NOTRACK install failed | Check `kubectl logs -n kube-system <nld-pod>` for the actual setup error. Often kernel module / iptables backend incompatibility. |
| `coredns_nodecache_setup_errors_total` returning empty | Querying `:9253` instead of `:9353` | Use the `metrics-binary` PodMonitor endpoint, NOT `metrics`. |
| `nodelocaldns_*` query returns nothing | Wrong metric prefix | The correct prefix is `coredns_nodecache_*`. |
| ImagePullBackOff on cold Karpenter node | Harbor lazy-pull race (~30-60s) | Expected on first NLD pod start per node. Resolves on its own. |
| Smoke test `test_nodelocaldns_daemonset` failing on a node | Node taint not in NLD's toleration list | Add the missing taint to the explicit toleration list in `daemonset.yaml`. Do NOT switch to `operator: Exists`. |
| `NodeLocalDNSDaemonSetDegraded` alert firing | One or more pods unavailable for >15m | Investigate the unavailable pod — likely image pull, scheduling, or crashloop. Iptables-mode means cluster impact is limited to that node falling through to cluster CoreDNS. |

## Things That Are Intentionally NOT Present

- **No NetworkPolicy** — VPC CNI doesn't enforce NetworkPolicies; adding one is dead weight.
- **No PodDisruptionBudget** — DaemonSet with `maxUnavailable: 1%` is already the disruption control.
- **No ServiceMonitor** — we use PodMonitor (project convention; ServiceMonitor would need targetable Endpoints, more boilerplate for no benefit).
- **No ClusterRole/RoleBinding** — binary doesn't talk to the API server.
- **No HorizontalPodAutoscaler** — DaemonSets aren't scaled by HPA.
- **No `dnsConfig` on workload pods** — pods continue using `dnsPolicy: ClusterFirst` (default), iptables intercepts at the kube-dns ClusterIP.
- **No node startup taint for NLD readiness** — iptables-mode fallthrough handles the brief startup race; a startup taint would require touching every NodePool and runner pod toleration.
- **No kubelet `--cluster-dns` change** — deliberately unchanged so iptables-mode fallthrough works.
- **No image pre-mirror to ECR** — see Harbor proxy cache section above.
