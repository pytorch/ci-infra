## Features

- Drop the `stream-buffers` dependency from the k8s hook, which removes the `Buffer() is deprecated` (Node DEP0005) warning printed once per containerized step. Exec stdout/stderr is now captured by a small in-repo `BufferSink` writable that concatenates chunks lazily, with no preallocated buffer and no deprecated `Buffer()` call. Capping/truncation and encoding behavior are unchanged. See actions/runner-container-hooks#261.

## SHA-256 Checksums

- docker: `<DOCKER_SHA>`
- k8s: `<K8S_SHA>`
