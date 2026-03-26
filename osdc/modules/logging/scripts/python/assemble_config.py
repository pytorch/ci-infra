#!/usr/bin/env python3
# /// script
# requires-python = ">=3.12"
# dependencies = ["pyyaml>=6.0"]
# ///
"""Assemble Alloy logging pipeline config from base + per-module pipelines.

Reads a base Alloy pipeline file containing a // MODULE_PIPELINES marker,
discovers per-module pipeline.alloy files (consumer override first, then upstream),
inserts them at the marker, and outputs a ConfigMap YAML.

Usage:
    uv run assemble_config.py \
        --base-pipeline base.alloy \
        --modules-dir /path/to/consumer/modules \
        --upstream-modules-dir /path/to/upstream/modules \
        --cluster arc-staging \
        --clusters-yaml /path/to/clusters.yaml \
        --namespace logging \
        --output generated/alloy-config.yaml
"""

import argparse
import sys
from pathlib import Path

import yaml

MODULE_PIPELINES_MARKER = "// MODULE_PIPELINES"


def load_cluster_modules(clusters_yaml_path: str | Path, cluster_id: str) -> list[str]:
    """Read clusters.yaml and return the module list for the given cluster."""
    with open(clusters_yaml_path) as f:
        cfg = yaml.safe_load(f)
    clusters = cfg.get("clusters", {})
    if cluster_id not in clusters:
        print(f"Error: unknown cluster '{cluster_id}'. Known: {', '.join(clusters.keys())}", file=sys.stderr)
        sys.exit(1)
    return clusters[cluster_id].get("modules", [])


def discover_pipeline(module_name: str, modules_dir: str | Path, upstream_modules_dir: str | Path) -> str | None:
    """Return pipeline.alloy content for a module, checking consumer dir first then upstream.

    Returns None if no pipeline file exists in either location.
    An empty/whitespace-only consumer file acts as an explicit opt-out — the upstream
    pipeline is NOT checked as a fallback. This lets consumers suppress a module's
    logging pipeline by placing an empty ``pipeline.alloy`` in their modules dir.
    """
    consumer_path = Path(modules_dir) / module_name / "logging" / "pipeline.alloy"
    upstream_path = Path(upstream_modules_dir) / module_name / "logging" / "pipeline.alloy"

    for path in (consumer_path, upstream_path):
        if path.is_file():
            content = path.read_text()
            if content.strip():
                return content
            # Empty/whitespace-only file — treat as absent
            return None

    return None


def assemble_config(base_content: str, module_pipelines: dict[str, str]) -> str:
    """Insert module pipeline content at the MODULE_PIPELINES marker in the base config.

    Each module's content is wrapped with comment delimiters and indented by 4 spaces
    to match loki.process block indentation. The marker line is removed.

    Raises SystemExit if module_pipelines is non-empty but the marker is not found in
    base_content — this indicates a broken base.alloy that would silently drop all
    module pipelines.
    """
    lines = base_content.splitlines(keepends=True)
    result: list[str] = []
    marker_found = False

    for line in lines:
        if MODULE_PIPELINES_MARKER in line:
            marker_found = True
            # Replace marker with all module pipelines
            for mod_name, content in module_pipelines.items():
                result.append(f"    // --- module: {mod_name} ---\n")
                for content_line in content.splitlines():
                    if content_line.strip():
                        result.append(f"    {content_line}\n")
                    else:
                        result.append("\n")
                result.append(f"    // --- end module: {mod_name} ---\n")
        else:
            result.append(line)

    if not marker_found and module_pipelines:
        print(
            f"Error: base pipeline is missing the '{MODULE_PIPELINES_MARKER}' marker "
            f"but {len(module_pipelines)} module pipeline(s) were discovered "
            f"({', '.join(module_pipelines)}). Module pipelines would be silently dropped.",
            file=sys.stderr,
        )
        sys.exit(1)

    return "".join(result)


def render_configmap(assembled_config: str, namespace: str, name: str = "alloy-logging-config") -> str:
    """Render a Kubernetes ConfigMap YAML containing the assembled Alloy config."""
    configmap = {
        "apiVersion": "v1",
        "kind": "ConfigMap",
        "metadata": {
            "name": name,
            "namespace": namespace,
        },
        "data": {
            "config.alloy": assembled_config,
        },
    }
    return yaml.dump(configmap, default_flow_style=False, sort_keys=False)


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Assemble Alloy logging pipeline config from base + module pipelines.")
    parser.add_argument("--base-pipeline", required=True, help="Path to base.alloy pipeline file")
    parser.add_argument("--modules-dir", required=True, help="Consumer modules directory (OSDC_ROOT/modules)")
    parser.add_argument(
        "--upstream-modules-dir", required=True, help="Upstream modules directory (OSDC_UPSTREAM/modules)"
    )
    parser.add_argument("--cluster", required=True, help="Cluster ID from clusters.yaml")
    parser.add_argument("--clusters-yaml", required=True, help="Path to clusters.yaml")
    parser.add_argument("--namespace", required=True, help="Namespace for the ConfigMap")
    parser.add_argument("--output", required=True, help="Output file path")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> None:
    args = parse_args(argv)

    # Read base pipeline
    base_path = Path(args.base_pipeline)
    if not base_path.is_file():
        print(f"Error: base pipeline not found: {base_path}", file=sys.stderr)
        sys.exit(1)
    base_content = base_path.read_text()

    # Get cluster's enabled modules
    modules = load_cluster_modules(args.clusters_yaml, args.cluster)

    # Discover pipeline files for each module
    # Skip 'logging' itself — the logging module owns the base pipeline, not a
    # per-module pipeline.  Without this filter the assembler would look for
    # modules/logging/logging/pipeline.alloy which doesn't exist (harmless but
    # confusing) or, worse, could pick up an accidental file at that path.
    module_pipelines: dict[str, str] = {}
    for mod_name in modules:
        if mod_name == "logging":
            continue
        content = discover_pipeline(mod_name, args.modules_dir, args.upstream_modules_dir)
        if content is not None:
            module_pipelines[mod_name] = content

    # Assemble the config
    assembled = assemble_config(base_content, module_pipelines)

    # Render ConfigMap
    configmap_yaml = render_configmap(assembled, args.namespace)

    # Write output
    output_path = Path(args.output)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(configmap_yaml)
    print(f"Wrote {output_path}")


if __name__ == "__main__":
    main()
