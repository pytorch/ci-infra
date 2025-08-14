#!/usr/bin/env python3

import argparse
import datetime
import logging
import os
import re
import sys
import time

from git import Repo
from github import Auth, Github, PaginatedList, CheckRun, Repository, PullRequest
from typing import List


RELEASE_BRANCH = 'prod_live'

PROD_RELEASE_LABEL = 'prod-release'
CANARY_LABEL = 'deploy-to-canary'
VANGUARD_LABEL = 'deploy-to-vanguard'
FAST_RELEASE_FIREFIGHT_LABEL = 'fast-release-firefight'

PROCEED_TO_VANGUARD_COMMENT = 'PROCEED_TO_VANGUARD'
PROCEED_TO_PROD_COMMENT = 'PROCEED_TO_PRODUCTION'
SHUTDOWN_VANGUARD_COMMENT = 'ABORT_DEPLOYMENT_SHUTDOWN_VANGUARD'
CLEANUP_DEPLOYMENT_COMMENT = 'CLEANUP_DEPLOYMENT'
ROLLBACK_PRODUCTION_COMMENT = 'TBD'


def nice_bool_option(s: str) -> bool:
    if s.lower().strip() in ['true', '1', 't', 'y', 'yes']:
        return True
    elif s.lower().strip() in ['false', '0', 'f', 'n', 'no', '']:
        return False
    else:
        raise ValueError(f'Invalid boolean option: {s}')


def gh_get_check_runs(repo: Repository.Repository, ref: str) -> PaginatedList.PaginatedList[CheckRun.CheckRun]:
    '''
    :calls: `GET /repos/{owner}/{repo}/commits/{ref}/check-runs
    '''
    return PaginatedList.PaginatedList(
        CheckRun.CheckRun,
        repo._requester,
        f'{repo.url}/commits/{ref}/check-runs',
        {
            'status': 'completed',
        },
        list_item='check_runs',
    )


def get_gh_client(opts: argparse.Namespace) -> Github:
    if opts.github_app_id and opts.github_app_key and opts.github_app_installation_id:
        logging.debug(f'Using github app private key credentials')
        auth = Auth.AppAuth(opts.github_app_id, opts.github_app_key).get_installation_auth(opts.github_app_installation_id)
        gh = Github(auth=auth)
        # this is to share the credentials with child processes (e.g. git)
        os.environ['GITHUB_TOKEN'] = auth.token
        os.environ['GIT_PASS'] = auth.token
        os.environ['GIT_USER'] = opts.bot_name
        return gh

    elif opts.github_token:
        logging.debug('Using github token credentials')
        auth = Auth.Token(opts.github_token)
        return Github(auth=auth)

    else:
        raise RuntimeError('No github token or app credentials provided')


def get_pr(repo: Repository.Repository, release_branch: str) -> PullRequest.PullRequest:
    pulls = list(repo.get_pulls(state='open', base=release_branch))
    if len(pulls) > 1:
        logging.error(f'Found more than one open pull request for {release_branch}: {", ".join(p.html_url for p in pulls)} by {", ".join(p.user.login for p in pulls)}')
        logging.error(f'Please finish the current multiple deployments or close the pull requests before continuing')
        raise RuntimeError(f'Found multiple open pull request for {release_branch}')
    elif len(pulls) == 0:
        logging.error(f'No open pull request for {release_branch}')
        raise RuntimeError(f'No open pull request for {release_branch}')

    logging.debug(f'Found open pull request for {release_branch}: {pulls[0].html_url} by {pulls[0].user.login}')
    return pulls[0]


def setup_local_git_auth(opts: argparse.Namespace, git_repo: Repo) -> None:
    # This convoluted authentication is to avoid setting the credentials in the git config, or URL that could expose to leaks
    git_repo.git.config('user.name', opts.bot_name)
    git_repo.git.config('user.email', f'{opts.github_app_installation_id}+{opts.bot_name}[bot]@users.noreply.github.com')

    try:
        git_repo.git.config('--unset-all', 'credential.helper')
    except:
        logging.debug('No credential helper set')
    try:
        git_repo.git.config('--unset-all', 'http.https://github.com/.extraheader')
    except:
        logging.debug('No extraheader for http.https://github.com/ set')
    try:
        git_repo.git.config('--unset-all', 'http.extraheader')
    except:
        logging.debug('No extraheader set')

    git_repo.git.config('credential.helper', '!f() { echo "username=${GIT_USER}\npassword=${GIT_PASS}"; }; f')


def create_git_branch(opts: argparse.Namespace, git_repo: Repo, branch_name: str) -> None:
    git_repo.create_head(branch_name)
    git_repo.remotes.origin.push(branch_name)
    logging.info(f'Created branch {branch_name} with ref {git_repo.head.commit.hexsha} and pushed to origin')


def create_git_tag(opts: argparse.Namespace, git_repo: Repo, tag_name: str) -> None:
    git_repo.create_tag(tag_name)
    git_repo.remotes.origin.push(tag_name)
    logging.info(f'Created tag {tag_name} with ref {git_repo.head.commit.hexsha} and pushed to origin')


def get_pr_user(pull: PullRequest) -> str:
    user_name_lst = re.findall(r'\(([a-zA-Z0-9_]+) [0-9]+\)', pull.body)
    if len(user_name_lst) != 1:
        logging.error(f'Found more than or less than one user in the PR body: {pull.html_url}')
        raise RuntimeError('Found more than or less than one user in the PR body')
    return user_name_lst[0]


# Commands
def open_release_pr(gh: Github, opts: argparse.Namespace) -> None:
    'open-rel-pr'
    repo = gh.get_repo(opts.repo)

    for pull in repo.get_pulls(state='open', base=opts.release_branch):
        logging.error(f'Found open pull request for {opts.release_branch}: {pull.html_url} by {pull.user.login}')
        logging.error(f'Please finish the current deployment or close the pull request before opening a new one')
        raise RuntimeError(f'Found open pull request for {opts.release_branch}')

    usr = gh.get_user_by_id(opts.github_actor_id)

    right_now = datetime.datetime.now()
    gh_right_now = right_now.strftime('%Y%m%d%H%M%S')
    human_right_now = right_now.strftime('%Y-%m-%dT%H:%M:%S')

    tag_name = f'arc-prod-release-{gh_right_now}'
    branch_name = f'prod-release/{gh_right_now}'

    git_repo = Repo('.')

    setup_local_git_auth(opts, git_repo)
    create_git_branch(opts, git_repo, branch_name)
    create_git_tag(opts, git_repo, tag_name)

    body = f'''Prod release {human_right_now} opened by {usr.name} ({usr.login} {opts.github_actor_id})

Head commit: {git_repo.head.commit.hexsha}
Deployment tag: {tag_name}
Branch name: {branch_name}

I am a bot ({opts.bot_name}), and this is an automated response. Please don't manipulate the
labels in this PR or I might get lost as I am not very smart. The labels are used to track the state of the deployment
and trigger the relevant jobs. As the steps proceed, I'll be adding comments to this PR to let you know what to do next.

So lets keep our conversation in the comments here, and I'll take care of the rest.

more details can be found in the relevant section for GHA runbook: https://fburl.com/pytorch-arc-deployment-docs'''

    pr = repo.create_pull(
        title=f'Prod release {human_right_now} by {usr.name}',
        body=body,
        head=branch_name,
        base=opts.release_branch
    )

    pr.add_to_assignees(usr)
    logging.info(f'Created pull request for {opts.release_branch}: {pr.html_url}')

    labels: List[str] = [PROD_RELEASE_LABEL, ]
    if opts.fast_release_firefight:
        pr.create_issue_comment(PROCEED_TO_VANGUARD_COMMENT)
        labels += [FAST_RELEASE_FIREFIGHT_LABEL, VANGUARD_LABEL, ]
    else:
        labels += [CANARY_LABEL, ]

    pr.add_to_labels(*labels)
    logging.debug(f'Added labels to pull request: {", ".join(labels)}')

    pr.create_issue_comment(f'I triggered the canary release for you, please wait for it to finish. I\'ll let you know when it\'s done.')


def add_comment_to_pr(gh: Github, opts: argparse.Namespace) -> None:
    'add-comment-to-pr'
    repo = gh.get_repo(opts.repo)
    pull = get_pr(repo, opts.release_branch)

    pull.create_issue_comment(opts.comment)
    logging.info(f'Added comment to pr: {pull.html_url}')


def wait_check_deployment(gh: Github, opts: argparse.Namespace) -> None:
    'wait-check-deployment'
    repo = gh.get_repo(opts.repo)
    pull = get_pr(repo, opts.release_branch)

    if opts.ignore_if_label.strip():
        for label in pull.get_labels():
            if label.name.strip().lower() == opts.ignore_if_label.strip().lower():
                logging.info(f'Found label {opts.ignore_if_label} on PR {pull.html_url}, ignoring this check')
                return

    commit = list(pull.get_commits())[-1]
    logging.debug(f'Latest commit on pr: {commit.sha}')
    logging.info(f'Checking for successful release ({opts.release_action_name}) on main...')

    found_success = False
    time_limit = datetime.datetime.now() + datetime.timedelta(minutes=15)
    while not found_success and time_limit > datetime.datetime.now():
        for wf in gh_get_check_runs(repo, commit.sha):
            if wf.name == opts.release_action_name and wf.conclusion == 'success':
                found_success = True
                break

        if not found_success:
            time.sleep(30)

    if not found_success:
        logging.error(f'No successful canary release ({opts.release_action_name}) found for {opts.release_branch}')
        raise RuntimeError('No successful canary release found')

    logging.info(f'Found successful canary release ({opts.release_action_name}) on for {opts.release_branch} ({commit.sha})')

    if opts.comment_to_add:
        pull.create_issue_comment(opts.comment_to_add)
        logging.info(f'Added comment to pr: {pull.html_url}')


def wait_check_user_comment(gh: Github, opts: argparse.Namespace) -> None:
    'wait-check-user-comment'

    assert(opts.comment is not None and opts.comment != '')

    repo = gh.get_repo(opts.repo)
    pull = get_pr(repo, opts.release_branch)

    found_coment = False
    time_limit = datetime.datetime.now() + datetime.timedelta(minutes=15)
    while not found_coment and time_limit > datetime.datetime.now():
        for comment in pull.get_issue_comments():
            if comment.user.type.lower() == 'bot':
                continue
            if comment.body.strip() != opts.comment.strip():
                continue
            if comment.get_reactions().totalCount == 0:
                continue
            found_coment = True
            break

        if not found_coment:
            time.sleep(30)

    if not found_coment:
        pull.create_issue_comment(f'Comment [{opts.comment}] not found on PR {pull.html_url}, so I can\'t proceed')
        logging.error(f'No comment found for {opts.comment} on PR {pull.html_url}')
        raise RuntimeError('No comment found')


def wait_check_bot_comment(gh: Github, opts: argparse.Namespace) -> None:
    'wait-check-bot-comment'

    assert(opts.comment is not None and opts.comment != '')

    repo = gh.get_repo(opts.repo)
    pull = get_pr(repo, opts.release_branch)

    for comment in pull.get_issue_comments():
        if comment.body.strip().startswith(opts.comment.strip()) and comment.user.type.lower() == 'bot':
            logging.info(f'Found comment for {opts.comment} on PR {pull.html_url}')
            return

    logging.error(f'No comment found for [{opts.comment}] on PR {pull.html_url}')
    raise RuntimeError('No comment found')


def react_pr_comment(gh: Github, opts: argparse.Namespace) -> None:
    'react-pr-comment'
    repo = gh.get_repo(opts.repo)
    pull = get_pr(repo, opts.release_branch)

    comments_lst = opts.comments.split(',')
    labels_lst = opts.labels.split(',')
    check_remove_labels_lst = opts.check_remove_labels.split(',')
    check_comments_lst = opts.check_comments.split('#')

    if not (len(comments_lst) == len(labels_lst) == len(check_remove_labels_lst) == len(check_comments_lst)):
        logging.error(f'Options "comments" ({len(comments_lst)}), "labels" ({len(labels_lst)}), "check-comments" ({len(check_comments_lst)}) and "check-remove-labels" ({len(check_remove_labels_lst)}) should be the same size!')
        raise RuntimeError('Options "comments", "labels", "check-comments" and "check-remove-labels" should be the same size!')

    check_dict = {}
    for comment, label, check_label, check_comment in zip(comments_lst, labels_lst, check_remove_labels_lst, check_comments_lst):
        check_dict[comment.strip()] = {
            'label': label.strip(),
            'check_label': check_label,
            'check_comment': check_comment.strip(),
        }

    issue_comments = list(pull.get_issue_comments())
    current_labels = set(l.name.lower() for l in pull.get_labels())

    for comment in reversed(issue_comments):
        if comment.user.type.lower() != 'bot' and comment.body.strip() in check_dict and comment.get_reactions().totalCount == 0:

            check = check_dict[comment.body.strip()]
            if check['check_label']:
                if check['check_label'].lower() not in current_labels:
                    comment.create_reaction('-1')
                    pull.create_issue_comment(f'Label {check["check_label"]} not found on PR {pull.html_url}, so I can\'t proceed with the deployment')
                    logging.error(f'Label {check["check_label"]} not found on PR {pull.html_url}')
                    raise RuntimeError(f'Required label not found on PR')

            if check['check_comment']:
                found_coment = False
                for c in issue_comments:
                    if c.body.strip().startswith(check['check_comment'].strip()):
                        found_coment = True
                        break

                if not found_coment:
                    comment.create_reaction('-1')
                    pull.create_issue_comment(f'Comment [{check["check_comment"]}] not found on PR {pull.html_url}, so I can\'t proceed with the deployment')
                    logging.error(f'Comment [{check["check_comment"]}] not found on PR {pull.html_url}')
                    raise RuntimeError(f'Required comment not found on PR')

            pull.remove_from_labels(check['check_label'])
            logging.debug(f'Removed label {check["check_label"]} from PR {pull.html_url}')

            pull.add_to_labels(check['label'])
            logging.debug(f'Added label {check["label"]} to PR {pull.html_url}')

            comment.create_reaction('rocket')
            logging.debug(f'Added reaction to comment: {comment.html_url}')

            return

    logging.error(f'No comment found for {opts.comments} on PR {pull.html_url}')
    raise RuntimeError('No comment found on PR')


def close_pr(gh: Github, opts: argparse.Namespace) -> None:
    'close-pr'
    repo = gh.get_repo(opts.repo)
    pull = get_pr(repo, opts.release_branch)

    pull.edit(state='closed')
    logging.info(f'Closed pull request for {opts.release_branch}: {pull.html_url}')


def check_pr_approved(gh: Github, opts: argparse.Namespace) -> None:
    'wait-check-pr-approved'
    repo = gh.get_repo(opts.repo)
    pull = get_pr(repo, opts.release_branch)

    reviews = list(pull.get_reviews())
    user_name = get_pr_user(pull)

    for review in reviews:
        if review.user.login != user_name and review.state == 'APPROVED':
            pull.create_issue_comment(f'PR approved by {review.user.login}')
            logging.info(f'Found approved PR for {opts.release_branch}: {pull.html_url} by {review.user.login}')
            return

    for review in reviews:
        if review.user.login == user_name and review.state == 'APPROVED':
            pull.create_issue_comment('PR self-approved, this is OK, but make sure to communicate this and avoid rolling out without letting the team know.')
            logging.info(f'Found self-approved PR for {opts.release_branch}: {pull.html_url}')
            return

    pull.create_issue_comment('PR is not approved, please make sure to get approval before merging. It is OK to self-approve, but make sure to communicate this and avoid rolling out without letting the team know.')
    logging.error(f'PR does not have any approval {opts.release_branch}: {pull.html_url}')
    raise RuntimeError('PR does not have any approval')


def merge_pr(gh: Github, opts: argparse.Namespace) -> None:
    'merge-pr'
    repo = gh.get_repo(opts.repo)
    pull = get_pr(repo, opts.release_branch)

    user_name = get_pr_user(pull)

    pull.merge(
        merge_method='merge',
        commit_title=f'Release {opts.release_branch}',
        commit_message=f'Merge for release {opts.release_branch}\nPR: {pull.html_url}\nBy: {user_name}'
    )
    logging.info(f'Merged pull request for {opts.release_branch}: {pull.html_url}')


CMD_MAP = {
    add_comment_to_pr.__doc__: add_comment_to_pr,
    check_pr_approved.__doc__: check_pr_approved,
    close_pr.__doc__: close_pr,
    merge_pr.__doc__: merge_pr,
    open_release_pr.__doc__: open_release_pr,
    react_pr_comment.__doc__: react_pr_comment,
    wait_check_bot_comment.__doc__: wait_check_bot_comment,
    wait_check_deployment.__doc__: wait_check_deployment,
    wait_check_user_comment.__doc__: wait_check_user_comment,
}


# Main
def parse_args() -> argparse.Namespace:
    opts = argparse.ArgumentParser(description='Deployment script for the project')

    opts.add_argument(
        '--github-token',
        help='Github token to use for the deployment',
        default=os.environ.get('GITHUB_TOKEN', '').strip() or None,
        type=str,
        required=False
    )
    opts.add_argument(
        '--github-app-id',
        help='Github app id to use for the deployment',
        default=os.environ.get('GITHUB_APP_ID', '').strip() or None,
        type=str,
        required=False
    )
    opts.add_argument(
        '--github-app-key',
        help='Github app key to use for the deployment',
        default=os.environ.get('GHA_PRIVATE_KEY_DEPLOY', '').strip() or None,
        type=str,
        required=False
    )
    opts.add_argument(
        '--github-app-installation-id',
        help='Github app installation id to use for the deployment',
        default=os.environ.get('GITHUB_APP_INSTALLATION_ID', '').strip() or None,
        type=int,
        required=False
    )
    opts.add_argument(
        '--repo',
        help='Github repo to use for the deployment',
        default=os.environ.get('GITHUB_REPOSITORY', 'meta-pytorch/pytorch-gha-infra').strip() or None,
        type=str,
        required=False
    )
    opts.add_argument(
        '--debug',
        help='Enable debug mode',
        action='store_true'
    )
    opts.add_argument(
        '--github-actor-id',
        help='Github actor id to use for the deployment',
        default=os.environ.get('GITHUB_ACTOR_ID', '').strip() or None,
        type=int,
        required=False
    )
    opts.add_argument(
        '--release-branch',
        help='Release branch to use for the deployment',
        default=RELEASE_BRANCH,
        type=str,
        required=False
    )
    opts.add_argument(
        '--bot-name',
        help='Name of the bot to use for the deployment',
        default='pytorch-arc-pr-deployment-bot',
        type=str,
        required=False
    )

    rel_subparsers = opts.add_subparsers(help='Release actions', dest='release_action')

    open_rel_issue_parser = rel_subparsers.add_parser(
        str(open_release_pr.__doc__),
        help='Opens the release issue'
    )
    open_rel_issue_parser.add_argument(
        '--fast-release-firefight',
        help='Enable fast release firefight mode',
        type=nice_bool_option,
        default=nice_bool_option(os.environ.get('FAST_RELEASE_FIREFIGHT', 'false')),
    )

    wait_check_deployment_parser = rel_subparsers.add_parser(
        str(wait_check_deployment.__doc__),
        help='Waits for the deployment to finish'
    )
    wait_check_deployment_parser.add_argument(
        '--release-action-name',
        help='Name of the action that releases and is a requirement for the release',
        type=str,
        required=True
    )
    wait_check_deployment_parser.add_argument(
        '--comment-to-add',
        help='String to add as a comment to the release issue',
        default='',
        type=str,
        required=False
    )
    wait_check_deployment_parser.add_argument(
        '--ignore-if-label',
        help='Ignore if the PR has this label',
        default='',
        type=str,
        required=False
    )

    wait_check_user_comment_parser = rel_subparsers.add_parser(
        str(wait_check_user_comment.__doc__),
        help='waits for user comment on PR'
    )
    wait_check_user_comment_parser.add_argument(
        '--comment',
        help='String to check as a comment to the release issue',
        type=str,
        required=True,
    )

    wait_check_bot_comment_parser = rel_subparsers.add_parser(
        str(wait_check_bot_comment.__doc__),
        help='waits for bot comment on PR'
    )
    wait_check_bot_comment_parser.add_argument(
        '--comment',
        help='String to check as a comment to the release issue',
        type=str,
        required=True,
    )

    react_pr_comment_parser = rel_subparsers.add_parser(
        str(react_pr_comment.__doc__),
        help='reacts to user comment on PR, add the corresponding label based on comment'
    )
    react_pr_comment_parser.add_argument(
        '--comments',
        help='Coma separated list of comments to react to',
        type=str,
        required=True,
    )
    react_pr_comment_parser.add_argument(
        '--labels',
        help='Coma separated list of labels to add',
        type=str,
        required=True,
    )
    react_pr_comment_parser.add_argument(
        '--check-remove-labels',
        help='Coma separated list of labels to check and remove if found',
        type=str,
        required=False,
        default='',
    )
    react_pr_comment_parser.add_argument(
        '--check-comments',
        help='"#" separated list of comments to check',
        type=str,
        required=False,
        default='',
    )

    add_comment_to_pr_parser = rel_subparsers.add_parser(
        str(add_comment_to_pr.__doc__),
        help='Adds a comment to the release issue'
    )
    add_comment_to_pr_parser.add_argument(
        '--comment',
        help='String to add as a comment to the release issue',
        type=str,
        required=True,
    )

    rel_subparsers.add_parser(
        str(close_pr.__doc__),
        help='Closes the release issue'
    )

    rel_subparsers.add_parser(
        str(check_pr_approved.__doc__),
        help='Checks if the release issue is approved'
    )

    rel_subparsers.add_parser(
        str(merge_pr.__doc__),
        help='Merges the release issue'
    )

    return opts.parse_args()


def main():
    opts = parse_args()

    logging.basicConfig(
        format="<%(name)s:%(levelname)s> - %(message)s",
        level=logging.DEBUG if opts.debug else logging.INFO,
        stream=sys.stderr
    )

    gh = get_gh_client(opts)
    try:
        CMD_MAP[opts.release_action](gh, opts)
    except Exception as e:
        gh.close()
        raise e


if __name__ == '__main__':
    main()
