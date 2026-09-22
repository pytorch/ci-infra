"""Run tests on a GPU server with a persistent disk snapshot.

Loads a pre-configured environment from a named disk and runs
a test suite — useful for CI or interactive debugging.

Usage:
    python run_tests.py
    python run_tests.py --branch feature/my-fix
"""
import sys

from gpu_dev import GpuDev, GpuDevTimeoutError

branch = sys.argv[1] if len(sys.argv) > 1 else "main"
client = GpuDev()

print(f"Reserving H100 with 'pytorch-dev' disk (branch: {branch})...")

try:
    sb = client.reserve(
        gpu_type="h100",
        gpu_count=1,
        hours=2,
        disk_name="pytorch-dev",       # pre-compiled PyTorch environment
        name=f"test-{branch[:20]}",
        on_progress=True,
    )
except GpuDevTimeoutError:
    print("No GPU capacity available — try again later or use spot")
    sys.exit(1)

print(f"\nRunning on {sb.pod_name} ({sb.instance_type})")

# Pull latest code
result = sb.exec(f"""
    cd /home/dev/pytorch && \
    git fetch origin && \
    git checkout {branch} && \
    git pull origin {branch}
""", timeout=120)
print(result.stdout[-200:] if result.stdout else "(no output)")

if result.exit_code != 0:
    print(f"Git checkout failed: {result.stderr}")
    sb.cancel()
    sys.exit(1)

# Run tests
print(f"\nRunning tests on {branch}...")
result = sb.exec(
    "cd /home/dev/pytorch && python test/run_test.py test_torch 2>&1 | tail -30",
    timeout=1800,
)
print(result.stdout)

# Show timing from reservation logs
print("\nReservation timeline:")
for entry in sb.logs():
    print(f"  [{entry['timestamp'][11:23]}] {entry['message'][:80]}")

exit_code = result.exit_code
sb.cancel()
print(f"\nTests {'PASSED' if exit_code == 0 else 'FAILED'} (exit {exit_code})")
sys.exit(exit_code)
