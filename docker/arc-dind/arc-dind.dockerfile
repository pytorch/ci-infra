FROM --platform=linux/amd64 docker:dind-rootless

SHELL ["/bin/sh", "-c"]

USER root

RUN mkdir -p /etc/containerd/certs.d/docker.io/ && \
    mkdir -p /etc/containerd/certs.d/ghcr.io/

COPY docker.io.hosts.toml /etc/containerd/certs.d/docker.io/hosts.toml
COPY ghcr.io.hosts.toml /etc/containerd/certs.d/ghcr.io/hosts.toml
COPY daemon.json /etc/docker/daemon.json

USER rootless
