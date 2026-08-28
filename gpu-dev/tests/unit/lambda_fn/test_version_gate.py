"""Unit tests for index.validate_cli_version + MIN_CLI_VERSION.

The lambda gates incoming SQS messages on the CLI version that produced them.
``validate_cli_version(message_body)`` reads ``message_body["version"]`` and:
  - raises ValueError if no version (missing / None / empty) -> "no longer supported"
  - parses both sides as int tuples (split on '.'); garbage -> (0, 0, 0)
  - raises ValueError if cli_ver_tuple < MIN_CLI_VERSION tuple -> "outdated"
  - otherwise returns None (accepted), logging success.

All tests are pure-logic; no AWS / network / k8s involved.
"""
import pytest


# --- MIN_CLI_VERSION constant ---------------------------------------------------

def test_min_cli_version_default(lambda_index):
    """conftest does not set MIN_CLI_VERSION, so it falls back to the default."""
    assert lambda_index.MIN_CLI_VERSION == "0.3.9"


def test_min_cli_version_is_parseable(lambda_index):
    """The configured minimum must itself parse into a 3-int tuple."""
    parts = lambda_index.MIN_CLI_VERSION.split(".")
    assert all(p.isdigit() for p in parts)
    assert tuple(map(int, parts)) == (0, 3, 9)


# --- missing / falsy version -> rejected ----------------------------------------

def test_missing_version_key_rejected(lambda_index):
    with pytest.raises(ValueError) as exc:
        lambda_index.validate_cli_version({})
    assert "no longer supported" in str(exc.value)
    assert "pip install --upgrade gpu-dev" in str(exc.value)


def test_none_version_rejected(lambda_index):
    with pytest.raises(ValueError) as exc:
        lambda_index.validate_cli_version({"version": None})
    assert "no longer supported" in str(exc.value)


def test_empty_string_version_rejected(lambda_index):
    """Empty string is falsy -> treated as a missing/old CLI, not parsed."""
    with pytest.raises(ValueError) as exc:
        lambda_index.validate_cli_version({"version": ""})
    assert "no longer supported" in str(exc.value)
    # The empty-string branch is the "missing" message, not the "outdated" one.
    assert "Minimum required version" not in str(exc.value)


# --- below minimum -> rejected with the 'outdated' message -----------------------

def test_version_below_min_patch_rejected(lambda_index, monkeypatch):
    monkeypatch.setattr(lambda_index, "MIN_CLI_VERSION", "0.3.9")
    with pytest.raises(ValueError) as exc:
        lambda_index.validate_cli_version({"version": "0.3.8"})
    msg = str(exc.value)
    assert "0.3.8" in msg
    assert "Minimum required version is 0.3.9" in msg
    assert "outdated" in msg


def test_version_below_min_minor_rejected(lambda_index, monkeypatch):
    monkeypatch.setattr(lambda_index, "MIN_CLI_VERSION", "0.3.9")
    with pytest.raises(ValueError):
        lambda_index.validate_cli_version({"version": "0.2.99"})


def test_version_below_min_major_rejected(lambda_index, monkeypatch):
    monkeypatch.setattr(lambda_index, "MIN_CLI_VERSION", "1.0.0")
    with pytest.raises(ValueError) as exc:
        lambda_index.validate_cli_version({"version": "0.9.9"})
    assert "Minimum required version is 1.0.0" in str(exc.value)


# --- equal / above minimum -> accepted (returns None, no raise) -----------------

def test_version_exactly_equal_accepted(lambda_index, monkeypatch):
    monkeypatch.setattr(lambda_index, "MIN_CLI_VERSION", "0.3.9")
    assert lambda_index.validate_cli_version({"version": "0.3.9"}) is None


def test_version_above_min_patch_accepted(lambda_index, monkeypatch):
    monkeypatch.setattr(lambda_index, "MIN_CLI_VERSION", "0.3.9")
    assert lambda_index.validate_cli_version({"version": "0.3.10"}) is None


def test_version_above_min_minor_accepted(lambda_index, monkeypatch):
    monkeypatch.setattr(lambda_index, "MIN_CLI_VERSION", "0.3.9")
    assert lambda_index.validate_cli_version({"version": "0.4.0"}) is None


def test_version_above_min_major_accepted(lambda_index, monkeypatch):
    monkeypatch.setattr(lambda_index, "MIN_CLI_VERSION", "0.3.9")
    assert lambda_index.validate_cli_version({"version": "1.0.0"}) is None


def test_patch_numeric_not_lexical(lambda_index, monkeypatch):
    """'0.3.10' must be >= '0.3.9' numerically (10 > 9), not lexically ('10' < '9')."""
    monkeypatch.setattr(lambda_index, "MIN_CLI_VERSION", "0.3.9")
    # Accepted because parsed as ints, not compared as strings.
    assert lambda_index.validate_cli_version({"version": "0.3.10"}) is None


# --- garbage / malformed versions -> parsed as (0, 0, 0) ------------------------

def test_garbage_version_rejected(lambda_index, monkeypatch):
    """Non-numeric version parses to (0,0,0) which is below any real minimum."""
    monkeypatch.setattr(lambda_index, "MIN_CLI_VERSION", "0.3.9")
    with pytest.raises(ValueError) as exc:
        lambda_index.validate_cli_version({"version": "abc"})
    # Falls into the 'outdated' branch (version was truthy, just unparseable).
    assert "Minimum required version is 0.3.9" in str(exc.value)


def test_partially_garbage_version_rejected(lambda_index, monkeypatch):
    """One non-int component makes the whole parse fall back to (0,0,0)."""
    monkeypatch.setattr(lambda_index, "MIN_CLI_VERSION", "0.3.9")
    with pytest.raises(ValueError):
        lambda_index.validate_cli_version({"version": "0.3.x"})


def test_version_with_suffix_rejected(lambda_index, monkeypatch):
    """'1.2.3-beta' -> '3-beta' is not an int -> (0,0,0) -> rejected."""
    monkeypatch.setattr(lambda_index, "MIN_CLI_VERSION", "0.3.9")
    with pytest.raises(ValueError):
        lambda_index.validate_cli_version({"version": "1.2.3-beta"})


def test_garbage_min_version_accepts_everything(lambda_index, monkeypatch):
    """If the configured minimum is garbage it parses to (0,0,0); any real
    version then compares >= (0,0,0) and is accepted."""
    monkeypatch.setattr(lambda_index, "MIN_CLI_VERSION", "not-a-version")
    assert lambda_index.validate_cli_version({"version": "0.0.1"}) is None
    # And a (0,0,0)-parsing version equals the (0,0,0) minimum -> not < -> accepted.
    assert lambda_index.validate_cli_version({"version": "also-garbage"}) is None


# --- shorter / longer tuple comparisons (Python tuple semantics) ----------------

def test_shorter_higher_minor_accepted(lambda_index, monkeypatch):
    """'0.4' -> (0,4); (0,4) < (0,3,9) is False (4 > 3) -> accepted."""
    monkeypatch.setattr(lambda_index, "MIN_CLI_VERSION", "0.3.9")
    assert lambda_index.validate_cli_version({"version": "0.4"}) is None


def test_shorter_equal_prefix_is_below(lambda_index, monkeypatch):
    """'0.3' -> (0,3); (0,3) < (0,3,9) is True (shorter prefix) -> rejected."""
    monkeypatch.setattr(lambda_index, "MIN_CLI_VERSION", "0.3.9")
    with pytest.raises(ValueError) as exc:
        lambda_index.validate_cli_version({"version": "0.3"})
    assert "Minimum required version is 0.3.9" in str(exc.value)


def test_longer_tuple_above_min_accepted(lambda_index, monkeypatch):
    """'0.3.9.1' -> (0,3,9,1); (0,3,9,1) < (0,3,9) is False -> accepted."""
    monkeypatch.setattr(lambda_index, "MIN_CLI_VERSION", "0.3.9")
    assert lambda_index.validate_cli_version({"version": "0.3.9.1"}) is None


# --- non-string version values --------------------------------------------------

def test_integer_version_value_rejected(lambda_index, monkeypatch):
    """A non-string truthy version (int) has no .split -> AttributeError caught
    in parse_version -> (0,0,0) -> below min -> rejected."""
    monkeypatch.setattr(lambda_index, "MIN_CLI_VERSION", "0.3.9")
    with pytest.raises(ValueError) as exc:
        lambda_index.validate_cli_version({"version": 5})
    assert "Minimum required version is 0.3.9" in str(exc.value)


def test_zero_version_value_rejected_as_missing(lambda_index):
    """Integer 0 is falsy -> hits the 'no version provided' branch."""
    with pytest.raises(ValueError) as exc:
        lambda_index.validate_cli_version({"version": 0})
    assert "no longer supported" in str(exc.value)
