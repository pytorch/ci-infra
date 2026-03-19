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

from analyze_node_utilization import ENI_MAX_PODS, kubelet_reserved  # noqa: E402

# ANSI colors
GREEN = "\033[0;32m"
RED = "\033[0;31m"
NC = "\033[0m"


def log_info(msg):
    print(f"{GREEN}\u2192{NC} {msg}")


def log_error(msg):
    print(f"{RED}\u2717{NC} {msg}", file=sys.stderr)


# ---------------------------------------------------------------------------
# Instance type specifications
# ---------------------------------------------------------------------------
# Static lookup table — add entries here when supporting new instance types.
# Values are from AWS documentation (total vCPU, total GiB memory).
# The kubelet_reserved function and ENI_MAX_PODS table are imported from
# analyze_node_utilization to avoid formula duplication.

INSTANCE_SPECS = {
    "m8gd.24xlarge": {"vcpu": 96, "memory_gib": 384, "arch": "arm64"},
    "m7gd.16xlarge": {"vcpu": 64, "memory_gib": 256, "arch": "arm64"},
    "m8gd.16xlarge": {"vcpu": 64, "memory_gib": 256, "arch": "arm64"},
    "m6id.24xlarge": {"vcpu": 96, "memory_gib": 384, "arch": "amd64"},
    "c7gd.16xlarge": {"vcpu": 64, "memory_gib": 128, "arch": "arm64"},
}

# ---------------------------------------------------------------------------
# Overhead constants (milliCPU and MiB)
# ---------------------------------------------------------------------------
# DaemonSet overhead measured from running clusters:
#   karpenter, git-cache-warmer, kube-proxy, aws-node, nvidia-device-plugin (if GPU)
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

    max_pods = ENI_MAX_PODS.get(instance_type, vcpu)  # fallback to vcpu if unknown
    reserved_cpu_m, reserved_mem_mi = kubelet_reserved(vcpu, memory_gib, max_pods)

    allocatable_cpu_m = vcpu * 1000 - reserved_cpu_m
    allocatable_mem_mi = memory_gib * 1024 - reserved_mem_mi

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
) -> str:
    """Generate the combined Deployment YAML for both architectures."""

    arm64_res = compute_pod_resources(arm64_instance, pods_per_node)
    amd64_res = compute_pod_resources(amd64_instance, pods_per_node)

    log_info(
        f"arm64 ({arm64_instance}): {arm64_res['cpu']} vCPU, {arm64_res['memory_gi']}Gi per pod "
        f"(allocatable: {arm64_res['allocatable_cpu_m']}m CPU, {arm64_res['allocatable_mem_mi']}Mi mem)"
    )
    log_info(
        f"amd64 ({amd64_instance}): {amd64_res['cpu']} vCPU, {amd64_res['memory_gi']}Gi per pod "
        f"(allocatable: {amd64_res['allocatable_cpu_m']}m CPU, {amd64_res['allocatable_mem_mi']}Mi mem)"
    )

    def _deployment_block(arch, instance_type, cpu, memory_gi):
        return f"""apiVersion: apps/v1
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
  replicas: {replicas}
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
      nodeSelector:
        workload-type: buildkit
        instance-type: "{instance_type}"

      tolerations:
        - key: instance-type
          operator: Equal
          value: "{instance_type}"
          effect: NoSchedule

      containers:
        - name: buildkitd
          image: moby/buildkit:v0.27.1
          args:
            - --addr
            - unix:///run/buildkit/buildkitd.sock
            - --addr
            - tcp://0.0.0.0:1234
            - --config
            - /etc/buildkit/buildkitd.toml
            - --root
            - /var/lib/buildkit

          ports:
            - name: buildkit
              containerPort: 1234
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
            - name: git-cache
              mountPath: /opt/git-cache
              readOnly: true

      volumes:
        - name: config
          configMap:
            name: buildkitd-config
        # NVMe-backed build cache (nodeadm localStorage or userData fallback)
        - name: buildkit-cache
          hostPath:
            path: /mnt/k8s-disks/0/buildkit-cache
            type: DirectoryOrCreate
        # Git object cache (maintained by git-cache-warmer DaemonSet)
        - name: git-cache
          hostPath:
            path: /mnt/k8s-disks/0/git-cache
            type: DirectoryOrCreate"""

    arm64_block = _deployment_block("arm64", arm64_instance, arm64_res["cpu"], arm64_res["memory_gi"])
    amd64_block = _deployment_block("amd64", amd64_instance, amd64_res["cpu"], amd64_res["memory_gi"])

    header = f"""# BuildKit Daemon Deployments — auto-generated by generate_buildkit.py
# Do not edit by hand. Re-run: just deploy-module <cluster> buildkit
#
# arm64 pods: {arm64_res["cpu"]} vCPU + {arm64_res["memory_gi"]}Gi each ({pods_per_node} per {arm64_instance})
# amd64 pods: {amd64_res["cpu"]} vCPU + {amd64_res["memory_gi"]}Gi each ({pods_per_node} per {amd64_instance})
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
) -> str:
    """Generate NodePool + EC2NodeClass YAML for both architectures."""

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
      - nodes: "0"

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
        - key: instance-type
          value: "{instance_type}"
          effect: NoSchedule

      startupTaints:
        - key: git-cache-not-ready
          value: "true"
          effect: NoSchedule

---
apiVersion: karpenter.k8s.aws/v1
kind: EC2NodeClass
metadata:
  name: buildkit-{arch}
spec:
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
    httpProtocolIPv6: disabled
    httpPutResponseHopLimit: 1
    httpTokens: required

  tags:
    Name: "CLUSTER_NAME_PLACEHOLDER-buildkit-{arch}"
    ManagedBy: "karpenter"
    NodePool: "buildkit-{arch}"
    InstanceType: "{instance_type}"
    Architecture: "{arch}\""""

    arm64_cpu_limit, arm64_mem_limit = _nodepool_limits(arm64_instance, replicas, pods_per_node)
    amd64_cpu_limit, amd64_mem_limit = _nodepool_limits(amd64_instance, replicas, pods_per_node)

    log_info(
        f"NodePool limits — arm64: {arm64_cpu_limit} CPU, {arm64_mem_limit}Gi | "
        f"amd64: {amd64_cpu_limit} CPU, {amd64_mem_limit}Gi"
    )

    arm64_block = _nodepool_block("arm64", arm64_instance, arm64_cpu_limit, arm64_mem_limit)
    amd64_block = _nodepool_block("amd64", amd64_instance, amd64_cpu_limit, amd64_mem_limit)

    return arm64_block + "\n\n---\n" + amd64_block + "\n"


def main():
    parser = argparse.ArgumentParser(description="Generate BuildKit Deployment and NodePool YAMLs")
    parser.add_argument("--arm64-instance-type", required=True, help="ARM64 instance type (e.g., m8gd.24xlarge)")
    parser.add_argument("--amd64-instance-type", required=True, help="AMD64 instance type (e.g., m6id.24xlarge)")
    parser.add_argument("--replicas", type=int, required=True, help="Replicas per architecture")
    parser.add_argument("--pods-per-node", type=int, required=True, help="BuildKit pods per node")
    parser.add_argument("--output-dir", required=True, help="Output directory for generated YAMLs")
    args = parser.parse_args()

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
    )
    deployment_path = output_dir / "deployment.yaml"
    deployment_path.write_text(deployment_yaml)
    log_info(f"Wrote {deployment_path}")

    # Generate nodepools
    nodepools_yaml = generate_nodepools_yaml(
        args.arm64_instance_type,
        args.amd64_instance_type,
        args.replicas,
        args.pods_per_node,
    )
    nodepools_path = output_dir / "nodepools.yaml"
    nodepools_path.write_text(nodepools_yaml)
    log_info(f"Wrote {nodepools_path}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
