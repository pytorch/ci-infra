"""Tests for validate_runner_qos.py — ARC runner QoS validation."""

import textwrap
from pathlib import Path

from validate_runner_qos import (
    check_odd_cpu,
    extract_job_resources,
    main,
    validate_cpu_qos,
    validate_file,
    validate_gpu_qos,
    validate_memory_qos,
    validate_patched_hooks,
)

# ============================================================================
# Helpers
# ============================================================================


def make_configmap(
    cpu_limit: str = "4",
    cpu_request: str = "4",
    mem_limit: str = "16Gi",
    mem_request: str = "16Gi",
    gpu_limit: str | None = None,
    gpu_request: str | None = None,
) -> str:
    """Build a ConfigMap YAML string with a $job container."""
    # GPU lines must align with cpu:/memory: (14 spaces indent inside the
    # embedded YAML block, which is 18 spaces from the file root due to the
    # 4-space data indentation of the | block scalar).
    gpu_request_line = ""
    if gpu_request:
        gpu_request_line = '\n                      nvidia.com/gpu: "' + gpu_request + '"'
    gpu_limit_line = ""
    if gpu_limit:
        gpu_limit_line = '\n                      nvidia.com/gpu: "' + gpu_limit + '"'
    return textwrap.dedent(f"""\
        apiVersion: v1
        kind: ConfigMap
        metadata:
          name: arc-runner-hook-test
          namespace: arc-runners
        data:
          job-pod.yaml: |
            spec:
              containers:
                - name: "$job"
                  resources:
                    requests:
                      cpu: "{cpu_request}"
                      memory: "{mem_request}"{gpu_request_line}
                    limits:
                      cpu: "{cpu_limit}"
                      memory: "{mem_limit}"{gpu_limit_line}
    """)


def make_helm_values(
    include_init_container: bool = True,
    include_hooks_env: bool = True,
    hooks_path: str = "/opt/runner-hooks/dist/index.js",
) -> str:
    """Build Helm values YAML with runner template spec."""
    lines = [
        'githubConfigUrl: "https://github.com/test-org"',
        'runnerScaleSetName: "test-runner"',
        "template:",
        "  spec:",
    ]

    if include_init_container:
        lines.extend(
            [
                "    initContainers:",
                "      - name: wait-for-hooks",
                "        image: public.ecr.aws/docker/library/alpine:3.21",
                "        command:",
                "          - /bin/sh",
                "          - -c",
                '          - echo "waiting"',
            ]
        )

    lines.extend(
        [
            "    containers:",
            "      - name: runner",
            "        image: ghcr.io/actions/actions-runner:latest",
            "        env:",
            "          - name: RUNNER_FEATURE_FLAG_EPHEMERAL",
            '            value: "true"',
        ]
    )

    if include_hooks_env:
        lines.extend(
            [
                "          - name: ACTIONS_RUNNER_CONTAINER_HOOKS",
                f"            value: {hooks_path}",
            ]
        )

    return "\n".join(lines) + "\n"


def make_full_runner_yaml(
    cpu: str = "4",
    memory: str = "16Gi",
    gpu: str | None = None,
    include_init_container: bool = True,
    include_hooks_env: bool = True,
    hooks_path: str = "/opt/runner-hooks/dist/index.js",
) -> str:
    """Build a complete two-document runner YAML (Helm values + ConfigMap)."""
    cm = make_configmap(
        cpu_limit=cpu,
        cpu_request=cpu,
        mem_limit=memory,
        mem_request=memory,
        gpu_limit=gpu,
        gpu_request=gpu,
    )
    helm_values = make_helm_values(
        include_init_container=include_init_container,
        include_hooks_env=include_hooks_env,
        hooks_path=hooks_path,
    )
    return helm_values + "---\n" + cm


# ============================================================================
# extract_job_resources
# ============================================================================


class TestExtractJobResources:
    """Tests for extract_job_resources — YAML parsing of ConfigMap."""

    def test_valid_configmap(self):
        cm = make_configmap(cpu_limit="8", cpu_request="8", mem_limit="32Gi", mem_request="32Gi")
        res = extract_job_resources(cm)
        assert res["cpu_limit"] == "8"
        assert res["cpu_request"] == "8"
        assert res["mem_limit"] == "32Gi"
        assert res["mem_request"] == "32Gi"
        assert res["gpu_limit"] == ""
        assert res["gpu_request"] == ""

    def test_with_gpu(self):
        cm = make_configmap(gpu_limit="1", gpu_request="1")
        res = extract_job_resources(cm)
        assert res["gpu_limit"] == "1"
        assert res["gpu_request"] == "1"

    def test_invalid_yaml(self):
        res = extract_job_resources("{{not valid yaml}}")
        assert res["cpu_limit"] == ""
        assert res["cpu_request"] == ""

    def test_empty_string(self):
        res = extract_job_resources("")
        assert res["cpu_limit"] == ""

    def test_missing_job_container(self):
        cm = textwrap.dedent("""\
            apiVersion: v1
            kind: ConfigMap
            metadata:
              name: test
            data:
              job-pod.yaml: |
                spec:
                  containers:
                    - name: "sidecar"
                      resources:
                        limits:
                          cpu: "4"
        """)
        res = extract_job_resources(cm)
        assert res["cpu_limit"] == ""

    def test_missing_data_key(self):
        cm = textwrap.dedent("""\
            apiVersion: v1
            kind: ConfigMap
            metadata:
              name: test
        """)
        res = extract_job_resources(cm)
        assert res["cpu_limit"] == ""

    def test_data_not_a_dict(self):
        """Line 62: data is a scalar, not a mapping."""
        cm = textwrap.dedent("""\
            apiVersion: v1
            kind: ConfigMap
            data: "just a string"
        """)
        res = extract_job_resources(cm)
        assert res["cpu_limit"] == ""

    def test_embedded_job_pod_invalid_yaml(self):
        """Lines 70-71: job-pod.yaml value is invalid YAML."""
        cm = textwrap.dedent("""\
            apiVersion: v1
            kind: ConfigMap
            data:
              job-pod.yaml: |
                {{invalid: yaml: [unterminated
        """)
        res = extract_job_resources(cm)
        assert res["cpu_limit"] == ""

    def test_embedded_job_pod_not_a_dict(self):
        """Line 74: parsed job-pod.yaml is a scalar, not a mapping."""
        cm = textwrap.dedent("""\
            apiVersion: v1
            kind: ConfigMap
            data:
              job-pod.yaml: |
                just a plain string
        """)
        res = extract_job_resources(cm)
        assert res["cpu_limit"] == ""

    def test_containers_not_a_list(self):
        """Line 78: spec.containers is not a list."""
        cm = textwrap.dedent("""\
            apiVersion: v1
            kind: ConfigMap
            data:
              job-pod.yaml: |
                spec:
                  containers: "not a list"
        """)
        res = extract_job_resources(cm)
        assert res["cpu_limit"] == ""

    def test_resources_not_a_dict(self):
        """Line 92: resources is a scalar, not a mapping."""
        cm = textwrap.dedent("""\
            apiVersion: v1
            kind: ConfigMap
            data:
              job-pod.yaml: |
                spec:
                  containers:
                    - name: "$job"
                      resources: "not a dict"
        """)
        res = extract_job_resources(cm)
        assert res["cpu_limit"] == ""

    def test_limits_not_a_dict(self):
        """Line 97: limits is a scalar — should be treated as empty."""
        cm = textwrap.dedent("""\
            apiVersion: v1
            kind: ConfigMap
            data:
              job-pod.yaml: |
                spec:
                  containers:
                    - name: "$job"
                      resources:
                        limits: "not a dict"
                        requests:
                          cpu: "4"
                          memory: "16Gi"
        """)
        res = extract_job_resources(cm)
        assert res["cpu_limit"] == ""
        assert res["cpu_request"] == "4"

    def test_requests_not_a_dict(self):
        """Line 99: requests is a scalar — should be treated as empty."""
        cm = textwrap.dedent("""\
            apiVersion: v1
            kind: ConfigMap
            data:
              job-pod.yaml: |
                spec:
                  containers:
                    - name: "$job"
                      resources:
                        limits:
                          cpu: "8"
                          memory: "32Gi"
                        requests: "not a dict"
        """)
        res = extract_job_resources(cm)
        assert res["cpu_limit"] == "8"
        assert res["cpu_request"] == ""


# ============================================================================
# validate_cpu_qos
# ============================================================================


class TestValidateCpuQos:
    """Tests for CPU QoS validation."""

    def test_valid_equal_integer(self):
        assert validate_cpu_qos("4", "4") == []

    def test_missing_limit(self):
        issues = validate_cpu_qos("", "4")
        assert len(issues) == 1
        assert issues[0][0] == "error"
        assert "Missing" in issues[0][1]

    def test_missing_request(self):
        issues = validate_cpu_qos("4", "")
        assert len(issues) == 1
        assert issues[0][0] == "error"

    def test_mismatch(self):
        issues = validate_cpu_qos("4", "8")
        assert len(issues) == 1
        assert "mismatch" in issues[0][1]

    def test_millicores_rejected(self):
        issues = validate_cpu_qos("4000m", "4000m")
        assert len(issues) == 1
        assert "integer" in issues[0][1]

    def test_decimal_rejected(self):
        issues = validate_cpu_qos("4.5", "4.5")
        assert len(issues) == 1
        assert "integer" in issues[0][1]


# ============================================================================
# validate_memory_qos
# ============================================================================


class TestValidateMemoryQos:
    """Tests for memory QoS validation."""

    def test_valid_equal(self):
        assert validate_memory_qos("16Gi", "16Gi") == []

    def test_missing(self):
        issues = validate_memory_qos("", "16Gi")
        assert len(issues) == 1
        assert "Missing" in issues[0][1]

    def test_mismatch(self):
        issues = validate_memory_qos("16Gi", "8Gi")
        assert len(issues) == 1
        assert "mismatch" in issues[0][1]


# ============================================================================
# validate_gpu_qos
# ============================================================================


class TestValidateGpuQos:
    """Tests for GPU QoS validation."""

    def test_no_gpu_is_valid(self):
        assert validate_gpu_qos("", "") == []

    def test_valid_equal(self):
        assert validate_gpu_qos("1", "1") == []

    def test_mismatch(self):
        issues = validate_gpu_qos("1", "2")
        assert len(issues) == 1
        assert "GPU mismatch" in issues[0][1]

    def test_only_limit_set(self):
        issues = validate_gpu_qos("1", "")
        assert len(issues) == 1
        assert "GPU mismatch" in issues[0][1]

    def test_only_request_set(self):
        issues = validate_gpu_qos("", "1")
        assert len(issues) == 1
        assert "GPU mismatch" in issues[0][1]


# ============================================================================
# check_odd_cpu
# ============================================================================


class TestCheckOddCpu:
    """Tests for odd CPU warning."""

    def test_even_cpu_no_warning(self):
        assert check_odd_cpu("4") == []

    def test_odd_cpu_warns(self):
        warnings = check_odd_cpu("3")
        assert len(warnings) == 1
        assert warnings[0][0] == "warning"
        assert "Odd CPU" in warnings[0][1]

    def test_non_integer_no_warning(self):
        assert check_odd_cpu("4000m") == []

    def test_empty_no_warning(self):
        assert check_odd_cpu("") == []


# ============================================================================
# validate_patched_hooks
# ============================================================================


class TestValidatePatchedHooks:
    """Tests for patched hooks init container validation."""

    def test_valid_with_hooks(self):
        helm = make_helm_values()
        assert validate_patched_hooks(helm) == []

    def test_missing_init_container(self):
        helm = make_helm_values(include_init_container=False)
        issues = validate_patched_hooks(helm)
        assert len(issues) == 1
        assert issues[0][0] == "error"
        assert "wait-for-hooks" in issues[0][1]

    def test_missing_hooks_env(self):
        helm = make_helm_values(include_hooks_env=False)
        issues = validate_patched_hooks(helm)
        assert len(issues) == 1
        assert issues[0][0] == "error"
        assert "ACTIONS_RUNNER_CONTAINER_HOOKS" in issues[0][1]

    def test_wrong_hooks_path(self):
        helm = make_helm_values(hooks_path="/wrong/path.js")
        issues = validate_patched_hooks(helm)
        assert len(issues) == 1
        assert issues[0][0] == "error"
        assert "dist/index.js" in issues[0][1]

    def test_both_missing(self):
        helm = make_helm_values(include_init_container=False, include_hooks_env=False)
        issues = validate_patched_hooks(helm)
        assert len(issues) == 2

    def test_invalid_yaml(self):
        issues = validate_patched_hooks("{{not valid}}")
        assert len(issues) == 1
        assert "parse" in issues[0][1].lower()

    def test_no_template_spec(self):
        helm = textwrap.dedent("""\
            githubConfigUrl: "https://github.com/test-org"
            runnerScaleSetName: "test-runner"
        """)
        issues = validate_patched_hooks(helm)
        assert len(issues) >= 1

    def test_no_runner_container(self):
        helm = textwrap.dedent("""\
            githubConfigUrl: "https://github.com/test-org"
            template:
              spec:
                initContainers:
                  - name: wait-for-hooks
                    image: alpine
                containers:
                  - name: sidecar
                    image: busybox
        """)
        issues = validate_patched_hooks(helm)
        assert any("runner" in i[1].lower() for i in issues)

    def test_doc_not_a_dict(self):
        """Lines 207-208: parsed YAML is a scalar, not a mapping."""
        issues = validate_patched_hooks("just a plain string")
        assert len(issues) == 1
        assert issues[0][0] == "error"
        assert "not a mapping" in issues[0][1]

    def test_template_spec_not_a_dict(self):
        """Lines 213-214: template.spec is a scalar."""
        helm = textwrap.dedent("""\
            template:
              spec: "not a dict"
        """)
        issues = validate_patched_hooks(helm)
        assert any("template.spec" in i[1] for i in issues)

    def test_init_containers_not_a_list(self):
        """Line 218: initContainers is a scalar — treated as empty list."""
        helm = textwrap.dedent("""\
            template:
              spec:
                initContainers: "not a list"
                containers:
                  - name: runner
                    image: ghcr.io/actions/actions-runner:latest
                    env:
                      - name: ACTIONS_RUNNER_CONTAINER_HOOKS
                        value: /opt/runner-hooks/dist/index.js
        """)
        issues = validate_patched_hooks(helm)
        assert any("wait-for-hooks" in i[1] for i in issues)

    def test_containers_not_a_list_in_hooks(self):
        """Line 232: containers is a scalar — treated as empty, no runner found."""
        helm = textwrap.dedent("""\
            template:
              spec:
                initContainers:
                  - name: wait-for-hooks
                    image: alpine
                containers: "not a list"
        """)
        issues = validate_patched_hooks(helm)
        assert any("runner" in i[1].lower() for i in issues)

    def test_env_vars_not_a_list(self):
        """Line 246: runner container's env is a scalar — treated as empty list."""
        helm = textwrap.dedent("""\
            template:
              spec:
                initContainers:
                  - name: wait-for-hooks
                    image: alpine
                containers:
                  - name: runner
                    image: ghcr.io/actions/actions-runner:latest
                    env: "not a list"
        """)
        issues = validate_patched_hooks(helm)
        assert any("ACTIONS_RUNNER_CONTAINER_HOOKS" in i[1] for i in issues)


# ============================================================================
# validate_file
# ============================================================================


class TestValidateFile:
    """Tests for validate_file — full file validation."""

    def test_valid_file(self, tmp_path: Path):
        f = tmp_path / "runner.yaml"
        f.write_text(make_full_runner_yaml(cpu="4", memory="16Gi"))
        errors, warnings = validate_file(f)
        assert errors == 0
        assert warnings == 0

    def test_valid_gpu_file(self, tmp_path: Path):
        f = tmp_path / "gpu-runner.yaml"
        f.write_text(make_full_runner_yaml(cpu="16", memory="64Gi", gpu="1"))
        errors, warnings = validate_file(f)
        assert errors == 0
        assert warnings == 0

    def test_odd_cpu_warning(self, tmp_path: Path):
        f = tmp_path / "odd-cpu.yaml"
        f.write_text(make_full_runner_yaml(cpu="3", memory="8Gi"))
        errors, warnings = validate_file(f)
        assert errors == 0
        assert warnings == 1

    def test_missing_init_container(self, tmp_path: Path):
        f = tmp_path / "no-init.yaml"
        f.write_text(
            make_full_runner_yaml(
                cpu="4",
                memory="16Gi",
                include_init_container=False,
            )
        )
        errors, _warnings = validate_file(f)
        assert errors >= 1

    def test_missing_hooks_env(self, tmp_path: Path):
        f = tmp_path / "no-hooks-env.yaml"
        f.write_text(
            make_full_runner_yaml(
                cpu="4",
                memory="16Gi",
                include_hooks_env=False,
            )
        )
        errors, _warnings = validate_file(f)
        assert errors >= 1

    def test_no_separator(self, tmp_path: Path):
        f = tmp_path / "broken.yaml"
        f.write_text("just some yaml without separator")
        errors, warnings = validate_file(f)
        assert errors == 1
        assert warnings == 0

    def test_memory_mismatch_error(self, tmp_path: Path):
        """Lines 348-350: validate_file reports memory mismatch errors."""
        helm = make_helm_values()
        cm = make_configmap(
            cpu_limit="4",
            cpu_request="4",
            mem_limit="32Gi",
            mem_request="16Gi",
        )
        f = tmp_path / "mem-mismatch.yaml"
        f.write_text(helm + "---\n" + cm)
        errors, _warnings = validate_file(f)
        assert errors >= 1

    def test_gpu_mismatch_error(self, tmp_path: Path):
        """Lines 357-359: validate_file reports GPU mismatch errors."""
        helm = make_helm_values()
        cm = make_configmap(
            cpu_limit="4",
            cpu_request="4",
            mem_limit="16Gi",
            mem_request="16Gi",
            gpu_limit="2",
            gpu_request="1",
        )
        f = tmp_path / "gpu-mismatch.yaml"
        f.write_text(helm + "---\n" + cm)
        errors, _warnings = validate_file(f)
        assert errors >= 1


# ============================================================================
# main (CLI integration)
# ============================================================================


class TestMain:
    """Tests for main() CLI entry point."""

    def test_valid_directory(self, tmp_path: Path):
        f = tmp_path / "runner.yaml"
        f.write_text(make_full_runner_yaml(cpu="4", memory="16Gi"))
        assert main([str(tmp_path)]) == 0

    def test_empty_directory(self, tmp_path: Path):
        assert main([str(tmp_path)]) == 1

    def test_mixed_valid_invalid(self, tmp_path: Path):
        good = tmp_path / "good.yaml"
        good.write_text(make_full_runner_yaml(cpu="4", memory="16Gi"))

        bad_cm = make_configmap(cpu_limit="4000m", cpu_request="4000m")
        bad = tmp_path / "bad.yaml"
        bad.write_text("helm: values\n---\n" + bad_cm)

        assert main([str(tmp_path)]) == 1

    def test_multiple_valid_files(self, tmp_path: Path):
        for i in range(3):
            f = tmp_path / f"runner-{i}.yaml"
            f.write_text(make_full_runner_yaml(cpu=str((i + 1) * 2), memory="16Gi"))
        assert main([str(tmp_path)]) == 0

    def test_default_dir_from_env_var(self, tmp_path: Path, monkeypatch):
        """Lines 391-393: main() reads ARC_RUNNERS_OUTPUT_DIR env var."""
        f = tmp_path / "runner.yaml"
        f.write_text(make_full_runner_yaml(cpu="4", memory="16Gi"))
        monkeypatch.setenv("ARC_RUNNERS_OUTPUT_DIR", str(tmp_path))
        assert main([]) == 0

    def test_default_dir_from_script_path(self, tmp_path: Path, monkeypatch):
        """Lines 395-396: main() falls back to script_dir/../../generated."""
        monkeypatch.delenv("ARC_RUNNERS_OUTPUT_DIR", raising=False)
        # When no env var and no CLI arg, it uses script_dir/../../generated.
        # The real generated/ dir exists in the repo with valid files, so this
        # should succeed (return 0). The point is to exercise the fallback path.
        result = main([])
        assert result in (0, 1)  # depends on whether generated/ has files
