# Cross-Repository CI Relay (CRCR)

Cross-Repository CI Relay (CRCR) is a GitHub webhook relay service for PyTorch out-of-tree backends. It receives webhook events from an upstream repository via a GitHub App, validates them against a whitelist, and forwards `repository_dispatch` events to registered downstream repositories — enabling downstream CI pipelines to react to upstream changes without being tightly coupled.

Architecture: GitHub App → Lambda webhook (AWS) → `repository_dispatch` → downstream repos

## Directory Structure

```Text
crcr/
├── Makefile                          # Root orchestration: terrafile / tflint / plan / apply / clean
├── Terrafile                         # Lambda zip asset source (pytorch/test-infra release)
├── requirements.txt                  # Python deps for terrafile script
├── scripts/
│   └── terrafile_lambdas.py          # Downloads Lambda zip assets from GitHub releases
├── modules/
│   └── backend-file/
│       ├── backend.tf                # S3 backend template (placeholder: #AWS_REGION)
│       └── backend-state.tf          # backend-state module template (placeholder: #AWS_REGION)
└── aws/
    └── <account-id>/
        └── <region>/
            ├── Makefile              # Region-level: init / plan / apply / clean
            ├── provider.tf           # AWS provider
            ├── main.tf
            ├── data.tf               # ElastiCache + VPC source Lambda data sources
            ├── iam.tf                # Lambda execution role + policies
            ├── webhook.tf            # Lambda function, Function URL, permissions
            ├── variables.tf          # Input variables
            └── outputs.tf            # Webhook URL output
```

Generated files (not committed):

```Text
aws/<account>/<region>/
├── dyn_locals.tf     # aws_region / aws_account_id locals (generated from directory name)
├── backend.tf        # Rendered S3 backend config
├── backend-state.tf  # Rendered backend-state module
└── .terraform/       # OpenTofu working directory
```

## Prerequisites

### 1. S3 Bucket and DynamoDB Table (manual, one-time)

Terraform remote state requires an S3 bucket and a DynamoDB lock table. Create them manually before the first deploy:

```bash
# Replace <region> with the target region, e.g. us-east-1
aws s3api create-bucket \
  --bucket tfstate-pyt-cross-repo-ci-relay-prod \
  --region <region>

aws s3api put-bucket-versioning \
  --bucket tfstate-pyt-cross-repo-ci-relay-prod \
  --versioning-configuration Status=Enabled

aws dynamodb create-table \
  --table-name tfstate-lock-pyt-cross-repo-ci-relay-prod \
  --attribute-definitions AttributeName=LockID,AttributeType=S \
  --key-schema AttributeName=LockID,KeyType=HASH \
  --billing-mode PAY_PER_REQUEST \
  --region <region>
```

### 2. AWS Secrets Manager Secret

The Lambda reads GitHub App private key and Redis credentials from a single Secrets Manager secret. Create it manually:

```bash
aws secretsmanager create-secret \
  --name <secret_name> \
  --region <region>
```

Then store the GitHub App private key and any other credentials in it. The default secret name used by variables.tf is `ci_secret-orSIF4`; override via `TF_VAR_secret_name` if needed.

### 3. Required Variables

Pass these as environment variables (CI/CD) or `terraform.tfvars` (local):

| Variable | Description | Example |
|----------|-------------|---------|
| `TF_VAR_vpc_lambda_name` | Name of an existing Lambda in the same VPC as Redis; its subnet/security-group config is reused | `some-existing-lambda` |
| `TF_VAR_redis_replication_group_id` | ElastiCache replication group ID | `crcr-redis-prod` |
| `TF_VAR_redis_login` | Redis credentials in `username:password` format (Redis 6+ ACL) | `crcr-user:s3cr3t` |

Variables with defaults (override as needed):

| Variable | Default | Description |
|----------|---------|-------------|
| `github_app_id` | `2847493` | GitHub App ID |
| `secret_name` | `ci_secret-orSIF4` | Secrets Manager secret name |
| `upstream_repo` | `cosdt/UpStream` | Upstream repository in `owner/repo` format |
| `allowlist_url` | *(upstream repo whitelist URL)* | URL to the relay whitelist YAML |
| `allowlist_ttl` | `3600` | Whitelist cache TTL in Redis (seconds) |

## Deployment

All commands run from the **root `crcr/` directory**. The root Makefile fans out to every `aws/<account>/<region>/` subdirectory automatically.

```bash
cd ci-infra/crcr

# Plan (dry run)
make plan

# Apply
make apply
```

## CI/CD (GitHub Actions)

| Workflow | Trigger | Action |
|----------|---------|--------|
| `crcr-on-pr.yml` | PR touching `crcr/**` or workflow files | TFLint + `tofu plan` |
| `crcr-deploy-prod.yml` | Manual (`workflow_dispatch`) | `tofu apply` |

Required GitHub repository secrets:

| Secret | Description |
|--------|-------------|
| `PY_FOUNDATION_AWS_ACC_ID` | AWS account ID |
| `PY_FOUNDATION_AWS_DEPLOY_ROLE` | IAM role name for OIDC assumption |
| `CRCR_VPC_LAMBDA_NAME` | `TF_VAR_vpc_lambda_name` |
| `CRCR_REDIS_REPLICATION_GROUP_ID` | `TF_VAR_redis_replication_group_id` |
| `CRCR_REDIS_LOGIN` | `TF_VAR_redis_login` |
