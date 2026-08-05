# Minimal, credential-free agent image for the sandbox prototype.
#
# Deliberately tiny: git + curl + jq are all it needs. It clones the target repo
# THROUGH the agent-vault proxy (which injects a read-only token the agent never
# sees) and calls Bedrock THROUGH the sigv4 proxy. The image holds no secrets and
# no AWS SDK — Bedrock is reached as plain HTTP to the signing proxy.
#
# Build + push once (the image path is passed to the Job via __AGENT_IMAGE__):
#   docker build -t <registry>/ci-agent-sandbox:prototype modules/agent-sandbox/agent
#   docker push  <registry>/ci-agent-sandbox:prototype
FROM public.ecr.aws/docker/library/debian:12-slim

RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        ca-certificates \
        curl \
        git \
        jq \
    && rm -rf /var/lib/apt/lists/*

COPY agent-run.sh /usr/local/bin/agent-run.sh
RUN chmod 0755 /usr/local/bin/agent-run.sh

# UID 1000, non-root. The pod securityContext enforces this too.
USER 1000

ENTRYPOINT ["/usr/local/bin/agent-run.sh"]
