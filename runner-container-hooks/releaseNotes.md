## Features

- Wait for configurable node taints to clear before creating workflow pods: prevents Karpenter-scheduler deadlock on fresh nodes where the runner tolerates a startup taint but the workflow pod does not. Configured via `ACTIONS_RUNNER_WAIT_FOR_NODE_TAINTS` (comma-separated taint keys) and `ACTIONS_RUNNER_WAIT_FOR_NODE_TAINTS_TIMEOUT_SECONDS` (default 300s). No-op when env var is unset (backward compatible).

## SHA-256 Checksums

The SHA-256 checksums for the packages included in this build are shown below:

- actions-runner-hooks-docker-<HOOK_VERSION>.zip <DOCKER_SHA>
- actions-runner-hooks-k8s-<HOOK_VERSION>.zip <K8S_SHA>
