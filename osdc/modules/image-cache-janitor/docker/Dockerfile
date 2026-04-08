FROM public.ecr.aws/docker/library/python:3.12-alpine
RUN apk add --no-cache util-linux=2.41.2-r0

# Install crictl (CRI CLI) so the janitor is self-contained and does not
# depend on the host AMI shipping crictl.
ARG TARGETARCH
ARG CRICTL_VERSION=v1.32.0
RUN wget -q -O /tmp/crictl.tar.gz \
    "https://github.com/kubernetes-sigs/cri-tools/releases/download/${CRICTL_VERSION}/crictl-${CRICTL_VERSION}-linux-${TARGETARCH}.tar.gz" \
    && tar xzf /tmp/crictl.tar.gz -C /usr/local/bin \
    && rm /tmp/crictl.tar.gz
