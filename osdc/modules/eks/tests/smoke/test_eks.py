"""Smoke tests for EKS cluster and AWS infrastructure."""

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
# VPC CNI addon env reconciliation
# ============================================================================


# Env keys/values the vpc-cni addon must push onto the live aws-node DaemonSet.
# Source of truth: aws_eks_addon.vpc_cni configuration_values in
# modules/eks/terraform/modules/eks/main.tf. Drift here means EKS
# reconciliation silently dropped an env key.
EXPECTED_VPC_CNI_ENV: dict[str, str] = {
    "AWS_VPC_K8S_CNI_CUSTOM_NETWORK_CFG": "true",
    "ENABLE_PREFIX_DELEGATION": "true",
    "ENI_CONFIG_LABEL_DEF": ENI_CONFIG_LABEL,
    "WARM_PREFIX_TARGET": "1",
}


class TestVPCCNIConfig:
    """The vpc-cni addon's env vars must land on the live aws-node DaemonSet.

    Catches the silent-failure mode where the addon spec is updated but EKS reconciliation
    quietly drops env keys (e.g., due to PRESERVE conflict on a hand-edited DaemonSet).
    """

    @pytest.mark.live
    def test_aws_node_env_vars_present(self) -> None:
        """Every running aws-node pod must carry all four expected env vars.

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
        # mode that arises if PRESERVE was left in place against a hand-edited
        # DaemonSet — but report ALL issues so unrelated drift isn't masked.
        rendered = [
            f"code={i.get('code', '<no-code>')!r} message={i.get('message', '<no-message>')!r} "
            f"resourceIds={i.get('resourceIds', [])}"
            for i in issues
        ]
        pytest.fail(
            f"vpc-cni addon on {cluster_name} reports {len(issues)} health issue(s) — "
            f"Env reconciliation likely failed (look for code='ConfigurationConflict'):\n  " + "\n  ".join(rendered)
        )


# ============================================================================
# NAT Gateway Topology
# ============================================================================


def _tag_value(tags: list[dict], key: str) -> str | None:
    """Extract a tag value from an AWS-style ``[{"Key": ..., "Value": ...}]`` list."""
    for t in tags or []:
        if t.get("Key") == key:
            return t.get("Value")
    return None


def _tags_to_dict(tags: list[dict]) -> dict[str, str]:
    """Flatten an AWS-style tag list into a {Key: Value} dict."""
    return {t.get("Key"): t.get("Value") for t in (tags or []) if "Key" in t}


def _explicit_rt_for_subnet(vpc_id: str, aws_region: str, subnet_id: str) -> tuple[dict | None, str | None]:
    """Return ``(route_table, error)`` for the EXPLICIT (non-main) RT association of a subnet.

    Main RT fallback is intentionally rejected — it indicates the per-(bucket, az) RT is missing.
    """
    rt_lookup = run_aws(
        [
            "ec2",
            "describe-route-tables",
            "--filters",
            f"Name=vpc-id,Values={vpc_id}",
            f"Name=association.subnet-id,Values={subnet_id}",
            "--region",
            aws_region,
        ],
        timeout=60,
    )
    rts = rt_lookup.get("RouteTables", []) or []
    explicit = [
        rt
        for rt in rts
        for assoc in (rt.get("Associations") or [])
        if assoc.get("SubnetId") == subnet_id and not assoc.get("Main", False)
    ]
    if len(explicit) != 1:
        rt_ids = [r.get("RouteTableId") for r in explicit]
        return None, f"expected exactly 1 explicit route-table association, got {len(explicit)} (rts={rt_ids})"
    return explicit[0], None


def _default_route_natgw(rt: dict) -> tuple[str | None, str | None]:
    """Return ``(nat_gateway_id, error)`` for the RT's ``0.0.0.0/0`` route."""
    default_routes = [r for r in (rt.get("Routes") or []) if r.get("DestinationCidrBlock") == "0.0.0.0/0"]
    if len(default_routes) != 1:
        return None, f"expected exactly one 0.0.0.0/0 route, got {len(default_routes)}"
    ngw_id = default_routes[0].get("NatGatewayId")
    if not ngw_id:
        return None, f"0.0.0.0/0 route does not target a NAT GW (route={default_routes[0]})"
    return ngw_id, None


@pytest.fixture(scope="session")
def vpc_id(eks_cluster_info) -> str:
    """The cluster's primary VPC ID, sourced from the EKS cluster description."""
    vpc = eks_cluster_info["cluster"].get("resourcesVpcConfig", {}).get("vpcId", "")
    assert vpc, "EKS cluster description missing resourcesVpcConfig.vpcId"
    return vpc


@pytest.fixture(scope="session")
def aws_region(cluster_config) -> str:
    """The cluster's AWS region (defaulted in clusters.yaml)."""
    return cluster_config["cluster"].get("region", "us-west-2")


@pytest.fixture(scope="session")
def pod_cidr_buckets(cluster_config) -> dict[str, dict[str, str]]:
    """The bucket -> {az: cidr} mapping from clusters.yaml ``base.pod_cidr_buckets``."""
    buckets = (cluster_config["cluster"].get("base") or {}).get("pod_cidr_buckets") or {}
    assert buckets, (
        "cluster config missing base.pod_cidr_buckets — required to derive the NAT topology "
        "(every NodePool's pod subnet maps to a (bucket, AZ) NAT GW)"
    )
    return buckets


@pytest.fixture(scope="session")
def cluster_azs(pod_cidr_buckets) -> list[str]:
    """Sorted list of AZ keys derived from the first bucket in ``pod_cidr_buckets``.

    ``pod_cidr_buckets`` is the single source of truth for AZs (each bucket's keys =
    the AZs that get pod subnets + NAT GWs); every bucket carries the same AZ keys
    (validated by ``_validate_pod_cidr_buckets`` in ``scripts/cluster-config.py``).
    """
    return sorted(next(iter(pod_cidr_buckets.values())).keys())


@pytest.fixture(scope="session")
def default_node_egress_bucket(pod_cidr_buckets) -> str:
    """Alphabetically first bucket — node-private RTs route through this bucket's NAT GW."""
    return sorted(pod_cidr_buckets.keys())[0]


@pytest.fixture(scope="session")
def vpc_nat_gateways(vpc_id, aws_region) -> list[dict]:
    """All NAT Gateways in the cluster's VPC (excluding deleted/deleting)."""
    result = run_aws(
        [
            "ec2",
            "describe-nat-gateways",
            "--filter",
            f"Name=vpc-id,Values={vpc_id}",
            "--region",
            aws_region,
        ],
        timeout=120,
    )
    # describe-nat-gateways returns historical entries (state=deleted) without an
    # explicit filter; restrict to live ones so the topology assertions reflect
    # current AWS state, not tombstones.
    return [ng for ng in result.get("NatGateways", []) if ng.get("State") not in ("deleted", "deleting", "failed")]


@pytest.fixture(scope="session")
def vpc_subnets(vpc_id, aws_region) -> list[dict]:
    """All subnets in the cluster's VPC (single AWS call shared by topology tests)."""
    result = run_aws(
        [
            "ec2",
            "describe-subnets",
            "--filters",
            f"Name=vpc-id,Values={vpc_id}",
            "--region",
            aws_region,
        ],
        timeout=120,
    )
    return result.get("Subnets", [])


class TestNATGatewayTopology:
    """Verify per-(bucket, AZ) NAT Gateway / EIP / route-table topology.

    Read-only AWS API checks against the live VPC. Drift here means terraform
    apply did not actually shape the topology that ``clusters.yaml`` and the VPC
    submodule promise — typically because of a botched apply, manual
    intervention in the AWS console, or a stale state file.
    """

    def test_nat_gateway_count_per_bucket_az(self, vpc_nat_gateways, pod_cidr_buckets, cluster_azs):
        """Exactly ``len(buckets) * len(azs)`` NAT GWs, each tagged with bucket+az."""
        expected_count = len(pod_cidr_buckets) * len(cluster_azs)
        tagged_natgws = [ng for ng in vpc_nat_gateways if _tag_value(ng.get("Tags", []), "osdc.io/nat-bucket")]
        actual_count = len(tagged_natgws)
        assert actual_count == expected_count, (
            f"expected {expected_count} NAT Gateways tagged with osdc.io/nat-bucket "
            f"(buckets={sorted(pod_cidr_buckets.keys())}, azs={cluster_azs}); "
            f"observed {actual_count} (total live NAT GWs in VPC: {len(vpc_nat_gateways)})"
        )

        # Every (bucket, AZ) cell must be present exactly once. A duplicate cell
        # or missing cell would still match the count above if one cell were
        # double-tagged and another absent.
        seen_cells: dict[tuple[str, str], list[str]] = {}
        for ng in tagged_natgws:
            tags = _tags_to_dict(ng.get("Tags", []))
            cell = (tags.get("osdc.io/nat-bucket", ""), tags.get("osdc.io/nat-az", ""))
            seen_cells.setdefault(cell, []).append(ng.get("NatGatewayId", "<unknown>"))

        expected_cells = {(b, az) for b in pod_cidr_buckets for az in cluster_azs}
        missing = expected_cells - set(seen_cells.keys())
        duplicated = {cell: ids for cell, ids in seen_cells.items() if len(ids) > 1}
        unexpected = set(seen_cells.keys()) - expected_cells
        problems: list[str] = []
        if missing:
            problems.append(f"missing (bucket, az) cells: {sorted(missing)}")
        if duplicated:
            problems.append(f"duplicated (bucket, az) cells: {duplicated}")
        if unexpected:
            problems.append(f"unexpected tag values not in clusters.yaml: {sorted(unexpected)}")
        assert not problems, "NAT GW topology mismatch:\n  " + "\n  ".join(problems)

    def test_nat_gateway_subnet_az_consistency(self, vpc_nat_gateways, vpc_subnets):
        """Each NAT GW's hosting subnet must report the same AZ as its osdc.io/nat-az tag.

        Catches the failure mode where the public-subnets-by-AZ lookup in nat.tf
        drifted positionally (e.g. ``var.azs[i]`` vs the actual subnet AZ) and a
        NAT GW landed in the wrong AZ.
        """
        subnet_az_by_id = {s.get("SubnetId"): s.get("AvailabilityZone") for s in vpc_subnets}
        problems: list[str] = []
        for ng in vpc_nat_gateways:
            ngw_id = ng.get("NatGatewayId", "<unknown>")
            tag_az = _tag_value(ng.get("Tags", []), "osdc.io/nat-az")
            if tag_az is None:
                # Untagged NAT GW (not managed by this terraform) — covered by count
                # test; skipping here keeps this test focused on AZ consistency.
                continue
            subnet_id = ng.get("SubnetId")
            actual_az = subnet_az_by_id.get(subnet_id)
            if actual_az is None:
                problems.append(f"{ngw_id}: subnet {subnet_id} not found among VPC subnets")
                continue
            if actual_az != tag_az:
                problems.append(
                    f"{ngw_id}: tag osdc.io/nat-az={tag_az!r} but subnet {subnet_id} reports AZ={actual_az!r}"
                )
        assert not problems, "NAT GW subnet/AZ mismatch:\n  " + "\n  ".join(problems)

    def test_each_nat_gateway_has_expected_eip_count(self, vpc_nat_gateways, resolve_config):
        """``1 + len(secondary_allocation_ids) == nat_gateway_eip_count`` for every NAT GW."""
        expected_eip_count = resolve_config("nat_gateway_eip_count", 8)
        problems: list[str] = []
        for ng in vpc_nat_gateways:
            ngw_id = ng.get("NatGatewayId", "<unknown>")
            tags = _tags_to_dict(ng.get("Tags", []))
            if tags.get("osdc.io/nat-bucket") is None:
                continue  # Skip unmanaged NAT GWs; covered by count test.
            addresses = ng.get("NatGatewayAddresses", []) or []
            primary = [a for a in addresses if a.get("IsPrimary")]
            secondary = [a for a in addresses if not a.get("IsPrimary")]
            actual = len(primary) + len(secondary)
            if actual != expected_eip_count:
                problems.append(
                    f"{ngw_id} (bucket={tags.get('osdc.io/nat-bucket')}, az={tags.get('osdc.io/nat-az')}): "
                    f"{len(primary)} primary + {len(secondary)} secondary = {actual} EIPs, "
                    f"expected {expected_eip_count}"
                )
            elif len(primary) != 1:
                problems.append(
                    f"{ngw_id}: expected exactly 1 primary EIP, got {len(primary)} (secondary={len(secondary)})"
                )
        assert not problems, f"NAT GW EIP count drift (expected {expected_eip_count} per NAT GW):\n  " + "\n  ".join(
            problems
        )

    def test_pod_subnet_route_table_associations(self, vpc_subnets, vpc_nat_gateways, vpc_id, aws_region):
        """Each pod subnet must be explicitly associated to a per-(bucket, az) RT
        whose ``0.0.0.0/0`` route points at the matching NAT GW.

        Verifies (a) explicit RT association (not main RT fallback);
        (b) RT's ``osdc.io/pod-route-{bucket,az}`` tags match the subnet's
        ``osdc.io/pod-subnet-{bucket,az}`` tags;
        (c) RT's default route resolves to a NAT GW carrying the same bucket+az.
        """
        nat_by_id = {ng.get("NatGatewayId"): ng for ng in vpc_nat_gateways}
        pod_subnets = [s for s in vpc_subnets if _tag_value(s.get("Tags", []), "osdc.io/pod-subnet-bucket")]
        assert pod_subnets, "no subnets carry the osdc.io/pod-subnet-bucket tag — pod subnets module not applied?"

        problems: list[str] = []
        for s in pod_subnets:
            subnet_id = s.get("SubnetId")
            stags = _tags_to_dict(s.get("Tags", []))
            sub_bucket = stags.get("osdc.io/pod-subnet-bucket")
            sub_az = stags.get("osdc.io/pod-subnet-az")
            ctx = f"pod subnet {subnet_id} (bucket={sub_bucket}, az={sub_az})"

            rt, err = _explicit_rt_for_subnet(vpc_id, aws_region, subnet_id)
            if err is not None:
                problems.append(f"{ctx}: {err}")
                continue
            rt_id = rt.get("RouteTableId", "<unknown>")
            rtags = _tags_to_dict(rt.get("Tags", []))
            rt_bucket = rtags.get("osdc.io/pod-route-bucket")
            rt_az = rtags.get("osdc.io/pod-route-az")
            if rt_bucket != sub_bucket or rt_az != sub_az:
                problems.append(
                    f"{ctx}: associated RT {rt_id} has tags pod-route-bucket={rt_bucket!r}, pod-route-az={rt_az!r}"
                )
                continue

            ngw_id, err = _default_route_natgw(rt)
            if err is not None:
                problems.append(f"RT {rt_id} (bucket={rt_bucket}, az={rt_az}): {err}")
                continue
            ng = nat_by_id.get(ngw_id)
            if ng is None:
                problems.append(
                    f"RT {rt_id} (bucket={rt_bucket}, az={rt_az}): 0.0.0.0/0 -> NAT GW {ngw_id} "
                    f"not present in VPC NAT GW listing"
                )
                continue
            ngtags = _tags_to_dict(ng.get("Tags", []))
            if ngtags.get("osdc.io/nat-bucket") != sub_bucket or ngtags.get("osdc.io/nat-az") != sub_az:
                problems.append(
                    f"RT {rt_id} (bucket={rt_bucket}, az={rt_az}): 0.0.0.0/0 -> NAT GW {ngw_id} "
                    f"with tags nat-bucket={ngtags.get('osdc.io/nat-bucket')!r}, "
                    f"nat-az={ngtags.get('osdc.io/nat-az')!r}"
                )

        assert not problems, "pod subnet RT topology drift:\n  " + "\n  ".join(problems)

    def test_private_route_table_associations_route_to_default_node_egress_bucket(
        self,
        vpc_subnets,
        vpc_nat_gateways,
        vpc_id,
        aws_region,
        default_node_egress_bucket,
    ):
        """Per-AZ private (node) RTs must route ``0.0.0.0/0`` through the
        ``default_node_egress_bucket`` NAT GW for that AZ.

        Filters: subnet has the ``Name`` tag matching ``*-private-<az>`` AND
        does NOT carry ``osdc.io/pod-subnet-bucket`` (which would mark it as a
        pod subnet).
        """
        nat_by_id = {ng.get("NatGatewayId"): ng for ng in vpc_nat_gateways}
        private_subnets = [
            s
            for s in vpc_subnets
            if _tag_value(s.get("Tags", []), "osdc.io/pod-subnet-bucket") is None
            and (_tag_value(s.get("Tags", []), "Name") or "").rsplit("-", 1)[0].endswith("-private")
        ]
        assert private_subnets, (
            "no per-AZ private subnets found (Name=*-private-<az> AND no osdc.io/pod-subnet-bucket tag)"
        )

        problems: list[str] = []
        for s in private_subnets:
            subnet_id = s.get("SubnetId")
            sub_az = s.get("AvailabilityZone", "")
            sub_name = _tag_value(s.get("Tags", []), "Name") or "<no-name>"
            ctx = f"private subnet {subnet_id} (Name={sub_name}, az={sub_az})"

            rt, err = _explicit_rt_for_subnet(vpc_id, aws_region, subnet_id)
            if err is not None:
                problems.append(f"{ctx}: {err}")
                continue
            rt_id = rt.get("RouteTableId", "<unknown>")
            ngw_id, err = _default_route_natgw(rt)
            if err is not None:
                problems.append(f"private RT {rt_id} ({ctx}): {err}")
                continue
            ng = nat_by_id.get(ngw_id)
            if ng is None:
                problems.append(
                    f"private RT {rt_id} ({ctx}): 0.0.0.0/0 -> NAT GW {ngw_id} not present in VPC NAT GW listing"
                )
                continue
            ngtags = _tags_to_dict(ng.get("Tags", []))
            ng_bucket = ngtags.get("osdc.io/nat-bucket")
            ng_az = ngtags.get("osdc.io/nat-az")
            if ng_bucket != default_node_egress_bucket or ng_az != sub_az:
                problems.append(
                    f"private RT {rt_id} ({ctx}): 0.0.0.0/0 -> NAT GW {ngw_id} with tags "
                    f"nat-bucket={ng_bucket!r}, nat-az={ng_az!r}; expected "
                    f"nat-bucket={default_node_egress_bucket!r}, nat-az={sub_az!r}"
                )

        assert not problems, (
            f"private subnet RT topology drift (default_node_egress_bucket={default_node_egress_bucket!r}):\n  "
            + "\n  ".join(problems)
        )
