# osdc — Open Source Developer Cloud

A self-hosted developer platform for GPU work. Devs ask for `1 / 2 / 4 / 8`
GPUs of a given type, the platform parks them on a Kubernetes pod with SSH
access, and tears it down when the reservation expires.

Built for PyTorch contributors — auth is via the GitHub public keys of users
with commit access — but the design is generic enough to plug into other
groups.

## What you get

- **Python CLI** (`gpu-dev`) with `reserve`, `list`, `extend`, `cancel`, and
  `config` commands. Real-time polling until your pod is ready.
- **GPU types**: T4, L4, A100, H100, B200. Pick the count (1, 2, 4, 8) and the
  duration in hours (fractional is fine, e.g. `--hours 0.25`).
- **SSH** straight into the pod via NodePort, with **your own GitHub public
  keys** injected — no separate credentials to manage.
- **Persistent disk** that survives between reservations (opt-in), backed by
  EBS snapshots. Or run with `--no-persist` for a clean `EmptyDir` workspace.
- **20 TB shared EFS** mounted at `/shared` with per-user folders.
- **NVIDIA profiling** ready out of the box (`ncu` / `nsys` work without
  manual driver tweaks), with one node per GPU type reserved as
  profiling-dedicated.
- **Grafana** dashboard at `<node-ip>:30080` with NVIDIA DCGM exporter
  metrics — utilization, memory, temp, power.
- **Multi-node NCCL** working over EFA with `OFI_NCCL_PROTOCOL=SENDRECV`.
  Tree algo gets ~21 GB/s bus bandwidth across 2× p5.48xlarge (16 H100).

## How it fits together

```
   ┌────────┐  reserve     ┌────────┐  enqueue  ┌────────────┐
   │  CLI   │ ───────────► │   API  │ ────────► │    SQS     │
   └────────┘              └────────┘           └─────┬──────┘
        ▲ poll                                        │
        │                                             ▼
        │              ┌──────────────────────────────────────┐
        │              │  Lambda  reservation processor       │
        │              │  - pick a node with free GPUs        │
        │              │  - attach EBS, mount /shared (EFS)   │
        │              │  - create K8s pod, inject GH keys    │
        │              └────────────────┬─────────────────────┘
        │                               │
        │                               ▼
        │                     ┌──────────────────┐
        │                     │    EKS (k8s)     │
        │  SSH (NodePort)     │  GPU node groups │
        └─────────────────────┤   T4 / L4 / H100 │
                              │   B200 / ...     │
                              └──────────────────┘

   DynamoDB holds reservation state & history; CloudWatch logs the lambdas.
```

## Repository layout

```
.
├── cli-tools/             # `gpu-dev` Python CLI (pyproject.toml)
├── terraform-gpu-devservers/
│                          # OpenTofu modules for EKS, node groups,
│                          # SQS, Lambda, DynamoDB, EFS, monitoring
├── admin/                 # operator scripts
├── docs/                  # user guide and architecture notes
└── tests/
```

## Getting started — as a user

You need: GitHub access to the configured org (PyTorch by default), and your
public keys uploaded to GitHub.

```bash
# 1. Install the CLI
pip install -e ./cli-tools/gpu-dev-cli

# 2. Point it at your deployment
gpu-dev config        # walks you through API URL + GitHub username

# 3. Reserve a GPU
gpu-dev reserve -g 1 -t h100 -h 2          # 1× H100 for 2 hours
gpu-dev reserve -g 8 -t b200 -h 24         # 8× B200 for a day
gpu-dev reserve -g 1 -t t4  -h 0.25        # 1× T4 for 15 minutes

# 4. Watch it come up; SSH instructions print when ready
gpu-dev list

# 5. Extend if you need more time (max total 48 h)
gpu-dev extend <reservation-id> --hours 12

# 6. Done? Free it up.
gpu-dev cancel <reservation-id>
```

Each reservation drops an SSH config file at
`~/.devgpu/<reservation_id>-sshconfig`, so connecting is just:

```bash
ssh -F ~/.devgpu/<reservation_id>-sshconfig gpu-dev
```

## Getting started — as an operator

You need: an AWS account with EC2 GPU capacity (reserved or on-demand), an
OpenTofu workstation, and credentials for whatever IAM role the modules
assume.

```bash
cd terraform-gpu-devservers
tf init                  # `tf` is aliased to `opentofu` in this repo
tf plan                  # read-only — agents are restricted to this
tf apply                 # only on a real workstation, not via the agent
```

Important variables to set in your `*.tfvars`:

- `aws_region` (defaults to `us-east-2`)
- node group sizing per GPU type (T4 / L4 / H100 / B200)
- `grafana_admin_password`
- the GitHub org/team that's allowed to reserve

Once nodes are up, label one per GPU type as profiling-dedicated so DCGM
doesn't fight Nsight for the device:

```bash
kubectl label node <h100-node> gpu.monitoring/profiling-dedicated=true
kubectl label node <b200-node> gpu.monitoring/profiling-dedicated=true
```

Grafana lands at `http://<node-ip>:30080` (admin / your configured password).
Pre-loaded dashboards: NVIDIA DCGM (community ID 12239) and a custom GPU
overview.

## Status

Working end-to-end on T4 / L4 / H100. B200 supported with on-demand capacity.
Active development — see [`PROGRESS.md`](PROGRESS.md) and [`TODO.md`](TODO.md)
for what's in flight and what's queued.

## License

See [`LICENSE`](LICENSE) once added. For now: ask before reusing.
