"""Tests for the OSDC hook wrapper's env var validation logic.

The wrapper.js is embedded in the runner ConfigMap (runner.yaml.tpl) and
validates environment variables before script execution. This file
reimplements the validation logic in Python so it can be tested without
Node.js.

The JS and Python implementations must stay in sync — if you change the
patterns in wrapper.js, update SKIP_VARS and BAD_PATTERNS here too.
"""

from __future__ import annotations

import re

import pytest

# ---------------------------------------------------------------------------
# Python reimplementation of wrapper.js validation logic
# ---------------------------------------------------------------------------

SKIP_VARS: set[str] = {
    "GITHUB_EVENT",
    "GITHUB_CONTEXT",
    "GITHUB_EVENT_PATH",
    "RUNNER_CONTEXT",
    "STEPS_CONTEXT",
    "NEEDS_CONTEXT",
    "INPUTS_CONTEXT",
    "MATRIX_CONTEXT",
    "STRATEGY_CONTEXT",
    "ENV_CONTEXT",
    "VARS_CONTEXT",
    "JOB_CONTEXT",
}

BAD_PATTERNS: list[tuple[re.Pattern[str], str]] = [
    (re.compile(r'"message"\s*:\s*"API rate limit exceeded'), "GitHub API rate limit error"),
    (re.compile(r'"message"\s*:\s*"Bad credentials"'), "GitHub auth error"),
    (re.compile(r'"message"\s*:\s*"Not Found"'), "GitHub 404 error"),
    (
        re.compile(r'"documentation_url"\s*:\s*"https://docs\.github\.com'),
        "GitHub API error response",
    ),
    (re.compile(r"<!DOCTYPE\s+html>", re.IGNORECASE), "HTML error page"),
    (re.compile(r"<html[\s>]", re.IGNORECASE), "HTML content"),
]

MIN_LENGTH = 30


def validate_env_vars(
    env_vars: dict[str, str] | None,
) -> list[dict[str, str]]:
    """Validate env vars for known bad patterns (mirrors wrapper.js)."""
    problems: list[dict[str, str]] = []
    if not env_vars or not isinstance(env_vars, dict):
        return problems
    for name, value in env_vars.items():
        if name in SKIP_VARS:
            continue
        if not isinstance(value, str) or len(value) < MIN_LENGTH:
            continue
        for pattern, label in BAD_PATTERNS:
            if pattern.search(value):
                problems.append({"name": name, "label": label, "snippet": value[:200]})
                break
    return problems


# ---------------------------------------------------------------------------
# Rate limit error fixtures
# ---------------------------------------------------------------------------

RATE_LIMIT_JSON = (
    '{"message": "API rate limit exceeded for installation ID 108527678. '
    "(https://docs.github.com/rest/overview/resources-in-the-rest-api"
    '#rate-limiting)", '
    '"documentation_url": "https://docs.github.com/rest/overview/resources'
    '-in-the-rest-api#rate-limiting"}'
)

BAD_CREDENTIALS_JSON = (
    '{"message": "Bad credentials", '
    '"documentation_url": "https://docs.github.com/rest/overview/resources'
    '-in-the-rest-api"}'
)

NOT_FOUND_JSON = (
    '{"message": "Not Found", "documentation_url": "https://docs.github.com/rest/overview/resources-in-the-rest-api"}'
)

HTML_ERROR_PAGE = (
    "<!DOCTYPE html><html><head><title>503 Service Temporarily Unavailable"
    "</title></head><body><h1>503</h1></body></html>"
)

HTML_SIMPLE = '<html lang="en"><body>Error</body></html>'


# ============================================================================
# Tests: bad pattern detection
# ============================================================================


class TestBadPatternDetection:
    """Each known bad pattern must be detected."""

    def test_rate_limit_json(self):
        problems = validate_env_vars({"CHANGED_FILES": RATE_LIMIT_JSON})
        assert len(problems) == 1
        assert problems[0]["name"] == "CHANGED_FILES"
        assert "rate limit" in problems[0]["label"].lower()

    def test_bad_credentials_json(self):
        problems = validate_env_vars({"TOKEN_DATA": BAD_CREDENTIALS_JSON})
        assert len(problems) == 1
        assert "auth" in problems[0]["label"].lower()

    def test_not_found_json(self):
        problems = validate_env_vars({"API_RESULT": NOT_FOUND_JSON})
        assert len(problems) == 1
        assert "404" in problems[0]["label"]

    def test_html_doctype_error_page(self):
        problems = validate_env_vars({"PAGE_CONTENT": HTML_ERROR_PAGE})
        assert len(problems) == 1
        assert "HTML" in problems[0]["label"]

    def test_html_tag_only(self):
        problems = validate_env_vars({"RESPONSE": HTML_SIMPLE})
        assert len(problems) == 1
        assert "HTML" in problems[0]["label"]

    def test_documentation_url_pattern(self):
        """The documentation_url pattern catches generic GitHub API errors."""
        val = (
            '{"message": "Resource not accessible by integration", '
            '"documentation_url": "https://docs.github.com/rest/reference/repos"}'
        )
        problems = validate_env_vars({"GH_RESULT": val})
        assert len(problems) == 1
        assert "API error" in problems[0]["label"]

    def test_multiple_bad_vars(self):
        """Multiple corrupted vars all get reported."""
        problems = validate_env_vars(
            {
                "VAR_A": RATE_LIMIT_JSON,
                "VAR_B": HTML_ERROR_PAGE,
                "VAR_C": BAD_CREDENTIALS_JSON,
            }
        )
        assert len(problems) == 3
        names = {p["name"] for p in problems}
        assert names == {"VAR_A", "VAR_B", "VAR_C"}

    def test_snippet_truncated_at_200(self):
        """Snippets are truncated to 200 chars."""
        long_val = RATE_LIMIT_JSON + "x" * 500
        problems = validate_env_vars({"LONG": long_val})
        assert len(problems) == 1
        assert len(problems[0]["snippet"]) == 200


# ============================================================================
# Tests: skip behavior
# ============================================================================


class TestSkipBehavior:
    """Known GitHub context vars and short values must be skipped."""

    @pytest.mark.parametrize("var_name", sorted(SKIP_VARS))
    def test_skip_github_context_vars(self, var_name: str):
        """GitHub context vars are skipped even if they contain bad patterns."""
        problems = validate_env_vars({var_name: RATE_LIMIT_JSON})
        assert problems == []

    def test_short_value_skipped(self):
        """Values shorter than MIN_LENGTH are skipped."""
        problems = validate_env_vars({"SHORT": '{"message": "err"}'})
        assert problems == []

    def test_exactly_min_length_checked(self):
        """Values at exactly MIN_LENGTH are checked."""
        # Pad a bad-pattern value to exactly MIN_LENGTH
        base = '"message": "Bad credentials"'
        padded = base + " " * (MIN_LENGTH - len(base))
        assert len(padded) == MIN_LENGTH
        problems = validate_env_vars({"VAR": padded})
        assert len(problems) == 1

    def test_non_string_value_skipped(self):
        """Non-string values are skipped without error."""
        problems = validate_env_vars({"NUM": 12345})  # type: ignore[dict-item]
        assert problems == []

    def test_none_env_vars(self):
        problems = validate_env_vars(None)
        assert problems == []

    def test_empty_dict(self):
        problems = validate_env_vars({})
        assert problems == []

    def test_not_a_dict(self):
        problems = validate_env_vars("not a dict")  # type: ignore[arg-type]
        assert problems == []


# ============================================================================
# Tests: false positive avoidance
# ============================================================================


class TestFalsePositiveAvoidance:
    """Normal env var values must not trigger false positives."""

    def test_normal_path(self):
        problems = validate_env_vars({"PATH": "/usr/local/bin:/usr/bin:/bin" + ":" * 30})
        assert problems == []

    def test_normal_json_config(self):
        """Legitimate JSON config with 'message' key but not an error."""
        val = '{"message": "Hello world, this is a test configuration value that is long enough"}'
        problems = validate_env_vars({"CONFIG": val})
        assert problems == []

    def test_github_url_in_normal_value(self):
        """A value mentioning github.com is not flagged without error patterns."""
        val = "https://github.com/pytorch/pytorch/actions/runs/12345 — check this out for details"
        problems = validate_env_vars({"RUN_URL": val})
        assert problems == []

    def test_html_in_short_value(self):
        """HTML-like content under MIN_LENGTH is not flagged."""
        problems = validate_env_vars({"TAG": "<html>"})
        assert problems == []

    def test_documentation_url_without_github(self):
        """documentation_url pointing elsewhere is not flagged."""
        val = '{"documentation_url": "https://example.com/docs/something-long-enough-to-pass-min"}'
        problems = validate_env_vars({"DOC": val})
        assert problems == []

    def test_file_list_value(self):
        """A normal CHANGED_FILES value (space-separated paths) is fine."""
        val = "src/main.py src/utils.py tests/test_main.py README.md setup.cfg pyproject.toml"
        problems = validate_env_vars({"CHANGED_FILES": val})
        assert problems == []

    def test_long_normal_string(self):
        """Very long normal string is not flagged."""
        val = "a" * 1000
        problems = validate_env_vars({"BIG": val})
        assert problems == []
