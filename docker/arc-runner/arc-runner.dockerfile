FROM --platform=linux/amd64 ghcr.io/actions/actions-runner:latest

COPY ../inarc /.inarc

SHELL ["/bin/bash", "-c"]

USER root

RUN usermod -u 1000 runner && \
    groupmod -g 1000 runner && \
    groupadd -g 2375 docker2 && \
    usermod -aG 1000 runner && \
    usermod -aG 2375 runner && \
    usermod -aG docker runner && \
    chown -R 1000:1000 /home/runner

RUN apt-get update && \
    apt-get install -y git curl unzip build-essential ubuntu-dev-tools dnsutils python3-pip zip && \
    apt-get clean

RUN cd /home/runner && \
    curl "https://awscli.amazonaws.com/awscli-exe-linux-x86_64.zip" -o "awscliv2.zip" && \
    unzip awscliv2.zip && \
    ./aws/install && \
    rm -rf awscliv2.zip aws

RUN mkdir -p /usr/temp_pb_install_dir && \
    ln -s /usr/lib64 "/usr/temp_pb_install_dir/lib64" && \
    curl -LO "https://github.com/protocolbuffers/protobuf/releases/download/v3.17.3/protobuf-all-3.17.3.tar.gz" --retry 3 && \
    tar -xvz --no-same-owner -C "/usr/temp_pb_install_dir" --strip-components 1 -f "protobuf-all-3.17.3.tar.gz" && \
    pushd "/usr/temp_pb_install_dir" && \
    ./configure && \
    make -j8 && \
    make check && \
    make install && \
    ldconfig && \
    popd && \
    rm -rf "/usr/temp_pb_install_dir" && \
    rm -f "protobuf-all-3.17.3.tar.gz"

USER runner
