## Features

- Inject same-node scheduling preference for job pods: hooks now look up the runner pod's node and add a weight-100 `preferredDuringSchedulingIgnoredDuringExecution` entry for `kubernetes.io/hostname`, so job pods prefer the same node as their runner pod while gracefully falling back to other nodes if unavailable

## Bugs

- fix: serialize Error objects properly in thrown error messages

## SHA-256 Checksums

The SHA-256 checksums for the packages included in this build are shown below:

- actions-runner-hooks-docker-<HOOK_VERSION>.zip <DOCKER_SHA>
- actions-runner-hooks-k8s-<HOOK_VERSION>.zip <K8S_SHA>
