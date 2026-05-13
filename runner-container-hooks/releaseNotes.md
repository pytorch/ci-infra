## Bugs

- Fix runner-pod cgroup OOMKill during workspace copy: `execCpToPod`/`execCpFromPod` now throttle the tar source stream against `WebSocket.bufferedAmount` (high-water 200 MiB, low-water 50 MiB, 50 ms poll) so the kc client cannot queue an unbounded workspace into native stdio buffers under back-pressure.
- Cap stderr capture at 64 KiB in `execCpToPod`/`execCpFromPod` to prevent another unbounded buffer growth path.
- Fix spurious retry storms on successful copies: previously any non-empty stderr (e.g. benign `tar: Removing leading '/'` warning) was treated as failure. Now reject only when the remote status is non-Success; on Success, capture stderr at debug log level.
- Add 60 s default per-call timeout to `execPodStep` and `execPodStepOutput` (was unbounded, would hang indefinitely if the WebSocket settled silently). Both functions accept an optional `timeoutMs?: number` parameter for callers needing a longer budget.
- Add process-level handlers for `SIGTERM`, `uncaughtException`, and `unhandledRejection` that emit a `[runner-container-hooks] FATAL: ...` log line to stderr before exit, eliminating silent crashes when the runner reaps the hook process.
- Use `WebSocket.terminate()` (not `close()`) on cancel/timeout paths so the remote command is actually killed (per `kubernetes-client/javascript#2532`).
- Pin `@kubernetes/client-node` to `1.4.0` exact (was `^1.3.0`) to prevent future minor upgrades from silently changing the WebSocket handler internals the new throttle relies on.

## SHA-256 Checksums

- docker: `<DOCKER_SHA>`
- k8s: `<K8S_SHA>`
