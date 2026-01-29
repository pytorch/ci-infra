# ODC User Guide

**Open Developer Cloud** - High-Performance GPU Development Platform

---

## Table of Contents

- [Quick Start](#quick-start)
- [Installation](#installation)
- [Making a Reservation](#making-a-reservation)
- [SSH Access](#ssh-access)
- [IDE Integration](#ide-integration)
- [Jupyter Lab](#jupyter-lab)
- [Claude Code](#claude-code)
- [Multinode Reservations](#multinode-reservations)
- [Persistent Storage](#persistent-storage)
- [CPU and Memory Allocation](#cpu-and-memory-allocation)
- [CUDA Versions](#cuda-versions)
- [Shared ccache](#shared-ccache)
- [Custom Docker Images](#custom-docker-images)
- [GPU Profiling (Nsight)](#gpu-profiling-nsight)
- [Managing Reservations](#managing-reservations)
- [Collaboration](#collaboration)
- [Architecture Overview](#architecture-overview)
- [GPU Types Reference](#gpu-types-reference)
- [Troubleshooting](#troubleshooting)
- [FAQ](#faq)

---

## Quick Start

```bash
# 1. Install the CLI
pip install "git+https://github.com/wdvr/osdc.git@release"

# 2. Configure your GitHub username (for SSH key authentication)
gpu-dev config set github_user your-github-username

# 3. Check GPU availability
gpu-dev avail

# 4. Reserve GPUs (interactive mode)
gpu-dev reserve

# 5. Connect to your reservation
gpu-dev connect
```

---

## Installation

### Prerequisites

- Python 3.8+
- AWS credentials configured
- GitHub account with SSH keys

### Install the CLI

```bash
# Install from GitHub (recommended)
pip install "git+https://github.com/wdvr/osdc.git@release"

# Or install from a local clone
git clone https://github.com/pytorch/odc.git
cd odc
pip install -e .
```

### Configure AWS Credentials

The CLI uses your AWS credentials. Configure via any of these methods:

```bash
# Option 1: AWS CLI configuration
aws configure

# Option 2: Environment variables
export AWS_ACCESS_KEY_ID=your-key
export AWS_SECRET_ACCESS_KEY=your-secret
export AWS_REGION=us-east-2

# Option 3: AWS SSO
aws sso login --profile your-profile
```

### Initial Setup

```bash
# Set your GitHub username (required for SSH authentication)
gpu-dev config set github_user your-github-username

# Verify configuration
gpu-dev config show

# Enable SSH config auto-include (recommended)
gpu-dev config ssh-include enable
```

---

## Making a Reservation

### Interactive Mode (Recommended for First-Time Users)

Simply run `gpu-dev reserve` without arguments to be guided through the process:

```bash
gpu-dev reserve
```

You'll be prompted to select:
1. GPU type (H100, B200, A100, etc.)
2. Number of GPUs (1, 2, 4, 8, or more)
3. Duration (in hours)
4. Persistent disk (create new, use existing, or none)
5. Jupyter Lab (enable/disable)

### Command-Line Mode

For quick reservations with known parameters:

```bash
# Reserve 4 H100 GPUs for 8 hours
gpu-dev reserve --gpu-type h100 --gpus 4 --hours 8

# With Jupyter Lab enabled
gpu-dev reserve -t h100 -g 4 -h 8 --jupyter

# Using a specific persistent disk
gpu-dev reserve -t h100 -g 4 -h 8 --disk my-project

# Temporary storage only (no persistent disk)
gpu-dev reserve -t h100 -g 4 -h 8 --disk none
```

### Reservation Options

| Option | Short | Description |
|--------|-------|-------------|
| `--gpu-type` | `-t` | GPU type: `b200`, `h200`, `h100`, `a100`, `a10g`, `l4`, `t4`, `t4-small`, `cpu-arm`, `cpu-x86` |
| `--gpus` | `-g` | Number of GPUs (1, 2, 4, 8, 12, 16, 20, 24, 32, 40, 48) |
| `--hours` | `-h` | Duration in hours (0.0833 to 24, supports decimals like 0.5 for 30 minutes) |
| `--name` | `-n` | Optional name for your reservation |
| `--jupyter` | | Enable Jupyter Lab access |
| `--disk` | | Persistent disk name, or `none` for temporary |
| `--distributed` | `-d` | Required for multinode reservations (>8 GPUs) |
| `--dockerfile` | | Path to custom Dockerfile |
| `--dockerimage` | | Custom Docker image URL |

### Check Availability Before Reserving

```bash
# See available GPUs by type
gpu-dev avail

# Watch availability in real-time
gpu-dev avail --watch
```

---

## SSH Access

### How SSH Authentication Works

ODC uses your GitHub SSH keys for authentication:

1. When you reserve, ODC fetches your public SSH keys from `https://github.com/<username>.keys`
2. These keys are injected into the pod's `~/.ssh/authorized_keys`
3. You can then SSH using any of your GitHub-associated SSH keys

**Important**: Make sure you have SSH keys configured on your GitHub account.

### Connecting to Your Reservation

The easiest way to connect:

```bash
# Auto-connects to your active reservation
gpu-dev connect

# Connect to a specific reservation (use 8-char prefix)
gpu-dev connect abc12345
```

### SSH Config Files

Each reservation creates an SSH config file at:
```
~/.gpu-dev/<reservation-id>-sshconfig
```

If you've enabled SSH config auto-include (`gpu-dev config ssh-include enable`), you can connect directly:

```bash
ssh <pod-name>
```

### Manual SSH Connection

You can also get the SSH command from reservation details:

```bash
gpu-dev show

# Output includes:
# SSH Command: ssh dev@<node-ip> -p <nodeport>
```

### SSH Port Forwarding

Port forwarding works as expected. This is useful for accessing services running on your reservation:

```bash
# Forward local port 8888 to remote port 8888 (e.g., for Jupyter)
ssh -L 8888:localhost:8888 <pod-name>

# Forward local port 6006 to remote port 6006 (e.g., for TensorBoard)
ssh -L 6006:localhost:6006 <pod-name>

# Multiple port forwards
ssh -L 8888:localhost:8888 -L 6006:localhost:6006 <pod-name>
```

### SSH Agent Forwarding

To use your local SSH keys on the server (e.g., for git operations):

```bash
# With -A flag
ssh -A <pod-name>

# Or use gpu-dev connect (includes -A by default)
gpu-dev connect
```

This lets you push/pull from GitHub on the server using your local SSH keys.

---

## IDE Integration

### VS Code Remote SSH

**Option 1: Command line**
```bash
code --remote ssh-remote+<pod-name> /home/dev
```

**Option 2: VS Code UI**
1. Install the "Remote - SSH" extension
2. Press `F1` → "Remote-SSH: Connect to Host..."
3. Select your pod from the list (requires SSH config auto-include enabled)
4. Open `/home/dev` as your workspace

**Option 3: Clickable link**
```bash
gpu-dev show
# Click the VS Code link in the output
```

### Cursor IDE

Cursor uses the same SSH configuration as VS Code:

1. Enable SSH config auto-include: `gpu-dev config ssh-include enable`
2. Open Cursor → Remote SSH
3. Select your pod from the list
4. Your reservation appears with the pod name

### SSH Config for IDEs

When you enable SSH config auto-include, it adds this line to `~/.ssh/config` and `~/.cursor/ssh_config`:

```
Include ~/.gpu-dev/*-sshconfig
```

This makes all your active reservations appear in your IDE's SSH host list automatically.

---

## Jupyter Lab

### Enable Jupyter Lab

**During reservation:**
```bash
gpu-dev reserve --jupyter -t h100 -g 4 -h 8
```

**On an existing reservation:**
```bash
gpu-dev edit <reservation-id> --enable-jupyter
```

### Access Jupyter Lab

After enabling, get the URL:

```bash
gpu-dev show <reservation-id>

# Output includes:
# Jupyter URL: http://<node-ip>:<port>
```

Open this URL in your browser. No password is required.

### Using Claude in Jupyter Lab

Claude Code can be used directly within Jupyter notebooks:

1. Connect to your reservation via Jupyter Lab
2. Open a terminal in Jupyter Lab
3. Run `claude` to start Claude Code CLI
4. Or use the Claude extension if installed

### Pre-installed Jupyter Packages

The default image includes:
- JupyterLab
- ipywidgets
- matplotlib, seaborn, plotly
- pandas, numpy
- tensorboard

---

## Claude Code

Claude Code CLI is pre-installed on all reservations.

### Starting Claude Code

```bash
# SSH to your reservation
gpu-dev connect

# Start Claude Code
claude
```

### Using Claude Code with Your Project

```bash
# Navigate to your project
cd /home/dev/my-project

# Start Claude Code in the project directory
claude

# Or start with a specific task
claude "help me debug this training script"
```

### Claude Code Features on GPU Servers

- Full access to GPU resources for running/testing code
- Can execute CUDA programs and PyTorch scripts
- Access to all installed tools (git, python, nvcc, etc.)
- Persistent workspace in `/home/dev`

---

## Multinode Reservations

For distributed training across multiple GPU nodes.

### Creating a Multinode Reservation

```bash
# 16 H100 GPUs across 2 nodes (8 GPUs per node)
gpu-dev reserve -t h100 -g 16 --distributed -h 12

# 24 H100 GPUs across 3 nodes
gpu-dev reserve -t h100 -g 24 --distributed -h 12

# 32 GPUs across 4 nodes
gpu-dev reserve -t h100 -g 32 --distributed -h 12
```

**Requirements:**
- GPU count must be a multiple of GPUs-per-node (8 for H100/B200/H200/A100)
- `--distributed` flag is required for >8 GPUs

### What You Get

Each node in a multinode reservation gets:
- A separate pod with its own hostname
- Full network connectivity to other nodes
- Access to the shared network storage
- EFA (Elastic Fabric Adapter) networking enabled

### Network Architecture

```
┌─────────────────────────────────────────────────────────────┐
│                    Kubernetes Cluster                        │
│  ┌─────────────┐    ┌─────────────┐    ┌─────────────┐      │
│  │   Pod 0     │    │   Pod 1     │    │   Pod 2     │      │
│  │  (8 GPUs)   │◄──►│  (8 GPUs)   │◄──►│  (8 GPUs)   │      │
│  │  MASTER     │    │  WORKER     │    │  WORKER     │      │
│  └──────┬──────┘    └──────┬──────┘    └──────┬──────┘      │
│         │                  │                  │              │
│         └────────────┬─────┴─────┬────────────┘              │
│                      │           │                           │
│               ┌──────┴───┐ ┌─────┴────┐                      │
│               │   EFA    │ │  Shared  │                      │
│               │ Network  │ │ Storage  │                      │
│               │(3200Gbps)│ │  (EFS)   │                      │
│               └──────────┘ └──────────┘                      │
└─────────────────────────────────────────────────────────────┘
```

### EFA Networking

EFA (Elastic Fabric Adapter) provides:
- **3200 Gbps** bandwidth on p5.48xlarge (H100) instances
- RDMA (Remote Direct Memory Access) support
- Kernel bypass for low-latency communication
- Native integration with NCCL for PyTorch distributed training

**Important**: EFA only works within the same Availability Zone. All nodes in a multinode reservation are placed in the same AZ.

### Pod Hostnames

Pods are accessible via DNS within the cluster:
```
<podname>-0.gpu-dev-headless.gpu-dev.svc.cluster.local
<podname>-1.gpu-dev-headless.gpu-dev.svc.cluster.local
...
```

### Running Distributed Training

**Example: PyTorch Distributed Data Parallel**

On the master node (Pod 0):
```bash
export MASTER_ADDR=$(hostname -I | awk '{print $1}')
export MASTER_PORT=29500
export WORLD_SIZE=2  # Number of nodes
export RANK=0

torchrun --nproc_per_node=8 \
         --nnodes=$WORLD_SIZE \
         --node_rank=$RANK \
         --master_addr=$MASTER_ADDR \
         --master_port=$MASTER_PORT \
         train.py
```

On worker nodes (Pod 1, 2, ...):
```bash
export MASTER_ADDR=<master-pod-ip>  # Get from Pod 0
export MASTER_PORT=29500
export WORLD_SIZE=2
export RANK=1  # Increment for each worker

torchrun --nproc_per_node=8 \
         --nnodes=$WORLD_SIZE \
         --node_rank=$RANK \
         --master_addr=$MASTER_ADDR \
         --master_port=$MASTER_PORT \
         train.py
```

### Performance Expectations

| Configuration | Total GPUs | Expected NCCL Bandwidth |
|--------------|------------|------------------------|
| Single node (NVLink) | 8 | ~450 GB/s |
| 2 nodes (EFA) | 16 | ~50-100 GB/s inter-node |
| 4 nodes (EFA) | 32 | ~50-100 GB/s inter-node |

**Note**: Intra-node communication uses NVLink (450 GB/s). Inter-node uses EFA (up to 400 Gbps / 50 GB/s per direction).

---

## Persistent Storage

ODC provides three types of storage:

```
┌────────────────────────────────────────────────────────────┐
│                    Storage Architecture                     │
├────────────────────────────────────────────────────────────┤
│                                                            │
│  /home/dev          - Persistent EBS disk (100GB)          │
│  │                    Your personal workspace               │
│  │                    Survives reservation expiry           │
│  │                    Automatic snapshots                   │
│  │                                                         │
│  /shared-personal   - Personal EFS storage (elastic)       │
│  │                    Large files, datasets                 │
│  │                    Shared across ALL your reservations   │
│  │                                                         │
│  /ccache_shared       - Shared ccache (all users)          │
│                         Compiler cache for faster builds   │
│                                                            │
└────────────────────────────────────────────────────────────┘
```

### Persistent Disk (/home/dev)

Your home directory is backed by a named persistent disk (EBS volume).

**Key characteristics:**
- **Size**: 100GB per disk
- **Performance**: Fast SSD storage (EBS gp3)
- **Snapshots**: Automatically taken when reservation ends
- **Multiple disks**: You can have multiple named disks for different projects

**Managing disks:**

```bash
# List your disks
gpu-dev disk list

# Create a new disk
gpu-dev disk create my-project

# Use a specific disk in a reservation
gpu-dev reserve --disk my-project -t h100 -g 4 -h 8

# View contents of a disk (from its snapshot)
gpu-dev disk list-content my-project

# Rename a disk
gpu-dev disk rename old-name new-name

# Delete a disk (snapshots kept for 30 days)
gpu-dev disk delete my-project
```

**When to use persistent disk:**
- Development work and code
- Virtual environments
- Smaller datasets
- Configuration files

### Shared Personal Storage (/shared-personal)

EFS-backed storage that persists across all your reservations.

**Key characteristics:**
- **Size**: Elastic (pay for what you use)
- **Performance**: Good for sequential reads/writes, higher latency than EBS
- **Sharing**: Personal to you, but accessible from any of your reservations

**When to use /shared-personal:**
- Large datasets
- Model checkpoints
- Files needed across multiple reservations
- Long-term storage

```bash
# Example: Store a large dataset
cp -r /path/to/dataset /shared-personal/datasets/

# Access from any reservation
ls /shared-personal/datasets/
```

### Performance Comparison

| Storage | Read Speed | Write Speed | Latency | Best For |
|---------|-----------|-------------|---------|----------|
| `/home/dev` (EBS) | ~500 MB/s | ~500 MB/s | <1ms | Code, venvs, active work |
| `/shared-personal` (EFS) | ~100 MB/s | ~50 MB/s | ~10ms | Large datasets, checkpoints |
| `/ccache_shared` (EFS) | ~100 MB/s | ~50 MB/s | ~10ms | Compiler cache |

### Temporary Storage

If you don't need persistent storage:

```bash
gpu-dev reserve --disk none -t h100 -g 4 -h 2
# or
gpu-dev reserve --no-persist -t h100 -g 4 -h 2
```

With temporary storage:
- `/home/dev` uses ephemeral container storage
- Data is lost when reservation ends
- Good for quick experiments or CI-like workflows

---

## CPU and Memory Allocation

CPU and memory are allocated proportionally based on the number of GPUs you reserve.

### How It Works

Each node has fixed CPU and memory resources. When you reserve GPUs, you get a proportional share:

```
Your Resources = (Your GPUs / Total Node GPUs) × Node Resources
```

**Example for H100 (p5.48xlarge):**
- Node total: 192 vCPUs, 2048 GB RAM
- 1 GPU (1/8 of node): 24 vCPUs, 256 GB RAM
- 4 GPUs (4/8 of node): 96 vCPUs, 1024 GB RAM
- 8 GPUs (full node): 192 vCPUs, 2048 GB RAM

### Resource Allocation by GPU Type

| GPU Type | GPUs/Node | Node CPUs | Node RAM | Per-GPU CPU | Per-GPU RAM |
|----------|-----------|-----------|----------|-------------|-------------|
| B200 | 8 | 192 | 2048 GB | 24 | 256 GB |
| H200 | 8 | 192 | 2048 GB | 24 | 256 GB |
| H100 | 8 | 192 | 2048 GB | 24 | 256 GB |
| A100 | 8 | 96 | 1152 GB | 12 | 144 GB |
| A10G | 4 | 48 | 192 GB | 12 | 48 GB |
| L4 | 4 | 48 | 192 GB | 12 | 48 GB |
| T4 | 4 | 48 | 192 GB | 12 | 48 GB |

### Burst Capacity

ODC uses Kubernetes "Burstable" QoS class:
- **Requests**: 90% of proportional allocation (guaranteed minimum)
- **Limits**: 150% of proportional allocation (burst ceiling)

This means you can temporarily use more CPU if the node has spare capacity.

### Checking Your Resources

Inside your reservation:

```bash
# Check CPU allocation
nproc

# Check memory
free -h

# Check GPU memory
nvidia-smi
```

---

## CUDA Versions

### Available CUDA Versions

The default image includes multiple CUDA versions:

| Version | Path | Notes |
|---------|------|-------|
| CUDA 12.8 | `/usr/local/cuda-12.8` | Default, aliased to `/usr/local/cuda` |
| CUDA 13.0 | `/usr/local/cuda-13.0` | Latest features |

### Checking Active CUDA Version

```bash
# Check nvcc version
nvcc --version

# Check CUDA_HOME
echo $CUDA_HOME

# Check nvidia-smi driver version
nvidia-smi
```

### Switching CUDA Versions

```bash
# Use CUDA 12.8 (default)
export CUDA_HOME=/usr/local/cuda-12.8
export PATH=$CUDA_HOME/bin:$PATH
export LD_LIBRARY_PATH=$CUDA_HOME/lib64:$LD_LIBRARY_PATH

# Use CUDA 13.0
export CUDA_HOME=/usr/local/cuda-13.0
export PATH=$CUDA_HOME/bin:$PATH
export LD_LIBRARY_PATH=$CUDA_HOME/lib64:$LD_LIBRARY_PATH
```

### Environment Variables

These are pre-set in your environment:

```bash
CUDA_12_PATH=/usr/local/cuda-12.8
CUDA_13_PATH=/usr/local/cuda-13.0
CUDA_HOME=/usr/local/cuda-12.8  # Default
```

### Verifying CUDA Works

```bash
# Simple test
python -c "import torch; print(torch.cuda.is_available())"

# Check CUDA version PyTorch sees
python -c "import torch; print(torch.version.cuda)"

# Run CUDA sample
nvidia-smi
```

---

## Shared ccache

ccache is a compiler cache that speeds up recompilation.

### How It Works

ODC provides a shared ccache directory at `/ccache_shared` that is:
- Shared across **all users**
- Persistent across reservations
- Pre-configured in your environment

### Environment Setup

Already configured in your shell:

```bash
echo $CCACHE_DIR
# /ccache_shared
```

### Benefits

When building PyTorch or other C++ projects:
- First build: Normal compilation time
- Subsequent builds: Much faster (cache hits)
- Benefits from other users' cache entries too

### Usage

ccache works automatically with most build systems:

```bash
# Building PyTorch (ccache used automatically)
cd pytorch
python setup.py develop

# Check cache stats
ccache -s

# Check cache hit rate
ccache -s | grep "cache hit"
```

### Cache Size

The shared cache has ample space. If you need to clear your contribution:

```bash
# Show stats
ccache -s

# Clear only if necessary (affects all users)
# ccache -C  # DON'T run unless needed
```

---

## Custom Docker Images

### Using a Pre-built Image

```bash
gpu-dev reserve --dockerimage pytorch/pytorch:2.3.0-cuda12.1-cudnn8-devel -t h100 -g 4 -h 8
```

**Requirements for custom images:**
- Must be accessible from the cluster (public or accessible registry)
- SSH server support (for remote access)

### Using a Custom Dockerfile

```bash
gpu-dev reserve --dockerfile ./Dockerfile -t h100 -g 4 -h 8
```

**Limitations:**
- Dockerfile max size: 512KB
- Build context max: ~700KB compressed
- Build happens at reservation time (adds to startup time)

**Example Dockerfile:**

```dockerfile
FROM pytorch/pytorch:2.3.0-cuda12.1-cudnn8-devel

# Install additional Python packages
RUN pip install transformers datasets accelerate einops

# Install system packages
RUN apt-get update && apt-get install -y \
    tmux \
    htop \
    && rm -rf /var/lib/apt/lists/*

# Install your project dependencies
COPY requirements.txt /tmp/
RUN pip install -r /tmp/requirements.txt
```

### Preserving Container Entrypoint

By default, ODC overrides the container's entrypoint to run an SSH server. To keep the original:

```bash
gpu-dev reserve --dockerimage myimage:latest --preserve-entrypoint -t h100 -g 4 -h 8
```

**Use case**: Running specific workloads that need the original entrypoint behavior.

### Default Image Contents

If you don't specify a custom image, you get:

**Base**: `pytorch/pytorch:2.9.1-cuda12.8-cudnn9-devel`

**Pre-installed:**
- PyTorch 2.9.1 with CUDA 12.8
- JupyterLab, matplotlib, pandas, numpy
- zsh with oh-my-zsh (default shell)
- vim, nano, tmux, htop, git
- Claude Code CLI
- Node.js 20

### Shell Customization

Your shell configuration is stored on your persistent disk and survives across reservations.

**How it works:**
1. On first reservation with a new disk (or with `--recreate-env`), shell configs are copied from the base image to `/home/dev`
2. These files are then yours to customize
3. Your customizations persist on the persistent disk

**Customizable files:**
- `~/.zshrc` - zsh configuration (default shell)
- `~/.bashrc` - bash configuration
- `~/.zprofile`, `~/.bash_profile` - login shell configs
- `~/.oh-my-zsh/` - oh-my-zsh themes and plugins

**Example customizations:**
```bash
# Add custom aliases to .zshrc
echo 'alias gs="git status"' >> ~/.zshrc

# Change oh-my-zsh theme
sed -i 's/ZSH_THEME="robbyrussell"/ZSH_THEME="agnoster"/' ~/.zshrc

# Add a plugin
sed -i 's/plugins=(git)/plugins=(git docker python)/' ~/.zshrc
```

**Reset shell environment:**
If you want to start fresh with default shell configs:
```bash
gpu-dev reserve --disk my-disk --recreate-env
```

---

## GPU Profiling (Nsight)

For detailed GPU performance analysis using NVIDIA Nsight tools.

### Request a Profiling Node

```bash
gpu-dev reserve -t h100 -g 8 --node-label nsight=true -h 8
```

**Why dedicated nodes?**
- NVIDIA DCGM (monitoring) conflicts with Nsight profiling
- Profiling-dedicated nodes have DCGM disabled
- One node per GPU type is reserved for profiling

### Available Profiling Tools

**Nsight Compute (ncu)** - Kernel-level profiling:
```bash
# Profile a CUDA application
ncu --target-processes all python train.py

# Profile specific kernels
ncu --kernel-name "your_kernel" ./your_app
```

**Nsight Systems (nsys)** - System-wide profiling:
```bash
# Capture a system trace
nsys profile python train.py

# With specific options
nsys profile --trace=cuda,nvtx,osrt python train.py
```

### What's Enabled for Profiling

When using `--node-label nsight=true`:
- `CAP_SYS_ADMIN` Linux capability (required for Nsight)
- Node-level setting: `NVreg_RestrictProfilingToAdminUsers=0`
- Full access to GPU performance counters

### Example Profiling Workflow

```bash
# 1. Reserve a profiling node
gpu-dev reserve -t h100 -g 8 --node-label nsight=true -h 4

# 2. Connect
gpu-dev connect

# 3. Run Nsight Systems
nsys profile -o my_trace python train.py

# 4. Run Nsight Compute for kernel analysis
ncu -o my_kernel_analysis python train.py

# 5. Download results for analysis in Nsight GUI
# (from your local machine)
scp <pod-name>:/home/dev/my_trace.nsys-rep ./
scp <pod-name>:/home/dev/my_kernel_analysis.ncu-rep ./
```

---

## Managing Reservations

### List Reservations

```bash
# Your active reservations
gpu-dev list

# All your reservations (including expired)
gpu-dev list --all

# All users' reservations
gpu-dev list --user all

# Watch mode (refreshes every 2 seconds)
gpu-dev list --watch

# Filter by status
gpu-dev list --status active
gpu-dev list --status queued
```

### Show Reservation Details

```bash
# Show your active reservation
gpu-dev show

# Show specific reservation (8-char prefix works)
gpu-dev show abc12345
```

Output includes:
- Status and expiration time
- SSH command and Jupyter URL
- Pod name and node IP
- Storage information
- Queue position (if queued)

### Extend a Reservation

```bash
# Interactive extension
gpu-dev edit <reservation-id> --extend

# You'll be prompted to enter additional hours
```

**Limits:**
- Maximum initial duration: 24 hours
- Maximum extension: 24 additional hours
- Total maximum: 48 hours

### Cancel a Reservation

```bash
# Cancel specific reservation
gpu-dev cancel abc12345

# Interactive selection
gpu-dev cancel

# Cancel all your reservations
gpu-dev cancel --all
```

### Expiry Warnings

You'll receive warnings before your reservation expires through multiple channels:

**MOTD Banner**: When you SSH into your reservation, you'll see an expiry warning banner at the top of your terminal (Message of the Day). This is displayed automatically on each login.

**Warning files** appear in your home directory:
```
~/WARN_EXPIRES_IN_30MIN.txt
~/WARN_EXPIRES_IN_15MIN.txt
~/WARN_EXPIRES_IN_5MIN.txt
```

**Wall messages**: Broadcast messages sent to all terminals.

**Timeline:**
- **30 minutes** before: Warning file created
- **15 minutes** before: Warning file + wall message
- **5 minutes** before: Warning file + wall message

**Tip**: You can extend your reservation before it expires using `gpu-dev edit <id> --extend`.

---

## Collaboration

### Add a Collaborator

Give another user SSH access to your reservation:

```bash
gpu-dev edit <reservation-id> --add-user colleague-github-username
```

**How it works:**
1. Fetches the target user's SSH keys from GitHub
2. Adds those keys to the pod's `~/.ssh/authorized_keys`
3. They can now SSH using their own keys

**The collaborator connects using:**
```bash
ssh dev@<node-ip> -p <nodeport>
```

### Limitations

- Both users must have GitHub SSH keys configured
- Only the reservation owner can manage collaborators
- Collaborators have full access (same as owner)

---

## Architecture Overview

### System Components

```
┌─────────────────────────────────────────────────────────────────┐
│                        ODC Architecture                          │
├─────────────────────────────────────────────────────────────────┤
│                                                                  │
│   ┌─────────────┐                                               │
│   │   gpu-dev   │  CLI tool on your machine                     │
│   │    CLI      │                                               │
│   └──────┬──────┘                                               │
│          │                                                       │
│          │ Reservation request                                   │
│          ▼                                                       │
│   ┌─────────────┐                                               │
│   │  SQS Queue  │  Async processing                             │
│   └──────┬──────┘                                               │
│          │                                                       │
│          │ Process reservation                                   │
│          ▼                                                       │
│   ┌─────────────┐     ┌─────────────┐                           │
│   │   Lambda    │────▶│  DynamoDB   │  State tracking           │
│   │  Processor  │     │             │                           │
│   └──────┬──────┘     └─────────────┘                           │
│          │                                                       │
│          │ Create pod                                            │
│          ▼                                                       │
│   ┌─────────────────────────────────────────────────────┐       │
│   │                 EKS Cluster                          │       │
│   │  ┌─────────┐  ┌─────────┐  ┌─────────┐  ┌─────────┐ │       │
│   │  │ H100    │  │ B200    │  │ A100    │  │ T4      │ │       │
│   │  │ Nodes   │  │ Nodes   │  │ Nodes   │  │ Nodes   │ │       │
│   │  └─────────┘  └─────────┘  └─────────┘  └─────────┘ │       │
│   └─────────────────────────────────────────────────────┘       │
│                                                                  │
└─────────────────────────────────────────────────────────────────┘
```

### Why Kubernetes?

ODC runs on Amazon EKS (Elastic Kubernetes Service) because:

1. **Resource isolation**: Each reservation is a pod with guaranteed GPU allocation
2. **Scheduling**: Kubernetes handles GPU placement and node selection
3. **Networking**: Built-in DNS, service discovery, and network policies
4. **Storage**: CSI drivers for EBS and EFS integration
5. **Scalability**: Auto-scaling node groups per GPU type

### How It Affects Your Work

**Mostly transparent**, but a few things to know:

- **Pod restarts**: If your pod crashes (e.g., OOM), it auto-restarts and you can reconnect
- **Node failures**: Rare, but if a node dies, your pod may be rescheduled
- **Network**: Pods have full internet access and inter-pod communication
- **Storage**: EBS disks are node-specific; snapshots enable portability

### Pod Startup Process

When your reservation is created, here's what happens:

1. **Init container** runs first:
   - Fetches your SSH public keys from GitHub
   - Sets up the `dev` user with your keys
   - Configures passwordless sudo

2. **Startup script** runs in the main container:
   - Mounts your persistent disk (if using one)
   - On first boot or with `--recreate-env`: copies shell configs (`.zshrc`, `.bashrc`, oh-my-zsh) to your home directory
   - On subsequent boots: uses your existing (possibly customized) shell configs
   - Sets up CPU/memory environment variables for your allocation

3. **SSH server** starts and waits for your connection

This means your first SSH login might take a few extra seconds while the startup completes.

### Infrastructure Details

| Component | Technology | Purpose |
|-----------|------------|---------|
| Compute | AWS EC2 (p5, p4d, g5, etc.) | GPU instances |
| Orchestration | Amazon EKS | Kubernetes cluster |
| GPU Management | NVIDIA GPU Operator | Drivers and device plugin |
| Block Storage | AWS EBS + CSI Driver | Persistent disks |
| File Storage | AWS EFS | Shared storage |
| Networking | AWS VPC + EFA | High-speed networking |
| State | AWS DynamoDB | Reservation tracking |
| Queue | AWS SQS | Async reservation processing |

---

## GPU Types Reference

| Type | Instance | GPUs | GPU Memory | Node CPU | Node RAM | Best For |
|------|----------|------|------------|----------|----------|----------|
| `b200` | p6-b200.48xlarge | 8 | 192 GB/GPU | 192 | 2048 GB | Latest NVIDIA Blackwell |
| `h200` | p5e.48xlarge | 8 | 141 GB/GPU | 192 | 2048 GB | Large models, high memory |
| `h100` | p5.48xlarge | 8 | 80 GB/GPU | 192 | 2048 GB | Production training |
| `a100` | p4d.24xlarge | 8 | 40 GB/GPU | 96 | 1152 GB | General ML training |
| `a10g` | g5.12xlarge | 4 | 24 GB/GPU | 48 | 192 GB | Inference, smaller training |
| `l4` | g6.12xlarge | 4 | 24 GB/GPU | 48 | 192 GB | Cost-effective inference |
| `t4` | g4dn.12xlarge | 4 | 16 GB/GPU | 48 | 192 GB | Development, testing |
| `t4-small` | g4dn.xlarge | 1 | 16 GB/GPU | 4 | 16 GB | Single GPU development |
| `cpu-arm` | c7g.4xlarge | 0 | N/A | 16 | 32 GB | ARM CPU workloads |
| `cpu-x86` | c7i.4xlarge | 0 | N/A | 16 | 32 GB | x86 CPU workloads |

---

## Troubleshooting

### Common Issues

#### "Queued" Status - Not Getting Resources

**Symptoms**: Reservation stays in "queued" status

**Cause**: No available GPUs of the requested type

**Solutions**:
```bash
# Check availability
gpu-dev avail

# Try a different GPU type
gpu-dev reserve -t t4 -g 4 -h 4  # Instead of h100

# Reduce GPU count
gpu-dev reserve -t h100 -g 4 -h 4  # Instead of 8

# Watch the queue
gpu-dev list --watch
```

#### Can't SSH to Reservation

**Symptoms**: `Connection refused` or `Permission denied`

**Causes and solutions**:

1. **Pod not ready yet**:
   ```bash
   gpu-dev show <id>
   # Wait for status: "active"
   ```

2. **Wrong SSH key**:
   ```bash
   # Check your GitHub username is correct
   gpu-dev config show

   # Verify keys at github.com
   curl https://github.com/<your-username>.keys
   ```

3. **SSH config not enabled**:
   ```bash
   gpu-dev config ssh-include enable
   ```

#### Disk In Use Error

**Symptoms**: "Disk is currently in use by another reservation"

**Solutions**:
```bash
# See which reservation has the disk
gpu-dev disk list

# Cancel the other reservation
gpu-dev cancel <other-reservation-id>

# Or use a different disk
gpu-dev reserve --disk another-disk -t h100 -g 4 -h 4

# Or use no disk
gpu-dev reserve --disk none -t h100 -g 4 -h 4
```

#### Pod Stuck in "Preparing"

**Symptoms**: Status stays at "preparing" for a long time

**Common causes**:
- Large Docker image being pulled
- Disk snapshot being restored
- Node scaling up

**What to do**:
```bash
# Check detailed status
gpu-dev show <id>

# Watch for progress
gpu-dev list --watch

# If stuck for >10 minutes, try canceling and recreating
gpu-dev cancel <id>
gpu-dev reserve ...
```

#### Out of Memory (OOM)

**Symptoms**: Process killed, "OOM" indicator in `gpu-dev list`

**Automatic Recovery**: ODC pods are configured with `restartPolicy: OnFailure`, which means if your container is killed due to OOM, it will automatically restart. Your SSH session will disconnect, but you can reconnect after a few seconds. Your persistent disk data (`/home/dev`) is preserved.

**Solutions**:
1. Reserve more GPUs (increases memory proportionally)
2. Reduce batch size in training
3. Use gradient checkpointing
4. Check for memory leaks

```bash
# See OOM events
gpu-dev show <id>
# Look for "OOM Events" section
```

**After OOM restart:**
- Reconnect via `gpu-dev connect`
- Your files in `/home/dev` and `/shared-personal` are preserved
- Running processes need to be restarted manually

### Debugging Commands

```bash
# Detailed reservation info
gpu-dev show <id>

# Watch reservation status
gpu-dev list --watch

# Check cluster capacity
gpu-dev status

# Check availability
gpu-dev avail

# View disk contents (from snapshot)
gpu-dev disk list-content <disk-name>
```

### Getting Help

```bash
# CLI help
gpu-dev help
gpu-dev <command> --help

# Show config and AWS identity
gpu-dev config show
```

---

## FAQ

### General

**Q: How long can I keep a reservation?**

A: Maximum 24 hours initially. You can extend once for up to 24 additional hours (48 hours total). Use `gpu-dev edit <id> --extend` before your reservation expires.

**Q: What happens when my reservation expires?**

A: Your pod is terminated, your persistent disk is snapshotted, and resources are released. Data in `/home/dev` is preserved in the disk snapshot. Data in `/shared-personal` persists independently.

**Q: Can I have multiple active reservations?**

A: Yes, but each uses separate resources. You can use different disks for each.

**Q: Are GPUs shared with other users?**

A: No. When you reserve GPUs, they're exclusively yours for the duration.

### Storage

**Q: Will I lose my data when the reservation ends?**

A: Data in `/home/dev` is preserved via disk snapshots. Data in `/shared-personal` persists always. Only temporary storage (`--disk none`) is lost.

**Q: How much storage do I get?**

A: 100GB persistent disk (`/home/dev`), plus elastic EFS storage (`/shared-personal`).

**Q: Can I access my disk's contents without an active reservation?**

A: Yes! Use `gpu-dev disk list-content <disk-name>` to see files from the last snapshot.

### Networking

**Q: Can I access the internet from my reservation?**

A: Yes, full outbound internet access is available.

**Q: Can I expose a port/service?**

A: SSH and Jupyter ports are exposed via NodePort. For other services, use SSH port forwarding (`ssh -L`).

**Q: How fast is inter-node networking for multinode?**

A: EFA provides up to 3200 Gbps (400 GB/s) bandwidth. Expect 50-100 GB/s effective throughput for NCCL operations.

### IDE and Tools

**Q: Can I use VS Code/Cursor?**

A: Yes! Enable SSH config auto-include (`gpu-dev config ssh-include enable`) and your reservations appear in the IDE's remote hosts list.

**Q: Is Docker available inside the container?**

A: Not by default. The pods run as containers, so Docker-in-Docker requires special configuration.

**Q: Can I install additional software?**

A: Yes, you have passwordless sudo access. Installed software persists if using a persistent disk.

**Q: Can I customize my shell (zsh/bash)?**

A: Yes! Your shell configuration files (`.zshrc`, `.bashrc`, etc.) are stored on your persistent disk. Customize them as you like - changes persist across reservations. Use `--recreate-env` to reset to defaults.

### Billing and Resources

**Q: How is CPU/memory allocated?**

A: Proportionally based on GPUs reserved. For example, 4 of 8 GPUs = 50% of node CPU and memory.

**Q: What if I need more CPU than my GPU allocation provides?**

A: Reserve more GPUs, or use a GPU type with better CPU ratio (like A10G or T4).

---

## Command Reference

| Command | Description |
|---------|-------------|
| `gpu-dev reserve` | Create a new reservation |
| `gpu-dev list` | List your reservations |
| `gpu-dev show [ID]` | Show reservation details |
| `gpu-dev connect [ID]` | SSH to your reservation |
| `gpu-dev cancel [ID]` | Cancel a reservation |
| `gpu-dev edit [ID]` | Modify a reservation |
| `gpu-dev avail` | Show GPU availability |
| `gpu-dev status` | Show cluster status |
| `gpu-dev disk list` | List your disks |
| `gpu-dev disk create NAME` | Create a new disk |
| `gpu-dev disk delete NAME` | Delete a disk |
| `gpu-dev disk list-content NAME` | View disk contents |
| `gpu-dev disk rename OLD NEW` | Rename a disk |
| `gpu-dev config show` | Show configuration |
| `gpu-dev config set KEY VALUE` | Set a config value |
| `gpu-dev config ssh-include enable` | Enable SSH config integration |
| `gpu-dev help` | Show help |

---

*For the latest updates and to report issues, visit the project repository.*
