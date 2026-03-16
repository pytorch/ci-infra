# Minimal base runner for Kubernetes mode
# Workflows specify container images - this is just the runner orchestrator
FROM ubuntu:22.04

ENV DEBIAN_FRONTEND=noninteractive

# Install ONLY essential tools for runner operation and debugging
# hadolint ignore=DL3008
RUN apt-get update && apt-get install -y --no-install-recommends \
    curl \
    git \
    jq \
    ca-certificates \
    tar \
    gzip \
    && rm -rf /var/lib/apt/lists/*

# Install GitHub Actions Runner
ARG RUNNER_VERSION=2.313.0
RUN useradd -m -s /bin/bash runner \
    && mkdir -p /home/runner/actions-runner

WORKDIR /home/runner/actions-runner
RUN curl -o actions-runner-linux-x64-${RUNNER_VERSION}.tar.gz \
        -L https://github.com/actions/runner/releases/download/v${RUNNER_VERSION}/actions-runner-linux-x64-${RUNNER_VERSION}.tar.gz \
    && tar xzf ./actions-runner-linux-x64-${RUNNER_VERSION}.tar.gz \
    && rm actions-runner-linux-x64-${RUNNER_VERSION}.tar.gz \
    && chown -R runner:runner /home/runner/actions-runner

# Install runner dependencies
RUN ./bin/installdependencies.sh

# Create workspace directory
RUN mkdir -p /home/runner/_work && chown -R runner:runner /home/runner/_work

USER runner
WORKDIR /home/runner/actions-runner

ENTRYPOINT ["/home/runner/actions-runner/bin/Runner.Listener"]
CMD ["run"]
