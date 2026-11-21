# EBS janitor

Deletes detached EBS root volumes left behind by the legacy EC2 CI runners in
fbossci. When a runner is reused, its root volume is replaced, and the
replacement doesn't get `DeleteOnTermination`, so it outlives the instance
(pytorch/test-infra#7930).

A detached volume is deleted when it is untagged or named after a runner fleet
(`gh-ci-action-runner`, `meta-pytorch-action-runner`), was restored from a
runner AMI (Amazon Linux 2023 or `Windows 20?? GHA CI`), and is at least two
days old. Every other volume is left alone. See `rules.py`.

It's a dry run unless `--execute` is passed, deletes at most `--max-deletes`
volumes per run (default 500), and stops a region after repeated errors.

```bash
cd ebs-janitor
uv run --no-project --with pytest==8.4.2 pytest -q
# dry run against the account in your AWS credentials
uv run --no-project --with boto3 python ebs_janitor.py
```
