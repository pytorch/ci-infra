#!/usr/bin/env bash
# mirror-images.sh - Copy Harbor bootstrap images from upstream registries to ECR
#
# Usage: mirror-images.sh <cluster-id>
#
# Reads images.yaml for the list of images to mirror.
# Resolves AWS account/region via cluster-config.py + tofu outputs.
# Creates ECR repos if they don't exist, then copies with crane.
#
# Requires: crane, aws CLI, tofu, uv

set -euo pipefail
source "$(cd "$(dirname "${BASH_SOURCE[0]}")/../../../scripts" && pwd)/mise-activate.sh"

# Colors
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[0;33m'
NC='\033[0m'

log_info() { echo -e "${GREEN}>${NC} $*"; }
log_warn() { echo -e "${YELLOW}!${NC} $*"; }
log_error() { echo -e "${RED}x${NC} $*"; }

if [[ $# -lt 1 ]]; then
    echo "Usage: $0 <cluster-id>"
    echo "  e.g. $0 arc-staging"
    exit 1
fi

CLUSTER="$1"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
IMAGES_FILE="$SCRIPT_DIR/../images.yaml"
CLUSTER_CONFIG="$SCRIPT_DIR/../../../scripts/cluster-config.py"

if [[ ! -f "$IMAGES_FILE" ]]; then
    log_error "images.yaml not found at $IMAGES_FILE"
    exit 1
fi

if [[ ! -f "$CLUSTER_CONFIG" ]]; then
    log_error "cluster-config.py not found at $CLUSTER_CONFIG"
    exit 1
fi

if ! command -v crane >/dev/null 2>&1; then
    log_error "crane not found. Install via mise: mise install crane"
    exit 1
fi

if ! command -v tofu >/dev/null 2>&1; then
    log_error "tofu not found. Install via mise: mise install opentofu"
    exit 1
fi

# Resolve cluster config via cluster-config.py
BUCKET=$(uv run "$CLUSTER_CONFIG" "$CLUSTER" state_bucket)
REGION=$(uv run "$CLUSTER_CONFIG" "$CLUSTER" region)
CLUSTER_NAME=$(uv run "$CLUSTER_CONFIG" "$CLUSTER" cluster_name)

log_info "Cluster: $CLUSTER (name=$CLUSTER_NAME, region=$REGION, bucket=$BUCKET)"

# Initialize tofu with the correct backend for this cluster
TF_DIR="$SCRIPT_DIR/../terraform"

log_info "Initializing tofu backend for $CLUSTER..."
(
    cd "$TF_DIR"
    tofu init -reconfigure \
        -backend-config="bucket=$BUCKET" \
        -backend-config="key=$CLUSTER/base/terraform.tfstate" \
        -backend-config="region=us-west-2" \
        -backend-config="dynamodb_table=ciforge-terraform-locks" \
        >/dev/null 2>&1
)

# Get AWS region from tofu outputs (authoritative) with fallback to cluster config
AWS_REGION=$(cd "$TF_DIR" && tofu output -raw aws_region 2>/dev/null || echo "$REGION")
AWS_ACCOUNT=$(aws sts get-caller-identity --query Account --output text)
ECR_REGISTRY="${AWS_ACCOUNT}.dkr.ecr.${AWS_REGION}.amazonaws.com"

log_info "ECR registry: $ECR_REGISTRY"
log_info "AWS region: $AWS_REGION"
echo ""

# Authenticate crane to ECR
log_info "Authenticating crane to ECR..."
aws ecr get-login-password --region "$AWS_REGION" | \
    crane auth login "$ECR_REGISTRY" --username AWS --password-stdin
echo ""

# Parse images.yaml and mirror each image
log_info "Reading image manifest from images.yaml..."
echo ""

IMAGES_JSON=$(uv run --with pyyaml python3 -c "
import yaml, json, sys
with open('$IMAGES_FILE') as f:
    data = yaml.safe_load(f)
json.dump(data['images'], sys.stdout)
")

TOTAL=$(echo "$IMAGES_JSON" | python3 -c "import json,sys; print(len(json.load(sys.stdin)))")
CURRENT=0
FAILED=0

for row in $(echo "$IMAGES_JSON" | python3 -c "
import json, sys
images = json.load(sys.stdin)
for img in images:
    print(f\"{img['source']}|{img['repository']}|{img['tag']}\")
"); do
    IFS='|' read -r SOURCE REPO TAG <<< "$row"
    CURRENT=$((CURRENT + 1))
    DEST="${ECR_REGISTRY}/${REPO}:${TAG}"

    echo "[$CURRENT/$TOTAL] $SOURCE"
    echo "      > $DEST"

    # Create ECR repository if it doesn't exist
    if ! aws ecr describe-repositories --repository-names "$REPO" --region "$AWS_REGION" >/dev/null 2>&1; then
        log_info "  Creating ECR repository: $REPO"
        aws ecr create-repository \
            --repository-name "$REPO" \
            --region "$AWS_REGION" \
            --image-scanning-configuration scanOnPush=false \
            --output text >/dev/null
    fi

    # Copy image with crane
    if crane copy "$SOURCE" "$DEST" 2>&1; then
        log_info "  Copied successfully"
    else
        log_error "  Failed to copy $SOURCE"
        FAILED=$((FAILED + 1))
    fi
    echo ""
done

echo "================================================================"
if [[ $FAILED -eq 0 ]]; then
    log_info "All $TOTAL images mirrored to ECR successfully"
else
    log_error "$FAILED/$TOTAL images failed to mirror"
    exit 1
fi
echo ""
echo "ECR_REGISTRY=$ECR_REGISTRY"
