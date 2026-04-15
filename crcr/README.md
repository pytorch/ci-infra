# CRCR - Cross Repo CI Relay

CRCR (Cross Repo CI Relay) is the infrastructure that enables PyTorch repository to automatically trigger CI workflows in downstream repositories without being tightly coupled.

When a developer creates or updates a PR in `pytorch/pytorch`, the system:

1. Receives webhook events via a GitHub App
2. Verifies the webhook signature (`X-Hub-Signature-256`)
3. Reads the allowlist YAML to determine eligible downstream repos
4. Dispatches `repository_dispatch` events to those repos
5. Downstream repos pull PyTorch code, build, test, and optionally report results back

Core components:

- **GitHub App** - Authentication hub and event bridge under the pytorch organization
- **AWS Lambda** - Webhook receiver and event dispatcher (Python 3.13)
- **ElastiCache (Redis)** - Caches the allowlist to reduce GitHub API calls
- **Secrets Manager** - Stores the GitHub App private key and webhook secret
- **VPC** - Network isolation for Lambda and Redis

For more details, see the RFC: https://github.com/pytorch/rfcs/pull/90

## Directory Structure

```text
crcr/
├── Terrafile                       # Module & asset dependency specification (YAML)
├── requirements.txt                # Python dependencies (PyYAML)
├── scripts/
│   └── terrafile_lambdas.py        # Downloads Terraform modules and Lambda ZIP assets
├── modules/
│   ├── backend-file/               # S3 backend configuration templates
│   │   ├── backend-state.tf
│   │   └── backend.tf
│   └── backend-state/              # Symlink to ../../modules/backend-state
└── aws/                            # Terraform deployment root
    ├── Makefile                    # Build orchestration (terrafile, init, plan, apply, clean)
    ├── main.tf                     # Terraform & provider version constraints
    ├── provider.tf                 # AWS provider configuration
    ├── variables.tf                # Input variables
    ├── locals.tf                   # Computed values (secret ARN, AZs, tags)
    ├── outputs.tf                  # Outputs (webhook URL, Redis endpoint)
    ├── vpc.tf                      # VPC and subnets
    ├── iam.tf                      # Lambda execution role and policies
    ├── secrets.tf                  # Secrets Manager secret and version
    ├── elasticache.tf              # Redis replication group
    ├── result.tf                   # Result callback lambda function and public function URL
    └── webhook.tf                  # Webhook lambda function and public function URL
```

## Prerequisites

### 1. Create S3 Bucket & DynamoDB Table

Terraform remote state requires an S3 bucket and a DynamoDB table for state locking. These must be created **once** before the first terraform init.

**Note:**
Replace `<env>` with the target value (e.g. `prod`, `canary`) and `<region>` with the target region (e.g. `us-east-1`):

```bash
aws s3api create-bucket \
  --bucket tfstate-pyt-crcr-<env> \
  --region <region>

aws s3api put-bucket-versioning \
  --bucket tfstate-pyt-crcr-<env> \
  --versioning-configuration Status=Enabled

aws dynamodb create-table \
  --table-name tfstate-lock-pyt-crcr-<env> \
  --attribute-definitions AttributeName=LockID,AttributeType=S \
  --key-schema AttributeName=LockID,KeyType=HASH \
  --billing-mode PAY_PER_REQUEST \
  --region <region>
```

## Configuration Variables

| Variable | Default | Description |
|---|---|---|
| `github_app_id` | N/A | GitHub App ID for the CRCR relay |
| `github_app_secret` | N/A | GitHub App webhook secret for HMAC signature verification (sensitive) |
| `github_app_privatekey` | N/A | PEM-encoded GitHub App private key (sensitive) |
| `environment` | N/A | Environment name for resource tagging and naming (e.g. `prod`, `canary`) |
| `upstream_repo` | `pytorch/pytorch` | GitHub upstream repository in `owner/repo` format |
| `allowlist_url` | `https://github.com/pytorch/pytorch/blob/main/.github/allowlist.yml` | GitHub URL to the relay allowlist YAML |
| `allowlist_ttl` | `1200` | Allowlist cache TTL in Redis (seconds) |
| `vpc_cidr_block` | `10.0.0.0/16` | CIDR block for the VPC |
| `availability_zone_suffixes` | `["a", "b"]` | Availability zone letter suffixes |
| `hud_api_url` | `N/A` | URL for sending callback data to HUD |
| `hud_bot_key` | `N/A` | Key to access to HUD (sensitive) |
| `oot_status_ttl` | `259200` | OOT workflow run status TTL in Redis (seconds) |

**Note:**

All Terraform variables are set via `TF_VAR_<name>` environment variables.

## Deployment

### Local Deployment

Set required variables first.

```bash
cd ci-infra/crcr/aws

export TF_VAR_github_app_id=123456
export TF_VAR_github_app_secret=<webhook_secret>
export TF_VAR_github_app_privatekey="$(cat path/to/key.pem)"
export TF_VAR_hud_api_url=<hud_api_url>
export TF_VAR_hud_bot_key=<hud_bot_key>
```

#### Deploy prod

```bash
export TF_VAR_environment=prod
make plan
make apply TERRAFORM_EXTRAS="-auto-approve -lock-timeout=15m"
```

#### Deploy canary with a personal AWS account

```bash
export REGION=us-east-1
export ACCOUNT=391835788720
export TF_VAR_environment=canary
make plan
make apply TERRAFORM_EXTRAS="-auto-approve -lock-timeout=15m"
```

> **Note**: When running locally, `AWS_PROFILE` is set to `ACCOUNT` for authentication (skipped in GitHub Actions where IAM role assumption is used instead).

### GitHub Actions Deployment

The production deployment is handled via the `crcr-deploy-prod.yml` workflow (`workflow_dispatch` trigger). To deploy:

1. **Configure GitHub Secrets** in the repository settings:
   - `CRCR_GITHUB_APP_ID` - GitHub App ID
   - `CRCR_GITHUB_APP_SECRET` - GitHub App webhook secret
   - `CRCR_GITHUB_APP_PRIVATEKEY` - PEM-encoded GitHub App private key
   - `CRCR_HUD_API_URL` - URL for sending callback data to HUD
   - `CRCR_HUD_BOT_KEY` - Key to access to HUD

2. **Trigger the workflow** manually from workflow_dispatch:

3. The workflow will:
   - Check out the code
   - Install OpenTofu 1.5.7
   - Install virtualenv
   - Assume the AWS IAM role via OIDC
   - Run `make apply` with `-auto-approve -lock-timeout=15m`

Concurrency is controlled by the group `terraform-make-apply-crcr` (no in-progress cancellation) to prevent parallel deployments.

## Feature Roadmap

CRCR follows a four-level progression system. Each level adds more integration between upstream PyTorch and downstream repos.

| Level | Name | Status | Description |
|---|---|---|---|
| **L1** | Events Only | **Current** | Webhook events are forwarded to downstream repos. No feedback to upstream PRs. Downstream repos receive `repository_dispatch` and run CI independently. |
| **L2** | HUD Visibility | developing | Downstream CI results are written to ClickHouse and displayed on a dedicated HUD page (`hud.pytorch.org/oot/[org]/[repo]`). Upstream PRs still show no check status. |
| **L3** | Label-Triggered PR Checks | developing | A non-blocking Check Run appears on upstream PRs when a `ciflow/oot/<name>` label is added. This is the recommended long-term target for most downstream repos. |
| **L4** | Always-On Blocking Checks | developing | Blocking Check Run auto-triggered for every PR. Reserved for critical accelerators only. Merge is blocked on failure. |
