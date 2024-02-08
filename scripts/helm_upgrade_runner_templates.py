from collections import defaultdict
from typing import Any
from itertools import chain
from typing import List, Dict
import argparse
import subprocess
import yaml
import os

from github import Github, Auth, PaginatedList, Organization
from github.GithubObject import CompletableGithubObject, NotSet, Attribute


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="compile a YAML and run kubectl with apply to it"
    )
    parser.add_argument(
        "--arc-runner-config-files",
        help="path to the ARC_RUNNER_CONFIG files",
        type=str,
        default=["ARC_RUNNER_CONFIG.yaml"],
        nargs='*',
    )
    parser.add_argument(
        "--namespace",
        help="namespace to apply the config to",
        type=str,
        default="actions-runner",
    )
    parser.add_argument(
        "--template-name",
        help="template to compile",
        type=str,
        required=True,
    )
    parser.add_argument(
        "--label-property",
        help="property to use as label",
        type=str,
        required=True,
    )
    parser.add_argument(
        "--container-mode",
        help="Set the ARC Runner container mode: dind, dind-rootless, kubernetes",
        type=str,
        required=True,
    )
    parser.add_argument(
        "--root-classes",
        help="root classes to use for the template",
        type=str,
        nargs='*',
        required=True,
    )
    parser.add_argument(
        "--helm-pkg-state-file",
        help="file to store helm_pkg items created/updated so we can clean ones that are not in the config",
        type=str,
        required=True,
    )
    parser.add_argument(
        "--runner-scope",
        help="scope for runner",
        type=str,
        choices=["pytorch-canary", "pytorch-org", "pytorch-repo", ],
        required=True,
    )
    parser.add_argument(
        "--additional-values",
        help="additional values to pass to the template",
        type=str,
        nargs='*',
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
    parser.add_argument(
        "--dry-run",
        help="dry run",
        action="store_true",
    )
    return parser.parse_args()


def get_arc_runner_config(arc_runner_config_file: str) -> List[Dict[str, str]]:
    with open(arc_runner_config_file, 'r') as file:
        return yaml.safe_load(file)


def get_merged_arc_runner_config(arc_runner_config_files: List[str], root_classes: List[str]) -> List[Dict[str, str]]:
    loaded_configs: List[List[Dict[str, str]]] = [
        get_arc_runner_config(cfg)[r_class]
        for cfg, r_class in zip(arc_runner_config_files, root_classes)
    ]

    matchin_els: Dict[str, List[Dict[str, str]]] = defaultdict(list)
    for loaded_config in loaded_configs:
        if 'runnerLabel' in loaded_config[0]:
            matchin_els_2: Dict[str, List[Dict[str, str]]] = defaultdict(list)
            for el in loaded_config:
                matchin_els_2[el['nodeType']].append(el)
            for node_type, els2 in matchin_els_2.items():
                els = matchin_els[node_type]
                matchin_els[node_type] = list()
                for el2 in els2:
                    matchin_els[node_type].append(el2)
                    for el in els:
                        matchin_els[node_type][-1].update(el)
        else:
            for el in loaded_config:
                if not len(matchin_els[el['nodeType']]):
                    matchin_els[el['nodeType']].append(el)
                else:
                    for ell in matchin_els[el['nodeType']]:
                        ell.update(el)

    return list(chain.from_iterable(matchin_els.values()))


def get_template(template_path: str, values: Dict[str, str]) -> str:
    with open(template_path, 'r') as file:
        template = file.read()
    for key, value in values.items():
        template = template.replace(f'$({key.upper()})', str(value))
    return template


def add_to_helm_pkg_state(helm_pkg_state_file: str, install_name: str, namespace: str) -> None:
    with open(helm_pkg_state_file, 'a') as file:
        file.write(f"{namespace},{install_name}\n")


def get_gh_client(opts: argparse.Namespace) -> Github:
    auth = Auth.AppAuth(opts.github_app_id, opts.github_app_key).get_installation_auth(opts.github_app_installation_id)
    gh = Github(auth=auth)
    # this is to share the credentials with child processes (e.g. git)
    os.environ['GITHUB_TOKEN'] = auth.token
    os.environ['GIT_PASS'] = auth.token
    return gh


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


def gh_create_runner_group(org: Organization.Organization, name: str, visibility: str, selected_repository_ids: List[int] = []) -> RunnerGroup:
    input = {
        'name': name,
        'visibility': visibility,
        'allows_public_repositories': bool(visibility == 'all' or 65600975 in selected_repository_ids),
    }
    if selected_repository_ids:
        input['selected_repository_ids'] = selected_repository_ids

    return org._requester.requestJsonAndCheck(
        "POST",
        f'{org.url}/actions/runner-groups',
        input=input,
    )


def main() -> None:
    options = parse_args()

    gh = get_gh_client(options)
    pytorch_org = gh.get_organization('pytorch')
    runner_groups = {rg.name: rg for rg in gh_get_runner_groups(pytorch_org)}
    container_mode = options.container_mode

    additional_values = {
        value.split('=')[0].upper(): value.split('=')[1]
        for value in options.additional_values or []
    }

    additional_values['RUNNERSCOPE'] = {
        'pytorch-org': 'https://github.com/pytorch',
        'pytorch-canary': 'https://github.com/pytorch',
        # 'pytorch-canary': 'https://github.com/pytorch/pytorch-canary',
        'pytorch-repo': 'https://github.com/pytorch',
        # 'pytorch-repo': 'https://github.com/pytorch/pytorch',
    }[options.runner_scope]

    if len(options.root_classes) != len(options.arc_runner_config_files):
        raise Exception("number of root classes and arc runner config files must match")

    for runner_config in get_merged_arc_runner_config(options.arc_runner_config_files, options.root_classes):
        label = runner_config[options.label_property]

        # Adjust label for multiple container mode runner scale set deployments
        label = f'{label}-{container_mode}'
        runner_config[options.label_property] = label

        additional_values['RUNNERARCH'] = [
            l['values'][0]
            for l in runner_config['requirements'] if l['key'] == 'kubernetes.io/arch'
        ][0]
        additional_values['RUNNEROS'] = [
            l['values'][0]
            for l in runner_config['requirements'] if l['key'] == 'kubernetes.io/os'
        ][0]

        additional_values['ENVRUNNERLABEL'] = label
        if additional_values['ENVIRONMENT'] == 'canary':
            additional_values['ENVRUNNERLABEL'] += '.canary'
        l = additional_values['ENVRUNNERLABEL']
        additional_values['RUNNERGROUP'] = f'arc-lf-{l}'

        install_name = f'rssi-{label}'
        to_apply = get_template(options.template_name, runner_config | additional_values)
        add_to_helm_pkg_state(options.helm_pkg_state_file, install_name, options.namespace)

        cmd = [
            'helm', 'upgrade', '--install', install_name, '--wait',
            '--namespace', options.namespace, '--create-namespace',
            'oci://ghcr.io/actions/actions-runner-controller-charts/gha-runner-scale-set', '--create-namespace',
            '--values',
            '-',
        ]
        print(f"helm upgrade for rssi-{label}: {' '.join(cmd)}")

        if options.dry_run:
            print("------------------------------------- compiled template -------------------------------------")
            print(to_apply)
        else:
            runner_scope = {
                'pytorch-org': 'all',
                'pytorch-canary': 'selected',
                'pytorch-repo': 'selected',
            }[options.runner_scope]
            selected_repository_ids = {
                'pytorch-org': [],
                'pytorch-canary': [398371105],
                'pytorch-repo': [65600975],
            }[options.runner_scope]
            if additional_values['RUNNERGROUP'] not in runner_groups:
                gh_create_runner_group(pytorch_org, additional_values['RUNNERGROUP'], runner_scope, selected_repository_ids)
            if subprocess.run(cmd, input=to_apply, capture_output=False, text=True).returncode != 0:
                print("------------------------------------- compiled template -------------------------------------")
                print(to_apply)
                raise Exception(f"Kubectl failed for {label}")


if __name__ == "__main__":
    main()
