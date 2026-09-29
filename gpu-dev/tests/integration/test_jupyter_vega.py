"""Integration: the Vega mime renderer stays disabled in the pod image.

JupyterLab still bundles a vega-functions below 6.1.1, so the image disables
and locks @jupyterlab/vega5-extension instead. Fails if a rebuild drops it.

reserve -> read page_config.json over SSH -> expect disabled AND locked.
Skipped unless --run-integration; the SSH step skips (not fails) when the runner
can't reach the pod or jupyter isn't on the image.
"""
import os

import pytest

from .conftest import reserved, exec_or_skip

pytestmark = pytest.mark.integration

# t4 by default: the check is image-level and t4 runs the same pod image
VEGA_TYPE = os.environ.get("GPU_DEV_VEGA_TYPE", "t4")
VEGA_GPUS = int(os.environ.get("GPU_DEV_VEGA_GPUS", "1"))

_EXT = "@jupyterlab/vega5-extension"

# Resolve the config dir via jupyter_core rather than hardcoding a sys_prefix path
# (it is /usr/etc/jupyter in this image, not /usr/local/etc/jupyter).
_CHECK = (
    "python3 -c 'import json, os\n"
    "from jupyter_core.paths import jupyter_config_path\n"
    f'e = "{_EXT}"\n'
    'c = [json.load(open(p)) for p in [os.path.join(d, "labconfig", "page_config.json")'
    " for d in jupyter_config_path()] if os.path.isfile(p)]\n"
    'print("VEGA_DISABLED=%d" % any(x.get("disabledExtensions", {}).get(e) for x in c))\n'
    'print("VEGA_LOCKED=%d" % any(x.get("lockedExtensions", {}).get(e) for x in c))\''
    " 2>&1 || echo NO_JUPYTER_CORE"
)


def test_vega5_renderer_disabled_and_locked(manager):
    with reserved(manager, gpu_type=VEGA_TYPE, gpu_count=VEGA_GPUS, hours=0.5) as (rid, conn):
        rc, probe = exec_or_skip(
            conn, "command -v jupyter >/dev/null 2>&1 && echo HAVE_JUPYTER || echo NO_JUPYTER")
        if "NO_JUPYTER" in probe:
            pytest.skip("jupyter not installed in the pod image")

        rc, out = exec_or_skip(conn, _CHECK, timeout=180)
        if "NO_JUPYTER_CORE" in out:
            pytest.skip(f"jupyter_core not importable by python3 in the pod: {out.strip()[:160]}")
        assert rc == 0, out
        assert "VEGA_DISABLED=1" in out, f"vega5 renderer not disabled — mitigation missing from the image: {out}"
        assert "VEGA_LOCKED=1" in out, f"vega5 renderer not locked — re-enablable from the UI: {out}"
