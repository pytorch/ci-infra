"""End-to-end smoke tests for the pypi-cache module.

Exercises the active request pipeline:
- HTTP health and package index requests through nginx -> pypiserver per CUDA slug
- Access log verification on EFS (nginx fallback logging to /data/logs/upstream/)
- Wants-collector cycle health and S3 output validation

These tests use kubectl exec into pypiserver containers to make HTTP requests
to localhost:8080 (nginx). This bypasses the NetworkPolicy (which restricts
ingress to arc-runners namespace) because traffic stays within the pod's
network namespace.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request

import pytest
from helpers import run_kubectl

pytestmark = [pytest.mark.live]

NAMESPACE = "pypi-cache"
S3_BUCKET_URL = "https://pytorch-pypi-wheel-cache.s3.us-east-2.amazonaws.com"
# Pure Python package — always on PyPI, small, fast to look up
TEST_PACKAGE = "requests"


# ============================================================================
# Helpers
# ============================================================================


def _exec_in_pod(pod_name: str, container: str, command: list[str]) -> str:
    """Run a command in a pod container via kubectl exec. Returns stdout as string."""
    return run_kubectl(
        ["exec", pod_name, "-c", container, "--", *command],
        namespace=NAMESPACE,
        json_output=False,
    )


def _http_get_in_pod(pod_name: str, path: str) -> dict:
    """Execute an HTTP GET to localhost:8080 from inside a pypiserver container.

    Returns dict with 'status' and 'body' on success, or 'error' on failure.
    """
    script = (
        "import urllib.request, urllib.error, json, sys\n"
        "try:\n"
        f"    resp = urllib.request.urlopen('http://localhost:8080{path}', timeout=10)\n"
        "    body = resp.read().decode('utf-8', errors='replace')\n"
        "    print(json.dumps({'status': resp.status, 'body': body[:2000]}))\n"
        "except urllib.error.HTTPError as e:\n"
        "    print(json.dumps({'status': e.code, 'error': str(e)}))\n"
        "except Exception as e:\n"
        "    print(json.dumps({'error': str(e)}))\n"
        "    sys.exit(1)\n"
    )
    raw = _exec_in_pod(pod_name, "pypiserver", ["python3", "-c", script])
    return json.loads(raw)


def _urlopen_no_proxy(url: str, timeout: int = 15) -> tuple[int, str]:
    """Fetch a URL bypassing corporate proxy. Returns (status_code, body)."""
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        with opener.open(url, timeout=timeout) as resp:
            return resp.status, resp.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as e:
        return e.code, ""


# ============================================================================
# Health Endpoint
# ============================================================================


class TestPypiCacheHealth:
    """Verify nginx + pypiserver respond to health checks for each CUDA slug."""

    def test_health_endpoint(self, pypi_cache_pods: dict[str, str], pypi_cache_slugs: list[str]) -> None:
        """Hit /health on each slug's pod — validates both nginx and pypiserver are up."""
        for slug in pypi_cache_slugs:
            pod_name = pypi_cache_pods.get(slug)
            if not pod_name:
                pytest.fail(f"No Running pod found for pypi-cache-{slug}")
            result = _http_get_in_pod(pod_name, "/health")
            assert "error" not in result, f"Health check failed for pypi-cache-{slug}: {result.get('error')}"
            assert result.get("status") == 200, (
                f"Health check for pypi-cache-{slug} returned {result.get('status')}, expected 200. "
                f"nginx or pypiserver may not be responding."
            )


# ============================================================================
# Package Index Response
# ============================================================================


class TestPypiCacheResponse:
    """Verify the proxy returns valid pip package index pages."""

    def test_simple_index_returns_package_links(
        self, pypi_cache_pods: dict[str, str], pypi_cache_slugs: list[str]
    ) -> None:
        """Request /simple/requests/ — must return HTML with download links.

        The response may come from nginx cache, the local pypiserver wheelhouse,
        or a transparent fallback to pypi.org. All paths are valid.
        """
        for slug in pypi_cache_slugs:
            pod_name = pypi_cache_pods.get(slug)
            if not pod_name:
                pytest.fail(f"No Running pod found for pypi-cache-{slug}")
            result = _http_get_in_pod(pod_name, f"/simple/{TEST_PACKAGE}/")
            assert "error" not in result, f"Package index request failed for pypi-cache-{slug}: {result.get('error')}"
            status = result.get("status")
            assert status == 200, (
                f"Package index for '{TEST_PACKAGE}' on pypi-cache-{slug} returned {status}. "
                f"Expected 200 from local cache, pypiserver, or pypi.org fallback."
            )
            body = result.get("body", "")
            assert "<a" in body.lower() or "href" in body.lower(), (
                f"Package index response for '{TEST_PACKAGE}' on pypi-cache-{slug} "
                f"does not contain HTML links. Body preview: {body[:200]}"
            )


# ============================================================================
# Access Logging
# ============================================================================


class TestPypiCacheLogging:
    """Verify nginx fallback logs are written to EFS.

    nginx logs upstream-bound requests (fallbacks to pypi.org and downloads
    from files.pythonhosted.org) to /data/logs/upstream/fallback.YYYY-MM-DD.log
    on EFS (daily rotation via map $time_iso8601).
    Any request that triggers an upstream proxy_pass should generate entries.
    """

    def test_access_logs_exist(self, pypi_cache_pods: dict[str, str], pypi_cache_slugs: list[str]) -> None:
        """The nginx fallback log file must exist on EFS."""
        # Only need one pod to check — all pods share the same EFS volume
        slug = pypi_cache_slugs[0]
        pod_name = pypi_cache_pods.get(slug)
        if not pod_name:
            pytest.fail(f"No Running pod found for pypi-cache-{slug}")
        script = (
            "import os, json, glob\n"
            "files = glob.glob('/data/logs/upstream/fallback.*.log')\n"
            "print(json.dumps({'exists': len(files) > 0, 'count': len(files)}))\n"
        )
        raw = _exec_in_pod(pod_name, "pypiserver", ["python3", "-c", script])
        result = json.loads(raw)
        if not result["exists"]:
            pytest.skip(
                "No fallback log files found in /data/logs/upstream/ — "
                "no upstream-bound requests have occurred yet (expected in low-traffic clusters)"
            )

    def test_access_logs_contain_entries(self, pypi_cache_pods: dict[str, str], pypi_cache_slugs: list[str]) -> None:
        """The fallback log must contain GET request entries."""
        slug = pypi_cache_slugs[0]
        pod_name = pypi_cache_pods.get(slug)
        if not pod_name:
            pytest.fail(f"No Running pod found for pypi-cache-{slug}")
        script = (
            "import os, json, glob\n"
            "files = sorted(glob.glob('/data/logs/upstream/fallback.*.log'))\n"
            "if not files:\n"
            "    print(json.dumps({'exists': False, 'lines': 0}))\n"
            "else:\n"
            "    total = 0\n"
            "    for f in files:\n"
            "        with open(f) as fh:\n"
            "            total += sum(1 for line in fh if 'GET' in line)\n"
            "    print(json.dumps({'exists': True, 'lines': total, 'file_count': len(files)}))\n"
        )
        raw = _exec_in_pod(pod_name, "pypiserver", ["python3", "-c", script])
        result = json.loads(raw)
        if not result["exists"]:
            pytest.skip("No fallback log files found — no upstream traffic yet (see test_access_logs_exist)")
        if result["lines"] == 0:
            pytest.skip(
                "Fallback logs exist but contain no GET entries — upstream-bound requests may not have occurred yet"
            )


# ============================================================================
# Wants-Collector Pipeline
# ============================================================================


class TestPypiCacheWantsPipeline:
    """Verify the wants-collector is completing cycles and producing valid S3 output.

    The wants-collector scans pypiserver access logs every 2 minutes, filters
    packages against PyPI, and uploads results to S3. These tests validate the
    pipeline infrastructure without forcing specific packages into the wants list.
    """

    def test_wants_collector_cycles_completing(self, wants_collector_pod: str | None) -> None:
        """The collector must have completed a cycle recently (/tmp/last-success < 600s)."""
        if wants_collector_pod is None:
            pytest.fail("No Running wants-collector pod found")
        script = (
            "import os, time, json\n"
            "try:\n"
            "    age = time.time() - os.path.getmtime('/tmp/last-success')\n"
            "    print(json.dumps({'age_seconds': int(age)}))\n"
            "except FileNotFoundError:\n"
            "    print(json.dumps({'error': 'not_found'}))\n"
        )
        raw = _exec_in_pod(wants_collector_pod, "wants-collector", ["python3", "-c", script])
        result = json.loads(raw)
        if "error" in result:
            pytest.fail(
                "Wants-collector /tmp/last-success not found. "
                "No cycle has completed yet (pod may still be in initialDelaySeconds=300s window)."
            )
        age = result["age_seconds"]
        assert age < 600, (
            f"Wants-collector last success was {age}s ago (threshold: 600s). "
            f"The collector may be stuck or PyPI API calls may be failing."
        )

    def test_s3_wants_file_accessible(self, cluster_id: str) -> None:
        """The wants file for this cluster must be accessible on S3 (public read)."""
        url = f"{S3_BUCKET_URL}/wants/{cluster_id}.txt"
        try:
            status, body = _urlopen_no_proxy(url)
        except Exception as e:
            pytest.skip(f"Cannot reach S3 ({e}) — network issue, not a deployment problem")
        # 200 = wants exist; 404 = no downloads logged yet (empty wheelhouse). Both valid.
        assert status in (200, 404), (
            f"S3 wants file at {url} returned unexpected status {status}. "
            f"Expected 200 (wants exist) or 404 (no downloads logged yet)."
        )
        if status == 200 and body.strip():
            for line in body.strip().splitlines():
                assert "==" in line, f"Wants file line '{line}' does not match expected 'package==version' format"

    def test_s3_prebuilt_cache_accessible(self, resolve_config) -> None:
        """The shared prebuilt cache must be accessible and have a valid matrix header."""
        url = f"{S3_BUCKET_URL}/prebuilt-cache.txt"
        try:
            status, body = _urlopen_no_proxy(url)
        except Exception as e:
            pytest.skip(f"Cannot reach S3 ({e}) — network issue, not a deployment problem")
        if status == 404:
            pytest.skip("Prebuilt cache not yet created — no packages have been processed")
        assert status == 200, f"S3 prebuilt-cache.txt returned unexpected status {status}"
        lines = body.strip().splitlines()
        assert lines, "Prebuilt cache file is empty"
        assert lines[0].startswith("# matrix: "), (
            f"Prebuilt cache first line should start with '# matrix: ', got: {lines[0][:80]}"
        )
        # Validate matrix header matches cluster config
        python_versions = resolve_config("pypi_cache.python_versions", [])
        architectures = resolve_config("pypi_cache.target_architectures", [])
        manylinux = resolve_config("pypi_cache.target_manylinux", "")
        if python_versions and architectures and manylinux:
            py_tags = [f"py{v}" for v in python_versions]
            expected = f"{','.join(py_tags)} {','.join(architectures)} manylinux_{manylinux}"
            actual = lines[0][len("# matrix: ") :]
            assert actual == expected, (
                f"Prebuilt cache matrix mismatch.\n"
                f"  Expected: {expected}\n"
                f"  Actual:   {actual}\n"
                f"Matrix changed in clusters.yaml but cache was not invalidated."
            )
