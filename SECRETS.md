# GitHub Actions Secrets Inventory

Central registry of every secret configured in this repository's GitHub Settings. Keep this file up to date: whenever a secret is added or removed, update the appropriate section in the same PR.

Related docs:

- [github-admin-permissions.md](github-admin-permissions.md) — who can rotate secrets
- [cloud-account-access.md](cloud-account-access.md) — AWS account access policy
- [crcr/README.md](crcr/README.md) — canonical docs for the CRCR GitHub App credentials

## Legend

| Field | Meaning |
|---|---|
| **Added by** | Person who created the secret entry in GitHub Settings. GitHub does not expose this natively — fill in manually when known. |
| **Requested by** | Team or person who originally asked for this secret to exist (may differ from who created it). |
| **Used by** | Workflow file(s) that reference the secret and the system it authenticates to. |
| **PoC** | Current point of contact for the secret (who to call if it needs rotating). |

---

## Environment Secrets

### `CANARY_GITHUB_TOKEN`

| | |
|---|---|
| **Environments** | osdc-staging, osdc-production |
| **Added by** | @jeanschmidt, @huydhn |
| **Requested by** | @jeanschmidt, @huydhn |
| **PoC** | @jeanschmidt, @huydhn · Meta |
| **Used by** | `_osdc-deploy.yml`, `_osdc-slow-tests.yml` — exposed as `GH_TOKEN` for `gh` CLI during OSDC integration tests and load tests |

### `META_AWS_ACC_ID`

| | |
|---|---|
| **Environments** | osdc-staging, osdc-production |
| **Added by** | @jeanschmidt, @huydhn |
| **Requested by** | @jeanschmidt, @huydhn |
| **PoC** | @jeanschmidt, @huydhn · Meta |
| **Used by** | `_osdc-deploy.yml`, `_osdc-slow-tests.yml`, `_osdc-plan.yml` — AWS account ID for the Meta-donated AWS account that hosts the OSDC ARC clusters (us-west-1 / us-west-2) |

### `META_AWS_DEPLOY_PLAN_ROLE`

| | |
|---|---|
| **Environments** | osdc-staging |
| **Added by** | @jeanschmidt, @huydhn |
| **Requested by** | @jeanschmidt, @huydhn |
| **PoC** | @jeanschmidt, @huydhn · Meta |
| **Used by** | `_osdc-plan.yml` — read-only OIDC IAM role in the Meta AWS account, used for `tofu plan` on pull requests so PRs cannot mutate state |

### `META_AWS_DEPLOY_ROLE`

| | |
|---|---|
| **Environments** | osdc-staging, osdc-production |
| **Added by** | @jeanschmidt, @huydhn |
| **Requested by** | @jeanschmidt, @huydhn |
| **PoC** | @jeanschmidt, @huydhn · Meta |
| **Used by** | `_osdc-deploy.yml`, `_osdc-slow-tests.yml` — apply-capable OIDC IAM role in the Meta AWS account, used for `tofu apply` / smoke / integration-test jobs |


### `LF_AWS_DEPLOY_ROLE_ARN`

| | |
|---|---|
| **Environments** | osdc-production |
| **Added by** | @zxiiro |
| **Requested by** | @zxiiro |
| **PoC** | @zxiiro, @jordanconway · Linux Foundation |
| **Used by** | `osdc-deploy-prod.yml` via `_osdc-deploy.yml` — OIDC IAM role ARN in the LF AWS account (`391835788720`), assumed for `tofu apply` / smoke / integration-test jobs for `lf-prod-aws-ue1` and `lf-prod-aws-ue2` |

### `LF_AWS_PLAN_ROLE_ARN`

| | |
|---|---|
| **Environments** | osdc-staging |
| **Added by** | @zxiiro |
| **Requested by** | @zxiiro |
| **PoC** | @zxiiro, @jordanconway · Linux Foundation |
| **Used by** | `osdc-plan-prod.yml` via `_osdc-plan.yml` — OIDC IAM role ARN in the LF AWS account (`391835788720`), assumed for `tofu plan` on pull requests for `lf-prod-aws-ue1` and `lf-prod-aws-ue2` |

### `CRCR_GITHUB_APP_ID`

| | |
|---|---|
| **Environments** | crcr-prod |
| **Added by** | @zxiiro |
| **Requested by** | @fffrog, @can-gaa-hou |
| **PoC** | @zxiiro · Linux Foundation |
| **Used by** | `crcr-deploy-prod.yml`, `crcr-on-pr.yml` — passed as `TF_VAR_github_app_id` to OpenTofu for the CRCR webhook-router GitHub App. See [crcr/README.md](crcr/README.md). |

### `CRCR_GITHUB_APP_PRIVATEKEY`

| | |
|---|---|
| **Environments** | crcr-prod |
| **Added by** | @zxiiro |
| **Requested by** | @fffrog, @can-gaa-hou |
| **PoC** | @zxiiro · Linux Foundation |
| **Used by** | `crcr-deploy-prod.yml`, `crcr-on-pr.yml` — PEM-encoded private key for the CRCR GitHub App |

### `CRCR_GITHUB_APP_SECRET`

| | |
|---|---|
| **Environments** | crcr-prod |
| **Added by** | @zxiiro |
| **Requested by** | @fffrog, @can-gaa-hou |
| **PoC** | @zxiiro · Linux Foundation |
| **Used by** | `crcr-deploy-prod.yml`, `crcr-on-pr.yml` — webhook secret for the CRCR GitHub App |

---

## Repository Secrets

### `LIST_PYTORCH_RUNNERS_GITHUB_TOKEN`

| | |
|---|---|
| **Added by** | @jeanschmidt |
| **Requested by** | @jeanschmidt |
| **PoC** | @jeanschmidt · Meta |
| **Used by** | `ali-deploy-canary.yml`, `ali-deploy-prod.yml`, `arc-deploy-prod.yml` — GitHub token used by ALI and ARC Terraform Makefiles to query the GitHub Actions Runners API (e.g. `GET /repos/pytorch/pytorch/actions/runners`) during apply |

### `PYTORCHCI_GRAFANA_API_TOKEN`

| | |
|---|---|
| **Added by** | @jeanschmidt |
| **Requested by** | @jeanschmidt |
| **PoC** | @jeanschmidt · Meta |
| **Used by** | `grafana-publish.yml`, `osdc-publish-dashboards.yml` — API token for the pytorchci Grafana Cloud instance; publishes dashboards from `grafana/` and `osdc/modules/monitoring/dashboards/` on every push to `main` |

### `PY_FOUNDATION_AWS_ACC_ID`

| | |
|---|---|
| **Added by** | @jeanschmidt |
| **Requested by** | @jeanschmidt |
| **PoC** | @zxiiro, @jordanconway · Linux Foundation |
| **Used by** | `ali-deploy-canary.yml`, `ali-deploy-prod.yml`, `ali-on-pr.yml`, `arc-deploy-prod.yml`, `arc-on-pr.yml`, `crcr-deploy-prod.yml`, `crcr-on-pr.yml` — AWS account ID for the PyTorch Foundation-owned AWS account (`391835788720`, us-east-1), used for ALI, ARC, and CRCR infrastructure |

### `PY_FOUNDATION_AWS_DEPLOY_ROLE`

| | |
|---|---|
| **Added by** | @jeanschmidt |
| **Requested by** | @jeanschmidt |
| **PoC** | @zxiiro, @jordanconway · Linux Foundation |
| **Used by** | `ali-deploy-canary.yml`, `ali-deploy-prod.yml`, `ali-on-pr.yml`, `arc-deploy-prod.yml`, `arc-on-pr.yml`, `crcr-deploy-prod.yml`, `crcr-on-pr.yml` — OIDC IAM role in the PyTorch Foundation AWS account, assumed for both `tofu plan` (PRs) and `tofu apply` (main branch) |

---

## Maintenance

When adding a new secret:

1. Add a new `###` section above in the appropriate group (Environment Secrets or Repository Secrets).
2. Fill in all fields — do not leave **Requested by** or **PoC** blank.
3. When removing a secret, delete its section and note the removal in the PR description.

GitHub does not surface who created a secret or when — if the creator is not recorded here at creation time, it is lost. Use the **Added by** field proactively.
