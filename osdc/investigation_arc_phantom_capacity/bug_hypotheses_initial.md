# Working Hypotheses (live document — promote to evidence when confirmed, strike when refuted)

## H1 — "HUD failure fallback: over-provision placeholders" commit advertises capacity without live placeholders

- **Source**: Phase 1 git log of ARC fork — commit `d5d94fb HUD failure fallback: over-provision placeholders`.
- **Why suspicious**: the commit subject suggests an explicit fallback path that bypasses the normal "wait for placeholders to be live" gate.
- **To verify**: Phase 2 Code Flow Agent + Phase 4 ARC fork module agent should read the commit diff and trace where its added code is invoked.

## H2 — `max_runners` defaults to `max_burst_capacity` regardless of placeholder state

- **Source**: Phase 1 — runner def has `max_burst_capacity: 30`.
- **Why suspicious**: if the listener emits `max_runners=30` to GitHub upfront and only LATER conditions it on placeholder liveness, GitHub will assign jobs before the gating ever kicks in.
- **To verify**: trace `CAPACITY_AWARE_*` listener env vars and the ghalistener code that talks to the GHA brokerage.

## H3 — Placeholder "live" check is satisfied by `PodScheduled` or `ContainerCreating`, not actual `Running`

- **Source**: hypothesis only.
- **Why suspicious**: a pod that can never schedule (no NodePool) stays Pending forever, but a pod that schedules and then ImagePullBackOffs/etc may have transient "Scheduled" status that satisfies a too-lenient liveness check.
- **To verify**: read the placeholder reconciliation code in the fork.

## H4 — The runner that picked up the job was NOT a placeholder — it was a real ephemeral runner that registered prior to its pod scheduling

- **Source**: hypothesis based on ARC architecture (registration happens early).
- **Why suspicious**: if the runner registers with GitHub via the listener BEFORE the pod is scheduled (or even if it never gets scheduled), GitHub may dispatch the job to it. The job then fails at Initialize containers because the pod never runs.
- **To verify**: Phase 6 cluster state — check whether there's a pod for runner `mt-l-x86aavx2-189-704-a10g-8-tfvvr-runner-np5rz` and whether it ever became Running.

## H5 — Listener proactive-capacity-max-runners feature has a bug where the "current registered" count is treated as available capacity

- **Source**: Phase 1 git log — `a3c294a Merge pull request #3 from jeanschmidt/jeanschmidt/proactive_capacity_max_runners`.
- **Why suspicious**: a feature branch dedicated to manipulating max_runners is the most likely culprit for advertising the wrong capacity.
- **To verify**: read the merge diff.

## H6 — `Detect and replace broken placeholder pairs` (commit d219a11) is interpreting unfulfillable placeholders as "broken" and replacing them in a way that grants capacity to non-placeholder runners

- **Source**: Phase 1 git log.
- **Why suspicious**: commit subject suggests automatic replacement; the replacement path may bypass the normal gate.
- **To verify**: read the commit diff and trace logic.

## Discarded

(none yet)
