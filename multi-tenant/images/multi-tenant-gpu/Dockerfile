FROM --platform=linux/amd64 ghcr.io/actions/actions-runner:latest AS BUILD

SHELL ["/bin/bash", "-c"]

USER root

# 1001 is what the base image already uses, and what the GitHub runner on ARC
# runs as (actions/runner-images#10936). Pinning it here rather than relying on
# the base keeps the uid deterministic if that image moves again. A job
# container whose user does not match this cannot write the runner's
# RUNNER_TEMP, which is outside GITHUB_WORKSPACE and so not covered by the
# chmod in pytorch's setup-linux.
RUN usermod -u 1001 runner && \
    groupmod -g 1001 runner && \
    groupadd -g 2375 docker2 && \
    usermod -aG 1001 runner && \
    usermod -aG 2375 runner && \
    usermod -aG docker runner && \
    chown -R 1001:1001 /home/runner

RUN apt-get update && \
    apt-get install -y \
        build-essential \
        cmake \
        curl \
        dnsutils \
        fontconfig \
        g++ \
        gcc \
        git \
        jq \
        kmod \
        libfontconfig1-dev \
        libgl1-mesa-glx \
        libjpeg-dev \
        libpango-1.0-0 \
        libpangoft2-1.0-0 \
        libpng-dev \
        libsdl2-2.0-0 \
        libsdl2-dev \
        libsndfile1-dev \
        libxml2-dev \
        libxslt1-dev \
        ninja-build \
        python3-pip \
        ubuntu-dev-tools \
        unzip \
        wget \
        zip \
        zlib1g-dev && \
    apt-get clean && \
    rm -rf /var/lib/apt/lists/*

RUN cd /home/runner && \
    curl "https://awscli.amazonaws.com/awscli-exe-linux-x86_64.zip" -o "awscliv2.zip" && \
    unzip awscliv2.zip && \
    ./aws/install && \
    rm -rf awscliv2.zip aws

RUN touch /.inarc && \
    chmod 0444 /.inarc

FROM --platform=linux/amd64 ghcr.io/actions/actions-runner:latest AS RUNNER
COPY --from=BUILD / /
USER runner
