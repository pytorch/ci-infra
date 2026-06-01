# Instant GPU sandboxes for PyTorch

Reserve a GPU box in ~1 second, with PyTorch already built. Repro a failing CI test,
edit C++/CUDA, and rebuild in seconds — not a cold 30-minute compile. From Python or
one CLI command.

## 1. Python SDK

```python
from gpu_dev import GpuDev

client = GpuDev()
with client.reserve(gpu_type="h100", gpu_count=1, hours=1) as sb:   # ~instant warm claim
    print(sb.exec("python -c 'import torch; print(torch.__version__, torch.cuda.is_available())'").stdout)
    sb.upload("./train.py", "/home/dev/train.py")
    print(sb.exec("python /home/dev/train.py").stdout)
# auto-cancels on exit
```

`reserve()` returns an **active** sandbox (no polling). `sb.exec()` / `sb.upload()`.
GPU types: `b200`, `h100`, `a100`, `t4`, MIG slices (`h100-mig-1g`, …), `cpu-x86`/`cpu-arm`.

## 2. Sub-second reservations

A pool of **pre-booted pods** (PyTorch prebuilt, SSH up) sits warm; a reserve just
*claims* one instead of booting from scratch.

```bash
gpu-dev reserve --gpu-type h100 --gpus 1     # warm claim → active in ~1s
```

| | cold boot | warm claim |
|---|---|---|
| reserve → usable | ~2–4 min (node scale, pull, SSH) | **~1–2 s** |
| `import torch` | builds / installs | **0 s** (prebuilt staged) |

## 3. `gpu-dev repro` — reproduce a failing PR/commit

```bash
gpu-dev repro pr/185264 test/inductor/test_flex_attention.py \
  TestFlexAttentionCUDA.test_large_kv_int64_pointer_math_cuda --gpu-type h100
```

Reserves a box, checks out the ref (PRs use `pull/N/merge` — what CI actually tests),
runs the test, prints the verdict, then **drops you into the box** at `~/pytorch` with
the ref checked out so you can fix and re-run. `--no-connect` = CI mode (run, auto-cancel,
exit code = test result).

## 4. `gpu-dev submit` — run a job, get results back

```bash
gpu-dev submit --runtime ./ -- python train.py            # sync cwd, run, sync results, auto-cancel
gpu-dev submit --gpus 16 --gpu-type h100 --runtime . -- bash run.sh   # multinode
```

Reserves → rsyncs your code up → runs the command → syncs results back → cancels. Exit
code mirrors the remote command. Multinode wires `RANK`/`SIZE`/`MASTER_ADDR`/… so
`torchrun` just works.

## Caching — why builds are fast

Two layers, so you almost never pay for a cold compile:

1. **Prebuilt tree.** Every box gets PyTorch already built at `viable/strict` and staged
   at `~/pytorch` → `import torch` works with **zero build**.
2. **Shared ccache** (`/ccache_shared`, one EFS mounted in every pod *and* the build node)
   → all C++/CUDA object compiles are cached and shared across users. Checking out a ref
   past `viable/strict`, or editing C++, reuses those objects instead of recompiling.

Expected timings (CUDA 13.2):

| action | time |
|---|---|
| warm claim → active | ~1–2 s |
| `import torch` (prebuilt) | 0 s |
| Python-only edit | 0 s (no rebuild — `PYTHONPATH=~/pytorch`) |
| edit one C++/CUDA file → rebuild | ~40 s (incremental: warm `build/` + ccache) |
| build a ref near `viable/strict` | a few min (mostly cache hits) |
| cold build (far ref / empty cache) | ~20–40 min (one-time; fills the cache for everyone) |

## PyTorch is fully editable

The box ships PyTorch **editable-installed**:
- **Python changes** — no rebuild; `PYTHONPATH=~/pytorch` resolves them immediately.
- **C++/CUDA changes** — `pip install -e . --no-build-isolation` rebuilds *incrementally*
  on the warm `build/` (ninja) with ccache, so a one-file edit is seconds-to-minutes,
  not a cold build.

So the loop is: repro a failing test → edit the kernel → rebuild in seconds → re-run.

---
*Draft — timings are from the m7i build node (128 jobs); a 1-GPU dev box builds at fewer
cores, so cold builds there are proportionally slower (see notes).*
