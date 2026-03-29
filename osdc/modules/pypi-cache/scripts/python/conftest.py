"""Pytest conftest: ensure scripts/python is importable for cross-module imports."""

import sys
from pathlib import Path

# generate_manifests.py imports from instance_specs and analyze_node_utilization
# (in scripts/python/).  When pytest runs in this directory, scripts/python/
# isn't on sys.path.
_scripts_python = str(Path(__file__).resolve().parents[4] / "scripts" / "python")
if _scripts_python not in sys.path:
    sys.path.insert(0, _scripts_python)
