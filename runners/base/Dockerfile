# GitHub Actions Runner Container - Base Image
# Purpose: Self-hosted runner with dynamic feature installation
# Base: Ubuntu 22.04 LTS for stability and broad package support

FROM ubuntu:22.04

# Prevent interactive prompts during package installation
ARG DEBIAN_FRONTEND=noninteractive

# Install essential system packages
# - curl: Download GitHub runner and install scripts
# - git: Required for GitHub Actions checkout
# - sudo: Allow runner user to install additional packages
# - jq: JSON parsing for GitHub API responses
# - ca-certificates: Ensure HTTPS connections work properly
RUN apt-get update && apt-get install -y \
    curl \
    git \
    sudo \
    jq \
    ca-certificates \
    && rm -rf /var/lib/apt/lists/* \
    && apt-get clean

# Create dedicated runner user with sudo privileges
# Security: Run as non-root user, but allow package installation
RUN useradd -m -s /bin/bash runner && \
    usermod -aG sudo runner && \
    echo "runner ALL=(ALL) NOPASSWD:ALL" >> /etc/sudoers

# Copy our feature installation system
# This allows dynamic tool installation based on environment variables
COPY scripts/install-features.sh /usr/local/bin/
RUN chmod +x /usr/local/bin/install-features.sh

# Switch to runner user for all subsequent operations
USER runner
WORKDIR /home/runner

# Download and extract GitHub Actions runner
# Version 2.311.0 is stable and widely tested
ARG RUNNER_VERSION=2.311.0
RUN curl -o actions-runner-linux-x64-${RUNNER_VERSION}.tar.gz -L \
    https://github.com/actions/runner/releases/download/v${RUNNER_VERSION}/actions-runner-linux-x64-${RUNNER_VERSION}.tar.gz && \
    tar xzf ./actions-runner-linux-x64-${RUNNER_VERSION}.tar.gz && \
    rm actions-runner-linux-x64-${RUNNER_VERSION}.tar.gz

# Copy and set up our custom entrypoint
# This handles feature installation and runner configuration
COPY scripts/entrypoint.sh /home/runner/
RUN sudo chown runner:runner /home/runner/entrypoint.sh && \
    chmod +x /home/runner/entrypoint.sh

# Default entrypoint - handles setup and starts the runner
ENTRYPOINT ["/home/runner/entrypoint.sh"]