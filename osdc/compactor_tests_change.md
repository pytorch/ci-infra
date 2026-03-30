# Plan: Restructure Node Compactor E2E Tests

## Context

The node compactor gained capacity reservation (do-not-disrupt annotations) behavior. A previous session added `COMPACTOR_CAPACITY_RESERVATION_NODES=1` to the e2e config, breaking Phase 2: the reserved node was excluded from taint candidates, so the test timed out waiting for all drained nodes to be tainted.

Rather than patching the broken test, we restructure the entire e2e suite into three focused test groups that properly validate the compactor's distinct behavior modes.

## Architecture

**Three sequential test groups in one file**, sharing the same pytest session and node infrastructure:

```
TestGroupA_Bare  →  TestGroupB_AntiFlap  →  TestGroupC_Reservation
```

- **Group A** provisions nodes (10 min). Groups B and C reuse them via config-switch (~30s each).
- Config switching: `patch_compactor_env()` + rollout wait between groups.
- Run with `-x` (stop on first failure) — groups are sequential.
- Estimated total: **~20 min** (vs ~15 min current, but actually tests the features).

### Config per group

| Config key | Group A (Bare) | Group B (Anti-flap) | Group C (Reservation) |
|---|---|---|---|
| `COMPACTOR_INTERVAL` | `5` | `5` | `5` |
| `COMPACTOR_TAINT_COOLDOWN` | `30` | `15` | `30` |
| `COMPACTOR_MIN_NODE_AGE` | `0` | `3600` (for B1), then `0` | `0` |
| `COMPACTOR_TAINT_RATE` | `1.0` | `0.34` | `1.0` |
| `COMPACTOR_FLEET_COOLDOWN` | `0` | `20` | `0` |
| `COMPACTOR_SPARE_CAPACITY_NODES` | `0` | `0` | `0` |
| `COMPACTOR_SPARE_CAPACITY_RATIO` | `0` | `0` | `0` |
| `COMPACTOR_CAPACITY_RESERVATION_NODES` | `0` | `0` | `1` |

Group B uses `min_node_age=3600` (1 hour) for the B1 test. By the time Group B runs, nodes from Group A are ~15-20 min old — well under 3600s. The compactor treats them as "young" and refuses to taint. After verifying this, the config switches to `min_node_age=0` and the same nodes get tainted immediately. This avoids new node provisioning entirely.

## Group A: Bare Compactor (no anti-flap, no reservation)

All safety mechanisms disabled. Tests core compactor logic.

### A1: `test_scale_up_no_over_taint`
- Create 9 pods (3 per r5.24xlarge). Wait for 3+ nodes, all pods Running.
- Assert: >= 1 node untainted (min_nodes=1), no `do-not-disrupt` annotation on any node.
- Timeout: PROVISION_TIMEOUT=600s, then stabilize with `wait_for_stable(stable_s=10)`.

### A2: `test_empty_nodes_get_tainted`
- Delete pods from all-but-one node.
- Assert: all drained nodes tainted OR deleted by Karpenter. At least 1 untainted.
- Timeout: TAINT_TIMEOUT=90s.
- **This is the fix for the Phase 2 bug** — with `reservation=0`, no node is excluded.

### A3: `test_karpenter_deletes_empty_tainted_nodes`
- Wait for all tainted nodes to be deleted (Karpenter WhenEmpty).
- Assert: 0 tainted nodes. At least 1 node survives (the one with pods).
- Timeout: KARPENTER_DELETE_TIMEOUT=360s.

### A4: `test_burst_absorption`
- Adaptive preamble: top up to 9 pods / 3 nodes if needed.
- Drain all-but-one node. Wait for taints. Create burst pods on tainted nodes.
- Assert: all pods reach Running (compactor untainted to absorb burst).
- Timeout: PROVISION_TIMEOUT + TAINT_TIMEOUT + BURST_TIMEOUT=180s.

### A5: `test_min_nodes_enforcement`
- Delete all pods.
- Assert: if any nodes exist, >= 1 is untainted. No `do-not-disrupt` on any node.
- `pytest.skip` if pool has 0 nodes.
- Timeout: TAINT_TIMEOUT=90s.

### A6: `test_sigterm_removes_taints`
- Scale compactor to 0 (SIGTERM). `try/finally` restores replicas=1.
- Assert: all compactor taints removed.
- `pytest.skip` if no tainted nodes exist.
- Timeout: TAINT_TIMEOUT=90s.

## Group B: Anti-Flap Mechanisms

Config switch at first test. Uses log-based assertions — search compactor logs for specific messages to confirm mechanisms fired.

### B1: `test_min_node_age_blocks_tainting`
- Config: `min_node_age=3600`, `taint_rate=1.0`, `fleet_cooldown=0`. All anti-flap except min_node_age disabled.
- Ensure 9 pods / 3+ nodes (reuse from Group A). Nodes are ~15-20 min old — well under 3600s.
- Delete pods from 2 nodes (make them empty surplus).
- Wait 2-3 compactor cycles (15s). Assert: drained nodes are NOT tainted (min_node_age=3600 protects them).
- Reconfigure compactor with `min_node_age=0` (quick rollout, ~30s).
- Wait for compactor to stabilize. Assert: drained nodes ARE now tainted (age protection removed).
- Timeout: TAINT_TIMEOUT=90s per phase.
- Flakiness mitigation: The 3600s threshold is vastly larger than node age (~1200s). No timing edge case. The negative assertion (NOT tainted) uses `wait_for_stable()` to confirm the state holds for 2+ cycles, not just a single snapshot.

### B2: `test_rate_limiting`
- Config switch: `min_node_age=0`, `taint_rate=0.34`, `fleet_cooldown=20`. (May already be correct from B1's reconfigure.)
- Ensure 9 pods / 3+ nodes. Record log position.
- Delete all 9 pods simultaneously — 3 surplus nodes, `taint_rate=0.34` → `ceil(3*0.34)=2` max new taints/cycle.
- Wait for all 3 nodes tainted (final state same regardless).
- Assert: compactor logs contain `"Rate-limited"` (from compactor.py:158). This is deterministic: 3 surplus, cap is 2, so 1 node MUST be rate-limited on cycle 1.
- Timeout: TAINT_TIMEOUT=90s.

### B3: `test_fleet_cooldown_blocks_after_burst`
- From B1 end state: 3 tainted empty nodes.
- Create 9 pods → go Pending → burst untaint triggers after ~35s (PENDING_POD_MIN_AGE=30s).
- Wait for all 9 pods Running. Record log position.
- Delete pods from 2 nodes (make them empty surplus).
- Wait for those 2 nodes tainted (fleet cooldown=20s, so first ~4 cycles blocked, then taints proceed).
- Assert: compactor logs contain `"Fleet cooldown blocked"` (from compactor.py:209). This is deterministic: burst untaint just happened, `fleet_cooldown=20s > 4 cycles * 5s interval`.
- Timeout: BURST_TIMEOUT=180s for burst, TAINT_TIMEOUT=90s for re-tainting.

### What we don't test in e2e (and why)

- **`taint_cooldown`**: The only non-mandatory untaint path is the safety-check failure in `compute_taints()`. Manufacturing this state in e2e requires precise pod distribution control. Thoroughly unit-tested.

## Group C: Reservation Behavior

Config switch: `COMPACTOR_CAPACITY_RESERVATION_NODES=1`, all anti-flap disabled.

### C1: `test_reserved_node_excluded_from_taint`
- Ensure 9 pods / 3+ nodes. Wait for stabilization with new config.
- Assert: exactly 1 node has `node-compactor.osdc.io/capacity-reserved=true` AND `karpenter.sh/do-not-disrupt=true`.
- Delete pods from all-but-one node (including from reserved node if it has pods).
- Wait for compactor to stabilize.
- Assert: reserved node is NOT tainted. Other empty nodes ARE tainted or deleted.
- Timeout: TAINT_TIMEOUT=90s.

### C2: `test_min_nodes_with_reservation`
- Delete all pods.
- Assert: >= 1 node untainted (min_nodes=1). >= 1 node has `do-not-disrupt`. The untainted + reserved should overlap (same node).
- `pytest.skip` if pool has 0 nodes.
- Timeout: TAINT_TIMEOUT=90s.

### C3: `test_reservation_cleanup_on_shutdown`
- Verify at least 1 node has reservation annotations.
- Scale compactor to 0 (SIGTERM). `try/finally` restores replicas=1.
- Assert: no node has `node-compactor.osdc.io/capacity-reserved`. No node has `karpenter.sh/do-not-disrupt` (that was set by the compactor). All compactor taints removed.
- Timeout: TAINT_TIMEOUT=90s.

## Flakiness Mitigations

1. **Never assert exact node counts.** Karpenter over-provisions sometimes. Always `>=` or `<=`.
2. **Accept "deleted by Karpenter" as equivalent to "tainted".** Nodes can vanish between cycles.
3. **Use `wait_for_stable()` before state assertions.** Ensures 2+ compactor cycles with no changes.
4. **Log-based assertions for anti-flap.** Deterministic strings logged when rate-limit/fleet-cooldown fire. No wall-clock timing dependency.
5. **Record log position before each log-assertion test.** Search only new lines. Prevents false positives from earlier tests.
6. **Adaptive preamble.** Burst absorption test tops up nodes if prior tests left fewer than needed.
7. **`try/finally` on all shutdown tests.** Restores replicas=1 even on failure, so subsequent groups aren't broken.

## Files to Modify

| File | Action | Key Changes |
|---|---|---|
| `tests/e2e/test_e2e.py` | **Rewrite** | 6 phases → 3 groups (A: 6 tests, B: 3 tests, C: 3 tests). ~450 lines. |
| `tests/e2e/conftest.py` | **Modify** | Session fixture starts with Group A config (reservation=0). Store per-group configs as constants. |
| `tests/e2e/helpers.py` | **Modify** | Add: `reconfigure_compactor()`, `get_reserved_nodes()`, `get_do_not_disrupt_nodes()`, `search_compactor_logs()`. |

All paths relative to `base/node-compactor/`.

### Key source files (read-only reference)
- `scripts/python/compactor.py` — reconcile() flow, log messages at lines 158, 209
- `scripts/python/packing.py` — compute_taints(), select_reserved_nodes()
- `scripts/python/reservations.py` — reconcile_reservations(), cleanup_reservations()
- `scripts/python/discovery.py` — PENDING_POD_MIN_AGE_SECONDS=30 (line 22)

## New Helper Functions

### `reconfigure_compactor(client, env_overrides, compactor_logs)`
Wraps: `patch_compactor_env()` → `wait_for_compactor_rollout()` → brief sleep for first reconciliation. Returns old env values for optional restore.

### `get_reserved_nodes(client, nodepool_name) → set[str]`
Returns node names having `node-compactor.osdc.io/capacity-reserved=true`.

### `get_do_not_disrupt_nodes(client, nodepool_name) → set[str]`
Returns node names having `karpenter.sh/do-not-disrupt=true`.

### `search_compactor_logs(collector, pattern, since_line=0) → list[str]`
Search the `CompactorLogCollector`'s captured lines (from `since_line` onward) for a regex pattern. Returns matching lines.

## Implementation Split (Parallel Agents)

Three agents can work in parallel:

1. **Agent: helpers.py** — Add the 4 new helper functions. No conflicts with other agents.
2. **Agent: conftest.py** — Modify session fixture to use Group A config, add group config constants, add `log_position` fixture.
3. **Agent: test_e2e.py** — Rewrite the test file with 3 groups and 12 tests. Depends on knowing the helper/conftest APIs (provide as spec).

## Verification

1. `just lint` — all linters pass
2. `just test` — all unit tests pass (e2e tests don't run in `just test`)
3. Manual review: every assertion in the old test suite (Phases 1-6) has a corresponding assertion in the new suite
4. E2e run: `just test-compactor <cluster>` on a live cluster (not automated in CI, done manually)
