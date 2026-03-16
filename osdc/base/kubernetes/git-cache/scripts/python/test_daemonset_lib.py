"""Unit tests for daemonset_lib — extracted testable functions from daemonset.py."""

import ast
from pathlib import Path
from unittest.mock import MagicMock, patch

import yaml
from daemonset_lib import (
    MetricsServer,
    _dir_size_bytes,
    active_slot,
    migrate_old_cache,
    set_current,
    staging_slot,
    wait_for_central,
)

# ============================================================================
# MetricsServer tests
# ============================================================================


class TestMetricsServerSet:
    """Tests for MetricsServer.set() — gauge values."""

    def test_set_value(self):
        m = MetricsServer()
        m.set("git_cache_node_sync_duration_seconds", 42.0)
        output = m.format()
        assert "git_cache_node_sync_duration_seconds 42.0" in output

    def test_set_overwrites_previous(self):
        m = MetricsServer()
        m.set("git_cache_node_sync_duration_seconds", 1.0)
        m.set("git_cache_node_sync_duration_seconds", 99.0)
        output = m.format()
        assert "git_cache_node_sync_duration_seconds 99.0" in output
        # The old value should not appear as a standalone metric line
        lines = [line for line in output.splitlines() if line.startswith("git_cache_node_sync_duration_seconds ")]
        assert len(lines) == 1
        assert lines[0].endswith("99.0")


class TestMetricsServerInc:
    """Tests for MetricsServer.inc() — counter values."""

    def test_inc_from_zero(self):
        m = MetricsServer()
        m.inc("git_cache_node_sync_success_total")
        output = m.format()
        assert "git_cache_node_sync_success_total 1.0" in output

    def test_inc_accumulates(self):
        m = MetricsServer()
        m.inc("git_cache_node_sync_errors_total")
        m.inc("git_cache_node_sync_errors_total")
        m.inc("git_cache_node_sync_errors_total")
        output = m.format()
        assert "git_cache_node_sync_errors_total 3.0" in output

    def test_inc_custom_delta(self):
        m = MetricsServer()
        m.inc("git_cache_node_sync_success_total", 5.0)
        output = m.format()
        assert "git_cache_node_sync_success_total 5.0" in output


class TestMetricsServerFormat:
    """Tests for Prometheus text exposition format output."""

    def test_help_and_type_lines(self):
        m = MetricsServer()
        output = m.format()
        assert "# HELP git_cache_node_sync_duration_seconds Duration of last rsync from central" in output
        assert "# TYPE git_cache_node_sync_duration_seconds gauge" in output

    def test_all_defined_metrics_present(self):
        m = MetricsServer()
        output = m.format()
        assert "git_cache_node_sync_duration_seconds" in output
        assert "git_cache_node_sync_success_total" in output
        assert "git_cache_node_sync_errors_total" in output
        assert "git_cache_node_last_sync_timestamp" in output
        assert "git_cache_node_cache_age_seconds" in output
        assert "git_cache_node_active_slot" in output
        assert "git_cache_node_taint_removed" in output
        assert "git_cache_node_cache_size_bytes" in output

    def test_cache_age_computed_dynamically(self):
        """cache_age_seconds should be computed from last_sync_timestamp."""
        m = MetricsServer()
        m.set("git_cache_node_last_sync_timestamp", 1000.0)
        # Pass a fixed 'now' so the test is deterministic
        output = m.format(now=1060.0)
        # cache_age_seconds should be ~60.0
        assert "git_cache_node_cache_age_seconds 60.0" in output

    def test_cache_age_zero_when_no_sync(self):
        """cache_age_seconds should be 0 when no sync has happened."""
        m = MetricsServer()
        output = m.format(now=5000.0)
        assert "git_cache_node_cache_age_seconds 0.0" in output

    def test_trailing_newline(self):
        m = MetricsServer()
        output = m.format()
        assert output.endswith("\n")


# ============================================================================
# active_slot / staging_slot tests
# ============================================================================


class TestActiveSlot:
    """Tests for active_slot() — symlink resolution."""

    def test_no_symlink_returns_none(self, tmp_path):
        assert active_slot(tmp_path) is None

    def test_symlink_to_slot_a(self, tmp_path):
        slot_a = tmp_path / "git-cache-a"
        slot_a.mkdir()
        (tmp_path / "git-cache").symlink_to(slot_a)
        assert active_slot(tmp_path) == slot_a.resolve()

    def test_symlink_to_slot_b(self, tmp_path):
        slot_b = tmp_path / "git-cache-b"
        slot_b.mkdir()
        (tmp_path / "git-cache").symlink_to(slot_b)
        assert active_slot(tmp_path) == slot_b.resolve()

    def test_symlink_to_invalid_target_returns_none(self, tmp_path):
        (tmp_path / "git-cache-a").mkdir()
        (tmp_path / "git-cache-b").mkdir()
        other = tmp_path / "git-cache-c"
        other.mkdir()
        (tmp_path / "git-cache").symlink_to(other)
        assert active_slot(tmp_path) is None


class TestStagingSlot:
    """Tests for staging_slot() — returns the opposite slot."""

    def test_active_a_returns_b(self, tmp_path):
        slot_a = tmp_path / "git-cache-a"
        slot_a.mkdir()
        (tmp_path / "git-cache-b").mkdir()
        (tmp_path / "git-cache").symlink_to(slot_a)
        result = staging_slot(tmp_path)
        assert result == tmp_path / "git-cache-b"

    def test_active_b_returns_a(self, tmp_path):
        (tmp_path / "git-cache-a").mkdir()
        slot_b = tmp_path / "git-cache-b"
        slot_b.mkdir()
        (tmp_path / "git-cache").symlink_to(slot_b)
        result = staging_slot(tmp_path)
        assert result == tmp_path / "git-cache-a"

    def test_no_active_returns_a(self, tmp_path):
        """When no symlink exists, staging defaults to git-cache-a."""
        result = staging_slot(tmp_path)
        assert result == tmp_path / "git-cache-a"


# ============================================================================
# set_current tests
# ============================================================================


class TestSetCurrent:
    """Tests for set_current() — atomic symlink swap."""

    def test_creates_symlink(self, tmp_path):
        slot = tmp_path / "git-cache-a"
        slot.mkdir()
        set_current(tmp_path, slot)
        link = tmp_path / "git-cache"
        assert link.is_symlink()
        assert link.resolve() == slot.resolve()

    def test_swaps_existing_symlink(self, tmp_path):
        slot_a = tmp_path / "git-cache-a"
        slot_b = tmp_path / "git-cache-b"
        slot_a.mkdir()
        slot_b.mkdir()
        set_current(tmp_path, slot_a)
        assert (tmp_path / "git-cache").resolve() == slot_a.resolve()
        set_current(tmp_path, slot_b)
        assert (tmp_path / "git-cache").resolve() == slot_b.resolve()

    def test_no_leftover_tmp_link(self, tmp_path):
        slot = tmp_path / "git-cache-a"
        slot.mkdir()
        set_current(tmp_path, slot)
        assert not (tmp_path / "git-cache.tmp").exists()


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
# wait_for_central tests
# ============================================================================


class TestWaitForCentral:
    """Tests for wait_for_central() — TCP readiness check."""

    @patch("daemonset_lib.socket.create_connection")
    @patch("daemonset_lib.time.sleep")
    def test_immediate_success(self, mock_sleep, mock_conn):
        """If the first connection succeeds, return immediately."""
        mock_sock = MagicMock()
        mock_conn.return_value = mock_sock
        elapsed = wait_for_central("localhost", 873, timeout=30)
        mock_conn.assert_called_once_with(("localhost", 873), timeout=5)
        mock_sock.close.assert_called_once()
        mock_sleep.assert_not_called()
        assert elapsed >= 0

    @patch("daemonset_lib.socket.create_connection")
    @patch("daemonset_lib.time.sleep")
    def test_connection_refused_then_success(self, mock_sleep, mock_conn):
        """Fail once with ConnectionRefusedError, then succeed."""
        mock_sock = MagicMock()
        mock_conn.side_effect = [ConnectionRefusedError(), mock_sock]
        wait_for_central("localhost", 873, timeout=600)
        assert mock_conn.call_count == 2
        mock_sock.close.assert_called_once()

    @patch("daemonset_lib.socket.create_connection")
    @patch("daemonset_lib.time.sleep")
    @patch("daemonset_lib.time.monotonic")
    def test_timeout_returns_elapsed(self, mock_monotonic, mock_sleep, mock_conn):
        """If connection never succeeds within timeout, return elapsed time."""
        # Calls: t0=0, deadline=t0+600, while-check=0 (<600, enter loop),
        # connect fails, sleep, while-check=700 (>=600, exit loop),
        # return-call=700 (700-t0=700)
        mock_monotonic.side_effect = [0, 0, 700, 700, 700]
        mock_conn.side_effect = ConnectionRefusedError()
        elapsed = wait_for_central("localhost", 873, timeout=600)
        assert elapsed == 700


# ============================================================================
# migrate_old_cache tests
# ============================================================================


class TestMigrateOldCache:
    """Tests for migrate_old_cache() — old layout migration."""

    def test_already_migrated_noop(self, tmp_path):
        """If git-cache is already a symlink, do nothing."""
        slot_a = tmp_path / "git-cache-a"
        slot_a.mkdir()
        (tmp_path / "git-cache").symlink_to(slot_a)
        result = migrate_old_cache(tmp_path)
        assert result is False

    def test_no_old_cache_noop(self, tmp_path):
        """If git-cache doesn't exist, do nothing."""
        result = migrate_old_cache(tmp_path)
        assert result is False

    def test_migrates_old_directory(self, tmp_path):
        """Old plain directory should be migrated to slot-a."""
        old_cache = tmp_path / "git-cache"
        old_cache.mkdir()
        # Create some repo content in the old cache
        (old_cache / "pytorch").mkdir()
        (old_cache / "pytorch" / "data.pack").write_bytes(b"packdata")

        result = migrate_old_cache(tmp_path)

        assert result is True
        # Old dir should be replaced by a symlink
        assert (tmp_path / "git-cache").is_symlink()
        # Content should be in slot-a
        slot_a = tmp_path / "git-cache-a"
        assert (slot_a / "pytorch" / "data.pack").read_bytes() == b"packdata"

    def test_migrated_cache_is_usable(self, tmp_path):
        """After migration, active_slot should resolve to slot-a."""
        old_cache = tmp_path / "git-cache"
        old_cache.mkdir()
        (old_cache / "repo.git").mkdir()

        migrate_old_cache(tmp_path)

        current = active_slot(tmp_path)
        slot_a = tmp_path / "git-cache-a"
        assert current == slot_a.resolve()


# ============================================================================
# Drift detection tests
# ============================================================================

# Path from test file to ConfigMap YAML (scripts/python/ -> ../../)
_CONFIGMAP_PATH = Path(__file__).resolve().parent.parent.parent / "daemonset-configmap.yaml"
_LIB_PATH = Path(__file__).resolve().parent / "daemonset_lib.py"

# Parameters that daemonset_lib intentionally changes for testability.
# The ConfigMap script uses module globals (MNT, CACHE_LINK, etc.)
# while the lib passes these explicitly so functions can be tested.
_INTENTIONAL_EXTRA_PARAMS = {
    "active_slot": {"mnt"},
    "staging_slot": {"mnt"},
    "set_current": {"mnt"},
    "migrate_old_cache": {"mnt"},
    "wait_for_central": {"host", "port"},
}

# The lib's MetricsServer.format() accepts ``now`` for testability;
# the ConfigMap version uses time.time() internally.
_INTENTIONAL_EXTRA_METHOD_PARAMS = {
    "MetricsServer": {
        "format": {"now"},
    },
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


class TestDaemonsetLibDriftDetection:
    """Detect drift between daemonset_lib.py and daemonset-configmap.yaml.

    daemonset_lib.py is a testable extraction of functions from the ConfigMap
    script. If someone edits the ConfigMap script (adds/removes/renames a
    function or changes its arguments) without updating the lib, these tests
    fail.

    Known intentional differences:
    - daemonset_lib parameterizes functions with ``mnt`` instead of using
        the module-level ``MNT`` global.
    - ``wait_for_central`` takes explicit ``host`` and ``port`` args instead
        of using ``CENTRAL_HOST`` / ``CENTRAL_PORT`` globals.
    - ``MetricsServer.format`` accepts a ``now`` parameter for deterministic
        testing; the ConfigMap version calls ``time.time()`` internally.
    - ``migrate_old_cache`` uses ``shutil.copytree`` instead of ``rsync``
        (the lib avoids subprocess calls for testability).
    """

    @staticmethod
    def _load_configmap_script() -> str:
        """Read the ConfigMap YAML and extract the embedded daemonset.py."""
        raw = _CONFIGMAP_PATH.read_text()
        cm = yaml.safe_load(raw)
        return cm["data"]["daemonset.py"]

    @staticmethod
    def _load_lib_source() -> str:
        return _LIB_PATH.read_text()

    def test_configmap_yaml_is_readable(self):
        """Sanity check: the ConfigMap YAML exists and has a daemonset.py key."""
        script = self._load_configmap_script()
        assert len(script) > 100, "daemonset.py script too short — file may be corrupt"

    def test_lib_functions_exist_in_configmap(self):
        """Every top-level function in daemonset_lib.py must have a
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
            f"Functions in daemonset_lib.py not found in ConfigMap script: {missing}. "
            "If you renamed or removed a function in the ConfigMap, update daemonset_lib.py too."
        )

    def test_lib_function_args_match_configmap(self):
        """Argument names of shared functions must match (minus intentional
        testability params like mnt, host, port)."""
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

        assert not mismatches, "Function argument mismatch between daemonset_lib.py and ConfigMap:\n" + "\n".join(
            mismatches
        )

    def test_metrics_server_methods_exist_in_configmap(self):
        """Every method in daemonset_lib.MetricsServer must exist in the
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
            f"MetricsServer methods in daemonset_lib.py not found in ConfigMap: {missing}. "
            "If you renamed or removed a method in the ConfigMap, update daemonset_lib.py too."
        )

    def test_metrics_server_method_args_match(self):
        """MetricsServer method signatures must match between lib and ConfigMap."""
        cm_sigs = _extract_signatures(self._load_configmap_script())
        lib_sigs = _extract_signatures(self._load_lib_source())

        cm_methods = _get_class_methods(cm_sigs, "MetricsServer")
        lib_methods = _get_class_methods(lib_sigs, "MetricsServer")

        intentional = _INTENTIONAL_EXTRA_METHOD_PARAMS.get("MetricsServer", {})

        mismatches = []
        for name, lib_args in lib_methods.items():
            if name not in cm_methods:
                continue  # Caught by test_metrics_server_methods_exist_in_configmap
            cm_args = cm_methods[name]
            extra = intentional.get(name, set())
            lib_args_filtered = [a for a in lib_args if a not in extra]
            if lib_args_filtered != cm_args:
                mismatches.append(f"  MetricsServer.{name}: lib={lib_args_filtered}, configmap={cm_args}")

        assert not mismatches, "MetricsServer method argument mismatch:\n" + "\n".join(mismatches)
