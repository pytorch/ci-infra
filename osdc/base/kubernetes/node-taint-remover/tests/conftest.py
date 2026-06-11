"""Make the sibling ``lib/`` directory importable for these tests.

The library file lives in ``lib/taint_remover.py`` so it can be IDE-friendly
and shipped via deploy.sh, but pytest's default rootdir insertion only puts
this ``tests/`` directory on ``sys.path``. Add the sibling explicitly.
"""

import sys
from pathlib import Path

_LIB = Path(__file__).resolve().parent.parent / "lib"
if str(_LIB) not in sys.path:
    sys.path.insert(0, str(_LIB))
