FROM ghcr.io/actions/actions-runner:2.313.0

RUN sudo apt-get update && sudo apt-get install -y \
    git \
    curl \
    openssh-client \
    && sudo rm -rf /var/lib/apt/lists/*
