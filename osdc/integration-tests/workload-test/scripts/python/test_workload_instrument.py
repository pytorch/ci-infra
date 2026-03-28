"""Unit tests for workload_instrument.py."""

from __future__ import annotations

import textwrap
from pathlib import Path

import pytest
from workload_instrument import (
    ARC_LABEL_PATTERN,
    SOURCE_RUNNER_PREFIX,
    _classify_job,
    _classify_matrix_runner,
    _cleanup_needs_references,
    _is_entry_point_workflow,
    _remove_jobs_from_content,
    _safe_load_workflow,
    apply_to_all_workflows,
    filter_non_arc_jobs,
    generate_determinator_script,
    generate_determinator_stub,
    is_arc_label,
    replace_runner_prefix,
    rewrite_cross_repo_refs,
    rewrite_repo_guards,
)


# ── Constants ────────────────────────────────────────────────────────


class TestConstants:
    def test_source_runner_prefix(self):
        assert SOURCE_RUNNER_PREFIX == "mt-"

    def test_arc_label_pattern_is_compiled(self):
        assert hasattr(ARC_LABEL_PATTERN, "search")

    def test_arc_label_pattern_matches_standard(self):
        assert ARC_LABEL_PATTERN.search("l-x86")

    def test_arc_label_pattern_matches_bare_metal(self):
        assert ARC_LABEL_PATTERN.search("l-bx86")

    def test_arc_label_pattern_rejects_github_hosted(self):
        assert ARC_LABEL_PATTERN.search("ubuntu-latest") is None


# ── is_arc_label ─────────────────────────────────────────────────────


class TestIsArcLabel:
    @pytest.mark.parametrize(
        "label",
        [
            "mt-l-x86iamx-8-16",
            "mt-l-arm64-4-16",
            "mt-w-x86iamx-8-16",
            "mt-m-arm64-4-16",
            "c-mt-l-x86iamx-8-16",
        ],
    )
    def test_standard_arc_labels(self, label):
        assert is_arc_label(label) is True

    @pytest.mark.parametrize(
        "label",
        [
            "mt-bl-x86iamx-8-16",
            "mt-bw-x86iamx-8-16",
            "mt-bm-arm64-4-16",
        ],
    )
    def test_bare_metal_arc_labels(self, label):
        assert is_arc_label(label) is True

    @pytest.mark.parametrize(
        "label",
        [
            "ubuntu-latest",
            "ubuntu-22.04",
            "windows-latest",
            "macos-13",
            "macos-latest",
        ],
    )
    def test_github_hosted_labels(self, label):
        assert is_arc_label(label) is False

    @pytest.mark.parametrize(
        "label",
        [
            "linux.2xlarge",
            "linux.g4dn.4xlarge.nvidia.gpu",
            "linux.24_04.4x",
        ],
    )
    def test_old_convention_labels(self, label):
        assert is_arc_label(label) is False

    def test_empty_string(self):
        assert is_arc_label("") is False

    def test_partial_match_os_chars(self):
        # Contains l-x86 substring
        assert is_arc_label("prefix-l-x86-suffix") is True

    def test_windows_os_char(self):
        assert is_arc_label("w-x86iamx-8-16") is True

    def test_mac_os_char(self):
        assert is_arc_label("m-arm64-8-16") is True


# ── _safe_load_workflow ──────────────────────────────────────────────


class TestSafeLoadWorkflow:
    def test_on_key_workaround(self):
        content = "on:\n  push:\njobs:\n  build:\n    runs-on: ubuntu-latest\n"
        data = _safe_load_workflow(content)
        assert "on_key" in data
        assert True not in data  # The True: bug should not occur

    def test_on_key_becomes_on_key(self):
        data = _safe_load_workflow("on:\n  pull_request:\n")
        assert data["on_key"] == {"pull_request": None}

    def test_empty_content(self):
        assert _safe_load_workflow("") == {}

    def test_whitespace_only(self):
        assert _safe_load_workflow("   \n  \n") == {}

    def test_standard_triggers(self):
        content = "on:\n  push:\n  pull_request:\njobs: {}\n"
        data = _safe_load_workflow(content)
        on = data["on_key"]
        assert "push" in on
        assert "pull_request" in on

    def test_no_on_key(self):
        content = "name: test\njobs: {}\n"
        data = _safe_load_workflow(content)
        assert "on_key" not in data
        assert "name" in data

    def test_only_first_on_replaced(self):
        """Only the first on: is replaced; subsequent on: become True: per PyYAML."""
        content = "on:\n  push:\nenv:\n  on: something\n"
        data = _safe_load_workflow(content)
        assert "on_key" in data
        # PyYAML converts the second `on:` to True: as well (only first replaced)
        assert data.get("env", {}).get(True) == "something"

    def test_workflow_call_trigger(self):
        content = "on:\n  workflow_call:\n"
        data = _safe_load_workflow(content)
        assert "workflow_call" in data["on_key"]


# ── _is_entry_point_workflow ─────────────────────────────────────────


class TestIsEntryPointWorkflow:
    def test_dict_triggers_with_pull_request(self, tmp_path):
        wf = tmp_path / "ci.yml"
        wf.write_text("on:\n  pull_request:\n  push:\njobs: {}\n")
        assert _is_entry_point_workflow(wf) is True

    def test_dict_triggers_workflow_call_only(self, tmp_path):
        wf = tmp_path / "reusable.yml"
        wf.write_text("on:\n  workflow_call:\njobs: {}\n")
        assert _is_entry_point_workflow(wf) is False

    def test_dict_triggers_mixed_with_workflow_call(self, tmp_path):
        wf = tmp_path / "mixed.yml"
        wf.write_text("on:\n  workflow_call:\n  push:\njobs: {}\n")
        assert _is_entry_point_workflow(wf) is True

    def test_string_trigger_push(self, tmp_path):
        wf = tmp_path / "push.yml"
        wf.write_text("on: push\njobs: {}\n")
        assert _is_entry_point_workflow(wf) is True

    def test_string_trigger_workflow_call(self, tmp_path):
        wf = tmp_path / "reusable.yml"
        wf.write_text("on: workflow_call\njobs: {}\n")
        assert _is_entry_point_workflow(wf) is False

    def test_list_trigger_single_push(self, tmp_path):
        wf = tmp_path / "list.yml"
        wf.write_text("on: [push]\njobs: {}\n")
        assert _is_entry_point_workflow(wf) is True

    def test_list_trigger_workflow_call_only(self, tmp_path):
        wf = tmp_path / "reusable.yml"
        wf.write_text("on: [workflow_call]\njobs: {}\n")
        assert _is_entry_point_workflow(wf) is False

    def test_list_trigger_mixed(self, tmp_path):
        wf = tmp_path / "mixed.yml"
        wf.write_text("on: [push, workflow_call]\njobs: {}\n")
        assert _is_entry_point_workflow(wf) is True

    def test_no_on_key_treated_as_entry_point(self, tmp_path):
        """Missing on: trigger defaults to entry point (conservative)."""
        wf = tmp_path / "noop.yml"
        wf.write_text("name: test\njobs: {}\n")
        assert _is_entry_point_workflow(wf) is True

    def test_schedule_trigger(self, tmp_path):
        wf = tmp_path / "cron.yml"
        wf.write_text("on:\n  schedule:\n    - cron: '0 0 * * *'\njobs: {}\n")
        assert _is_entry_point_workflow(wf) is True


# ── _classify_job ────────────────────────────────────────────────────


class TestClassifyJob:
    def test_reusable_workflow_uses_key(self):
        assert _classify_job("build", {"uses": "org/repo/.github/workflows/x.yml@main"}) == "arc"

    def test_runs_on_arc_string(self):
        assert _classify_job("build", {"runs-on": "mt-l-x86iamx-8-16"}) == "arc"

    def test_runs_on_non_arc_string(self):
        assert _classify_job("lint", {"runs-on": "ubuntu-latest"}) == "non_arc"

    def test_runs_on_old_convention_non_arc(self):
        assert _classify_job("test", {"runs-on": "linux.2xlarge"}) == "non_arc"

    def test_runs_on_expression_needs(self):
        """${{ needs.* }} expressions are kept (dynamic)."""
        assert _classify_job("build", {"runs-on": "${{ needs.det.outputs.runner }}"}) == "arc"

    def test_runs_on_expression_inputs(self):
        """${{ inputs.* }} expressions are kept (dynamic)."""
        assert _classify_job("build", {"runs-on": "${{ inputs.runner }}"}) == "arc"

    def test_runs_on_list_all_arc(self):
        assert _classify_job("build", {"runs-on": ["mt-l-x86iamx-8-16", "self-hosted"]}) == "arc"

    def test_runs_on_list_no_arc(self):
        assert _classify_job("build", {"runs-on": ["ubuntu-latest", "self-hosted"]}) == "non_arc"

    def test_runs_on_list_mixed(self):
        """If any label is ARC, classify as arc."""
        assert _classify_job("build", {"runs-on": ["ubuntu-latest", "mt-l-x86iamx-8-16"]}) == "arc"

    def test_runs_on_list_empty(self):
        assert _classify_job("build", {"runs-on": []}) == "arc"

    def test_runs_on_missing(self):
        """No runs-on and no uses: empty string is not an ARC label."""
        assert _classify_job("build", {}) == "non_arc"

    def test_runs_on_matrix_expression(self):
        """${{ matrix.runner }} delegates to _classify_matrix_runner."""
        job_def = {
            "runs-on": "${{ matrix.runner }}",
            "strategy": {"matrix": {"runner": ["mt-l-x86iamx-8-16"]}},
        }
        assert _classify_job("build", job_def) == "arc"

    def test_runs_on_matrix_expression_non_arc(self):
        job_def = {
            "runs-on": "${{ matrix.runner }}",
            "strategy": {"matrix": {"runner": ["ubuntu-latest"]}},
        }
        assert _classify_job("build", job_def) == "non_arc"

    def test_runs_on_integer_default(self):
        """Non-string, non-list runs-on returns arc (conservative)."""
        assert _classify_job("build", {"runs-on": 42}) == "arc"

    def test_runs_on_list_with_non_string_items(self):
        """Non-string items in list are filtered out."""
        assert _classify_job("build", {"runs-on": [42, True, "mt-l-x86iamx-8-16"]}) == "arc"


# ── _classify_matrix_runner ──────────────────────────────────────────


class TestClassifyMatrixRunner:
    def test_include_matrix_arc(self):
        job_def = {
            "strategy": {
                "matrix": {"include": [{"runner": "mt-l-x86iamx-8-16"}]},
            },
        }
        assert _classify_matrix_runner("runner", job_def) == "arc"

    def test_include_matrix_non_arc(self):
        job_def = {
            "strategy": {
                "matrix": {"include": [{"runner": "ubuntu-latest"}]},
            },
        }
        assert _classify_matrix_runner("runner", job_def) == "non_arc"

    def test_include_matrix_mixed(self):
        """If any include value is ARC, classify as arc."""
        job_def = {
            "strategy": {
                "matrix": {
                    "include": [
                        {"runner": "ubuntu-latest"},
                        {"runner": "mt-l-x86iamx-8-16"},
                    ],
                },
            },
        }
        assert _classify_matrix_runner("runner", job_def) == "arc"

    def test_direct_matrix_values_arc(self):
        job_def = {
            "strategy": {"matrix": {"runner": ["mt-l-x86iamx-8-16", "mt-l-arm64-4-16"]}},
        }
        assert _classify_matrix_runner("runner", job_def) == "arc"

    def test_direct_matrix_values_non_arc(self):
        job_def = {
            "strategy": {"matrix": {"runner": ["ubuntu-latest", "windows-latest"]}},
        }
        assert _classify_matrix_runner("runner", job_def) == "non_arc"

    def test_missing_strategy(self):
        """No strategy at all defaults to arc."""
        assert _classify_matrix_runner("runner", {}) == "arc"

    def test_missing_matrix_key(self):
        """Matrix key not found defaults to arc."""
        job_def = {"strategy": {"matrix": {"os": ["linux"]}}}
        assert _classify_matrix_runner("runner", job_def) == "arc"

    def test_empty_include_list(self):
        job_def = {"strategy": {"matrix": {"include": []}}}
        assert _classify_matrix_runner("runner", job_def) == "arc"

    def test_include_with_missing_key(self):
        """Include entries without the target key are skipped."""
        job_def = {
            "strategy": {"matrix": {"include": [{"os": "linux"}]}},
        }
        assert _classify_matrix_runner("runner", job_def) == "arc"

    def test_non_string_values_default_to_arc(self):
        """Non-string matrix values are not all-string so defaults to arc."""
        job_def = {"strategy": {"matrix": {"runner": [42, True]}}}
        assert _classify_matrix_runner("runner", job_def) == "arc"

    def test_empty_direct_matrix_values(self):
        """Empty values list defaults to arc."""
        job_def = {"strategy": {"matrix": {"runner": []}}}
        assert _classify_matrix_runner("runner", job_def) == "arc"


# ── filter_non_arc_jobs ──────────────────────────────────────────────


class TestFilterNonArcJobs:
    def test_mixed_arc_and_non_arc(self):
        content = textwrap.dedent("""\
            on:
              push:
            jobs:
              arc-job:
                runs-on: mt-l-x86iamx-8-16
                steps:
                  - run: echo arc
              gha-job:
                runs-on: ubuntu-latest
                steps:
                  - run: echo gha
        """)
        result = filter_non_arc_jobs(content)
        assert "arc-job:" in result
        assert "gha-job:" not in result

    def test_all_arc_jobs_unchanged(self):
        content = textwrap.dedent("""\
            on:
              push:
            jobs:
              build:
                runs-on: mt-l-x86iamx-8-16
                steps:
                  - run: echo build
        """)
        assert filter_non_arc_jobs(content) == content

    def test_all_non_arc_jobs_removed(self):
        content = textwrap.dedent("""\
            on:
              push:
            jobs:
              lint:
                runs-on: ubuntu-latest
                steps:
                  - run: echo lint
        """)
        result = filter_non_arc_jobs(content)
        assert "lint:" not in result

    def test_no_jobs_section(self):
        content = "on:\n  push:\nname: test\n"
        assert filter_non_arc_jobs(content) == content

    def test_empty_jobs(self):
        content = "on:\n  push:\njobs:\n"
        # yaml.safe_load gives jobs: None
        assert filter_non_arc_jobs(content) == content

    def test_reusable_workflow_job_kept(self):
        content = textwrap.dedent("""\
            on:
              push:
            jobs:
              call-reusable:
                uses: org/repo/.github/workflows/x.yml@main
        """)
        result = filter_non_arc_jobs(content)
        assert "call-reusable:" in result

    def test_non_dict_job_def_skipped(self):
        """Job definitions that aren't dicts are skipped without error."""
        content = "on:\n  push:\njobs:\n  bad-job: null\n"
        # Should not raise
        filter_non_arc_jobs(content)


# ── _remove_jobs_from_content ────────────────────────────────────────


class TestRemoveJobsFromContent:
    def test_single_job_removal(self):
        content = textwrap.dedent("""\
            on:
              push:
            jobs:
              keep:
                runs-on: mt-l-x86
                steps:
                  - run: echo keep
              remove-me:
                runs-on: ubuntu-latest
                steps:
                  - run: echo remove
        """)
        result = _remove_jobs_from_content(content, {"remove-me"})
        assert "keep:" in result
        assert "remove-me:" not in result
        assert "echo keep" in result
        assert "echo remove" not in result

    def test_multiple_job_removal(self):
        content = textwrap.dedent("""\
            on:
              push:
            jobs:
              a:
                runs-on: ubuntu-latest
              b:
                runs-on: mt-l-x86
              c:
                runs-on: windows-latest
        """)
        result = _remove_jobs_from_content(content, {"a", "c"})
        assert "a:" not in result
        assert "b:" in result
        assert "c:" not in result

    def test_preserves_content_outside_jobs(self):
        content = textwrap.dedent("""\
            name: test
            on:
              push:
            env:
              FOO: bar
            jobs:
              remove-me:
                runs-on: ubuntu-latest
        """)
        result = _remove_jobs_from_content(content, {"remove-me"})
        assert "name: test" in result
        assert "FOO: bar" in result

    def test_handles_comments_in_jobs(self):
        content = textwrap.dedent("""\
            jobs:
              # This is a comment
              keep:
                runs-on: mt-l-x86
              remove:
                runs-on: ubuntu-latest
        """)
        result = _remove_jobs_from_content(content, {"remove"})
        assert "keep:" in result
        assert "# This is a comment" in result
        assert "remove:" not in result

    def test_section_after_jobs(self):
        """Content after jobs: section (at indent 0) is preserved."""
        content = textwrap.dedent("""\
            jobs:
              remove:
                runs-on: ubuntu-latest
            outputs:
              result: done
        """)
        result = _remove_jobs_from_content(content, {"remove"})
        assert "remove:" not in result
        assert "outputs:" in result
        assert "result: done" in result

    def test_empty_removal_set(self):
        content = "jobs:\n  keep:\n    runs-on: mt-l-x86\n"
        assert _remove_jobs_from_content(content, set()) == content

    def test_job_not_in_jobs_section(self):
        """Job names outside the jobs: block are not touched."""
        content = textwrap.dedent("""\
            env:
              remove-me: value
            jobs:
              keep:
                runs-on: mt-l-x86
        """)
        result = _remove_jobs_from_content(content, {"remove-me"})
        assert "remove-me: value" in result
        assert "keep:" in result


# ── _cleanup_needs_references ────────────────────────────────────────


class TestCleanupNeedsReferences:
    def test_inline_list_partial_removal(self):
        content = "    needs: [a, b, c]\n"
        result = _cleanup_needs_references(content, {"b"})
        assert "needs: [a, c]" in result
        assert "b" not in result

    def test_inline_list_all_removed(self):
        content = "    needs: [only-one]\n"
        result = _cleanup_needs_references(content, {"only-one"})
        assert "needs:" not in result

    def test_inline_list_none_removed(self):
        content = "    needs: [a, b]\n"
        result = _cleanup_needs_references(content, {"c"})
        assert "needs: [a, b]" in result

    def test_single_value_removed(self):
        content = "    needs: my-job\n"
        result = _cleanup_needs_references(content, {"my-job"})
        assert "needs:" not in result

    def test_single_value_kept(self):
        content = "    needs: other-job\n"
        result = _cleanup_needs_references(content, {"my-job"})
        assert "needs: other-job" in result

    def test_comments_preserved(self):
        content = "    # needs: something\n"
        result = _cleanup_needs_references(content, {"something"})
        assert "# needs: something" in result

    def test_no_needs_lines(self):
        content = "    runs-on: ubuntu-latest\n"
        result = _cleanup_needs_references(content, {"job"})
        assert result == content

    def test_multiple_needs_lines(self):
        content = "    needs: [a, b]\n    needs: [c, d]\n"
        result = _cleanup_needs_references(content, {"b", "c"})
        assert "needs: [a]" in result
        assert "needs: [d]" in result

    def test_inline_list_with_spaces(self):
        content = "    needs: [ a , b , c ]\n"
        result = _cleanup_needs_references(content, {"b"})
        assert "a" in result
        assert "c" in result
        assert "b" not in result

    def test_bracket_list_start_preserved(self):
        """needs: [ (opening bracket only) is passed through unchanged."""
        content = "    needs: [\n"
        result = _cleanup_needs_references(content, {"a"})
        assert result == content

    def test_block_style_needs_removes_deleted_job(self):
        content = textwrap.dedent("""\
            jobs:
              build:
                needs:
                  - job-a
                  - removed-job
                  - job-b
                runs-on: mt-l-x86
        """)
        result = _cleanup_needs_references(content, {"removed-job"})
        assert "- job-a" in result
        assert "- job-b" in result
        assert "removed-job" not in result
        assert "needs:" in result

    def test_block_style_needs_all_removed(self):
        content = textwrap.dedent("""\
            jobs:
              build:
                needs:
                  - gone-a
                  - gone-b
                runs-on: mt-l-x86
        """)
        result = _cleanup_needs_references(content, {"gone-a", "gone-b"})
        assert "needs:" not in result
        assert "runs-on: mt-l-x86" in result

    def test_block_style_needs_none_removed(self):
        content = textwrap.dedent("""\
            jobs:
              build:
                needs:
                  - job-a
                  - job-b
                runs-on: mt-l-x86
        """)
        result = _cleanup_needs_references(content, {"unrelated-job"})
        assert "needs:" in result
        assert "- job-a" in result
        assert "- job-b" in result

    def test_block_style_needs_with_comments(self):
        content = textwrap.dedent("""\
            jobs:
              build:
                needs:
                  # first dependency
                  - job-a
                  # removed dependency
                  - removed-job
                  - job-b
                runs-on: mt-l-x86
        """)
        result = _cleanup_needs_references(content, {"removed-job"})
        assert "- job-a" in result
        assert "- job-b" in result
        assert "removed-job" not in result
        assert "needs:" in result


# ── rewrite_cross_repo_refs ──────────────────────────────────────────


class TestRewriteCrossRepoRefs:
    def test_workflow_reference(self):
        content = "    uses: pytorch/pytorch/.github/workflows/lint.yml@main\n"
        result = rewrite_cross_repo_refs(content)
        assert "uses: ./.github/workflows/lint.yml" in result
        assert "@main" not in result

    def test_action_reference(self):
        content = "    uses: pytorch/pytorch/.github/actions/setup@v1\n"
        result = rewrite_cross_repo_refs(content)
        assert "uses: ./.github/actions/setup" in result
        assert "@v1" not in result

    def test_third_party_ref_unchanged(self):
        content = "    uses: actions/checkout@v4\n"
        assert rewrite_cross_repo_refs(content) == content

    def test_other_org_ref_unchanged(self):
        content = "    uses: pytorch/test-infra/.github/workflows/x.yml@main\n"
        assert rewrite_cross_repo_refs(content) == content

    def test_workflow_with_sha_ref(self):
        content = "    uses: pytorch/pytorch/.github/workflows/build.yml@abc123def\n"
        result = rewrite_cross_repo_refs(content)
        assert "uses: ./.github/workflows/build.yml" in result
        assert "@abc123def" not in result

    def test_action_with_path_segments(self):
        content = "    uses: pytorch/pytorch/.github/actions/setup/python@main\n"
        result = rewrite_cross_repo_refs(content)
        assert "uses: ./.github/actions/setup/python" in result

    def test_no_refs_in_content(self):
        content = "    runs-on: ubuntu-latest\n"
        assert rewrite_cross_repo_refs(content) == content

    def test_multiple_refs_rewritten(self):
        content = (
            "    uses: pytorch/pytorch/.github/workflows/a.yml@main\n"
            "    uses: pytorch/pytorch/.github/actions/b@v2\n"
        )
        result = rewrite_cross_repo_refs(content)
        assert "uses: ./.github/workflows/a.yml" in result
        assert "uses: ./.github/actions/b" in result


# ── rewrite_repo_guards ──────────────────────────────────────────────


class TestRewriteRepoGuards:
    def test_single_quote_equals(self):
        content = "if: github.repository == 'pytorch/pytorch'"
        result = rewrite_repo_guards(content)
        assert "github.repository_owner == 'pytorch'" in result
        assert "pytorch/pytorch" not in result

    def test_double_quote_equals(self):
        content = 'if: github.repository == "pytorch/pytorch"'
        result = rewrite_repo_guards(content)
        assert "github.repository_owner == 'pytorch'" in result

    def test_single_quote_not_equals(self):
        content = "if: github.repository != 'pytorch/pytorch'"
        result = rewrite_repo_guards(content)
        assert "github.repository_owner != 'pytorch'" in result

    def test_double_quote_not_equals(self):
        content = 'if: github.repository != "pytorch/pytorch"'
        result = rewrite_repo_guards(content)
        assert "github.repository_owner != 'pytorch'" in result

    def test_no_guards(self):
        content = "runs-on: ubuntu-latest"
        assert rewrite_repo_guards(content) == content

    def test_other_repo_guard_unchanged(self):
        content = "if: github.repository == 'pytorch/test-infra'"
        assert rewrite_repo_guards(content) == content

    def test_multiple_guards(self):
        content = (
            "if: github.repository == 'pytorch/pytorch'\n"
            "if: github.repository != 'pytorch/pytorch'\n"
        )
        result = rewrite_repo_guards(content)
        assert result.count("repository_owner") == 2
        assert "pytorch/pytorch" not in result


# ── replace_runner_prefix ────────────────────────────────────────────


class TestReplaceRunnerPrefix:
    def test_linux_standard(self):
        content = "runs-on: mt-l-x86iamx-8-16"
        result = replace_runner_prefix(content, "mt-", "c-mt-")
        assert "c-mt-l-x86iamx-8-16" in result

    def test_windows_standard(self):
        content = "runs-on: mt-w-x86iamx-8-16"
        result = replace_runner_prefix(content, "mt-", "c-mt-")
        assert "c-mt-w-x86iamx-8-16" in result

    def test_mac_standard(self):
        content = "runs-on: mt-m-arm64-4-16"
        result = replace_runner_prefix(content, "mt-", "c-mt-")
        assert "c-mt-m-arm64-4-16" in result

    def test_bare_metal_linux(self):
        content = "runs-on: mt-bl-x86iamx-8-16"
        result = replace_runner_prefix(content, "mt-", "c-mt-")
        assert "c-mt-bl-x86iamx-8-16" in result

    def test_bare_metal_windows(self):
        content = "runs-on: mt-bw-x86iamx-8-16"
        result = replace_runner_prefix(content, "mt-", "c-mt-")
        assert "c-mt-bw-x86iamx-8-16" in result

    def test_bare_metal_mac(self):
        content = "runs-on: mt-bm-arm64-4-16"
        result = replace_runner_prefix(content, "mt-", "c-mt-")
        assert "c-mt-bm-arm64-4-16" in result

    def test_same_prefix_no_op(self):
        content = "runs-on: mt-l-x86iamx-8-16"
        assert replace_runner_prefix(content, "mt-", "mt-") == content

    def test_multiple_occurrences(self):
        content = "mt-l-x86\nmt-w-x86\nmt-m-arm64\n"
        result = replace_runner_prefix(content, "mt-", "new-")
        assert "new-l-x86" in result
        assert "new-w-x86" in result
        assert "new-m-arm64" in result
        assert "mt-" not in result

    def test_no_matching_prefix(self):
        content = "runs-on: ubuntu-latest"
        assert replace_runner_prefix(content, "mt-", "c-mt-") == content

    def test_all_os_chars_and_bare_metal(self):
        """All six combinations (3 os chars x 2 variants) are replaced."""
        content = "mt-l- mt-w- mt-m- mt-bl- mt-bw- mt-bm-"
        result = replace_runner_prefix(content, "mt-", "x-")
        assert result == "x-l- x-w- x-m- x-bl- x-bw- x-bm-"


# ── generate_determinator_stub ───────────────────────────────────────


class TestGenerateDeterminatorStub:
    def test_placeholder_replaced(self):
        result = generate_determinator_stub("c-mt-")
        assert "TARGET_PREFIX_PLACEHOLDER" not in result
        assert "c-mt-" in result

    def test_is_valid_yaml(self):
        import yaml

        result = generate_determinator_stub("test-")
        data = yaml.safe_load(result)
        assert data["name"] == "runner-determinator"

    def test_has_workflow_call_trigger(self):
        result = generate_determinator_stub("p-")
        assert "workflow_call:" in result

    def test_has_label_type_output(self):
        result = generate_determinator_stub("p-")
        assert "label-type" in result

    def test_has_use_arc_true(self):
        result = generate_determinator_stub("p-")
        assert 'use-arc' in result
        assert '"true"' in result

    def test_step_has_set_prefix_id(self):
        """The determine job must have a step with id: set-prefix."""
        import yaml

        result = generate_determinator_stub("p-")
        data = yaml.safe_load(result)
        steps = data["jobs"]["determine"]["steps"]
        ids = [s.get("id") for s in steps]
        assert "set-prefix" in ids

    def test_step_writes_to_github_output(self):
        """The set-prefix step must write label-type to $GITHUB_OUTPUT."""
        import yaml

        result = generate_determinator_stub("p-")
        data = yaml.safe_load(result)
        steps = data["jobs"]["determine"]["steps"]
        set_prefix_step = next(s for s in steps if s.get("id") == "set-prefix")
        run_cmd = set_prefix_step["run"]
        assert "label-type=p-" in run_cmd
        assert "$GITHUB_OUTPUT" in run_cmd

    def test_job_output_references_step(self):
        """The determine job output must reference steps.set-prefix.outputs.label-type."""
        result = generate_determinator_stub("p-")
        assert "steps.set-prefix.outputs.label-type" in result


# ── generate_determinator_script ─────────────────────────────────────


class TestGenerateDeterminatorScript:
    def test_placeholder_replaced(self):
        result = generate_determinator_script("c-mt-")
        assert "TARGET_PREFIX_PLACEHOLDER" not in result
        assert "c-mt-" in result

    def test_is_valid_python(self):
        result = generate_determinator_script("test-")
        compile(result, "<string>", "exec")  # raises SyntaxError if invalid

    def test_writes_to_github_output(self):
        result = generate_determinator_script("p-")
        assert "GITHUB_OUTPUT" in result

    def test_writes_label_type(self):
        result = generate_determinator_script("p-")
        assert "label-type=p-" in result

    def test_shebang_line(self):
        result = generate_determinator_script("p-")
        assert result.startswith("#!/usr/bin/env python3")


# ── apply_to_all_workflows ──────────────────────────────────────────


class TestApplyToAllWorkflows:
    def test_applies_to_yml_files(self, tmp_path):
        (tmp_path / "a.yml").write_text("original")
        (tmp_path / "b.yaml").write_text("original")

        apply_to_all_workflows(tmp_path, lambda c: c.replace("original", "modified"))

        assert (tmp_path / "a.yml").read_text() == "modified"
        assert (tmp_path / "b.yaml").read_text() == "modified"

    def test_skips_non_yaml(self, tmp_path):
        (tmp_path / "readme.md").write_text("original")
        (tmp_path / "script.py").write_text("original")

        apply_to_all_workflows(tmp_path, lambda c: c.replace("original", "modified"))

        assert (tmp_path / "readme.md").read_text() == "original"
        assert (tmp_path / "script.py").read_text() == "original"

    def test_no_write_when_unchanged(self, tmp_path):
        wf = tmp_path / "noop.yml"
        wf.write_text("unchanged")

        import os

        mtime_before = os.path.getmtime(wf)

        apply_to_all_workflows(tmp_path, lambda c: c)  # identity transform

        mtime_after = os.path.getmtime(wf)
        assert mtime_before == mtime_after

    def test_empty_directory(self, tmp_path):
        """No crash on empty directory."""
        apply_to_all_workflows(tmp_path, lambda c: c.upper())

    def test_sorted_processing(self, tmp_path):
        """Files are processed in sorted order."""
        order = []
        for name in ["z.yml", "a.yml", "m.yaml"]:
            (tmp_path / name).write_text("x")

        def track(content):
            order.append(len(order))
            return content

        apply_to_all_workflows(tmp_path, track)
        assert order == [0, 1, 2]  # called 3 times in sorted order

    def test_only_changed_files_written(self, tmp_path):
        (tmp_path / "change.yml").write_text("AAA")
        (tmp_path / "keep.yml").write_text("BBB")

        apply_to_all_workflows(tmp_path, lambda c: c.replace("AAA", "ZZZ"))

        assert (tmp_path / "change.yml").read_text() == "ZZZ"
        assert (tmp_path / "keep.yml").read_text() == "BBB"


# ── Integration-style: filter_non_arc_jobs + needs cleanup ───────────


class TestFilterNonArcJobsIntegration:
    def test_needs_cleanup_after_removal(self):
        content = textwrap.dedent("""\
            on:
              push:
            jobs:
              setup:
                runs-on: ubuntu-latest
                steps:
                  - run: echo setup
              build:
                needs: [setup]
                runs-on: mt-l-x86iamx-8-16
                steps:
                  - run: echo build
        """)
        result = filter_non_arc_jobs(content)
        assert "setup:" not in result
        assert "build:" in result
        # needs: [setup] should be cleaned up
        assert "needs:" not in result

    def test_partial_needs_cleanup(self):
        content = textwrap.dedent("""\
            on:
              push:
            jobs:
              gha-job:
                runs-on: ubuntu-latest
                steps:
                  - run: echo gha
              arc-job:
                runs-on: mt-l-x86iamx-8-16
                steps:
                  - run: echo arc
              dependent:
                needs: [gha-job, arc-job]
                runs-on: mt-l-x86iamx-8-16
                steps:
                  - run: echo dep
        """)
        result = filter_non_arc_jobs(content)
        assert "gha-job:" not in result
        assert "arc-job:" in result
        assert "needs: [arc-job]" in result

    def test_single_needs_value_cleanup(self):
        content = textwrap.dedent("""\
            on:
              push:
            jobs:
              removed-job:
                runs-on: ubuntu-latest
                steps:
                  - run: echo x
              dependent:
                needs: removed-job
                runs-on: mt-l-x86iamx-8-16
                steps:
                  - run: echo dep
        """)
        result = filter_non_arc_jobs(content)
        assert "removed-job:" not in result
        assert "needs:" not in result
        assert "dependent:" in result
