"""Tests for is_daemonset_pod, pod_cpu_request, and pod_memory_request in models.py."""

import unittest
from unittest.mock import MagicMock

from models import is_daemonset_pod, pod_cpu_request, pod_memory_request


def _mock_pod(metadata=True, owner_refs=None, containers=None, spec=True):
    """Build a mock Pod with optional metadata, ownerReferences, spec, and containers.

    Args:
        metadata: If False/None, pod.metadata is set to that value.
                  If True (default), a MagicMock metadata object is created.
        owner_refs: Value for pod.metadata.ownerReferences (None, [], or list of refs).
        containers: Value for pod.spec.containers (None, [], or list of container mocks).
        spec: If False/None, pod.spec is set to that value.
              If True (default), a MagicMock spec object is created.
    """
    pod = MagicMock()

    if metadata is None or metadata is False:
        pod.metadata = None
    else:
        pod.metadata = MagicMock()
        pod.metadata.ownerReferences = owner_refs

    if spec is None or spec is False:
        pod.spec = None
    else:
        pod.spec = MagicMock()
        pod.spec.containers = containers

    return pod


def _mock_owner_ref(kind):
    """Build a mock ownerReference with the given kind."""
    ref = MagicMock()
    ref.kind = kind
    return ref


def _mock_container(cpu=None, memory=None, has_resources=True, has_requests=True):
    """Build a mock container with optional resource requests.

    Args:
        cpu: CPU request string (e.g. "500m", "2"). None means no cpu key.
        memory: Memory request string (e.g. "1Gi", "512Mi"). None means no memory key.
        has_resources: If False, container.resources is None.
        has_requests: If False, container.resources.requests is None.
    """
    container = MagicMock()

    if not has_resources:
        container.resources = None
        return container

    container.resources = MagicMock()

    if not has_requests:
        container.resources.requests = None
        return container

    requests = {}
    if cpu is not None:
        requests["cpu"] = cpu
    if memory is not None:
        requests["memory"] = memory
    container.resources.requests = requests

    return container


class TestIsDaemonsetPod(unittest.TestCase):
    """Tests for is_daemonset_pod."""

    def test_metadata_is_none(self):
        pod = _mock_pod(metadata=None)
        self.assertFalse(is_daemonset_pod(pod))

    def test_owner_references_is_none(self):
        pod = _mock_pod(owner_refs=None)
        self.assertFalse(is_daemonset_pod(pod))

    def test_owner_references_empty_list(self):
        pod = _mock_pod(owner_refs=[])
        self.assertFalse(is_daemonset_pod(pod))

    def test_owner_references_has_daemonset(self):
        pod = _mock_pod(owner_refs=[_mock_owner_ref("DaemonSet")])
        self.assertTrue(is_daemonset_pod(pod))

    def test_owner_references_has_non_daemonset(self):
        pod = _mock_pod(owner_refs=[_mock_owner_ref("ReplicaSet")])
        self.assertFalse(is_daemonset_pod(pod))

    def test_owner_references_mixed_includes_daemonset(self):
        refs = [_mock_owner_ref("ReplicaSet"), _mock_owner_ref("DaemonSet")]
        pod = _mock_pod(owner_refs=refs)
        self.assertTrue(is_daemonset_pod(pod))

    def test_owner_references_multiple_non_daemonset(self):
        refs = [_mock_owner_ref("ReplicaSet"), _mock_owner_ref("Job")]
        pod = _mock_pod(owner_refs=refs)
        self.assertFalse(is_daemonset_pod(pod))


class TestPodCpuRequest(unittest.TestCase):
    """Tests for pod_cpu_request."""

    def test_spec_is_none(self):
        pod = _mock_pod(spec=None)
        self.assertEqual(pod_cpu_request(pod), 0.0)

    def test_containers_is_none(self):
        pod = _mock_pod(containers=None)
        self.assertEqual(pod_cpu_request(pod), 0.0)

    def test_containers_empty_list(self):
        pod = _mock_pod(containers=[])
        self.assertEqual(pod_cpu_request(pod), 0.0)

    def test_container_no_resources(self):
        c = _mock_container(has_resources=False)
        pod = _mock_pod(containers=[c])
        self.assertEqual(pod_cpu_request(pod), 0.0)

    def test_container_no_requests(self):
        c = _mock_container(has_requests=False)
        pod = _mock_pod(containers=[c])
        self.assertEqual(pod_cpu_request(pod), 0.0)

    def test_container_requests_no_cpu_key(self):
        c = _mock_container(cpu=None, memory="1Gi")
        pod = _mock_pod(containers=[c])
        self.assertEqual(pod_cpu_request(pod), 0.0)

    def test_single_container_millicores(self):
        c = _mock_container(cpu="500m")
        pod = _mock_pod(containers=[c])
        self.assertAlmostEqual(pod_cpu_request(pod), 0.5)

    def test_single_container_whole_cores(self):
        c = _mock_container(cpu="2")
        pod = _mock_pod(containers=[c])
        self.assertAlmostEqual(pod_cpu_request(pod), 2.0)

    def test_multiple_containers_sum(self):
        c1 = _mock_container(cpu="250m")
        c2 = _mock_container(cpu="750m")
        pod = _mock_pod(containers=[c1, c2])
        self.assertAlmostEqual(pod_cpu_request(pod), 1.0)

    def test_mixed_containers_some_without_cpu(self):
        c1 = _mock_container(cpu="1")
        c2 = _mock_container(has_resources=False)
        c3 = _mock_container(cpu="500m")
        pod = _mock_pod(containers=[c1, c2, c3])
        self.assertAlmostEqual(pod_cpu_request(pod), 1.5)


class TestPodMemoryRequest(unittest.TestCase):
    """Tests for pod_memory_request."""

    def test_spec_is_none(self):
        pod = _mock_pod(spec=None)
        self.assertEqual(pod_memory_request(pod), 0)

    def test_containers_is_none(self):
        pod = _mock_pod(containers=None)
        self.assertEqual(pod_memory_request(pod), 0)

    def test_containers_empty_list(self):
        pod = _mock_pod(containers=[])
        self.assertEqual(pod_memory_request(pod), 0)

    def test_container_no_resources(self):
        c = _mock_container(has_resources=False)
        pod = _mock_pod(containers=[c])
        self.assertEqual(pod_memory_request(pod), 0)

    def test_container_no_requests(self):
        c = _mock_container(has_requests=False)
        pod = _mock_pod(containers=[c])
        self.assertEqual(pod_memory_request(pod), 0)

    def test_container_requests_no_memory_key(self):
        c = _mock_container(cpu="1", memory=None)
        pod = _mock_pod(containers=[c])
        self.assertEqual(pod_memory_request(pod), 0)

    def test_single_container_gi(self):
        c = _mock_container(memory="1Gi")
        pod = _mock_pod(containers=[c])
        self.assertEqual(pod_memory_request(pod), 1024**3)

    def test_single_container_mi(self):
        c = _mock_container(memory="512Mi")
        pod = _mock_pod(containers=[c])
        self.assertEqual(pod_memory_request(pod), 512 * 1024**2)

    def test_multiple_containers_sum(self):
        c1 = _mock_container(memory="1Gi")
        c2 = _mock_container(memory="2Gi")
        pod = _mock_pod(containers=[c1, c2])
        self.assertEqual(pod_memory_request(pod), 3 * 1024**3)

    def test_mixed_containers_some_without_memory(self):
        c1 = _mock_container(memory="256Mi")
        c2 = _mock_container(has_resources=False)
        c3 = _mock_container(memory="256Mi")
        pod = _mock_pod(containers=[c1, c2, c3])
        self.assertEqual(pod_memory_request(pod), 512 * 1024**2)


if __name__ == "__main__":
    unittest.main()
