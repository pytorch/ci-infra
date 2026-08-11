# Credential-free (except its Bedrock IRSA role) agent worker image.
#
# A long-running HTTP worker (server.py) callable over the network like buildkitd.
# It clones PUBLIC repos anonymously (no token) and calls Bedrock directly via
# boto3 (which picks up the pod's IRSA web-identity credentials). No proxies.
#
# Build + push once to the in-cluster Harbor osdc project (see README):
#   docker buildx build --platform linux/amd64 -t ci-agent-sandbox:prototype \
#     -o type=docker,dest=/tmp/agent.tar modules/agent-sandbox/agent
#   crane push /tmp/agent.tar <harbor>/osdc/ci-agent-sandbox:prototype
FROM public.ecr.aws/docker/library/debian:12-slim

RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        ca-certificates \
        git \
        python3 \
        python3-pip \
    && pip3 install --no-cache-dir --break-system-packages boto3 \
    && rm -rf /var/lib/apt/lists/*

COPY server.py /usr/local/bin/server.py
RUN chmod 0755 /usr/local/bin/server.py

# UID 1000, non-root. The pod securityContext enforces this too.
USER 1000

EXPOSE 8080
ENTRYPOINT ["python3", "/usr/local/bin/server.py"]
