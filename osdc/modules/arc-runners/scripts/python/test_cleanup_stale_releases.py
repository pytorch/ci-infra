"""Tests for cleanup_stale_releases.py."""

from cleanup_stale_releases import (
    expected_runner_names,
    find_orphaned_secrets,
    find_stale_runners,
    normalize_name,
    stale_release_names,
)

# ============================================================================
# normalize_name (mirrors generate_runners.py — tested here for completeness)
# ============================================================================


class TestNormalizeName:
    def test_dots_replaced(self):
        assert normalize_name("a.b.c") == "a-b-c"

    def test_underscores_replaced(self):
        assert normalize_name("a_b_c") == "a-b-c"

    def test_mixed(self):
        assert normalize_name("x86.avx_512") == "x86-avx-512"

    def test_already_clean(self):
        assert normalize_name("my-runner") == "my-runner"

    def test_empty_string(self):
        assert normalize_name("") == ""


# ============================================================================
# expected_runner_names
# ============================================================================


class TestExpectedRunnerNames:
    def test_strips_yaml_suffix_and_normalizes(self):
        filenames = ["a.linux.cpu.yaml", "b_gpu_runner.yaml", "clean-name.yaml"]
        result = expected_runner_names(filenames)
        assert result == ["a-linux-cpu", "b-gpu-runner", "clean-name"]

    def test_empty_list(self):
        assert expected_runner_names([]) == []

    def test_single_file(self):
        assert expected_runner_names(["runner.yaml"]) == ["runner"]

    def test_no_yaml_suffix(self):
        # Defensive: if filename doesn't end in .yaml, removesuffix is a no-op
        assert expected_runner_names(["runner.txt"]) == ["runner-txt"]


# ============================================================================
# find_stale_runners
# ============================================================================


class TestFindStaleRunners:
    def test_no_stale_when_all_match(self):
        expected = ["cpu-runner", "gpu-runner"]
        deployed = ["arc-runner-hook-cpu-runner", "arc-runner-hook-gpu-runner"]
        assert find_stale_runners(expected, deployed) == []

    def test_stale_detected(self):
        expected = ["cpu-runner"]
        deployed = ["arc-runner-hook-cpu-runner", "arc-runner-hook-old-runner"]
        assert find_stale_runners(expected, deployed) == ["old-runner"]

    def test_multiple_stale(self):
        expected = ["runner-a"]
        deployed = [
            "arc-runner-hook-runner-a",
            "arc-runner-hook-runner-b",
            "arc-runner-hook-runner-c",
        ]
        result = find_stale_runners(expected, deployed)
        assert sorted(result) == ["runner-b", "runner-c"]

    def test_empty_expected(self):
        deployed = ["arc-runner-hook-runner-a"]
        assert find_stale_runners([], deployed) == ["runner-a"]

    def test_empty_deployed(self):
        assert find_stale_runners(["runner-a"], []) == []

    def test_both_empty(self):
        assert find_stale_runners([], []) == []

    def test_ignores_non_prefixed_configmaps(self):
        """ConfigMaps without the arc-runner-hook- prefix are skipped."""
        expected = ["runner-a"]
        deployed = [
            "arc-runner-hook-runner-a",
            "some-other-configmap",
            "arc-controller-config",
        ]
        assert find_stale_runners(expected, deployed) == []

    def test_preserves_order(self):
        expected = []
        deployed = [
            "arc-runner-hook-z-runner",
            "arc-runner-hook-a-runner",
            "arc-runner-hook-m-runner",
        ]
        result = find_stale_runners(expected, deployed)
        assert result == ["z-runner", "a-runner", "m-runner"]


# ============================================================================
# stale_release_names
# ============================================================================


class TestStaleReleaseNames:
    def test_adds_arc_prefix(self):
        assert stale_release_names(["old-runner", "removed-gpu"]) == [
            "arc-old-runner",
            "arc-removed-gpu",
        ]

    def test_empty(self):
        assert stale_release_names([]) == []

    def test_single(self):
        assert stale_release_names(["foo"]) == ["arc-foo"]


# ============================================================================
# find_orphaned_secrets
# ============================================================================


class TestFindOrphanedSecrets:
    def test_identifies_orphans_from_stale_release(self):
        stale = ["arc-old-runner"]
        secrets = [
            {"secret_name": "sh.helm.release.v1.arc-old-runner.v1", "release_name": "arc-old-runner"},
            {"secret_name": "sh.helm.release.v1.arc-old-runner.v2", "release_name": "arc-old-runner"},
            {"secret_name": "sh.helm.release.v1.arc-active.v1", "release_name": "arc-active"},
        ]
        result = find_orphaned_secrets(stale, secrets)
        assert result == [
            "sh.helm.release.v1.arc-old-runner.v1",
            "sh.helm.release.v1.arc-old-runner.v2",
        ]

    def test_no_orphans_when_no_stale(self):
        secrets = [
            {"secret_name": "sh.helm.release.v1.arc-active.v1", "release_name": "arc-active"},
        ]
        assert find_orphaned_secrets([], secrets) == []

    def test_no_orphans_when_no_secrets(self):
        assert find_orphaned_secrets(["arc-old"], []) == []

    def test_multiple_stale_releases(self):
        stale = ["arc-old-a", "arc-old-b"]
        secrets = [
            {"secret_name": "sh.helm.release.v1.arc-old-a.v1", "release_name": "arc-old-a"},
            {"secret_name": "sh.helm.release.v1.arc-old-b.v3", "release_name": "arc-old-b"},
            {"secret_name": "sh.helm.release.v1.arc-active.v1", "release_name": "arc-active"},
        ]
        result = find_orphaned_secrets(stale, secrets)
        assert result == [
            "sh.helm.release.v1.arc-old-a.v1",
            "sh.helm.release.v1.arc-old-b.v3",
        ]

    def test_active_releases_never_deleted(self):
        """Even with many secrets, active releases are always preserved."""
        stale = ["arc-removed"]
        secrets = [
            {"secret_name": "sh.helm.release.v1.arc-active.v1", "release_name": "arc-active"},
            {"secret_name": "sh.helm.release.v1.arc-active.v2", "release_name": "arc-active"},
            {"secret_name": "sh.helm.release.v1.arc-active.v3", "release_name": "arc-active"},
        ]
        assert find_orphaned_secrets(stale, secrets) == []

    def test_missing_release_name_key(self):
        """Secrets with missing release_name are skipped (not crashed on)."""
        stale = ["arc-old"]
        secrets = [
            {"secret_name": "corrupt-secret"},
            {"secret_name": "sh.helm.release.v1.arc-old.v1", "release_name": "arc-old"},
        ]
        result = find_orphaned_secrets(stale, secrets)
        assert result == ["sh.helm.release.v1.arc-old.v1"]

    def test_empty_release_name_not_matched(self):
        """A secret with empty release_name is not matched as stale."""
        stale = ["arc-old"]
        secrets = [
            {"secret_name": "weird-secret", "release_name": ""},
        ]
        assert find_orphaned_secrets(stale, secrets) == []


# ============================================================================
# Integration: end-to-end stale detection pipeline
# ============================================================================


class TestEndToEnd:
    def test_full_pipeline(self):
        """Simulate the full Steps 4+5 pipeline."""
        # Step 1: generated files (current defs)
        generated_files = ["a.linux.cpu.yaml", "b.linux.gpu.yaml"]
        expected = expected_runner_names(generated_files)
        assert expected == ["a-linux-cpu", "b-linux-gpu"]

        # Step 4: compare against deployed ConfigMaps
        deployed_cms = [
            "arc-runner-hook-a-linux-cpu",  # still active
            "arc-runner-hook-b-linux-gpu",  # still active
            "arc-runner-hook-old-arm-runner",  # removed from defs
        ]
        stale = find_stale_runners(expected, deployed_cms)
        assert stale == ["old-arm-runner"]

        # Convert to Helm release names
        releases = stale_release_names(stale)
        assert releases == ["arc-old-arm-runner"]

        # Step 5: find orphaned secrets
        helm_secrets = [
            # Active runner secrets (preserved)
            {"secret_name": "sh.helm.release.v1.arc-a-linux-cpu.v1", "release_name": "arc-a-linux-cpu"},
            {"secret_name": "sh.helm.release.v1.arc-a-linux-cpu.v2", "release_name": "arc-a-linux-cpu"},
            {"secret_name": "sh.helm.release.v1.arc-b-linux-gpu.v1", "release_name": "arc-b-linux-gpu"},
            # Stale runner secrets (orphaned — should be deleted)
            {"secret_name": "sh.helm.release.v1.arc-old-arm-runner.v1", "release_name": "arc-old-arm-runner"},
            {"secret_name": "sh.helm.release.v1.arc-old-arm-runner.v2", "release_name": "arc-old-arm-runner"},
            {"secret_name": "sh.helm.release.v1.arc-old-arm-runner.v3", "release_name": "arc-old-arm-runner"},
        ]
        orphans = find_orphaned_secrets(releases, helm_secrets)
        assert orphans == [
            "sh.helm.release.v1.arc-old-arm-runner.v1",
            "sh.helm.release.v1.arc-old-arm-runner.v2",
            "sh.helm.release.v1.arc-old-arm-runner.v3",
        ]

    def test_multi_module_isolation(self):
        """Two modules in the same namespace don't interfere with each other.

        arc-runners deploys cpu/gpu runners, arc-runners-b200 deploys B200 runners.
        When arc-runners cleans up, it must NOT touch B200 secrets.
        """
        # Module 1 (arc-runners): expected runners
        mod1_expected = expected_runner_names(["linux-cpu.yaml"])

        # Module 1's deployed ConfigMaps (scoped by osdc.io/module=arc-runners)
        mod1_deployed_cms = [
            "arc-runner-hook-linux-cpu",
            "arc-runner-hook-old-runner",  # stale in module 1
        ]
        mod1_stale = find_stale_runners(mod1_expected, mod1_deployed_cms)
        assert mod1_stale == ["old-runner"]

        mod1_releases = stale_release_names(mod1_stale)
        assert mod1_releases == ["arc-old-runner"]

        # ALL Helm secrets in namespace (from both modules)
        all_secrets = [
            # Module 1 active
            {"secret_name": "sh.helm.release.v1.arc-linux-cpu.v1", "release_name": "arc-linux-cpu"},
            # Module 1 stale (should be deleted)
            {"secret_name": "sh.helm.release.v1.arc-old-runner.v1", "release_name": "arc-old-runner"},
            {"secret_name": "sh.helm.release.v1.arc-old-runner.v2", "release_name": "arc-old-runner"},
            # Module 2 (B200) — must NOT be touched
            {"secret_name": "sh.helm.release.v1.arc-a-linux-b200.v1", "release_name": "arc-a-linux-b200"},
            {"secret_name": "sh.helm.release.v1.arc-a-linux-b200.v2", "release_name": "arc-a-linux-b200"},
        ]

        orphans = find_orphaned_secrets(mod1_releases, all_secrets)
        # Only module 1's stale secrets are returned
        assert orphans == [
            "sh.helm.release.v1.arc-old-runner.v1",
            "sh.helm.release.v1.arc-old-runner.v2",
        ]
        # B200 secrets are NOT in the orphan list
        b200_names = [s["secret_name"] for s in all_secrets if "b200" in s["secret_name"]]
        for b200 in b200_names:
            assert b200 not in orphans

    def test_nothing_stale_no_orphans(self):
        """When all runners are current, no cleanup happens."""
        expected = expected_runner_names(["runner-a.yaml", "runner-b.yaml"])
        deployed = ["arc-runner-hook-runner-a", "arc-runner-hook-runner-b"]
        stale = find_stale_runners(expected, deployed)
        assert stale == []
        releases = stale_release_names(stale)
        assert releases == []
        # Step 5 would skip entirely (STALE_RELEASES is empty)
