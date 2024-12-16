
resource "aws_iam_role" "gha_target_determinator_s3_read_write" {
  name = "gha_target_determinator_s3_read_write"

  max_session_duration = 18000
  description = "Allows PyTorch Target Determinator GHA to have read/write access to s3://target-determinator-assets/ bucket"
  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Effect = "Allow"
        Principal = {
          Federated = "arn:aws:iam::308535385114:oidc-provider/token.actions.githubusercontent.com"
        }
        Action = "sts:AssumeRoleWithWebIdentity"
        Condition = {
          StringEquals = {
            "token.actions.githubusercontent.com:aud" = "sts.amazonaws.com"
          }
          StringLike = {
            "token.actions.githubusercontent.com:sub" = "repo:pytorch/pytorch:environment:target-determinator-env"
          }
        }
      }
    ]
  })

  inline_policy {
    name = "gha_target_determinator_s3_read_write"
    policy = jsonencode({
      Version = "2012-10-17"
      Statement = [
        {
          Action   = [
            "s3:GetObject*",
            "s3:GetBucket*",
            "s3:PutObject*",
            "s3:ListBucket*",
            "s3:DeleteObject",
          ]
          Effect   = "Allow"
          Resource = [
            "arn:aws:s3:::target-determinator-assets/*",
            "arn:aws:s3:::target-determinator-assets"
          ]
        },
      ]
    })
  }

  tags = {
    project = var.ali_prod_environment
    environment = "pytorch-target-determinator-workflows"
    workflow = "target-determinator-indexer"
  }
}

# Role for using packer to create AMIs
resource "aws_iam_role" "gha-packer-role" {
  name = "gha-packer-role"

  max_session_duration = 18000
  description = "Allows PyTorch runners to run Packer to build AMIs."
  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Effect = "Allow"
        Principal = {
          Federated = "arn:aws:iam::${local.aws_account_id}:oidc-provider/token.actions.githubusercontent.com"
        }
        Action = "sts:AssumeRoleWithWebIdentity"
        Condition = {
          StringEquals = {
            "token.actions.githubusercontent.com:aud" = "sts.amazonaws.com"
          }
          StringLike = {
            "token.actions.githubusercontent.com:sub" = [
              "repo:pytorch/pytorch:environment:packer-build-env",
              "repo:pytorch/pytorch-canary:environment:packer-build-env",
              "repo:pytorch/test-infra:environment:packer-build-env"
            ]
          }
        }
      }
    ]
  })

  inline_policy {
    name = "gha-packer-policy"
    policy = jsonencode({
      Version = "2012-10-17"
      Statement = [
        {
          Effect   = "Allow"
          Action   = [
            "ec2:AssociateIamInstanceProfile",
            "ec2:AttachVolume",
            "ec2:AuthorizeSecurityGroupIngress",
            "ec2:CopyImage",
            "ec2:CreateImage",
            "ec2:CreateKeypair",
            "ec2:CreateSecurityGroup",
            "ec2:CreateSnapshot",
            "ec2:CreateTags",
            "ec2:CreateVolume",
            "ec2:DeleteKeyPair",
            "ec2:DeleteSecurityGroup",
            "ec2:DeleteSnapshot",
            "ec2:DeleteVolume",
            "ec2:DeregisterImage",
            "ec2:DescribeImageAttribute",
            "ec2:DescribeImages",
            "ec2:DescribeInstances",
            "ec2:DescribeInstanceStatus",
            "ec2:DescribeRegions",
            "ec2:DescribeSecurityGroups",
            "ec2:DescribeSnapshots",
            "ec2:DescribeSubnets",
            "ec2:DescribeTags",
            "ec2:DescribeVolumes",
            "ec2:DetachVolume",
            "ec2:GetPasswordData",
            "ec2:ModifyImageAttribute",
            "ec2:ModifyInstanceAttribute",
            "ec2:ModifySnapshotAttribute",
            "ec2:RegisterImage",
            "ec2:ReplaceIamInstanceProfileAssociation",
            "ec2:RunInstances",
            "ec2:StopInstances",
            "ec2:TerminateInstances",
            "iam:PassRole",
            "iam:GetInstanceProfile"
          ]
          Resource = [
            "*",
          ]
        },
      ]
    })
  }

  tags = {
    project = var.ali_prod_environment
    environment = "pytorch-packer-workflows"
    workflow = "build-windows-ami"
  }
}
