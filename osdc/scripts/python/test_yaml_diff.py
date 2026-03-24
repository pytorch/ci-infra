"""Unit tests for scripts/yaml-diff.py."""

import subprocess
import sys
from pathlib import Path

SCRIPT = str(Path(__file__).resolve().parent.parent / "yaml-diff.py")


def run_yaml_diff(file1: str, file2: str) -> subprocess.CompletedProcess:
    """Invoke yaml-diff.py via uv run and return the result."""
    return subprocess.run(
        [sys.executable, SCRIPT, file1, file2],
        capture_output=True,
        text=True,
    )


# ---------------------------------------------------------------------------
# Identical files
# ---------------------------------------------------------------------------


class TestIdenticalFiles:
    def test_same_content(self, tmp_path):
        content = "key: value\nlist:\n  - a\n  - b\n"
        f1 = tmp_path / "a.yaml"
        f2 = tmp_path / "b.yaml"
        f1.write_text(content)
        f2.write_text(content)
        assert run_yaml_diff(str(f1), str(f2)).returncode == 0

    def test_comments_and_blank_lines_ignored(self, tmp_path):
        f1 = tmp_path / "a.yaml"
        f2 = tmp_path / "b.yaml"
        f1.write_text("# comment\nkey: value\n\n")
        f2.write_text("key: value\n# different comment\n")
        assert run_yaml_diff(str(f1), str(f2)).returncode == 0


# ---------------------------------------------------------------------------
# Different files
# ---------------------------------------------------------------------------


class TestDifferentFiles:
    def test_different_values(self, tmp_path):
        f1 = tmp_path / "a.yaml"
        f2 = tmp_path / "b.yaml"
        f1.write_text("key: value1\n")
        f2.write_text("key: value2\n")
        assert run_yaml_diff(str(f1), str(f2)).returncode == 1

    def test_empty_vs_nonempty(self, tmp_path):
        f1 = tmp_path / "a.yaml"
        f2 = tmp_path / "b.yaml"
        f1.write_text("")
        f2.write_text("key: value\n")
        assert run_yaml_diff(str(f1), str(f2)).returncode == 1


# ---------------------------------------------------------------------------
# Multi-document YAML
# ---------------------------------------------------------------------------


class TestMultiDocument:
    def test_documents_reordered(self, tmp_path):
        f1 = tmp_path / "a.yaml"
        f2 = tmp_path / "b.yaml"
        f1.write_text("name: alpha\n---\nname: bravo\n")
        f2.write_text("name: bravo\n---\nname: alpha\n")
        assert run_yaml_diff(str(f1), str(f2)).returncode == 0

    def test_single_doc_vs_multi_doc_same_content(self, tmp_path):
        """Single-doc and multi-doc with the same data differ (document boundary matters)."""
        f1 = tmp_path / "a.yaml"
        f2 = tmp_path / "b.yaml"
        f1.write_text("name: alpha\n")
        f2.write_text("name: alpha\n---\nname: alpha\n")
        assert run_yaml_diff(str(f1), str(f2)).returncode == 1

    def test_extra_empty_documents_ignored(self, tmp_path):
        """Trailing --- separators produce None docs which are filtered out."""
        f1 = tmp_path / "a.yaml"
        f2 = tmp_path / "b.yaml"
        f1.write_text("name: alpha\n")
        f2.write_text("---\nname: alpha\n---\n")
        assert run_yaml_diff(str(f1), str(f2)).returncode == 0


# ---------------------------------------------------------------------------
# Key ordering independence
# ---------------------------------------------------------------------------


class TestKeyOrdering:
    def test_different_key_order_is_equal(self, tmp_path):
        f1 = tmp_path / "a.yaml"
        f2 = tmp_path / "b.yaml"
        f1.write_text("a: 1\nb: 2\nc: 3\n")
        f2.write_text("c: 3\na: 1\nb: 2\n")
        assert run_yaml_diff(str(f1), str(f2)).returncode == 0


# ---------------------------------------------------------------------------
# Error handling
# ---------------------------------------------------------------------------


class TestErrors:
    def test_missing_file(self, tmp_path):
        f1 = tmp_path / "exists.yaml"
        f1.write_text("key: value\n")
        result = run_yaml_diff(str(f1), str(tmp_path / "missing.yaml"))
        assert result.returncode == 2
        assert "File not found" in result.stderr

    def test_malformed_yaml(self, tmp_path):
        f1 = tmp_path / "a.yaml"
        f2 = tmp_path / "b.yaml"
        f1.write_text("key: value\n")
        f2.write_text(":\n  :\n    - ][bad\n")
        result = run_yaml_diff(str(f1), str(f2))
        assert result.returncode == 2
        assert "Invalid YAML" in result.stderr

    def test_wrong_arg_count(self):
        result = subprocess.run(
            [sys.executable, SCRIPT],
            capture_output=True,
            text=True,
        )
        assert result.returncode == 2
        assert "Usage" in result.stderr
