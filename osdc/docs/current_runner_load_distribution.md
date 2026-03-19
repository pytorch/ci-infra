# Runner Load Distribution (pytorch/pytorch)

Job counts and peak concurrency by runner type over the last 30 days. Source: `default.workflow_job` ClickHouse table, queried 2026-03-18.

**Methodology:**
- Job counts: `COUNT(*)` per runner label.
- Peak concurrency: event-based calculation using `started_at` / `completed_at` timestamps. Each job creates a +1 event at start and -1 at completion; a running sum gives the concurrent count at every event; the max is the peak. Long-running jobs (hours) are properly counted for their full duration.
- The `lf.` prefix (Linux Foundation) is stripped and merged with non-prefixed equivalents — they run on the same hardware.
- Meta-labels (`self-hosted`, `Linux`, `macOS`, `windows`, `X64`, `ARM64`, etc.) are excluded.
- **Scope: pytorch/pytorch only.** Other repos share the same runner pools but are not included. True infrastructure peak may be higher.

## Self-hosted Linux runners (old labels)

| Runner Type | Jobs (30d) | Peak Concurrent |
|---|---|---|
| linux.2xlarge | 491,396 | 1,473 |
| linux.c7i.2xlarge | 350,911 | 927 |
| linux.4xlarge | 279,660 | 1,293 |
| linux.g5.4xlarge.nvidia.gpu | 93,759 | 695 |
| linux.g6.4xlarge.experimental.nvidia.gpu | 61,263 | 422 |
| linux.2xlarge.amx | 46,174 | 384 |
| linux.large | 37,043 | 15 |
| linux.12xlarge | 31,542 | 164 |
| linux.c7i.4xlarge | 29,111 | 91 |
| linux.arm64.m7g.4xlarge | 23,594 | 76 |
| linux.g4dn.metal.nvidia.gpu | 19,040 | 91 |
| linux.arm64.m8g.4xlarge | 17,935 | 76 |
| linux.g4dn.12xlarge.nvidia.gpu | 17,625 | 183 |
| linux.12xlarge.memory | 10,978 | 64 |
| linux.4xlarge.memory | 10,695 | 71 |
| linux.12xlarge.memory.ephemeral | 9,815 | 353 |
| linux.g5.12xlarge.nvidia.gpu | 7,932 | 80 |
| linux.9xlarge.ephemeral | 7,678 | 65 |
| linux.8xlarge.amx | 7,663 | 174 |
| linux.24xl.spr-metal | 6,983 | 45 |
| linux.r7i.2xlarge | 5,173 | 24 |
| linux.arm64.r7g.12xlarge.memory | 5,046 | 153 |
| linux.arm64.2xlarge | 4,552 | 53 |
| linux.24xlarge.memory | 3,652 | 28 |
| linux.g4dn.4xlarge.nvidia.gpu | 3,651 | 83 |
| linux.g5.48xlarge.nvidia.gpu | 3,465 | 42 |
| linux.g6.12xlarge.nvidia.gpu | 3,261 | 29 |
| linux.arm64.2xlarge.ephemeral | 1,536 | 20 |
| linux.8xlarge.memory | 1,522 | 12 |
| linux.r7i.4xlarge | 1,449 | 18 |
| linux.10xlarge.avx2 | 1,290 | 30 |
| linux.arm64.m7g.metal | 1,170 | 39 |
| linux.c7i.12xlarge | 858 | 25 |
| linux.24xlarge.amd | 720 | 24 |
| linux.2xlarge.avx2 | 534 | 18 |
| linux.4xlarge.nvidia.gpu | 468 | 21 |
| linux.2xlarge.memory | 15 | 4 |
| linux.24xlarge | 14 | 2 |

## GitHub-hosted runners

| Runner Type | Jobs (30d) | Peak Concurrent |
|---|---|---|
| ubuntu-latest | 276,308 | 137 |
| linux.24_04.4x | 58,699 | 98 |
| ubuntu-22.04 | 56,849 | 77 |
| ubuntu-24.04 | 1,611 | 4 |

## Windows runners

| Runner Type | Jobs (30d) | Peak Concurrent |
|---|---|---|
| windows.4xlarge.nonephemeral | 33,998 | 227 |
| windows.12xlarge | 5,480 | 190 |
| windows.g4dn.xlarge | 3,397 | 49 |
| windows.4xlarge | 2,949 | 49 |
| windows-11-arm64-preview | 1,412 | 16 |

## macOS runners

| Runner Type | Jobs (30d) | Peak Concurrent |
|---|---|---|
| macos-m1-stable | 23,884 | 90 |
| macos-m2-15 | 4,973 | 34 |
| macos-m1-14 | 4,789 | 30 |
| macos-14-xlarge | 1,067 | 21 |
| macos-m2-26 | 179 | 4 |

## ROCm (AMD GPU) runners

| Runner Type | Jobs (30d) | Peak Concurrent |
|---|---|---|
| linux.rocm.gpu.gfx950.1 | 41,952 | 323 |
| linux.rocm.gpu.gfx950.4 | 13,150 | 122 |
| linux.rocm.gpu.gfx942.1 | 7,913 | 80 |
| linux.rocm.gpu.gfx950.2 | 2,894 | 99 |
| linux.rocm.gpu.mi210.1.test | 1,935 | 37 |
| linux.rocm.gpu.mi250.1 | 1,264 | 70 |
| linux.rocm.gpu.gfx942.4 | 1,247 | 27 |
| linux.rocm.gpu.2 | 1,236 | 48 |
| linux.rocm.gpu.mi210.2.test | 725 | 16 |
| linux.rocm.gpu.gfx1100 | 638 | 10 |
| linux.rocm.gpu.gfx950.1.test | 285 | 48 |
| linux.rocm.gpu.mi300-test | 182 | 16 |
| linux.rocm.gpu.4 | 176 | 12 |
| linux.rocm.gpu.gfx950.4.test | 139 | 27 |
| linux.rocm.gpu.gfx942.1.stg | 100 | 8 |
| linux.rocm.mi250.docker-cache | 90 | 2 |
| linux.rocm.mi210.docker-cache | 89 | 2 |
| linux.rocm.gpu.gfx942.4.stg | 36 | 3 |
| linux.rocm.gpu.gfx942.1.b | 11 | 2 |
| linux.rocm.gpu.gfx942.1.test | 5 | 5 |
| linux.rocm.gpu.gfx942.4.b | 3 | 3 |
| linux.rocm.gpu.gfx942.4.test | 3 | 3 |

## Other providers (AWS H100, DGX B200, TPU, XPU, s390x)

| Runner Type | Jobs (30d) | Peak Concurrent |
|---|---|---|
| linux.idc.xpu | 5,721 | 70 |
| linux.aws.h100 | 4,940 | 44 |
| linux.dgx.b200 | 3,116 | 29 |
| linux.s390x | 2,283 | 45 |
| linux.aws.a100 | 1,803 | 38 |
| linux.google.tpuv7x.1 | 1,512 | 10 |
| linux.dgx.b200.8 | 445 | 12 |
| linux.aws.h100.4 | 244 | 9 |
| linux.client.xpu | 97 | 8 |
| linux.aws.h100.8 | 36 | 1 |
| a.linux.b200.2 | 9 | 2 |

## OSDC runners (new labels, early migration)

| Runner Type | Jobs (30d) | Peak Concurrent |
|---|---|---|
| l-x86iamx-8-16 | 89 | 15 |
| l-x86iavx512-2-4 | 18 | 3 |
| l-x86iavx512-16-64-t4 | 9 | 2 |
| l-x86iavx512-8-16 | 9 | 2 |
