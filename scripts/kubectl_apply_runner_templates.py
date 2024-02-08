from collections import ChainMap, defaultdict
from itertools import chain
from typing import List, Dict
import argparse
import subprocess
import yaml


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
        default="actions-runner-system",
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
        "--root-classes",
        help="root classes to use for the template",
        type=str,
        nargs='*',
        required=True,
    )
    parser.add_argument(
        "--rds-state-file",
        help="file to store rds items created/updated so we can clean ones that are not in the config",
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
        "--delete",
        help="delete instead of creating",
        action="store_true",
    )
    parser.add_argument(
        "--additional-values",
        help="additional values to pass to the template",
        type=str,
        nargs='*',
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


def add_to_rds_state(rds_state_file: str, compiled_teplate: str, namespace: str) -> None:
    data = yaml.safe_load(compiled_teplate)

    resource = data['kind']
    name = data['metadata']['name']

    with open(rds_state_file, 'a') as file:
        file.write(f"{namespace},{resource},{name}\n")


def main() -> None:
    options = parse_args()
    additional_values = {
        value.split('=')[0].upper(): value.split('=')[1]
        for value in options.additional_values or []
    }

    additional_values['RUNNERSCOPE'] = {
        'pytorch-org': 'organization: pytorch',
        'pytorch-canary': 'repository: pytorch/pytorch-canary',
        'pytorch-repo': 'repository: pytorch/pytorch',
        # 'enterprise': 'enterprise: meta',
    }[options.runner_scope]

    if len(options.root_classes) != len(options.arc_runner_config_files):
        raise Exception("number of root classes and arc runner config files must match")

    for runner_config in get_merged_arc_runner_config(options.arc_runner_config_files, options.root_classes):
        label = runner_config[options.label_property]

        additional_values['RUNNERARCH'] = [
            l['values'][0]
            for l in runner_config['requirements'] if l['key'] == 'kubernetes.io/arch'
        ][0]
        additional_values['RUNNEROS'] = [
            l['values'][0]
            for l in runner_config['requirements'] if l['key'] == 'kubernetes.io/os'
        ][0]
        additional_values['RUNNERGROUP'] = {
            'canary': '',
            'vanguard': 'group: arc-vanguard',
            'prod': 'group: arc-prod',
        }[additional_values['ENVIRONMENT']]

        if additional_values['ENVIRONMENT'] == 'canary':
            additional_values['ENVRUNNERLABEL'] = label + '.' + additional_values['ENVIRONMENT']
        else:
            additional_values['ENVRUNNERLABEL'] = label

        to_apply = get_template(options.template_name, runner_config | additional_values)

        add_to_rds_state(options.rds_state_file, to_apply, options.namespace)

        if options.delete:
            cmd = ['kubectl', 'delete', f'--namespace={options.namespace}', '-f', '-', '--force']
            print(f"Kubectl delete for {label}: {' '.join(cmd)}")
        else:
            cmd = ['kubectl', 'apply', f'--namespace={options.namespace}', '-f', '-']
            print(f"Kubectl apply for {label}: {' '.join(cmd)}")

        if options.dry_run:
            print("------------------------------------- compiled template -------------------------------------")
            print(to_apply)
        else:
            if subprocess.run(cmd, input=to_apply, capture_output=False, text=True).returncode != 0:
                print("------------------------------------- compiled template -------------------------------------")
                print(to_apply)
                raise Exception(f"Kubectl failed for {label}")


if __name__ == "__main__":
    main()
