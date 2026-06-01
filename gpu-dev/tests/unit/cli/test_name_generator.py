"""Unit tests for the name utilities used by the gpu-dev CLI.

Source under test: cli-tools/gpu-dev-cli/gpu_dev_cli/name_generator.py

Three pure functions, no I/O or AWS:
  - is_valid_name(name)      -> DNS-hostname validity predicate.
  - sanitize_name(name)      -> best-effort DNS-safe rewrite ("" on total failure).
  - generate_unique_name(existing, preferred) -> sanitize + de-dup with -N suffixes.

These tests pin the *real* branch behavior (length bounds, hyphen rules,
consecutive-hyphen collapse, charset, 63-char truncation, numbered-variation
de-duplication, and the ValueError paths).
"""
import pytest

from gpu_dev_cli.name_generator import (
    generate_unique_name,
    is_valid_name,
    sanitize_name,
)


# --------------------------------------------------------------------------- #
# is_valid_name
# --------------------------------------------------------------------------- #
class TestIsValidName:
    @pytest.mark.parametrize("name", [
        "a",                       # single char
        "abc",
        "abc123",
        "a-b-c",
        "x" * 63,                  # exactly the max length
        "0",                       # single digit
        "node-1",
        "a1b2c3",
    ])
    def test_valid_names(self, name):
        assert is_valid_name(name) is True

    @pytest.mark.parametrize("name", [
        "",                        # empty -> falsy guard
    ])
    def test_empty_is_invalid(self, name):
        assert is_valid_name(name) is False

    def test_too_long_is_invalid(self):
        # 64 chars exceeds the DNS label cap of 63.
        assert is_valid_name("a" * 64) is False

    def test_exactly_63_is_valid_boundary(self):
        assert is_valid_name("a" * 63) is True
        assert is_valid_name("a" * 64) is False

    @pytest.mark.parametrize("name", [
        "-abc",                    # leading hyphen
        "abc-",                    # trailing hyphen
        "-",                       # single hyphen is both leading & trailing
    ])
    def test_leading_or_trailing_hyphen_is_invalid(self, name):
        assert is_valid_name(name) is False

    @pytest.mark.parametrize("name", [
        "a--b",                    # consecutive hyphens
        "ab---cd",
    ])
    def test_consecutive_hyphens_invalid(self, name):
        assert is_valid_name(name) is False

    @pytest.mark.parametrize("name", [
        "ABC",                     # uppercase
        "Abc",
        "abc_def",                 # underscore
        "abc.def",                 # dot
        "abc def",                 # space
        "abc!",                    # punctuation
        "abc/def",
    ])
    def test_disallowed_charset_is_invalid(self, name):
        assert is_valid_name(name) is False

    def test_digit_only_and_hyphen_mix_valid(self):
        assert is_valid_name("1-2-3") is True

    def test_uppercase_single_char_invalid(self):
        # 'A'.islower() is False, 'A'.isdigit() is False -> rejected.
        assert is_valid_name("A") is False

    @pytest.mark.xfail(
        reason="BUG: is_valid_name uses str.islower()/str.isdigit() which accept "
               "non-ASCII unicode letters/digits (e.g. 'é', arabic-indic digits). "
               "These are NOT valid DNS label characters; the docstring promises "
               "'only lowercase letters, numbers, and hyphens' (ASCII).",
        strict=True,
    )
    def test_unicode_letter_should_be_invalid(self):
        # 'é'.islower() is True so the loop accepts it, wrongly returning True.
        assert is_valid_name("café") is False


# --------------------------------------------------------------------------- #
# sanitize_name
# --------------------------------------------------------------------------- #
class TestSanitizeName:
    def test_empty_returns_empty(self):
        assert sanitize_name("") == ""

    def test_none_returns_empty(self):
        # `if not name` catches None as well as "".
        assert sanitize_name(None) == ""

    def test_alnum_only_passthrough(self):
        # No separators / hyphens, already lowercase -> unchanged.
        assert sanitize_name("abc123") == "abc123"

    @pytest.mark.xfail(
        reason="BUG: sanitize_name DROPS literal '-' characters. The keep-branch "
               "only matches alnum, and the replace-branch only matches "
               "[' ', '_', '.'] -> a hyphen falls through and is skipped. So an "
               "already-valid DNS name like 'abc-123' is mangled to 'abc123'. A "
               "round-trip (sanitize of an already-clean name) is not idempotent.",
        strict=True,
    )
    def test_literal_hyphen_should_be_preserved(self):
        assert sanitize_name("abc-123") == "abc-123"

    def test_literal_hyphen_actually_dropped(self):
        # Documents the real (buggy) behavior so it's pinned until fixed.
        assert sanitize_name("abc-123") == "abc123"

    def test_lowercases(self):
        assert sanitize_name("ABCdef") == "abcdef"
        assert sanitize_name("MyServer") == "myserver"

    @pytest.mark.parametrize("raw,expected", [
        ("my server", "my-server"),     # space -> hyphen
        ("my_server", "my-server"),     # underscore -> hyphen
        ("my.server", "my-server"),     # dot -> hyphen
    ])
    def test_separators_become_hyphens(self, raw, expected):
        assert sanitize_name(raw) == expected

    def test_disallowed_chars_dropped(self):
        # '!' and '/' are neither alnum nor in [' ', '_', '.'] -> skipped.
        assert sanitize_name("abc!def/ghi") == "abcdefghi"

    def test_consecutive_separators_collapse(self):
        # "a   b" -> "a---b" -> "a-b"
        assert sanitize_name("a   b") == "a-b"
        assert sanitize_name("a_._ b") == "a-b"

    def test_leading_trailing_separators_stripped(self):
        assert sanitize_name("  hello  ") == "hello"
        assert sanitize_name("__edge__") == "edge"
        assert sanitize_name("...dots...") == "dots"

    def test_mixed_realistic(self):
        assert sanitize_name("My Cool Server!!!") == "my-cool-server"

    def test_only_invalid_chars_returns_empty(self):
        # All chars dropped -> "" (Lambda is expected to generate a name).
        assert sanitize_name("!!!") == ""
        assert sanitize_name("@#$%") == ""

    def test_only_separators_returns_empty(self):
        # Separators -> hyphens -> collapse -> strip -> "".
        assert sanitize_name("___") == ""
        assert sanitize_name("   ") == ""

    def test_truncation_to_63(self):
        out = sanitize_name("a" * 100)
        assert len(out) == 63
        assert out == "a" * 63

    def test_truncation_rstrips_trailing_hyphen(self):
        # 62 'a' then a separator that becomes '-' at position 63 (index 62);
        # slice [:63] keeps the trailing '-', which must be rstrip'd.
        raw = "a" * 62 + " b"          # -> "aaaa...a-b" (64 chars before trunc)
        out = sanitize_name(raw)
        assert len(out) <= 63
        assert not out.endswith("-")
        assert out == "a" * 62          # the '-' at index 62 stripped, 'b' cut off

    def test_truncation_result_is_valid(self):
        # A long messy name should come out DNS-valid.
        out = sanitize_name("Very Long Server Name " * 10)
        assert is_valid_name(out)

    def test_digits_preserved(self):
        assert sanitize_name("server007") == "server007"

    @pytest.mark.xfail(
        reason="BUG: sanitize_name keeps non-ASCII chars accepted by "
               "str.islower()/str.isdigit() (e.g. 'é'), producing names that "
               "are not DNS-safe despite the function's stated purpose.",
        strict=True,
    )
    def test_unicode_letters_should_be_dropped(self):
        assert sanitize_name("café") == "caf"


# --------------------------------------------------------------------------- #
# generate_unique_name
# --------------------------------------------------------------------------- #
class TestGenerateUniqueName:
    def test_no_preferred_name_raises(self):
        with pytest.raises(ValueError, match="without preferred_name"):
            generate_unique_name([])

    def test_explicit_none_preferred_raises(self):
        with pytest.raises(ValueError, match="without preferred_name"):
            generate_unique_name(["x"], preferred_name=None)

    def test_unsanitizable_preferred_raises(self):
        # Sanitizes to "" -> the "Invalid preferred name" branch.
        with pytest.raises(ValueError, match="Invalid preferred name"):
            generate_unique_name([], preferred_name="!!!")

    def test_returns_sanitized_when_available(self):
        assert generate_unique_name([], preferred_name="My Server") == "my-server"

    def test_returns_base_when_not_taken(self):
        assert generate_unique_name(["other"], preferred_name="alpha") == "alpha"

    def test_first_collision_appends_2(self):
        # base taken -> first variation is "-2" (range starts at 2).
        assert generate_unique_name(["alpha"], preferred_name="alpha") == "alpha-2"

    def test_skips_taken_variations(self):
        existing = ["alpha", "alpha-2", "alpha-3"]
        assert generate_unique_name(existing, preferred_name="alpha") == "alpha-4"

    def test_dedup_uses_sanitized_base(self):
        # Preferred is sanitized first, then de-duped against existing.
        existing = ["my-server"]
        assert generate_unique_name(existing, preferred_name="My Server") == "my-server-2"

    def test_variation_respects_63_char_limit(self):
        # base length 62 + "-2" = 64 > 63, so it must skip to a candidate that
        # fits. "-10".."-99" (len 65) also too long; only single-digit suffixes
        # could fit but those are >63 too (62+2=64). Actually NONE fit here, so
        # this should fall through to the exhaustion error.
        base = "a" * 62
        with pytest.raises(ValueError, match="after trying 999 variations"):
            generate_unique_name([base], preferred_name=base)

    def test_variation_within_limit_when_base_short(self):
        base = "a" * 60                      # 60 + "-2" = 62 <= 63, fits
        out = generate_unique_name([base], preferred_name=base)
        assert out == base + "-2"
        assert len(out) <= 63

    def test_exhaustion_raises(self):
        base = "node"
        existing = [base] + [f"{base}-{i}" for i in range(2, 1000)]
        with pytest.raises(ValueError, match="after trying 999 variations"):
            generate_unique_name(existing, preferred_name=base)

    def test_last_variation_999_is_returnable(self):
        base = "node"
        # Occupy base + -2..-998, leaving -999 free.
        existing = [base] + [f"{base}-{i}" for i in range(2, 999)]
        assert generate_unique_name(existing, preferred_name=base) == f"{base}-999"

    def test_result_is_valid_name(self):
        out = generate_unique_name(["alpha"], preferred_name="alpha")
        assert is_valid_name(out)

    def test_existing_names_not_mutated(self):
        existing = ["alpha"]
        before = list(existing)
        generate_unique_name(existing, preferred_name="alpha")
        assert existing == before
