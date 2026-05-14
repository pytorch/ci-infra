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

Supports three definition formats:
  - ``nodepool:``  — Legacy single-instance format (one NodePool per file)
  - ``fleet:``     — Fleet format (multiple instances share a fleet name)
  - ``fleets:``    — Multi-fleet format (GPU families with several fleets per file)
"""

import os
import shutil
import sys
from pathlib import Path

import yaml

# instance_specs lives in scripts/python/ at the repo root.  Add it to
# sys.path so the import works both when run directly (deploy.sh) and
# when run via pytest (pyproject.toml testpaths also adds it).
_scripts_python = str(Path(__file__).resolve().parents[4] / "scripts" / "python")
if _scripts_python not in sys.path:
    sys.path.insert(0, _scripts_python)

from cni_constants import (  # noqa: E402
    AZ_NAME_RE,
    BUCKET_NAME_RE,
    ENI_CONFIG_LABEL,
    bucket_eniconfig_name,
)
from instance_specs import INSTANCE_ENI_DATA, INSTANCE_SPECS  # noqa: E402

# ANSI colors
GREEN = "\033[0;32m"
RED = "\033[0;31m"
NC = "\033[0m"

# Kubelet max-pods caps for prefix-delegation math.
# Defaults follow AWS's max-pods-calculator.sh: 110 for instances with vCPU<=30,
# 250 for vCPU>30 (per-AWS recommended cap for IP-dense workloads).
DEFAULT_MAX_PODS_CAP = 110
PD_HARD_CEILING = 250
PREFIXES_PER_SLOT = 16  # Each ENI slot holds one /28 prefix = 16 IPs under PD

# Kubernetes object name length limit (DNS-1123 label).
_K8S_NAME_MAX = 63


def _validate_bucket(bucket, where, valid_buckets=None):
    """Raise ValueError if bucket is missing, malformed, or not declared by this cluster.

    ``where`` is a string used in the error message to identify the def
    (e.g. "fleet 'c7i-runner' in c7i-runner.yaml").

    ``valid_buckets`` is the set/list of bucket names declared in the target
    cluster's ``base.pod_cidr_buckets`` (sourced via ``NODEPOOLS_VALID_BUCKETS``).
    When provided, a def referencing a bucket not in this set fails fast — this
    catches the case where a cluster trims its bucket set (e.g. omits bucket-4)
    but a def still references the missing bucket, which would otherwise emit a
    NodePool whose ENI-config label points at a non-existent ENIConfig CR.
    When ``None`` (e.g. unit tests that don't set the env var), validation
    falls back to format-only and the cluster-coherence check is skipped.
    """
    if bucket is None:
        raise ValueError(f"{where}: missing required 'bucket' field — must be one of bucket-1..bucket-4")
    if not isinstance(bucket, str) or not BUCKET_NAME_RE.match(bucket):
        raise ValueError(f"{where}: invalid bucket {bucket!r} — must match 'bucket-N' where N is 1-4")
    if valid_buckets is not None and bucket not in valid_buckets:
        raise ValueError(
            f"{where}: bucket {bucket!r} is not defined in this cluster's pod_cidr_buckets "
            f"(valid: {sorted(valid_buckets)})"
        )


def _parse_valid_buckets(env_value):
    """Parse the comma-separated bucket-name list from NODEPOOLS_VALID_BUCKETS.

    Returns ``None`` when the env var is missing or empty so that test contexts
    that don't set it fall through to format-only bucket validation (preserves
    the legacy/no-cluster path used by unit tests). When provided, every entry
    must match ``BUCKET_NAME_RE`` — a malformed entry indicates the upstream
    cluster-config produced bad data and the run aborts.
    """
    if env_value is None or env_value == "":
        return None
    parts = [b.strip() for b in env_value.split(",")]
    parts = [b for b in parts if b]
    if not parts:
        return None
    for bucket in parts:
        if not BUCKET_NAME_RE.match(bucket):
            raise ValueError(
                f"NODEPOOLS_VALID_BUCKETS contains invalid bucket {bucket!r} — must match 'bucket-N' where N is 1-4"
            )
    return parts


def _parse_azs(env_value):
    """Parse the comma-separated AZ list from NODEPOOLS_AZS.

    Strips whitespace defensively. Raises ValueError on missing or empty input,
    on any malformed AZ entry, or on duplicate AZs (which would silently make
    the generator overwrite its own per-AZ output files).
    """
    if env_value is None or env_value == "":
        raise ValueError("NODEPOOLS_AZS is required — must be a non-empty comma-separated list of AZ names")
    azs = [az.strip() for az in env_value.split(",")]
    azs = [az for az in azs if az]
    if not azs:
        raise ValueError(f"NODEPOOLS_AZS={env_value!r} parsed to empty list — must contain at least one AZ")
    for az in azs:
        if not AZ_NAME_RE.match(az):
            raise ValueError(
                f"NODEPOOLS_AZS contains invalid AZ {az!r} — must match canonical AWS AZ format like 'us-east-2a'"
            )
    # Reject duplicates — duplicate AZs would silently overwrite generated
    # files (one (def, AZ) pair per output file) and skew downstream packing.
    seen = set()
    duplicates = []
    for az in azs:
        if az in seen and az not in duplicates:
            duplicates.append(az)
        seen.add(az)
    if duplicates:
        raise ValueError(
            f"NODEPOOLS_AZS contains duplicate AZ(s): {', '.join(duplicates)} — each AZ must appear at most once"
        )
    return azs


def compute_pd_max_pods(instance_type: str, *, custom_networking: bool = True) -> int:
    """Return the prefix-delegation-derived kubelet max-pods ceiling for an instance type.

    Mirrors AWS's max-pods-calculator.sh (used by AL2/AL2023 EKS AMIs). The
    ``(eni_count - 1)`` term reserves the primary ENI for node-only traffic when
    VPC CNI Custom Networking is on. ``PD_HARD_CEILING=250`` matches AWS's
    recommended cap for >30 vCPU instances; smaller instances cap at 110.

    Source: github.com/awslabs/amazon-eks-ami nodeadm/internal/kubelet/eni_max_pods.go
    """
    if instance_type not in INSTANCE_ENI_DATA:
        raise ValueError(
            f"{instance_type} missing from INSTANCE_ENI_DATA in scripts/python/instance_specs.py — "
            f"add eni_count + ipv4_per_eni from awslabs/amazon-eks-ami nodeadm/internal/kubelet/instance-info.jsonl"
        )
    if instance_type not in INSTANCE_SPECS:
        raise ValueError(f"{instance_type} missing from INSTANCE_SPECS in scripts/python/instance_specs.py")
    eni = INSTANCE_ENI_DATA[instance_type]
    spec = INSTANCE_SPECS[instance_type]
    usable_enis = eni["eni_count"] - (1 if custom_networking else 0)
    if usable_enis < 1:
        raise ValueError(
            f"{instance_type} has eni_count={eni['eni_count']} → usable_enis={usable_enis} "
            f"with custom_networking={custom_networking}; cannot host pods"
        )
    raw = usable_enis * (eni["ipv4_per_eni"] - 1) * PREFIXES_PER_SLOT + 2
    cap = PD_HARD_CEILING if spec["vcpu"] > 30 else DEFAULT_MAX_PODS_CAP
    return min(cap, raw)


def resolve_max_pods(nodepool_def: dict) -> int:
    """Resolve the kubelet max-pods value to render on an EC2NodeClass.

    - If def has explicit ``max_pods: <int>``, use it (must be <= PD ceiling).
    - Otherwise default to ``min(110, pd_ceiling)`` \u2014 conservative cap that
      keeps general pools off the kubelet 110 wall regardless of instance shape.
    """
    instance_type = nodepool_def["instance_type"]
    name = nodepool_def.get("name", "<unnamed>")
    pd_ceiling = compute_pd_max_pods(instance_type)
    explicit = nodepool_def.get("max_pods")
    if explicit is not None:
        if isinstance(explicit, bool) or not isinstance(explicit, int):
            raise ValueError(
                f"max_pods for pool '{name}' ({instance_type}) must be an int, got {type(explicit).__name__}"
            )
        if explicit > pd_ceiling:
            raise ValueError(f"max_pods={explicit} for pool '{name}' ({instance_type}) exceeds PD ceiling {pd_ceiling}")
        if explicit < 1:
            raise ValueError(f"max_pods={explicit} for pool '{name}' ({instance_type}) must be >= 1")
        return explicit
    return min(DEFAULT_MAX_PODS_CAP, pd_ceiling)


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


def generate_nodepool_yaml(nodepool_def, module_name, defs_dir=None, az=None):
    """Generate a combined NodePool + EC2NodeClass YAML string.

    When ``az`` is provided, emits an AZ-pinned variant: NodePool/EC2NodeClass
    name is suffixed with ``-{az}``, a ``topology.kubernetes.io/zone`` requirement
    is added, and the per-(bucket, AZ) ENI config label is rendered on the
    template labels block. The ``bucket`` field on the def is required when
    ``az`` is provided.
    """
    base_name = nodepool_def["name"]
    if az is not None:
        name = f"{base_name}-{az}"
        if len(name) > _K8S_NAME_MAX:
            raise ValueError(
                f"Generated NodePool name {name!r} exceeds Kubernetes DNS-1123 limit of {_K8S_NAME_MAX} chars "
                f"(len={len(name)}); shorten the def name '{base_name}'"
            )
    else:
        name = base_name
    instance_type = nodepool_def["instance_type"]
    arch = _detect_arch(instance_type, nodepool_def.get("arch"))
    is_gpu = nodepool_def.get("gpu", False)
    has_nvme = nodepool_def.get("has_nvme", False)
    user_data_script_path = nodepool_def.get("user_data_script")

    # Fleet-specific fields (only present for fleet-format defs)
    fleet_name = nodepool_def.get("fleet_name")
    weight = nodepool_def.get("weight")

    # Per-def kubelet topology overrides (e.g. B200 needs single-numa-node/pod)
    topology_policy = nodepool_def.get("topology_manager_policy", "best-effort")
    topology_scope = nodepool_def.get("topology_manager_scope", "container")

    # Read optional user data script for embedding as a MIME part
    indented_userdata = _read_user_data_script(user_data_script_path, defs_dir) if defs_dir else None

    node_disk_size = _get_node_disk_size(nodepool_def)

    # ----- Kubelet max-pods (prefix-delegation aware) -----
    # Karpenter only honors kubelet keys you set on EC2NodeClass.spec.kubelet.
    # Set ONLY here — do NOT also set in user-data NodeConfig (avoids drift).
    max_pods = resolve_max_pods(nodepool_def)

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
    # TODO(CVE-2026-31431): the AL2023 aliases / name globs below already track
    # @latest, so node rotation picks up the fix automatically once AWS ships a
    # kernel 6.12.85+ AMI. Once rolled out across all nodes, remove
    # osdc/base/kubernetes/algif-mitigation.yaml.
    # https://explore.alas.aws.amazon.com/CVE-2026-31431.html
    # TODO(CVE-2026-43284): the AL2023 aliases / name globs below already track
    # @latest, so node rotation picks up the fix automatically once AWS ships a
    # kernel with the DirtyFrag fix (6.1.170+ or 6.12.83+). Once rolled out
    # across all nodes, remove osdc/base/kubernetes/dirtyfrag-mitigation.yaml.
    # https://aws.amazon.com/security/security-bulletins/2026-027-aws/
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

    # ----- Fleet-specific YAML blocks -----
    weight_block = f"  weight: {weight}\n" if weight is not None else ""
    fleet_label = f'        node-fleet: "{fleet_name}"\n' if fleet_name else ""
    fleet_taint = (
        (f'        - key: node-fleet\n          value: "{fleet_name}"\n          effect: NoSchedule\n')
        if fleet_name
        else ""
    )

    # ----- AZ pinning + bucket ENI-config label -----
    # Both come together: when generating an AZ-pinned variant we also emit
    # the per-(bucket, AZ) eni-config label that tells VPC CNI which ENIConfig
    # CR to use for pod IP allocation on this node.
    if az is not None:
        bucket_label_value = bucket_eniconfig_name(nodepool_def["bucket"], az)
        bucket_label = f'        {ENI_CONFIG_LABEL}: "{bucket_label_value}"\n'
        zone_requirement = (
            f'        - key: topology.kubernetes.io/zone\n          operator: In\n          values: ["{az}"]\n'
        )
    else:
        bucket_label = ""
        zone_requirement = ""

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
{weight_block}\
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
{bucket_label}\
{fleet_label}\
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
{zone_requirement}\

      nodeClassRef:
        group: karpenter.k8s.aws
        kind: EC2NodeClass
        name: {name}

      taints:
{fleet_taint}\
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

  kubelet:
    maxPods: {max_pods}
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


def _process_nodepool(nodepool_def, def_file, defs_dir, output_dir, module_name, azs, region=None, valid_buckets=None):
    """Process a legacy ``nodepool:`` definition. Returns count of generated files.

    Emits one NodePool/EC2NodeClass pair per AZ in ``azs``.
    """
    name = nodepool_def.get("name")
    instance_type = nodepool_def.get("instance_type")

    if not name or not instance_type:
        raise ValueError(f"Invalid {def_file.name}: missing 'name' or 'instance_type'")

    # Bucket is required at the top level for nodepool: defs.
    _validate_bucket(
        nodepool_def.get("bucket"),
        where=f"nodepool '{name}' in {def_file.name}",
        valid_buckets=valid_buckets,
    )

    if _is_excluded_for_region(nodepool_def, region):
        log_info(f"  {def_file.name}: skipped (excluded in region '{region}')")
        return 0

    is_gpu = nodepool_def.get("gpu", False)
    has_nvme = nodepool_def.get("has_nvme", False)
    node_disk = _get_node_disk_size(nodepool_def)
    log_info(
        f"  {def_file.name}: {instance_type} ({'GPU' if is_gpu else 'CPU'}, "
        f"{nodepool_def.get('arch', 'amd64')}, node_disk={node_disk}Gi{', NVMe' if has_nvme else ''})"
    )

    if instance_type not in INSTANCE_SPECS:
        raise ValueError(
            f"Instance type '{instance_type}' in {def_file.name} "
            f"not found in INSTANCE_SPECS. "
            f"Add it to scripts/python/instance_specs.py first."
        )

    # Auto-derive fleet name for legacy defs so nodes get the node-fleet label/taint
    nodepool_def["fleet_name"] = instance_type.split(".")[0]

    generated = 0
    for az in azs:
        content = generate_nodepool_yaml(nodepool_def, module_name, defs_dir, az=az)
        out_path = output_dir / f"{name}-{az}.yaml"
        out_path.write_text(content)
        generated += 1
    return generated


def _fleet_nodepool_name(fleet_name, instance_type, name_suffix=""):
    """Compute the NodePool name for a fleet instance entry.

    Default: ``<instance>-<size>`` (e.g. ``c7i.48xlarge`` → ``c7i-48xlarge``).
    When the fleet name doesn't match the instance family prefix (e.g. a
    ``c7i-runner`` fleet built on ``c7i.*`` instances), the fleet name is used
    in place of the instance family so multiple fleets sharing the same
    instance types still produce unique NodePool names.
    """
    instance_family = instance_type.split(".")[0]
    if fleet_name == instance_family:
        name = instance_type.replace(".", "-")
    else:
        instance_size = instance_type.split(".", 1)[1].replace(".", "-")
        name = f"{fleet_name}-{instance_size}"
    if name_suffix:
        name = f"{name}{name_suffix}"
    return name


def _build_fleet_nodepool_def(fleet_data, inst, name_suffix="", extra_labels=None, bucket=None):
    """Build a nodepool_def dict from a fleet instance entry.

    ``bucket`` is the per-fleet pod-IP bucket name (e.g. ``bucket-1``); it is
    propagated onto the returned dict so ``generate_nodepool_yaml`` can render
    the per-(bucket, AZ) ENI-config label. When ``bucket`` is None it falls
    back to ``fleet_data["bucket"]``.
    """
    instance_type = inst["type"]
    name = _fleet_nodepool_name(fleet_data["name"], instance_type, name_suffix)
    if bucket is None:
        bucket = fleet_data.get("bucket")

    nodepool_def = {
        "name": name,
        "instance_type": instance_type,
        "arch": fleet_data["arch"],
        "gpu": fleet_data.get("gpu", False),
        "has_nvme": inst.get("has_nvme", False),
        "node_disk_size": inst["node_disk_size"],
        "baremetal": inst.get("baremetal", False),
        # Fleet-specific fields
        "fleet_name": fleet_data["name"],
        "weight": inst["weight"],
        # Pod-IP bucket
        "bucket": bucket,
        # Per-instance overrides
        "extra_labels": inst.get("extra_labels", {}),
        "capacity_type": inst.get("capacity_type", "on-demand"),
        "capacity_reservation_ids": inst.get("capacity_reservation_ids", []),
    }

    # Only set optional keys when explicitly provided — leaving them absent
    # lets generate_nodepool_yaml() fall through to its own defaults.
    for key in ("node_compactor", "topology_manager_policy", "topology_manager_scope", "user_data_script"):
        val = inst.get(key)
        if val is not None:
            nodepool_def[key] = val

    # max_pods: fleet-level default with per-instance override
    fleet_default_max_pods = fleet_data.get("max_pods")
    inst_max_pods = inst.get("max_pods")
    if inst_max_pods is not None:
        nodepool_def["max_pods"] = inst_max_pods
    elif fleet_default_max_pods is not None:
        nodepool_def["max_pods"] = fleet_default_max_pods

    if extra_labels:
        merged = dict(nodepool_def["extra_labels"])
        merged.update(extra_labels)
        nodepool_def["extra_labels"] = merged

    return nodepool_def


def _validate_fleet(fleet_data, def_file, valid_buckets=None):
    """Validate fleet data structure and instance types against INSTANCE_SPECS."""
    for key in ("name", "arch"):
        if key not in fleet_data:
            raise ValueError(f"Fleet in {def_file.name}: missing required key '{key}'")

    fleet_name = fleet_data["name"]
    # Bucket is required at the fleet root (pod-IP isolation).
    _validate_bucket(
        fleet_data.get("bucket"),
        where=f"fleet '{fleet_name}' in {def_file.name}",
        valid_buckets=valid_buckets,
    )

    for section in ("instances", "release"):
        for i, inst in enumerate(fleet_data.get(section, [])):
            for key in ("type", "weight", "node_disk_size"):
                if key not in inst:
                    raise ValueError(
                        f"Fleet '{fleet_name}' in {def_file.name}, {section}[{i}]: missing required key '{key}'"
                    )
            if inst["type"] not in INSTANCE_SPECS:
                raise ValueError(
                    f"Fleet '{fleet_name}' in {def_file.name}: instance type '{inst['type']}' "
                    f"not found in INSTANCE_SPECS. "
                    f"Add it to scripts/python/instance_specs.py before using it."
                )


def _is_excluded_for_region(fleet_or_pool_def, region):
    """Return True if the given region appears in the def's ``exclude_regions`` list.

    No-op (returns False) when ``region`` is empty/None or when the def has no
    ``exclude_regions`` key — keeps the generator backward-compatible for
    consumers that don't pass NODEPOOLS_REGION.
    """
    if not region:
        return False
    return region in (fleet_or_pool_def.get("exclude_regions") or [])


def _process_fleet(fleet_data, def_file, defs_dir, output_dir, module_name, azs, region=None, valid_buckets=None):
    """Process a ``fleet:`` definition. Returns count of generated files.

    Emits one NodePool/EC2NodeClass pair per (instance, AZ) pair. The fleet's
    ``bucket`` field (already validated) is propagated onto each per-instance
    nodepool_def so the generator can render the matching per-(bucket, AZ)
    ENI-config label.
    """
    _validate_fleet(fleet_data, def_file, valid_buckets=valid_buckets)

    fleet_name = fleet_data["name"]
    if _is_excluded_for_region(fleet_data, region):
        log_info(f"  Fleet '{fleet_name}': skipped (excluded in region '{region}')")
        return 0

    bucket = fleet_data["bucket"]
    instances = fleet_data.get("instances", [])
    release_instances = fleet_data.get("release", [])

    log_info(f"  Fleet '{fleet_name}': {len(instances)} instance(s)")

    generated = 0
    for inst in instances:
        nodepool_def = _build_fleet_nodepool_def(fleet_data, inst, bucket=bucket)
        for az in azs:
            content = generate_nodepool_yaml(nodepool_def, module_name, defs_dir, az=az)
            out_path = output_dir / f"{nodepool_def['name']}-{az}.yaml"
            out_path.write_text(content)
            generated += 1

    if release_instances:
        log_info(f"  Fleet '{fleet_name}': {len(release_instances)} release instance(s)")
        for inst in release_instances:
            nodepool_def = _build_fleet_nodepool_def(
                fleet_data,
                inst,
                name_suffix="-release",
                extra_labels={"osdc.io/runner-class": "release"},
                bucket=bucket,
            )
            for az in azs:
                content = generate_nodepool_yaml(nodepool_def, module_name, defs_dir, az=az)
                out_path = output_dir / f"{nodepool_def['name']}-{az}.yaml"
                out_path.write_text(content)
                generated += 1

    return generated


def main():
    script_dir = Path(__file__).parent
    module_dir = script_dir.parent.parent
    defs_dir = Path(os.environ["NODEPOOLS_DEFS_DIR"]) if "NODEPOOLS_DEFS_DIR" in os.environ else module_dir / "defs"
    output_dir = (
        Path(os.environ["NODEPOOLS_OUTPUT_DIR"]) if "NODEPOOLS_OUTPUT_DIR" in os.environ else module_dir / "generated"
    )
    module_name = os.environ.get("NODEPOOLS_MODULE_NAME", "nodepools")
    # Cluster region — used to honor exclude_regions on fleet/nodepool defs.
    # When unset, exclude_regions is a no-op (backward-compatible).
    region = os.environ.get("NODEPOOLS_REGION", "")

    # AZ list — required: generator emits one NodePool per AZ per def.
    # Sourced from NODEPOOLS_AZS, populated by deploy.sh from cluster-config.py.
    try:
        azs = _parse_azs(os.environ.get("NODEPOOLS_AZS"))
    except ValueError as e:
        log_error(str(e))
        return 1

    # Valid bucket set — optional: when provided by deploy.sh (sourced from
    # cluster-config.py valid-buckets), refuses any def whose bucket isn't
    # declared in the target cluster's base.pod_cidr_buckets. When unset
    # (e.g. unit tests), bucket validation falls back to format-only.
    try:
        valid_buckets = _parse_valid_buckets(os.environ.get("NODEPOOLS_VALID_BUCKETS"))
    except ValueError as e:
        log_error(str(e))
        return 1

    # Safety: refuse to wipe defs if NODEPOOLS_OUTPUT_DIR is misconfigured
    # (e.g. set equal to NODEPOOLS_DEFS_DIR or any ancestor of it). The next
    # step rmtree's output_dir, which would silently destroy the def files.
    resolved_defs = defs_dir.resolve()
    resolved_output = output_dir.resolve()
    if resolved_output == resolved_defs or resolved_defs.is_relative_to(resolved_output):
        log_error(
            f"NODEPOOLS_OUTPUT_DIR ({resolved_output}) equals or is an ancestor of "
            f"NODEPOOLS_DEFS_DIR ({resolved_defs}); refusing to rmtree to avoid "
            f"destroying definition files. Set NODEPOOLS_OUTPUT_DIR to a separate path."
        )
        return 1

    # Clean output dir so removed defs don't leave stale generated files
    if output_dir.exists():
        shutil.rmtree(output_dir)
    output_dir.mkdir()

    def_files = sorted(defs_dir.glob("*.yaml"))
    if not def_files:
        log_error(f"No definition files found in {defs_dir}")
        return 1

    log_info(f"Found {len(def_files)} nodepool definition(s); expanding across AZs: {','.join(azs)}")
    if region:
        log_info(f"Target region: {region} (fleets with matching exclude_regions will be skipped)")

    generated = 0
    skipped = []
    for def_file in def_files:
        try:
            with open(def_file) as f:
                data = yaml.safe_load(f)

            if not data:
                log_error(f"Invalid {def_file.name}: empty file")
                skipped.append(def_file.name)
                continue

            # Determine format: fleet, fleets, or legacy nodepool
            if "fleet" in data:
                generated += _process_fleet(
                    data["fleet"],
                    def_file,
                    defs_dir,
                    output_dir,
                    module_name,
                    azs,
                    region,
                    valid_buckets=valid_buckets,
                )
            elif "fleets" in data:
                for fleet_data in data["fleets"]:
                    generated += _process_fleet(
                        fleet_data,
                        def_file,
                        defs_dir,
                        output_dir,
                        module_name,
                        azs,
                        region,
                        valid_buckets=valid_buckets,
                    )
            elif "nodepool" in data:
                generated += _process_nodepool(
                    data["nodepool"],
                    def_file,
                    defs_dir,
                    output_dir,
                    module_name,
                    azs,
                    region,
                    valid_buckets=valid_buckets,
                )
            else:
                log_error(f"Invalid {def_file.name}: missing 'nodepool', 'fleet', or 'fleets' key")
                skipped.append(def_file.name)
                continue

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
