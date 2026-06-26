#!/usr/bin/env python3
# /// script
# requires-python = ">=3.12"
# dependencies = ["pyyaml>=6.0"]
# ///
"""Wrap the Alloy base pipeline in a Kubernetes ConfigMap YAML.

Usage:
    uv run assemble_config.py \
        --base-pipeline base.alloy \
        --namespace logging \
        --output generated/alloy-config.yaml
"""

import argparse
import sys
from pathlib import Path

import yaml


def render_configmap(assembled_config: str, namespace: str, name: str = "alloy-logging-config") -> str:
    """Render a Kubernetes ConfigMap YAML containing the Alloy config."""
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
    parser = argparse.ArgumentParser(description="Wrap the Alloy base pipeline in a ConfigMap YAML.")
    parser.add_argument("--base-pipeline", required=True, help="Path to base.alloy pipeline file")
    parser.add_argument("--namespace", required=True, help="Namespace for the ConfigMap")
    parser.add_argument("--output", required=True, help="Output file path")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> None:
    args = parse_args(argv)

    base_path = Path(args.base_pipeline)
    if not base_path.is_file():
        print(f"Error: base pipeline not found: {base_path}", file=sys.stderr)
        sys.exit(1)
    base_content = base_path.read_text()

    configmap_yaml = render_configmap(base_content, args.namespace)

    output_path = Path(args.output)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(configmap_yaml)
    print(f"Wrote {output_path}")


if __name__ == "__main__":
    main()
