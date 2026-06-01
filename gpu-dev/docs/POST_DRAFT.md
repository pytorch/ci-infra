# Instant GPU sandboxes for PyTorch

Reserve an **H100 or B200 in under a second** — full GPUs *or* MIG slices — with PyTorch
already built and importable. Ephemeral and auto-cleaned, like a serverless function but
with a GPU attached. From Python or one CLI command.

## 1. Sub-second reservations
A warm pool of pre-booted pods means a reserve *claims* one instead of cold-booting it:
- **H100 / B200** — full GPUs (1–8) or **MIG slices** (`h100-mig-1g`, `b200-mig-3g`, …); also A100 / T4 / CPU.
- **~1 s** to an active box (vs minutes for a cold boot).

## 2. PyTorch prebuilt + editable
Every box gets PyTorch already built at **`viable/strict`** (the last all-green trunk
commit) and installed as an **editable install** (`pip install -e .`) at `~/pytorch`:
- `import torch` works **instantly** — zero build.
- Edit Python → no rebuild (editable). Edit C++/CUDA → `pip install -e . --no-build-isolation`
  rebuilds **incrementally** (warm `build/` + shared ccache + mold linker) — seconds, not minutes.

Two ways in, both ephemeral with PyTorch ready:
```bash
gpu-dev reserve --gpu-type h100 --gpus 1 --no-persist
```
```python
from gpu_dev import GpuDev

with GpuDev().reserve(gpu_type="b200", gpu_count=1) as sandbox:   # ~1s, auto-cleans on exit
    sandbox.upload("train.py", "/home/dev/train.py")
    r = sandbox.exec("cd ~/pytorch && python /home/dev/train.py")  # GPU + torch already there
    print(r.stdout, r.returncode)
```
That ephemeral, isolated, auto-cleaned shape is also great for **agent sandboxing** — hand
an agent a throwaway GPU box to run its own generated code/tests; it vanishes when done.

## 3. Repro a failing PR/commit
```bash
gpu-dev repro pr/185264 test/inductor/test_flex_attention.py \
  TestFlexAttentionCUDA.test_large_kv_int64_pointer_math_cuda --gpu-type h100
```
Reserves a box, checks out the ref (a **merged** PR → its real land commit on `main`),
builds it off-pod (~100 s, then cached for everyone), runs the test, and drops you in to fix it.

## 4. Run a job (multinode)
```bash
gpu-dev submit --gpus 16 --gpu-type h100 --runtime . -- bash run.sh
```
Reserves (here 2× 8-GPU nodes), rsyncs your code up, runs it — `RANK` / `SIZE` /
`MASTER_ADDR` / `MASTER_PORT` are wired so `torchrun` just works — syncs results back, and
auto-cancels. Exit code mirrors the job.

---

**Feedback wanted.** What would you build on a sub-second, ephemeral, lambda-like GPU
sandbox — CI repros, agent sandboxes, interactive dev, burst jobs? Tell us your use cases.
