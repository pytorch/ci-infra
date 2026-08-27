# `gpu-dev submit` — guide & footguns

`gpu-dev submit` reserves a box, (optionally) rsyncs a local dir up, runs your
command over SSH, syncs results back, and auto-cancels. It's the non-interactive
sibling of `gpu-dev reserve` — good for CI-style validation, one-shot test runs,
and scripted repros.

```bash
# run a script in a local dir on 1x H100, sync results back, auto-cancel
gpu-dev submit --runtime ./ --gpu-type h100 -- bash run.sh

# validate a PyTorch PR's tests on H100 (stages + builds the PR for you)
gpu-dev submit --gpu-type h100 --no-persistent-disk --ref pr/186015 -- \
    python test/test_foo.py -k some_test

# keep the box after the job (debug a failure interactively)
gpu-dev submit --keep-alive --gpu-type h100 -- pytest test/test_x.py
```

Exit code = your command's exit code (so it composes in scripts/CI).

---

## Footguns (read before your first `--ref` run)

### 1. `--ref` stages PyTorch in the background — `submit` now waits for it
With `--ref`, the in-pod startup checks out your ref into `/home/dev/pytorch`
**in the background** and only chowns the tree to `dev` + finishes the checkout
at the very end. Historically `submit` could SSH in and run your command before
that finished, so you'd hit:
- a **root-owned** `/home/dev/pytorch` (git: *"detected dubious ownership"*), and
- a **source/installed-torch mismatch** → `import torch` fails (the ref source is
  checked out but the importable `.so` is still the stale prebuilt base).

`submit` now **waits for staging to complete**, marks the tree a git
`safe.directory`, and (by default) **rebuilds incrementally** so the installed
torch matches the checked-out ref before your command runs. You don't need the
`sudo chown` / `safe.directory` workaround anymore.

### 2. `--ref` rebuilds torch by default — use `--no-build` to skip
The dropped-in `build/` + `.so` come from the **base** tree, not your ref. To make
`import torch` reflect your ref's compiled (C++/CUDA) changes, `submit --ref`
runs `pip install -e . --no-build-isolation` (incremental, warm `build/` →
typically tens of seconds; a cold/cross-arch build is much longer).

- Pass **`--no-build`** for Python-only PRs or quick checks — skips the rebuild
  (import still works; it just won't include compiled changes).
- A rebuild failure exits **90** *before* your command runs (so a broken build
  doesn't masquerade as a test failure).

### 3. Prebuilt fast path is **prod-arch only** (H100 / B200)
The by-SHA / viable-strict prebuilt trees are compiled for `sm_90;sm_100`
(H100/B200). On other GPU types (t4, a100, l4, …) or staging there's no matching
prebuilt, so `--ref` falls back to a **full from-scratch build** — slow. Validate
ref-based jobs on `--gpu-type h100` (or `b200`).

### 4. `--ref` is ignored with `--disk`
A persistent disk brings its own `/home/dev/pytorch`; `--ref` does **not** stage
onto a `--disk` reservation (and `submit` won't rebuild it). Use
`--no-persistent-disk` (or omit `--disk`) when you want a ref staged.

### 5. `--preserve-entrypoint` needs SSH
`submit` runs your command over SSH, so a custom image with
`--preserve-entrypoint` must still expose the SSH harness or `submit` can't reach
it. For pure entrypoint containers, use `reserve`, not `submit`.

### 6. Results sync-back is best-effort
With `--runtime`, output is rsync'd back to your local dir when the job exits
(unless `--no-pull`). If the box dies mid-job (spot reclaim, expiry) the sync-back
may be partial — you'll see a warning. For long jobs prefer `--keep-alive` and
pull manually, or write important artifacts to `/shared-personal` (persists
across reservations).

### 7. `--hours` is a ceiling, not the runtime
It's the reservation lifetime cap; the job auto-cancels as soon as your command
exits (unless `--keep-alive`). Set it high enough that queueing + build + run fit.

---

## Finding footguns early

- `gpu-dev submit --keep-alive … -- true` then `gpu-dev connect <id>` — get a
  box in the exact submit state and poke around before committing a real run.
- With `--ref`, watch staging directly: `tail -f /home/dev/.pytorch-staging.log`
  in the pod; `.pytorch-ready` (HEAD sha) is written when staging is done.
- `python -c "import torch; print(torch.__file__, torch.version.git_version)"`
  confirms which torch you're actually importing vs. the ref you asked for.

Found a new one? Add it here and ping `oncall:pytorch_release_engineering`.
