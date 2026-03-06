#!/usr/bin/env python3
# /// script
# requires-python = ">=3.12"
# dependencies = ["pyyaml>=6.0"]
# ///
"""Generate Karpenter NodePool YAMLs from nodepool definitions.

Reads:  modules/nodepools/defs/*.yaml
Writes: modules/nodepools/generated/*.yaml (one per definition)

Each generated file contains a Karpenter NodePool + EC2NodeClass pair.
CLUSTER_NAME_PLACEHOLDER is used everywhere a cluster name would go —
deploy.sh does sed replacement at apply time with the actual cluster name.
"""

import sys
from pathlib import Path

import yaml

# ANSI colors
GREEN = '\033[0;32m'
RED = '\033[0;31m'
NC = '\033[0m'

def log_info(msg):
    print(f"{GREEN}\u2192{NC} {msg}")

def log_error(msg):
    print(f"{RED}\u2717{NC} {msg}")


def _detect_arch(instance_type, arch_hint):
    """Return the Kubernetes architecture label value.

    Uses the explicit arch from the def, with a fallback heuristic based on
    Graviton instance families (c7g, m7g, c7gd, etc.).
    """
    if arch_hint:
        return arch_hint
    # Graviton instance families contain 'g' after the generation number.
    family = instance_type.split('.')[0]
    if 'g' in family[2:]:
        return 'arm64'
    return 'amd64'


def _compute_node_disk_size(instance_type, per_pod_disk, max_pods):
    """Compute EBS volume size for a node based on worst-case pod packing.

    The node disk must hold ephemeral storage for all concurrent pods,
    plus ~100Gi overhead for the OS, container images, and kubelet.

    max_pods comes from the nodepool def — it's the expected max concurrent
    pods per node based on actual runner sizing (CPU/memory/GPU constraints).
    For GPU pools this is typically the GPU count; for CPU pools it depends
    on the smallest runner that targets this pool.
    """
    os_overhead = 100  # Gi
    return max_pods * per_pod_disk + os_overhead


def generate_nodepool_yaml(nodepool_def):
    """Generate a combined NodePool + EC2NodeClass YAML string."""
    name = nodepool_def['name']
    instance_type = nodepool_def['instance_type']
    arch = _detect_arch(instance_type, nodepool_def.get('arch'))
    disk_size = nodepool_def.get('disk_size', 100)
    is_gpu = nodepool_def.get('gpu', False)

    max_pods = nodepool_def.get('max_pods_per_node', 10)
    node_disk_size = _compute_node_disk_size(instance_type, disk_size, max_pods)

    # ----- GPU vs CPU settings -----
    if is_gpu:
        ami_family_block = '  amiFamily: AL2023'
        ami_selector_block = """  amiSelectorTerms:
    - name: "amazon-eks-node-al2023-x86_64-nvidia-*\""""
        consolidation_policy = 'WhenEmptyOrUnderutilized'
        consolidation_after = '3h'
        disruption_budget = '0'
        iops = 5000
        throughput = 250

        gpu_labels = '        nvidia.com/gpu: "true"\n'
        gpu_taints = """        - key: nvidia.com/gpu
          value: "true"
          effect: NoSchedule
"""
        gpu_setup = """
          # Enable GPU persistence mode for consistent performance
          nvidia-smi -pm 1 || true
"""
        gpu_tags = '    GPU: "nvidia"\n'
    else:
        ami_family_block = ''
        ami_selector_block = """  amiSelectorTerms:
    - alias: al2023@latest"""
        consolidation_policy = 'WhenEmptyOrUnderutilized'
        consolidation_after = '3h'
        disruption_budget = '10%'
        iops = 3000
        throughput = 125

        gpu_labels = ''
        gpu_taints = ''
        gpu_setup = ''
        gpu_tags = ''

    # ----- Build YAML -----
    yaml_content = f"""# Karpenter NodePool + EC2NodeClass: {instance_type}
# Auto-generated from defs/{name}.yaml — do not edit by hand.

apiVersion: karpenter.sh/v1
kind: NodePool
metadata:
  name: {name}
spec:
  disruption:
    consolidationPolicy: {consolidation_policy}
    consolidateAfter: {consolidation_after}
    budgets:
      - nodes: "{disruption_budget}"

  template:
    metadata:
      labels:
        workload-type: github-runner
        instance-type: "{instance_type}"
{gpu_labels}\
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
        name: {name}

      taints:
        - key: instance-type
          value: "{instance_type}"
          effect: NoSchedule
{gpu_taints}\
      startupTaints:
        - key: git-cache-not-ready
          value: "true"
          effect: NoSchedule

---
apiVersion: karpenter.k8s.aws/v1
kind: EC2NodeClass
metadata:
  name: {name}
spec:
{ami_family_block + chr(10) if ami_family_block else ""}\
{ami_selector_block}

  subnetSelectorTerms:
    - tags:
        karpenter.sh/discovery: "CLUSTER_NAME_PLACEHOLDER"

  securityGroupSelectorTerms:
    - tags:
        karpenter.sh/discovery: "CLUSTER_NAME_PLACEHOLDER"

  role: "CLUSTER_NAME_PLACEHOLDER-node-role"

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
          topologyManagerPolicy: single-numa-node
          topologyManagerScope: pod

      instance:
        localUserData: |
          #!/bin/bash
          set -euo pipefail

          # ---- Registry mirror configuration (Harbor pull-through cache) ----
          echo "Post-bootstrap: Configuring registry mirrors..."
          HARBOR_PORT=30002

          for registry_project in \\
            "docker.io dockerhub-cache https://docker.io" \\
            "ghcr.io ghcr-cache https://ghcr.io" \\
            "public.ecr.aws ecr-public-cache https://public.ecr.aws" \\
            "nvcr.io nvcr-cache https://nvcr.io" \\
            "registry.k8s.io k8s-cache https://registry.k8s.io" \\
            "quay.io quay-cache https://quay.io"; do
            set -- $registry_project
            registry=$1; project=$2; upstream=$3
            mkdir -p /etc/containerd/certs.d/$registry
            cat > /etc/containerd/certs.d/$registry/hosts.toml <<MIRRORS
          server = "$upstream"

          [host."http://localhost:$HARBOR_PORT/v2/$project"]
            capabilities = ["pull", "resolve"]
            skip_verify = true
            override_path = true

          [host."$upstream"]
            capabilities = ["pull", "resolve"]
          MIRRORS
          done
          echo "Registry mirrors configured for 6 registries"

          # ---- CPU performance tuning ----
          echo "Post-bootstrap: Configuring performance settings for {instance_type}..."

          for cpu_governor in /sys/devices/system/cpu/cpu*/cpufreq/scaling_governor; do
            if [ -f "$cpu_governor" ]; then
              echo "performance" > "$cpu_governor" || true
            fi
          done

          cat > /etc/systemd/system/cpu-performance.service <<'EOFS'
          [Unit]
          Description=Set CPU governor to performance mode
          After=multi-user.target

          [Service]
          Type=oneshot
          ExecStart=/bin/bash -c \\
            'for gov in /sys/devices/system/cpu/cpu*/cpufreq/scaling_governor; \\
            do echo performance > $gov 2>/dev/null || true; done'
          RemainAfterExit=yes

          [Install]
          WantedBy=multi-user.target
          EOFS

          systemctl daemon-reload
          systemctl enable cpu-performance.service
          systemctl start cpu-performance.service
{gpu_setup}\
          echo "Performance configuration complete for {instance_type}"

    --==BOUNDARY==--

  blockDeviceMappings:
    - deviceName: /dev/xvda
      ebs:
        volumeSize: {node_disk_size}Gi
        volumeType: gp3
        iops: {iops}
        throughput: {throughput}
        deleteOnTermination: true
        encrypted: true

  metadataOptions:
    httpEndpoint: enabled
    httpProtocolIPv6: disabled
    httpPutResponseHopLimit: 1
    httpTokens: required

  tags:
    Name: "CLUSTER_NAME_PLACEHOLDER-{name}"
    ManagedBy: "karpenter"
    NodePool: "{name}"
    InstanceType: "{instance_type}"
{gpu_tags}"""

    return yaml_content


def main():
    script_dir = Path(__file__).parent
    module_dir = script_dir.parent.parent
    defs_dir = module_dir / 'defs'
    output_dir = module_dir / 'generated'

    output_dir.mkdir(exist_ok=True)

    def_files = sorted(defs_dir.glob('*.yaml'))
    if not def_files:
        log_error(f"No definition files found in {defs_dir}")
        return 1

    log_info(f"Found {len(def_files)} nodepool definition(s)")

    generated = 0
    for def_file in def_files:
        try:
            with open(def_file) as f:
                data = yaml.safe_load(f)

            if not data or 'nodepool' not in data:
                log_error(f"Skipping {def_file.name}: missing 'nodepool' key")
                continue

            nodepool_def = data['nodepool']
            name = nodepool_def.get('name')
            instance_type = nodepool_def.get('instance_type')

            if not name or not instance_type:
                log_error(f"Skipping {def_file.name}: missing 'name' or 'instance_type'")
                continue

            is_gpu = nodepool_def.get('gpu', False)
            max_pods = nodepool_def.get('max_pods_per_node', 10)
            node_disk = _compute_node_disk_size(instance_type, nodepool_def.get('disk_size', 100), max_pods)
            log_info(f"  {def_file.name}: {instance_type} ({'GPU' if is_gpu else 'CPU'}, {nodepool_def.get('arch', 'amd64')}, pod={nodepool_def.get('disk_size', 100)}Gi, node={node_disk}Gi)")

            content = generate_nodepool_yaml(nodepool_def)
            out_path = output_dir / f"{name}.yaml"
            out_path.write_text(content)
            generated += 1

        except Exception as e:
            log_error(f"Failed to process {def_file.name}: {e}")
            return 1

    log_info(f"Generated {generated} NodePool(s) in {output_dir}")
    return 0


if __name__ == '__main__':
    sys.exit(main())
