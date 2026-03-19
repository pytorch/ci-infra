/*
 * RDS PostgreSQL instance for gharts (GitHub Actions Runner Token Service).
 *
 * Placed in the private subnets of the cluster VPC. Access is restricted to
 * within the VPC CIDR so only pods running on the cluster can connect.
 *
 * Authentication uses IAM RDS auth — no static password is required.
 * The gharts service account is bound to an IAM role (IRSA) that carries the
 * rds-db:connect permission.
 */

# Subnet group covering all private subnets in the VPC
resource "aws_db_subnet_group" "gharts" {
  name       = "${var.arc_dev_environment}-gharts"
  subnet_ids = module.arc_runners_vpc.private_subnets

  tags = {
    Environment = var.arc_dev_environment
    Project     = var.arc_dev_environment
  }
}

# Allow inbound PostgreSQL from within the VPC (EKS nodes and pods)
resource "aws_security_group" "gharts_rds" {
  name        = "${var.arc_dev_environment}-gharts-rds"
  description = "Allow PostgreSQL access from EKS nodes"
  vpc_id      = module.arc_runners_vpc.vpc_id

  ingress {
    description = "PostgreSQL from VPC"
    from_port   = 5432
    to_port     = 5432
    protocol    = "tcp"
    cidr_blocks = [module.arc_runners_vpc.vpc_cidr_block]
  }

  egress {
    from_port   = 0
    to_port     = 0
    protocol    = "-1"
    cidr_blocks = ["0.0.0.0/0"]
  }

  tags = {
    Environment = var.arc_dev_environment
    Project     = var.arc_dev_environment
  }
}

resource "aws_db_instance" "gharts" {
  identifier        = "${var.arc_dev_environment}-gharts"
  engine            = "postgres"
  engine_version    = "16.13"
  instance_class    = "db.t3.micro"
  allocated_storage = 20
  storage_type      = "gp3"
  storage_encrypted = true

  db_name  = "gharts"
  username = "gharts"

  # IAM authentication — no static password needed
  iam_database_authentication_enabled = true
  manage_master_user_password         = true

  db_subnet_group_name   = aws_db_subnet_group.gharts.name
  vpc_security_group_ids = [aws_security_group.gharts_rds.id]

  # Dev instance — no Multi-AZ
  multi_az                = false
  backup_retention_period = 1
  skip_final_snapshot     = true
  deletion_protection     = true

  tags = {
    Environment = var.arc_dev_environment
    Project     = var.arc_dev_environment
  }
}

# IAM policy granting rds-db:connect for the gharts DB user
resource "aws_iam_policy" "gharts_rds_connect" {
  name = "${var.arc_dev_environment}-gharts-rds-connect"

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect   = "Allow"
      Action   = "rds-db:connect"
      Resource = "arn:aws:rds-db:${local.aws_region}:${local.aws_account_id}:dbuser:${aws_db_instance.gharts.resource_id}/gharts"
    }]
  })
}

# IAM role for the gharts service account (IRSA)
resource "aws_iam_role" "gharts" {
  name = "${var.arc_dev_environment}-gharts"

  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect = "Allow"
      Principal = {
        Federated = module.pytorch_arc_dev_eks.oidc_provider_arn
      }
      Action = "sts:AssumeRoleWithWebIdentity"
      Condition = {
        StringEquals = {
          "${replace(module.pytorch_arc_dev_eks.cluster_oidc_issuer_url, "https://", "")}:aud" = "sts.amazonaws.com"
          "${replace(module.pytorch_arc_dev_eks.cluster_oidc_issuer_url, "https://", "")}:sub" = "system:serviceaccount:gharts:gharts"
        }
      }
    }]
  })
}

resource "aws_iam_role_policy_attachment" "gharts_rds_connect" {
  policy_arn = aws_iam_policy.gharts_rds_connect.arn
  role       = aws_iam_role.gharts.name
}
