import importlib.util
import os
import threading

import pytest


@pytest.fixture(scope="session")
def rpc_mod():
    """Load the RPC server module via importlib (filename has a dash)."""
    spec = importlib.util.spec_from_file_location(
        "rpc_server",
        os.path.join(os.path.dirname(__file__), "../../src/k8s/rpc-server.py"),
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture(autouse=True)
def _reset_state(rpc_mod):
    """Reset all module-level globals before each test."""
    rpc_mod._auth_token = None
    rpc_mod._job_id = None
    rpc_mod._job_status = "idle"
    rpc_mod._exit_code = None
    rpc_mod._process = None
    rpc_mod._last_heartbeat = None


@pytest.fixture()
def log_dir(rpc_mod, tmp_path):
    """Point LOG_DIR to a temp directory for the duration of the test."""
    orig = rpc_mod.LOG_DIR
    rpc_mod.LOG_DIR = str(tmp_path)
    yield tmp_path
    rpc_mod.LOG_DIR = orig


@pytest.fixture()
def server(rpc_mod):
    """Start an HTTP server on a random port; yield the base URL."""
    srv = rpc_mod.ThreadingHTTPServer(("127.0.0.1", 0), rpc_mod.Handler)
    port = srv.server_address[1]
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    yield f"http://127.0.0.1:{port}"
    srv.shutdown()
