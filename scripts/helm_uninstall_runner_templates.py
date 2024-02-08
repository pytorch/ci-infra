from typing import List, Dict, Set
import argparse
import subprocess
import json


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="delete resources not stored in the helm_pkg-state-file file"
    )
    parser.add_argument(
        "--helm-pkg-state-file",
        help="file to store helm_pkg items created/updated so we can clean ones that are not in the config",
        type=str,
    )
    parser.add_argument(
        '--dry-run',
        help='do not actually delete anything',
        action='store_true',
    )
    return parser.parse_args()


def get_helm_pkg_state(helm_pkg_state_file: str) -> Dict[str, Set[str]]:
    with open(helm_pkg_state_file, "r") as f:
        helm_pkg_state_entries = [l.split(',') for l in f.read().splitlines()]

    helm_pkg_state = {}
    for namespace, install_name in helm_pkg_state_entries:
        if namespace not in helm_pkg_state:
            helm_pkg_state[namespace] = set()
        helm_pkg_state[namespace].add(install_name)

    return helm_pkg_state


def get_deployed_helm_pkg(namespace: str) -> List[str]:
    result = subprocess.run(
        ["helm", "list", "--namespace", namespace, "--output", "json" ],
        check=True,
        capture_output=True,
    )
    return [
        item['name']
        for item in json.loads(result.stdout.decode('utf-8'))
    ]


def delete_helm_pkg_not_in_state(dry_run: bool, namespace: str, found: List[str], should_b_installed: Set[str]) -> None:
    for name in found:
        if name not in should_b_installed:
            cmd = ["helm", "uninstall", name, "--namespace", namespace, ]
            if dry_run:
                print(f"would delete {name} in {namespace}: {cmd}")
            else:
                print(f"deleting {name} in {namespace}")
                subprocess.run(cmd, check=True)


def main() -> None:
    options = parse_args()
    helm_pkg_state = get_helm_pkg_state(options.helm_pkg_state_file)

    for namespace, should_b_installed in helm_pkg_state.items():
        print(f"finding stale helm packages in {namespace}")
        found = get_deployed_helm_pkg(namespace)
        found_lst = ", ".join(found) if found else "none"
        print(f'Found currently installed helm packages in {namespace}: {found_lst}')
        delete_helm_pkg_not_in_state(options.dry_run, namespace, found, should_b_installed)

    print("done")


if __name__ == "__main__":
    main()
