## Features

- Add IPv6 dual-stack support to the in-pod RPC server: binds `::` with `IPV6_V6ONLY` cleared so the same socket accepts both IPv4 and IPv6 connections. URL construction in the hook now brackets IPv6 literals (`[fd00::1]:port`), and the in-pod health probe uses `localhost` so it resolves to whichever loopback address is available. Works on IPv4-only, IPv6-only, and dual-stack pods with no v4-only regression.
- Forward GitHub Actions job cancellation to the workflow pod via a new RPC `/kill` endpoint. When the runner sends `SIGTERM` to the hook, the hook now signals the in-pod RPC server, which terminates the running script's process group instead of leaving it orphaned.

## SHA-256 Checksums

- docker: `<DOCKER_SHA>`
- k8s: `<K8S_SHA>`
