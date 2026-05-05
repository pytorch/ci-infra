"""arc-runners smoke test fixtures.

Reuses the shared smoke fixtures via star-import, then layers on arc-runners
specific fixtures (currently: regenerated runner YAMLs for the cluster under
test).
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest
import yaml
from smoke_conftest import *  # noqa: F403


@pytest.fixture(scope="session")
def generated_arc_runners(cluster_id: str, upstream_dir: Path) -> dict[str, dict]:
    """Regenerate ARC runner YAMLs for the cluster and return them parsed.

    Runs ``just generate-arc-runners <cluster>`` exactly once per pytest
    session (session scope) so the on-disk generated YAMLs match the cluster
    under test before any test reads them. This is the only legitimate way to
    get post-override values (e.g. ``force_proactive_capacity_zero`` flips
    ``CAPACITY_AWARE_PROACTIVE_CAPACITY`` to ``"0"`` on staging).

    Returns:
        Mapping ``def_name -> parsed first YAML document`` (the chart values
        block, which contains ``listenerTemplate``). The second YAML doc (the
        ConfigMap) is intentionally dropped — coherence tests only need the
        listener env block.
    """
    result = subprocess.run(
        ["just", "generate-arc-runners", cluster_id],
        cwd=str(upstream_dir),
        capture_output=True,
        text=True,
        timeout=300,
        check=False,
    )
    if result.returncode != 0:
        pytest.fail(
            f"`just generate-arc-runners {cluster_id}` failed (rc={result.returncode}):\n"
            f"--- stdout ---\n{result.stdout}\n--- stderr ---\n{result.stderr}"
        )

    generated_dir = upstream_dir / "modules" / "arc-runners" / "generated"
    out: dict[str, dict] = {}
    for yaml_file in sorted(generated_dir.glob("*.yaml")):
        # Each generated YAML is a multi-doc file: doc 1 = chart values (with
        # listenerTemplate), doc 2 = job-pod hook ConfigMap. Coherence tests
        # only consume doc 1; keep just that.
        docs = list(yaml.safe_load_all(yaml_file.read_text()))
        if not docs or not isinstance(docs[0], dict):
            pytest.fail(f"Generated YAML {yaml_file} has no parseable first document")
        out[yaml_file.stem] = docs[0]
    if not out:
        pytest.fail(f"No generated YAMLs found in {generated_dir} after running the recipe")
    return out
