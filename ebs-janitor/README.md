# EBS janitor

Cleans up detached (`available`) EBS volumes in the Meta (fbossci) and LF fleet
accounts. It runs daily from `.github/workflows/ebs-janitor.yml` and is a **dry
run unless `--execute` is passed**.

Most leftovers come from two leaks:

- **Legacy runner root volumes.** When a runner is reused, its root volume is
  replaced, and the replacement doesn't get `DeleteOnTermination=true`, so it
  outlives the instance (pytorch/test-infra#7930).
- **gpu-dev disks.** gpu-dev keeps user data in snapshots. At the end of a
  reservation it snapshots the disk and then deletes it, but if the snapshot
  takes longer than its 30-minute wait, the volume is left behind.

## Rules

For each detached volume, the first matching rule decides:

| Rule | Matches | Action |
|---|---|---|
| `keep-tag` | has the `ebs-janitor/keep` tag | skip |
| `too-new` | created less than `min_age_days` ago | skip |
| `janitor-snapshot-done` / `-pending` | a janitor snapshot of it exists | delete once it completes |
| `runner-root` | no tags or a runner `Name`, and created from an AMI matching `runner_root.ami_name_patterns` | delete |
| `gpu-dev-backed-up` | gpu-dev disk with a completed gpu-dev snapshot taken after its `last_used` | delete |
| `gpu-dev-not-backed-up` | gpu-dev disk without one | snapshot |
| `pvc-live-cluster` | Kubernetes PVC of a cluster that still exists | report only |
| `pvc-deleted-cluster` | Kubernetes PVC of a cluster that no longer exists | snapshot (delete if older than `old_volume_days`) |
| `unknown` | anything else | tag `ebs-janitor/first-seen`; after `grace_days`, snapshot (delete if older than `old_volume_days`) |

"Snapshot" means: create a janitor snapshot now, then delete the volume on a
later run once the snapshot has completed. Janitor snapshots are tagged
`ebs-janitor/managed=true`, carry the source volume's tags under
`ebs-janitor/orig/`, and are deleted after `snapshot_retention_days`. They
deliberately don't carry `gpu-dev-user` / `disk_name`, otherwise gpu-dev would
restore an old abandoned disk as a user's newest snapshot.

To keep a volume, tag it `ebs-janitor/keep`.

## Safety

- Per-run caps on volume deletes, snapshots created and snapshots deleted.
- A permission check (`DryRun=True`) runs before acting, and each volume is
  re-checked right before its action. Volumes that were attached or retagged
  since the scan are skipped.
- A region stops after more than 5 errors (or 5% of its actions).
- Volumes whose source AMI can't be resolved are never treated as runner disks.
- Output never includes tag values (gpu-dev tags hold user emails); logs list
  volume IDs, rules and actions only. CloudTrail records every API call.

## Running locally

```bash
cd ebs-janitor
uv run --no-project --with-requirements requirements.txt pytest -q
# dry run against the account in your AWS credentials
uv run --no-project --with-requirements requirements.txt python ebs_janitor.py --config policies/meta.yaml
```

Policies: `policies/meta.yaml` (fbossci, gpu-dev rule on) and `policies/lf.yaml`
(LF account). Anything added to `runner_root.ami_name_patterns` is deleted
without a snapshot, so only list AMIs whose disks hold nothing but CI scratch
space.
