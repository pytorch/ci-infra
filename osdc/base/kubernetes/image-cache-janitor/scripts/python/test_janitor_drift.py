"""Drift detection tests — verify janitor_lib.py stays in sync with configmap.yaml.

janitor_lib.py is a testable extraction of functions from the ConfigMap
script. If someone edits the ConfigMap script (adds/removes/renames a
function or changes its arguments) without updating the lib, these tests
fail.
"""

import ast
from pathlib import Path

import yaml

# Path from test file to ConfigMap YAML (scripts/python/ -> ../../)
_CONFIGMAP_PATH = Path(__file__).resolve().parent.parent.parent / "configmap.yaml"
_LIB_PATH = Path(__file__).resolve().parent / "janitor_lib.py"


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


class TestJanitorLibDriftDetection:
    """Detect drift between janitor_lib.py and configmap.yaml.

    Known intentional differences:
    - The ConfigMap script has additional runtime functions (run_crictl,
      remove_image, _run_gc_cycle, main) that are NOT in the lib because
      they depend on host access (nsenter/crictl).
    - The ConfigMap MetricsServer has start() for the HTTP server; the lib
      version omits it.
    """

    @staticmethod
    def _load_configmap_script() -> str:
        """Read the ConfigMap YAML and extract the embedded janitor.py."""
        raw = _CONFIGMAP_PATH.read_text()
        cm = yaml.safe_load(raw)
        return cm["data"]["janitor.py"]

    @staticmethod
    def _load_lib_source() -> str:
        return _LIB_PATH.read_text()

    def test_configmap_yaml_is_readable(self):
        """Sanity check: the ConfigMap YAML exists and has a janitor.py key."""
        script = self._load_configmap_script()
        assert len(script) > 100, "janitor.py script too short — file may be corrupt"

    def test_lib_functions_exist_in_configmap(self):
        """Every top-level function in janitor_lib.py must have a
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
            f"Functions in janitor_lib.py not found in ConfigMap script: {missing}. "
            "If you renamed or removed a function in the ConfigMap, update janitor_lib.py too."
        )

    def test_lib_function_args_match_configmap(self):
        """Argument names of shared functions must match."""
        cm_sigs = _extract_signatures(self._load_configmap_script())
        lib_sigs = _extract_signatures(self._load_lib_source())

        cm_funcs = _get_top_level_funcs(cm_sigs)
        lib_funcs = _get_top_level_funcs(lib_sigs)

        mismatches = []
        for name, lib_args in lib_funcs.items():
            if name not in cm_funcs:
                continue
            cm_args = cm_funcs[name]
            if lib_args != cm_args:
                mismatches.append(f"  {name}: lib={lib_args}, configmap={cm_args}")

        assert not mismatches, "Function argument mismatch between janitor_lib.py and ConfigMap:\n" + "\n".join(
            mismatches
        )

    def test_metrics_server_methods_exist_in_configmap(self):
        """Every method in janitor_lib.MetricsServer must exist in the
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
            f"MetricsServer methods in janitor_lib.py not found in ConfigMap: {missing}. "
            "If you renamed or removed a method in the ConfigMap, update janitor_lib.py too."
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
                continue
            cm_args = cm_methods[name]
            if lib_args != cm_args:
                mismatches.append(f"  MetricsServer.{name}: lib={lib_args}, configmap={cm_args}")

        assert not mismatches, "MetricsServer method argument mismatch:\n" + "\n".join(mismatches)

    def test_image_info_dataclass_exists_in_configmap(self):
        """ImageInfo dataclass must exist in both lib and ConfigMap."""
        cm_sigs = _extract_signatures(self._load_configmap_script())
        lib_sigs = _extract_signatures(self._load_lib_source())

        assert "ImageInfo" in cm_sigs, "ImageInfo class missing from ConfigMap script"
        assert "ImageInfo" in lib_sigs, "ImageInfo class missing from janitor_lib.py"
