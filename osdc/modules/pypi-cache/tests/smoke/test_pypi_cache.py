"""Smoke tests for the pypi-cache module.

Validates that the pypi-cache infrastructure is deployed and healthy:
- Namespace, ServiceAccounts (with IRSA annotation), ConfigMaps
- EFS-backed StorageClass and PVC
- Per-CUDA Deployments and Services (dynamic from clusters.yaml)
- Wants-collector Deployment (log scanner + S3 uploader)
- Wheel-syncer Deployment (S3-to-EFS wheel downloader)
- Karpenter NodePool (when instance_type is configured)
"""

from __future__ import annotations

import pytest
from helpers import (
    assert_deployment_ready,
    filter_deployments,
    filter_services,
    run_kubectl,
)

pytestmark = [pytest.mark.live]

NAMESPACE = "pypi-cache"
STORAGECLASS_NAME = "efs-pypi-cache"
PVC_NAME = "pypi-cache-data"
WANTS_COLLECTOR_DEPLOYMENT = "pypi-wants-collector"
WANTS_COLLECTOR_SA = "pypi-wants-collector"
WHEEL_SYNCER_DEPLOYMENT = "pypi-wheel-syncer"
WHEEL_SYNCER_SA = "pypi-wheel-syncer"


# ============================================================================
# Fixtures
# ============================================================================


@pytest.fixture(scope="session")
def instance_type(resolve_config) -> str:
    """Read pypi_cache.instance_type from cluster config (empty string if unset)."""
    return resolve_config("pypi_cache.instance_type", "")


# ============================================================================
# Namespace
# ============================================================================


class TestPypiCacheNamespace:
    """Verify the pypi-cache namespace exists."""

    def test_namespace_exists(self, all_namespaces: dict) -> None:
        names = [ns["metadata"]["name"] for ns in all_namespaces.get("items", [])]
        assert NAMESPACE in names, f"Namespace '{NAMESPACE}' not found. The pypi-cache module has not been deployed."


# ============================================================================
# ServiceAccounts
# ============================================================================


class TestPypiCacheServiceAccounts:
    """Verify ServiceAccounts exist and are properly annotated."""

    def test_pypi_cache_sa_exists(self) -> None:
        result = run_kubectl(["get", "serviceaccount", "pypi-cache"], namespace=NAMESPACE)
        assert result["metadata"]["name"] == "pypi-cache"

    def test_wants_collector_sa_exists(self) -> None:
        result = run_kubectl(["get", "serviceaccount", WANTS_COLLECTOR_SA], namespace=NAMESPACE)
        assert result["metadata"]["name"] == WANTS_COLLECTOR_SA

    def test_wants_collector_sa_has_irsa_annotation(self) -> None:
        """IRSA annotation is required for the wants-collector to access S3."""
        result = run_kubectl(["get", "serviceaccount", WANTS_COLLECTOR_SA], namespace=NAMESPACE)
        annotations = result.get("metadata", {}).get("annotations", {})
        irsa_key = "eks.amazonaws.com/role-arn"
        assert irsa_key in annotations, (
            f"ServiceAccount '{WANTS_COLLECTOR_SA}' missing IRSA annotation '{irsa_key}'. "
            f"The wants-collector will not be able to upload to S3."
        )
        assert annotations[irsa_key].startswith("arn:aws:iam::"), (
            f"IRSA annotation value does not look like an IAM role ARN: {annotations[irsa_key]}"
        )

    def test_wheel_syncer_sa_exists(self) -> None:
        result = run_kubectl(["get", "serviceaccount", WHEEL_SYNCER_SA], namespace=NAMESPACE)
        assert result["metadata"]["name"] == WHEEL_SYNCER_SA

    def test_wheel_syncer_sa_has_irsa_annotation(self) -> None:
        """IRSA annotation is required for the wheel-syncer to download from S3."""
        result = run_kubectl(["get", "serviceaccount", WHEEL_SYNCER_SA], namespace=NAMESPACE)
        annotations = result.get("metadata", {}).get("annotations", {})
        irsa_key = "eks.amazonaws.com/role-arn"
        assert irsa_key in annotations, (
            f"ServiceAccount '{WHEEL_SYNCER_SA}' missing IRSA annotation '{irsa_key}'. "
            f"The wheel-syncer will not be able to download wheels from S3."
        )
        assert annotations[irsa_key].startswith("arn:aws:iam::"), (
            f"IRSA annotation value does not look like an IAM role ARN: {annotations[irsa_key]}"
        )


# ============================================================================
# ConfigMaps
# ============================================================================


class TestPypiCacheConfigMaps:
    """Verify ConfigMaps exist with expected content."""

    def test_nginx_config_configmap_exists(self) -> None:
        result = run_kubectl(["get", "configmap", "pypi-cache-nginx-config"], namespace=NAMESPACE)
        data = result.get("data", {})
        assert "nginx.conf" in data, "ConfigMap 'pypi-cache-nginx-config' missing 'nginx.conf' key"

    def test_nginx_config_has_resolved_dns(self) -> None:
        """The nginx.conf must have the DNS resolver placeholder replaced."""
        result = run_kubectl(["get", "configmap", "pypi-cache-nginx-config"], namespace=NAMESPACE)
        conf = result.get("data", {}).get("nginx.conf", "")
        assert "__DNS_RESOLVER__" not in conf, (
            "nginx.conf still contains '__DNS_RESOLVER__' placeholder. "
            "The deploy.sh sed substitution failed — nginx proxy_pass will not resolve upstream hostnames."
        )
        assert "__NGINX_MAX_CACHE_SIZE__" not in conf, (
            "nginx.conf still contains '__NGINX_MAX_CACHE_SIZE__' placeholder."
        )

    def test_wants_collector_scripts_configmap_exists(self) -> None:
        result = run_kubectl(["get", "configmap", "pypi-wants-collector-scripts"], namespace=NAMESPACE)
        data = result.get("data", {})
        assert "wants_collector.py" in data, (
            "ConfigMap 'pypi-wants-collector-scripts' missing 'wants_collector.py'. "
            "The wants-collector pod will fail to start."
        )

    def test_wheel_syncer_scripts_configmap_exists(self) -> None:
        result = run_kubectl(["get", "configmap", "pypi-wheel-syncer-scripts"], namespace=NAMESPACE)
        data = result.get("data", {})
        assert "wheel_syncer.py" in data, (
            "ConfigMap 'pypi-wheel-syncer-scripts' missing 'wheel_syncer.py'. The wheel-syncer pod will fail to start."
        )


# ============================================================================
# Storage
# ============================================================================


class TestPypiCacheStorage:
    """Verify EFS StorageClass and PVC."""

    def test_storageclass_exists(self, all_storageclasses: dict) -> None:
        names = [sc["metadata"]["name"] for sc in all_storageclasses.get("items", [])]
        assert STORAGECLASS_NAME in names, (
            f"StorageClass '{STORAGECLASS_NAME}' not found. EFS CSI driver may not be configured."
        )

    def test_storageclass_provisioner(self, all_storageclasses: dict) -> None:
        for sc in all_storageclasses.get("items", []):
            if sc["metadata"]["name"] == STORAGECLASS_NAME:
                assert sc["provisioner"] == "efs.csi.aws.com", (
                    f"StorageClass '{STORAGECLASS_NAME}' has wrong provisioner: {sc['provisioner']}"
                )
                return
        pytest.fail(f"StorageClass '{STORAGECLASS_NAME}' not found")

    def test_pvc_exists_and_bound(self) -> None:
        result = run_kubectl(["get", "pvc", PVC_NAME], namespace=NAMESPACE)
        phase = result.get("status", {}).get("phase", "")
        assert phase == "Bound", (
            f"PVC '{PVC_NAME}' is in phase '{phase}', expected 'Bound'. "
            f"EFS filesystem may not be available or mount targets may be missing."
        )
        access_modes = result.get("spec", {}).get("accessModes", [])
        assert "ReadWriteMany" in access_modes, (
            f"PVC '{PVC_NAME}' access modes {access_modes} do not include ReadWriteMany. "
            f"Multiple pypi-cache replicas will not be able to share the filesystem."
        )


# ============================================================================
# Per-CUDA Deployments
# ============================================================================


class TestPypiCacheDeployments:
    """Verify per-CUDA cache Deployments and the wants-collector are ready."""

    def test_cache_deployments_exist(self, all_deployments: dict, pypi_cache_slugs: list[str]) -> None:
        """All expected pypi-cache-{slug} Deployments must exist."""
        deployed = filter_deployments(all_deployments, namespace=NAMESPACE, name_contains="pypi-cache-")
        deployed_names = {d["metadata"]["name"] for d in deployed}
        for slug in pypi_cache_slugs:
            name = f"pypi-cache-{slug}"
            assert name in deployed_names, (
                f"Deployment '{name}' not found in {NAMESPACE}. "
                f"CUDA slug '{slug}' from clusters.yaml has no corresponding deployment."
            )

    def test_cache_deployments_ready(self, all_deployments: dict, pypi_cache_slugs: list[str]) -> None:
        """All pypi-cache Deployments must have all replicas ready."""
        for slug in pypi_cache_slugs:
            assert_deployment_ready(all_deployments, NAMESPACE, f"pypi-cache-{slug}")

    def test_wants_collector_deployment_ready(self, all_deployments: dict) -> None:
        assert_deployment_ready(all_deployments, NAMESPACE, WANTS_COLLECTOR_DEPLOYMENT)

    def test_wheel_syncer_deployment_ready(self, all_deployments: dict) -> None:
        assert_deployment_ready(all_deployments, NAMESPACE, WHEEL_SYNCER_DEPLOYMENT)


# ============================================================================
# Services
# ============================================================================


class TestPypiCacheServices:
    """Verify per-CUDA ClusterIP Services exist with correct port."""

    def test_cache_services_exist(self, all_services: dict, pypi_cache_slugs: list[str]) -> None:
        deployed = filter_services(all_services, namespace=NAMESPACE, name_contains="pypi-cache-")
        deployed_names = {s["metadata"]["name"] for s in deployed}
        for slug in pypi_cache_slugs:
            name = f"pypi-cache-{slug}"
            assert name in deployed_names, (
                f"Service '{name}' not found in {NAMESPACE}. "
                f"Jobs targeting CUDA slug '{slug}' will not be able to reach the cache."
            )

    def test_cache_services_port(self, all_services: dict, pypi_cache_slugs: list[str]) -> None:
        """Each service must expose port 8080 (the nginx proxy port)."""
        for slug in pypi_cache_slugs:
            name = f"pypi-cache-{slug}"
            svcs = filter_services(all_services, namespace=NAMESPACE, name=name)
            if not svcs:
                continue  # Caught by test_cache_services_exist
            ports = svcs[0].get("spec", {}).get("ports", [])
            port_numbers = [p.get("port") for p in ports]
            assert 8080 in port_numbers, (
                f"Service '{name}' does not expose port 8080. "
                f"PIP_INDEX_URL/PIP_EXTRA_INDEX_URL pointing at port 8080 will fail."
            )


# ============================================================================
# NetworkPolicy
# ============================================================================


class TestPypiCacheNetworkPolicy:
    """Verify NetworkPolicy restricts ingress to arc-runners namespace."""

    def test_network_policy_exists(self) -> None:
        result = run_kubectl(["get", "networkpolicy", "pypi-cache-ingress"], namespace=NAMESPACE)
        assert result["metadata"]["name"] == "pypi-cache-ingress"

    def test_network_policy_allows_arc_runners_only(self) -> None:
        """Ingress must be restricted to arc-runners namespace on port 8080."""
        result = run_kubectl(["get", "networkpolicy", "pypi-cache-ingress"], namespace=NAMESPACE)
        ingress_rules = result.get("spec", {}).get("ingress", [])
        assert ingress_rules, (
            "NetworkPolicy 'pypi-cache-ingress' has no ingress rules. All traffic to pypi-cache pods will be blocked."
        )
        from_selectors = ingress_rules[0].get("from", [])
        ns_labels = [s.get("namespaceSelector", {}).get("matchLabels", {}) for s in from_selectors]
        arc_runners_selector = {"kubernetes.io/metadata.name": "arc-runners"}
        assert arc_runners_selector in ns_labels, (
            "NetworkPolicy 'pypi-cache-ingress' does not restrict ingress to arc-runners namespace. "
            "Pods from other namespaces could access the cache, creating a cache poisoning vector."
        )


# ============================================================================
# Pod Spec Validation
# ============================================================================


class TestPypiCachePodSpec:
    """Verify pod spec for cache and wants-collector Deployments."""

    @pytest.fixture
    def cache_pod_spec(self, all_deployments: dict, pypi_cache_slugs: list[str]) -> dict:
        """Return the pod spec from the first cache Deployment."""
        slug = pypi_cache_slugs[0]
        deploys = filter_deployments(all_deployments, namespace=NAMESPACE, name=f"pypi-cache-{slug}")
        assert deploys, f"No deployment found for pypi-cache-{slug}"
        return deploys[0]["spec"]["template"]["spec"]

    @pytest.fixture(
        params=[
            (WANTS_COLLECTOR_DEPLOYMENT, "wants-collector"),
            (WHEEL_SYNCER_DEPLOYMENT, "wheel-syncer"),
        ],
    )
    def pipeline_pod_spec(self, request, all_deployments: dict) -> tuple[str, dict]:
        """Return (component_label, pod_spec) for each pipeline Deployment."""
        name, label = request.param
        deploys = filter_deployments(all_deployments, namespace=NAMESPACE, name=name)
        assert deploys, f"Deployment '{name}' not found"
        return label, deploys[0]["spec"]["template"]["spec"]

    def test_cache_pod_has_two_containers(self, cache_pod_spec: dict) -> None:
        """Each cache pod must have nginx (proxy) and pypiserver (backend)."""
        containers = cache_pod_spec.get("containers", [])
        names = {c["name"] for c in containers}
        assert "nginx" in names, "Cache pod missing 'nginx' container"
        assert "pypiserver" in names, "Cache pod missing 'pypiserver' container"

    def test_cache_pod_security_context(self, cache_pod_spec: dict) -> None:
        """Cache pods must run as non-root with read-only root filesystem."""
        sc = cache_pod_spec.get("securityContext", {})
        assert sc.get("runAsNonRoot") is True, "Cache pod must have runAsNonRoot: true"
        for container in cache_pod_spec.get("containers", []):
            csc = container.get("securityContext", {})
            assert csc.get("readOnlyRootFilesystem") is True, (
                f"Container '{container['name']}' must have readOnlyRootFilesystem: true"
            )

    def test_cache_pod_pvc_mounted(self, cache_pod_spec: dict) -> None:
        """Cache pods must mount the shared EFS PVC."""
        volumes = cache_pod_spec.get("volumes", [])
        pvc_volumes = [v for v in volumes if v.get("persistentVolumeClaim", {}).get("claimName") == PVC_NAME]
        assert pvc_volumes, f"Cache pod does not mount PVC '{PVC_NAME}'"

    def test_pipeline_pod_readonly_root(self, pipeline_pod_spec: tuple[str, dict]) -> None:
        label, spec = pipeline_pod_spec
        for container in spec.get("containers", []):
            csc = container.get("securityContext", {})
            assert csc.get("readOnlyRootFilesystem") is True, (
                f"{label} container '{container['name']}' must have readOnlyRootFilesystem: true"
            )

    def test_pipeline_pod_has_liveness_probe(self, pipeline_pod_spec: tuple[str, dict]) -> None:
        """Liveness probe detects stalled cycles (no success in 10 minutes)."""
        label, spec = pipeline_pod_spec
        containers = spec.get("containers", [])
        assert containers, f"No containers in {label} pod"
        probe = containers[0].get("livenessProbe")
        assert probe is not None, (
            f"{label} container missing livenessProbe. A stalled process will not be restarted automatically."
        )

    def test_pipeline_pod_pvc_readwrite(self, pipeline_pod_spec: tuple[str, dict]) -> None:
        """Pipeline pods must mount the EFS PVC read-write."""
        label, spec = pipeline_pod_spec
        containers = spec.get("containers", [])
        assert containers, f"No containers in {label} pod"
        mounts = containers[0].get("volumeMounts", [])
        data_mounts = [m for m in mounts if m.get("name") == "data"]
        assert data_mounts, f"{label} missing 'data' volume mount"
        assert data_mounts[0].get("readOnly") is not True, f"{label} 'data' volume must be read-write"

    def test_cache_pod_tolerates_workload_taint(self, cache_pod_spec: dict, instance_type: str) -> None:
        """When dedicated nodes are configured, cache pods must tolerate workload=pypi-cache taint."""
        if not instance_type:
            pytest.skip("No instance_type configured — no dedicated node taint to tolerate")
        tolerations = cache_pod_spec.get("tolerations", [])
        workload_tolerations = [t for t in tolerations if t.get("key") == "workload" and t.get("value") == "pypi-cache"]
        assert workload_tolerations, (
            "Cache pod missing toleration for 'workload=pypi-cache:NoSchedule'. "
            "Pods will not schedule on dedicated pypi-cache Karpenter nodes."
        )

    def test_pipeline_tolerates_critical_addons(self, pipeline_pod_spec: tuple[str, dict]) -> None:
        """Pipeline pods run on base infra nodes which have CriticalAddonsOnly taint."""
        label, spec = pipeline_pod_spec
        tolerations = spec.get("tolerations", [])
        keys = {t.get("key") for t in tolerations}
        assert "CriticalAddonsOnly" in keys, (
            f"{label} missing toleration for 'CriticalAddonsOnly'. It will not schedule on base infrastructure nodes."
        )


# ============================================================================
# Karpenter NodePools (conditional)
# ============================================================================


class TestPypiCacheNodePools:
    """Verify Karpenter NodePool and EC2NodeClass exist when instance_type is configured."""

    def test_nodepool_exists_when_instance_type_set(self, all_nodepools: dict, instance_type: str) -> None:
        if not instance_type:
            pytest.skip("No instance_type configured — pypi-cache uses shared base nodes")
        names = [np["metadata"]["name"] for np in all_nodepools.get("items", [])]
        assert "pypi-cache" in names, (
            f"NodePool 'pypi-cache' not found but instance_type='{instance_type}' is configured. "
            f"Pypi-cache pods will not get dedicated nodes."
        )

    def test_ec2nodeclass_exists_when_instance_type_set(self, instance_type: str) -> None:
        if not instance_type:
            pytest.skip("No instance_type configured — pypi-cache uses shared base nodes")
        result = run_kubectl(["get", "ec2nodeclasses.karpenter.k8s.aws", "pypi-cache", "-o", "json"])
        assert result["metadata"]["name"] == "pypi-cache", (
            f"EC2NodeClass 'pypi-cache' not found but instance_type='{instance_type}' is configured. "
            f"Karpenter will not be able to provision nodes for pypi-cache pods."
        )
