#!/usr/bin/env python3

'''
$ pip install requests pyyaml
'''

import argparse
import hashlib
import subprocess
import urllib.request
import yaml


def cli_args():
    parser = argparse.ArgumentParser(description="Just an example",
                                 formatter_class=argparse.ArgumentDefaultsHelpFormatter)
    parser.add_argument("-t", "--terrafile", help="location of terrafile", default="Terrafile")
    parser.add_argument("-m", "--modules", help="location of modules", default="tf-modules")
    return parser.parse_args()


def main():
    args = cli_args()
    versions = ''

    with open(args.terrafile) as f:
        tf_modules = yaml.load(f, Loader=yaml.SafeLoader)

    subprocess.run(['rm', '-rf', args.modules, ])

    for idx, (mod_folder, mod_spec, ) in enumerate(tf_modules.items()):
        print(f'Processing {mod_folder} with spec ({idx + 1} of {len(tf_modules)}):')
        print(f'{yaml.dump(mod_spec, default_flow_style=False)}')
        print("=========================================")

        source = mod_spec['source']

        if 'tag' in mod_spec and mod_spec['tag']:
            tag = mod_spec['tag']
            subprocess.run([
                'git', 'clone', '--depth', '1', '--branch', tag, f'https://github.com/{source}', f'{args.modules}/TMP',
            ])
        else:
            subprocess.run([
                'git', 'clone', f'https://github.com/{source}', f'{args.modules}/TMP',
            ])
            if 'rev' in mod_spec and mod_spec['rev']:
                subprocess.run(
                    ['git', 'checkout', mod_spec['rev'], ],
                    cwd=f'./{args.modules}/TMP'
                )

        proc = subprocess.Popen(
            ['git', 'rev-parse', 'HEAD', ],
            cwd=f'./{args.modules}/TMP',
            stdout=subprocess.PIPE,
            universal_newlines=True
        )
        git_hash = proc.stdout.read().replace('\n', '')
        versions += f'{mod_folder}:\n\tgit-hash: "{git_hash}"\n'
        proc.stdout.close()

        if 'module-root' in mod_spec and mod_spec['module-root']:
            mod_root = mod_spec['module-root']
            subprocess.run([
                'mv', f'{args.modules}/TMP/{mod_root}', f'{args.modules}/{mod_folder}'
            ])
            subprocess.run(['rm', '-rf', f'{args.modules}/TMP'])
        else:
            subprocess.run([
                'mv', f'{args.modules}/TMP', f'{args.modules}/{mod_folder}'
            ])

        assets = []
        if 'asset' in mod_spec and mod_spec['asset']:
            assets = [mod_spec['asset']]
        if 'assets' in mod_spec and mod_spec['assets']:
            assets += mod_spec['assets']

        if 'asset-folders' in mod_spec and mod_spec['asset-folders'] and assets:
            for asset in assets:
                urllib.request.urlretrieve(
                    f'https://github.com/{source}/releases/download/{tag}/{asset}',
                    f'{args.modules}/TMP_ASSET'
                )
                asset_hash = hashlib.md5(open(f'{args.modules}/TMP_ASSET','rb').read()).hexdigest()
                versions += f'\tasset-hash: "{asset_hash}"\n'
                for asset_folder in mod_spec['asset-folders']:
                    subprocess.run([
                        'mkdir', '-p', asset_folder
                    ])
                    subprocess.run([
                        'cp', f'{args.modules}/TMP_ASSET', f'{asset_folder}/{asset}'
                    ])
                subprocess.run([
                    'rm', '-rf', f'{args.modules}/TMP_ASSET'
                ])

        with open(f'{args.modules}/VERSIONS', 'w') as f:
            f.write(versions)


if __name__ == '__main__':
    main()
