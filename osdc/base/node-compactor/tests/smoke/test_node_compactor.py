"""Smoke tests for the node-compactor controller."""

import pytest
from helpers import assert_deployment_ready, filter_services, run_kubectl

pytestmark = [pytest.mark.live]

NAMESPACE = "kube-system"


# ============================================================================
# Node Compactor
# ============================================================================


class TestNodeCompactor:
    """Verify the node-compactor controller is deployed and healthy."""

    @pytest.fixture(autouse=True)
    def _skip_if_disabled(self, resolve_config):
        enabled = resolve_config("node_compactor.enabled", True)
        if not enabled:
            pytest.skip("node_compactor is disabled for this cluster")

    def test_deployment_exists_and_ready(self, all_deployments):
        assert_deployment_ready(all_deployments, NAMESPACE, "node-compactor")

    def test_service_exists(self, all_services):
        svcs = filter_services(all_services, namespace=NAMESPACE, name="node-compactor")
        assert len(svcs) == 1, "Service node-compactor not found in kube-system"

    def test_service_account_exists(self):
        result = run_kubectl(["get", "serviceaccount", "node-compactor"], namespace=NAMESPACE)
        assert result["metadata"]["name"] == "node-compactor"

    def test_cluster_role_exists(self):
        result = run_kubectl(["get", "clusterrole", "node-compactor"])
        assert result["metadata"]["name"] == "node-compactor"
