"""arc-runners smoke test fixtures.

Reuses the shared smoke fixtures via star-import, then layers on arc-runners
specific fixtures (currently: regenerated runner YAMLs for the cluster under
test).
"""

from __future__ import annotations

import os
import subprocess
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
def generated_arc_runners(
    cluster_id: str,
    upstream_dir: Path,
    enabled_modules: list[str],
) -> dict[str, dict]:
    """Regenerate ARC runner YAMLs for the cluster and return them parsed.

    Runs ``just generate-arc-runners <cluster>`` exactly once per enabled
    ``arc-runners*`` module (session scope). Per-class submodules
    (arc-runners-b200, arc-runners-h100, ...) reuse the same generator with
    ``ARC_RUNNERS_DEFS_DIR`` / ``ARC_RUNNERS_OUTPUT_DIR`` /
    ``ARC_RUNNERS_MODULE_NAME`` overrides so their listener pods are covered
    by the coherence tests too. Without this aggregation, listeners owned by
    GPU submodules look like "stale scale-sets" to the test.

    Returns:
        Mapping ``def_name -> parsed first YAML document`` (the chart values
        block, which contains ``listenerTemplate``). The second YAML doc (the
        ConfigMap) is intentionally dropped — coherence tests only need the
        listener env block. Def names are unique across modules.
    """
    arc_modules = [
        m for m in enabled_modules if m == _ARC_RUNNERS_MODULE_PREFIX or m.startswith(f"{_ARC_RUNNERS_MODULE_PREFIX}-")
    ]
    if not arc_modules:
        pytest.skip("no arc-runners* modules enabled for this cluster")

    out: dict[str, dict] = {}
    for module in arc_modules:
        module_dir = upstream_dir / "modules" / module
        if not module_dir.is_dir():
            # Consumer-only module not present in this checkout — skip rather
            # than fail; the live cluster may still have its listeners, but
            # we have no defs to validate them against here.
            continue
        defs_dir = module_dir / "defs"
        generated_dir = module_dir / "generated"
        env = {
            **os.environ,
            "ARC_RUNNERS_DEFS_DIR": str(defs_dir),
            "ARC_RUNNERS_OUTPUT_DIR": str(generated_dir),
            "ARC_RUNNERS_MODULE_NAME": module,
        }
        result = subprocess.run(
            ["just", "generate-arc-runners", cluster_id],
            cwd=str(upstream_dir),
            capture_output=True,
            text=True,
            timeout=300,
            check=False,
            env=env,
        )
        if result.returncode != 0:
            pytest.fail(
                f"`just generate-arc-runners {cluster_id}` failed for module {module!r} "
                f"(rc={result.returncode}):\n"
                f"--- stdout ---\n{result.stdout}\n--- stderr ---\n{result.stderr}"
            )

        for yaml_file in sorted(generated_dir.glob("*.yaml")):
            # Each generated YAML is a multi-doc file: doc 1 = chart values
            # (with listenerTemplate), doc 2 = job-pod hook ConfigMap. Coherence
            # tests only consume doc 1; keep just that.
            docs = list(yaml.safe_load_all(yaml_file.read_text()))
            if not docs or not isinstance(docs[0], dict):
                pytest.fail(f"Generated YAML {yaml_file} has no parseable first document")
            out[yaml_file.stem] = docs[0]

    if not out:
        pytest.fail(f"No generated YAMLs found across enabled arc-runners modules: {arc_modules}")
    return out
