"""Smoke tests for EKS cluster and AWS infrastructure (incl. PR 7 vpc-cni env)."""

import subprocess

import pytest
import yaml
from cni_constants import ENI_CONFIG_LABEL
from helpers import get_unstable_node_names, pod_is_on_unstable_node, run_aws, run_kubectl

pytestmark = [pytest.mark.live, pytest.mark.aws]


# ============================================================================
# Fixtures
# ============================================================================


@pytest.fixture(scope="session")
def eks_cluster_info(cluster_config):
    """Fetch EKS cluster description once for all tests."""
    cluster_name = cluster_config["cluster"]["cluster_name"]
    region = cluster_config["cluster"].get("region", "us-west-2")
    return run_aws(["eks", "describe-cluster", "--name", cluster_name, "--region", region])


# ============================================================================
# EKS Cluster
# ============================================================================


class TestEKSCluster:
    """Verify the EKS cluster is healthy and configured correctly."""

    def test_cluster_is_active(self, eks_cluster_info, cluster_config):
        status = eks_cluster_info["cluster"]["status"]
        cluster_name = cluster_config["cluster"]["cluster_name"]
        assert status == "ACTIVE", f"EKS cluster {cluster_name} status is {status}, expected ACTIVE"

    def test_cluster_version(self, eks_cluster_info, resolve_config):
        expected_version = resolve_config("eks_version", "1.35")
        actual_version = eks_cluster_info["cluster"]["version"]
        assert actual_version == expected_version, (
            f"EKS version {actual_version} does not match expected {expected_version}"
        )

    def test_oidc_provider_configured(self, eks_cluster_info, cluster_config):
        cluster_name = cluster_config["cluster"]["cluster_name"]
        oidc_issuer = eks_cluster_info["cluster"].get("identity", {}).get("oidc", {}).get("issuer", "")
        assert oidc_issuer, f"OIDC provider not configured for cluster {cluster_name}"


# ============================================================================
# EKS Addons
# ============================================================================


_REQUIRED_ADDONS = ["vpc-cni", "coredns", "kube-proxy", "aws-ebs-csi-driver"]

# Terminal addon statuses that indicate a real failure (not transient churn).
_ADDON_FAILURE_STATUSES = {"CREATE_FAILED", "DELETE_FAILED"}

# Addons backed by DaemonSets — their pod-level health is the real signal.
# Maps addon name to (namespace, pod label selector key, pod label selector value).
_DAEMONSET_ADDONS: dict[str, tuple[str, str, str]] = {
    "kube-proxy": ("kube-system", "k8s-app", "kube-proxy"),
    "vpc-cni": ("kube-system", "k8s-app", "aws-node"),
    "aws-ebs-csi-driver": ("kube-system", "app", "ebs-csi-node"),
}


class TestEKSAddons:
    """Verify required EKS addons are installed and healthy.

    Uses a two-layer check:
    1. Addon must not be in a terminal failure state (CREATE_FAILED, DELETE_FAILED).
    2. For DaemonSet-backed addons (kube-proxy, vpc-cni, ebs-csi), verify actual
       pod health — all pods on stable nodes must be Running. This is resilient to
       transient DEGRADED status caused by Karpenter node churn.
    """

    @pytest.mark.parametrize("addon_name", _REQUIRED_ADDONS)
    def test_addon_healthy(self, cluster_config, addon_name):
        cluster_name = cluster_config["cluster"]["cluster_name"]
        region = cluster_config["cluster"].get("region", "us-west-2")
        result = run_aws(
            [
                "eks",
                "describe-addon",
                "--cluster-name",
                cluster_name,
                "--addon-name",
                addon_name,
                "--region",
                region,
            ],
            timeout=120,
        )
        status = result["addon"]["status"]
        assert status not in _ADDON_FAILURE_STATUSES, f"Addon {addon_name} is in terminal failure state: {status}"

        if status == "ACTIVE":
            return

        # Status is DEGRADED or UPDATING — verify actual pod health for
        # DaemonSet-backed addons (the most common case for transient DEGRADED).
        ds_info = _DAEMONSET_ADDONS.get(addon_name)
        if ds_info is None:
            # Non-DaemonSet addon (e.g. coredns) — can't do pod-level check,
            # so accept non-failure status.
            return

        ns, label_key, label_value = ds_info
        pods_result = run_kubectl(["get", "pods", "-l", f"{label_key}={label_value}"], namespace=ns)
        pods = pods_result.get("items", [])
        assert len(pods) > 0, f"Addon {addon_name}: no pods found with label {label_key}={label_value} in {ns}"

        nodes_result = run_kubectl(["get", "nodes"])
        unstable_nodes = get_unstable_node_names(nodes_result)

        # Bucket non-Running pods into "on stable host" (real failure) vs "on
        # unstable/missing host" (Karpenter-roll race). Counting both for the
        # diagnostic message even though only the first triggers the assertion.
        not_running: list[str] = []
        excluded: list[str] = []
        for p in pods:
            if p["status"].get("phase") == "Running":
                continue
            if pod_is_on_unstable_node(p, nodes_result):
                excluded.append(p["metadata"]["name"])
            else:
                not_running.append(p["metadata"]["name"])

        assert not not_running, (
            f"Addon {addon_name} status is {status} and pods are unhealthy on stable nodes: "
            f"{not_running} ({len(unstable_nodes)} unstable nodes, "
            f"{len(excluded)} pods on unstable/missing hosts excluded)"
        )


# ============================================================================
# ECR Images
# ============================================================================


class TestECRImages:
    """Verify ECR repositories exist for all bootstrap images."""

    def test_ecr_repos_exist(self, cluster_config, upstream_dir):
        region = cluster_config["cluster"].get("region", "us-west-2")
        images_path = upstream_dir / "modules" / "eks" / "images.yaml"
        assert images_path.exists(), f"images.yaml not found at {images_path}"

        with open(images_path) as f:
            images_data = yaml.safe_load(f)

        expected_repos = [img["repository"] for img in images_data.get("images", [])]
        assert len(expected_repos) > 0, "No images defined in images.yaml"

        result = run_aws(["ecr", "describe-repositories", "--region", region])
        ecr_repo_names = [r["repositoryName"] for r in result.get("repositories", [])]

        missing = [repo for repo in expected_repos if repo not in ecr_repo_names]
        assert not missing, f"ECR repositories missing for bootstrap images: {missing}"


# ============================================================================
# CoreDNS Topology Pinning
# ============================================================================


class TestCoreDNSTopology:
    """Verify CoreDNS replicaCount, topology spread, autoscaling, and PDB.

    Pinned via aws_eks_addon.coredns configuration_values in
    modules/eks/terraform/modules/eks/main.tf. Replica count is per-cluster
    via clusters.yaml (coredns.replicas — default 6, staging 2).
    """

    def test_replica_count_matches_clusters_yaml(self, resolve_config):
        expected = resolve_config("coredns.replicas", 6)
        deploy = run_kubectl(["get", "deployment", "coredns"], namespace="kube-system")
        actual = deploy.get("spec", {}).get("replicas")
        assert actual == expected, (
            f"CoreDNS Deployment.spec.replicas={actual} does not match clusters.yaml expected={expected}"
        )

    def test_zone_topology_spread_is_hard(self, resolve_config):
        deploy = run_kubectl(["get", "deployment", "coredns"], namespace="kube-system")
        constraints = deploy.get("spec", {}).get("template", {}).get("spec", {}).get("topologySpreadConstraints", [])
        zone_rules = [
            c
            for c in constraints
            if c.get("topologyKey") == "topology.kubernetes.io/zone" and c.get("whenUnsatisfiable") == "DoNotSchedule"
        ]
        assert zone_rules, (
            f"CoreDNS Deployment missing hard zone spread (topology.kubernetes.io/zone, DoNotSchedule). "
            f"Found constraints: {constraints}"
        )
        # maxSkew=2 tolerates AWS subnet placement drift (e.g. 4-2-0 across AZs after
        # AMI rolls) while still preventing catastrophic 6-0-0 single-AZ pinning.
        for rule in zone_rules:
            assert rule.get("maxSkew") == 2, (
                f"CoreDNS zone spread maxSkew={rule.get('maxSkew')}, expected 2. Constraint: {rule}"
            )
            assert rule.get("labelSelector", {}).get("matchLabels", {}).get("k8s-app") == "kube-dns", (
                f"CoreDNS zone spread labelSelector mismatch. Constraint: {rule}"
            )

    def test_hostname_topology_spread_is_soft(self, resolve_config):
        """Soft hostname spread (ScheduleAnyway) keeps replicas off the same node when possible."""
        deploy = run_kubectl(["get", "deployment", "coredns"], namespace="kube-system")
        constraints = deploy.get("spec", {}).get("template", {}).get("spec", {}).get("topologySpreadConstraints", [])
        host_rules = [
            c
            for c in constraints
            if c.get("topologyKey") == "kubernetes.io/hostname" and c.get("whenUnsatisfiable") == "ScheduleAnyway"
        ]
        assert host_rules, (
            f"CoreDNS Deployment missing soft hostname spread (kubernetes.io/hostname, ScheduleAnyway). "
            f"Found constraints: {constraints}"
        )
        for rule in host_rules:
            assert rule.get("maxSkew") == 1, (
                f"CoreDNS hostname spread maxSkew={rule.get('maxSkew')}, expected 1. Constraint: {rule}"
            )
            assert rule.get("labelSelector", {}).get("matchLabels", {}).get("k8s-app") == "kube-dns", (
                f"CoreDNS hostname spread labelSelector mismatch. Constraint: {rule}"
            )

    def test_no_cluster_proportional_autoscaler(self, cluster_config):
        """Addon-managed autoscaling must be off — no CPA Deployment should exist for coredns.

        EKS CoreDNS uses the cluster-proportional-autoscaler (CPA), which runs as a
        Deployment named 'coredns-autoscaler' (newer addon versions) or 'eks-coredns-autoscaler'
        (older spelling). It is NOT a HorizontalPodAutoscaler. With autoScaling.enabled=false
        in the addon configuration_values, neither Deployment should be present.
        """
        cluster_name = cluster_config["cluster"]["cluster_name"]
        for cpa_name in ("coredns-autoscaler", "eks-coredns-autoscaler"):
            try:
                deploy = run_kubectl(["get", "deployment", cpa_name], namespace="kube-system")
            except subprocess.CalledProcessError:
                # 'NotFound' surfaces as kubectl exit 1 — that's the desired state.
                continue
            # If we got JSON back, the CPA Deployment exists — that's a regression.
            pytest.fail(
                f"Unexpected cluster-proportional-autoscaler Deployment '{cpa_name}' in kube-system "
                f"on {cluster_name} (autoScaling.enabled=false should prevent this): {deploy}"
            )

    def test_pdb_max_unavailable_is_one(self):
        pdbs = run_kubectl(["get", "poddisruptionbudgets"], namespace="kube-system")
        items = pdbs.get("items", [])
        coredns_pdbs = [
            p
            for p in items
            if p.get("spec", {}).get("selector", {}).get("matchLabels", {}).get("k8s-app") == "kube-dns"
        ]
        assert coredns_pdbs, f"No PodDisruptionBudget found targeting CoreDNS (k8s-app=kube-dns). Items: {items}"
        # EKS addon picks one PDB resource name (typically 'coredns'); assert all
        # found PDBs have maxUnavailable=1 (defensive against multiple stale PDBs).
        for pdb in coredns_pdbs:
            spec = pdb.get("spec", {})
            max_unavail = spec.get("maxUnavailable")
            assert max_unavail == 1, (
                f"CoreDNS PDB {pdb['metadata']['name']} has maxUnavailable={max_unavail}, expected 1"
            )


# ============================================================================
# VPC CNI addon env reconciliation (PR 7)
# ============================================================================


# Env keys/values the vpc-cni addon must push onto the live aws-node DaemonSet.
# Source of truth: aws_eks_addon.vpc_cni configuration_values in
# modules/eks/terraform/modules/eks/main.tf (PR 7). Drift here means EKS
# reconciliation silently dropped an env key (most often a ConfigurationConflict
# with a hand-edited DaemonSet under PRESERVE).
EXPECTED_VPC_CNI_ENV: dict[str, str] = {
    "AWS_VPC_K8S_CNI_CUSTOM_NETWORK_CFG": "true",
    "ENABLE_PREFIX_DELEGATION": "true",
    "ENI_CONFIG_LABEL_DEF": ENI_CONFIG_LABEL,
    "WARM_PREFIX_TARGET": "1",
}


class TestVPCCNIConfig:
    """The vpc-cni addon's env vars must land on the live aws-node DaemonSet (PR 7).

    Catches the silent-failure mode where the addon spec is updated but EKS reconciliation
    quietly drops env keys (e.g., due to PRESERVE conflict on a hand-edited DaemonSet).
    """

    @pytest.mark.live
    def test_aws_node_env_vars_present(self) -> None:
        """Every running aws-node pod must carry all four expected env vars (PR 7).

        Iterates ALL running pods to catch partial-rollout drift (some pods reconciled,
        others stale). Single-pod sampling could pick a stale pod and mask drift on
        the others.
        """
        pods_result = run_kubectl(["get", "pods", "-l", "k8s-app=aws-node"], namespace="kube-system")
        pods = pods_result.get("items", [])
        assert pods, "no aws-node pods found in kube-system (vpc-cni addon not installed?)"

        running = [p for p in pods if p.get("status", {}).get("phase") == "Running"]
        if not running:
            pytest.fail(
                f"no running aws-node pods found in kube-system "
                f"(observed {len(pods)} pods, none with status.phase=='Running')"
            )

        problems: list[str] = []
        for pod in running:
            pod_name = pod.get("metadata", {}).get("name", "<unknown>")
            node_name = pod.get("spec", {}).get("nodeName", "<unknown>")
            containers = pod.get("spec", {}).get("containers", [])
            aws_node_containers = [c for c in containers if c.get("name") == "aws-node"]
            if not aws_node_containers:
                problems.append(f"{pod_name} (node={node_name}): no container named 'aws-node'")
                continue

            env_list = aws_node_containers[0].get("env", []) or []
            env_by_name = {e.get("name"): e.get("value") for e in env_list if "name" in e}

            for key, expected in EXPECTED_VPC_CNI_ENV.items():
                if key not in env_by_name:
                    problems.append(f"{pod_name} (node={node_name}): {key} missing (expected {expected!r})")
                    continue
                actual = env_by_name[key]
                if actual != expected:
                    problems.append(f"{pod_name} (node={node_name}): {key} = {actual!r}, expected {expected!r}")

        assert not problems, (
            f"vpc-cni addon env reconciliation incomplete on {len(running)} aws-node pod(s):\n  "
            + "\n  ".join(problems)
        )

    @pytest.mark.live
    @pytest.mark.aws
    def test_addon_health_issues_empty(self, cluster_config) -> None:
        """aws eks describe-addon must report zero ConfigurationConflict issues."""
        cluster_name = cluster_config["cluster"]["cluster_name"]
        region = cluster_config["cluster"].get("region", "us-west-2")
        result = run_aws(
            [
                "eks",
                "describe-addon",
                "--cluster-name",
                cluster_name,
                "--addon-name",
                "vpc-cni",
                "--region",
                region,
            ],
            timeout=120,
        )
        issues = result.get("addon", {}).get("health", {}).get("issues", []) or []
        if not issues:
            return

        # Surface ConfigurationConflict prominently — that's the specific failure
        # mode PR 7 introduces if PRESERVE was left in place against a hand-edited
        # DaemonSet — but report ALL issues so unrelated drift isn't masked.
        rendered = [
            f"code={i.get('code', '<no-code>')!r} message={i.get('message', '<no-message>')!r} "
            f"resourceIds={i.get('resourceIds', [])}"
            for i in issues
        ]
        pytest.fail(
            f"vpc-cni addon on {cluster_name} reports {len(issues)} health issue(s) — "
            f"PR 7 env reconciliation likely failed (look for code='ConfigurationConflict'):\n  "
            + "\n  ".join(rendered)
        )
