"""AWS instance type specifications and ENI pod limits.

Single source of truth for instance hardware specs used across OSDC scripts:
- analyze_node_utilization.py (runner packing analysis)
- generate_buildkit.py (BuildKit pod sizing)
- collect_instance_memory.py (live memory collection)
- simulate_cluster.py (cluster simulation)
- generate_nodepools.py (Karpenter EC2NodeClass kubelet.maxPods PD math)

This file owns THREE dicts that MUST stay in lock-step. When adding a new
instance type (runner nodepool or BuildKit node), populate ALL THREE:

- ``INSTANCE_SPECS``    — vCPU / memory / GPU / arch (compute facts).
- ``INSTANCE_ENI_DATA`` — raw AWS ENI shape (``eni_count`` + ``ipv4_per_eni``),
  populated from upstream
  ``awslabs/amazon-eks-ami nodeadm/internal/kubelet/instance-info.jsonl``.
  Required by the nodepools generator's prefix-delegation max-pods math.
- ``ENI_MAX_PODS``      — legacy non-PD AWS-stock max-pods table used by
  the cluster simulator and BuildKit pod-sizing math (NOT used by the
  prefix-delegation generator).

Run ``uv run scripts/python/collect_instance_memory.py`` against a live cluster
to obtain precise memory_mi values for new entries.
"""

# ---------------------------------------------------------------------------
# AWS instance type specs
#
# Fields:
#   vcpu       — total vCPUs on the instance
#   memory_gib — AWS-advertised memory (for display/documentation)
#   memory_mi  — actual Kubernetes node capacity in MiB (what Karpenter sees
#                for scheduling). Always less than advertised due to
#                hypervisor/firmware overhead.  Where actual values haven't
#                been collected from a live node, we estimate as
#                memory_gib * 1024 * 0.925 (Karpenter's 7.5% overhead).
#   gpu        — number of GPUs (0 for CPU-only)
#   arch       — CPU architecture: "amd64" or "arm64"
# ---------------------------------------------------------------------------
INSTANCE_SPECS: dict[str, dict] = {
    # x86 CPU — compute-optimized (~2 GiB/core)
    # Fleet fallback sizes
    "c7a.8xlarge": {"vcpu": 32, "memory_gib": 64, "memory_mi": 60620, "gpu": 0, "arch": "amd64"},
    "c7a.12xlarge": {"vcpu": 48, "memory_gib": 96, "memory_mi": 90931, "gpu": 0, "arch": "amd64"},
    "c7a.16xlarge": {"vcpu": 64, "memory_gib": 128, "memory_mi": 121241, "gpu": 0, "arch": "amd64"},
    "c7a.24xlarge": {"vcpu": 96, "memory_gib": 192, "memory_mi": 181862, "gpu": 0, "arch": "amd64"},
    "c7a.48xlarge": {"vcpu": 192, "memory_gib": 384, "memory_mi": 363724, "gpu": 0, "arch": "amd64"},
    "c7i.8xlarge": {"vcpu": 32, "memory_gib": 64, "memory_mi": 60620, "gpu": 0, "arch": "amd64"},
    "c7i.12xlarge": {"vcpu": 48, "memory_gib": 96, "memory_mi": 90931, "gpu": 0, "arch": "amd64"},
    "c7i.16xlarge": {"vcpu": 64, "memory_gib": 128, "memory_mi": 121241, "gpu": 0, "arch": "amd64"},
    "c7i.24xlarge": {"vcpu": 96, "memory_gib": 192, "memory_mi": 181862, "gpu": 0, "arch": "amd64"},
    "c7i.metal-24xl": {"vcpu": 96, "memory_gib": 192, "memory_mi": 181862, "gpu": 0, "arch": "amd64"},
    "c7i.48xlarge": {"vcpu": 192, "memory_gib": 384, "memory_mi": 363724, "gpu": 0, "arch": "amd64"},
    # x86 CPU — balanced (~4 GiB/core)
    "m6i.32xlarge": {"vcpu": 128, "memory_gib": 512, "memory_mi": 485081, "gpu": 0, "arch": "amd64"},
    # Fleet fallback sizes
    "m7i.8xlarge": {"vcpu": 32, "memory_gib": 128, "memory_mi": 121241, "gpu": 0, "arch": "amd64"},
    "m7i.12xlarge": {"vcpu": 48, "memory_gib": 192, "memory_mi": 181862, "gpu": 0, "arch": "amd64"},
    "m7i.16xlarge": {"vcpu": 64, "memory_gib": 256, "memory_mi": 242540, "gpu": 0, "arch": "amd64"},
    "m7i.24xlarge": {"vcpu": 96, "memory_gib": 384, "memory_mi": 363724, "gpu": 0, "arch": "amd64"},
    "m7i.48xlarge": {"vcpu": 192, "memory_gib": 768, "memory_mi": 727449, "gpu": 0, "arch": "amd64"},
    # x86 CPU — memory-optimized (~8 GiB/core)
    # Fleet fallback sizes
    "r5.8xlarge": {"vcpu": 32, "memory_gib": 256, "memory_mi": 242540, "gpu": 0, "arch": "amd64"},
    "r5.12xlarge": {"vcpu": 48, "memory_gib": 384, "memory_mi": 363724, "gpu": 0, "arch": "amd64"},
    "r5.16xlarge": {"vcpu": 64, "memory_gib": 512, "memory_mi": 485081, "gpu": 0, "arch": "amd64"},
    "r5.24xlarge": {"vcpu": 96, "memory_gib": 768, "memory_mi": 727449, "gpu": 0, "arch": "amd64"},
    "r7a.8xlarge": {"vcpu": 32, "memory_gib": 256, "memory_mi": 242540, "gpu": 0, "arch": "amd64"},
    "r7a.12xlarge": {"vcpu": 48, "memory_gib": 384, "memory_mi": 363724, "gpu": 0, "arch": "amd64"},
    "r7a.16xlarge": {"vcpu": 64, "memory_gib": 512, "memory_mi": 485081, "gpu": 0, "arch": "amd64"},
    "r7a.24xlarge": {"vcpu": 96, "memory_gib": 768, "memory_mi": 727449, "gpu": 0, "arch": "amd64"},
    "r7a.48xlarge": {"vcpu": 192, "memory_gib": 1536, "memory_mi": 1454899, "gpu": 0, "arch": "amd64"},
    "r7i.8xlarge": {"vcpu": 32, "memory_gib": 256, "memory_mi": 242540, "gpu": 0, "arch": "amd64"},
    "r7i.16xlarge": {"vcpu": 64, "memory_gib": 512, "memory_mi": 485081, "gpu": 0, "arch": "amd64"},
    "r7i.24xlarge": {"vcpu": 96, "memory_gib": 768, "memory_mi": 727449, "gpu": 0, "arch": "amd64"},
    "r7i.48xlarge": {"vcpu": 192, "memory_gib": 1536, "memory_mi": 1454899, "gpu": 0, "arch": "amd64"},
    # ARM64 CPU
    # Fleet fallback sizes
    "m8g.8xlarge": {"vcpu": 32, "memory_gib": 128, "memory_mi": 121241, "gpu": 0, "arch": "arm64"},
    "m8g.12xlarge": {"vcpu": 48, "memory_gib": 192, "memory_mi": 181862, "gpu": 0, "arch": "arm64"},
    "m8g.16xlarge": {"vcpu": 64, "memory_gib": 256, "memory_mi": 242540, "gpu": 0, "arch": "arm64"},
    "m8g.24xlarge": {"vcpu": 96, "memory_gib": 384, "memory_mi": 363724, "gpu": 0, "arch": "arm64"},
    "m8g.48xlarge": {"vcpu": 192, "memory_gib": 768, "memory_mi": 727449, "gpu": 0, "arch": "arm64"},
    "r7g.16xlarge": {"vcpu": 64, "memory_gib": 512, "memory_mi": 485081, "gpu": 0, "arch": "arm64"},
    # GPU instances — 1-GPU
    "g4dn.8xlarge": {"vcpu": 32, "memory_gib": 128, "memory_mi": 121241, "gpu": 1, "arch": "amd64"},
    # Fleet fallback sizes
    "g4dn.16xlarge": {"vcpu": 64, "memory_gib": 256, "memory_mi": 242540, "gpu": 1, "arch": "amd64"},
    "g5.8xlarge": {"vcpu": 32, "memory_gib": 128, "memory_mi": 121241, "gpu": 1, "arch": "amd64"},
    # Fleet fallback sizes
    "g5.16xlarge": {"vcpu": 64, "memory_gib": 256, "memory_mi": 242540, "gpu": 1, "arch": "amd64"},
    "g6.8xlarge": {"vcpu": 32, "memory_gib": 128, "memory_mi": 121241, "gpu": 1, "arch": "amd64"},
    # Fleet fallback sizes
    "g6.16xlarge": {"vcpu": 64, "memory_gib": 256, "memory_mi": 242540, "gpu": 1, "arch": "amd64"},
    # GPU instances — 4-GPU
    "g4dn.12xlarge": {"vcpu": 48, "memory_gib": 192, "memory_mi": 181862, "gpu": 4, "arch": "amd64"},
    "g5.12xlarge": {"vcpu": 48, "memory_gib": 192, "memory_mi": 181862, "gpu": 4, "arch": "amd64"},
    # Fleet fallback sizes
    "g5.24xlarge": {"vcpu": 96, "memory_gib": 384, "memory_mi": 363724, "gpu": 4, "arch": "amd64"},
    "g6.12xlarge": {"vcpu": 48, "memory_gib": 192, "memory_mi": 181862, "gpu": 4, "arch": "amd64"},
    # Fleet fallback sizes
    "g6.24xlarge": {"vcpu": 96, "memory_gib": 384, "memory_mi": 363724, "gpu": 4, "arch": "amd64"},
    # GPU instances — 8-GPU
    "g4dn.metal": {"vcpu": 96, "memory_gib": 384, "memory_mi": 363724, "gpu": 8, "arch": "amd64"},
    "g5.48xlarge": {"vcpu": 192, "memory_gib": 768, "memory_mi": 727449, "gpu": 8, "arch": "amd64"},
    "g6.48xlarge": {"vcpu": 192, "memory_gib": 768, "memory_mi": 727449, "gpu": 8, "arch": "amd64"},
    "p4d.24xlarge": {"vcpu": 96, "memory_gib": 1152, "memory_mi": 1090765, "gpu": 8, "arch": "amd64"},
    "p5.48xlarge": {"vcpu": 192, "memory_gib": 2048, "memory_mi": 1939865, "gpu": 8, "arch": "amd64"},
    "p6-b200.48xlarge": {"vcpu": 192, "memory_gib": 2048, "memory_mi": 1939865, "gpu": 8, "arch": "amd64"},
    # pypi-cache instances
    "r7i.2xlarge": {"vcpu": 8, "memory_gib": 64, "memory_mi": 60620, "gpu": 0, "arch": "amd64"},
    "r7i.12xlarge": {"vcpu": 48, "memory_gib": 384, "memory_mi": 363724, "gpu": 0, "arch": "amd64"},
    "r5d.12xlarge": {"vcpu": 48, "memory_gib": 384, "memory_mi": 363724, "gpu": 0, "arch": "amd64", "nvme_gib": 1800},
    # BuildKit instances (used by modules/buildkit/)
    "m8gd.24xlarge": {"vcpu": 96, "memory_gib": 384, "memory_mi": 363724, "gpu": 0, "arch": "arm64"},
    "m6id.24xlarge": {"vcpu": 96, "memory_gib": 384, "memory_mi": 363724, "gpu": 0, "arch": "amd64"},
    "c7gd.16xlarge": {"vcpu": 64, "memory_gib": 128, "memory_mi": 121241, "gpu": 0, "arch": "arm64"},
    "m7gd.16xlarge": {"vcpu": 64, "memory_gib": 256, "memory_mi": 242540, "gpu": 0, "arch": "arm64"},
    "m8gd.16xlarge": {"vcpu": 64, "memory_gib": 256, "memory_mi": 242540, "gpu": 0, "arch": "arm64"},
}

# ---------------------------------------------------------------------------
# Per-instance ENI shape — raw AWS facts used for prefix-delegation max-pods math.
#
# Fields:
#   eni_count    — default network card's max ENIs (multi-NIC instances:
#                  default-card subset only — that's what Karpenter and
#                  AWS's max-pods calculator both use).
#   ipv4_per_eni — IPv4 addresses per ENI (this is the per-ENI slot count;
#                  with prefix delegation each slot holds 16 IPs).
#
# Multi-NIC instances (p4d, p5, p6-b200): eni_count is the DEFAULT network
# card's ENIs only — that's what Karpenter and the AWS max-pods calculator
# both use.
#
# Source: awslabs/amazon-eks-ami nodeadm/internal/kubelet/instance-info.jsonl.
# Schema docs: nodeadm/internal/kubelet/eni_max_pods.go.
# ---------------------------------------------------------------------------
INSTANCE_ENI_DATA: dict[str, dict[str, int]] = {
    "c7a.8xlarge": {"eni_count": 8, "ipv4_per_eni": 30},
    "c7a.12xlarge": {"eni_count": 8, "ipv4_per_eni": 30},
    "c7a.16xlarge": {"eni_count": 15, "ipv4_per_eni": 50},
    "c7a.24xlarge": {"eni_count": 15, "ipv4_per_eni": 50},
    "c7a.48xlarge": {"eni_count": 15, "ipv4_per_eni": 50},
    "c7i.8xlarge": {"eni_count": 8, "ipv4_per_eni": 30},
    "c7i.12xlarge": {"eni_count": 8, "ipv4_per_eni": 30},
    "c7i.16xlarge": {"eni_count": 15, "ipv4_per_eni": 50},
    "c7i.24xlarge": {"eni_count": 15, "ipv4_per_eni": 50},
    "c7i.metal-24xl": {"eni_count": 15, "ipv4_per_eni": 50},
    "c7i.48xlarge": {"eni_count": 15, "ipv4_per_eni": 50},
    "m6i.32xlarge": {"eni_count": 15, "ipv4_per_eni": 50},
    "m7i.8xlarge": {"eni_count": 8, "ipv4_per_eni": 30},
    "m7i.12xlarge": {"eni_count": 8, "ipv4_per_eni": 30},
    "m7i.16xlarge": {"eni_count": 15, "ipv4_per_eni": 50},
    "m7i.24xlarge": {"eni_count": 15, "ipv4_per_eni": 50},
    "m7i.48xlarge": {"eni_count": 15, "ipv4_per_eni": 50},
    "r5.8xlarge": {"eni_count": 8, "ipv4_per_eni": 30},
    "r5.12xlarge": {"eni_count": 8, "ipv4_per_eni": 30},
    "r5.16xlarge": {"eni_count": 15, "ipv4_per_eni": 50},
    "r5.24xlarge": {"eni_count": 15, "ipv4_per_eni": 50},
    "r7a.8xlarge": {"eni_count": 8, "ipv4_per_eni": 30},
    "r7a.12xlarge": {"eni_count": 8, "ipv4_per_eni": 30},
    "r7a.16xlarge": {"eni_count": 15, "ipv4_per_eni": 50},
    "r7a.24xlarge": {"eni_count": 15, "ipv4_per_eni": 50},
    "r7a.48xlarge": {"eni_count": 15, "ipv4_per_eni": 50},
    "r7i.8xlarge": {"eni_count": 8, "ipv4_per_eni": 30},
    "r7i.16xlarge": {"eni_count": 15, "ipv4_per_eni": 50},
    "r7i.24xlarge": {"eni_count": 15, "ipv4_per_eni": 50},
    "r7i.48xlarge": {"eni_count": 15, "ipv4_per_eni": 50},
    "m8g.8xlarge": {"eni_count": 8, "ipv4_per_eni": 30},
    "m8g.12xlarge": {"eni_count": 8, "ipv4_per_eni": 30},
    "m8g.16xlarge": {"eni_count": 15, "ipv4_per_eni": 50},
    "m8g.24xlarge": {"eni_count": 15, "ipv4_per_eni": 50},
    "m8g.48xlarge": {"eni_count": 15, "ipv4_per_eni": 50},
    "r7g.16xlarge": {"eni_count": 15, "ipv4_per_eni": 50},
    "g4dn.8xlarge": {"eni_count": 4, "ipv4_per_eni": 15},
    "g4dn.16xlarge": {"eni_count": 4, "ipv4_per_eni": 15},
    "g5.8xlarge": {"eni_count": 8, "ipv4_per_eni": 30},
    "g5.16xlarge": {"eni_count": 8, "ipv4_per_eni": 30},
    "g6.8xlarge": {"eni_count": 8, "ipv4_per_eni": 30},
    "g6.16xlarge": {"eni_count": 15, "ipv4_per_eni": 50},
    "g4dn.12xlarge": {"eni_count": 8, "ipv4_per_eni": 30},
    "g5.12xlarge": {"eni_count": 15, "ipv4_per_eni": 50},
    "g5.24xlarge": {"eni_count": 15, "ipv4_per_eni": 50},
    "g6.12xlarge": {"eni_count": 8, "ipv4_per_eni": 30},
    "g6.24xlarge": {"eni_count": 15, "ipv4_per_eni": 50},
    "g4dn.metal": {"eni_count": 15, "ipv4_per_eni": 50},
    "g5.48xlarge": {"eni_count": 7, "ipv4_per_eni": 50},  # NOT 15 — verified anomaly in upstream
    "g6.48xlarge": {"eni_count": 15, "ipv4_per_eni": 50},
    "p4d.24xlarge": {"eni_count": 15, "ipv4_per_eni": 50},
    "p5.48xlarge": {"eni_count": 2, "ipv4_per_eni": 50},
    "p6-b200.48xlarge": {"eni_count": 4, "ipv4_per_eni": 50},
    "r7i.2xlarge": {"eni_count": 4, "ipv4_per_eni": 15},
    "r7i.12xlarge": {"eni_count": 8, "ipv4_per_eni": 30},
    "r5d.12xlarge": {"eni_count": 8, "ipv4_per_eni": 30},
    "m8gd.24xlarge": {"eni_count": 15, "ipv4_per_eni": 50},
    "m6id.24xlarge": {"eni_count": 15, "ipv4_per_eni": 50},
    "c7gd.16xlarge": {"eni_count": 15, "ipv4_per_eni": 50},
    "m7gd.16xlarge": {"eni_count": 15, "ipv4_per_eni": 50},
    "m8gd.16xlarge": {"eni_count": 15, "ipv4_per_eni": 50},
}

# ---------------------------------------------------------------------------
# EKS max pods per instance type (from ENI limits).
# Source: awslabs/amazon-eks-ami eni-max-pods.txt
# The kubelet memory reservation formula uses max_pods, NOT vCPU count.
# ---------------------------------------------------------------------------
ENI_MAX_PODS: dict[str, int] = {
    # Runner node instance types
    # Fleet fallback sizes — c7a
    "c7a.8xlarge": 234,
    "c7a.12xlarge": 234,
    "c7a.16xlarge": 737,
    "c7a.24xlarge": 737,
    "c7a.48xlarge": 737,
    # Fleet fallback sizes — c7i
    "c7i.8xlarge": 234,
    "c7i.12xlarge": 234,
    "c7i.16xlarge": 737,
    "c7i.24xlarge": 737,
    "c7i.metal-24xl": 737,
    "c7i.48xlarge": 737,
    "m6i.32xlarge": 737,
    # Fleet fallback sizes — m7i
    "m7i.8xlarge": 234,
    "m7i.12xlarge": 234,
    "m7i.16xlarge": 737,
    "m7i.24xlarge": 737,
    "m7i.48xlarge": 737,
    # Fleet fallback sizes — r5
    "r5.8xlarge": 234,
    "r5.12xlarge": 234,
    "r5.16xlarge": 737,
    "r5.24xlarge": 737,
    # Fleet fallback sizes — r7a
    "r7a.8xlarge": 234,
    "r7a.12xlarge": 234,
    "r7a.16xlarge": 737,
    "r7a.24xlarge": 737,
    "r7a.48xlarge": 737,
    # Fleet fallback sizes — r7i
    "r7i.8xlarge": 234,
    "r7i.16xlarge": 737,
    "r7i.24xlarge": 737,
    "r7i.48xlarge": 737,
    # Fleet fallback sizes — m8g
    "m8g.8xlarge": 234,
    "m8g.12xlarge": 234,
    "m8g.16xlarge": 737,
    "m8g.24xlarge": 737,
    "m8g.48xlarge": 737,
    "r7g.16xlarge": 737,
    "g4dn.8xlarge": 58,
    # Fleet fallback sizes — g4dn
    "g4dn.16xlarge": 234,
    "g4dn.12xlarge": 234,
    "g4dn.metal": 737,
    "g5.8xlarge": 234,
    # Fleet fallback sizes — g5
    "g5.16xlarge": 234,
    "g5.12xlarge": 737,
    "g5.24xlarge": 737,
    "g5.48xlarge": 345,
    "g6.8xlarge": 234,
    # Fleet fallback sizes — g6
    "g6.16xlarge": 234,
    "g6.12xlarge": 234,
    "g6.24xlarge": 234,
    "g6.48xlarge": 737,
    "p4d.24xlarge": 250,
    "p5.48xlarge": 198,
    "p6-b200.48xlarge": 198,
    # pypi-cache instance types
    "r7i.2xlarge": 56,  # 4 ENIs x 15 IPs - 4
    "r7i.12xlarge": 234,  # 8 ENIs x 30 IPs - 8 (estimated)
    "r5d.12xlarge": 234,  # 8 ENIs x 30 IPs
    # BuildKit instance types
    "m8gd.24xlarge": 737,
    "m6id.24xlarge": 737,
    "c7gd.16xlarge": 737,
    "m7gd.16xlarge": 737,
    "m8gd.16xlarge": 737,
}
