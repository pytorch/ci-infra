# GPU Dev SDK

Python SDK for GPU development server reservations. Reserve GPU-powered development environments programmatically.

## Install

```bash
pip install gpu-dev-sdk
```

## Quick Start

```python
from gpu_dev import GpuDev

client = GpuDev()

# Reserve 2 H100 GPUs for 4 hours
sandbox = client.reserve(gpu_type="h100", gpu_count=2, hours=4)
print(f"SSH: {sandbox.ssh_command}")

# Execute commands
result = sandbox.exec("nvidia-smi")
print(result.stdout)

# Upload code and run training
sandbox.upload("./train.py", "/home/dev/train.py")
result = sandbox.exec("python /home/dev/train.py")

# Download results
sandbox.download("/home/dev/output/", "./results/")

# Clean up
sandbox.cancel()
```

## Context Manager

Automatically cancels the reservation when done:

```python
with client.reserve(gpu_type="t4") as sb:
    sb.exec("python train.py")
# reservation cancelled automatically
```

## Progress Tracking

```python
# Built-in progress logging
sandbox = client.reserve(gpu_type="h100", on_progress=True)
# [  1.5s] pending
# [  3.2s] preparing
# [  8.1s] 🚀 Container running
# [ 22.4s] Ready

# Custom callback
sandbox = client.reserve(
    gpu_type="h100",
    on_progress=lambda msg, t: print(f"⏳ [{t:.0f}s] {msg}")
)
```

## Available GPU Types

| Type | GPUs/node | Architecture |
|------|-----------|-------------|
| `h100` | 8 | Hopper |
| `h200` | 8 | Hopper |
| `b200` | 8 | Blackwell |
| `b300` | 8 | Blackwell |
| `a100` | 8 | Ampere |
| `t4` | 4 | Turing |
| `l4` | 4 | Ada Lovelace |
| `rtxpro6000` | 4 | Blackwell |

MIG slices: `h100-mig-1g`, `h100-mig-2g`, `h100-mig-3g`, `b200-mig-*`

## API Reference

### `GpuDev` — Client

```python
client = GpuDev()                           # Uses ~/.config/gpu-dev/config.json
client = GpuDev(GpuDevConfig(github_user="octocat"))  # Explicit config
```

| Method | Description |
|--------|-------------|
| `reserve(gpu_type, gpu_count, hours, ...)` | Reserve GPUs, returns `Sandbox` |
| `get(reservation_id)` | Get `Sandbox` for existing reservation |
| `list(status=[...])` | List reservations as `Sandbox` objects |
| `availability()` | GPU availability by type |
| `disks()` | List persistent disks |
| `search_logs(reservation_id)` | Get processing logs for any reservation |

### `Sandbox` — Reserved Environment

```python
sandbox = client.reserve(gpu_type="h100")
```

**Methods:**

| Method | Description |
|--------|-------------|
| `exec(command, timeout=None)` | Run shell command, returns `ExecResult` |
| `upload(local, remote)` | Upload file/directory via rsync |
| `download(remote, local)` | Download file/directory via rsync |
| `cancel()` | Cancel the reservation |
| `extend(hours)` | Extend duration |
| `refresh()` | Refresh status from server |
| `add_user(github_username)` | Grant SSH access to another user |
| `wait_until_ready(timeout, on_progress)` | Block until active |
| `logs()` | Get reservation processing log |
| `pod_logs(lines=50)` | Get container stdout via SSH |

**Properties:**

| Property | Description |
|----------|-------------|
| `id` | Reservation ID |
| `status` | Current status |
| `gpu_type` | GPU type |
| `gpu_count` | Number of GPUs |
| `ssh_command` | SSH command string |
| `pod_name` | SSH hostname |
| `fqdn` | Fully-qualified domain name |
| `is_active` | Whether ready for commands |
| `expires_at` | Expiration time |
| `disk_name` | Attached persistent disk |
| `instance_type` | EC2 instance type |
| `created_at` | Creation timestamp |
| `node_ip` | Node public IP |
| `detailed_status` | Detailed status message |
| `user_id` | Owner's user ID |

### `ExecResult`

```python
result = sandbox.exec("echo hello")
result.exit_code  # 0
result.stdout     # "hello\n"
result.stderr     # ""
```

## Logs & Debugging

```python
# Reservation processing log (what happened during setup)
for entry in sandbox.logs():
    print(f"[{entry['timestamp'][11:23]}] {entry['message']}")

# Look up logs for any reservation by ID prefix
for entry in client.search_logs("abc12345"):
    print(f"[{entry['timestamp'][11:23]}] {entry['message']}")

# Container stdout (via SSH)
print(sandbox.pod_logs(lines=20))
```

## Spot Instances

Use spot instances for lower cost (may be preempted):

```python
sandbox = client.reserve(gpu_type="h100", spot=True, hours=2)
```

## Persistent Disks

Data persists across reservations when using named disks:

```python
# First session
sb = client.reserve(gpu_type="h100", disk_name="my-project")
sb.exec("pip install torch && echo done")
sb.cancel()

# Later session — packages still installed
sb = client.reserve(gpu_type="h100", disk_name="my-project")
sb.exec("python -c 'import torch; print(torch.__version__)'")
```

## Jupyter

```python
sb = client.reserve(gpu_type="t4", jupyter=True)
print(f"Jupyter: {sb.jupyter_url}")
```

## Configuration

The SDK reads `~/.config/gpu-dev/config.json` (shared with the CLI):

```json
{
  "github_user": "your-github-username",
  "environment": "prod"
}
```

Or configure programmatically:

```python
from gpu_dev import GpuDev, GpuDevConfig

client = GpuDev(GpuDevConfig(
    github_user="octocat",
    environment="prod",
    default_timeout_minutes=15,
))
```

## Error Handling

```python
from gpu_dev import (
    GpuDevError,           # Base error
    GpuDevAuthError,       # Authentication failed
    GpuDevNotFoundError,   # Reservation not found
    GpuDevTimeoutError,    # Operation timed out
    GpuDevValidationError, # Invalid parameters
    GpuDevConnectionError, # SSH connection failed
    GpuDevCapacityError,   # No GPUs available
)

try:
    sandbox = client.reserve(gpu_type="h100", gpu_count=16)
except GpuDevValidationError as e:
    print(f"Invalid request: {e}")
except GpuDevTimeoutError:
    print("Reservation timed out — GPUs may be busy")
```

Credentials are cached to disk (45-min TTL) and auto-refreshed on expiry — no manual re-auth needed in long-running notebooks.

## Interactive Notebook

See [examples/quickstart.ipynb](examples/quickstart.ipynb) for a hands-on walkthrough.
