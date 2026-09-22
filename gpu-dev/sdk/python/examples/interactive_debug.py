"""Interactive debugging: reserve a GPU, poke around, inspect logs.

Use this in a Python REPL or Jupyter notebook for ad-hoc debugging.

    from gpu_dev import GpuDev
    client = GpuDev()
    exec(open("examples/interactive_debug.py").read())
"""
from gpu_dev import GpuDev

client = GpuDev()

# Show what's available
print("GPU Availability:")
for gpu, info in sorted(client.availability().items()):
    if info.total > 0:
        print(f"  {gpu:15s} {info.available:>3d}/{info.total} free")

# Show active reservations
print("\nActive reservations:")
for sb in client.list():
    print(f"  {sb.id[:8]}  {sb.gpu_count}x {sb.gpu_type:10s}  {sb.status.value:10s}  disk={sb.disk_name or '-'}")

# Show disks
print("\nDisks:")
for d in client.disks():
    status = "IN USE" if d.in_use else "free"
    print(f"  {d.name:20s}  {d.snapshot_count:>3d} snapshots  {status}")

# Reconnect to most recent active reservation
active = client.list(status=["active"])
if active:
    sb = active[0]
    print(f"\nReconnected to {sb.id[:8]} ({sb.gpu_count}x {sb.gpu_type})")
    print(f"  SSH: ssh {sb.pod_name}")
    print(f"  Disk: {sb.disk_name}")
    print(f"  Expires: {sb.expires_at}")

    # Quick health check
    result = sb.exec("nvidia-smi -L 2>&1 | head -4", timeout=5)
    if result.exit_code == 0:
        print(f"  GPU: {result.stdout.strip()}")
    else:
        print(f"  GPU check failed (exit {result.exit_code})")

    # Show setup logs
    print(f"\n  Setup log:")
    for entry in sb.logs():
        print(f"    [{entry['timestamp'][11:19]}] {entry['message'][:70]}")
else:
    print("\nNo active reservations")

# Look up a past reservation's logs
# client.search_logs("abc12345")
