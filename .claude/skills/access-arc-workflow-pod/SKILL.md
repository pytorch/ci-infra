---
name: access-arc-workflow-pod
description: Access ARC runner and workflow pods on PyTorch's EKS CI cluster. Use when needing to inspect or debug running workflow pods. Triggered by mentions of "access workflow pod", "debug workflow pod", "ssh into runner", "inspect CI pod", or "debug ARC pod".
---

# Access ARC Workflow Pod

Provides instructions for connecting to ARC runner and workflow pods on PyTorch's production EKS cluster.

## When to use this skill

Use when the user asks to:
- SSH into a runner or workflow pod
- Inspect processes, files, or state inside a CI workflow pod
- Run ad-hoc commands in a workflow pod
- Hold a workflow pod alive for investigation

## Prerequisites

- **Meta VPN**: Must be connected to Meta corporate VPN
- **cloud_corp CLI**: Required to obtain AWS credentials for EKS cluster access
- **kubectl**: Must be installed and available on PATH

## Instructions

### Step 1: Identify the pod names

Extract the **runner pod name** from the GitHub Actions job log. Navigate to the job URL and look at **Step 1 ("Set up job"), line 2**, which shows the runner name.

Example URL pattern:
```
https://github.com/pytorch/pytorch/actions/runs/<run_id>/job/<job_id>#step:1:2
```

The runner name looks like: `l-x86iamx-8-16-hl267-runner-p9snl`

The **workflow pod name** is the runner pod name with `-workflow` appended:
```
l-x86iamx-8-16-hl267-runner-p9snl-workflow
```

- **Runner pod** (`<name>`): Lightweight orchestrator (200m CPU, 512Mi). Runs `Runner.Listener`, `Runner.Worker`, and the container hooks node process.
- **Workflow pod** (`<name>-workflow`): Heavy compute pod where CI steps actually execute. PID 1 is `tail -f /dev/null` (keepalive); the runner's container hooks use `kubectl exec` to run each workflow step inside this container.

### Step 2: Configure EKS access

Get AWS credentials and configure kubeconfig:

```bash
# Get credentials (outputs JSON with AccessKeyId, SecretAccessKey, SessionToken)
cloud_corp aws get-creds -o cli 308535385114 -r SSOAdmin

# Set up kubeconfig (production cluster)
aws eks update-kubeconfig --name pytorch-arc-cbr-production --region us-east-2 --alias pytorch-arc-cbr-production
```

**Important for Claude Code sessions**: The bash shell routes HTTPS through a local proxy (`x2pagentd` on port 10054). Bypass it for EKS API calls by prepending:
```bash
NO_PROXY="$NO_PROXY,*.eks.amazonaws.com" no_proxy="$no_proxy,*.eks.amazonaws.com" kubectl ...
```

All pods are in the `arc-runners` namespace.

### Step 3: Common debugging operations

#### SSH into a workflow pod (interactive shell)
```bash
kubectl exec -it <runner-name>-workflow -n arc-runners -- /bin/bash
```

#### Run ad-hoc commands (non-interactive)
```bash
# List all running processes
kubectl exec <runner-name>-workflow -n arc-runners -- ps aux

# Check filesystem
kubectl exec <runner-name>-workflow -n arc-runners -- ls -la /__w/

# Check OS info
kubectl exec <runner-name>-workflow -n arc-runners -- cat /etc/os-release

# Check environment variables
kubectl exec <runner-name>-workflow -n arc-runners -- env
```

#### Inspect the runner pod (orchestrator)
```bash
kubectl exec <runner-name> -n arc-runners -- ps aux
```

#### Pause a running process to hold the pod for investigation
```bash
# Find the PID of the process to pause
kubectl exec <runner-name>-workflow -n arc-runners -- ps aux

# Send SIGSTOP to pause a process indefinitely (pod stays alive)
kubectl exec <runner-name>-workflow -n arc-runners -- kill -STOP <pid>

# Send SIGCONT to resume when done investigating
kubectl exec <runner-name>-workflow -n arc-runners -- kill -CONT <pid>
```

This is useful to freeze a CI step mid-execution and keep the pod alive for debugging. The GitHub Actions job will eventually time out, but you get time to inspect state.

## Cluster details

| Property | Value |
|----------|-------|
| Cluster name | `pytorch-arc-cbr-production` |
| Region | `us-east-2` |
| AWS Account | `308535385114` |
| Namespace | `arc-runners` |
| IAM Role | `SSOAdmin` (via cloud_corp) |

## Notes

- Workflow pods use `kubernetes-novolume` container mode — PID 1 is `tail -f /dev/null` and steps are exec'd in by the hooks
- Zombie `[git] <defunct>` processes are common and harmless — the `tail` PID 1 doesn't reap orphans
- Runner pods are ephemeral — they are deleted shortly after the workflow completes
- If the workflow pod doesn't exist yet, the job may still be initializing or waiting for scheduling
