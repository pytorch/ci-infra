# Dedicated always-on build node for the prebuilt pytorch (viable/strict) pipeline.
# m7i.48xlarge = 192 vCPU / 768 GB. RAM is the binding constraint for high-parallel
# PyTorch builds — a 56GB node OOM-kills at MAX_JOBS=24, while 768GB comfortably
# feeds ~192 parallel compile jobs. The hourly incremental build job pins here via
# nodeSelector NodeType=build (no GpuType label, so reservation/warm pods never land here).
resource "aws_launch_template" "build_launch_template" {
  name_prefix   = "${var.prefix}-build-"
  image_id      = data.aws_ami.eks_gpu_ami_x86_64.id
  key_name      = var.key_pair_name
  instance_type = "m7i.48xlarge"

  vpc_security_group_ids = [aws_security_group.gpu_dev_sg.id]

  block_device_mappings {
    device_name = "/dev/xvda"
    ebs {
      volume_size           = 1024 # build workspace + build/ ninja state + ccache + clone
      volume_type           = "gp3"
      iops                  = 8000
      throughput            = 500
      delete_on_termination = true
      encrypted             = true
    }
  }

  iam_instance_profile {
    name = aws_iam_instance_profile.eks_node_instance_profile.name
  }

  user_data = base64encode(templatefile("${path.module}/templates/al2023-cpu-user-data.sh", {
    cluster_name     = aws_eks_cluster.gpu_dev_cluster.name
    cluster_endpoint = aws_eks_cluster.gpu_dev_cluster.endpoint
    cluster_ca       = aws_eks_cluster.gpu_dev_cluster.certificate_authority[0].data
    cluster_cidr     = var.vpc_cidr
    region           = local.current_config.aws_region
    gpu_type         = "build" # -> node label NodeType=build
  }))

  tag_specifications {
    resource_type = "instance"
    tags = {
      Name        = "${var.prefix}-build-node"
      Environment = local.current_config.environment
      NodeType    = "build"
    }
  }

  tags = {
    Name        = "${var.prefix}-build-launch-template"
    Environment = local.current_config.environment
  }
}

resource "aws_autoscaling_group" "build_nodes" {
  name                      = "${var.prefix}-build-nodes"
  vpc_zone_identifier       = [aws_subnet.gpu_dev_subnet.id, aws_subnet.gpu_dev_subnet_secondary.id]
  health_check_type         = "EC2"
  health_check_grace_period = 300

  min_size         = 1
  max_size         = 1
  desired_capacity = 1

  launch_template {
    id      = aws_launch_template.build_launch_template.id
    version = "$Latest"
  }

  instance_refresh {
    strategy = "Rolling"
    preferences {
      min_healthy_percentage = 0
    }
  }

  tag {
    key                 = "Name"
    value               = "${var.prefix}-build-node"
    propagate_at_launch = true
  }

  tag {
    key                 = "kubernetes.io/cluster/${aws_eks_cluster.gpu_dev_cluster.name}"
    value               = "owned"
    propagate_at_launch = true
  }

  tag {
    key                 = "Environment"
    value               = local.current_config.environment
    propagate_at_launch = true
  }

  tag {
    key                 = "NodeType"
    value               = "build"
    propagate_at_launch = true
  }
}
