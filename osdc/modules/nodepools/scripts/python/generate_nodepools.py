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

import os
import shutil
import sys
from pathlib import Path

import yaml

# ANSI colors
GREEN = "\033[0;32m"
RED = "\033[0;31m"
NC = "\033[0m"


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
    family = instance_type.split(".")[0]
    if "g" in family[2:]:
        return "arm64"
    return "amd64"


def _get_node_disk_size(nodepool_def):
    """Return the EBS volume size for a node.

    Uses `node_disk_size` from the def directly. This value should be
    pre-computed as the worst-case total: max concurrent pods (determined
    by CPU/memory/GPU constraints) x largest per-pod disk + OS overhead.
    """
    node_disk = nodepool_def.get("node_disk_size")
    if node_disk:
        return node_disk

    # Legacy fallback: compute from max_pods_per_node * disk_size + 100
    os_overhead = 100  # Gi
    max_pods = nodepool_def.get("max_pods_per_node", 10)
    per_pod_disk = nodepool_def.get("disk_size", 100)
    return max_pods * per_pod_disk + os_overhead


def _read_user_data_script(script_path, defs_dir):
    """Read and indent a user data script for embedding in YAML userData.

    The script_path is relative to the module directory (parent of defs/).
    Returns the indented script content ready for MIME embedding, or None.
    """
    if not script_path:
        return None

    module_dir = defs_dir.parent
    full_path = module_dir / script_path
    if not full_path.exists():
        raise FileNotFoundError(f"user_data_script not found: {full_path}")

    script_content = full_path.read_text()
    # Indent 4 spaces for YAML embedding inside the userData MIME block
    return "\n".join("    " + line if line.strip() else "" for line in script_content.splitlines())


def _user_data_script_mime_part(indented_script):
    """Return the text/x-shellscript MIME part, or empty string if no script."""
    if not indented_script:
        return ""
    return f"""
    --==BOUNDARY==
    Content-Type: text/x-shellscript; charset="us-ascii"

{indented_script}
"""


def generate_nodepool_yaml(nodepool_def, module_name, defs_dir=None):
    """Generate a combined NodePool + EC2NodeClass YAML string."""
    name = nodepool_def["name"]
    instance_type = nodepool_def["instance_type"]
    arch = _detect_arch(instance_type, nodepool_def.get("arch"))
    is_gpu = nodepool_def.get("gpu", False)
    has_nvme = nodepool_def.get("has_nvme", False)
    user_data_script_path = nodepool_def.get("user_data_script")

    # Per-def kubelet topology overrides (e.g. B200 needs single-numa-node/pod)
    topology_policy = nodepool_def.get("topology_manager_policy", "restricted")
    topology_scope = nodepool_def.get("topology_manager_scope", "container")

    # Read optional user data script for embedding as a MIME part
    indented_userdata = _read_user_data_script(user_data_script_path, defs_dir) if defs_dir else None

    node_disk_size = _get_node_disk_size(nodepool_def)

    # ----- Capacity block / reservation support -----
    capacity_type = nodepool_def.get("capacity_type", "on-demand")
    capacity_reservation_ids = nodepool_def.get("capacity_reservation_ids", [])

    # ----- Node compactor opt-in -----
    # NodePools labeled osdc.io/node-compactor are managed by the compactor
    # controller, which handles consolidation via NoSchedule taints instead
    # of Karpenter's disruptive consolidation.
    # Default comes from cluster-level config (via env var), not per-def hardcode
    cluster_compactor_enabled = os.environ.get("NODEPOOLS_COMPACTOR_ENABLED", "false").lower() == "true"
    compactor_enabled = nodepool_def.get("node_compactor", cluster_compactor_enabled)

    if compactor_enabled:
        # Compactor handles underutilized case; Karpenter only handles empty
        consolidation_policy = "WhenEmpty"
        consolidation_after = "2m"
        compactor_label = '    osdc.io/node-compactor: "true"\n'
    else:
        consolidation_policy = "WhenEmptyOrUnderutilized"
        consolidation_after = "3h"
        compactor_label = ""

    # ----- GPU vs CPU settings -----
    if is_gpu:
        ami_family_block = "  amiFamily: AL2023"
        ami_selector_block = """  amiSelectorTerms:
    - name: "amazon-eks-node-al2023-x86_64-nvidia-*\""""
        if compactor_enabled:
            disruption_budget = os.environ.get("NODEPOOLS_GPU_DISRUPTION_BUDGET", "100%")
            consolidation_after = os.environ.get("NODEPOOLS_GPU_CONSOLIDATE_AFTER", "2m")
        else:
            disruption_budget = "0"
            consolidation_policy = "WhenEmptyOrUnderutilized"
            consolidation_after = os.environ.get("NODEPOOLS_GPU_CONSOLIDATE_AFTER", "3h")
        iops = 16000
        throughput = 1000

        gpu_labels = '        nvidia.com/gpu: "true"\n'
        gpu_taints = """        - key: nvidia.com/gpu
          value: "true"
          effect: NoSchedule
"""
        gpu_tags = '    GPU: "nvidia"\n'
    else:
        ami_family_block = ""
        ami_selector_block = """  amiSelectorTerms:
    - alias: al2023@latest"""
        if compactor_enabled:
            # Compactor-managed: all empty nodes can be cleaned simultaneously
            disruption_budget = os.environ.get("NODEPOOLS_CPU_DISRUPTION_BUDGET", "100%")
            consolidation_after = os.environ.get("NODEPOOLS_CPU_CONSOLIDATE_AFTER", "2m")
        else:
            consolidation_policy = "WhenEmptyOrUnderutilized"
            consolidation_after = os.environ.get("NODEPOOLS_CPU_CONSOLIDATE_AFTER", "3h")
            disruption_budget = os.environ.get("NODEPOOLS_CPU_DISRUPTION_BUDGET", "10%")

        iops = 16000
        throughput = 1000

        gpu_labels = ""
        gpu_taints = ""
        gpu_tags = ""

    # ----- Baremetal consolidate_after override -----
    # Baremetal instances take much longer to provision, so they get a longer
    # consolidation window to avoid unnecessary churn.
    if nodepool_def.get("baremetal", False):
        baremetal_override = os.environ.get("NODEPOOLS_BAREMETAL_CONSOLIDATE_AFTER")
        if baremetal_override:
            consolidation_after = baremetal_override

    # ----- Capacity reservation block (EC2NodeClass) -----
    if capacity_reservation_ids:
        cr_lines = "\n".join(f'    - id: "{cr_id}"' for cr_id in capacity_reservation_ids)
        capacity_reservation_block = f"""
  capacityReservationSelectorTerms:
{cr_lines}
"""
    else:
        capacity_reservation_block = "\n"

    # ----- Extra labels (e.g. runner-class for release runners) -----
    extra_labels = nodepool_def.get("extra_labels", {})
    extra_labels_yaml = ""
    for label_key, label_value in extra_labels.items():
        extra_labels_yaml += f'        {label_key}: "{label_value}"\n'

    # ----- Build YAML -----
    yaml_content = f"""# Karpenter NodePool + EC2NodeClass: {instance_type}
# Auto-generated from defs/{name}.yaml — do not edit by hand.

apiVersion: karpenter.sh/v1
kind: NodePool
metadata:
  name: {name}
  labels:
    osdc.io/module: {module_name}
{compactor_label}\
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
{extra_labels_yaml}\
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
          values: ["{capacity_type}"]
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
  labels:
    osdc.io/module: {module_name}
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
{"  instanceStorePolicy: RAID0" + chr(10) if has_nvme else ""}\
{capacity_reservation_block}\
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
          topologyManagerPolicy: {topology_policy}
          topologyManagerScope: {topology_scope}
{"          topologyManagerPolicyOptions:" + chr(10) + '            prefer-closest-numa-nodes: "true"' + chr(10) if topology_policy in ("restricted", "best-effort") else ""}\
          containerLogMaxSize: 50Mi
          containerLogMaxFiles: 5
{_user_data_script_mime_part(indented_userdata)}
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
    defs_dir = Path(os.environ["NODEPOOLS_DEFS_DIR"]) if "NODEPOOLS_DEFS_DIR" in os.environ else module_dir / "defs"
    output_dir = (
        Path(os.environ["NODEPOOLS_OUTPUT_DIR"]) if "NODEPOOLS_OUTPUT_DIR" in os.environ else module_dir / "generated"
    )
    module_name = os.environ.get("NODEPOOLS_MODULE_NAME", "nodepools")

    # Clean output dir so removed defs don't leave stale generated files
    if output_dir.exists():
        shutil.rmtree(output_dir)
    output_dir.mkdir()

    def_files = sorted(defs_dir.glob("*.yaml"))
    if not def_files:
        log_error(f"No definition files found in {defs_dir}")
        return 1

    log_info(f"Found {len(def_files)} nodepool definition(s)")

    generated = 0
    skipped = []
    for def_file in def_files:
        try:
            with open(def_file) as f:
                data = yaml.safe_load(f)

            if not data or "nodepool" not in data:
                log_error(f"Invalid {def_file.name}: missing 'nodepool' key")
                skipped.append(def_file.name)
                continue

            nodepool_def = data["nodepool"]
            name = nodepool_def.get("name")
            instance_type = nodepool_def.get("instance_type")

            if not name or not instance_type:
                log_error(f"Invalid {def_file.name}: missing 'name' or 'instance_type'")
                skipped.append(def_file.name)
                continue

            is_gpu = nodepool_def.get("gpu", False)
            has_nvme = nodepool_def.get("has_nvme", False)
            node_disk = _get_node_disk_size(nodepool_def)
            log_info(
                f"  {def_file.name}: {instance_type} ({'GPU' if is_gpu else 'CPU'}, {nodepool_def.get('arch', 'amd64')}, node_disk={node_disk}Gi{', NVMe' if has_nvme else ''})"
            )

            content = generate_nodepool_yaml(nodepool_def, module_name, defs_dir)
            out_path = output_dir / f"{name}.yaml"
            out_path.write_text(content)
            generated += 1

        except Exception as e:
            log_error(f"Failed to process {def_file.name}: {e}")
            return 1

    if skipped:
        log_error(f"Aborting: {len(skipped)} definition(s) were invalid: {', '.join(skipped)}")
        return 1

    log_info(f"Generated {generated} NodePool(s) in {output_dir}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
