FROM ghcr.io/actions/actions-runner:latest

COPY ../inarc /.inarc
COPY ../inarc /.inarc-dind-rootless

SHELL ["/bin/sh", "-c"]

USER root

RUN usermod -u 1000 runner && \
    groupmod -g 1000 runner

RUN apt-get update && \
    apt-get install -y git curl unzip build-essential ubuntu-dev-tools dnsutils python3-pip zip && \
    apt-get clean && \
    cd /home/runner && \
    curl "https://awscli.amazonaws.com/awscli-exe-linux-x86_64.zip" -o "awscliv2.zip" && \
    unzip awscliv2.zip && \
    ./aws/install && \
    rm -rf awscliv2.zip aws

USER runner
