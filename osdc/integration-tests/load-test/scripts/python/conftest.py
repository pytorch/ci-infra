"""Pytest conftest: add integration-tests/scripts/python to sys.path.

The load test modules import from the shared integration test directory
(run.py, phases.py). This conftest ensures those imports resolve during
test collection.
"""

import sys
from pathlib import Path

_INTEG_SCRIPTS = Path(__file__).resolve().parent.parent.parent.parent / "scripts" / "python"
if str(_INTEG_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_INTEG_SCRIPTS))
