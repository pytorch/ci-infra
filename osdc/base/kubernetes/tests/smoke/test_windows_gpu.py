"""Smoke tests for Windows GPU support (process-isolated CUDA)."""

import pytest

from helpers import run_kubectl

pytestmark = [pytest.mark.live]

GPU_RESOURCE = "directx.microsoft.com/display"
DEVICE_PLUGIN_DS = "device-plugin-wddm"
SMOKE_JOB = "windows-cuda-smoke-test"
KUBE_SYSTEM = "kube-system"


def _windows_nodes(all_nodes):
    return [
        n
        for n in all_nodes["items"]
        if n["metadata"]["labels"].get("kubernetes.io/os") == "windows"
    ]


class TestWindowsGPU:
    def test_device_plugin_daemonset_exists(self, all_daemonsets):
        names = {d["metadata"]["name"] for d in all_daemonsets["items"]}
        assert DEVICE_PLUGIN_DS in names, f"{DEVICE_PLUGIN_DS} DaemonSet not deployed"

    def test_device_plugin_running_on_windows_node(self, all_pods, all_nodes):
        node_names = {n["metadata"]["name"] for n in _windows_nodes(all_nodes)}
        if not node_names:
            pytest.skip("no Windows node")
        running = any(
            p["metadata"].get("namespace") == KUBE_SYSTEM
            and p["metadata"].get("labels", {}).get("app") == DEVICE_PLUGIN_DS
            and p["spec"].get("nodeName") in node_names
            and p["status"].get("phase") == "Running"
            for p in all_pods["items"]
        )
        assert running, f"{DEVICE_PLUGIN_DS} pod not Running on a Windows node"

    def test_gpu_resource_registered(self, all_nodes):
        nodes = _windows_nodes(all_nodes)
        if not nodes:
            pytest.skip("no Windows node")
        for node in nodes:
            capacity = node["status"].get("capacity", {})
            assert capacity.get(GPU_RESOURCE, "0") != "0", (
                f"{GPU_RESOURCE} not registered on node {node['metadata']['name']}"
            )

    def test_cuda_smoke_passes(self, all_nodes, upstream_dir):
        if not _windows_nodes(all_nodes):
            pytest.skip("no Windows node")
        manifest = upstream_dir / "base/kubernetes/tests/windows-cuda-smoke-test.yaml"
        run_kubectl(["delete", "job", SMOKE_JOB, "--ignore-not-found"], json_output=False)
        run_kubectl(["apply", "-f", str(manifest)], json_output=False)
        run_kubectl(
            ["wait", "--for=condition=complete", f"job/{SMOKE_JOB}", "--timeout=600s"],
            json_output=False,
        )
        logs = run_kubectl(["logs", f"job/{SMOKE_JOB}"], json_output=False)
        assert "Result = PASS" in logs, f"CUDA smoke test did not pass:\n{logs}"
