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

  ami_baker_user_data = base64encode(templatefile("${path.module}/templates/ami-baker-user-data.sh", {
    image_uri = local.latest_image_uri
  }))

  # Use baked AMI when available (checked AFTER baker runs), fall back to standard.
  gpu_ami_id = length(data.aws_ami_ids.gpu_baked_resolved.ids) > 0 ? data.aws_ami_ids.gpu_baked_resolved.ids[0] : data.aws_ami.eks_gpu_ami_x86_64.id
}

# Pre-build check: does the baked AMI already exist? Controls whether baker runs.
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

# Post-build lookup: re-reads AFTER the baker finishes, so a freshly built AMI
# is picked up in the same apply (no second apply needed).
data "aws_ami_ids" "gpu_baked_resolved" {
  depends_on = [null_resource.ami_baker]
  owners     = ["self"]

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
      echo "Building baked GPU AMI: ${local.ami_baker_name}"

      REGION="${local.current_config.aws_region}"
      BASE_AMI="${data.aws_ami.eks_gpu_ami_x86_64.id}"
      SUBNET="${aws_subnet.gpu_dev_subnet.id}"
      SG="${aws_security_group.gpu_dev_sg.id}"
      IAM_PROFILE="${aws_iam_instance_profile.eks_node_instance_profile.name}"

      # Launch builder instance
      echo "Launching builder instance (c7i.xlarge)..."
      INSTANCE_ID=$(aws ec2 run-instances \
        --region "$REGION" \
        --image-id "$BASE_AMI" \
        --instance-type c7i.xlarge \
        --subnet-id "$SUBNET" \
        --security-group-ids "$SG" \
        --iam-instance-profile Name="$IAM_PROFILE" \
        --user-data "${local.ami_baker_user_data}" \
        --tag-specifications "ResourceType=instance,Tags=[{Key=Name,Value=gpu-dev-ami-baker},{Key=Purpose,Value=ami-build}]" \
        --block-device-mappings '[{"DeviceName":"/dev/xvda","Ebs":{"VolumeSize":200,"VolumeType":"gp3","DeleteOnTermination":true}}]' \
        --query "Instances[0].InstanceId" --output text)

      echo "Builder instance: $INSTANCE_ID"

      # Wait for instance to be running
      echo "Waiting for instance to start..."
      aws ec2 wait instance-running --region "$REGION" --instance-ids "$INSTANCE_ID"

      # Wait for cloud-init to complete
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
      echo "AMI available: $AMI_ID"

      # Terminate builder
      echo "Terminating builder instance..."
      aws ec2 terminate-instances --region "$REGION" --instance-ids "$INSTANCE_ID" >/dev/null
      echo "Builder terminated. Baked AMI ready."
    SCRIPT
  }
}
