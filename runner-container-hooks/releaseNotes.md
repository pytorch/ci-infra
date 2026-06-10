## Features

- Replace the embedded in-pod Python RPC server with a static, musl-linked Rust binary (embedded per-architecture and selected by the pod's `uname -m`). The in-pod helper no longer depends on `python3` or `node` being present in the workflow image, so it runs on glibc, musl/Alpine, distroless, and scratch images alike.
- Harden the RPC server lifecycle: fix heartbeat-watchdog races (a timed-out job can no longer have its kill land on an unrelated job, nor clobber a fresh job's heartbeat), preserve the real exit cause for signal-killed jobs (e.g. `-9`/`-15` instead of `-1`), report a distinct `killing` status while a job is being torn down, forward termination signals to the user job so it isn't orphaned (re-raising the signal so the server's own exit status reflects it), recover from mutex poisoning instead of failing every subsequent request, and fail fast with an actionable error when the server binary hasn't been embedded.

## SHA-256 Checksums

- docker: `<DOCKER_SHA>`
- k8s: `<K8S_SHA>`
