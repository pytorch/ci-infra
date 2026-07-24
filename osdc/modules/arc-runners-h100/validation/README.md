# H100 CUDA fabric-handle IPC validation probe

Rollout validation for the H100 fabric runner (`mt-l-x86iamx-44-450-h100-fab`)
and its IMEX channel. This is a manual probe, not a unit test. It is not wired
into `just test` or any module deploy.

## What it validates

That a torch workload on a fabric-enabled H100 runner actually uses CUDA
**fabric handles** for expandable-segments IPC, instead of silently falling back
to POSIX file-descriptor handles.

## Why it exists (the false-green risk)

`c10::cuda::get_fabric_access()` decides whether the CUDA caching allocator can
share expandable memory segments over a FABRIC handle. It probes NVML and then
runs a full CUDA allocate / export / import cycle over the fabric handle
(`isFabricSupported()` in `c10/cuda/PeerToPeerAccess.cpp`). That import is what
needs the IMEX channel device inside the container.

If the IMEX channel is missing or unusable, the cycle fails and
`ExpandableSegment::detectHandleType()` (in `c10/cuda/CUDACachingAllocator.cpp`)
GRACEFULLY falls back to POSIX-fd handles. The workload still completes and exits
0. So a green job does not prove fabric was used - it can be a silent POSIX
fallback. This is the "false green" the rollout must guard against.

There is no Python API that reports the handle type actually used, so the probe
captures the C++ INFO logs c10 emits and asserts on them:

| C++ log line (INFO)                                   | Meaning                       |
| ----------------------------------------------------- | ----------------------------- |
| `use fabric handle to share expandable segments`      | fabric export was used (good) |
| `use posix fd to share expandable segments`           | silent POSIX fallback (bad)   |
| `use fabric handle to import expandable segments`      | fabric import was used (good) |
| `using fabric to exchange memory handles`             | `isFabricSupported()` passed  |
| `... falling back to fd handle exchange`               | fabric probe failed           |

The probe forces the expandable-segments IPC path (allocate a >2 MiB tensor,
`reduce_tensor` to export and cache the handle, then free it to run the segment
destructor - the path PR #190860 fixed), captures the logs, and prints:

- `FABRIC_VALIDATION: PASS` - fabric SHARE line present, no POSIX-fallback line.
- `FABRIC_VALIDATION: FAIL` - fell back to POSIX, or the path was never exercised
  (inconclusive), or the workload errored. Exit code is non-zero on FAIL.

It deliberately leaves `expandable_segments_handle_type` UNSPECIFIED. UNSPECIFIED
is the auto-detect-or-fall-back path we must validate; forcing FABRIC would make
c10 hard-error instead of falling back, hiding the exact false-green under test.

## How it works

`fabric_probe.py` runs the torch workload in a child process and evaluates that
child's captured C++ logs after it exits (so glog buffers are flushed).

- Multiprocess mode (default when >=2 GPUs) mirrors
  `test/distributed/test_p2p_ipc.py::test_p2p_ipc_expandable_segments`: rank 0 on
  GPU 0 allocates and exports; rank 1 on GPU 1 imports without pre-allocating
  (the `#179220` regression shape). This exercises the real cross-GPU fabric
  round trip the IMEX channel enables, asserting both the fabric SHARE and the
  fabric IMPORT log lines.
- Single-process mode (`--single-process`, or automatic on a 1-GPU host) does
  allocate / export / free in one process. Because `isFabricSupported()` already
  runs a self-contained export+import cycle through the IMEX channel while
  deciding the handle type, one GPU is enough to prove the channel is usable and
  that the SHARE path took fabric.

Required environment (set by the manifest, or `os.environ.setdefault` when run
standalone): `NVIDIA_IMEX_CHANNELS=0`, `PYTORCH_CUDA_ALLOC_CONF=expandable_segments:True`,
`TORCH_CUDA_EXPANDABLE_SEGMENTS_IPC=1`, `TORCH_CPP_LOG_LEVEL=INFO`, `GLOG_logtostderr=1`.

## Image requirement

The probe only validates the torch build it runs inside. That build MUST contain
the c10 expandable-segments fabric IPC code and be built with CUDA >= 12.4.
Prefer the exact image the H100 CI jobs run. If the build lacks the code, the
probe reports an inconclusive FAIL (no share log at all) rather than a false
pass - so a wrong image cannot masquerade as success.

## Run it: (a) directly via kubectl on meta-prod-aws-uw1

From the `osdc/` repo root, against the `meta-prod-aws-uw1` cluster context:

```bash
# 1. Edit fabric-validation-pod.yaml and set spec.containers[].image to the
#    torch/CUDA image under validation (replaces REPLACE_WITH_TORCH_CUDA_IMAGE).

# 2. Publish the probe source as a ConfigMap (single source of truth - the .py).
kubectl create configmap fabric-probe -n default \
  --from-file=fabric_probe.py=modules/arc-runners-h100/validation/fabric_probe.py

# 3. Launch the probe Pod.
kubectl apply -f modules/arc-runners-h100/validation/fabric-validation-pod.yaml

# 4. Watch the verdict (the last log line is FABRIC_VALIDATION: PASS/FAIL).
kubectl logs -f pod/fabric-validation -n default

# 5. Clean up.
kubectl delete pod/fabric-validation configmap/fabric-probe -n default
```

A p5.48xlarge node must be available (Capacity Blocks reserved). The Pod
tolerates the H100 fleet taints and requests `nvidia.com/gpu: 2`.

## Run it: (b) as a workflow_dispatch job on the fabric runner

Running on the runner uses the CI job's own torch image, so it validates exactly
what production runs. No image placeholder to fill in. Example workflow:

```yaml
name: h100-fabric-validation
on:
  workflow_dispatch:
jobs:
  fabric-probe:
    runs-on: mt-l-x86iamx-44-450-h100-fab
    steps:
      - name: Checkout osdc (for the probe source)
        uses: actions/checkout@v4
      - name: Run fabric validation probe
        env:
          NVIDIA_IMEX_CHANNELS: "0"
          PYTORCH_CUDA_ALLOC_CONF: "expandable_segments:True"
          TORCH_CUDA_EXPANDABLE_SEGMENTS_IPC: "1"
          TORCH_CPP_LOG_LEVEL: "INFO"
          GLOG_logtostderr: "1"
        run: python modules/arc-runners-h100/validation/fabric_probe.py
```

The step fails (non-zero exit) if fabric was not used, turning the false-green
into a real red. Adjust the checkout to wherever the probe source is available
on the runner.

## Files

- `fabric_probe.py` - the probe (driver + torch workload + log assertion).
- `fabric-validation-pod.yaml` - the standalone kubectl Pod.
- `README.md` - this file.
