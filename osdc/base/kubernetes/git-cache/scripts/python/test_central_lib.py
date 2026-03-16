"""Unit tests for central_lib — extracted testable functions from central.py."""

import ast
from pathlib import Path

import yaml
from central_lib import (
    MetricsServer,
    _dir_size_bytes,
    active_slot,
    set_current,
    slot_is_valid,
    staging_slot,
)

# ============================================================================
# MetricsServer tests
# ============================================================================


class TestMetricsServerSet:
    """Tests for MetricsServer.set() — gauge values."""

    def test_set_without_labels(self):
        m = MetricsServer()
        m.set("my_gauge", 42.0)
        output = m.format_metrics()
        assert "my_gauge 42.0" in output

    def test_set_with_labels(self):
        m = MetricsServer()
        m.set("my_gauge", 7.5, {"repo": "pytorch/pytorch"})
        output = m.format_metrics()
        assert 'my_gauge{repo="pytorch/pytorch"} 7.5' in output

    def test_set_overwrites_previous(self):
        m = MetricsServer()
        m.set("my_gauge", 1.0)
        m.set("my_gauge", 99.0)
        output = m.format_metrics()
        assert "my_gauge 99.0" in output
        assert "my_gauge 1.0" not in output

    def test_set_multiple_label_sets(self):
        m = MetricsServer()
        m.set("size", 100.0, {"repo": "a"})
        m.set("size", 200.0, {"repo": "b"})
        output = m.format_metrics()
        assert 'size{repo="a"} 100.0' in output
        assert 'size{repo="b"} 200.0' in output


class TestMetricsServerInc:
    """Tests for MetricsServer.inc() — counter values."""

    def test_inc_from_zero(self):
        m = MetricsServer()
        m.inc("my_counter")
        output = m.format_metrics()
        assert "my_counter 1" in output

    def test_inc_accumulates(self):
        m = MetricsServer()
        m.inc("my_counter")
        m.inc("my_counter")
        m.inc("my_counter")
        output = m.format_metrics()
        assert "my_counter 3" in output

    def test_inc_with_labels(self):
        m = MetricsServer()
        m.inc("errors", {"repo": "pytorch/pytorch"})
        m.inc("errors", {"repo": "pytorch/pytorch"})
        output = m.format_metrics()
        assert 'errors{repo="pytorch/pytorch"} 2' in output

    def test_inc_custom_amount(self):
        m = MetricsServer()
        m.inc("my_counter", amount=5)
        output = m.format_metrics()
        assert "my_counter 5" in output


class TestMetricsServerObserve:
    """Tests for MetricsServer.observe() — histogram values."""

    def test_observe_single_value(self):
        m = MetricsServer()
        m.observe("git_cache_central_clone_duration_seconds", 3.0, {"repo": "a"})
        output = m.format_metrics()
        assert "_bucket" in output
        assert "_sum" in output
        assert "_count" in output

    def test_observe_bucket_placement(self):
        """A value of 3.0 should land in the 5.0 bucket (smallest >= 3.0)."""
        m = MetricsServer()
        m.observe("git_cache_central_clone_duration_seconds", 3.0, {"repo": "a"})
        output = m.format_metrics()
        lines = output.splitlines()
        # Find bucket lines — 1.0 bucket should have cumulative 0, 5.0 should have 1
        bucket_1 = [line for line in lines if 'le="1.0"' in line and 'repo="a"' in line]
        bucket_5 = [line for line in lines if 'le="5.0"' in line and 'repo="a"' in line]
        assert len(bucket_1) == 1
        assert bucket_1[0].endswith(" 0")
        assert len(bucket_5) == 1
        assert bucket_5[0].endswith(" 1")

    def test_observe_sum_and_count(self):
        m = MetricsServer()
        m.observe("git_cache_central_clone_duration_seconds", 10.0, {"repo": "x"})
        m.observe("git_cache_central_clone_duration_seconds", 20.0, {"repo": "x"})
        output = m.format_metrics()
        assert "git_cache_central_clone_duration_seconds_sum" in output
        assert "git_cache_central_clone_duration_seconds_count" in output
        # sum = 30.0, count = 2
        assert '_sum{repo="x"} 30.0' in output
        assert '_count{repo="x"} 2' in output

    def test_observe_cumulative_buckets(self):
        """Histogram buckets must be cumulative in output."""
        m = MetricsServer()
        # One value at 2.0 (→ 5.0 bucket), one at 50.0 (→ 60.0 bucket)
        m.observe("git_cache_central_clone_duration_seconds", 2.0)
        m.observe("git_cache_central_clone_duration_seconds", 50.0)
        output = m.format_metrics()
        # 1.0 bucket: 0
        assert 'le="1.0"} 0' in output
        # 5.0 bucket: 1 (the 2.0 value)
        assert 'le="5.0"} 1' in output
        # 60.0 bucket: 2 (cumulative: 1 + 1)
        assert 'le="60.0"} 2' in output
        # +Inf bucket: 2 (cumulative total)
        assert 'le="+Inf"} 2' in output

    def test_observe_inf_bucket_value(self):
        """A very large value should land in the +Inf bucket."""
        m = MetricsServer()
        m.observe("git_cache_central_clone_duration_seconds", 99999.0)
        output = m.format_metrics()
        assert 'le="+Inf"} 1' in output


class TestMetricsServerFormatMetrics:
    """Tests for Prometheus text exposition format output."""

    def test_empty_metrics(self):
        m = MetricsServer()
        output = m.format_metrics()
        # No metrics registered — only the trailing empty string from join
        assert output == ""

    def test_help_and_type_lines(self):
        m = MetricsServer()
        m.set("git_cache_central_repos_total", 5)
        output = m.format_metrics()
        assert "# HELP git_cache_central_repos_total Number of repos being cached" in output
        assert "# TYPE git_cache_central_repos_total gauge" in output

    def test_unknown_metric_type(self):
        m = MetricsServer()
        m.set("unknown_metric_xyz", 1.0)
        output = m.format_metrics()
        assert "# TYPE unknown_metric_xyz untyped" in output

    def test_labels_sorted_alphabetically(self):
        m = MetricsServer()
        m.set("my_metric", 1.0, {"z_label": "z", "a_label": "a"})
        output = m.format_metrics()
        assert 'a_label="a",z_label="z"' in output


# ============================================================================
# active_slot / staging_slot tests
# ============================================================================


class TestActiveSlot:
    """Tests for active_slot() — symlink resolution."""

    def test_no_symlink_returns_none(self, tmp_path):
        assert active_slot(tmp_path) is None

    def test_symlink_to_slot_a(self, tmp_path):
        slot_a = tmp_path / "cache-a"
        slot_a.mkdir()
        (tmp_path / "current").symlink_to(slot_a)
        assert active_slot(tmp_path) == slot_a.resolve()

    def test_symlink_to_slot_b(self, tmp_path):
        slot_b = tmp_path / "cache-b"
        slot_b.mkdir()
        (tmp_path / "current").symlink_to(slot_b)
        assert active_slot(tmp_path) == slot_b.resolve()

    def test_symlink_to_invalid_target_returns_none(self, tmp_path):
        (tmp_path / "cache-a").mkdir()
        (tmp_path / "cache-b").mkdir()
        other = tmp_path / "cache-c"
        other.mkdir()
        (tmp_path / "current").symlink_to(other)
        assert active_slot(tmp_path) is None


class TestStagingSlot:
    """Tests for staging_slot() — returns the opposite slot."""

    def test_active_a_returns_b(self, tmp_path):
        slot_a = tmp_path / "cache-a"
        slot_a.mkdir()
        (tmp_path / "cache-b").mkdir()
        (tmp_path / "current").symlink_to(slot_a)
        result = staging_slot(tmp_path)
        assert result == tmp_path / "cache-b"

    def test_active_b_returns_a(self, tmp_path):
        (tmp_path / "cache-a").mkdir()
        slot_b = tmp_path / "cache-b"
        slot_b.mkdir()
        (tmp_path / "current").symlink_to(slot_b)
        result = staging_slot(tmp_path)
        assert result == tmp_path / "cache-a"

    def test_no_active_returns_a(self, tmp_path):
        """When no symlink exists, staging defaults to cache-a."""
        result = staging_slot(tmp_path)
        assert result == tmp_path / "cache-a"


# ============================================================================
# slot_is_valid tests
# ============================================================================


class TestSlotIsValid:
    """Tests for slot_is_valid() — checks repo dirs exist in a slot."""

    def test_empty_slot_no_repos(self, tmp_path):
        slot = tmp_path / "cache-a"
        slot.mkdir()
        assert slot_is_valid(slot, [], []) is False

    def test_valid_full_repo(self, tmp_path):
        slot = tmp_path / "cache-a"
        (slot / "pytorch" / "pytorch" / ".git").mkdir(parents=True)
        assert slot_is_valid(slot, ["pytorch/pytorch"], []) is True

    def test_valid_bare_repo(self, tmp_path):
        slot = tmp_path / "cache-a"
        (slot / "pytorch" / "test-infra.git" / "objects").mkdir(parents=True)
        assert slot_is_valid(slot, [], ["pytorch/test-infra"]) is True

    def test_partial_repos_still_valid(self, tmp_path):
        """If at least one repo exists, slot is valid."""
        slot = tmp_path / "cache-a"
        (slot / "pytorch" / "pytorch" / ".git").mkdir(parents=True)
        # test-infra is missing — slot is still valid (has pytorch)
        assert slot_is_valid(slot, ["pytorch/pytorch"], ["pytorch/test-infra"]) is True

    def test_no_matching_repos(self, tmp_path):
        slot = tmp_path / "cache-a"
        slot.mkdir()
        assert slot_is_valid(slot, ["pytorch/pytorch"], ["pytorch/test-infra"]) is False

    def test_nonexistent_slot(self, tmp_path):
        slot = tmp_path / "nonexistent"
        assert slot_is_valid(slot, ["pytorch/pytorch"], []) is False


# ============================================================================
# set_current tests
# ============================================================================


class TestSetCurrent:
    """Tests for set_current() — atomic symlink swap."""

    def test_creates_symlink(self, tmp_path):
        slot = tmp_path / "cache-a"
        slot.mkdir()
        set_current(tmp_path, slot)
        link = tmp_path / "current"
        assert link.is_symlink()
        assert link.resolve() == slot.resolve()

    def test_swaps_existing_symlink(self, tmp_path):
        slot_a = tmp_path / "cache-a"
        slot_b = tmp_path / "cache-b"
        slot_a.mkdir()
        slot_b.mkdir()
        set_current(tmp_path, slot_a)
        assert (tmp_path / "current").resolve() == slot_a.resolve()
        set_current(tmp_path, slot_b)
        assert (tmp_path / "current").resolve() == slot_b.resolve()

    def test_no_leftover_tmp_link(self, tmp_path):
        slot = tmp_path / "cache-a"
        slot.mkdir()
        set_current(tmp_path, slot)
        assert not (tmp_path / "current.tmp").exists()


# ============================================================================
# _dir_size_bytes tests
# ============================================================================


class TestDirSizeBytes:
    """Tests for _dir_size_bytes() — filesystem traversal."""

    def test_empty_dir(self, tmp_path):
        d = tmp_path / "empty"
        d.mkdir()
        assert _dir_size_bytes(d) == 0

    def test_known_file_sizes(self, tmp_path):
        d = tmp_path / "data"
        d.mkdir()
        (d / "a.txt").write_bytes(b"hello")  # 5 bytes
        (d / "b.txt").write_bytes(b"world!")  # 6 bytes
        assert _dir_size_bytes(d) == 11

    def test_nested_dirs(self, tmp_path):
        d = tmp_path / "root"
        sub = d / "sub" / "deep"
        sub.mkdir(parents=True)
        (d / "top.txt").write_bytes(b"aa")  # 2 bytes
        (sub / "deep.txt").write_bytes(b"bbb")  # 3 bytes
        assert _dir_size_bytes(d) == 5

    def test_nonexistent_path(self, tmp_path):
        assert _dir_size_bytes(tmp_path / "nonexistent") == 0


# ============================================================================
# Drift detection tests
# ============================================================================

# Path from test file to ConfigMap YAML (scripts/python/ -> ../../)
_CONFIGMAP_PATH = Path(__file__).resolve().parent.parent.parent / "central-configmap.yaml"
_LIB_PATH = Path(__file__).resolve().parent / "central_lib.py"

# Parameters that central_lib intentionally adds for testability.
# The ConfigMap script uses module-level globals (DATA_DIR, REPOS_FULL, etc.)
# while the lib passes these explicitly so functions can be tested in isolation.
_INTENTIONAL_EXTRA_PARAMS = {
    "active_slot": {"cache_dir"},
    "staging_slot": {"cache_dir"},
    "slot_is_valid": {"repos_full", "repos_bare"},
    "set_current": {"cache_dir"},
}


def _extract_signatures(source: str) -> dict[str, dict[str, list[str]]]:
    """Parse Python source and return function/method signatures.

    Returns a nested dict:
        {"top_level": {"func_name": [arg_names]},
        "ClassName": {"method_name": [arg_names]}}

    ``self`` is excluded from method argument lists.
    """
    tree = ast.parse(source)
    result: dict[str, dict[str, list[str]]] = {}

    for node in ast.walk(tree):
        if isinstance(node, ast.ClassDef):
            methods: dict[str, list[str]] = {}
            for item in node.body:
                if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    args = [a.arg for a in item.args.args if a.arg != "self"]
                    methods[item.name] = args
            result[node.name] = methods
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            # Skip methods already captured inside classes
            parent_is_class = False
            for parent in ast.walk(tree):
                if isinstance(parent, ast.ClassDef) and node in ast.walk(parent) and node is not parent:
                    parent_is_class = True
                    break
            if not parent_is_class:
                result.setdefault("top_level", {})[node.name] = [a.arg for a in node.args.args]

    return result


def _get_top_level_funcs(tree_sigs: dict[str, dict[str, list[str]]]) -> dict[str, list[str]]:
    return tree_sigs.get("top_level", {})


def _get_class_methods(tree_sigs: dict[str, dict[str, list[str]]], class_name: str) -> dict[str, list[str]]:
    return tree_sigs.get(class_name, {})


class TestCentralLibDriftDetection:
    """Detect drift between central_lib.py and central-configmap.yaml.

    central_lib.py is a testable extraction of functions from the ConfigMap
    script. If someone edits the ConfigMap script (adds/removes/renames a
    function or changes its arguments) without updating the lib, these tests
    fail.

    Known intentional differences:
    - central_lib parameterizes functions with explicit args (``cache_dir``,
        ``repos_full``, ``repos_bare``) instead of using module globals.
    - ``set_current`` in the lib omits the metrics side-effect calls
        (metrics.inc, metrics.set) that the ConfigMap version performs.
    """

    @staticmethod
    def _load_configmap_script() -> str:
        """Read the ConfigMap YAML and extract the embedded central.py."""
        raw = _CONFIGMAP_PATH.read_text()
        cm = yaml.safe_load(raw)
        return cm["data"]["central.py"]

    @staticmethod
    def _load_lib_source() -> str:
        return _LIB_PATH.read_text()

    def test_configmap_yaml_is_readable(self):
        """Sanity check: the ConfigMap YAML exists and has a central.py key."""
        script = self._load_configmap_script()
        assert len(script) > 100, "central.py script too short — file may be corrupt"

    def test_lib_functions_exist_in_configmap(self):
        """Every top-level function in central_lib.py must have a
        corresponding function in the ConfigMap script."""
        cm_sigs = _extract_signatures(self._load_configmap_script())
        lib_sigs = _extract_signatures(self._load_lib_source())

        cm_funcs = _get_top_level_funcs(cm_sigs)
        lib_funcs = _get_top_level_funcs(lib_sigs)

        missing = []
        for name in lib_funcs:
            if name not in cm_funcs:
                missing.append(name)

        assert not missing, (
            f"Functions in central_lib.py not found in ConfigMap script: {missing}. "
            "If you renamed or removed a function in the ConfigMap, update central_lib.py too."
        )

    def test_lib_function_args_match_configmap(self):
        """Argument names of shared functions must match (minus intentional
        testability params like cache_dir, repos_full, repos_bare)."""
        cm_sigs = _extract_signatures(self._load_configmap_script())
        lib_sigs = _extract_signatures(self._load_lib_source())

        cm_funcs = _get_top_level_funcs(cm_sigs)
        lib_funcs = _get_top_level_funcs(lib_sigs)

        mismatches = []
        for name, lib_args in lib_funcs.items():
            if name not in cm_funcs:
                continue  # Caught by test_lib_functions_exist_in_configmap
            cm_args = cm_funcs[name]
            extra = _INTENTIONAL_EXTRA_PARAMS.get(name, set())
            # Remove intentional extra params from the lib args for comparison
            lib_args_filtered = [a for a in lib_args if a not in extra]
            if lib_args_filtered != cm_args:
                mismatches.append(f"  {name}: lib={lib_args_filtered}, configmap={cm_args}")

        assert not mismatches, "Function argument mismatch between central_lib.py and ConfigMap:\n" + "\n".join(
            mismatches
        )

    def test_metrics_server_methods_exist_in_configmap(self):
        """Every method in central_lib.MetricsServer must exist in the
        ConfigMap's MetricsServer class."""
        cm_sigs = _extract_signatures(self._load_configmap_script())
        lib_sigs = _extract_signatures(self._load_lib_source())

        cm_methods = _get_class_methods(cm_sigs, "MetricsServer")
        lib_methods = _get_class_methods(lib_sigs, "MetricsServer")

        missing = []
        for name in lib_methods:
            if name not in cm_methods:
                missing.append(name)

        assert not missing, (
            f"MetricsServer methods in central_lib.py not found in ConfigMap: {missing}. "
            "If you renamed or removed a method in the ConfigMap, update central_lib.py too."
        )

    def test_metrics_server_method_args_match(self):
        """MetricsServer method signatures must match between lib and ConfigMap."""
        cm_sigs = _extract_signatures(self._load_configmap_script())
        lib_sigs = _extract_signatures(self._load_lib_source())

        cm_methods = _get_class_methods(cm_sigs, "MetricsServer")
        lib_methods = _get_class_methods(lib_sigs, "MetricsServer")

        mismatches = []
        for name, lib_args in lib_methods.items():
            if name not in cm_methods:
                continue  # Caught by test_metrics_server_methods_exist_in_configmap
            cm_args = cm_methods[name]
            if lib_args != cm_args:
                mismatches.append(f"  MetricsServer.{name}: lib={lib_args}, configmap={cm_args}")

        assert not mismatches, "MetricsServer method argument mismatch:\n" + "\n".join(mismatches)
