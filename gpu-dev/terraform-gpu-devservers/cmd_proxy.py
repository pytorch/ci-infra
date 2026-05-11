#!/usr/bin/env python3
"""Local command proxy - runs shell commands and streams output via HTTP."""
import subprocess
import sys
from http.server import HTTPServer, BaseHTTPRequestHandler
import json
import os

os.environ["AWS_PROFILE"] = "admin"
os.environ["KUBECONFIG"] = os.path.expanduser("~/.kube/config")

class Handler(BaseHTTPRequestHandler):
    def do_POST(self):
        length = int(self.headers.get("Content-Length", 0))
        body = json.loads(self.rfile.read(length)) if length else {}
        cmd = body.get("cmd", "")
        if not cmd:
            self.send_response(400)
            self.end_headers()
            self.wfile.write(b"missing cmd")
            return

        self.send_response(200)
        self.send_header("Content-Type", "text/plain")
        self.end_headers()

        try:
            result = subprocess.run(
                cmd, shell=True, capture_output=True, text=True, timeout=120,
                env={**os.environ, "PATH": "/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin:" + os.environ.get("PATH", "")}
            )
            output = result.stdout
            if result.stderr:
                output += "\n--- stderr ---\n" + result.stderr
            output += f"\n--- exit code: {result.returncode} ---"
            self.wfile.write(output.encode())
        except subprocess.TimeoutExpired:
            self.wfile.write(b"TIMEOUT after 120s")
        except Exception as e:
            self.wfile.write(f"ERROR: {e}".encode())

    def log_message(self, format, *args):
        print(f"[cmd_proxy] {args[0]}")

if __name__ == "__main__":
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 9876
    print(f"[cmd_proxy] Listening on http://localhost:{port}")
    print(f"[cmd_proxy] AWS_PROFILE={os.environ.get('AWS_PROFILE')}")
    HTTPServer(("127.0.0.1", port), Handler).serve_forever()
