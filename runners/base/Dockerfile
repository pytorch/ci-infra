# GitHub Actions Runner Container - Base Image with Rust-based Feature Installer
# Purpose: Self-hosted runner with dynamic feature installation via Rust
# Base: Ubuntu 22.04 LTS for stability and broad package support

FROM ubuntu:22.04

# Prevent interactive prompts during package installation
ARG DEBIAN_FRONTEND=noninteractive

# Install essential system packages + Rust build dependencies
# - curl: Download GitHub runner and install scripts
# - git: Required for GitHub Actions checkout
# - sudo: Allow runner user to install additional packages
# - jq: JSON parsing for GitHub API responses
# - ca-certificates: Ensure HTTPS connections work properly
# - build-essential: Required for Rust compilation
RUN apt-get update && apt-get install -y \
    curl \
    git \
    sudo \
    jq \
    ca-certificates \
    build-essential \
    && rm -rf /var/lib/apt/lists/* \
    && apt-get clean

# Install Rust toolchain for building the feature installer
RUN curl --proto '=https' --tlsv1.2 -sSf https://sh.rustup.rs | sh -s -- -y
ENV PATH="/root/.cargo/bin:${PATH}"

# Create dedicated runner user with sudo privileges
# Security: Run as non-root user, but allow package installation
RUN useradd -m -s /bin/bash runner && \
    usermod -aG sudo runner && \
    echo "runner ALL=(ALL) NOPASSWD:ALL" >> /etc/sudoers

# Copy and build Rust-based feature installer
COPY runner-installer/ /tmp/runner-installer/
WORKDIR /tmp/runner-installer
RUN cargo build --release && \
    cp target/release/runner-installer /usr/local/bin/ && \
    chmod +x /usr/local/bin/runner-installer

# Clean up build dependencies to reduce image size
RUN rm -rf /tmp/runner-installer ~/.cargo/registry ~/.cargo/git

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
COPY runners/base/scripts/entrypoint.sh /home/runner/
RUN sudo chown runner:runner /home/runner/entrypoint.sh && \
    chmod +x /home/runner/entrypoint.sh

# Default entrypoint - handles setup and starts the runner
ENTRYPOINT ["/home/runner/entrypoint.sh"]