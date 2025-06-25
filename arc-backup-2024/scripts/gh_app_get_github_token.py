import argparse
import os

from github import Auth, Github


def parse_args():
    parser = argparse.ArgumentParser(description='Get a GitHub token for an app')
    parser.add_argument(
        '-i',
        '--app-id',
        help='Github app id to use for the deployment',
        default=os.environ.get('GITHUB_APP_ID', '').strip() or None,
        type=str,
        required=False
    )
    parser.add_argument(
        '-k',
        '--app-key',
        help='Github app key to use for the deployment',
        default=os.environ.get('GHA_PRIVATE_KEY_DEPLOY', '').strip() or None,
        type=str,
        required=False
    )
    parser.add_argument(
        '-l',
        '--installation-id',
        help='Github app installation id to use for the deployment',
        default=os.environ.get('GITHUB_APP_INSTALLATION_ID', '').strip() or None,
        type=int,
        required=False
    )
    return parser.parse_args()


def get_github_token(github_app_id: str, github_app_installation_id: int, github_app_key: str):
    auth = Auth.AppAuth(github_app_id, github_app_key).get_installation_auth(github_app_installation_id)
    Github(auth=auth)
    return auth.token


def main():
    opts = parse_args()
    print(get_github_token(opts.app_id, opts.installation_id, opts.app_key))


if __name__ == '__main__':
    main()
