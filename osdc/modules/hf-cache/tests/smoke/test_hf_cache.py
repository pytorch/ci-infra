"""Smoke tests for the hf-cache module.

Validates the deployed state: namespace, IRSA-annotated ServiceAccount, the
read-only rclone mount DaemonSet, and that the arc-runners job-pod template was
rendered with the /mnt/hf_cache mount.
"""

from __future__ import annotations

import pytest
from helpers import assert_daemonset_healthy, filter_daemonsets, run_kubectl

pytestmark = [pytest.mark.live]

NAMESPACE = "hf-cache"
MOUNT_DS = "hf-cache-mount"
MOUNT_DS_LARGEGPU = "hf-cache-mount-largegpu"
MOUNT_SA = "hf-cache-mount"
IRSA_KEY = "eks.amazonaws.com/role-arn"
MOUNT_PATH = "/mnt/hf_cache"
GPU_NAME_LABEL = "karpenter.k8s.aws/instance-gpu-name"
LARGE_GPU_VALUES = {"h100", "b200"}
RUNNER_NODE_SELECTOR = {"workload-type": ["github-runner"]}


class TestHfCacheNamespace:
    def test_namespace_exists(self, all_namespaces: dict) -> None:
        names = [ns["metadata"]["name"] for ns in all_namespaces.get("items", [])]
        assert NAMESPACE in names, f"Namespace '{NAMESPACE}' not found — hf-cache not deployed."


class TestHfCacheServiceAccount:
    def test_mount_sa_exists(self) -> None:
        result = run_kubectl(["get", "serviceaccount", MOUNT_SA], namespace=NAMESPACE)
        assert result["metadata"]["name"] == MOUNT_SA

    def test_mount_sa_has_irsa_annotation(self) -> None:
        """IRSA annotation lets the rclone mount read S3."""
        result = run_kubectl(["get", "serviceaccount", MOUNT_SA], namespace=NAMESPACE)
        ann = result.get("metadata", {}).get("annotations", {})
        assert IRSA_KEY in ann, f"SA '{MOUNT_SA}' missing IRSA annotation '{IRSA_KEY}' — mount can't read S3."
        assert ann[IRSA_KEY].startswith("arn:aws:iam::"), f"IRSA annotation is not an IAM role ARN: {ann[IRSA_KEY]}"


class TestHfCacheMountDaemonSet:
    @pytest.fixture
    def mount_pod_spec(self, all_daemonsets: dict) -> dict:
        ds = filter_daemonsets(all_daemonsets, namespace=NAMESPACE, name=MOUNT_DS)
        assert ds, f"DaemonSet '{MOUNT_DS}' not found in {NAMESPACE}."
        return ds[0]["spec"]["template"]["spec"]

    def test_daemonset_ready(self, all_daemonsets: dict, all_nodes: dict) -> None:
        # The mount runs on the autoscaling github-runner pool, so a few pods are
        # always mid-init on freshly-scaled nodes. Tolerate ready/desired gaps that
        # are fully explained by unstable nodes (new/NotReady/cordoned/deleting) — but
        # still fail on pods stuck unready on stable nodes (a real mount break).
        assert_daemonset_healthy(
            all_daemonsets,
            all_nodes,
            NAMESPACE,
            name=MOUNT_DS,
            node_selector=RUNNER_NODE_SELECTOR,
        )

    def test_targets_runner_nodes(self, mount_pod_spec: dict) -> None:
        sel = mount_pod_spec.get("nodeSelector", {})
        assert sel.get("workload-type") == "github-runner", (
            "mount DaemonSet must target workload-type=github-runner nodes (where job pods land)."
        )

    def test_container_privileged(self, mount_pod_spec: dict) -> None:
        sc = mount_pod_spec["containers"][0].get("securityContext", {})
        assert sc.get("privileged") is True, "rclone container must be privileged (FUSE + Bidirectional propagation)."

    def test_mount_is_read_only(self, mount_pod_spec: dict) -> None:
        cmd = " ".join(mount_pod_spec["containers"][0].get("command", []))
        assert "--read-only" in cmd, "rclone mount must be --read-only — job pods must not write the shared cache."

    def test_hostpath_bidirectional(self, mount_pod_spec: dict) -> None:
        mounts = mount_pod_spec["containers"][0].get("volumeMounts", [])
        hf = [m for m in mounts if m.get("mountPath") == MOUNT_PATH]
        assert hf, f"rclone container missing {MOUNT_PATH} volume mount."
        assert hf[0].get("mountPropagation") == "Bidirectional", (
            f"{MOUNT_PATH} must use Bidirectional propagation so job pods see the FUSE mount."
        )
        host_vols = [v for v in mount_pod_spec.get("volumes", []) if v.get("hostPath", {}).get("path") == MOUNT_PATH]
        assert host_vols, f"mount DaemonSet must hostPath-mount {MOUNT_PATH}."

    def test_liveness_probe(self, mount_pod_spec: dict) -> None:
        probe = mount_pod_spec["containers"][0].get("livenessProbe")
        assert probe is not None, "rclone container needs a livenessProbe so a hung FUSE mount is restarted."

    def test_taint_remover_sidecar(self, mount_pod_spec: dict) -> None:
        """A taint-remover container clears the scheduling gate once the FUSE is up."""
        names = [c.get("name") for c in mount_pod_spec.get("containers", [])]
        assert "taint-remover" in names, (
            "mount DaemonSet must include the taint-remover container — without it the "
            "node-init.osdc.io/hf-cache startup taint is never cleared and runner pods never schedule."
        )

    def test_taint_remover_unprivileged(self, mount_pod_spec: dict) -> None:
        """taint-remover must NOT be privileged — it waits on rclone's sentinel file, not the host."""
        tr = next(c for c in mount_pod_spec["containers"] if c.get("name") == "taint-remover")
        sc = tr.get("securityContext", {})
        assert sc.get("privileged") is not True, (
            "taint-remover must not be privileged (sentinel handshake, no host access)."
        )
        assert sc.get("allowPrivilegeEscalation") is False, "taint-remover must set allowPrivilegeEscalation: false."
        assert "ALL" in (sc.get("capabilities", {}).get("drop") or []), "taint-remover must drop all capabilities."

    def test_hostpid_enabled(self, mount_pod_spec: dict) -> None:
        """hostPID is required by the prepare-host-mount init (nsenter -t 1 into the host)."""
        assert mount_pod_spec.get("hostPID") is True, (
            "mount DaemonSet needs hostPID for the prepare-host-mount init's nsenter into the host."
        )

    def test_prepare_host_mount_init(self, mount_pod_spec: dict) -> None:
        """An init container must make /mnt/hf_cache an rshared host mount point.

        Without it the rclone Bidirectional FUSE has no shared host peer and never
        propagates to job pods (they see an empty dir).
        """
        inits = mount_pod_spec.get("initContainers", [])
        prep = [c for c in inits if c.get("name") == "prepare-host-mount"]
        assert prep, "mount DaemonSet must run the prepare-host-mount initContainer (rshared host mount point)."
        cmd = " ".join(prep[0].get("command", []))
        assert "make-rshared" in cmd, "prepare-host-mount must make /mnt/hf_cache rshared on the host."
        assert prep[0].get("securityContext", {}).get("privileged") is True, (
            "prepare-host-mount must be privileged to nsenter the host and mount."
        )

    def test_taint_remover_lib_volume(self, mount_pod_spec: dict) -> None:
        vols = mount_pod_spec.get("volumes", [])
        lib = [v for v in vols if v.get("configMap", {}).get("name") == "node-taint-remover-lib"]
        assert lib, "mount DaemonSet must mount the node-taint-remover-lib ConfigMap for the taint-remover."


def _gpu_affinity_op(pod_spec: dict) -> tuple[str, set]:
    """Return (operator, values) of the instance-gpu-name node-affinity requirement."""
    terms = (
        pod_spec.get("affinity", {})
        .get("nodeAffinity", {})
        .get("requiredDuringSchedulingIgnoredDuringExecution", {})
        .get("nodeSelectorTerms", [])
    )
    for term in terms:
        for expr in term.get("matchExpressions", []):
            if expr.get("key") == GPU_NAME_LABEL:
                return expr.get("operator", ""), set(expr.get("values", []))
    return "", set()


class TestHfCacheLargeGpuSplit:
    """The mount runs as two DaemonSets: standard (CPU + small GPU) and -largegpu
    (H100/B200, raised rclone memory). They must be mutually exclusive so exactly
    one rclone mount runs per node, and only the large-GPU one carries the big limit.
    """

    def _pod_spec(self, all_daemonsets: dict, name: str) -> dict:
        ds = filter_daemonsets(all_daemonsets, namespace=NAMESPACE, name=name)
        assert ds, f"DaemonSet '{name}' not found in {NAMESPACE}."
        return ds[0]["spec"]["template"]["spec"]

    def _rclone_mem(self, pod_spec: dict) -> str:
        rclone = next(c for c in pod_spec["containers"] if c.get("name") == "rclone")
        return rclone.get("resources", {}).get("limits", {}).get("memory", "")

    def test_largegpu_daemonset_targets_h100_b200(self, all_daemonsets: dict) -> None:
        op, values = _gpu_affinity_op(self._pod_spec(all_daemonsets, MOUNT_DS_LARGEGPU))
        assert values == LARGE_GPU_VALUES, f"{MOUNT_DS_LARGEGPU} must target {LARGE_GPU_VALUES}; got {values}."
        assert op == "In", f"{MOUNT_DS_LARGEGPU} must use In affinity to select large-GPU nodes; got op={op}."

    def test_standard_daemonset_excludes_h100_b200(self, all_daemonsets: dict) -> None:
        op, values = _gpu_affinity_op(self._pod_spec(all_daemonsets, MOUNT_DS))
        assert values == LARGE_GPU_VALUES, f"{MOUNT_DS} affinity must reference {LARGE_GPU_VALUES}; got {values}."
        assert op == "NotIn", (
            f"{MOUNT_DS} must use NotIn affinity so it doesn't double-mount large-GPU nodes; got op={op}."
        )

    def test_largegpu_has_more_memory(self, all_daemonsets: dict) -> None:
        std = self._rclone_mem(self._pod_spec(all_daemonsets, MOUNT_DS))
        big = self._rclone_mem(self._pod_spec(all_daemonsets, MOUNT_DS_LARGEGPU))

        def gib(v: str) -> float:
            return float(v[:-2]) if v.endswith("Gi") else float(v[:-2]) / 1024 if v.endswith("Mi") else float(v)

        assert gib(big) > gib(std), (
            f"{MOUNT_DS_LARGEGPU} rclone memory limit ({big}) must exceed the standard one ({std})."
        )


class TestHfCacheTaintGate:
    """The startup-taint gate: lib ConfigMap + RBAC must exist so the taint is removable."""

    def test_taint_remover_lib_configmap_exists(self) -> None:
        result = run_kubectl(["get", "configmap", "node-taint-remover-lib"], namespace=NAMESPACE)
        assert "taint_remover.py" in result.get("data", {}), (
            "node-taint-remover-lib ConfigMap in hf-cache ns missing taint_remover.py — deploy.sh did not render it."
        )

    def test_taint_remover_rbac(self) -> None:
        """The hf-cache-mount SA needs get/patch on nodes to clear its taint."""
        role = run_kubectl(["get", "clusterrole", "hf-cache-taint-remover"])
        verbs = {v for rule in role.get("rules", []) for v in rule.get("verbs", [])}
        assert {"get", "patch"} <= verbs, (
            f"hf-cache-taint-remover ClusterRole must allow get+patch on nodes, got {verbs}"
        )
        binding = run_kubectl(["get", "clusterrolebinding", "hf-cache-taint-remover"])
        subjects = [(s.get("kind"), s.get("name"), s.get("namespace")) for s in binding.get("subjects", [])]
        assert ("ServiceAccount", MOUNT_SA, NAMESPACE) in subjects, (
            f"hf-cache-taint-remover binding must bind SA {MOUNT_SA}/{NAMESPACE}, got {subjects}"
        )


class TestHfCacheRunnerWiring:
    """The arc-runners job-pod template must carry the /mnt/hf_cache mount when hf-cache is enabled."""

    def test_job_pod_template_mounts_cache(self, enabled_modules: list[str]) -> None:
        if "arc-runners" not in enabled_modules:
            pytest.skip("arc-runners not enabled — no job-pod template to check")
        cms = run_kubectl(["get", "configmap"], namespace="arc-runners")
        hooks = [c for c in cms.get("items", []) if c["metadata"]["name"].startswith("arc-runner-hook-")]
        assert hooks, "No arc-runner-hook-* ConfigMaps in arc-runners — runners not generated?"
        wired = [c for c in hooks if MOUNT_PATH in c.get("data", {}).get("job-pod.yaml", "")]
        assert wired, (
            f"No arc-runner-hook job-pod.yaml mounts {MOUNT_PATH} — the BEGIN_HF_CACHE block was not rendered. "
            "Re-deploy arc-runners after enabling hf-cache."
        )
