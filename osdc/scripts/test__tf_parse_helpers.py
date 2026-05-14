"""Unit tests for the shared HCL/Terraform source-parsing helpers."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "scripts"))

from _tf_parse_helpers import (  # noqa: E402
    env_block,
    resource_block,
    strip_double_quoted_strings,
)


class TestResourceBlock:
    """resource_block extracts the brace-balanced body of a top-level HCL resource."""

    def test_extracts_body_with_outer_braces(self) -> None:
        text = 'resource "aws_thing" "my_thing" {\n  foo = "bar"\n}\n'
        block = resource_block(text, "aws_thing", "my_thing")
        assert block.startswith("{")
        assert block.endswith("}")
        assert 'foo = "bar"' in block

    def test_handles_nested_braces(self) -> None:
        text = 'resource "aws_thing" "my_thing" {\n  cfg = jsonencode({\n    nested = { a = "b" }\n  })\n}\n'
        block = resource_block(text, "aws_thing", "my_thing")
        assert 'nested = { a = "b" }' in block
        # Outer braces preserved
        assert block.count("{") == block.count("}")

    def test_missing_resource_raises(self) -> None:
        with pytest.raises(AssertionError, match=r"aws_thing.*not_found.*not found"):
            resource_block("# no matching resource\n", "aws_thing", "not_found")

    def test_unterminated_block_raises(self) -> None:
        # Opening brace but never closes
        text = 'resource "aws_thing" "my_thing" {\n  foo = "bar"\n'
        with pytest.raises(AssertionError, match="unterminated"):
            resource_block(text, "aws_thing", "my_thing")


class TestEnvBlock:
    """env_block extracts the body of `env = { ... }` (excluding outer braces)."""

    def test_extracts_env_body(self) -> None:
        text = '{\n  env = {\n    K = "v"\n  }\n}\n'
        body = env_block(text)
        assert 'K = "v"' in body
        assert "env" not in body  # outer key stripped

    def test_missing_env_raises(self) -> None:
        with pytest.raises(AssertionError, match="no env"):
            env_block('{\n  foo = "bar"\n}\n')

    def test_unterminated_env_raises(self) -> None:
        with pytest.raises(AssertionError, match="unterminated"):
            env_block('env = {\n  K = "v"\n')


class TestStripDoubleQuotedStrings:
    """strip_double_quoted_strings replaces "..." with '' so substring checks don't false-match."""

    def test_strips_basic_string(self) -> None:
        assert strip_double_quoted_strings('foo = "var.x"') == 'foo = ""'

    def test_handles_escaped_quotes(self) -> None:
        # The string `"a\"b"` should be stripped as one unit
        assert strip_double_quoted_strings(r'k = "a\"b"') == 'k = ""'

    def test_leaves_non_string_text_untouched(self) -> None:
        assert strip_double_quoted_strings("var.x = 1") == "var.x = 1"
