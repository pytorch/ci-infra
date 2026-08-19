# Sandbox dispatcher image — the trusted side of the sandbox.
#
# It receives /run, creates one Kubernetes Job per request (the task image, under
# gVisor), and reads the result back from the pod's log. It holds K8s RBAC scoped to
# jobs + pods + pods/log in ai-sandbox, and no AWS identity: it never clones and never
# prompts a model, so the untrusted work stays in the task pods.
#
# Separate image from agent/ on purpose — a shared image would ship the dispatcher's
# dependencies inside the untrusted sandbox. This one is stdlib-only too (urllib
# against the in-cluster API, like base/kubernetes/node-taint-remover), so the split
# costs a Dockerfile and buys that boundary.
#
# Built and pushed to the in-cluster Harbor osdc project by the module's deploy.sh
# under a content-hash tag — not built by hand. See the module README.
FROM public.ecr.aws/docker/library/debian:12-slim

RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        ca-certificates \
        python3 \
    && rm -rf /var/lib/apt/lists/*

COPY dispatcher.py /usr/local/bin/dispatcher.py
RUN chmod 0755 /usr/local/bin/dispatcher.py

# UID 1000, non-root. The pod securityContext enforces this too.
USER 1000

EXPOSE 8080
ENTRYPOINT ["python3", "/usr/local/bin/dispatcher.py"]
