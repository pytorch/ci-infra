# Credential-free agent worker image for the sandbox prototype.
#
# A long-running HTTP worker (server.py) callable over the network like
# buildkitd. It clones the target repo THROUGH the agent-vault proxy (which
# injects a read-only token the worker never sees) and calls Bedrock THROUGH the
# sigv4 proxy. The image holds no secrets and no AWS SDK — Bedrock is reached as
# plain HTTP to the signing proxy.
#
# Build + push once (the image path is passed via clusters.yaml agent_sandbox.agent_image):
#   docker build -t <registry>/ci-agent-sandbox:prototype modules/agent-sandbox/agent
#   docker push  <registry>/ci-agent-sandbox:prototype
FROM public.ecr.aws/docker/library/debian:12-slim

RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        ca-certificates \
        git \
        python3 \
    && rm -rf /var/lib/apt/lists/*

COPY server.py /usr/local/bin/server.py
RUN chmod 0755 /usr/local/bin/server.py

# UID 1000, non-root. The pod securityContext enforces this too.
USER 1000

EXPOSE 8080
ENTRYPOINT ["python3", "/usr/local/bin/server.py"]
