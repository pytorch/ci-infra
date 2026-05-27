"""Unit tests for validate-runner-bump.py."""

import importlib.util
import json
from pathlib import Path

import pytest

_spec = importlib.util.spec_from_file_location(
    "validate_runner_bump",
    Path(__file__).resolve().parent / "validate-runner-bump.py",
)
mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(mod)


def _capture(monkeypatch, capsys, patch_json) -> tuple[int, dict[str, str]]:
    monkeypatch.setenv("PATCH_JSON", patch_json if isinstance(patch_json, str) else json.dumps(patch_json))
    rc = mod.main()
    out = capsys.readouterr().out
    parsed: dict[str, str] = {}
    for line in out.splitlines():
        if "=" in line:
            k, v = line.split("=", 1)
            parsed[k] = v
    return rc, parsed


def _pr_form(filename: str, patch: str) -> list[dict]:
    return [{"filename": filename, "patch": patch}]


def _commit_form(filename: str, patch: str) -> dict:
    return {"files": [{"filename": filename, "patch": patch}]}


def _bump_patch(old: str, new: str, indent: str = "    ", suffix: str = "") -> str:
    return (
        "@@ -1,3 +1,3 @@\n"
        " context_line_before\n"
        f'-{indent}runner_image_tag: "{old}"{suffix}\n'
        f'+{indent}runner_image_tag: "{new}"{suffix}\n'
        " context_line_after\n"
    )


# ---------------------------------------------------------------------------
# env / setup
# ---------------------------------------------------------------------------


def test_missing_patch_json_exits_nonzero(monkeypatch, capsys):
    monkeypatch.delenv("PATCH_JSON", raising=False)
    rc = mod.main()
    err = capsys.readouterr().err
    assert rc == 2
    assert "PATCH_JSON env var is required" in err


def test_malformed_json_exits_nonzero(monkeypatch, capsys):
    monkeypatch.setenv("PATCH_JSON", "{not-json")
    rc = mod.main()
    err = capsys.readouterr().err
    assert rc == 2
    assert "not valid JSON" in err


def test_unexpected_payload_shape_exits_nonzero(monkeypatch, capsys):
    monkeypatch.setenv("PATCH_JSON", json.dumps("a string"))
    rc = mod.main()
    err = capsys.readouterr().err
    assert rc == 2
    assert "must be array or object" in err


def test_empty_patch_json_env_var_treated_as_missing(monkeypatch, capsys):
    monkeypatch.setenv("PATCH_JSON", "")
    rc = mod.main()
    err = capsys.readouterr().err
    assert rc == 2
    assert "PATCH_JSON env var is required" in err


# ---------------------------------------------------------------------------
# file-count branch
# ---------------------------------------------------------------------------


def test_zero_files_pr_form(monkeypatch, capsys):
    _, out = _capture(monkeypatch, capsys, [])
    assert out["decision"] == "close-wrong-file-count"
    assert "got 0" in out["reason"]


def test_zero_files_commit_form(monkeypatch, capsys):
    _, out = _capture(monkeypatch, capsys, {"files": []})
    assert out["decision"] == "close-wrong-file-count"


def test_commit_form_missing_files_key(monkeypatch, capsys):
    _, out = _capture(monkeypatch, capsys, {})
    assert out["decision"] == "close-wrong-file-count"


def test_two_files_rejected(monkeypatch, capsys):
    payload = [
        {"filename": "osdc/clusters.yaml", "patch": "+x"},
        {"filename": "README.md", "patch": "+y"},
    ]
    _, out = _capture(monkeypatch, capsys, payload)
    assert out["decision"] == "close-wrong-file-count"
    assert "got 2" in out["reason"]


# ---------------------------------------------------------------------------
# wrong-file branch
# ---------------------------------------------------------------------------


def test_wrong_file(monkeypatch, capsys):
    _, out = _capture(monkeypatch, capsys, _pr_form("README.md", "irrelevant"))
    assert out["decision"] == "close-wrong-file"
    assert "README.md" in out["reason"]


def test_workflow_file_substitution_blocked(monkeypatch, capsys):
    """The exact attack the --ref main hardening defends against."""
    _, out = _capture(monkeypatch, capsys, _pr_form(".github/workflows/osdc-pr-validate.yml", "x"))
    assert out["decision"] == "close-wrong-file"


# ---------------------------------------------------------------------------
# no-patch branch
# ---------------------------------------------------------------------------


def test_null_patch(monkeypatch, capsys):
    _, out = _capture(monkeypatch, capsys, [{"filename": "osdc/clusters.yaml", "patch": None}])
    assert out["decision"] == "close-no-patch"


def test_empty_patch(monkeypatch, capsys):
    _, out = _capture(monkeypatch, capsys, _pr_form("osdc/clusters.yaml", ""))
    assert out["decision"] == "close-no-patch"


def test_missing_patch_key(monkeypatch, capsys):
    _, out = _capture(monkeypatch, capsys, [{"filename": "osdc/clusters.yaml"}])
    assert out["decision"] == "close-no-patch"


# ---------------------------------------------------------------------------
# multi-line branch
# ---------------------------------------------------------------------------


def test_only_addition_no_removal(monkeypatch, capsys):
    patch = '@@ -1 +1,2 @@\n context\n+    runner_image_tag: "1.2.3"\n'
    _, out = _capture(monkeypatch, capsys, _pr_form("osdc/clusters.yaml", patch))
    assert out["decision"] == "close-multi-line"
    assert "+1/-0" in out["reason"]


def test_only_removal_no_addition(monkeypatch, capsys):
    patch = '@@ -1,2 +1 @@\n context\n-    runner_image_tag: "1.2.3"\n'
    _, out = _capture(monkeypatch, capsys, _pr_form("osdc/clusters.yaml", patch))
    assert out["decision"] == "close-multi-line"
    assert "+0/-1" in out["reason"]


def test_two_additions(monkeypatch, capsys):
    patch = '@@ -1 +1,2 @@\n context\n+    runner_image_tag: "1.2.3"\n+    runner_image_tag: "1.2.4"\n'
    _, out = _capture(monkeypatch, capsys, _pr_form("osdc/clusters.yaml", patch))
    assert out["decision"] == "close-multi-line"


def test_hunk_headers_are_ignored(monkeypatch, capsys):
    """Lines starting with `++`/`--` (hunk markers) must not be counted."""
    patch = _bump_patch("1.2.3", "1.2.4")
    _, out = _capture(monkeypatch, capsys, _pr_form("osdc/clusters.yaml", patch))
    assert out["decision"] == "approve"


# ---------------------------------------------------------------------------
# bad-pattern branch
# ---------------------------------------------------------------------------


def test_non_semver_version(monkeypatch, capsys):
    patch = _bump_patch("1.2", "1.3")
    _, out = _capture(monkeypatch, capsys, _pr_form("osdc/clusters.yaml", patch))
    assert out["decision"] == "close-bad-pattern"


def test_wrong_indentation(monkeypatch, capsys):
    patch = _bump_patch("1.2.3", "1.2.4", indent="  ")  # 2 spaces instead of 4
    _, out = _capture(monkeypatch, capsys, _pr_form("osdc/clusters.yaml", patch))
    assert out["decision"] == "close-bad-pattern"


def test_wrong_key(monkeypatch, capsys):
    patch = '@@ -1 +1 @@\n-    something_else: "1.2.3"\n+    something_else: "1.2.4"\n'
    _, out = _capture(monkeypatch, capsys, _pr_form("osdc/clusters.yaml", patch))
    assert out["decision"] == "close-bad-pattern"


def test_unquoted_version(monkeypatch, capsys):
    patch = "@@ -1 +1 @@\n-    runner_image_tag: 1.2.3\n+    runner_image_tag: 1.2.4\n"
    _, out = _capture(monkeypatch, capsys, _pr_form("osdc/clusters.yaml", patch))
    assert out["decision"] == "close-bad-pattern"


def test_old_line_bad_pattern(monkeypatch, capsys):
    """new line is fine but old line is malformed."""
    patch = '@@ -1 +1 @@\n-    runner_image_tag: not-semver\n+    runner_image_tag: "1.2.4"\n'
    _, out = _capture(monkeypatch, capsys, _pr_form("osdc/clusters.yaml", patch))
    assert out["decision"] == "close-bad-pattern"
    assert "old line" in out["reason"]


# ---------------------------------------------------------------------------
# no-change branch
# ---------------------------------------------------------------------------


def test_no_version_change(monkeypatch, capsys):
    # Old and new lines have identical version strings — comment difference is ignored.
    patch = '@@ -1 +1 @@\n-    runner_image_tag: "1.2.3"\n+    runner_image_tag: "1.2.3" # bumped comment\n'
    _, out = _capture(monkeypatch, capsys, _pr_form("osdc/clusters.yaml", patch))
    assert out["decision"] == "close-no-change"


# ---------------------------------------------------------------------------
# downgrade branch
# ---------------------------------------------------------------------------


def test_downgrade_minor(monkeypatch, capsys):
    patch = _bump_patch("1.3.0", "1.2.99")
    _, out = _capture(monkeypatch, capsys, _pr_form("osdc/clusters.yaml", patch))
    assert out["decision"] == "close-downgrade"
    assert "1.3.0 -> 1.2.99" in out["reason"]


def test_downgrade_patch(monkeypatch, capsys):
    patch = _bump_patch("1.2.4", "1.2.3")
    _, out = _capture(monkeypatch, capsys, _pr_form("osdc/clusters.yaml", patch))
    assert out["decision"] == "close-downgrade"


def test_downgrade_major(monkeypatch, capsys):
    patch = _bump_patch("2.0.0", "1.99.99")
    _, out = _capture(monkeypatch, capsys, _pr_form("osdc/clusters.yaml", patch))
    assert out["decision"] == "close-downgrade"


def test_lexicographic_vs_numeric_ordering(monkeypatch, capsys):
    """1.10.0 > 1.9.0 numerically (was a sort -V bug magnet in bash)."""
    patch = _bump_patch("1.9.0", "1.10.0")
    _, out = _capture(monkeypatch, capsys, _pr_form("osdc/clusters.yaml", patch))
    assert out["decision"] == "approve"
    assert out["old_ver"] == "1.9.0"
    assert out["new_ver"] == "1.10.0"


# ---------------------------------------------------------------------------
# approve branch
# ---------------------------------------------------------------------------


def test_approve_minor_bump_pr_form(monkeypatch, capsys):
    patch = _bump_patch("1.2.3", "1.3.0")
    _, out = _capture(monkeypatch, capsys, _pr_form("osdc/clusters.yaml", patch))
    assert out["decision"] == "approve"
    assert out["old_ver"] == "1.2.3"
    assert out["new_ver"] == "1.3.0"
    assert out["reason"] == "1.2.3 -> 1.3.0"


def test_approve_patch_bump_commit_form(monkeypatch, capsys):
    patch = _bump_patch("2.0.0", "2.0.1")
    _, out = _capture(monkeypatch, capsys, _commit_form("osdc/clusters.yaml", patch))
    assert out["decision"] == "approve"
    assert out["old_ver"] == "2.0.0"
    assert out["new_ver"] == "2.0.1"


def test_approve_with_inline_comment(monkeypatch, capsys):
    patch = _bump_patch("1.2.3", "1.2.4", suffix=" # auto-update")
    _, out = _capture(monkeypatch, capsys, _pr_form("osdc/clusters.yaml", patch))
    assert out["decision"] == "approve"


def test_approve_with_trailing_whitespace(monkeypatch, capsys):
    patch = _bump_patch("1.2.3", "1.2.4", suffix="   ")
    _, out = _capture(monkeypatch, capsys, _pr_form("osdc/clusters.yaml", patch))
    assert out["decision"] == "approve"


def test_approve_with_tab_after_colon(monkeypatch, capsys):
    patch = '@@ -1 +1 @@\n-    runner_image_tag:\t"1.2.3"\n+    runner_image_tag:\t"1.2.4"\n'
    _, out = _capture(monkeypatch, capsys, _pr_form("osdc/clusters.yaml", patch))
    assert out["decision"] == "approve"


def test_approve_outputs_all_four_keys(monkeypatch, capsys):
    """The approve branch is the only one that emits old_ver/new_ver."""
    patch = _bump_patch("1.2.3", "1.2.4")
    _, out = _capture(monkeypatch, capsys, _pr_form("osdc/clusters.yaml", patch))
    assert set(out) == {"decision", "reason", "old_ver", "new_ver"}


def test_close_decisions_do_not_emit_versions(monkeypatch, capsys):
    _, out = _capture(monkeypatch, capsys, _pr_form("README.md", "x"))
    assert "old_ver" not in out
    assert "new_ver" not in out


# ---------------------------------------------------------------------------
# helper: _semver_tuple
# ---------------------------------------------------------------------------


def test_semver_tuple():
    assert mod._semver_tuple("1.2.3") == (1, 2, 3)
    assert mod._semver_tuple("0.0.0") == (0, 0, 0)
    assert mod._semver_tuple("10.20.30") == (10, 20, 30)


@pytest.mark.parametrize(
    ("a", "b"),
    [
        ("1.9.0", "1.10.0"),
        ("1.2.9", "1.2.10"),
        ("1.0.0", "2.0.0"),
    ],
)
def test_semver_ordering_numeric(a, b):
    assert mod._semver_tuple(a) < mod._semver_tuple(b)
