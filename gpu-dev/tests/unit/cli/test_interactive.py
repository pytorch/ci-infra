"""Unit tests for gpu_dev_cli.interactive (no TTY, nothing prompts).

Focus (per task):
  * _is_spot_type — the spot SKU predicate.
  * the spot-hide gate in select_gpu_type_interactive — spot rows/types are
    filtered out of the questionary choices unless show_spot=True (or the env is
    spot-only, in which case nothing is hidden).
  * _mig_breakdown — compact per-slice availability rendered as a sub-row under
    the parent GPU ("12×1G" etc.) and folded into the parent's totals.
  * the boxed-row width / _bar / _line / _ft helpers, reached via the captured
    `choices` argument to questionary.select (the separators ARE the box).

Everything is exercised WITHOUT a real terminal: check_interactive_support and
questionary.select are patched so nothing ever prompts. The pure helpers
(_format_eta_seconds, the validators) are called directly.

questionary IS installed in the test env, so we use the real Separator/Choice
objects and inspect their .title / .value / .line attributes.
"""
from unittest.mock import MagicMock, patch

import pytest
import questionary

from gpu_dev_cli import interactive
from gpu_dev_cli.interactive import (
    _is_spot_type,
    _format_eta_seconds,
    _validate_duration,
    _validate_github_username,
    _validate_extension,
    _validate_disk_name,
    select_gpu_type_interactive,
)


# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #

def _is_choice(x):
    return isinstance(x, questionary.Choice) and not isinstance(x, questionary.Separator)


def _is_sep(x):
    return isinstance(x, questionary.Separator)


def _choice_values(choices):
    return [c.value for c in choices if _is_choice(c)]


def _flatten_title(title):
    """Choice titles are either plain strings or FormattedText (list of
    (style, text) tuples). Return a single plain string for substring asserts."""
    if isinstance(title, str):
        return title
    if isinstance(title, (list, tuple)):
        return "".join(t[1] for t in title if isinstance(t, (list, tuple)) and len(t) >= 2)
    return str(title)


def _sep_lines(choices):
    """Plain text of every Separator (box borders + header + mig/maint rows)."""
    return [getattr(s, "line", "") for s in choices if _is_sep(s)]


def _run_selector(availability, *, show_spot=False, environment="prod",
                  answer=None, environments=None):
    """Invoke select_gpu_type_interactive with everything stubbed so it never
    prompts, returning (result, captured_choices).

    `answer` is what the (patched) questionary.select(...).ask() yields.
    `environments` lets a test inject a custom Config.ENVIRONMENTS mapping
    (defaults to the real one from gpu_dev_cli.config).
    """
    from gpu_dev_cli.config import Config as RealConfig

    captured = {}

    def _fake_select(message, choices=None, **kwargs):
        captured["message"] = message
        captured["choices"] = choices
        m = MagicMock()
        m.ask.return_value = answer
        return m

    fake_cfg = MagicMock(name="config")
    fake_cfg.user_config = {"environment": environment}

    fake_config_cls = MagicMock(name="Config")
    fake_config_cls.ENVIRONMENTS = (
        environments if environments is not None else RealConfig.ENVIRONMENTS
    )

    # The prod+show_spot path does `import boto3; boto3.resource(...).Table().scan()`
    # to fetch us-east-1 spot availability. Force it to raise so the function's
    # `except Exception: pass` branch is taken — keeps the test hermetic (no DNS,
    # no AWS) and deterministic regardless of credentials.
    import boto3
    with patch.object(interactive, "check_interactive_support", return_value=True), \
         patch.object(interactive.questionary, "select", side_effect=_fake_select), \
         patch("gpu_dev_cli.config.load_config", return_value=fake_cfg), \
         patch("gpu_dev_cli.config.Config", fake_config_cls), \
         patch.object(boto3, "resource", side_effect=RuntimeError("no aws in tests")):
        result = select_gpu_type_interactive(availability, show_spot=show_spot)

    return result, captured.get("choices", [])


# --------------------------------------------------------------------------- #
# _is_spot_type
# --------------------------------------------------------------------------- #

class TestIsSpotType:
    def test_cpu_spot_is_spot(self):
        assert _is_spot_type("cpu-spot") is True

    @pytest.mark.parametrize("gt", ["h100-spot", "b200-spot", "t4-spot", "-spot"])
    def test_dash_spot_suffix_is_spot(self, gt):
        assert _is_spot_type(gt) is True

    @pytest.mark.parametrize("gt", ["h100", "cpu", "cpu-x", "spot", "h100-mig-1g", "spotty"])
    def test_non_spot(self, gt):
        assert _is_spot_type(gt) is False

    def test_plain_cpu_not_spot(self):
        # only the exact "cpu-spot" string matches the cpu branch
        assert _is_spot_type("cpu") is False
        assert _is_spot_type("cpu-arm") is False


# --------------------------------------------------------------------------- #
# _format_eta_seconds
# --------------------------------------------------------------------------- #

class TestFormatEtaSeconds:
    def test_under_a_minute(self):
        assert _format_eta_seconds(0) == "<1min"
        assert _format_eta_seconds(59) == "<1min"

    def test_exactly_one_minute(self):
        assert _format_eta_seconds(60) == "1min"

    def test_minutes_floor(self):
        assert _format_eta_seconds(119) == "1min"
        assert _format_eta_seconds(720) == "12min"

    def test_under_an_hour_boundary(self):
        assert _format_eta_seconds(3599) == "59min"

    def test_exact_hours_drops_minutes(self):
        assert _format_eta_seconds(3600) == "1h"
        assert _format_eta_seconds(7200) == "2h"

    def test_hours_and_minutes(self):
        # 1h24min -> 3600 + 24*60 = 5040
        assert _format_eta_seconds(5040) == "1h24min"
        assert _format_eta_seconds(3660) == "1h1min"


# --------------------------------------------------------------------------- #
# validators (return True on success, a string message on failure)
# --------------------------------------------------------------------------- #

class TestValidateDuration:
    def test_valid(self):
        assert _validate_duration("1") is True
        assert _validate_duration("24") is True
        assert _validate_duration("0.0833") is True

    def test_too_short(self):
        msg = _validate_duration("0.05")
        assert isinstance(msg, str) and "Minimum" in msg

    def test_too_long_gpu(self):
        msg = _validate_duration("25")
        assert isinstance(msg, str) and "Maximum" in msg

    def test_too_long_allowed_when_unlimited(self):
        assert _validate_duration("100", unlimited=True) is True

    def test_unlimited_still_enforces_minimum(self):
        msg = _validate_duration("0.01", unlimited=True)
        assert isinstance(msg, str) and "Minimum" in msg

    def test_non_numeric(self):
        msg = _validate_duration("abc")
        assert isinstance(msg, str) and "valid number" in msg


class TestValidateGithubUsername:
    def test_valid(self):
        assert _validate_github_username("octo-cat") is True
        assert _validate_github_username("user_1.x") is True

    def test_empty(self):
        assert isinstance(_validate_github_username(""), str)
        assert isinstance(_validate_github_username("   "), str)

    def test_invalid_chars(self):
        msg = _validate_github_username("bad name!")
        assert isinstance(msg, str) and "Invalid" in msg

    def test_too_long(self):
        msg = _validate_github_username("a" * 40)
        assert isinstance(msg, str) and "too long" in msg

    def test_max_length_ok(self):
        assert _validate_github_username("a" * 39) is True


class TestValidateExtension:
    def test_valid(self):
        assert _validate_extension("1") is True
        assert _validate_extension("24") is True

    def test_non_positive(self):
        assert isinstance(_validate_extension("0"), str)
        assert isinstance(_validate_extension("-3"), str)

    def test_too_long(self):
        msg = _validate_extension("25")
        assert isinstance(msg, str) and "Maximum" in msg

    def test_non_numeric(self):
        assert isinstance(_validate_extension("ten"), str)


class TestValidateDiskName:
    def test_valid(self):
        assert _validate_disk_name("my-disk_1") is True

    def test_empty(self):
        assert isinstance(_validate_disk_name(""), str)
        assert isinstance(_validate_disk_name("   "), str)

    def test_invalid_chars(self):
        msg = _validate_disk_name("bad disk!")
        assert isinstance(msg, str) and "only letters" in msg

    def test_too_long(self):
        msg = _validate_disk_name("d" * 51)
        assert isinstance(msg, str) and "too long" in msg

    def test_max_length_ok(self):
        assert _validate_disk_name("d" * 50) is True


# --------------------------------------------------------------------------- #
# check_interactive_support — no-TTY behavior
# --------------------------------------------------------------------------- #

class TestCheckInteractiveSupport:
    def test_false_when_questionary_missing(self):
        with patch.object(interactive, "INTERACTIVE_AVAILABLE", False):
            assert interactive.check_interactive_support() is False

    def test_false_when_not_a_tty(self):
        stdin = MagicMock()
        stdin.isatty.return_value = False
        with patch.object(interactive, "INTERACTIVE_AVAILABLE", True), \
             patch.object(interactive.sys, "stdin", stdin):
            assert interactive.check_interactive_support() is False

    def test_true_when_tty_and_available(self):
        stdin = MagicMock()
        stdin.isatty.return_value = True
        with patch.object(interactive, "INTERACTIVE_AVAILABLE", True), \
             patch.object(interactive.sys, "stdin", stdin):
            assert interactive.check_interactive_support() is True


# --------------------------------------------------------------------------- #
# select_gpu_type_interactive — early return without TTY
# --------------------------------------------------------------------------- #

def test_selector_returns_none_without_interactive_support():
    with patch.object(interactive, "check_interactive_support", return_value=False):
        assert select_gpu_type_interactive({"h100": {"available": 1}}) is None


# --------------------------------------------------------------------------- #
# select_gpu_type_interactive — the selectable value flows back unchanged
# --------------------------------------------------------------------------- #

def test_selector_returns_chosen_value():
    avail = {"h100": {"available": 2, "total": 8, "max_reservable": 8}}
    result, _ = _run_selector(avail, answer="h100")
    assert result == "h100"


# --------------------------------------------------------------------------- #
# select_gpu_type_interactive — MIG SKUs hidden from the top-level rows
# --------------------------------------------------------------------------- #

class TestMigHiddenFromTopLevel:
    def test_mig_skus_not_selectable_rows(self):
        avail = {
            "h100": {"available": 4, "total": 8, "max_reservable": 8},
            "h100-mig-1g": {"available": 12, "total": 16},
            "h100-mig-2g": {"available": 4, "total": 8},
        }
        _, choices = _run_selector(avail)
        # Only "h100" (and the control choices) are selectable values; the MIG
        # SKUs never become their own Choice.
        vals = _choice_values(choices)
        assert "h100" in vals
        assert "h100-mig-1g" not in vals
        assert "h100-mig-2g" not in vals

    def test_mig_breakdown_rendered_as_subrow(self):
        avail = {
            "h100": {"available": 4, "total": 8, "max_reservable": 8},
            "h100-mig-1g": {"available": 12, "total": 16},
            "h100-mig-2g": {"available": 4, "total": 8},
        }
        _, choices = _run_selector(avail)
        seps = "\n".join(_sep_lines(choices))
        # _mig_breakdown: "12×1G" and "4×2G", on the " └─ MIG" sub-row.
        assert "└─ MIG" in seps
        assert "12×1G" in seps
        assert "4×2G" in seps
        # parent totals folded in: avail 12+4=16, total 16+8=24 appear on that row.
        mig_row = [ln for ln in _sep_lines(choices) if "└─ MIG" in ln][0]
        assert "16" in mig_row  # summed available
        assert "24" in mig_row  # summed total

    def test_no_mig_subrow_when_no_slices(self):
        avail = {"h100": {"available": 4, "total": 8, "max_reservable": 8}}
        _, choices = _run_selector(avail)
        seps = "\n".join(_sep_lines(choices))
        assert "MIG" not in seps


# --------------------------------------------------------------------------- #
# select_gpu_type_interactive — the spot-hide gate
# --------------------------------------------------------------------------- #

class TestSpotHideGate:
    def _avail(self):
        return {
            "h100": {"available": 2, "total": 8, "max_reservable": 8},
            "cpu-spot": {"available": 0, "total": 0, "max_reservable": 0},
        }

    def test_spot_hidden_by_default(self):
        _, choices = _run_selector(self._avail(), show_spot=False)
        vals = _choice_values(choices)
        # cpu-spot is filtered out of the visible rows.
        assert "h100" in vals
        assert "cpu-spot" not in vals
        # ...and the "Show spot options" toggle is offered instead.
        titles = [_flatten_title(c.title) for c in choices if _is_choice(c)]
        assert any("Show spot options" in t for t in titles)
        assert any("_show_spot" == c.value for c in choices if _is_choice(c))

    def test_show_spot_true_keeps_spot_type_visible(self):
        # With show_spot=True the cpu-spot row is NOT filtered out by the gate.
        _, choices = _run_selector(self._avail(), show_spot=True)
        vals = _choice_values(choices)
        assert "cpu-spot" in vals
        # and the toggle is no longer offered (nothing left to reveal).
        titles = [_flatten_title(c.title) for c in choices if _is_choice(c)]
        assert not any("Show spot options" in t for t in titles)

    def test_refresh_choice_always_present(self):
        _, choices = _run_selector(self._avail())
        assert "_refresh" in _choice_values(choices)

    def test_spot_only_env_does_not_hide_everything(self):
        # When NO non-spot, non-MIG type exists, the gate must NOT hide spot
        # (_non_spot_exists is False) — otherwise the menu would be empty.
        avail = {"cpu-spot": {"available": 1, "total": 4, "max_reservable": 4}}
        _, choices = _run_selector(avail, show_spot=False)
        vals = _choice_values(choices)
        assert "cpu-spot" in vals
        # No spot toggle, since spot is already shown.
        titles = [_flatten_title(c.title) for c in choices if _is_choice(c)]
        assert not any("Show spot options" in t for t in titles)

    def test_mig_only_does_not_count_as_non_spot(self):
        # _non_spot_exists ignores -mig- SKUs; with only mig + spot, spot must
        # still be shown (the menu would otherwise hide both spot and the mig
        # parent has no row).
        avail = {
            "h100-mig-1g": {"available": 4, "total": 16},
            "cpu-spot": {"available": 2, "total": 4, "max_reservable": 4},
        }
        _, choices = _run_selector(avail, show_spot=False)
        # cpu-spot shown because no non-spot non-mig type exists.
        assert "cpu-spot" in _choice_values(choices)


# --------------------------------------------------------------------------- #
# select_gpu_type_interactive — the boxed-row width / _bar / _line / _ft helpers
# --------------------------------------------------------------------------- #

class TestBoxRendering:
    def _choices(self, avail, **kw):
        _, choices = _run_selector(avail, **kw)
        return choices

    def test_header_row_present(self):
        avail = {"h100": {"available": 2, "total": 8, "max_reservable": 8}}
        seps = "\n".join(_sep_lines(self._choices(avail)))
        for h in ("GPU Type", "Avail", "MaxRes", "Total", "Status"):
            assert h in seps

    def test_box_borders_present(self):
        avail = {"h100": {"available": 2, "total": 8, "max_reservable": 8}}
        lines = _sep_lines(self._choices(avail))
        joined = "\n".join(lines)
        # top / middle-divider / bottom box corners from _bar()
        assert any(ln.startswith("┌") and ln.endswith("┐") for ln in lines)
        assert any(ln.startswith("├") and ln.endswith("┤") for ln in lines)
        assert any(ln.startswith("└") and ln.endswith("┘") for ln in lines)

    def test_column_width_consistent(self):
        # All box rows (corners + header + the colored data row) must share the
        # exact same printable width — that is the whole point of the W[] sizing.
        avail = {
            "h100": {"available": 2, "total": 8, "max_reservable": 8},
            "b200": {"available": 0, "total": 8, "max_reservable": 8},
        }
        _, choices = _run_selector(avail)
        sep_widths = {len(ln) for ln in _sep_lines(choices) if ln and ln[0] in "┌├└│"}
        # data rows are FormattedText Choices — reconstruct their printable text
        data_widths = set()
        for c in choices:
            if _is_choice(c) and not isinstance(c.title, str):
                data_widths.add(len(_flatten_title(c.title)))
        assert len(sep_widths) == 1, f"box rows ragged: {sep_widths}"
        # data rows match the box width too
        assert data_widths == sep_widths, (data_widths, sep_widths)

    def test_gpu_type_uppercased_in_cell(self):
        avail = {"h100": {"available": 2, "total": 8, "max_reservable": 8}}
        _, choices = _run_selector(avail)
        data = [_flatten_title(c.title) for c in choices
                if _is_choice(c) and not isinstance(c.title, str)]
        assert any("H100" in d for d in data)

    def test_empty_availability_shows_none_row(self):
        # No GPU rows at all -> a "(none)" placeholder Separator is rendered.
        _, choices = _run_selector({})
        seps = "\n".join(_sep_lines(choices))
        assert "(none)" in seps
        # still selectable: refresh remains.
        assert "_refresh" in _choice_values(choices)


# --------------------------------------------------------------------------- #
# select_gpu_type_interactive — _status text (available / queued)
# --------------------------------------------------------------------------- #

class TestStatusCells:
    def test_available_now_when_free(self):
        avail = {"h100": {"available": 3, "total": 8, "max_reservable": 8}}
        _, choices = _run_selector(avail)
        row = [_flatten_title(c.title) for c in choices
               if _is_choice(c) and not isinstance(c.title, str)][0]
        assert "available now" in row

    def test_queued_when_none_free_no_eta(self):
        avail = {"h100": {"available": 0, "total": 8, "max_reservable": 0,
                          "queue_length": 0, "estimated_wait_minutes": 0}}
        _, choices = _run_selector(avail)
        row = [_flatten_title(c.title) for c in choices
               if _is_choice(c) and not isinstance(c.title, str)][0]
        assert "queued" in row

    def test_queue_length_appended(self):
        avail = {"h100": {"available": 0, "total": 8, "max_reservable": 0,
                          "queue_length": 3, "estimated_wait_minutes": 0}}
        _, choices = _run_selector(avail)
        row = [_flatten_title(c.title) for c in choices
               if _is_choice(c) and not isinstance(c.title, str)][0]
        assert "3 queued" in row

    def test_maintenance_row_is_separator_not_choice(self):
        avail = {"h100": {"available": 0, "total": 8, "maintenance": True,
                          "maintenance_reason": "driver upgrade"}}
        _, choices = _run_selector(avail)
        # maintenance rows render as a Separator (non-selectable), not a Choice.
        assert "h100" not in _choice_values(choices)
        seps = "\n".join(_sep_lines(choices))
        assert "MAINT" in seps
        assert "driver upgrade" in seps
