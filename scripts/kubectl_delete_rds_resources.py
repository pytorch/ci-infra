from typing import List, Dict, Set
import argparse
import subprocess
import json


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="delete resources not stored in the rds-state-file file"
    )
    parser.add_argument(
        "--rds-state-file",
        help="file to store rds items created/updated so we can clean ones that are not in the config",
        type=str,
    )
    parser.add_argument(
        '--dry-run',
        help='do not actually delete anything',
        action='store_true',
    )
    return parser.parse_args()


def get_rds_state(rds_state_file: str) -> Dict[str, Dict[str, List[str]]]:
    with open(rds_state_file, "r") as f:
        rds_state_entries = [l.split(',') for l in f.read().splitlines()]

    rds_state = {}
    for namespace, resource, name in rds_state_entries:
        if namespace not in rds_state:
            rds_state[namespace] = {}
        if resource not in rds_state[namespace]:
            rds_state[namespace][resource] = []
        rds_state[namespace][resource].append(name)

    return rds_state


def get_deployed_rds(resource: str, namespace: str) -> List[str]:
    result = subprocess.run(
        ["kubectl", "get", resource, "--namespace", namespace, "-o", "json" ],
        check=True,
        capture_output=True,
    )
    return [
        item['metadata']['name']
        for item in json.loads(result.stdout.decode('utf-8'))['items']
    ]


def delete_rds_not_in_state(dry_run: bool, resource: str, namespace: str, found: List[str], names: Set[str]) -> None:
    for name in found:
        if name not in names:
            cmd = ["kubectl", "delete", resource, name, "--namespace", namespace, "--grace-period=90", "--ignore-not-found", "--timeout=7m"]
            if dry_run:
                print(f"would delete {resource} {name} in {namespace}: {cmd}")
            else:
                print(f"deleting {resource} {name} in {namespace}")
                subprocess.run(cmd, check=True)


def main() -> None:
    options = parse_args()
    rds_state = get_rds_state(options.rds_state_file)

    for namespace, resources in rds_state.items():

        # NodePool must be deleted before EC2NodeClass
        if "EC2NodeClass" in resources and "NodePool" in resources:
            np = resources["NodePool"]
            del resources["NodePool"]
            nc = resources["EC2NodeClass"]
            del resources["EC2NodeClass"]
            resources_items = list(resources.items())
            resources_items.append(("NodePool", np))
            resources_items.append(("EC2NodeClass", nc))
        else:
            resources_items = resources.items()

        for resource, names in resources_items:
            print(f"finding stale {resource}s in {namespace}")
            names = set(names)
            found = get_deployed_rds(resource, namespace)
            delete_rds_not_in_state(options.dry_run, resource, namespace, found, names)

    print("done")


if __name__ == "__main__":
    main()
