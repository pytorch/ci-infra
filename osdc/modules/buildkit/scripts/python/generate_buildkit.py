#!/usr/bin/env python3
# /// script
# requires-python = ">=3.12"
# dependencies = ["pyyaml>=6.0"]
# ///
"""Generate BuildKit Deployment and NodePool YAMLs with dynamic pod sizing.

Computes pod resource requests based on instance type specs, ensuring
exactly pods_per_node pods fit on each node with margin for overhead.

Reads instance types, replicas, and pods_per_node via CLI arguments
(passed from deploy.sh, which reads clusters.yaml).

Outputs:
  generated/deployment.yaml  — arm64 + amd64 Deployments
  generated/nodepools.yaml   — Karpenter NodePool + EC2NodeClass per arch
"""

import argparse
import math
import sys
from pathlib import Path

# analyze_node_utilization lives in scripts/python/ at the repo root.
# Add it to sys.path so the import works both when run directly (deploy.sh)
# and when run via pytest (conftest.py also adds it).
_scripts_python = str(Path(__file__).resolve().parents[4] / "scripts" / "python")
if _scripts_python not in sys.path:
    sys.path.insert(0, _scripts_python)

from analyze_node_utilization import kubelet_reserved  # noqa: E402
from instance_specs import ENI_MAX_PODS, INSTANCE_SPECS  # noqa: E402

# ANSI colors
GREEN = "\033[0;32m"
RED = "\033[0;31m"
NC = "\033[0m"

# Sentinel for optional template lines. An optional fragment is either its YAML
# lines or this sentinel; lines equal to it are dropped when a block is assembled
# (see _deployment_block). This lets every fragment sit on its own line in the
# templates below instead of being concatenated onto an adjacent line.
_OMIT = "<<omit>>"


def log_info(msg):
    print(f"{GREEN}\u2192{NC} {msg}")


def log_error(msg):
    print(f"{RED}\u2717{NC} {msg}", file=sys.stderr)


# ---------------------------------------------------------------------------
# Overhead constants (milliCPU and MiB)
# ---------------------------------------------------------------------------
# DaemonSet overhead measured from running clusters:
#   karpenter, kube-proxy, aws-node, nvidia-device-plugin (if GPU)
DAEMONSET_OVERHEAD_CPU_M = 300  # 300m total across all DaemonSets
DAEMONSET_OVERHEAD_MEM_MI = 440  # 440Mi total across all DaemonSets

# Margin factor — 10% headroom for future growth in daemonsets/kubelet reserved
MARGIN = 0.90


def compute_pod_resources(instance_type: str, pods_per_node: int) -> dict:
    """Compute per-pod CPU and memory for Guaranteed QoS.

    Formula:
      allocatable = total - kubelet_reserved
      usable = allocatable - daemonset_overhead
      per_pod = floor(usable * margin / pods_per_node)
    """
    spec = INSTANCE_SPECS[instance_type]
    vcpu = spec["vcpu"]
    memory_gib = spec["memory_gib"]
    memory_mi = spec["memory_mi"]

    max_pods = ENI_MAX_PODS.get(instance_type, vcpu)  # fallback to vcpu if unknown
    reserved_cpu_m, reserved_mem_mi = kubelet_reserved(vcpu, memory_gib, max_pods)

    allocatable_cpu_m = vcpu * 1000 - reserved_cpu_m
    allocatable_mem_mi = memory_mi - reserved_mem_mi

    usable_cpu_m = allocatable_cpu_m - DAEMONSET_OVERHEAD_CPU_M
    usable_mem_mi = allocatable_mem_mi - DAEMONSET_OVERHEAD_MEM_MI

    pod_cpu = math.floor(usable_cpu_m * MARGIN / pods_per_node)
    pod_mem_mi = math.floor(usable_mem_mi * MARGIN / pods_per_node)

    # Truncate to whole vCPU and GiB — Guaranteed QoS with cpuManagerPolicy=static
    # works best with integer CPU counts. The ~2% loss is within the 10% margin.
    pod_mem_gi = pod_mem_mi // 1024

    return {
        "cpu": pod_cpu // 1000,
        "memory_gi": pod_mem_gi,
        "allocatable_cpu_m": allocatable_cpu_m,
        "allocatable_mem_mi": allocatable_mem_mi,
    }


def generate_deployment_yaml(
    arm64_instance: str,
    amd64_instance: str,
    replicas: int,
    pods_per_node: int,
    amd64_replicas: int | None = None,
    arm64_replicas: int | None = None,
    amd64_pods_per_node: int | None = None,
    arm64_pods_per_node: int | None = None,
    autoscaling: bool = False,
) -> str:
    """Generate the combined Deployment YAML for both architectures.

    `replicas`/`pods_per_node` are the per-arch defaults; the `amd64_*`/`arm64_*`
    overrides let the arches use different counts and packing.
    """

    amd64_replicas = amd64_replicas if amd64_replicas is not None else replicas
    arm64_replicas = arm64_replicas if arm64_replicas is not None else replicas
    amd64_pods_per_node = amd64_pods_per_node if amd64_pods_per_node is not None else pods_per_node
    arm64_pods_per_node = arm64_pods_per_node if arm64_pods_per_node is not None else pods_per_node

    arm64_res = compute_pod_resources(arm64_instance, arm64_pods_per_node)
    amd64_res = compute_pod_resources(amd64_instance, amd64_pods_per_node)

    # When KEDA owns the replica count, omit `replicas` and add a preStop drain
    # that holds the pod open until its in-flight build finishes. Each fragment is
    # either its YAML or _OMIT; _deployment_block drops _OMIT lines (matched after
    # stripping), so single lines carry their indent in the template (e.g.
    # `      {grace_line}`) while multi-line blocks self-indent. (`replicas_line`
    # is per-arch — computed below.)
    grace_line = "terminationGracePeriodSeconds: 8100" if autoscaling else _OMIT
    lifecycle_block = (
        """          lifecycle:
            preStop:
              exec:
                command: ["/bin/sh", "/opt/drain/drain.sh"]"""
        if autoscaling
        else _OMIT
    )
    drain_mount = (
        """            - name: drain
              mountPath: /opt/drain
              readOnly: true"""
        if autoscaling
        else _OMIT
    )
    drain_volume = (
        """        - name: drain
          configMap:
            name: buildkitd-drain
            defaultMode: 0555"""
        if autoscaling
        else _OMIT
    )

    log_info(
        f"arm64 ({arm64_instance}): {arm64_res['cpu']} vCPU, {arm64_res['memory_gi']}Gi per pod "
        f"(allocatable: {arm64_res['allocatable_cpu_m']}m CPU, {arm64_res['allocatable_mem_mi']}Mi mem)"
    )
    log_info(
        f"amd64 ({amd64_instance}): {amd64_res['cpu']} vCPU, {amd64_res['memory_gi']}Gi per pod "
        f"(allocatable: {amd64_res['allocatable_cpu_m']}m CPU, {amd64_res['allocatable_mem_mi']}Mi mem)"
    )

    def _deployment_block(arch, instance_type, cpu, memory_gi, replicas, pods_per_node):
        replicas_line = _OMIT if autoscaling else f"replicas: {replicas}"
        block = f"""apiVersion: apps/v1
kind: Deployment
metadata:
  name: buildkitd-{arch}
  namespace: buildkit
  labels:
    app: buildkitd
    arch: {arch}
    app.kubernetes.io/name: buildkitd
    app.kubernetes.io/component: build-service
spec:
  {replicas_line}
  strategy:
    type: RollingUpdate
    rollingUpdate:
      maxSurge: 0
      maxUnavailable: 1
  selector:
    matchLabels:
      app: buildkitd
      arch: {arch}
  template:
    metadata:
      labels:
        app: buildkitd
        arch: {arch}
    spec:
      {grace_line}
      nodeSelector:
        workload-type: buildkit
        instance-type: "{instance_type}"

      tolerations:
        - key: workload/buildkit-{arch}
          operator: Equal
          value: "true"
          effect: NoSchedule
        - key: instance-type
          operator: Equal
          value: "{instance_type}"
          effect: NoSchedule

      containers:
        - name: buildkitd
          image: moby/buildkit:v0.29.0
          args:
            - --addr
            - unix:///run/buildkit/buildkitd.sock
            - --addr
            - tcp://[::]:1234
            - --debugaddr
            - '[::]:9090'
            - --config
            - /etc/buildkit/buildkitd.toml
            - --root
            - /var/lib/buildkit

          ports:
            - name: buildkit
              containerPort: 1234
              protocol: TCP
            - name: metrics
              containerPort: 9090
              protocol: TCP
          # Guaranteed QoS: requests == limits for static CPU pinning
          # {cpu} vCPU + {memory_gi}Gi = {pods_per_node} pods per {instance_type}
          resources:
            requests:
              cpu: "{cpu}"
              memory: "{memory_gi}Gi"
            limits:
              cpu: "{cpu}"
              memory: "{memory_gi}Gi"

          env:
            - name: POD_NAME
              valueFrom:
                fieldRef:
                  fieldPath: metadata.name

          securityContext:
            privileged: true
{lifecycle_block}

          readinessProbe:
            exec:
              command:
                - buildctl
                - debug
                - workers
            initialDelaySeconds: 5
            periodSeconds: 30

          livenessProbe:
            exec:
              command:
                - buildctl
                - debug
                - workers
            initialDelaySeconds: 5
            periodSeconds: 30

          volumeMounts:
            - name: config
              mountPath: /etc/buildkit
              readOnly: true
            - name: buildkit-cache
              mountPath: /var/lib/buildkit
              subPathExpr: $(POD_NAME)
{drain_mount}

      volumes:
        - name: config
          configMap:
            name: buildkitd-config
        # NVMe-backed build cache (nodeadm localStorage or userData fallback)
        - name: buildkit-cache
          hostPath:
            path: /mnt/k8s-disks/0/buildkit-cache
            type: DirectoryOrCreate
{drain_volume}"""
        return "\n".join(line for line in block.splitlines() if line.strip() != _OMIT)

    arm64_block = _deployment_block(
        "arm64", arm64_instance, arm64_res["cpu"], arm64_res["memory_gi"], arm64_replicas, arm64_pods_per_node
    )
    amd64_block = _deployment_block(
        "amd64", amd64_instance, amd64_res["cpu"], amd64_res["memory_gi"], amd64_replicas, amd64_pods_per_node
    )

    header = f"""# BuildKit Daemon Deployments — auto-generated by generate_buildkit.py
# Do not edit by hand. Re-run: just deploy-module <cluster> buildkit
#
# arm64 pods: {arm64_res["cpu"]} vCPU + {arm64_res["memory_gi"]}Gi each ({arm64_pods_per_node} per {arm64_instance}) x {arm64_replicas}
# amd64 pods: {amd64_res["cpu"]} vCPU + {amd64_res["memory_gi"]}Gi each ({amd64_pods_per_node} per {amd64_instance}) x {amd64_replicas}
#
# Users target a specific architecture via Service name:
#   buildctl --addr tcp://buildkitd-arm64.buildkit:1234 ...  (ARM64 build)
#   buildctl --addr tcp://buildkitd-amd64.buildkit:1234 ...  (x86_64 build)
#   buildctl --addr tcp://buildkitd.buildkit:1234 ...        (any arch, round-robin)
"""

    return header + "\n" + arm64_block + "\n\n---\n" + amd64_block + "\n"


def generate_nodepools_yaml(
    arm64_instance: str,
    amd64_instance: str,
    replicas: int,
    pods_per_node: int,
    amd64_replicas: int | None = None,
    arm64_replicas: int | None = None,
    amd64_pods_per_node: int | None = None,
    arm64_pods_per_node: int | None = None,
) -> str:
    """Generate NodePool + EC2NodeClass YAML for both architectures."""

    amd64_replicas = amd64_replicas if amd64_replicas is not None else replicas
    arm64_replicas = arm64_replicas if arm64_replicas is not None else replicas
    amd64_pods_per_node = amd64_pods_per_node if amd64_pods_per_node is not None else pods_per_node
    arm64_pods_per_node = arm64_pods_per_node if arm64_pods_per_node is not None else pods_per_node

    def _nodepool_limits(instance_type, replicas, pods_per_node):
        """Compute NodePool resource limits with headroom."""
        spec = INSTANCE_SPECS[instance_type]
        # Nodes needed = ceil(replicas / pods_per_node)
        # Add 100% headroom for rolling updates
        nodes_needed = math.ceil(replicas / pods_per_node)
        max_nodes = nodes_needed * 2
        cpu_limit = max_nodes * spec["vcpu"]
        memory_limit_gi = max_nodes * spec["memory_gib"]
        return cpu_limit, memory_limit_gi

    def _nodepool_block(arch, instance_type, cpu_limit, memory_limit_gi):
        return f"""# Karpenter NodePool + EC2NodeClass: buildkit-{arch}
# Auto-generated from generate_buildkit.py — do not edit by hand.
# Instance type: {instance_type}

apiVersion: karpenter.sh/v1
kind: NodePool
metadata:
  name: buildkit-{arch}
spec:
  limits:
    cpu: "{cpu_limit}"
    memory: "{memory_limit_gi}Gi"

  disruption:
    consolidationPolicy: WhenEmpty
    consolidateAfter: 5m
    budgets:
      - nodes: "1"

  template:
    metadata:
      labels:
        workload-type: buildkit
        instance-type: "{instance_type}"

    spec:
      requirements:
        - key: kubernetes.io/arch
          operator: In
          values: ["{arch}"]
        - key: kubernetes.io/os
          operator: In
          values: ["linux"]
        - key: karpenter.sh/capacity-type
          operator: In
          values: ["on-demand"]
        - key: node.kubernetes.io/instance-type
          operator: In
          values:
            - {instance_type}

      nodeClassRef:
        group: karpenter.k8s.aws
        kind: EC2NodeClass
        name: buildkit-{arch}

      taints:
        - key: workload/buildkit-{arch}
          value: "true"
          effect: NoSchedule
        - key: instance-type
          value: "{instance_type}"
          effect: NoSchedule

---
apiVersion: karpenter.k8s.aws/v1
kind: EC2NodeClass
metadata:
  name: buildkit-{arch}
spec:
  # TODO(CVE-2026-31431): al2023@latest tracks the newest AL2023 AMI; once node
  # rotation picks up a kernel 6.12.85+ AMI, remove
  # osdc/base/kubernetes/algif-mitigation.yaml.
  # https://explore.alas.aws.amazon.com/CVE-2026-31431.html
  # TODO(CVE-2026-43284): al2023@latest tracks the newest AL2023 AMI; once node
  # rotation picks up a kernel with the DirtyFrag fix (6.1.170+ or 6.12.83+),
  # remove osdc/base/kubernetes/dirtyfrag-mitigation.yaml.
  # https://aws.amazon.com/security/security-bulletins/2026-027-aws/
  amiSelectorTerms:
    - alias: al2023@latest

  subnetSelectorTerms:
    - tags:
        karpenter.sh/discovery: "CLUSTER_NAME_PLACEHOLDER"

  securityGroupSelectorTerms:
    - tags:
        karpenter.sh/discovery: "CLUSTER_NAME_PLACEHOLDER"

  role: "CLUSTER_NAME_PLACEHOLDER-node-role"

  instanceStorePolicy: RAID0

  userData: |
    MIME-Version: 1.0
    Content-Type: multipart/mixed; boundary="==BOUNDARY=="

    --==BOUNDARY==
    Content-Type: application/node.eks.aws

    ---
    apiVersion: node.eks.aws/v1alpha1
    kind: NodeConfig
    spec:
      kubelet:
        config:
          cpuManagerPolicy: static
          topologyManagerPolicy: restricted
          topologyManagerScope: container
          topologyManagerPolicyOptions:
            prefer-closest-numa-nodes: "true"
          containerLogMaxSize: 50Mi
          containerLogMaxFiles: 5

    --==BOUNDARY==--

  blockDeviceMappings:
    - deviceName: /dev/xvda
      ebs:
        volumeSize: 100Gi
        volumeType: gp3
        iops: 3000
        throughput: 125
        deleteOnTermination: true
        encrypted: true

  metadataOptions:
    httpEndpoint: enabled
    httpProtocolIPv6: enabled
    httpPutResponseHopLimit: 1
    httpTokens: required

  tags:
    Name: "CLUSTER_NAME_PLACEHOLDER-buildkit-{arch}"
    ManagedBy: "karpenter"
    NodePool: "buildkit-{arch}"
    InstanceType: "{instance_type}"
    Architecture: "{arch}\""""

    arm64_cpu_limit, arm64_mem_limit = _nodepool_limits(arm64_instance, arm64_replicas, arm64_pods_per_node)
    amd64_cpu_limit, amd64_mem_limit = _nodepool_limits(amd64_instance, amd64_replicas, amd64_pods_per_node)

    log_info(
        f"NodePool limits — arm64: {arm64_cpu_limit} CPU, {arm64_mem_limit}Gi | "
        f"amd64: {amd64_cpu_limit} CPU, {amd64_mem_limit}Gi"
    )

    arm64_block = _nodepool_block("arm64", arm64_instance, arm64_cpu_limit, arm64_mem_limit)
    amd64_block = _nodepool_block("amd64", amd64_instance, amd64_cpu_limit, amd64_mem_limit)

    return arm64_block + "\n\n---\n" + amd64_block + "\n"


def generate_autoscaling_yaml(
    amd64_min: int,
    amd64_max: int,
    arm64_min: int,
    arm64_max: int,
    amd64_fallback: int = 0,
    arm64_fallback: int = 0,
) -> str:
    """Generate per-arch KEDA ScaledObjects.

    Each arch scales on its HAProxy backend's active build count
    (haproxy_backend_current_sessions), scraped in-cluster from the buildkit LB
    metrics endpoint — no external metrics backend. With server maxconn=1, the LB
    queues bursts while KEDA/Karpenter bring up pods; minReplicaCount keeps a warm
    baseline so the common case has a free pod immediately.
    """

    metrics_url = "http://buildkitd-lb-metrics.buildkit.svc.cluster.local:9404/metrics"

    def _scaledobject(arch, backend, min_replicas, max_replicas, fallback):
        # If KEDA can't read the scale metric (e.g. LB metrics endpoint down),
        # hold the proven fixed pool instead of letting the HPA freeze.
        fallback_yaml = ""
        if fallback:
            fallback_yaml = f"\n  fallback:\n    failureThreshold: 3\n    replicas: {fallback}"
        return f"""apiVersion: keda.sh/v1alpha1
kind: ScaledObject
metadata:
  name: buildkitd-{arch}
  namespace: buildkit
spec:
  scaleTargetRef:
    name: buildkitd-{arch}
  minReplicaCount: {min_replicas}
  maxReplicaCount: {max_replicas}{fallback_yaml}
  cooldownPeriod: 1200
  advanced:
    horizontalPodAutoscalerConfig:
      behavior:
        scaleDown:
          # No-flap: hold a pod ~20 min after idle (reuses its NVMe layer cache),
          # then shed at most max(10 pods, 20%) per 20 min.
          stabilizationWindowSeconds: 1200
          policies:
            - type: Pods
              value: 10
              periodSeconds: 1200
            - type: Percent
              value: 20
              periodSeconds: 1200
          selectPolicy: Max
  triggers:
    - type: metrics-api
      metadata:
        url: "{metrics_url}"
        format: "prometheus"
        valueLocation: 'haproxy_backend_current_sessions{{proxy="{backend}"}}'
        targetValue: "1"
"""

    header = "# KEDA autoscaling — auto-generated by generate_buildkit.py. Do not edit by hand.\n"
    return (
        header
        + "\n"
        + _scaledobject("amd64", "bk_amd64", amd64_min, amd64_max, amd64_fallback)
        + "\n---\n"
        + _scaledobject("arm64", "bk_arm64", arm64_min, arm64_max, arm64_fallback)
    )


def main():
    parser = argparse.ArgumentParser(description="Generate BuildKit Deployment and NodePool YAMLs")
    parser.add_argument("--arm64-instance-type", required=True, help="ARM64 instance type (e.g., m8gd.24xlarge)")
    parser.add_argument("--amd64-instance-type", required=True, help="AMD64 instance type (e.g., m6id.24xlarge)")
    parser.add_argument("--replicas", type=int, required=True, help="Default replicas per architecture")
    parser.add_argument("--pods-per-node", type=int, required=True, help="Default BuildKit pods per node")
    parser.add_argument("--amd64-replicas", type=int, default=None, help="Override amd64 replica count")
    parser.add_argument("--arm64-replicas", type=int, default=None, help="Override arm64 replica count")
    parser.add_argument("--amd64-pods-per-node", type=int, default=None, help="Override amd64 pods per node")
    parser.add_argument("--arm64-pods-per-node", type=int, default=None, help="Override arm64 pods per node")
    parser.add_argument("--output-dir", required=True, help="Output directory for generated YAMLs")
    parser.add_argument("--autoscaling", action="store_true", help="Generate KEDA autoscaling manifests")
    parser.add_argument("--amd64-min", type=int, default=0, help="KEDA minReplicaCount for amd64")
    parser.add_argument("--amd64-max", type=int, default=0, help="KEDA maxReplicaCount for amd64")
    parser.add_argument("--arm64-min", type=int, default=0, help="KEDA minReplicaCount for arm64")
    parser.add_argument("--arm64-max", type=int, default=0, help="KEDA maxReplicaCount for arm64")
    parser.add_argument("--amd64-fallback", type=int, default=0, help="KEDA fallback replicas for amd64 (0=off)")
    parser.add_argument("--arm64-fallback", type=int, default=0, help="KEDA fallback replicas for arm64 (0=off)")
    args = parser.parse_args()

    if args.autoscaling and not (args.amd64_max and args.arm64_max):
        log_error("--autoscaling requires --amd64-max and --arm64-max")
        return 1

    # Validate instance types
    for it in [args.arm64_instance_type, args.amd64_instance_type]:
        if it not in INSTANCE_SPECS:
            log_error(f"Unknown instance type: {it}")
            log_error(f"Known types: {', '.join(sorted(INSTANCE_SPECS.keys()))}")
            return 1

    # Validate arch matches
    if INSTANCE_SPECS[args.arm64_instance_type]["arch"] != "arm64":
        log_error(f"{args.arm64_instance_type} is not an arm64 instance type")
        return 1
    if INSTANCE_SPECS[args.amd64_instance_type]["arch"] != "amd64":
        log_error(f"{args.amd64_instance_type} is not an amd64 instance type")
        return 1

    output_dir = Path(args.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    log_info("Generating BuildKit manifests for:")
    log_info(f"  arm64: {args.arm64_instance_type}, amd64: {args.amd64_instance_type}")
    log_info(f"  replicas: {args.replicas}, pods_per_node: {args.pods_per_node}")

    # Generate deployment
    deployment_yaml = generate_deployment_yaml(
        args.arm64_instance_type,
        args.amd64_instance_type,
        args.replicas,
        args.pods_per_node,
        amd64_replicas=args.amd64_replicas,
        arm64_replicas=args.arm64_replicas,
        amd64_pods_per_node=args.amd64_pods_per_node,
        arm64_pods_per_node=args.arm64_pods_per_node,
        autoscaling=args.autoscaling,
    )
    deployment_path = output_dir / "deployment.yaml"
    deployment_path.write_text(deployment_yaml)
    log_info(f"Wrote {deployment_path}")

    # Size NodePool limits for the peak: the per-arch autoscaling ceiling
    # (amd64_max / arm64_max) when enabled, otherwise the per-arch replica counts.
    if args.autoscaling:
        nodepools_yaml = generate_nodepools_yaml(
            args.arm64_instance_type,
            args.amd64_instance_type,
            args.replicas,
            args.pods_per_node,
            amd64_replicas=args.amd64_max,
            arm64_replicas=args.arm64_max,
            amd64_pods_per_node=args.amd64_pods_per_node,
            arm64_pods_per_node=args.arm64_pods_per_node,
        )
    else:
        nodepools_yaml = generate_nodepools_yaml(
            args.arm64_instance_type,
            args.amd64_instance_type,
            args.replicas,
            args.pods_per_node,
            amd64_replicas=args.amd64_replicas,
            arm64_replicas=args.arm64_replicas,
            amd64_pods_per_node=args.amd64_pods_per_node,
            arm64_pods_per_node=args.arm64_pods_per_node,
        )
    nodepools_path = output_dir / "nodepools.yaml"
    nodepools_path.write_text(nodepools_yaml)
    log_info(f"Wrote {nodepools_path}")

    if args.autoscaling:
        autoscaling_yaml = generate_autoscaling_yaml(
            args.amd64_min,
            args.amd64_max,
            args.arm64_min,
            args.arm64_max,
            amd64_fallback=args.amd64_fallback,
            arm64_fallback=args.arm64_fallback,
        )
        autoscaling_path = output_dir / "autoscaling.yaml"
        autoscaling_path.write_text(autoscaling_yaml)
        log_info(f"Wrote {autoscaling_path}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
