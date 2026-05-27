## Features

- Protect the in-pod RPC server from OOM-kills under memory pressure: the server now sets its own `oom_score_adj` to `-1000` at startup so the kernel kills the user job (the actual memory consumer) instead of the RPC server. The spawned user job resets its `oom_score_adj` back to `0` via an async-signal-safe `preexec_fn` so it remains a normal OOM candidate. Result: when a workflow pod hits its memory limit, the hook driver still has a working channel to report a clean failure instead of seeing the RPC server die mid-step.

## SHA-256 Checksums

- docker: `<DOCKER_SHA>`
- k8s: `<K8S_SHA>`
