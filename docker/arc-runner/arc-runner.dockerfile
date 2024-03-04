FROM ghcr.io/actions/actions-runner:latest

COPY ../inarc /.inarc
COPY ../inarc /.inarc-dind-rootless

SHELL ["/bin/sh", "-c"]

RUN sudo echo "jenkins:x:1001:1001::/var/lib/jenkins:" >> /etc/passwd && \
    sudo echo "jenkins:x:1001:" >> /etc/group && \
    sudo echo "jenkins:*:19110:0:99999:7:::" >>/etc/shadow && \
    sudo mkdir -p /var/lib/jenkins/workspace && \
    sudo mkdir -p /var/lib/jenkins/.ccache && \
    sudo chown -R jenkins:jenkins /var/lib/jenkins && \
    sudo chown jenkins:jenkins /usr/local && \
    sudo echo 'jenkins ALL=(ALL) NOPASSWD:ALL' > /etc/sudoers.d/jenkins

RUN sudo apt-get update && \
    sudo apt-get install -y git curl unzip build-essential ubuntu-dev-tools dnsutils python3-pip zip && \
    sudo apt-get clean && \
    cd /home/runner && \
    curl "https://awscli.amazonaws.com/awscli-exe-linux-x86_64.zip" -o "awscliv2.zip" && \
    unzip awscliv2.zip && \
    sudo ./aws/install && \
    rm -rf awscliv2.zip aws
