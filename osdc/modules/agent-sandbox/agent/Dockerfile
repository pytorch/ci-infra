# Credential-free sandbox task image.
#
# One task per pod: task.py clones a public repo directly and calls Bedrock THROUGH
# the sigv4 proxy, prints the result as JSON, and exits. The dispatcher creates the
# Job (../dispatcher) — nothing in this image listens on a socket, and no state
# survives the pod. The image holds no secrets and no AWS SDK: Bedrock is reached as
# plain HTTP to the signing proxy, which owns the credential.
#
# Built and pushed to the in-cluster Harbor osdc project by the module's deploy.sh
# under a content-hash tag — not built by hand. See the module README.
FROM public.ecr.aws/docker/library/debian:12-slim

RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        ca-certificates \
        git \
        python3 \
    && rm -rf /var/lib/apt/lists/*

COPY sandbox.py task.py /usr/local/bin/
RUN chmod 0755 /usr/local/bin/sandbox.py /usr/local/bin/task.py

# task.py imports sandbox.py from the same directory.
ENV PYTHONPATH=/usr/local/bin

# UID 1000, non-root. The pod securityContext enforces this too.
USER 1000

ENTRYPOINT ["python3", "/usr/local/bin/task.py"]
