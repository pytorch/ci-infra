"""arc-runners smoke test fixtures.

Reuses the shared smoke fixtures via star-import, then layers on arc-runners
specific fixtures (currently: parsed runner YAMLs for the cluster under
test).
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml
from smoke_conftest import *  # noqa: F403

# Submodules that delegate to arc-runners/deploy.sh with their own defs/
# (e.g. arc-runners-b200, arc-runners-h100). They share the upstream template
# but live under their own modules/ directory and emit YAMLs into their own
# generated/ dir. The fixture below regenerates each one separately so the
# coherence tests see ALL listener pods, not just the base arc-runners ones.
_ARC_RUNNERS_MODULE_PREFIX = "arc-runners"


@pytest.fixture(scope="session")
def generated_arc_runners(cluster_id: str, upstream_dir: Path) -> dict[str, dict]:
    """Parse pre-generated ARC runner YAMLs across every arc-runners* module.

    Multiple modules can deploy ARC runners — the canonical ``arc-runners``
    plus per-GPU-arch variants (``arc-runners-b200``, ``arc-runners-h100``,
    ...). Each variant owns its own ``defs/`` and ``generated/`` dir; this
    fixture unions YAMLs across all of them so listener-pod ↔ def coherence
    checks see the complete set deployed to the cluster.

    The YAMLs are produced by ``just smoke``'s pre-generation loop, which
    invokes ``just generate-arc-runners`` once per enabled arc-runners*
    module before pytest starts. We do NOT regenerate from inside this
    fixture — under pytest-xdist multiple workers would race on the shared
    output directories. If you're running pytest directly (outside
    ``just smoke``), run the recipe yourself for each variant first.

    Returns:
        Mapping ``def_name -> parsed first YAML document`` (the chart values
        block, which contains ``listenerTemplate``). The second YAML doc
        (the ConfigMap) is intentionally dropped — coherence tests only
        need the listener env block.
    """
    modules_dir = upstream_dir / "modules"
    generated_dirs = sorted(modules_dir.glob("arc-runners*/generated"))
    if not generated_dirs:
        pytest.fail(
            f"No arc-runners*/generated directories under {modules_dir}. "
            f"Run `just generate-arc-runners {cluster_id}` first (or invoke "
            f"`just smoke {cluster_id}`, which does it for you)."
        )

    out: dict[str, dict] = {}
    for generated_dir in generated_dirs:
        for yaml_file in sorted(generated_dir.glob("*.yaml")):
            # Each generated YAML is a multi-doc file: doc 1 = chart values
            # (with listenerTemplate), doc 2 = job-pod hook ConfigMap.
            # Coherence tests only consume doc 1; keep just that.
            docs = list(yaml.safe_load_all(yaml_file.read_text()))
            if not docs or not isinstance(docs[0], dict):
                pytest.fail(f"Generated YAML {yaml_file} has no parseable first document")
            out[yaml_file.stem] = docs[0]
    if not out:
        pytest.fail(
            f"No generated YAMLs found in {[str(d) for d in generated_dirs]}. "
            f"Run `just generate-arc-runners {cluster_id}` first."
        )
    return out
