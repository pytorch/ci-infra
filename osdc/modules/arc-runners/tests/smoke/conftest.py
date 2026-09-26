"""arc-runners smoke test fixtures.

Reuses the shared smoke fixtures via star-import, then layers on arc-runners
specific fixtures (currently: parsed generated scale sets for the cluster under
test).
"""

from __future__ import annotations

from pathlib import Path

import pytest
from runner_defs import (
    GeneratedScaleSet,
    arc_runners_module_names,
    load_generated_scale_sets,
)
from smoke_conftest import *  # noqa: F403


@pytest.fixture(scope="session")
def generated_scale_sets(
    cluster_id: str,
    upstream_dir: Path,
    enabled_modules: list[str],
    resolve_config,
) -> list[GeneratedScaleSet]:
    """Parse pre-generated ARC scale sets across every ENABLED arc-runners* module.

    Multiple modules can deploy ARC runners — the canonical ``arc-runners`` plus
    per-GPU-arch variants (``arc-runners-b200``, ``arc-runners-h100``, ...). Each
    owns its own ``defs/`` and ``generated/`` dir; this fixture unions the
    generated files across the enabled ones so listener/placeholder ↔ scale-set
    coherence checks see the complete set deployed to the cluster. Per-org
    fan-out also emits one file per additional org (``<resource_slug>-<def>``);
    each becomes its own entry keyed by an org-unique scale-set label.

    The YAMLs are produced by ``just smoke``'s pre-generation loop, which invokes
    ``just generate-arc-runners`` once per enabled arc-runners* module before
    pytest starts. We do NOT regenerate here — under pytest-xdist multiple
    workers would race on the shared output directories. If you run pytest
    directly (outside ``just smoke``), run the recipe yourself first.

    Returns a list of :class:`GeneratedScaleSet` — the generated files are the
    source of truth for what the cluster deploys (see the class docstring).
    """
    prefix = resolve_config("arc-runners.runner_name_prefix", "")
    enabled_arc = arc_runners_module_names(upstream_dir) & set(enabled_modules)
    scale_sets = load_generated_scale_sets(upstream_dir, prefix, enabled_arc)
    if not scale_sets:
        pytest.fail(
            f"No generated ARC scale sets for modules {sorted(enabled_arc)}. "
            f"Run `just generate-arc-runners {cluster_id}` first (or invoke "
            f"`just smoke {cluster_id}`, which does it for you)."
        )
    return scale_sets
