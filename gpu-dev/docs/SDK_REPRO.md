# gpu-dev SDK + repro — sub-second GPU sandboxes

Reserve a **warm** GPU box in ~1s, run code on it, and auto-clean up — from Python
or one CLI command. Backed by a pool of pre-booted pods with **PyTorch prebuilt**
(viable/strict), so `import torch` works instantly with no build. And when you *do*
have to compile — your ref moved past viable/strict, or you touch C++ — a shared
compiler cache (ccache) makes it an **incremental, not a cold, build** (see
[Builds are cached](#builds-are-cached-shared-ccache)).

> Requires `gpu-dev` ≥ 0.7.1 (CLI **and** SDK in one package): `pip install --upgrade gpu-dev`

## Python SDK

```python
from gpu_dev import GpuDev

client = GpuDev()

# ~instant warm claim; ephemeral; auto-cancels on context exit.
with client.reserve(gpu_type="b200", gpu_count=1, hours=1) as sb:
    print(sb.ssh_command)
    print(sb.exec("python -c 'import torch; print(torch.__version__, torch.cuda.is_available())'").stdout)
    sb.upload("./train.py", "/home/dev/train.py")
    print(sb.exec("python /home/dev/train.py").stdout)
```

- `reserve(...)` returns an **active** `Sandbox` (warm claim, no polling). It falls
  back to a queued reservation only if no warm pod / not eligible.
- `gpu_type`: `b200`, `h100`, `a100`, `t4`, MIG slices (`b200-mig-1g`, …), `cpu-x86`/`cpu-arm`.
- `sb.exec(cmd)` → `.stdout` / `.stderr` / `.returncode`. `sb.upload(local, remote)`.
- Context manager auto-cancels; or `client.reserve(..., wait=...)` + `sb.cancel()`.

## Repro a GitHub issue / failing CI test

PyTorch is pre-staged at `~/pytorch` (importable). To reproduce a failure, point at
the **PR or commit** and run the test.

**One CLI command** (reserve → checkout → run → auto-cancel):
```bash
gpu-dev repro pr/185264 test/inductor/test_flex_attention.py TestFlexAttentionCUDA.test_large_kv_int64_pointer_math_cuda
```
- `REF`: `pr/<N>`, `#<N>`, a bare PR number, a branch, or a commit sha.
- PRs use **`pull/<N>/merge`** (what CI actually tests — the PR merged onto current
  trunk), falling back to `/head`. Use this, not the raw branch.
- `--keep` to inspect afterward instead of auto-cancelling.

**From the SDK:**
```python
with client.reserve(gpu_type="b200", gpu_count=1, ref="pr/185264") as sb:   # --ref ⇒ ephemeral + staged
    r = sb.exec(
        "cd ~/pytorch && "
        "python test/inductor/test_flex_attention.py "
        "TestFlexAttentionCUDA.test_large_kv_int64_pointer_math_cuda"
    )
    print(r.stdout, r.stderr)
```

**Inside the pod**, iterate fast:
```bash
cd ~/pytorch
git fetch origin pull/<N>/merge && git checkout FETCH_HEAD   # or a commit sha
python test/<path> <TestClass>.<test>
# edit C++? rebuild incrementally on the warm build/ (~tens of sec):
pip install -e . --no-build-isolation
```
Python-only changes need no rebuild — `PYTHONPATH=~/pytorch` already resolves.

## Builds are cached (shared ccache)

Two layers of caching mean you almost never pay for a cold, from-scratch build —
including the full C++/CUDA compile (gcc/nvcc):

1. **Prebuilt tree.** Every box gets PyTorch already built at viable/strict and
   staged at `~/pytorch`, so `import torch` works with **zero build**.
2. **Shared compiler cache (ccache).** `CCACHE_DIR=/ccache_shared` is an EFS volume
   mounted in **every** dev pod *and* the dedicated build node, so all the C++/CUDA
   object compiles are cached and **shared across users and the build node**. When
   you check out a ref past viable/strict — or edit C++ — the rebuild reuses those
   cached objects (and the warm `build/` for ninja) instead of recompiling from
   scratch. So even a "full" `pip install -e .` is a warm build, not a cold one.

Measured (m7i build node, 128 jobs, CUDA 13.2):

| scenario | time |
|---|---|
| `import torch` (prebuilt, no build) | ~0s |
| incremental (1 kernel changed + relink) | ~40s |
| ninja no-op (nothing changed) | ~20s |
| from-scratch `build/` with warm ccache (~86% hit) | ~21 min |

(A true cold build from an empty ccache is far longer.) The cache stays warm on its
own: an hourly build-node job compiles each viable/strict bump into `/ccache_shared`,
so the objects you need are usually already there by the time you build — and your
own compiles populate it for the next person too.

## Gotchas
- **`/merge` vs `/head`**: `/head` is the PR author's raw branch and often lacks
  trunk-added tests; `/merge` is what CI ran. `gpu-dev repro` / `--ref` use `/merge`.
- **The prebuilt is viable/strict.** If your ref moved past it and a test needs new
  C++, do the one incremental `pip install -e . --no-build-isolation` — it's fast
  (warm shared ccache), not a cold build. See [Builds are cached](#builds-are-cached-shared-ccache).
- **Ephemeral by design.** Repro boxes have no persistent disk; bring code via
  `--ref`, `sb.upload`, or git.

See also: `sdk/python/README.md` and `sdk/python/examples/`.
