#!/usr/bin/env python3
import argparse
import contextlib
import json
import os
import signal
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

LOG_DIR = "/tmp/rpc-logs"
PORT_FILE = "/tmp/rpc-server.port"
MAX_CHUNK = 1048576  # 1 MB

_lock = threading.Lock()
_auth_token = None
_job_id = None
_job_status = "idle"
_exit_code = None
_process = None
_last_heartbeat = None

def _check_auth(handler):
    if _auth_token is None:
        handler.send_error(403, "No auth token set")
        return False
    if handler.headers.get("X-Auth-Token") != _auth_token:
        handler.send_error(403, "Invalid auth token")
        return False
    return True

def _send_json(handler, data, status=200):
    body = json.dumps(data).encode()
    handler.send_response(status)
    handler.send_header("Content-Type", "application/json")
    handler.send_header("Content-Length", str(len(body)))
    handler.end_headers()
    handler.wfile.write(body)

def _read_body(handler):
    length = int(handler.headers.get("Content-Length", 0))
    return json.loads(handler.rfile.read(length)) if length else {}

def _wait_for_process(proc, job_id):
    global _job_status, _exit_code
    try:
        proc.wait()
        code = proc.returncode
    except Exception:
        code = -1
    with _lock:
        if _job_id == job_id:
            _exit_code = code
            _job_status = "completed" if code == 0 else "failed"


def _start_exec(job_id, script_path):
    """Open log files and spawn the subprocess. Called OUTSIDE the lock.

    Returns (proc, stdout_file, stderr_file) on success.
    Raises on failure (caller is responsible for cleanup).
    """
    stdout_file = open(os.path.join(LOG_DIR, f"{job_id}.stdout"), "wb")
    stderr_file = open(os.path.join(LOG_DIR, f"{job_id}.stderr"), "wb")
    try:
        proc = subprocess.Popen(
            ["sh", "-e", script_path],
            stdout=stdout_file, stderr=stderr_file, start_new_session=True,
        )
    except Exception:
        stdout_file.close()
        stderr_file.close()
        raise
    return proc, stdout_file, stderr_file

def _kill_process():
    global _job_status, _exit_code
    proc = _process
    if proc is None or proc.poll() is not None:
        return
    try:
        os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
    except OSError:
        return
    try:
        proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        with contextlib.suppress(OSError):
            os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
        proc.wait()
    with _lock:
        if _exit_code is None:
            _exit_code = -1
            _job_status = "failed"

class Handler(BaseHTTPRequestHandler):
    def log_request(self, code="-", size="-"):
        path = self.path.split("?")[0] if self.path else self.path
        if path == "/heartbeat":
            return
        print(f"{self.command} {path} -> {code}")

    def log_message(self, format, *args):
        pass

    def do_GET(self):
        path = self.path.split("?")[0]

        if path == "/health":
            _send_json(self, {"status": "ok"})
            return

        if not _check_auth(self):
            return

        if path == "/status":
            with _lock:
                data = {
                    "id": _job_id,
                    "status": _job_status,
                    "exit_code": _exit_code,
                }
            _send_json(self, data)
            return

        if path == "/logs":
            self._handle_logs()
            return

        self.send_error(404)

    def do_POST(self):
        path = self.path.split("?")[0]

        if path == "/exec":
            self._handle_exec()
            return

        if not _check_auth(self):
            return

        if path == "/heartbeat":
            global _last_heartbeat
            with _lock:
                _last_heartbeat = time.time()
            _send_json(self, {"status": "ok"})
            return

        self.send_error(404)

    def _handle_exec(self):
        global _last_heartbeat
        token = self.headers.get("X-Auth-Token")
        if not token:
            self.send_error(403, "Missing auth token")
            return

        body = _read_body(self)
        job_id = body.get("id")
        script_path = body.get("path")
        if not job_id or not script_path:
            self.send_error(400, "Missing id or path")
            return

        # Step 1: Under lock — validate auth, check state, set sentinel.
        with _lock:
            if token != _auth_token:
                self.send_error(403, "Invalid auth token")
                return
            if _job_status in ("running", "starting"):
                _send_json(self, {"error": "A job is already running"}, 409)
                return
            prev_status = _job_status
            # "starting" sentinel prevents concurrent execs while we release
            # the lock for I/O.
            _set_status("starting")

        # Step 2: Outside lock — open files, spawn process.
        try:
            proc, stdout_file, stderr_file = _start_exec(job_id, script_path)
        except Exception as exc:
            # Step 4: Re-acquire lock, revert sentinel.
            with _lock:
                _set_status(prev_status)
            _send_json(self, {"error": str(exc)}, 500)
            return

        # Step 3: Under lock — commit running state.
        with _lock:
            _set_globals(job_id, "running", None, proc)
            _last_heartbeat = time.time()

        threading.Thread(
            target=_wait_for_process, args=(proc, job_id), daemon=True,
        ).start()
        _send_json(self, {"id": job_id, "status": "running"})

    def _send_bytes(self, data, more_data=False):
        self.send_response(200)
        self.send_header("Content-Type", "application/octet-stream")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("X-More-Data", "true" if more_data else "false")
        self.end_headers()
        if data:
            self.wfile.write(data)

    def _handle_logs(self):
        offset = 0
        stream = "stdout"
        qs = self.path.split("?", 1)
        if len(qs) > 1:
            for p in qs[1].split("&"):
                if p.startswith("offset="):
                    with contextlib.suppress(ValueError):
                        offset = int(p.split("=", 1)[1])
                elif p.startswith("stream="):
                    val = p.split("=", 1)[1]
                    if val in ("stdout", "stderr"):
                        stream = val
        with _lock:
            job_id = _job_id
        if job_id is None:
            return self._send_bytes(b"")
        log_file = os.path.join(LOG_DIR, f"{job_id}.{stream}")
        try:
            with open(log_file, "rb") as f:
                f.seek(0, 2)
                size = f.tell()
                if offset >= size:
                    return self._send_bytes(b"")
                f.seek(offset)
                chunk = f.read(MAX_CHUNK)
                remaining = size - offset - len(chunk)
                self._send_bytes(chunk, more_data=(remaining > 0))
        except FileNotFoundError:
            self._send_bytes(b"")


def _set_status(status):
    """Set _job_status (must be called with _lock held)."""
    global _job_status
    _job_status = status


def _set_globals(job_id, status, exit_code, process):
    """Set all job-related globals (must be called with _lock held)."""
    global _job_id, _job_status, _exit_code, _process
    _job_id = job_id
    _job_status = status
    _exit_code = exit_code
    _process = process


def _heartbeat_watchdog():
    global _last_heartbeat
    while True:
        time.sleep(5)
        with _lock:
            last = _last_heartbeat
        if last is None:
            continue
        if time.time() - last > 60:
            print(
                "Heartbeat timeout: no heartbeat for 60s, killing current process"
            )
            _kill_process()
            with _lock:
                _last_heartbeat = None

def _signal_handler(signum, frame):
    _kill_process()
    sys.exit(0)

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=8080)
    parser.add_argument("--token", type=str, required=True)
    args = parser.parse_args()

    global _auth_token
    _auth_token = args.token

    os.makedirs(LOG_DIR, exist_ok=True)

    signal.signal(signal.SIGTERM, _signal_handler)
    signal.signal(signal.SIGINT, _signal_handler)

    watchdog = threading.Thread(target=_heartbeat_watchdog, daemon=True)
    watchdog.start()

    server = ThreadingHTTPServer(("0.0.0.0", args.port), Handler)
    actual_port = server.server_address[1]

    # Write the actual bound port so the deployer can discover it.
    # This is written AFTER bind+listen so the port is guaranteed usable.
    with open(PORT_FILE, "w") as f:
        f.write(str(actual_port))

    print(f"RPC server listening on 0.0.0.0:{actual_port}")
    server.serve_forever()


if __name__ == "__main__":
    main()
