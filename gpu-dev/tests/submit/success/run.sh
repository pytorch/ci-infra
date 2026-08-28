#!/usr/bin/env bash
# Smoke test for `gpu-dev submit`: runs on a single GPU, expected exit 0.
set -euo pipefail

echo "=== host ==="
hostname
date -u

echo "=== nvidia-smi ==="
nvidia-smi | tee nvidia-info.txt

echo "=== compute ==="
python3 - <<'PY' | tee compute.txt
import torch
assert torch.cuda.is_available(), "CUDA not available"
n = torch.cuda.device_count()
x = torch.arange(1_000_000, device="cuda", dtype=torch.float32)
s = x.sum().item()
print(f"devices={n} sum(0..999_999)={s}")
PY

echo "ok at $(date -u)" > status.txt
echo "DONE"
