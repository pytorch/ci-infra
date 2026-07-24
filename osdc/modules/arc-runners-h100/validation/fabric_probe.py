#!/usr/bin/env python3
"""Rollout validation probe for CUDA fabric-handle IPC on H100 with an IMEX channel.

This is NOT a unit test. It is a manual validation probe for the H100 fabric
runner rollout. It must never be wired into `just test`.

Why this probe exists
---------------------
`c10::cuda::get_fabric_access()` (c10/cuda/PeerToPeerAccess.cpp) probes NVML plus
a full CUDA allocate/export/import cycle over a FABRIC handle. When the IMEX
channel is missing or unusable that cycle fails and the caching allocator
(`ExpandableSegment::detectHandleType`, c10/cuda/CUDACachingAllocator.cpp)
GRACEFULLY falls back to POSIX-fd handles. A torch workload therefore exits 0
whether or not fabric handles were ever used. Exit code alone cannot tell a
fabric run apart from a silent POSIX fallback ("false green").

There is no Python API that reports which handle type the allocator picked, so
this probe captures the C++ INFO logs emitted by c10 and ASSERTS on them:

  - "use fabric handle to share expandable segments"  -> fabric export was used
  - "use posix fd to share expandable segments"       -> silent POSIX fallback
  - "use fabric handle to import expandable segments"  -> fabric import was used

The probe forces the allocator down the expandable-segments IPC path (allocate
>2 MiB, share via reduce_tensor to cache the exported handle, then free to run
the segment destructor), captures the logs, and fails hard unless the fabric
SHARE line is present and no POSIX-fallback line is.

Handle-type note: the probe deliberately leaves
`expandable_segments_handle_type` UNSPECIFIED (never forces FABRIC). UNSPECIFIED
is exactly the auto-detect-or-fall-back path we must validate; forcing FABRIC
would make c10 hard-error instead of falling back, which would hide the very
false-green this probe guards against.

Modes
-----
- multiprocess (default when >=2 GPUs): mirrors test/distributed/test_p2p_ipc.py
  test_p2p_ipc_expandable_segments. Rank 0 (GPU 0) allocates and exports; rank 1
  (GPU 1) imports without pre-allocating (the #179220 regression shape). This
  exercises the real cross-GPU fabric round trip the IMEX channel enables.
- single-process (fallback / 1-GPU): allocate, reduce_tensor to export, free.
  isFabricSupported() already runs a self-contained export+import cycle through
  the IMEX channel while deciding the handle type, so even one GPU proves the
  channel is usable and that the SHARE path took fabric. Use --single-process to
  force it.

The actual torch workload runs in a child process; its C++ logs are captured
after it fully exits (so glog buffers are flushed) and then evaluated.
"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys

# Set before any torch import so the CUDA allocator picks these up on first
# touch. setdefault lets the k8s manifest or an operator override any of them.
_ENV_DEFAULTS = {
    "PYTORCH_CUDA_ALLOC_CONF": "expandable_segments:True",
    "TORCH_CUDA_EXPANDABLE_SEGMENTS_IPC": "1",
    "TORCH_CPP_LOG_LEVEL": "INFO",
    "GLOG_logtostderr": "1",
}
for _k, _v in _ENV_DEFAULTS.items():
    os.environ.setdefault(_k, _v)

# Contract with c10 source. These exact strings are emitted by
# c10/cuda/CUDACachingAllocator.cpp and c10/cuda/PeerToPeerAccess.cpp.
LOG_FABRIC_SHARE = "use fabric handle to share expandable segments"
LOG_POSIX_SHARE = "use posix fd to share expandable segments"
LOG_FABRIC_IMPORT = "use fabric handle to import expandable segments"
LOG_POSIX_IMPORT = "use posix fd to import expandable segments"
LOG_FABRIC_OK = "using fabric to exchange memory handles"
LOG_FABRIC_FALLBACK = "falling back to fd handle exchange"

# float32 * 2Mi elements = 8 MiB; comfortably above the 2 MiB segment size so an
# expandable segment is created (mirrors test_p2p_ipc_expandable_segments).
TENSOR_ELEMENTS = 2 * 1024 * 1024
MASTER_ADDR = "127.0.0.1"
MASTER_PORT = "29511"

MODE_MARKER = "PROBE_MODE:"
VERDICT_MARKER = "FABRIC_VALIDATION:"


def _log(msg: str) -> None:
    print(f"[probe] {msg}", flush=True)


def _single_process_workload() -> None:
    import gc

    import torch
    from torch.multiprocessing.reductions import reduce_tensor

    print(f"{MODE_MARKER} single-process", flush=True)
    if not torch.cuda.is_available():
        raise SystemExit("CUDA is not available; cannot run the fabric probe")

    torch.cuda.set_device(0)
    torch.cuda.memory._set_allocator_settings("expandable_segments:True")
    torch.cuda.empty_cache()

    device = torch.device("cuda", 0)
    mib = TENSOR_ELEMENTS * 4 / 1024 / 1024
    _log(f"allocating {mib:.0f} MiB expandable segment on {device}")
    tensor = torch.randn(TENSOR_ELEMENTS, device=device)

    # reduce_tensor exports and caches the shareable handle, emitting the
    # SHARE log line for the handle type the allocator selected.
    meta = reduce_tensor(tensor)
    _log("reduce_tensor() completed (handle exported)")

    # Free to run the ExpandableSegment destructor against the cached handle
    # (the path PR #190860 fixed).
    del meta
    del tensor
    gc.collect()
    torch.cuda.empty_cache()
    torch.cuda.synchronize()
    _log("freed segment (destructor exercised)")


def _dist_worker(rank: int, world_size: int) -> None:
    import gc

    import torch
    import torch.distributed as dist
    from torch.multiprocessing.reductions import reduce_tensor

    os.environ.setdefault("MASTER_ADDR", MASTER_ADDR)
    os.environ.setdefault("MASTER_PORT", MASTER_PORT)

    torch.cuda.set_device(rank)
    dist.init_process_group(backend="gloo", rank=rank, world_size=world_size)
    try:
        device = torch.device("cuda", rank)
        if rank == 0:
            torch.cuda.memory._set_allocator_settings("expandable_segments:True")
            torch.cuda.empty_cache()
            mib = TENSOR_ELEMENTS * 4 / 1024 / 1024
            _log(f"rank0 producer: allocating {mib:.0f} MiB expandable segment on {device}")
            tensor = torch.randn(TENSOR_ELEMENTS, device=device)
            meta = reduce_tensor(tensor)
            _log("rank0 producer: exported handle, broadcasting to consumer")
            dist.broadcast_object_list([meta], src=0)
            dist.barrier()
            del meta
            del tensor
            gc.collect()
            torch.cuda.empty_cache()
            _log("rank0 producer: freed segment (destructor exercised)")
        else:
            # Do NOT pre-allocate: a consumer that has never touched the
            # allocator is the #179220 regression shape.
            _log(f"rank{rank} consumer: receiving handle (no pre-allocation)")
            recv: list[object] = [None]
            dist.broadcast_object_list(recv, src=0)
            func, args = recv[0]  # type: ignore[misc]
            args = list(args)
            # args[6] is storage_device (rebuild_cuda_tensor): import onto this
            # consumer's own GPU, exercising the cross-GPU fabric import.
            args[6] = rank
            tensor = func(*args)
            _log(f"rank{rank} consumer: imported handle onto {device}")
            dist.barrier()
            del tensor
            gc.collect()
            torch.cuda.empty_cache()
            _log(f"rank{rank} consumer: freed imported segment")
        torch.cuda.synchronize()
    finally:
        dist.destroy_process_group()


def _multiprocess_workload() -> None:
    import torch
    import torch.multiprocessing as mp

    if not torch.cuda.is_available():
        raise SystemExit("CUDA is not available; cannot run the fabric probe")
    device_count = torch.cuda.device_count()
    if device_count < 2:
        raise SystemExit(f"multiprocess mode needs >=2 GPUs, found {device_count}; use --single-process")

    world_size = 2
    print(f"{MODE_MARKER} multiprocess ({world_size} GPUs)", flush=True)
    os.environ.setdefault("MASTER_ADDR", MASTER_ADDR)
    os.environ.setdefault("MASTER_PORT", MASTER_PORT)
    mp.spawn(_dist_worker, args=(world_size,), nprocs=world_size, join=True)


def _run_workload(single_process: bool) -> int:
    import torch

    if single_process or torch.cuda.device_count() < 2:
        _single_process_workload()
    else:
        _multiprocess_workload()
    return 0


def _verdict(log: str, workload_rc: int) -> tuple[bool, str]:
    fabric_share = LOG_FABRIC_SHARE in log
    posix_share = LOG_POSIX_SHARE in log
    fabric_import = LOG_FABRIC_IMPORT in log
    posix_import = LOG_POSIX_IMPORT in log
    multiprocess = f"{MODE_MARKER} multiprocess" in log

    if workload_rc != 0:
        return False, f"workload process exited non-zero ({workload_rc}); see logs above"
    if not fabric_share and not posix_share:
        return False, (
            "SHARE path never exercised: no fabric/posix share log seen. The torch build may "
            "lack expandable-segments IPC, the segment was not created, or C++ INFO logging was "
            "not enabled (TORCH_CPP_LOG_LEVEL=INFO). Result is inconclusive, not a pass."
        )
    if posix_share:
        return False, "fell back to POSIX fd on the SHARE (export) path - fabric was NOT used"
    if multiprocess and posix_import:
        return False, "fell back to POSIX fd on the IMPORT path - fabric was NOT used"
    if multiprocess and not fabric_import:
        return False, "consumer never imported via fabric; cross-GPU round trip did not complete"
    return True, "fabric handle used on the expandable-segments IPC path"


def _evaluate(log: str, workload_rc: int) -> int:
    signals = {
        "fabric share": LOG_FABRIC_SHARE in log,
        "posix share (fallback)": LOG_POSIX_SHARE in log,
        "fabric import": LOG_FABRIC_IMPORT in log,
        "posix import (fallback)": LOG_POSIX_IMPORT in log,
        "isFabricSupported ok": LOG_FABRIC_OK in log,
        "isFabricSupported fell back": LOG_FABRIC_FALLBACK in log,
    }
    _log("captured C++ log signals:")
    for name, seen in signals.items():
        print(f"    {'FOUND' if seen else '-    '}  {name}", flush=True)

    passed, reason = _verdict(log, workload_rc)
    if passed:
        print(f"{VERDICT_MARKER} PASS - {reason}", flush=True)
        return 0
    print(f"{VERDICT_MARKER} FAIL - {reason}", flush=True)
    return 1


def _run_driver(single_process: bool) -> int:
    cmd = [sys.executable, os.path.abspath(__file__), "--run"]
    if single_process:
        cmd.append("--single-process")
    _log("launching workload: " + " ".join(cmd))

    proc = subprocess.Popen(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        bufsize=1,
    )
    captured: list[str] = []
    if proc.stdout is not None:
        for line in proc.stdout:
            sys.stdout.write(line)
            sys.stdout.flush()
            captured.append(line)
    workload_rc = proc.wait()
    return _evaluate("".join(captured), workload_rc)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--run",
        action="store_true",
        help="internal: execute the torch workload (invoked by the driver)",
    )
    parser.add_argument(
        "--single-process",
        action="store_true",
        help="force the single-process, single-GPU workload",
    )
    args = parser.parse_args()

    if args.run:
        return _run_workload(single_process=args.single_process)
    return _run_driver(single_process=args.single_process)


if __name__ == "__main__":
    sys.exit(main())
