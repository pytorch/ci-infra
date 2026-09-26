"""Smoke test: placeholder pods must be schedulable wherever the workflow pod is.

The capacity-aware listener pre-creates placeholder pods to reserve capacity
for incoming workflow pods. This file validates that each LIVE placeholder
pod's scheduling envelope (required nodeAffinity + tolerations) is COMPATIBLE
with the workflow pod template it is supposed to reserve capacity for.

# Subset semantics

We want: "anywhere the placeholder fits, the workflow pod also fits".
This requires:

  - Required nodeAffinity terms — placeholder MUST have AT LEAST every
    required term the workflow pod has. More required terms = stricter
    selection. Placeholder can be stricter (set ⊇ workflow required terms).

  - Tolerations — placeholder MUST tolerate AT MOST what the workflow pod
    tolerates. A toleration relaxes the schedulable set. If the placeholder
    tolerated a taint the workflow pod doesn't, the placeholder could land
    on a node the workflow pod cannot use (workflow won't take over that
    reserved capacity). Placeholder tolerations ⊆ workflow tolerations.

If parity fails: placeholders pre-warm capacity that workflow pods cannot
actually claim. Capacity reservation silently degrades.
"""

from __future__ import annotations

import subprocess
from typing import Any

import pytest
import yaml
from helpers import filter_pods, run_kubectl
from runner_defs import GeneratedScaleSet, scale_set_label_of_pod

pytestmark = [pytest.mark.live]

NAMESPACE = "arc-runners"

# Labels set by the listener's PlaceholderManager (see
# /actions-runner-controller/cmd/ghalistener/capacity/placeholder.go).
LABEL_MANAGED_BY = "app.kubernetes.io/managed-by"
LABEL_ROLE = "capacity.actions.github.com/role"
LABEL_SCALE_SET = "actions.github.com/scale-set-name"
ROLE_PLACEHOLDER_WORKFLOW = "placeholder-workflow"
MANAGED_BY_VALUE = "capacity-monitor"

WORKFLOW_PLACEHOLDER_LABELS = {
    LABEL_MANAGED_BY: MANAGED_BY_VALUE,
    LABEL_ROLE: ROLE_PLACEHOLDER_WORKFLOW,
}

# Tolerations that legitimately appear on the live placeholder pod but not in
# the pre-admission workflow pod template (with the same tuple shape). Filtered
# from the placeholder before the subset comparison because they don't represent
# a real scheduling-envelope divergence at runtime:
#
#   - node.kubernetes.io/not-ready / node.kubernetes.io/unreachable (NoExecute):
#     auto-injected on every pod by the kube-apiserver DefaultTolerationSeconds
#     admission plugin. tolerationSeconds is apiserver-flag-driven so we match
#     by (key, effect) only.
#
#   - nvidia.com/gpu (NoSchedule): the OSDC PlaceholderManager emits this with
#     operator=Exists (no value), while the workflow pod template emits it with
#     operator=Equal, value="true" (matching the GPU NodePool taint). Both
#     tolerate the actual taint at runtime; the test's 4-tuple equality just
#     sees them as distinct. The placeholder's Exists is technically broader
#     than the workflow's Equal/"true" — accepted because the GPU node taint
#     in this cluster always uses value "true".
IGNORED_PLACEHOLDER_TOLERATIONS: frozenset[tuple[str, str]] = frozenset(
    {
        ("node.kubernetes.io/not-ready", "NoExecute"),
        ("node.kubernetes.io/unreachable", "NoExecute"),
        ("nvidia.com/gpu", "NoSchedule"),
    }
)


def _proactive_capacity_from_generated(generated_doc: dict) -> int:
    """Return the CAPACITY_AWARE_PROACTIVE_CAPACITY env value from a generated YAML.

    Returns 0 if the env var is missing, has no literal value, or the value
    cannot be parsed as int — defensive default that excludes the def from
    the parity check rather than crashing.
    """
    containers = generated_doc.get("listenerTemplate", {}).get("spec", {}).get("containers", []) or []
    if not containers:
        return 0
    for entry in containers[0].get("env", []) or []:
        if entry.get("name") == "CAPACITY_AWARE_PROACTIVE_CAPACITY":
            raw = entry.get("value", "")
            try:
                return int(raw)
            except (TypeError, ValueError):
                return 0
    return 0


# ---------------------------------------------------------------------------
# Workflow pod-template loading (lenient)
# ---------------------------------------------------------------------------


def _load_workflow_pod_template(cm_name: str) -> dict[str, Any] | None:
    """Load the workflow pod template from the named hook ConfigMap.

    The ConfigMap's job-pod.yaml uses ``$job`` as the container name (a hook
    DSL placeholder), so we cannot validate it as a Pod object — just navigate
    the dict with yaml.safe_load. Returns the parsed dict's ``spec`` (the
    fields we care about: nodeSelector / tolerations / affinity) or None when
    the ConfigMap is missing or malformed.

    Contract: ``None`` means "ConfigMap absent / no usable template" — the
    caller treats that as a failure with a clear message. Other failures
    (timeouts, network issues, malformed JSON from kubectl) propagate so the
    test fails loudly instead of silently degrading to a misleading None.
    """
    try:
        cm = run_kubectl(["get", "configmap", cm_name], namespace=NAMESPACE)
    except subprocess.CalledProcessError:
        # kubectl returns non-zero when the ConfigMap doesn't exist — surface
        # that as None so the caller can emit a clear "missing template"
        # failure. Other exception classes (TimeoutExpired, JSONDecodeError,
        # OSError) propagate intentionally.
        return None
    raw = cm.get("data", {}).get("job-pod.yaml", "")
    if not raw:
        return None
    parsed = yaml.safe_load(raw)
    if not isinstance(parsed, dict):
        return None
    return parsed.get("spec", {}) or {}


# ---------------------------------------------------------------------------
# Affinity / toleration extraction + subset semantics
# ---------------------------------------------------------------------------


def _required_terms(spec: dict[str, Any]) -> list[dict[str, Any]]:
    """Return the list of required matchExpressions terms from a pod spec.

    Each entry in the returned list is a single matchExpression dict with
    keys ``key`` / ``operator`` / ``values`` (values may be missing for
    Exists / DoesNotExist).

    A pod has no required nodeAffinity by default — return [].
    """
    affinity = spec.get("affinity") or {}
    node_aff = affinity.get("nodeAffinity") or {}
    required = node_aff.get("requiredDuringSchedulingIgnoredDuringExecution") or {}
    if not required:
        return []
    out: list[dict[str, Any]] = []
    for term in required.get("nodeSelectorTerms", []) or []:
        for me in term.get("matchExpressions", []) or []:
            out.append(me)
    return out


def _term_compatible(placeholder: dict, workflow: dict) -> bool:
    """True if the placeholder term is at least as restrictive as workflow's.

    Same key + same operator. For value-bearing operators (In / NotIn),
    placeholder values must be a SUBSET of workflow values (placeholder
    matches a strict subset of nodes). For Exists / DoesNotExist (no values),
    a key+operator match is sufficient.
    """
    if placeholder.get("key") != workflow.get("key"):
        return False
    if placeholder.get("operator") != workflow.get("operator"):
        return False
    op = placeholder.get("operator")
    if op in ("In", "NotIn"):
        ph_vals = set(placeholder.get("values", []) or [])
        wf_vals = set(workflow.get("values", []) or [])
        return ph_vals.issubset(wf_vals)
    return True


def _missing_required_terms(placeholder_spec: dict, workflow_spec: dict) -> list[dict]:
    """Return workflow required terms NOT covered by any placeholder term.

    For parity, the placeholder must have a compatible counterpart for every
    workflow required term (placeholder ⊇ workflow on required affinity).

    GPU asymmetry note: workflow pods declare the GPU node-selector only in
    ``preferredDuringSchedulingIgnoredDuringExecution``, while placeholders
    declare it in ``requiredDuringSchedulingIgnoredDuringExecution``. The
    placeholder is intentionally stricter — it MUST land on a GPU node to
    reserve capacity. Workflow uses preferred because the GPU NodePool's
    selector + tolerations already guarantee GPU node placement. This
    asymmetry is fine under our subset semantics: stricter placeholder
    affinity is allowed (placeholder ⊇ workflow required terms).
    """
    ph_terms = _required_terms(placeholder_spec)
    missing = []
    for wf_term in _required_terms(workflow_spec):
        if not any(_term_compatible(ph, wf_term) for ph in ph_terms):
            missing.append(wf_term)
    return missing


def _toleration_key(t: dict[str, Any]) -> tuple:
    """Hashable identity for a toleration — key + operator + value + effect."""
    return (t.get("key"), t.get("operator"), t.get("value"), t.get("effect"))


# Tolerations injected by K8s admission controllers that are absent from the
# pre-admission workflow pod template loaded from the hook ConfigMap. The
# placeholder is observed LIVE (post-admission), so these would always
# appear as spurious "excess" entries. Filter them on the placeholder side
# — when the workflow pod is actually scheduled, admission will inject the
# same tolerations.
#
# Sources:
#   - DefaultTolerationSeconds → not-ready / unreachable @ NoExecute (300s)
#     added to every pod that doesn't already declare them.
#   - ExtendedResourceToleration → nvidia.com/gpu @ NoSchedule added to any
#     pod requesting that extended resource. Workflow pods request the GPU
#     so they get it; placeholders mirror it explicitly to land on the same
#     nodes (placeholders themselves don't request the resource).
_ADMISSION_NOEXECUTE_KEYS = frozenset({"node.kubernetes.io/not-ready", "node.kubernetes.io/unreachable"})
_ADMISSION_NOSCHEDULE_EXTENDED_RESOURCE_KEYS = frozenset({"nvidia.com/gpu"})


def _is_admission_injected_toleration(t: dict[str, Any]) -> bool:
    if t.get("operator") != "Exists":
        return False
    effect = t.get("effect")
    key = t.get("key")
    if effect == "NoExecute" and key in _ADMISSION_NOEXECUTE_KEYS:
        return True
    return effect == "NoSchedule" and key in _ADMISSION_NOSCHEDULE_EXTENDED_RESOURCE_KEYS


def _excess_tolerations(placeholder_spec: dict, workflow_spec: dict) -> list[dict]:
    """Return placeholder tolerations NOT present on the workflow pod.

    Excess tolerations relax scheduling for the placeholder beyond what the
    workflow pod allows — the placeholder could land somewhere the workflow
    pod cannot. Returns [] when placeholder ⊆ workflow.

    Tolerations that are benign divergences from the workflow pod template
    are stripped from the placeholder before comparison — see
    ``IGNORED_PLACEHOLDER_TOLERATIONS``.
    """
    wf = {_toleration_key(t) for t in (workflow_spec.get("tolerations") or [])}
    return [
        t
        for t in (placeholder_spec.get("tolerations") or [])
        if (t.get("key"), t.get("effect")) not in IGNORED_PLACEHOLDER_TOLERATIONS and _toleration_key(t) not in wf
    ]


# ---------------------------------------------------------------------------
# The test
# ---------------------------------------------------------------------------


class TestPlaceholderSchedulingParity:
    """Parity check between live placeholder pods and the workflow pod template.

    Placeholder pods reserve capacity for workflow pods. To be useful, the
    placeholder's scheduling envelope must be COMPATIBLE with the workflow
    pod's: required affinity ⊇ workflow's, tolerations ⊆ workflow's.
    See module docstring for the full subset rationale.
    """

    def test_workflow_placeholder_parity(
        self,
        all_pods: dict,
        enabled_modules: list[str],
        generated_scale_sets: list[GeneratedScaleSet],
    ) -> None:
        if "arc-runners" not in enabled_modules:
            pytest.skip("arc-runners module not enabled")

        # Only scale sets that opted into proactive capacity run placeholders.
        # Use the GENERATED YAML's CAPACITY_AWARE_PROACTIVE_CAPACITY env value
        # (post-override) — not the raw def — so cluster-specific overrides like
        # `proactive_capacity_max` on staging correctly skip all scale sets.
        # Key by the org-unique scale-set-name label: per-org fan-out gives each
        # org its own scale set (and its own hook ConfigMap) sharing a def, so a
        # def-keyed grouping would conflate the two orgs' placeholders.
        proactive = {
            s.scale_set_label: s for s in generated_scale_sets if _proactive_capacity_from_generated(s.values) > 0
        }
        if not proactive:
            pytest.skip("no scale sets with proactive_capacity > 0")

        # Group live placeholder pods by their owning scale set via the
        # scale-set-name label (org-unique — resourceName for additional orgs,
        # runnerScaleSetName for the primary). ARC's capacity monitor stamps this
        # label from the AutoscalingRunnerSet's Kubernetes name, so it matches the
        # generated scale_set_label exactly (no prefix stripping needed).
        all_ph_pods = filter_pods(all_pods, namespace=NAMESPACE, labels=WORKFLOW_PLACEHOLDER_LABELS)
        ph_pods_by_label: dict[str, list[dict]] = {label: [] for label in proactive}
        unattributed: list[str] = []
        for pod in all_ph_pods:
            pod_name = pod.get("metadata", {}).get("name", "?")
            label = scale_set_label_of_pod(pod)
            if not label:
                unattributed.append(f"{pod_name}: placeholder missing {LABEL_SCALE_SET!r} label")
                continue
            if label not in proactive:
                # Placeholder for a scale set without proactive_capacity > 0
                # locally — likely a stale scale-set or a def/org removed without
                # the corresponding placeholders being cleaned up. Skip silently;
                # the env-var coherence test in test_runners.py catches stale
                # scale-sets directly.
                continue
            ph_pods_by_label[label].append(pod)

        problems: list[str] = list(unattributed)
        info: list[str] = []

        for label, scale_set in proactive.items():
            ph_pods = ph_pods_by_label[label]
            if not ph_pods:
                # Informational, not a failure — placeholders may be in
                # between cycles or all preempted/Pending right now. Report
                # the effective (post-override) proactive_capacity from the
                # generated YAML, not the raw def value.
                effective = _proactive_capacity_from_generated(scale_set.values)
                info.append(f"{label}: no live workflow placeholders (proactive={effective})")
                continue

            # Load the workflow template from THIS scale set's own hook ConfigMap
            # (org-specific: arc-runner-hook-<slug>-<def> for an additional org).
            workflow_spec = _load_workflow_pod_template(scale_set.configmap_name)
            if workflow_spec is None:
                problems.append(
                    f"{label}: workflow pod template (hook ConfigMap {scale_set.configmap_name}) missing or empty"
                )
                continue

            for pod in ph_pods:
                pod_name = pod["metadata"]["name"]
                ph_spec = pod.get("spec", {})

                missing = _missing_required_terms(ph_spec, workflow_spec)
                for term in missing:
                    problems.append(
                        f"[{label}] {pod_name}: workflow requires affinity term not present on "
                        f"placeholder — expected {term}"
                    )

                excess = _excess_tolerations(ph_spec, workflow_spec)
                for tol in excess:
                    problems.append(
                        f"[{label}] {pod_name}: placeholder tolerates {tol} which the workflow "
                        f"pod does not — placeholder could land where workflow cannot"
                    )

        if info:
            # Surface skip-style notices through the test report (they don't
            # fail the test, but they're worth seeing in CI output).
            print("\nPlaceholder parity informational notes:")
            for line in info:
                print(f"  - {line}")

        assert not problems, "Workflow placeholder ↔ workflow pod parity failures:\n" + "\n".join(problems)
