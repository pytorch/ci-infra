# Sandbox dispatcher image — the trusted side of the sandbox.
#
# It receives /run, verifies the caller's GitHub OIDC token, creates one Kubernetes Job
# per request (the task image, under gVisor), and reads the result back from the pod's
# log. It holds K8s RBAC scoped to
# jobs + pods + pods/log in ai-sandbox, and no AWS identity: it never clones and never
# prompts a model, so the untrusted work stays in the task pods.
#
# Separate image from agent/ on purpose — a shared image would ship the dispatcher's
# dependencies inside the untrusted sandbox. That is what the split buys, and it is why
# adding a JWT library here costs the sandbox nothing.
#
# The same image also runs the JWKS refresher CronJob, under a different command and a
# different service account with no create-Job RBAC. One image, two callers.
#
# Built and pushed to the in-cluster Harbor osdc project by the module's deploy.sh
# under a content-hash tag — not built by hand. See the module README.
FROM public.ecr.aws/docker/library/debian:12-slim

# python3-jwt and python3-cryptography, from apt rather than pip, so the image keeps its
# no-pip property. They are here because GitHub signs its OIDC tokens RS256 and the
# Python standard library has no public-key crypto at all — see dispatcher/oidc.py for
# why a hand-rolled verifier was considered and rejected. This does not weaken the
# separate-image boundary below: the untrusted sandbox image is built from agent/ and
# gains nothing from this line.
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        ca-certificates \
        python3 \
        python3-cryptography \
        python3-jwt \
    && rm -rf /var/lib/apt/lists/*

# The modules are listed rather than globbed so the image cannot pick up a test file,
# and so adding one is a visible line in review.
COPY __main__.py http_api.py tasks.py kube.py oidc.py authorize.py jwks_refresh.py \
     /usr/local/lib/sandbox-dispatcher/

# UID 1000, non-root. The pod securityContext enforces this too.
USER 1000

EXPOSE 8080
# `python3 <dir>` runs <dir>/__main__.py with <dir> prepended to sys.path, so the
# modules import each other by bare name with no package, no PYTHONPATH and no
# installation step.
ENTRYPOINT ["python3", "/usr/local/lib/sandbox-dispatcher"]
