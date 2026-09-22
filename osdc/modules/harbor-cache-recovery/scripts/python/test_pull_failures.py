"""Tests for pull_failures: pod scanning and failure classification.

Also holds the verbatim containerd messages and pod/container mock builders shared
with the other test modules in this directory.
"""

import logging
from datetime import UTC, datetime, timedelta
from unittest.mock import MagicMock

import pytest
from pull_failures import (
    CACHE_CORRUPTION_INDICATORS,
    NEVER_PURGE_INDICATORS,
    REGISTRY_TO_PROJECT,
    _extract_waiting_failures,
    find_pull_failures,
    log_detections,
    parse_image_reference,
    select_purge_targets,
)

# Verbatim kubelet waiting messages observed in the fleet.

CORRUPTION_WITH_PRECONDITION = (
    'rpc error: code = FailedPrecondition desc = failed to pull and unpack image "docker.io/grafana/alloy:v1.14.0": '
    'failed commit on ref "index-sha256:f509...": "index-sha256:f509..." failed size validation: 347532 != 3117: '
    "failed precondition"
)

HTML_MEDIA_TYPE = (
    'rpc error: code = NotFound desc = failed to pull and unpack image "docker.io/rclone/rclone:1.69.1": '
    "failed to unpack image on snapshotter overlayfs: unexpected media type text/html for sha256:c29d0c84...: "
    "not found"
)

TLS_FAULT = "failed to copy: local error: tls: bad record MAC"

DISK_FULL = (
    "failed to copy: write /var/lib/containerd/io.containerd.content.v1.content/ingest/a1b2c3/data: "
    "no space left on device"
)

BARE_PRECONDITION = "rpc error: code = FailedPrecondition desc = failed precondition"

UNEXPECTED_CONTENT_DIGEST = "unexpected content digest sha256:abc"

# Layer-blob faults. Purging drops DB rows but not the blob, and Harbor re-serves the
# same bytes, so neither of these may reach the purge path.
LAYER_BLOB_FAULT = (
    'failed to extract layer (application/vnd.oci.image.layer.v1.tar+gzip sha256:abc) to overlayfs as "extract-1": '
    "gzip: invalid header"
)

WRONG_DIFF_ID = 'wrong diff id "sha256:aaa" calculated on extraction "sha256:bbb", desc "sha256:ccc"'

VALID_DIGEST = "sha256:" + "0cfdcc701ce933c6d243c6b0b2da767366dc9f2e99961d4c3754b0b78084cdda"

# The tag@digest form used by all 44 manifests under modules/arc-runners/generated/.
PINNED_RUNNER_IMAGE = "ghcr.io/actions/actions-runner:2.336.0@sha256:0cfdcc701ce933c6d243c6b0b2da767366dc9f2e99961d4c"

LAYER_ENOSPC = (
    'failed to extract layer (application/vnd.oci.image.layer.v1.tar+gzip sha256:abc) to overlayfs as "extract-1": '
    "write /var/lib/containerd/io.containerd.snapshotter.v1.overlayfs/snapshots/42/fs/bin/x: no space left on device"
)


def make_container_status(image="nginx:latest", reason=None, message=None):
    cs = MagicMock()
    cs.image = image
    if reason:
        cs.state.waiting.reason = reason
        cs.state.waiting.message = message or ""
    else:
        cs.state.waiting = None
    return cs


def make_pod(
    name="test-pod",
    namespace="default",
    age_seconds=300,
    container_statuses=None,
    init_container_statuses=None,
):
    now = datetime.now(UTC)
    pod = MagicMock()
    pod.metadata.name = name
    pod.metadata.namespace = namespace
    pod.metadata.creationTimestamp = now - timedelta(seconds=age_seconds)
    pod.status.containerStatuses = container_statuses
    pod.status.initContainerStatuses = init_container_statuses
    return pod


def failing_pod(image, message, name="pod-1", namespace="default"):
    cs = make_container_status(image=image, reason="ImagePullBackOff", message=message)
    return make_pod(name=name, namespace=namespace, container_statuses=[cs])


def client_listing(pods):
    client = MagicMock()
    client.list.return_value = pods
    return client


def _extract_one(message, reason="ImagePullBackOff", image="grafana/alloy:v1.14.0"):
    return _extract_waiting_failures([make_container_status(image=image, reason=reason, message=message)])


def _failure(project="dockerhub-cache", repo="grafana/alloy", ref="v1.14.0", purgeable=True, pod="pod-1"):
    return {
        "pod_name": pod,
        "namespace": "default",
        "image": f"{repo}:{ref}",
        "harbor_project": project,
        "repo_path": repo,
        "reference": ref,
        "message": "m",
        "purgeable": purgeable,
    }


# ============================================================================
# parse_image_reference
# ============================================================================


class TestParseImageReference:
    def test_docker_hub_short(self):
        assert parse_image_reference("nginx") == ("docker.io", "library/nginx", "latest")

    def test_docker_hub_short_with_tag(self):
        assert parse_image_reference("nginx:latest") == ("docker.io", "library/nginx", "latest")

    def test_docker_hub_org(self):
        assert parse_image_reference("grafana/alloy:v1.14.0") == ("docker.io", "grafana/alloy", "v1.14.0")

    def test_docker_hub_explicit(self):
        assert parse_image_reference("docker.io/grafana/alloy:v1.14.0") == ("docker.io", "grafana/alloy", "v1.14.0")

    def test_ghcr(self):
        assert parse_image_reference("ghcr.io/actions/runner:latest") == ("ghcr.io", "actions/runner", "latest")

    def test_quay(self):
        result = parse_image_reference("quay.io/prometheus-operator/prometheus-config-reloader:v0.81.0")
        assert result == ("quay.io", "prometheus-operator/prometheus-config-reloader", "v0.81.0")

    def test_k8s_registry(self):
        assert parse_image_reference("registry.k8s.io/pause:3.9") == ("registry.k8s.io", "pause", "3.9")

    def test_nvcr(self):
        assert parse_image_reference("nvcr.io/nvidia/cuda:12.0") == ("nvcr.io", "nvidia/cuda", "12.0")

    def test_ecr_public(self):
        result = parse_image_reference("public.ecr.aws/docker/library/nginx:latest")
        assert result == ("public.ecr.aws", "docker/library/nginx", "latest")

    def test_digest_reference(self):
        result = parse_image_reference("ghcr.io/actions/runner@sha256:abc123")
        assert result == ("ghcr.io", "actions/runner", "sha256:abc123")

    def test_tag_wins_over_digest(self):
        result = parse_image_reference("ghcr.io/actions/runner:v3@sha256:abc123")
        assert result == ("ghcr.io", "actions/runner", "v3")

    def test_pinned_runner_manifest_form_uses_tag(self):
        assert parse_image_reference(PINNED_RUNNER_IMAGE) == ("ghcr.io", "actions/actions-runner", "2.336.0")

    def test_unknown_registry_returns_none(self):
        assert parse_image_reference("my-private-registry.com/app:v1") is None

    def test_localhost_returns_none(self):
        assert parse_image_reference("localhost:30002/osdc/image:tag") is None

    def test_harbor_hostname_returns_none(self):
        assert parse_image_reference("harbor:30002/osdc/image:tag") is None

    def test_no_tag(self):
        assert parse_image_reference("grafana/alloy") == ("docker.io", "grafana/alloy", "latest")

    def test_deep_path(self):
        result = parse_image_reference("ghcr.io/org/sub/image:v1")
        assert result == ("ghcr.io", "org/sub/image", "v1")

    def test_docker_hub_library_explicit(self):
        assert parse_image_reference("docker.io/library/nginx:latest") == ("docker.io", "library/nginx", "latest")


# ============================================================================
# _extract_waiting_failures
# ============================================================================


class TestExtractWaitingFailures:
    def test_none_statuses(self):
        assert _extract_waiting_failures(None) == []

    def test_empty_list(self):
        assert _extract_waiting_failures([]) == []

    def test_running_container_skipped(self):
        assert _extract_waiting_failures([make_container_status()]) == []

    def test_imagepullbackoff_without_corruption_skipped(self):
        assert _extract_one("unauthorized: authentication required") == []

    def test_other_waiting_reasons_skipped(self):
        assert _extract_one("failed size validation", reason="CrashLoopBackOff") == []

    @pytest.mark.parametrize("indicator", CACHE_CORRUPTION_INDICATORS)
    def test_every_corruption_indicator_is_purgeable(self, indicator):
        results = _extract_one(f'failed to pull and unpack image "docker.io/grafana/alloy:v1.14.0": {indicator} foo')
        assert len(results) == 1
        assert results[0]["purgeable"] is True
        assert results[0]["image"] == "grafana/alloy:v1.14.0"

    def test_errimagepull_reason_accepted(self):
        assert len(_extract_one("failed size validation: 1 != 2", reason="ErrImagePull")) == 1

    def test_real_corruption_with_trailing_precondition(self):
        results = _extract_one(CORRUPTION_WITH_PRECONDITION)
        assert len(results) == 1
        assert results[0]["purgeable"] is True

    @pytest.mark.parametrize(
        "message",
        [TLS_FAULT, BARE_PRECONDITION, UNEXPECTED_CONTENT_DIGEST],
        ids=["tls_fault", "bare_precondition", "unexpected_content_digest"],
    )
    def test_non_corruption_messages_are_ignored(self, message):
        assert _extract_one(message) == []

    @pytest.mark.parametrize(
        "message",
        [LAYER_BLOB_FAULT, WRONG_DIFF_ID, "failed to extract layer sha256:abc"],
        ids=["invalid_gzip", "wrong_diff_id", "bare_extract_layer"],
    )
    def test_layer_blob_faults_never_reach_the_purge_path(self, message):
        # A purge removes DB rows, not blobs, so these would loop forever.
        assert _extract_one(message) == []

    @pytest.mark.parametrize(
        "message",
        [HTML_MEDIA_TYPE, DISK_FULL, LAYER_ENOSPC],
        ids=["html_media_type", "disk_full_copy", "disk_full_through_unpack"],
    )
    def test_node_local_faults_detected_but_not_purgeable(self, message):
        results = _extract_one(message)
        assert len(results) == 1
        assert results[0]["purgeable"] is False

    @pytest.mark.parametrize("indicator", NEVER_PURGE_INDICATORS)
    def test_every_deny_indicator_vetoes_a_corruption_match(self, indicator):
        # Each deny string must win even when a real corruption string is also present.
        results = _extract_one(f"{CORRUPTION_WITH_PRECONDITION} :: {indicator}")
        assert len(results) == 1
        assert results[0]["purgeable"] is False

    def test_multiline_message_still_matches(self):
        message = (
            'failed to pull and unpack image "docker.io/grafana/alloy:v1.14.0"\n'
            'failed commit on ref "layer-sha256:abc"\n'
            "failed size validation: 347532 != 3117"
        )
        results = _extract_one(message)
        assert len(results) == 1
        assert results[0]["purgeable"] is True

    def test_multiline_deny_list_still_matches(self):
        results = _extract_one("failed to unpack image on snapshotter overlayfs:\nunexpected media type text/html")
        assert len(results) == 1
        assert results[0]["purgeable"] is False


# ============================================================================
# find_pull_failures
# ============================================================================


class TestFindPullFailures:
    def test_no_pods(self):
        assert find_pull_failures(client_listing([]), 120) == []

    def test_skips_young_pods(self):
        cs = make_container_status(
            image="grafana/alloy:v1.14.0",
            reason="ImagePullBackOff",
            message="failed size validation: 348055 != 1621",
        )
        pod = make_pod(age_seconds=60, container_statuses=[cs])
        assert find_pull_failures(client_listing([pod]), 120) == []

    def test_detects_corruption(self):
        cs = make_container_status(
            image="grafana/alloy:v1.14.0",
            reason="ImagePullBackOff",
            message=CORRUPTION_WITH_PRECONDITION,
        )
        pod = make_pod(name="alloy-abc", namespace="logging", container_statuses=[cs])
        results = find_pull_failures(client_listing([pod]), 120)
        assert len(results) == 1
        assert results[0]["pod_name"] == "alloy-abc"
        assert results[0]["namespace"] == "logging"
        assert results[0]["harbor_project"] == "dockerhub-cache"
        assert results[0]["repo_path"] == "grafana/alloy"
        assert results[0]["reference"] == "v1.14.0"
        assert results[0]["purgeable"] is True

    def test_carries_digest_reference(self):
        cs = make_container_status(
            image=f"ghcr.io/actions/runner@{VALID_DIGEST}",
            reason="ImagePullBackOff",
            message="failed size validation: 1 != 2",
        )
        pod = make_pod(container_statuses=[cs])
        results = find_pull_failures(client_listing([pod]), 120)
        assert results[0]["reference"] == VALID_DIGEST

    @pytest.mark.parametrize("bad_tag", ["..", ".", "-nope"], ids=["dotdot", "dot", "leading_dash"])
    def test_ungrammatical_reference_is_skipped(self, bad_tag):
        cs = make_container_status(
            image=f"grafana/alloy:{bad_tag}",
            reason="ImagePullBackOff",
            message="failed size validation: 1 != 2",
        )
        pod = make_pod(container_statuses=[cs])
        assert find_pull_failures(client_listing([pod]), 120) == []

    def test_truncated_digest_is_skipped(self):
        cs = make_container_status(
            image="ghcr.io/actions/runner@sha256:abc123",
            reason="ImagePullBackOff",
            message="failed size validation: 1 != 2",
        )
        pod = make_pod(container_statuses=[cs])
        assert find_pull_failures(client_listing([pod]), 120) == []

    def test_empty_image_is_dropped(self):
        cs = make_container_status(
            image="",
            reason="ImagePullBackOff",
            message="failed size validation: 1 != 2",
        )
        pod = make_pod(container_statuses=[cs])
        assert find_pull_failures(client_listing([pod]), 120) == []

    def test_html_media_type_marked_not_purgeable(self):
        cs = make_container_status(
            image="rclone/rclone:1.69.1",
            reason="ImagePullBackOff",
            message=HTML_MEDIA_TYPE,
        )
        pod = make_pod(container_statuses=[cs])
        results = find_pull_failures(client_listing([pod]), 120)
        assert len(results) == 1
        assert results[0]["purgeable"] is False

    def test_truncates_long_messages(self):
        cs = make_container_status(
            image="grafana/alloy:v1.14.0",
            reason="ImagePullBackOff",
            message="failed size validation: " + "x" * 500,
        )
        pod = make_pod(container_statuses=[cs])
        assert len(find_pull_failures(client_listing([pod]), 120)[0]["message"]) == 200

    def test_message_is_collapsed_to_one_line(self):
        cs = make_container_status(
            image="grafana/alloy:v1.14.0",
            reason="ImagePullBackOff",
            message="failed size validation: 1 != 2\nWARNING forged log line\r\nsecond",
        )
        pod = make_pod(container_statuses=[cs])
        stored = find_pull_failures(client_listing([pod]), 120)[0]["message"]
        assert "\n" not in stored
        assert "\r" not in stored
        assert stored == "failed size validation: 1 != 2 WARNING forged log line second"

    def test_skips_auth_errors(self):
        cs = make_container_status(
            image="ghcr.io/private/repo:v1",
            reason="ImagePullBackOff",
            message="unauthorized: authentication required",
        )
        pod = make_pod(container_statuses=[cs])
        assert find_pull_failures(client_listing([pod]), 120) == []

    def test_skips_unknown_registry(self):
        cs = make_container_status(
            image="my-registry.com/app:v1",
            reason="ImagePullBackOff",
            message="failed size validation: 100 != 200",
        )
        pod = make_pod(container_statuses=[cs])
        assert find_pull_failures(client_listing([pod]), 120) == []

    def test_detects_init_container_failures(self):
        cs = make_container_status(
            image="quay.io/prom/node-exporter:v1.7.0",
            reason="ErrImagePull",
            message="short read: expected 512 bytes",
        )
        pod = make_pod(init_container_statuses=[cs])
        results = find_pull_failures(client_listing([pod]), 120)
        assert len(results) == 1
        assert results[0]["harbor_project"] == "quay-cache"
        assert results[0]["purgeable"] is True

    def test_handles_pod_without_status(self):
        pod = make_pod()
        pod.status = None
        assert find_pull_failures(client_listing([pod]), 120) == []

    def test_handles_pod_without_timestamp(self):
        pod = make_pod()
        pod.metadata.creationTimestamp = None
        assert find_pull_failures(client_listing([pod]), 120) == []

    def test_naive_timestamp_treated_as_utc(self):
        cs = make_container_status(
            image="grafana/alloy:v1.14.0",
            reason="ImagePullBackOff",
            message="failed size validation: 1 != 2",
        )
        pod = make_pod(container_statuses=[cs])
        pod.metadata.creationTimestamp = pod.metadata.creationTimestamp.replace(tzinfo=None)
        assert len(find_pull_failures(client_listing([pod]), 120)) == 1


# ============================================================================
# select_purge_targets
# ============================================================================


class TestSelectPurgeTargets:
    def test_empty(self):
        assert select_purge_targets([]) == {}

    def test_dedupes_by_artifact(self):
        failures = [_failure(pod=f"p{i}") for i in range(3)]
        assert list(select_purge_targets(failures)) == ["dockerhub-cache/grafana/alloy:v1.14.0"]

    def test_distinct_references_are_distinct_targets(self):
        failures = [_failure(ref="v1"), _failure(ref="v2")]
        assert len(select_purge_targets(failures)) == 2

    def test_denied_artifact_excluded(self):
        assert select_purge_targets([_failure(purgeable=False)]) == {}

    @pytest.mark.parametrize("deny_first", [True, False], ids=["deny_first", "allow_first"])
    def test_deny_wins_regardless_of_order(self, deny_first):
        deny = _failure(purgeable=False, pod="denied-pod")
        allow = _failure(purgeable=True, pod="allowed-pod")
        failures = [deny, allow] if deny_first else [allow, deny]
        assert select_purge_targets(failures) == {}

    def test_deny_does_not_leak_across_artifacts(self):
        failures = [
            _failure(ref="bad", purgeable=False),
            _failure(ref="good", purgeable=True),
        ]
        assert list(select_purge_targets(failures)) == ["dockerhub-cache/grafana/alloy:good"]

    def test_deny_is_scoped_by_project(self):
        failures = [
            _failure(project="ghcr-cache", purgeable=False),
            _failure(project="dockerhub-cache", purgeable=True),
        ]
        assert list(select_purge_targets(failures)) == ["dockerhub-cache/grafana/alloy:v1.14.0"]

    def test_override_is_logged_once(self, caplog):
        failures = [_failure(purgeable=False, pod=f"d{i}") for i in range(3)]
        failures += [_failure(purgeable=True, pod=f"a{i}") for i in range(3)]
        with caplog.at_level(logging.WARNING, logger="harbor-cache-recovery"):
            select_purge_targets(failures)
        assert len([r for r in caplog.records if "also matched a never-purge indicator" in r.getMessage()]) == 1


# ============================================================================
# log_detections
# ============================================================================


class TestLogDetections:
    def test_never_purgeable_collapsed_to_one_line(self, caplog):
        failures = [_failure(purgeable=False, pod=f"rclone-{i}") for i in range(50)]
        with caplog.at_level(logging.INFO, logger="harbor-cache-recovery"):
            log_detections(failures)
        blocked = [r for r in caplog.records if "not purgeable" in r.getMessage()]
        assert len(blocked) == 1
        assert "Detected on 50 pod(s)" in blocked[0].getMessage()
        assert "dockerhub-cache/grafana/alloy:v1.14.0" in blocked[0].getMessage()
        assert "example_pod=default/rclone-0" in blocked[0].getMessage()

    def test_distinct_never_purgeable_artifacts_logged_separately(self, caplog):
        failures = [_failure(ref="a", purgeable=False), _failure(ref="b", purgeable=False)]
        with caplog.at_level(logging.INFO, logger="harbor-cache-recovery"):
            log_detections(failures)
        assert len([r for r in caplog.records if "not purgeable" in r.getMessage()]) == 2

    def test_purgeable_logged_per_pod(self, caplog):
        failures = [_failure(pod=f"p{i}") for i in range(3)]
        with caplog.at_level(logging.INFO, logger="harbor-cache-recovery"):
            log_detections(failures)
        assert len([r for r in caplog.records if r.getMessage().startswith("Detected:")]) == 3


# ============================================================================
# Constants
# ============================================================================


class TestConstants:
    def test_all_registries_covered(self):
        expected = {"docker.io", "ghcr.io", "public.ecr.aws", "nvcr.io", "registry.k8s.io", "quay.io"}
        assert set(REGISTRY_TO_PROJECT.keys()) == expected

    def test_corruption_indicators_non_empty(self):
        assert len(CACHE_CORRUPTION_INDICATORS) >= 4

    def test_never_purge_indicators_non_empty(self):
        assert len(NEVER_PURGE_INDICATORS) >= 1

    def test_node_local_faults_are_denied(self):
        for errno_text in ("no space left on device", "input/output error"):
            assert errno_text in NEVER_PURGE_INDICATORS

    def test_indicators_are_not_substrings_of_each_other(self):
        for group in (CACHE_CORRUPTION_INDICATORS, NEVER_PURGE_INDICATORS):
            for outer in group:
                others = [i for i in group if i != outer]
                assert not any(inner in outer for inner in others), f"{outer} subsumes another indicator"

    def test_removed_indicators_are_gone(self):
        assert "failed to copy" not in CACHE_CORRUPTION_INDICATORS
        assert "failed precondition" not in CACHE_CORRUPTION_INDICATORS
        assert "unexpected content digest" not in CACHE_CORRUPTION_INDICATORS

    def test_no_blob_level_indicators(self):
        # Purging removes DB rows, not blobs, so a blob-level signal would loop forever.
        assert "failed to extract layer" not in CACHE_CORRUPTION_INDICATORS
        assert "wrong diff id" not in CACHE_CORRUPTION_INDICATORS
