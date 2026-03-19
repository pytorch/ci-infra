"""Static PyTorch CI workload data for cluster simulation.

Sourced from:
    - docs/runner_naming_convention.md (label mapping)
    - docs/current_runner_load_distribution.md (peak concurrency)

These are point-in-time snapshots. Update when the runner fleet or
CI workload profile changes significantly.
"""

# Old GitHub runner label → new OSDC runner label
# Provider prefix (mt-) stripped — runner defs use the bare label.
# Sourced from runner_naming_convention.md

OLD_TO_NEW_LABEL: dict[str, str] = {
    # x86 CPU — Intel AVX-512
    "linux.large": "l-x86iavx512-2-4",
    "linux.2xlarge": "l-x86iavx512-8-16",
    "linux.c7i.2xlarge": "l-x86iavx512-8-16",
    "linux.4xlarge": "l-x86iavx512-16-32",
    "linux.c7i.4xlarge": "l-x86iavx512-16-32",
    "linux.9xlarge.ephemeral": "l-x86iavx512-36-72",
    "linux.12xlarge": "l-x86iavx512-48-96",
    "linux.c7i.12xlarge": "l-x86iavx512-48-96",
    "linux.24xlarge": "l-x86iavx512-94-192",
    "linux.24xl.spr-metal": "l-bx86iamx-94-192",
    # x86 CPU — Intel AMX
    "linux.2xlarge.amx": "l-x86iamx-8-32",
    "linux.8xlarge.amx": "l-x86iamx-32-128",
    # x86 CPU — Intel AVX2 (m4 family)
    "linux.2xlarge.avx2": "l-x86iavx2-8-32",
    "linux.10xlarge.avx2": "l-x86iavx2-40-160",
    # x86 CPU — Memory-optimized
    "linux.r7i.2xlarge": "l-x86iavx512-8-64",
    "linux.2xlarge.memory": "l-x86iavx512-8-64",
    "linux.r7i.4xlarge": "l-x86iavx512-16-128",
    "linux.4xlarge.memory": "l-x86iavx512-16-128",
    "linux.8xlarge.memory": "l-x86iavx512-32-256",
    "linux.12xlarge.memory": "l-x86iavx512-48-384",
    "linux.12xlarge.memory.ephemeral": "l-x86iavx512-48-384",
    "linux.24xlarge.memory": "l-x86iavx512-94-768",
    # x86 CPU — AMD
    "linux.24xlarge.amd": "l-x86aavx512-125-508",
    # x86 GPU — T4 (g4dn)
    "linux.4xlarge.nvidia.gpu": "l-x86iavx512-29-125-t4",
    "linux.g4dn.4xlarge.nvidia.gpu": "l-x86iavx512-29-125-t4",
    "linux.g4dn.12xlarge.nvidia.gpu": "l-x86iavx512-45-188-t4-4",
    "linux.g4dn.metal.nvidia.gpu": "l-bx86iavx512-94-384-t4-8",
    # x86 GPU — A10G (g5)
    "linux.g5.4xlarge.nvidia.gpu": "l-x86aavx2-29-125-a10g",
    "linux.g5.12xlarge.nvidia.gpu": "l-x86aavx2-45-188-a10g-4",
    "linux.g5.48xlarge.nvidia.gpu": "l-x86aavx2-192-768-a10g-8",
    # x86 GPU — L4 (g6)
    "linux.g6.4xlarge.experimental.nvidia.gpu": "l-x86aavx2-29-125-l4",
    "linux.g6.12xlarge.nvidia.gpu": "l-x86aavx2-45-188-l4-4",
    # ARM64
    "linux.arm64.2xlarge": "l-arm64g2-6-32",
    "linux.arm64.2xlarge.ephemeral": "l-arm64g2-6-32",
    "linux.arm64.m7g.4xlarge": "l-arm64g3-16-64",
    "linux.arm64.m8g.4xlarge": "l-arm64g4-16-64",
    "linux.arm64.r7g.12xlarge.memory": "l-arm64g3-61-509",
    "linux.arm64.m7g.metal": "l-barm64g3-62-256",
}

# ---------------------------------------------------------------------------
# Peak concurrent runner counts per old label (30-day window)
# Source: docs/current_runner_load_distribution.md, queried 2026-03-18
# ---------------------------------------------------------------------------

PEAK_CONCURRENT: dict[str, int] = {
    "linux.2xlarge": 1473,
    "linux.c7i.2xlarge": 927,
    "linux.4xlarge": 1293,
    "linux.g5.4xlarge.nvidia.gpu": 695,
    "linux.g6.4xlarge.experimental.nvidia.gpu": 422,
    "linux.2xlarge.amx": 384,
    "linux.large": 15,
    "linux.12xlarge": 164,
    "linux.c7i.4xlarge": 91,
    "linux.arm64.m7g.4xlarge": 76,
    "linux.g4dn.metal.nvidia.gpu": 91,
    "linux.arm64.m8g.4xlarge": 76,
    "linux.g4dn.12xlarge.nvidia.gpu": 183,
    "linux.12xlarge.memory": 64,
    "linux.4xlarge.memory": 71,
    "linux.12xlarge.memory.ephemeral": 353,
    "linux.g5.12xlarge.nvidia.gpu": 80,
    "linux.9xlarge.ephemeral": 65,
    "linux.8xlarge.amx": 174,
    "linux.24xl.spr-metal": 45,
    "linux.r7i.2xlarge": 24,
    "linux.arm64.r7g.12xlarge.memory": 153,
    "linux.arm64.2xlarge": 53,
    "linux.24xlarge.memory": 28,
    "linux.g4dn.4xlarge.nvidia.gpu": 83,
    "linux.g5.48xlarge.nvidia.gpu": 42,
    "linux.g6.12xlarge.nvidia.gpu": 29,
    "linux.arm64.2xlarge.ephemeral": 20,
    "linux.8xlarge.memory": 12,
    "linux.r7i.4xlarge": 18,
    "linux.10xlarge.avx2": 30,
    "linux.arm64.m7g.metal": 39,
    "linux.c7i.12xlarge": 25,
    "linux.24xlarge.amd": 24,
    "linux.2xlarge.avx2": 18,
    "linux.4xlarge.nvidia.gpu": 21,
    "linux.2xlarge.memory": 4,
    "linux.24xlarge": 2,
}
