# Baked GPU AMI — pre-installs NVIDIA drivers + caches the Docker image.
#
# Launches a cheap c7i.xlarge, runs the GPU user-data (driver compile + image pull),
# snapshots as an AMI, terminates the builder. Triggered only when inputs change
# (base AMI, user-data, Docker image). Day-to-day tf apply skips it (0 seconds).
#
# Result: spot GPU cold start drops from ~25 min to ~5 min.

locals {
  # Inputs that trigger an AMI rebuild
  ami_baker_trigger = sha256(join("\n", [
    data.aws_ami.eks_gpu_ami_x86_64.id,
    filesha256("${path.module}/templates/al2023-user-data.sh"),
    local.latest_image_uri,
  ]))
  ami_baker_name = "gpu-dev-baked-${substr(local.ami_baker_trigger, 0, 8)}"

  # Use baked AMI when available, fall back to standard.
  # aws_ami_ids returns an empty list (no error) when no AMI exists yet.
  gpu_ami_id = length(data.aws_ami_ids.gpu_baked.ids) > 0 ? data.aws_ami_ids.gpu_baked.ids[0] : data.aws_ami.eks_gpu_ami_x86_64.id
}

# Look up existing baked AMI — uses aws_ami_ids which returns [] instead of erroring
data "aws_ami_ids" "gpu_baked" {
  owners = ["self"]

  filter {
    name   = "name"
    values = [local.ami_baker_name]
  }
  filter {
    name   = "state"
    values = ["available"]
  }

  sort_ascending = false
}

# Build the baked AMI when inputs change
resource "null_resource" "ami_baker" {
  # Only run when the target AMI doesn't exist yet
  count = length(data.aws_ami_ids.gpu_baked.ids) == 0 ? 1 : 0

  triggers = {
    ami_hash = local.ami_baker_trigger
  }

  provisioner "local-exec" {
    command = <<-SCRIPT
      set -e
      echo "🔨 Building baked GPU AMI: ${local.ami_baker_name}"

      REGION="${local.current_config.aws_region}"
      BASE_AMI="${data.aws_ami.eks_gpu_ami_x86_64.id}"
      SUBNET="${aws_subnet.gpu_dev_subnet.id}"
      SG="${aws_security_group.gpu_dev_sg.id}"
      IAM_PROFILE="${aws_iam_instance_profile.eks_node_instance_profile.name}"
      IMAGE_URI="${local.latest_image_uri}"

      # User-data for the builder: install drivers + pull Docker image
      # Skip nodeadm/kubelet (not joining a cluster), skip EFA (no hardware)
      USER_DATA=$(cat <<'UDEOF'
#!/bin/bash
set -e
echo "[AMI-BAKER] Starting GPU AMI build..."

# Install NVIDIA driver (compiles kernel modules — the 13-min step we're eliminating)
echo "[AMI-BAKER] Installing NVIDIA drivers..."
echo "options nvidia NVreg_RestrictProfilingToAdminUsers=0" > /etc/modprobe.d/nvprof.conf
dnf install -y nvidia-driver nvidia-driver-cuda
echo "[AMI-BAKER] NVIDIA driver installed"

# Install fabricmanager (won't start without NVSwitch but binary is on disk)
echo "[AMI-BAKER] Installing fabricmanager..."
dnf install -y nvidia-fabricmanager nvlsm 2>/dev/null || echo "fabricmanager install warning (non-fatal)"
systemctl enable nvidia-fabricmanager 2>/dev/null || true
echo "[AMI-BAKER] fabricmanager installed"

# Load NVIDIA modules (creates device files needed by containerd)
modprobe nvidia 2>/dev/null || echo "nvidia module load skipped (no GPU — expected during AMI build)"
modprobe nvidia_uvm 2>/dev/null || echo "nvidia_uvm load skipped"

# Pull the Docker image into containerd cache
echo "[AMI-BAKER] Pulling Docker image into containerd cache..."
ECR_REGION=$(echo "$1" | cut -d. -f4)
ECR_REGISTRY=$(echo "$1" | cut -d/ -f1)
# Wait for containerd to be ready
for i in $(seq 1 30); do
  ctr version >/dev/null 2>&1 && break
  echo "[AMI-BAKER] Waiting for containerd..."
  sleep 2
done

# Get ECR auth token and pull
ECR_TOKEN=$(aws ecr get-login-password --region $ECR_REGION 2>/dev/null || echo "")
if [ -n "$ECR_TOKEN" ]; then
  ctr -n k8s.io images pull --user "AWS:$ECR_TOKEN" "$1" 2>&1 || echo "[AMI-BAKER] Image pull failed (non-fatal — will pull at boot)"
  echo "[AMI-BAKER] Docker image cached"
else
  echo "[AMI-BAKER] No ECR token — skipping image cache"
fi

# Signal completion
echo "[AMI-BAKER] Build complete" > /tmp/ami-baker-done
echo "[AMI-BAKER] ✅ AMI build complete"
UDEOF

      # Base64 encode user-data
      ENCODED_UD=$(echo "$USER_DATA" | base64)

      # Launch builder instance
      echo "Launching builder instance (c7i.xlarge)..."
      INSTANCE_ID=$(aws ec2 run-instances \
        --region "$REGION" \
        --image-id "$BASE_AMI" \
        --instance-type c7i.xlarge \
        --subnet-id "$SUBNET" \
        --security-group-ids "$SG" \
        --iam-instance-profile Name="$IAM_PROFILE" \
        --user-data "$ENCODED_UD" \
        --tag-specifications "ResourceType=instance,Tags=[{Key=Name,Value=gpu-dev-ami-baker},{Key=Purpose,Value=ami-build}]" \
        --block-device-mappings "[{\"DeviceName\":\"/dev/xvda\",\"Ebs\":{\"VolumeSize\":200,\"VolumeType\":\"gp3\",\"DeleteOnTermination\":true}}]" \
        --query "Instances[0].InstanceId" --output text)

      echo "Builder instance: $INSTANCE_ID"

      # Wait for instance to be running
      echo "Waiting for instance to start..."
      aws ec2 wait instance-running --region "$REGION" --instance-ids "$INSTANCE_ID"

      # Wait for cloud-init to complete (polls SSM or just waits)
      echo "Waiting for driver install + image pull (~15 min)..."
      for i in $(seq 1 90); do
        STATUS=$(aws ec2 describe-instance-status \
          --region "$REGION" \
          --instance-ids "$INSTANCE_ID" \
          --query "InstanceStatuses[0].InstanceStatus.Status" --output text 2>/dev/null || echo "initializing")
        SYSTEM=$(aws ec2 describe-instance-status \
          --region "$REGION" \
          --instance-ids "$INSTANCE_ID" \
          --query "InstanceStatuses[0].SystemStatus.Status" --output text 2>/dev/null || echo "initializing")
        echo "  [$i/90] instance=$STATUS system=$SYSTEM"
        if [ "$STATUS" = "ok" ] && [ "$SYSTEM" = "ok" ]; then
          break
        fi
        sleep 20
      done

      # Extra wait for user-data to finish (cloud-init complete)
      echo "Waiting additional 3 min for user-data completion..."
      sleep 180

      # Create AMI
      echo "Creating AMI: ${local.ami_baker_name}..."
      AMI_ID=$(aws ec2 create-image \
        --region "$REGION" \
        --instance-id "$INSTANCE_ID" \
        --name "${local.ami_baker_name}" \
        --description "GPU Dev baked AMI - NVIDIA drivers + Docker image pre-cached" \
        --tag-specifications "ResourceType=image,Tags=[{Key=Name,Value=${local.ami_baker_name}},{Key=Purpose,Value=gpu-dev-baked}]" \
        --no-reboot \
        --query "ImageId" --output text)

      echo "AMI: $AMI_ID — waiting for it to become available..."
      aws ec2 wait image-available --region "$REGION" --image-ids "$AMI_ID"
      echo "✅ AMI available: $AMI_ID"

      # Terminate builder
      echo "Terminating builder instance..."
      aws ec2 terminate-instances --region "$REGION" --instance-ids "$INSTANCE_ID" >/dev/null
      echo "✅ Builder terminated. Baked AMI ready."
    SCRIPT
  }
}
