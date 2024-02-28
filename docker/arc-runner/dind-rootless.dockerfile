FROM ghcr.io/actions/actions-runner:latest

# Copy, so avoid create new hashes as much as possible
COPY ../inarc /.inarc
COPY ../inarc /.inarc-dind-rootless

RUN sudo apt-get update && \
    sudo apt-get install -y git curl unzip build-essential ubuntu-dev-tools dnsutils python3-pip zip && \
    sudo apt-get clean && \
    cd /home/runner && \
    curl "https://awscli.amazonaws.com/awscli-exe-linux-x86_64.zip" -o "awscliv2.zip" && \
    unzip awscliv2.zip && \
    sudo ./aws/install && \
    rm -rf awscliv2.zip aws
