#!/usr/bin/env bash
set -euo pipefail
#
# Bake a Windows GPU AMI: launch a clean g4dn instance from the EKS-optimized
# Windows AMI, install the NVIDIA WDDM 595.97 driver, snapshot, and terminate.
#
# Prereq: the g4dn vCPU quota must have a free slot (the running Windows GPU
# node group must be scaled to 0 first — the quota is 4 vCPU = 1x g4dn.xlarge).
#
# Usage:
#   bake-windows-gpu-ami.sh <base-ami-id> <subnet-id> <security-group-id> <instance-profile-name>
#
# Output: prints the new AMI ID on the last line.

BASE_AMI="${1:?base-ami-id required}"
SUBNET_ID="${2:?subnet-id required}"
SG_ID="${3:?security-group-id required}"
IAM_PROFILE="${4:?instance-profile-name required}"
REGION="${AWS_REGION:-us-east-1}"

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
DRIVER_PS1="$SCRIPT_DIR/install-gpu-driver.ps1"

echo "==> launching temp g4dn instance from $BASE_AMI"
INSTANCE_ID=$(aws ec2 run-instances \
  --image-id "$BASE_AMI" \
  --instance-type g4dn.xlarge \
  --subnet-id "$SUBNET_ID" \
  --security-group-ids "$SG_ID" \
  --iam-instance-profile "Name=$IAM_PROFILE" \
  --count 1 \
  --query 'Instances[0].InstanceId' --output text)
echo "    instance: $INSTANCE_ID"

echo "==> waiting for instance running"
aws ec2 wait instance-running --instance-ids "$INSTANCE_ID"

echo "==> waiting for SSM online"
for _ in $(seq 1 60); do
  if aws ssm describe-instance-information \
      --filters "Key=InstanceIds,Values=$INSTANCE_ID" \
      --query 'InstanceInformationList[0].PingStatus' --output text 2>/dev/null | grep -q Online; then
    echo "    SSM online"
    break
  fi
  sleep 10
done

echo "==> installing GPU driver via SSM"
python3 - "$INSTANCE_ID" "$DRIVER_PS1" <<'PYEOF'
import boto3, sys, time
inst, ps1_path = sys.argv[1], sys.argv[2]
with open(ps1_path) as f:
    script = f.read()
ssm = boto3.client('ssm', region_name='us-east-1')
r = ssm.send_command(InstanceIds=[inst], DocumentName='AWS-RunPowerShellScript',
                     Parameters={'commands': [script]}, TimeoutSeconds=900)
cid = r['Command']['CommandId']
print("    SSM command:", cid)
inv = {}
for _ in range(120):
    inv = ssm.get_command_invocation(CommandId=cid, InstanceId=inst)
    if inv['Status'] in ('Success', 'Failed', 'Cancelled', 'TimedOut'):
        break
    time.sleep(10)
print(inv.get('StandardOutputContent', ''))
print(inv.get('StandardErrorContent', ''), file=sys.stderr)
if inv['Status'] != 'Success':
    sys.exit(1)
PYEOF

echo "==> creating AMI (reboot activates the driver + clean snapshot)"
AMI_NAME="windows-gpu-59597-$(date +%Y%m%d%H%M%S)"
AMI_ID=$(aws ec2 create-image \
  --instance-id "$INSTANCE_ID" \
  --name "$AMI_NAME" \
  --description "EKS Windows 2025 + NVIDIA WDDM 595.97 pre-baked" \
  --query 'ImageId' --output text)
echo "    AMI: $AMI_ID"

echo "==> waiting for AMI available"
# The built-in `aws ec2 wait image-available` gives up after ~10m, which is too
# short for a 200GB Windows root-volume snapshot. Poll manually for up to 30m.
AMI_AVAILABLE=false
for _ in $(seq 1 60); do
  STATE=$(aws ec2 describe-images --image-ids "$AMI_ID" \
    --query 'Images[0].State' --output text 2>/dev/null || echo "pending")
  if [ "$STATE" = "available" ]; then
    AMI_AVAILABLE=true
    echo "    AMI available"
    break
  fi
  echo "    AMI state: $STATE (waiting)"
  sleep 30
done
if [ "$AMI_AVAILABLE" != "true" ]; then
  echo "ERROR: AMI $AMI_ID did not become available within 30m" >&2
  exit 1
fi

echo "==> terminating temp instance"
aws ec2 terminate-instances --instance-ids "$INSTANCE_ID" >/dev/null

echo "==> DONE"
echo "$AMI_ID"
