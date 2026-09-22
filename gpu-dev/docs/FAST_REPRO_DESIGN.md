# Fast repro design — any commit from the last 72h, ready in < 2 min

**Goal:** `gpu-dev repro <ref>` for *any* pytorch/pytorch commit (or PR) from the
trailing ~72 h lands the developer in a pod with that ref built + importable in
**under 2 minutes**, then the in-pod edit→rebuild loop stays in the tens of seconds.

This is the design note for getting there. Status: proposal — nothing here is built
yet beyond the existing viable/strict prebuild.

---

## Where we are today (and why it's slow)

Measured on a live repro (`gpu-dev-h100-1e3f9c`, 1× H100, commit `3e3b83dd`):
- The pod builds the ref **in-pod**, capped at **36 cores** (`min(192, 192·(1/8)·1.5)`
  for a 1-GPU box) while ~156 cores on the node sit idle.
- ccache was **~95 % miss** (live: +4 hits / +75 misses over 30 s) because:
  1. the requested commit wasn't the one we'd warmed (the `pull/N/merge` SHA had
     drifted onto newer trunk), and
  2. the shared ccache is still small/cold (10 GB, just un-capped to 250 G).
- So: full cores unused, cache cold → a ~30–40 min cold compile.

Two structural problems: **the build runs on the wrong machine** (a GPU-share-capped
pod) and **we only cache one point** (viable/strict), so any other commit is a large,
mostly-uncached delta.

## Rejected: just lift the pod CPU limit ("CPU burst")

Letting a 1-GPU pod burst into the node's idle cores would speed the compile, but
**more parallel compiles = more concurrent RAM** (nvcc/cutlass TUs are memory-hungry);
against the pod's memory limit that risks **OOM-killing the build**. And it only helps
the *cold* path — it does nothing about caching. So we don't go this way.

## Chosen shape: build off-pod on a beefy node, stage the result

Three moving parts:

1. **Build farm** — the existing always-on build node (m7i.48xlarge, 192 vCPU /
   768 GB). Big RAM means we can run 128–192-way without OOM. (Bigger instances exist
   — `m7i.metal-48xl`, `c7i.48xlarge`, `hpc7a` — but beyond ~128 jobs PyTorch hits
   serial points: codegen, cmake configure, the final link. So "biggest box" is a
   marginal lever; **caching + linker are the real levers**, below.)
2. **Two-tier cache:**
   - **ccache** (`/ccache_shared`, 250 G EFS) — object cache, already shared by build
     node + all pods. Makes per-TU recompiles cheap.
   - **Artifact cache** (`/ccache_shared/prebuilt/by-sha/<sha>.tar.zst`, NEW) — whole
     *built trees* keyed by commit SHA. An exact hit means **no build at all**, just
     stage.
3. **Stage into the pod** — reflink-copy the built tree onto the pod's `/home/dev/pytorch`
   (same path the build used, so CMake's absolute paths stay valid → in-pod incremental
   rebuilds keep working). Reflink is ~instant when the node NVMe and the pod emptyDir
   are the same filesystem (warm pods already satisfy this).

### Request flow for `gpu-dev repro <ref>`

```
resolve ref → concrete SHA          (pr/N → pull/N/merge resolved to a pinned SHA NOW,
                                      so it can't drift; cache is keyed by that SHA)
claim a warm pod                     (~1 s, instant)
artifact cache has <sha>?
  ├─ yes → pod pulls by-sha/<sha>.tar.zst → reflink to /home/dev/pytorch   (~30–60 s)
  │         import torch works, ZERO build. Total ≈ claim + stage < ~90 s.
  └─ no  → build node builds <sha> incrementally from the nearest snapshot, publishes
            by-sha/<sha>.tar.zst, pod stages it.                            (build + stage)
run the test → drop the user in the box
```

### How we keep the "no" branch fast for *any* last-72h commit

The on-demand build must be a **small** delta, or it blows the 2-min budget. So we
pre-seed a **snapshot ladder** across the window:

- A cron builds PyTorch at a cadence across the trailing 72 h — every viable/strict
  bump (already happens) **plus** a fixed cadence (say every ~2 h, ~24–36 snapshots),
  each published to the artifact cache. Each snapshot is incremental from the previous
  one, so the ladder is cheap to maintain once warm.
- An on-demand request for commit `C` starts from the **nearest snapshot's build tree**
  (≤ ~2 h / a few hundred commits away), `git checkout C`, incremental build. Small
  dirty set + warm ccache → the compile is seconds, and the **link** becomes the floor.

### Killing the link floor (critical for < 2 min)

Even a 1-file delta relinks `libtorch_cuda.so` — normally ~1–3 min, which alone busts
the budget. Fix: **use a fast linker (mold, or lld)** for the build-node + in-pod
builds (`-fuse-ld=mold`). mold links libtorch in ~10–20 s. This is the single most
important build-speed change after caching.

### Staging speed

- Warm pods: node NVMe ↔ pod emptyDir same FS → reflink, near-instant. Verify node
  bootstrap puts kubelet emptyDir on `/mnt/nvme` (see the open item in CLAUDE.md).
- The by-sha tarball (~7–10 GB zstd) is pulled to node NVMe once per node and reflinked
  per pod; a DaemonSet can pre-pull the most-recently-requested SHAs.
- Arch: built trees are `sm_90;sm_100` (H100/B200). This whole fast path is **prod
  (H100/B200) only**; t4/staging would need a separate `sm_75` ladder (out of scope —
  staging is for plumbing tests, not perf).

---

## Storage budget

- Snapshot ladder: ~30 trees × ~7–10 GB zstd ≈ **250–300 GB**.
- On-demand built trees: LRU, keep last ~N requested SHAs (~50–100 GB).
- ccache: 250 G.
- Total on `ccache_shared` EFS: ~**500–650 GB**. EFS is elastic (no provisioning) +
  30-day IA lifecycle, so cost ≈ stored-GB only (~$0.30/GB-mo ⇒ ~$150–200/mo). Prune
  the ladder to the 72 h window so it doesn't grow unbounded.

## Phasing

- **Phase 1 — off-pod build + artifact cache.** `repro`/`--ref` build the SHA on the
  build node (192-way, warm ccache) instead of in-pod; publish `by-sha/<sha>`; pod
  stages it. Removes the 36-core + OOM problem. Reuses the existing prebuild machinery.
  *Biggest single win; do this first.*
- **Phase 2 — exact-SHA direct stage.** On an artifact-cache hit, skip the build
  entirely; just pull + reflink. Every repro populates the cache for the next dev.
- **Phase 3 — snapshot ladder + nearest-snapshot delta builds** across 72 h, so the
  cold "no" branch stays small. Tune cadence vs storage.
- **Phase 4 — mold/lld linker** (drop the link floor) and **cuDNN fidelity**
  (`USE_CUDNN=1`, add libcudnn to the image — see CLAUDE.md todo) so prebuilt matches CI.
- **Phase 5 — scale:** 2–3 build nodes + a small dispatch/queue so concurrent repros of
  uncached SHAs don't serialize behind one flock. Optionally pre-build **CI-red commits**
  (via the treehugger/HUD MCP) — those are exactly the commits people repro, so a red
  commit is in the artifact cache before anyone asks.

## Open decisions (need your call)

1. **Snapshot cadence vs storage** — every 2 h (~30 trees, ~300 GB) vs tighter (smaller
   deltas, more storage). Recommendation: start at viable/strict-bumps + every 2 h.
2. **Storage ceiling** — OK to let `ccache_shared` grow to ~500–650 GB? (elastic, ~$150–
   200/mo.)
3. **Build nodes** — keep 1 for now (Phases 1–4), add the dispatch/queue only at Phase 5?
4. **Pre-build CI-red commits** via HUD — worth it (targets real repro demand) or skip
   for v1?

## Edit→rebuild loop (unchanged, already good)

Once staged, in-pod iteration stays as-is: Python edits need no rebuild
(`PYTHONPATH=~/pytorch`); a C++/CUDA edit is `pip install -e . --no-build-isolation`,
an incremental build on the warm `build/` + ccache (~40 s, or ~15 s with mold). The
off-pod path is only for the *initial* "get me to ref X" jump.
