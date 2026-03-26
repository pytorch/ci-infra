"""Unit tests for scripts/yaml-diff.py."""

import importlib.util
import sys
from pathlib import Path
from unittest.mock import patch

# Import yaml-diff.py via importlib (filename has a hyphen, can't use normal import)
_spec = importlib.util.spec_from_file_location(
    "yaml_diff", str(Path(__file__).resolve().parent.parent / "yaml-diff.py")
)
_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_mod)
normalize_documents = _mod.normalize_documents
yaml_diff_main = _mod.main


def _call_main(file1: str, file2: str) -> int:
    """Invoke yaml_diff.main() with simulated argv."""
    with patch.object(sys, "argv", ["yaml-diff.py", file1, file2]):
        return yaml_diff_main()


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
        assert _call_main(str(f1), str(f2)) == 0

    def test_comments_and_blank_lines_ignored(self, tmp_path):
        f1 = tmp_path / "a.yaml"
        f2 = tmp_path / "b.yaml"
        f1.write_text("# comment\nkey: value\n\n")
        f2.write_text("key: value\n# different comment\n")
        assert _call_main(str(f1), str(f2)) == 0


# ---------------------------------------------------------------------------
# Different files
# ---------------------------------------------------------------------------


class TestDifferentFiles:
    def test_different_values(self, tmp_path):
        f1 = tmp_path / "a.yaml"
        f2 = tmp_path / "b.yaml"
        f1.write_text("key: value1\n")
        f2.write_text("key: value2\n")
        assert _call_main(str(f1), str(f2)) == 1

    def test_empty_vs_nonempty(self, tmp_path):
        f1 = tmp_path / "a.yaml"
        f2 = tmp_path / "b.yaml"
        f1.write_text("")
        f2.write_text("key: value\n")
        assert _call_main(str(f1), str(f2)) == 1


# ---------------------------------------------------------------------------
# Multi-document YAML
# ---------------------------------------------------------------------------


class TestMultiDocument:
    def test_documents_reordered(self, tmp_path):
        f1 = tmp_path / "a.yaml"
        f2 = tmp_path / "b.yaml"
        f1.write_text("name: alpha\n---\nname: bravo\n")
        f2.write_text("name: bravo\n---\nname: alpha\n")
        assert _call_main(str(f1), str(f2)) == 0

    def test_single_doc_vs_multi_doc_same_content(self, tmp_path):
        """Single-doc and multi-doc with the same data differ (document boundary matters)."""
        f1 = tmp_path / "a.yaml"
        f2 = tmp_path / "b.yaml"
        f1.write_text("name: alpha\n")
        f2.write_text("name: alpha\n---\nname: alpha\n")
        assert _call_main(str(f1), str(f2)) == 1

    def test_extra_empty_documents_ignored(self, tmp_path):
        """Trailing --- separators produce None docs which are filtered out."""
        f1 = tmp_path / "a.yaml"
        f2 = tmp_path / "b.yaml"
        f1.write_text("name: alpha\n")
        f2.write_text("---\nname: alpha\n---\n")
        assert _call_main(str(f1), str(f2)) == 0


# ---------------------------------------------------------------------------
# Key ordering independence
# ---------------------------------------------------------------------------


class TestKeyOrdering:
    def test_different_key_order_is_equal(self, tmp_path):
        f1 = tmp_path / "a.yaml"
        f2 = tmp_path / "b.yaml"
        f1.write_text("a: 1\nb: 2\nc: 3\n")
        f2.write_text("c: 3\na: 1\nb: 2\n")
        assert _call_main(str(f1), str(f2)) == 0


# ---------------------------------------------------------------------------
# Error handling
# ---------------------------------------------------------------------------


class TestErrors:
    def test_missing_file(self, tmp_path):
        f1 = tmp_path / "exists.yaml"
        f1.write_text("key: value\n")
        assert _call_main(str(f1), str(tmp_path / "missing.yaml")) == 2

    def test_malformed_yaml(self, tmp_path):
        f1 = tmp_path / "a.yaml"
        f2 = tmp_path / "b.yaml"
        f1.write_text("key: value\n")
        f2.write_text(":\n  :\n    - ][bad\n")
        assert _call_main(str(f1), str(f2)) == 2

    def test_wrong_arg_count(self):
        with patch.object(sys, "argv", ["yaml-diff.py"]):
            assert yaml_diff_main() == 2


# ---------------------------------------------------------------------------
# normalize_documents direct tests
# ---------------------------------------------------------------------------


class TestNormalizeDocuments:
    def test_returns_sorted_normalized_strings(self, tmp_path):
        f = tmp_path / "test.yaml"
        f.write_text("b: 2\na: 1\n---\nz: 26\n")
        result = normalize_documents(str(f))
        assert len(result) == 2
        # Each document is normalized with sort_keys=True
        assert "a: 1" in result[0]
        assert "b: 2" in result[0]

    def test_filters_none_documents(self, tmp_path):
        f = tmp_path / "test.yaml"
        f.write_text("---\nkey: value\n---\n")
        result = normalize_documents(str(f))
        assert len(result) == 1
