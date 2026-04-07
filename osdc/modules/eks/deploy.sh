#!/usr/bin/env bash
set -euo pipefail
#
# EKS base module deploy script.
# Called by: just deploy-module (as the first module in the list)
# Args: $1=cluster-id  $2=cluster-name  $3=region
#
# Handles core AWS/EKS infrastructure + Harbor pull-through cache:
#   1. Terraform: VPC + EKS cluster + Harbor S3/IAM
#   2. Kubeconfig
#   3. Mirror Harbor bootstrap images to ECR
#   4. Deploy Harbor (helm + secrets + proxy cache config)
#
# Other components (git-cache, node-compactor, etc.) are
# deployed as separate modules via deploy-module.

CLUSTER="$1"
CNAME="$2"
REGION="$3"
MODULE_DIR="$(cd "$(dirname "$0")" && pwd)"
UPSTREAM_ROOT="${OSDC_UPSTREAM:-$(cd "$MODULE_DIR/../.." && pwd)}"
# shellcheck source=/dev/null
source "$UPSTREAM_ROOT/scripts/mise-activate.sh"
# shellcheck source=/dev/null
source "$UPSTREAM_ROOT/scripts/helm-upgrade.sh"
CFG="$UPSTREAM_ROOT/scripts/cluster-config.py"
SCRIPTS="$UPSTREAM_ROOT/scripts"

BUCKET=$(uv run "$CFG" "$CLUSTER" state_bucket)
TFVARS=$(uv run "$CFG" "$CLUSTER" tfvars)
STATE_REGION="us-west-2" # state buckets always in us-west-2

# ─── Preflight ────────────────────────────────────────────────────────────
if ! aws s3api head-bucket --bucket "${BUCKET}" --region "${STATE_REGION}" 2>/dev/null; then
  echo ""
  echo "ERROR: State bucket '${BUCKET}' does not exist."
  echo ""
  echo "Run bootstrap first:"
  echo "  just bootstrap ${CLUSTER}"
  echo ""
  exit 1
fi

# ─── Phase 1: Terraform ──────────────────────────────────────────────────
echo ""
echo "━━━ EKS: Terraform ━━━"
cd "$MODULE_DIR/infra"
tofu init -reconfigure \
  -backend-config="bucket=${BUCKET}" \
  -backend-config="key=${CLUSTER}/base/terraform.tfstate" \
  -backend-config="region=${STATE_REGION}" \
  -backend-config="dynamodb_table=ciforge-terraform-locks"

set +e
# shellcheck disable=SC2086
eval tofu plan $TFVARS -out=tfplan -detailed-exitcode
PLAN_EXIT=$?
set -e

if [[ $PLAN_EXIT -eq 0 ]]; then
  echo "No changes. Skipping apply."
  rm -f tfplan
elif [[ $PLAN_EXIT -eq 2 ]]; then
  echo ""
  CONFIRM="${OSDC_CONFIRM:-ask}"
  if [[ "$CONFIRM" == "yes" ]]; then
    echo "Auto-confirmed (OSDC_CONFIRM=yes)"
  elif [[ "$CONFIRM" == "no" ]]; then
    rm -f tfplan
    echo "Cancelled (OSDC_CONFIRM=no)."
    exit 1
  else
    read -p "Apply this plan? [y/N] " -n 1 -r
    echo
    if [[ ! $REPLY =~ ^[Yy]$ ]]; then
      rm -f tfplan
      echo "Cancelled."
      exit 1
    fi
  fi
  tofu apply tfplan
  rm -f tfplan
else
  rm -f tfplan
  echo "Tofu plan failed."
  exit 1
fi
cd - >/dev/null

# ─── Phase 2: Kubeconfig ─────────────────────────────────────────────────
echo ""
echo "━━━ EKS: Kubeconfig ━━━"
echo "Updating kubeconfig for $CNAME ($REGION)..."
NO_PROXY="${NO_PROXY:-},.eks.amazonaws.com" no_proxy="${no_proxy:-},.eks.amazonaws.com" \
  "$UPSTREAM_ROOT/scripts/kubeconfig-lock.sh" --name "$CNAME" --region "$REGION" --alias "$CNAME"

# ─── Phase 3: Mirror bootstrap images ────────────────────────────────────
echo ""
echo "━━━ EKS: Mirror bootstrap images ━━━"
"$MODULE_DIR/scripts/mirror-images.sh" "$CLUSTER"

# ─── Phase 4: Harbor ────────────────────────────────────────────────────
echo ""
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo "Harbor Pull-Through Cache - ${CLUSTER}"
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"

# Wait for base nodes
echo "Waiting for base nodes to be ready..."
kubectl wait --for=condition=Ready nodes -l role=base-infrastructure --timeout=10m
echo ""

# Get Terraform outputs (already initialized from Phase 1)
cd "$MODULE_DIR/infra"
HARBOR_ROLE=$(tofu output -raw harbor_role_arn)
HARBOR_BUCKET=$(tofu output -raw harbor_s3_bucket)
HARBOR_S3_KEY=$(tofu output -raw harbor_s3_access_key_id)
HARBOR_S3_SECRET=$(tofu output -raw harbor_s3_secret_access_key)
AWS_REGION=$(tofu output -raw aws_region 2>/dev/null || echo "us-west-2")
AWS_ACCOUNT=$(aws sts get-caller-identity --query Account --output text)
cd - >/dev/null
echo ""

ECR_REGISTRY="${AWS_ACCOUNT}.dkr.ecr.${AWS_REGION}.amazonaws.com"

# Create ServiceAccount for Harbor registry (IRSA)
echo "Creating harbor-registry ServiceAccount..."
kubectl apply -f - <<EOF
apiVersion: v1
kind: ServiceAccount
metadata:
    name: harbor-registry
    namespace: harbor-system
    annotations:
        eks.amazonaws.com/role-arn: "${HARBOR_ROLE}"
EOF

# Create S3 credentials secret
echo "Creating Harbor S3 credentials secret..."
kubectl create secret generic harbor-s3-credentials \
  --namespace harbor-system \
  --from-literal=REGISTRY_STORAGE_S3_ACCESSKEY="${HARBOR_S3_KEY}" \
  --from-literal=REGISTRY_STORAGE_S3_SECRETKEY="${HARBOR_S3_SECRET}" \
  --dry-run=client -o yaml | kubectl apply -f -
echo ""

# Auto-generate passwords (once, on first deploy)
if ! kubectl get secret harbor-admin-password -n harbor-system >/dev/null 2>&1; then
  echo "Generating Harbor admin password..."
  HARBOR_ADMIN_PW=$(openssl rand -base64 32 | tr -d '/+=' | head -c 32)
  kubectl create secret generic harbor-admin-password \
    --namespace harbor-system \
    --from-literal=password="${HARBOR_ADMIN_PW}"
else
  echo "Harbor admin password secret already exists, reusing..."
fi
HARBOR_ADMIN_PW=$(kubectl get secret harbor-admin-password -n harbor-system \
  -o jsonpath='{.data.password}' | base64 -d)

if ! kubectl get secret harbor-db-password -n harbor-system >/dev/null 2>&1; then
  echo "Generating Harbor DB password..."
  HARBOR_DB_PW=$(openssl rand -base64 32 | tr -d '/+=' | head -c 32)
  kubectl create secret generic harbor-db-password \
    --namespace harbor-system \
    --from-literal=password="${HARBOR_DB_PW}"
else
  echo "Harbor DB password secret already exists, reusing..."
fi
HARBOR_DB_PW=$(kubectl get secret harbor-db-password -n harbor-system \
  -o jsonpath='{.data.password}' | base64 -d)
echo ""

# Read per-cluster Harbor config
HARBOR_CORE_REPLICAS=$(uv run "$CFG" "$CLUSTER" harbor.core_replicas 2)
HARBOR_REGISTRY_REPLICAS=$(uv run "$CFG" "$CLUSTER" harbor.registry_replicas 2)
HARBOR_NGINX_REPLICAS=$(uv run "$CFG" "$CLUSTER" harbor.nginx_replicas 3)

# Install Harbor
echo "Installing Harbor..."
helm repo add harbor https://helm.goharbor.io 2>/dev/null || true
helm repo update harbor
ECR_HARBOR="${ECR_REGISTRY}/mirror/goharbor"

HELM_MAX_RETRIES=3
for attempt in $(seq 1 "$HELM_MAX_RETRIES"); do
  if helm_upgrade_by_input_hash harbor harbor-system \
    --create-namespace \
    --history-max 3 \
    -f "$MODULE_DIR/helm/harbor/values.yaml" \
    --set core.image.repository="${ECR_HARBOR}/harbor-core" \
    --set jobservice.image.repository="${ECR_HARBOR}/harbor-jobservice" \
    --set portal.image.repository="${ECR_HARBOR}/harbor-portal" \
    --set registry.registry.image.repository="${ECR_HARBOR}/registry-photon" \
    --set registry.controller.image.repository="${ECR_HARBOR}/harbor-registryctl" \
    --set database.internal.image.repository="${ECR_HARBOR}/harbor-db" \
    --set redis.internal.image.repository="${ECR_HARBOR}/redis-photon" \
    --set nginx.image.repository="${ECR_HARBOR}/nginx-photon" \
    --set exporter.image.repository="${ECR_HARBOR}/harbor-exporter" \
    --set persistence.imageChartStorage.s3.bucket="${HARBOR_BUCKET}" \
    --set persistence.imageChartStorage.s3.region="${AWS_REGION}" \
    --set persistence.imageChartStorage.s3.existingSecret=harbor-s3-credentials \
    --set registry.serviceAccountName=harbor-registry \
    --set registry.automountServiceAccountToken=true \
    --set harborAdminPassword="${HARBOR_ADMIN_PW}" \
    --set database.internal.password="${HARBOR_DB_PW}" \
    --set core.replicas="${HARBOR_CORE_REPLICAS}" \
    --set registry.replicas="${HARBOR_REGISTRY_REPLICAS}" \
    --set nginx.replicas="${HARBOR_NGINX_REPLICAS}" \
    --timeout 15m \
    --wait \
    --burst-limit 10000 \
    --qps 500 \
    harbor/harbor \
    --version 1.18.2; then
    break
  fi
  if [ "$attempt" -eq "$HELM_MAX_RETRIES" ]; then
    echo "Helm upgrade failed after $HELM_MAX_RETRIES attempts"
    exit 1
  fi
  echo "  Helm upgrade attempt $attempt/$HELM_MAX_RETRIES failed, retrying in 30s..."
  sleep 30
done
echo ""

# Ensure admin password matches K8s secret
echo "Ensuring Harbor admin password is up to date..."
kubectl port-forward -n harbor-system svc/harbor 8090:80 &
PW_PF_PID=$!
PW_MAX_RETRIES=12
for pw_attempt in $(seq 1 "$PW_MAX_RETRIES"); do
  if curl -sf -o /dev/null http://localhost:8090/api/v2.0/health 2>/dev/null; then
    break
  fi
  if [ "$pw_attempt" -eq "$PW_MAX_RETRIES" ]; then
    echo "  Harbor API not reachable after $PW_MAX_RETRIES attempts"
    kill "$PW_PF_PID" 2>/dev/null || true
    exit 1
  fi
  echo "  Waiting for Harbor API (attempt $pw_attempt/$PW_MAX_RETRIES)..."
  sleep 5
done
HTTP_CODE=$(curl -s -o /dev/null -w "%{http_code}" \
  -X PUT "http://localhost:8090/api/v2.0/users/1/password" \
  -u "admin:Harbor12345" \
  -H "Content-Type: application/json" \
  -d "{\"old_password\":\"Harbor12345\",\"new_password\":\"${HARBOR_ADMIN_PW}\"}")
kill "$PW_PF_PID" 2>/dev/null || true
if [ "$HTTP_CODE" = "200" ]; then
  echo "  Admin password migrated from default"
elif [ "$HTTP_CODE" = "401" ]; then
  echo "  Password already up to date"
else
  echo "  Warning: password migration returned HTTP $HTTP_CODE"
fi
echo ""

# Configure proxy cache projects
echo "Configuring proxy cache projects..."
HARBOR_ADMIN_PW_CURRENT=$(kubectl get secret harbor-admin-password -n harbor-system \
  -o jsonpath='{.data.password}' | base64 -d)

kubectl port-forward -n harbor-system svc/harbor 8080:80 &
PF_PID=$!
trap 'kill '"$PF_PID"' 2>/dev/null || true' EXIT

for hp_attempt in $(seq 1 12); do
  if curl -sf -o /dev/null http://localhost:8080/api/v2.0/health 2>/dev/null; then
    break
  fi
  if [ "$hp_attempt" -eq 12 ]; then
    echo "  Harbor API not reachable after 12 attempts"
    exit 1
  fi
  echo "  Waiting for Harbor API (attempt $hp_attempt/12)..."
  sleep 5
done

CRED_ARGS=""
if kubectl get secret harbor-dockerhub-credentials -n harbor-system >/dev/null 2>&1; then
  DH_USER=$(kubectl get secret harbor-dockerhub-credentials -n harbor-system \
    -o jsonpath='{.data.username}' | base64 -d)
  DH_TOKEN=$(kubectl get secret harbor-dockerhub-credentials -n harbor-system \
    -o jsonpath='{.data.token}' | base64 -d)
  CRED_ARGS="${CRED_ARGS} --dockerhub-username ${DH_USER} --dockerhub-token ${DH_TOKEN}"
fi
if kubectl get secret harbor-github-credentials -n harbor-system >/dev/null 2>&1; then
  GH_USER=$(kubectl get secret harbor-github-credentials -n harbor-system \
    -o jsonpath='{.data.username}' | base64 -d)
  GH_TOKEN=$(kubectl get secret harbor-github-credentials -n harbor-system \
    -o jsonpath='{.data.token}' | base64 -d)
  CRED_ARGS="${CRED_ARGS} --github-username ${GH_USER} --github-token ${GH_TOKEN}"
fi

# shellcheck disable=SC2086
uv run "$SCRIPTS/python/configure_harbor_projects.py" \
  --harbor-url http://localhost:8080 \
  --admin-password "${HARBOR_ADMIN_PW_CURRENT}" \
  ${CRED_ARGS}

kill "$PF_PID" 2>/dev/null || true
echo ""

echo "EKS base module deployed."
