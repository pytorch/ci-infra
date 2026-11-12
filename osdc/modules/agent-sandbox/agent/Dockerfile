# Credential-free sandbox task image.
#
# The pod runs sandboxd, which idles on :9090 until the dispatcher starts task.py through
# it — once per pod. task.py clones a public repo directly and calls Bedrock THROUGH the
# sigv4 proxy, prints the result as JSON, and exits. No state survives the pod. The image
# holds no secrets and no AWS SDK: Bedrock is reached as plain HTTP to the signing proxy,
# which owns the credential.
#
# Built and pushed to the in-cluster Harbor osdc project by the module's deploy.sh
# under a content-hash tag — not built by hand. See the module README.

# sandboxd (kubernetes-sigs/agent-sandbox): the daemon through which the dispatcher starts
# each task. A static binary, pinned by digest.
FROM registry.k8s.io/agent-sandbox/sandboxd:v1.0.10@sha256:91c6c3fef9a3790d0e3c152efc647b36dcf6aaac3728a63770f91a1051553728 AS sandboxd

FROM public.ecr.aws/docker/library/debian:12-slim

RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        ca-certificates \
        git \
        python3 \
    && rm -rf /var/lib/apt/lists/*

COPY sandbox.py agent_loop.py task.py /usr/local/bin/
RUN chmod 0755 /usr/local/bin/sandbox.py /usr/local/bin/agent_loop.py /usr/local/bin/task.py

# task.py imports sandbox.py from the same directory.
ENV PYTHONPATH=/usr/local/bin

COPY --from=sandboxd /usr/local/bin/sandboxd /usr/local/bin/sandboxd
# sandboxd's --root-dir: every task runs with this as its working directory.
RUN mkdir -p /workspace && chown 1000:1000 /workspace

# UID 1000, non-root. The pod securityContext enforces this too.
USER 1000

ENTRYPOINT ["/usr/local/bin/sandboxd"]
