"""Pod scanning and classification of Harbor proxy-cache pull failures."""

import logging
import re
from datetime import UTC, datetime

from lightkube import Client
from lightkube.resources.core_v1 import Pod

log = logging.getLogger("harbor-cache-recovery")

REGISTRY_TO_PROJECT = {
    "docker.io": "dockerhub-cache",
    "ghcr.io": "ghcr-cache",
    "public.ecr.aws": "ecr-public-cache",
    "nvcr.io": "nvcr-cache",
    "registry.k8s.io": "k8s-cache",
    "quay.io": "quay-cache",
}

# Manifest-level faults only. Deleting an artifact drops DB rows and queues an
# artifact_trash entry (src/controller/artifact/controller.go deleteDeeply); the blob
# itself survives until Harbor GC runs, and src/controller/proxy/local.go UseLocalBlob
# re-serves it straight from the registry without consulting those rows. So a
# layer-blob fault cannot be repaired by purging and must not be listed here.
CACHE_CORRUPTION_INDICATORS = (
    "failed size validation",  # core/metadata/content.go:670,685
    "unexpected commit digest",  # plugins/content/local/writer.go:135
    "unexpected commit size",  # plugins/content/local/writer.go:112
    "unexpected digest",  # core/metadata/content.go:673
    "short read: expected",  # core/content/helpers.go:221
)

# Node-local faults reach the pull path wrapped in whatever error containerd was
# building at the time (core/unpack/unpacker.go:545 wraps fsApplier.Apply with %w and
# no error-class filter), so an allow-list string can front them. The cache is healthy;
# purging evicts good artifacts for every other consumer. Strings are the Go
# syscall.Errno renderings.
NEVER_PURGE_INDICATORS = (
    "unexpected media type",  # core/images/image.go:242
    "no space left on device",  # ENOSPC
    "input/output error",  # EIO
    "disk quota exceeded",  # EDQUOT
    "read-only file system",  # EROFS
    "too many open files",  # EMFILE/ENFILE
)

# OCI grammars. Anything else cannot be trusted into the path of a DELETE: "." and ".."
# survive percent-encoding unchanged and urllib3 normalizes them away on the wire,
# turning an artifact-scoped delete into a repository-scoped one.
TAG_PATTERN = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9._-]{0,127}$")
DIGEST_PATTERN = re.compile(r"^sha256:[0-9a-f]{64}$")


def parse_image_reference(image: str) -> tuple[str, str, str] | None:
    """Parse image into (registry, repo_path, reference). Returns None for unknown registries.

    The tag wins over the digest when the image carries both. Harbor's proxy rebuilds
    multi-arch indexes from the children it has cached and stores the result under its
    own recomputed digest (src/controller/proxy/manifestcache.go updateManifestList),
    so the upstream digest a pod pins usually addresses nothing in Harbor.

    >>> parse_image_reference("grafana/alloy:v1.14.0")
    ('docker.io', 'grafana/alloy', 'v1.14.0')
    >>> parse_image_reference("ghcr.io/actions/runner:latest")
    ('ghcr.io', 'actions/runner', 'latest')
    >>> parse_image_reference("nginx")
    ('docker.io', 'library/nginx', 'latest')
    >>> parse_image_reference("ghcr.io/actions/runner@sha256:abc123")
    ('ghcr.io', 'actions/runner', 'sha256:abc123')
    >>> parse_image_reference("ghcr.io/actions/actions-runner:2.336.0@sha256:0cfdcc70")
    ('ghcr.io', 'actions/actions-runner', '2.336.0')
    """
    name, _, digest = image.partition("@")

    parts = name.split("/")
    last = parts[-1]
    tag = ""
    if ":" in last:
        parts[-1], tag = last.rsplit(":", 1)
    ref = "/".join(parts)

    parts = ref.split("/", 1)
    if len(parts) == 1:
        registry, repo_path = "docker.io", f"library/{parts[0]}"
    elif "." in parts[0] or ":" in parts[0]:
        registry, repo_path = parts[0], parts[1]
    else:
        registry, repo_path = "docker.io", ref

    if registry not in REGISTRY_TO_PROJECT:
        return None
    return registry, repo_path, tag or digest or "latest"


def is_digest(reference: str) -> bool:
    # OCI tag grammar forbids ":", so a colon can only come from a digest.
    return ":" in reference


def _is_valid_reference(reference: str) -> bool:
    return bool(TAG_PATTERN.match(reference) or DIGEST_PATTERN.match(reference))


def artifact_key(failure: dict) -> str:
    return f"{failure['harbor_project']}/{failure['repo_path']}:{failure['reference']}"


def _extract_waiting_failures(statuses: list | None) -> list[dict]:
    """Extract ImagePullBackOff entries matching a cache indicator.

    Entries are tagged ``purgeable``. A NEVER_PURGE_INDICATORS match wins over any
    corruption match: containerd derives the digest from the served body for those,
    so the cache commits cleanly and there is no corrupt artifact to evict.
    """
    if not statuses:
        return []
    results = []
    for cs in statuses:
        waiting = getattr(cs, "state", None)
        waiting = getattr(waiting, "waiting", None) if waiting else None
        if not waiting:
            continue
        reason = getattr(waiting, "reason", None) or ""
        if reason not in ("ImagePullBackOff", "ErrImagePull"):
            continue
        message = getattr(waiting, "message", None) or ""
        if any(ind in message for ind in NEVER_PURGE_INDICATORS):
            purgeable = False
        elif any(ind in message for ind in CACHE_CORRUPTION_INDICATORS):
            purgeable = True
        else:
            continue
        image = getattr(cs, "image", "") or ""
        if not image:
            log.warning("Cache indicator matched but the container reports no image; skipping")
            continue
        results.append({"image": image, "message": message, "purgeable": purgeable})
    return results


def find_pull_failures(client: Client, min_pod_age_seconds: int) -> list[dict]:
    """Find containers with cache-related ImagePullBackOff errors.

    Returns list of dicts: pod_name, namespace, image, harbor_project, repo_path,
    reference, message, purgeable.
    """
    now = datetime.now(UTC)
    failures = []

    for pod in client.list(Pod, namespace="*"):
        created = getattr(pod.metadata, "creationTimestamp", None)
        if created is None:
            continue
        if created.tzinfo is None:
            created = created.replace(tzinfo=UTC)
        if (now - created).total_seconds() < min_pod_age_seconds:
            continue

        status = pod.status
        if not status:
            continue

        all_entries = []
        all_entries.extend(_extract_waiting_failures(getattr(status, "containerStatuses", None)))
        all_entries.extend(_extract_waiting_failures(getattr(status, "initContainerStatuses", None)))

        for entry in all_entries:
            parsed = parse_image_reference(entry["image"])
            if parsed is None:
                continue
            registry, repo_path, reference = parsed
            if not _is_valid_reference(reference):
                log.warning(
                    "Skipping %s/%s: image %s yields reference %r, which is neither a valid tag nor a digest",
                    pod.metadata.namespace,
                    pod.metadata.name,
                    entry["image"],
                    reference,
                )
                continue
            failures.append(
                {
                    "pod_name": pod.metadata.name,
                    "namespace": pod.metadata.namespace,
                    "image": entry["image"],
                    "harbor_project": REGISTRY_TO_PROJECT[registry],
                    "repo_path": repo_path,
                    "reference": reference,
                    # Collapsed to one line before it reaches the log: the message
                    # echoes upstream registry error bodies, and an embedded newline
                    # would let those forge a log record.
                    "message": " ".join(entry["message"].split())[:200],
                    "purgeable": entry["purgeable"],
                }
            )

    return failures


def log_detections(failures: list[dict]) -> None:
    """Log purgeable failures per pod, and never-purgeable ones per distinct artifact.

    The never-purgeable class is the bulk of real failures and carries the full
    containerd message, so one line per pod would bury everything else in the log
    during exactly the incident someone is reading it for.
    """
    blocked: dict[str, list[dict]] = {}
    for f in failures:
        if f["purgeable"]:
            log.info("Detected: %s/%s image=%s", f["namespace"], f["pod_name"], f["image"])
        else:
            blocked.setdefault(artifact_key(f), []).append(f)

    for key, group in blocked.items():
        first = group[0]
        log.info(
            "Detected on %d pod(s), not purgeable (needs investigation): %s image=%s example_pod=%s/%s message=%s",
            len(group),
            key,
            first["image"],
            first["namespace"],
            first["pod_name"],
            first["message"],
        )


def select_purge_targets(failures: list[dict]) -> dict[str, dict]:
    """Deduplicate purgeable failures by artifact. A deny match wins for the whole run.

    Classification is per message but the purge is per artifact, so an artifact seen
    with a never-purge message anywhere in the run must be excluded even when another
    pod reported corruption for it. Both message shapes come from the same upstream
    serving bad bytes, so the pairing is a realistic coincidence rather than a
    contrived one, and the deny classification is the one that has to hold.
    """
    denied = {artifact_key(f) for f in failures if not f["purgeable"]}
    for key in sorted(denied & {artifact_key(f) for f in failures if f["purgeable"]}):
        log.warning("Corruption match for %s ignored: the artifact also matched a never-purge indicator", key)

    targets: dict[str, dict] = {}
    for f in failures:
        key = artifact_key(f)
        if f["purgeable"] and key not in denied:
            targets.setdefault(key, f)
    return targets
