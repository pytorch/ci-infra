#!/usr/bin/env bash

set -euo pipefail

mkdir -p /etc/eks/nodeadm.d

IMDS_BASE="http://169.254.169.254"
IMDS_TOKEN_TTL_SECONDS=21600
IMDS_RETRIES=5
IMDS_RETRY_SLEEP=2

# IMDS can return transient failures during early boot before the network stack is fully ready.
fetch_imds() {
  local method=$1
  local path=$2
  local extra_header=$3
  local attempt
  for attempt in $(seq 1 "$IMDS_RETRIES"); do
    if curl -fsS --connect-timeout 2 --max-time 5 -X "$method" -H "$extra_header" "${IMDS_BASE}${path}"; then
      return 0
    fi
    echo "[eks-base-pre-nodeadm] IMDS ${method} ${path} attempt ${attempt}/${IMDS_RETRIES} failed" >&2
    sleep "$IMDS_RETRY_SLEEP"
  done
  return 1
}

TOKEN=$(fetch_imds PUT /latest/api/token "X-aws-ec2-metadata-token-ttl-seconds: ${IMDS_TOKEN_TTL_SECONDS}")
AZ=$(fetch_imds GET /latest/meta-data/placement/availability-zone "X-aws-ec2-metadata-token: ${TOKEN}")

if [[ ! "$AZ" =~ ^[a-z]{2}-[a-z]+-[0-9][a-z]$ ]]; then
  echo "[eks-base-pre-nodeadm] IMDS returned malformed AZ value: '${AZ}'" >&2
  exit 1
fi

# 'ipam.osdc.internal/eni-config' is the canonical eni-config label key —
# also defined as ENI_CONFIG_LABEL in scripts/python/cni_constants.py and
# referenced by the nodepool generator and smoke tests. Keep in sync.
cat >/etc/eks/nodeadm.d/50-eni-config-az.yaml <<YAML
apiVersion: node.eks.aws/v1alpha1
kind: NodeConfig
spec:
  kubelet:
    flags:
      - --node-labels=ipam.osdc.internal/eni-config=${AZ}
YAML

echo "[eks-base-pre-nodeadm] wrote drop-in for AZ=${AZ}"
