#!/usr/bin/env python3
import argparse
import json
import logging
import shutil
import sys
from pathlib import Path
from typing import Dict, List


def parse_args(argv: List[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Generate Grafana dashboard resources."
    )
    parser.add_argument("--folder", required=True, help="Grafana folder UID")
    return parser.parse_args(argv)


def wrap_dashboard(
    dashboard: object,
    folder_uid: str,
    dashboard_name: str,
) -> Dict[str, object]:
    return {
        "apiVersion": "dashboard.grafana.app/v2",
        "kind": "Dashboard",
        "metadata": {
            "name": f"ci-infra-{folder_uid}-{dashboard_name}",
            "annotations": {
                "grafana.app/folder": folder_uid,
            },
        },
        "spec": dashboard,
    }


def reset_generated_dir(generated_dir: Path) -> None:
    if generated_dir.is_symlink() or generated_dir.is_file():
        generated_dir.unlink()
    elif generated_dir.exists():
        shutil.rmtree(generated_dir)

    generated_dir.mkdir(parents=True, exist_ok=True)


def main(argv: List[str]) -> int:
    logging.basicConfig(
        level=logging.INFO, format="%(levelname)s: %(message)s", stream=sys.stderr
    )

    args = parse_args(argv)

    dashboard_files = sorted(Path(".").glob("*.json"))
    if not dashboard_files:
        logging.error("No dashboard JSON files found.")
        return 1

    generated_dir = Path("generated")
    reset_generated_dir(generated_dir)

    for dashboard_file in dashboard_files:
        with dashboard_file.open(encoding="utf-8") as source:
            dashboard = json.load(source)
        resource = wrap_dashboard(dashboard, args.folder, dashboard_file.stem)
        output_file = generated_dir / dashboard_file.name
        with output_file.open("w", encoding="utf-8") as output:
            json.dump(resource, output, indent=2, ensure_ascii=False)
            output.write("\n")

    logging.info(
        "Generated %s dashboard resource(s) in %s",
        len(dashboard_files),
        generated_dir.resolve(),
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
