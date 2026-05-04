#!/bin/bash
# Post-deploy setup for B200 MIG split (6 full + 2 partitioned per node).
# Run ONCE after PR #77 is merged + tf applied + the new docker/lambda is live.

set -e

NS=gpu-operator
CM=default-mig-parted-config
PROFILE_NAME=b200-6full-2mig-balanced

echo "=== Checking current MIG profile in ConfigMap ==="
if kubectl -n "$NS" get configmap "$CM" -o jsonpath='{.data.config\.yaml}' | grep -q "$PROFILE_NAME:"; then
    echo "Profile $PROFILE_NAME already present — skipping ConfigMap edit"
else
    echo "Profile $PROFILE_NAME missing. Patching ConfigMap..."

    # Save current ConfigMap content
    kubectl -n "$NS" get configmap "$CM" -o yaml > /tmp/mig-config-backup.yaml
    echo "Backup saved to /tmp/mig-config-backup.yaml"

    # Append our profile under mig-configs:
    # NOTE: this is a sed-driven append. ClusterPolicy's controller MAY revert this if it
    # reconciles. If you see the profile disappear, re-run this script. If it keeps reverting,
    # we'll need to fork the ConfigMap (next iteration).
    kubectl -n "$NS" get configmap "$CM" -o jsonpath='{.data.config\.yaml}' > /tmp/mig-config.yaml

    cat >> /tmp/mig-config.yaml <<'EOF'

  # Mixed B200 split: GPUs 0-5 stay full (reservable as --gpu-type b200), GPUs 6-7 partitioned.
  # Per partitioned GPU: 2x 1g.23gb + 1x 2g.45gb + 1x 3g.90gb. Per node: 6 full + 4 small + 2 medium + 2 large.
  b200-6full-2mig-balanced:
    - device-filter: ["0x290110DE"]
      devices: [0, 1, 2, 3, 4, 5]
      mig-enabled: false
    - device-filter: ["0x290110DE"]
      devices: [6, 7]
      mig-enabled: true
      mig-devices:
        "1g.23gb": 2
        "2g.45gb": 1
        "3g.90gb": 1
EOF

    # Re-encode and patch
    kubectl -n "$NS" create configmap "$CM" --from-file=config.yaml=/tmp/mig-config.yaml --dry-run=client -o yaml \
        | kubectl -n "$NS" patch configmap "$CM" --patch-file=/dev/stdin
    echo "ConfigMap patched."
fi

echo
echo "=== Picking a B200 node to label ==="
NODE=$(kubectl get nodes -l GpuType=b200 -o jsonpath='{.items[0].metadata.name}')
if [ -z "$NODE" ]; then
    echo "No B200 nodes found. Exiting."
    exit 1
fi
echo "Will label: $NODE"
read -p "Proceed? (y/N): " CONFIRM
if [ "$CONFIRM" != "y" ]; then
    echo "Aborted."
    exit 0
fi

kubectl label node "$NODE" "nvidia.com/mig.config=$PROFILE_NAME" --overwrite
echo "Node labelled. nvidia-mig-manager will partition GPUs 6-7 (drains existing pods if any)."
echo
echo "Watch progress with:"
echo "  kubectl logs -n gpu-operator -l app=nvidia-mig-manager -f"
echo "  kubectl get node $NODE -o jsonpath='{.status.allocatable}' | jq ."
echo
echo "After ~2-5 min, allocatable should show:"
echo "  nvidia.com/gpu:           6"
echo "  nvidia.com/mig-1g.23gb:   4"
echo "  nvidia.com/mig-2g.45gb:   2"
echo "  nvidia.com/mig-3g.90gb:   2"
