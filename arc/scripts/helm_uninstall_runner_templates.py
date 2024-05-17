from typing import List, Dict, Set, Any
import argparse
import subprocess
import json
import os
import itertools

from github import Github, Auth, PaginatedList, Organization
from github.GithubObject import CompletableGithubObject, NotSet, Attribute


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
        "--eks-environment",
        help="environment that the EKS cluster is in - only clean runner groups when deploying to prod",
        type=str,
    )
    parser.add_argument(
        '--dry-run',
        help='do not actually delete anything',
        action='store_true',
    )
    parser.add_argument(
        '--github-app-id',
        help='Github app id to use for the deployment',
        default=os.environ.get('GITHUB_APP_ID'),
        type=str,
        required=False
    )
    parser.add_argument(
        '--github-app-key',
        help='Github app key to use for the deployment',
        default=os.environ.get('GHA_PRIVATE_KEY'),
        type=str,
        required=False
    )
    parser.add_argument(
        '--github-app-installation-id',
        help='Github app installation id to use for the deployment',
        default=os.environ.get('GITHUB_APP_INSTALLATION_ID'),
        type=int,
        required=False
    )
    return parser.parse_args()


class RunnerGroup(CompletableGithubObject):
    """
    This class represents check runs.
    The reference can be found here https://docs.github.com/en/enterprise-cloud@latest/rest/actions/self-hosted-runner-groups?apiVersion=2022-11-28#list-self-hosted-runner-groups-for-an-organization
    """

    def _initAttributes(self) -> None:
        self._id: Attribute[int] = NotSet
        self._name: Attribute[str] = NotSet
        self._visibility: Attribute[str] = NotSet
        self._url: Attribute[str] = NotSet

    @property
    def id(self) -> int:
        self._completeIfNotSet(self._id)
        return self._id.value

    @property
    def name(self) -> str:
        self._completeIfNotSet(self._name)
        return self._name.value

    @property
    def visibility(self) -> str:
        self._completeIfNotSet(self._visibility)
        return self._visibility.value

    @property
    def url(self) -> str:
        self._completeIfNotSet(self._url)
        return self._url.value

    def _useAttributes(self, attributes: dict[str, Any]) -> None:
        attributes["url"] = "https://api.github.com/orgs/pytorch/actions/runner-groups/" + str(attributes["id"])
        if "id" in attributes:
            self._id = self._makeIntAttribute(attributes["id"])
        if "name" in attributes:
            self._name = self._makeStringAttribute(attributes["name"])
        if "visibility" in attributes:
            self._visibility = self._makeStringAttribute(attributes["visibility"])
        if "url" in attributes:
            self._url = self._makeStringAttribute(attributes["url"])


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
                print(f" * deleting {name} in {namespace}")
                print(f"running: {cmd}")
                subprocess.run(cmd, check=True)
                print(" * deleted {name} in {namespace}")


def uninstall_helm_packages(dry_run: bool, helm_pkg_state: Dict[str, Set[str]]) -> None:
    for namespace, should_b_installed in helm_pkg_state.items():
        print(f"finding stale helm packages in {namespace}")
        found = get_deployed_helm_pkg(namespace)
        found_lst = ", ".join(found) if found else "none"
        print(f'Found currently installed helm packages in {namespace}: {found_lst}')
        delete_helm_pkg_not_in_state(dry_run, namespace, found, should_b_installed)

    print("done")


def get_gh_client(opts: argparse.Namespace) -> Github:
    auth = Auth.AppAuth(opts.github_app_id, opts.github_app_key).get_installation_auth(opts.github_app_installation_id)
    gh = Github(auth=auth)
    # this is to share the credentials with child processes (e.g. git)
    os.environ['GITHUB_TOKEN'] = auth.token
    os.environ['GIT_PASS'] = auth.token
    return gh


def gh_get_runner_groups(org: Organization.Organization) -> PaginatedList.PaginatedList[RunnerGroup]:
    return PaginatedList.PaginatedList(
        RunnerGroup,
        org._requester,
        f'{org.url}/actions/runner-groups',
        {
            'status': 'completed',
        },
        list_item='runner_groups',
    )


def delete_runner_group(org: Organization.Organization, rg: RunnerGroup) -> None:
    return org._requester.requestJsonAndCheck(
        "DELETE",
        f'{org.url}/actions/runner-groups/{rg.id}',
    )


def remove_stale_gh_runner_groups(gh: Github, helm_pkg_state: Dict[str, Set[str]], dry_run: bool) -> None:
    runners_names = set(
        x.replace("rssi-", "")
        for x in itertools.chain(*helm_pkg_state.values())
    )

    pytorch_org = gh.get_organization('pytorch')
    runner_groups = {rg.name: rg for rg in gh_get_runner_groups(pytorch_org) if rg.name.startswith('arc-lf-')}
    for runner_group in runner_groups.keys():
        simplified_name = runner_group.replace("arc-lf-", "").replace(".canary", "")
        if simplified_name not in runners_names:
            print(f" * deleting runner group {runner_group}")
            if dry_run:
                print(f"would delete runner group {runner_group}")
            else:
                delete_runner_group(pytorch_org, runner_groups[runner_group])
                print(" * deleted runner group {runner_group}")


def main() -> None:
    options = parse_args()
    helm_pkg_state = get_helm_pkg_state(options.helm_pkg_state_file)
    uninstall_helm_packages(options.dry_run, helm_pkg_state)
    if options.eks_environment == "prod":
        print("Deploying to production, will remove stale runner groups")
        remove_stale_gh_runner_groups(get_gh_client(options), helm_pkg_state, options.dry_run)


if __name__ == "__main__":
    main()
