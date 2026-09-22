"""Run the same job across multiple GPU types and compare results.

Useful for benchmarking or testing compatibility across hardware.

Usage:
    python batch_multi_gpu.py
"""
from concurrent.futures import ThreadPoolExecutor, as_completed

from gpu_dev import GpuDev, GpuDevError

client = GpuDev()

BENCHMARK_CMD = """
python3 -c '
import torch, time
gpu = torch.cuda.get_device_name(0)
x = torch.randn(4096, 4096, device="cuda")
torch.cuda.synchronize()
t0 = time.time()
for _ in range(100):
    y = x @ x
torch.cuda.synchronize()
ms = (time.time() - t0) * 1000
print(f"{gpu}|{ms:.0f}")
'
"""

GPU_TYPES = ["t4", "l4", "rtxpro6000"]


def run_benchmark(gpu_type: str) -> dict:
    try:
        sb = client.reserve(
            gpu_type=gpu_type,
            gpu_count=1,
            hours=0.25,
            name=f"bench-{gpu_type}",
        )
        result = sb.exec(BENCHMARK_CMD.strip(), timeout=30)
        sb.cancel()

        if result.exit_code == 0 and "|" in result.stdout:
            gpu_name, ms = result.stdout.strip().split("|")
            return {"gpu_type": gpu_type, "gpu_name": gpu_name, "ms": float(ms), "ok": True}
        return {"gpu_type": gpu_type, "error": result.stderr or result.stdout, "ok": False}
    except GpuDevError as e:
        return {"gpu_type": gpu_type, "error": str(e), "ok": False}


print(f"Benchmarking matmul 4096x4096 x100 across {len(GPU_TYPES)} GPU types...\n")

# Run in parallel
with ThreadPoolExecutor(max_workers=len(GPU_TYPES)) as ex:
    futures = {ex.submit(run_benchmark, gt): gt for gt in GPU_TYPES}

    print(f"{'GPU Type':15s} {'GPU Name':30s} {'Time':>8s}")
    print("-" * 55)
    for future in as_completed(futures):
        r = future.result()
        if r["ok"]:
            print(f"{r['gpu_type']:15s} {r['gpu_name']:30s} {r['ms']:>7.0f}ms")
        else:
            print(f"{r['gpu_type']:15s} FAILED: {r['error'][:40]}")

print("\nDone")
