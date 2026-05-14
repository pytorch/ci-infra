"""Shared HCL/Terraform source-parsing helpers used by `scripts/test_*.py` unit tests.

These tests deliberately use regex+string-balance parsing rather than a true HCL parser:
- No new dependency required (stdlib only)
- Small surface area, easy to reason about
- Intentionally fragile to refactors (`local.x` substitutions, heredocs) so that
  test failures surface the refactor explicitly
"""

from __future__ import annotations

import re


def resource_block(text: str, resource_type: str, name: str) -> str:
    """Return the brace-balanced body of a top-level
    `resource "<resource_type>" "<name>" { ... }` block (including the outer braces).
    """
    pattern = re.compile(
        rf'resource\s+"{re.escape(resource_type)}"\s+"{re.escape(name)}"\s*\{{',
        re.MULTILINE,
    )
    m = pattern.search(text)
    assert m, f'resource "{resource_type}" "{name}" not found'  # noqa: S101 — test helper, fail loud
    start = m.end() - 1  # position of opening brace
    depth = 0
    for i in range(start, len(text)):
        if text[i] == "{":
            depth += 1
        elif text[i] == "}":
            depth -= 1
            if depth == 0:
                return text[start : i + 1]
    raise AssertionError(f"unterminated block for {resource_type}.{name}")


def env_block(block_text: str) -> str:
    """Extract the brace-balanced contents of `env = { ... }` (excluding the outer braces)."""
    m = re.search(r"env\s*=\s*\{", block_text)
    assert m, "no env = { ... } map found in addon block"  # noqa: S101 — test helper, fail loud
    start = m.end()  # position right after the opening {
    depth = 1
    for i in range(start, len(block_text)):
        if block_text[i] == "{":
            depth += 1
        elif block_text[i] == "}":
            depth -= 1
            if depth == 0:
                return block_text[start:i]
    raise AssertionError("unterminated env { ... } block")


def strip_double_quoted_strings(text: str) -> str:
    """Replace double-quoted string regions with empty placeholders.

    Useful before substring searches for tokens like `var.` or `local.`, so that
    a string literal like `"from var.x"` does not false-positive.
    """
    return re.sub(r'"(?:\\.|[^"\\])*"', '""', text)
