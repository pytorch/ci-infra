"""Helpers for loading ARC runner definitions and mapping live pods to defs.

These helpers are shared between the listener env-var coherence test and the
placeholder ↔ workflow scheduling parity test.
"""

from __future__ import annotations

import os
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path

import yaml

# Label that ARC sets on listener pods. Value = AutoscalingRunnerSet's name,
# which is the chart's `runnerScaleSetName` = `<runner_name_prefix><def_name>`.
SCALE_SET_NAME_LABEL = "actions.github.com/scale-set-name"


def arc_runners_module_names(upstream_dir: Path) -> set[str]:
    """Names of every arc-runners* module in the codebase (canonical + variants).

    A module qualifies if it lives at ``modules/arc-runners*`` and has a
    ``defs/`` subdir (variants without defs/ aren't real runner modules).
    Used by tests to scope cluster-resource queries to the per-module
    ``osdc.io/module=<name>`` label values.
    """
    return {p.name for p in (upstream_dir / "modules").glob("arc-runners*") if p.is_dir() and (p / "defs").is_dir()}


def defs_dirs(upstream_dir: Path, modules: Iterable[str] | None = None) -> list[Path]:
    """Resolve ARC runner def directories.

    Honors the ``ARC_RUNNERS_DEFS_DIR`` env override (returns just that path
    in a single-element list) for single-module tooling.

    If ``modules`` is provided, returns ``modules/<m>/defs`` for each — used
    by tests that want to scope to a cluster's *enabled* arc-runners*
    modules. If omitted, returns the union of every ``arc-runners*/defs`` in
    the codebase.
    """
    override = os.environ.get("ARC_RUNNERS_DEFS_DIR")
    if override:
        return [Path(override)]
    if modules is not None:
        return [upstream_dir / "modules" / m / "defs" for m in sorted(modules)]
    return sorted((upstream_dir / "modules").glob("arc-runners*/defs"))


def load_runner_defs(upstream_dir: Path, modules: Iterable[str] | None = None) -> list[dict]:
    """Load all runner definition YAML files; return the inner `runner` dicts."""
    out: list[dict] = []
    for d in defs_dirs(upstream_dir, modules):
        if not d.is_dir():
            continue
        for f in sorted(d.glob("*.yaml")):
            data = yaml.safe_load(f.read_text())
            if data and "runner" in data:
                out.append(data["runner"])
    return out


def load_defs_by_name(upstream_dir: Path, modules: Iterable[str] | None = None) -> dict[str, dict]:
    """Index runner defs by `name` for fast lookup."""
    return {d["name"]: d for d in load_runner_defs(upstream_dir, modules)}


def def_name_from_scale_set(scale_set_name: str, runner_name_prefix: str) -> str | None:
    """Strip the cluster's runner_name_prefix from a scale-set name.

    Returns None if the name does not start with the prefix (which would
    indicate a stale scale-set or a misconfigured cluster — surface that
    rather than silently returning the unstripped name).

    Example:
        scale_set_name = "c-mt-l-arm64g2-6-32"
        runner_name_prefix = "c-mt-"
        returns: "l-arm64g2-6-32"

    An empty prefix is allowed — the scale-set name is the def name as-is.
    """
    if runner_name_prefix and not scale_set_name.startswith(runner_name_prefix):
        return None
    return scale_set_name[len(runner_name_prefix) :]


def def_for_listener_pod(
    pod: dict,
    defs_by_name: dict[str, dict],
    runner_name_prefix: str,
) -> tuple[str | None, dict | None]:
    """Map a listener pod to its runner def via the scale-set-name label.

    Returns ``(def_name, def_dict)`` or ``(None, None)`` if no mapping.
    The def_name is returned even when the def is missing (for diagnostics).
    """
    labels = pod.get("metadata", {}).get("labels", {})
    scale_set = labels.get(SCALE_SET_NAME_LABEL)
    if not scale_set:
        return None, None
    def_name = def_name_from_scale_set(scale_set, runner_name_prefix)
    if def_name is None:
        return None, None
    return def_name, defs_by_name.get(def_name)


def scale_set_label_from_values(values: dict) -> str:
    """The ``actions.github.com/scale-set-name`` label ARC stamps on this scale
    set's listener and placeholder pods, read from a generated chart-values doc.

    ARC labels those pods with the AutoscalingRunnerSet's Kubernetes name, which
    the chart derives as ``resourceName | default runnerScaleSetName``
    (gha-runner-scale-set/templates/_helpers.tpl; the controller copies that name
    onto the listener pod, and the capacity monitor onto placeholder pods).
    Per-org fan-out sets ``resourceName`` (``<resource_slug>-<def>``) so each org's
    pods carry an org-unique label; the primary org leaves it unset and the label
    is ``runnerScaleSetName`` (``<runner_name_prefix><def>``). This asymmetry is
    why a bare prefix-strip cannot map an additional org's pods to their def.
    """
    resource_name = values.get("resourceName")
    if resource_name:
        return str(resource_name)
    return str(values.get("runnerScaleSetName") or "")


def scale_set_label_of_pod(pod: dict) -> str | None:
    """The ``actions.github.com/scale-set-name`` label on a live listener or
    placeholder pod, or None when absent."""
    return pod.get("metadata", {}).get("labels", {}).get(SCALE_SET_NAME_LABEL)


@dataclass(frozen=True)
class GeneratedScaleSet:
    """One generated ARC scale set — the parsed contents of a single
    ``modules/<arc-runners*>/generated/*.yaml`` file.

    Generated files are the source of truth for what a cluster deploys. Per-org
    fan-out emits one file per (def, org): the primary org keyed by the bare def
    name and each additional org by ``<resource_slug>-<def>``, each with its own
    org-unique hook ConfigMap and scale-set label. Reconstructing expected names
    or the pod-to-def mapping from def names alone misses every additional-org
    scale set — read them from here instead.
    """

    resource_id: str  # generated file stem: "<def>" (primary) or "<slug>-<def>"
    scale_set_label: str  # actions.github.com/scale-set-name on live pods
    def_name: str  # bare runner-def name, shared across a def's orgs
    values: dict  # doc 1: chart values (runnerScaleSetName, listenerTemplate, ...)
    configmap: dict  # doc 2: job-pod hook ConfigMap

    @property
    def configmap_name(self) -> str:
        """Authoritative hook ConfigMap name (doc 2 ``metadata.name``)."""
        return (self.configmap.get("metadata") or {}).get("name", "") or ""


def generated_dirs(upstream_dir: Path, modules: Iterable[str] | None = None) -> list[Path]:
    """Resolve ARC generated-config directories (mirrors :func:`defs_dirs`).

    If ``modules`` is provided, returns ``modules/<m>/generated`` for each — used
    to scope to a cluster's *enabled* arc-runners* modules. If omitted, returns
    the union of every ``arc-runners*/generated`` in the codebase.
    """
    if modules is not None:
        return [upstream_dir / "modules" / m / "generated" for m in sorted(modules)]
    return sorted((upstream_dir / "modules").glob("arc-runners*/generated"))


def read_generated_scale_sets(dirs: Iterable[Path], runner_name_prefix: str) -> list[GeneratedScaleSet]:
    """Parse every generated runner YAML in ``dirs`` into GeneratedScaleSet entries.

    Each generated file is a two-doc YAML: doc 1 = chart values (with the
    identity fields ``runnerScaleSetName`` / ``resourceName``), doc 2 = the hook
    ConfigMap. ``def_name`` is recovered by stripping ``runner_name_prefix`` from
    ``runnerScaleSetName`` — which the generator always emits as
    ``<prefix><def>`` for every org — so the bare def is shared across a def's
    primary and additional-org scale sets.
    """
    out: list[GeneratedScaleSet] = []
    for d in dirs:
        if not d.is_dir():
            continue
        for f in sorted(d.glob("*.yaml")):
            docs = list(yaml.safe_load_all(f.read_text()))
            values = docs[0] if docs and isinstance(docs[0], dict) else {}
            configmap = next(
                (doc for doc in docs if isinstance(doc, dict) and doc.get("kind") == "ConfigMap"),
                {},
            )
            def_name = def_name_from_scale_set(str(values.get("runnerScaleSetName") or ""), runner_name_prefix)
            out.append(
                GeneratedScaleSet(
                    resource_id=f.stem,
                    scale_set_label=scale_set_label_from_values(values),
                    def_name=def_name or "",
                    values=values,
                    configmap=configmap,
                )
            )
    return out


def load_generated_scale_sets(
    upstream_dir: Path, runner_name_prefix: str, modules: Iterable[str] | None = None
) -> list[GeneratedScaleSet]:
    """GeneratedScaleSet entries across the given arc-runners* modules' generated dirs."""
    return read_generated_scale_sets(generated_dirs(upstream_dir, modules), runner_name_prefix)


def scale_sets_by_label(scale_sets: Iterable[GeneratedScaleSet]) -> dict[str, GeneratedScaleSet]:
    """Index scale sets by their (org-unique) live-pod scale-set-name label."""
    return {s.scale_set_label: s for s in scale_sets}
