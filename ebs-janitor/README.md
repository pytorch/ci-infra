# EBS janitor

Deletes detached EBS root volumes left behind by the legacy EC2 CI runners in
fbossci. When a runner is reused, its root volume is replaced, and the
replacement doesn't get `DeleteOnTermination`, so it outlives the instance
(pytorch/test-infra#7930).

A detached volume is deleted when it is untagged or named after a runner fleet
(`gh-ci-action-runner`, `meta-pytorch-action-runner`), was restored from a
runner AMI (Amazon Linux 2023, the account's `gha-runner-*` copies of it, or
`Windows 20?? GHA CI`), and is at least two days old. Every other volume is left
alone. See `rules.py`.

Each run also counts the untagged or runner-named volumes it skipped, by source
AMI. A new runner AMI that `RUNNER_AMI_PATTERNS` doesn't cover yet, or one AWS
stops showing this account, shows up there.

It's a dry run unless `--execute` is passed, deletes at most `--max-deletes`
volumes per run (default 500), and stops a region after repeated errors.

```bash
cd ebs-janitor
uv run --no-project --with pytest==8.4.2 pytest -q
# dry run against fbossci, with the same pinned dependencies as CI
uv run --no-project --with-requirements requirements.txt python ebs_janitor.py
```

`requirements.txt` is the hash-pinned lock for `requirements.in`. After editing
`requirements.in`, regenerate it:

```bash
uv pip compile requirements.in --python-version 3.12 --generate-hashes -o requirements.txt
```

## In CI

`.github/workflows/ebs-janitor.yml` runs a dry run every day at 06:17 UTC with
the read-only plan role (`osdc-staging` environment). To delete, start the
workflow by hand from `main` with `execute` checked; it then uses the deploy role
(`osdc-production` environment). `max_deletes` caps the deletes for that run.
