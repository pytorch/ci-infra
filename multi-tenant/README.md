# multi-tenant

## A brief warning
Please be aware that running ansible locally can be harmful and dangerous. Those operations are destructive and running multiple in parallel can hinder hosts in a bad state, potentially hard to recover. So, if you are planning to do so, please communicate and reach out to teamates to make sure nothing is running in parallel.

The recommended approach is to open a PR and test your changes by pushing to the PR, it is safer: keys are already there. Also, other users can see your actions and changes. And more importantly: it should be concurrency protected.

## Setup
Prerequisites: [`mise`](https://mise.jdx.dev/) and [`just`](https://just.systems/).
mise pins the toolchain (`mise.toml`: python, uv, awscli, jq) and
[uv](https://docs.astral.sh/uv/) manages the Python deps (`pyproject.toml`).

Sync the Python environment once:
```bash
just setup
```

Secrets are loaded automatically from `secrets.env` (git-ignored). It must export
`GH_APP_PK`, `ECR_READONLY_AWS_CREDENTIALS`, and `INSTANCE_LABELS`.

## Running ansible against the B200 hosts
```bash
just setup-host                 # all B200 hosts
just setup-host dgxb200-03      # a single host / group
```
The fleet uses SSH password auth for the `pytorch` user (no keys), so the
recipes prompt for both the SSH password (`-k`) and the sudo password (`-K`) —
the same value on this fleet. See `just --list` for the full set of recipes.

## Inventory
The ansible inventory is manually managed and can be found on `inventory/manual_inventory`, please make sure to keep it up to date.

Some commands for debug:

```bash
# for checking which cgroups a process is aligned with
systemd-cgls
# logging in as a specific user with systemd enabled
sudo su -l $USER
# status and logs for the service
systemctl status ghad-manager
journalctl -u ghad-manager.service
# full command docker is running for github daemon in a specific user environment (after logging in as that user)
docker ps --all --no-trunc
docker logs ghad-main-shared-instance-container -f
```

