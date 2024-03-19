resource "aws_iam_role" "karpenter_node_role" {
  name = "KarpenterNodeRole-${local.cluster_name}"

  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Action = "sts:AssumeRole"
        Effect = "Allow"
        Principal = {
          Service = "ec2.amazonaws.com"
        }
      },
    ]
  })

  inline_policy {
    name = "KarpenterNodeRole-${local.cluster_name}-inline-policy"
    policy = jsonencode({
      Version = "2012-10-17"
      Statement = [
        {
          Effect = "Allow"
          Action = [
            "secretsmanager:GetResourcePolicy",
            "secretsmanager:GetSecretValue",
            "secretsmanager:DescribeSecret",
            "secretsmanager:ListSecretVersionIds"
          ]
          Resource = [
            resource.aws_secretsmanager_secret.pytorch_internal_docker_registry_auth.arn
          ]
        },
        {
          Effect = "Allow"
          Action = [
            "secretsmanager:BatchGetSecretValue",
            "secretsmanager:ListSecrets"
          ]
          Resource = "*"
        },
        {
            "Action": [
                "ecr:GetAuthorizationToken"
            ],
            "Effect": "Allow",
            "Resource": "*"
        }
      ]
    })
  }

  tags = {
    Project     = "runners-eks"
    Environment = var.environment
    Context     = "${var.environment}-runners-eks-${var.aws_vpc_suffix}"
  }
}

resource "aws_iam_policy" "karpenter_node_role_access_other_accounts_resouces_policy" {
  name        = "KarpenterControllerRole-${local.cluster_name}"
  path        = "/"
  description = "Policy for Karpenter Controller"

  # policy copied from the other account :)
  policy = <<EOT
{
  "Version": "2012-10-17",
  "Statement": [
    {
        "Sid": "githubActionDist",
        "Effect": "Allow",
        "Action": [
            "s3:GetObject",
            "s3:GetObjectAcl"
        ],
        "Resource": [
            "arn:aws:s3:::gh-ci-dist-4qifyecn3hpj4fxctb1tsjmo/*"
        ]
    },
    {
        "Effect": "Allow",
        "Action": [
            "s3:ListBucket",
            "s3:GetBucketLocation"
        ],
        "Resource": "arn:aws:s3:::gh-ci-dist-4qifyecn3hpj4fxctb1tsjmo"
    },
    {
        "Action": [
            "ecr:BatchCheckLayerAvailability",
            "ecr:BatchGetImage",
            "ecr:CompleteLayerUpload",
            "ecr:DescribeImages",
            "ecr:DescribeRepositories",
            "ecr:GetDownloadUrlForLayer",
            "ecr:InitiateLayerUpload",
            "ecr:ListImages",
            "ecr:PutImage",
            "ecr:UploadLayerPart",
            "ecr:GetAuthorizationToken"
        ],
        "Effect": "Allow",
        "Resource": ["arn:aws:ecr:us-east-1:308535385114:repository/pytorch/*"]
    },
    {
        "Action": [
            "s3:ListBucket"
        ],
        "Effect": "Allow",
        "Resource": [
            "arn:aws:s3:::ossci-compiler-clang-cache-circleci-xla"
        ],
        "Sid": "ListObjectsInBucketLinuxXLA"
    },
    {
        "Action": "s3:*Object",
        "Effect": "Allow",
        "Resource": [
            "arn:aws:s3:::ossci-compiler-clang-cache-circleci-xla/*"
        ],
        "Sid": "AllObjectActionsLinuxXLA"
    },
    {
        "Action": [
            "s3:ListBucket"
        ],
        "Effect": "Allow",
        "Resource": [
            "arn:aws:s3:::ossci-compiler-cache-circleci-v2"
        ],
        "Sid": "ListObjectsInBucketLinux"
    },
    {
        "Action": "s3:*Object",
        "Effect": "Allow",
        "Resource": [
            "arn:aws:s3:::ossci-compiler-cache-circleci-v2/*"
        ],
        "Sid": "AllObjectActionsLinux"
    },
    {
        "Action": [
            "s3:ListBucket"
        ],
        "Effect": "Allow",
        "Resource": [
            "arn:aws:s3:::ossci-compiler-cache"
        ],
        "Sid": "ListObjectsInBucketWindows"
    },
    {
        "Action": "s3:*Object",
        "Effect": "Allow",
        "Resource": [
            "arn:aws:s3:::ossci-compiler-cache/*"
        ],
        "Sid": "AllObjectActionsWindows"
    },
    {
        "Action": [
            "s3:ListBucket"
        ],
        "Effect": "Allow",
        "Resource": [
            "arn:aws:s3:::ossci-linux-build"
        ],
        "Sid": "ListObjectsInBucketECRBackup"
    },
    {
        "Action": "s3:*Object",
        "Effect": "Allow",
        "Resource": [
            "arn:aws:s3:::ossci-linux-build/*"
        ],
        "Sid": "AllObjectActionsECRBackup"
    },
    {
        "Action": [
            "s3:ListBucket"
        ],
        "Effect": "Allow",
        "Resource": [
            "arn:aws:s3:::gha-artifacts"
        ],
        "Sid": "ListObjectsInBucketGHAArtifacts"
    },
    {
        "Action": "s3:*",
        "Effect": "Allow",
        "Resource": [
            "arn:aws:s3:::gha-artifacts/*"
        ],
        "Sid": "AllObjectActionsGHAArtifacts"
    },
    {
        "Action": "s3:*Object",
        "Effect": "Allow",
        "Resource": [
            "arn:aws:s3:::ossci-metrics/*"
        ],
        "Sid": "AllObjectActionsOssciMetrics"
    },
    {
        "Action": "s3:*Object",
        "Effect": "Allow",
        "Resource": [
            "arn:aws:s3:::ossci-raw-job-status/*"
        ],
        "Sid": "AllObjectActionsOssciRawJobStatus"
    },
    {
        "Action": "s3:*Object",
        "Effect": "Allow",
        "Resource": [
            "arn:aws:s3:::torchci-aggregated-stats/*"
        ],
        "Sid": "AllObjectActionsTorchciAggregatedStats"
    },
    {
        "Action": "s3:*Object",
        "Effect": "Allow",
        "Resource": [
            "arn:aws:s3:::torchci-contribution-data/*"
        ],
        "Sid": "AllObjectActionsContributionData"
    },
    {
        "Action": "s3:*Object",
        "Effect": "Allow",
        "Resource": [
            "arn:aws:s3:::torchci-alerts/*"
        ],
        "Sid": "AllObjectActionsAlerts"
    },
    {
        "Action": [
            "s3:ListBucket"
        ],
        "Effect": "Allow",
        "Resource": [
            "arn:aws:s3:::doc-previews"
        ],
        "Sid": "ListObjectsInBucketDocPreviews"
    },
    {
        "Action": "s3:*Object",
        "Effect": "Allow",
        "Resource": [
            "arn:aws:s3:::doc-previews/*"
        ],
        "Sid": "AllObjectActionsDocPreviews"
    },
    {
        "Action": "s3:*Object*",
        "Effect": "Allow",
        "Resource": [
            "arn:aws:s3:::pytorch-tutorial-build-pull-request/*"
        ],
        "Sid": "AllObjectActionsTutorialsPR"
    },
    {
        "Effect": "Allow",
        "Action": [
            "ec2:CreateTags"
        ],
        "Resource": "*"
    }
  ]
}
EOT
}

resource "aws_iam_role_policy_attachment" "karpenter_node_role_anothers_accounts_resources" {
  role       = aws_iam_role.karpenter_node_role.name
  policy_arn = aws_iam_policy.karpenter_node_role_access_other_accounts_resouces_policy.arn
}

resource "aws_iam_role_policy_attachment" "karpenter_node_role_AmazonEKSWorkerNodePolicy" {
  role       = aws_iam_role.karpenter_node_role.name
  policy_arn = "arn:aws:iam::aws:policy/AmazonEKSWorkerNodePolicy"
}

resource "aws_iam_role_policy_attachment" "karpenter_node_role_AmazonEKS_CNI_Policy" {
  role       = aws_iam_role.karpenter_node_role.name
  policy_arn = "arn:aws:iam::aws:policy/AmazonEKS_CNI_Policy"
}

resource "aws_iam_role_policy_attachment" "karpenter_node_role_AmazonEC2ContainerRegistryReadOnly" {
  role       = aws_iam_role.karpenter_node_role.name
  policy_arn = "arn:aws:iam::aws:policy/AmazonEC2ContainerRegistryReadOnly"
}

resource "aws_iam_role_policy_attachment" "karpenter_node_role_AmazonSSMManagedInstanceCore" {
  role       = aws_iam_role.karpenter_node_role.name
  policy_arn = "arn:aws:iam::aws:policy/AmazonSSMManagedInstanceCore"
}

resource "aws_iam_role" "karpenter_controler_role" {
  name = "KarpenterControllerRole-${local.cluster_name}"

  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Effect = "Allow"
        Principal = {
          Federated = "arn:aws:iam::${var.aws_account_id}:oidc-provider/${module.eks.oidc_provider}"
        }
        Action = "sts:AssumeRoleWithWebIdentity"
        Condition = {
          StringEquals = {
            "${module.eks.oidc_provider}:aud" = "sts.amazonaws.com"
            "${module.eks.oidc_provider}:sub" = "system:serviceaccount:karpenter:karpenter"
          }
        }
      },
    ]
  })

  tags = {
    Project     = "runners-eks"
    Environment = var.environment
    Context     = local.cluster_name
  }
}

resource "aws_iam_policy" "karpenter_controler_policy" {
  name        = "KarpenterControllerRole-${local.cluster_name}"
  path        = "/"
  description = "Policy for Karpenter Controller"

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Effect   = "Allow"
        Resource = "*"
        Sid      = "Karpenter"
        Action   = [
            "ec2:CreateFleet",
            "ec2:CreateLaunchTemplate",
            "ec2:CreateTags",
            "ec2:DeleteLaunchTemplate",
            "ec2:DescribeAvailabilityZones",
            "ec2:DescribeImages",
            "ec2:DescribeInstances",
            "ec2:DescribeInstanceTypeOfferings",
            "ec2:DescribeInstanceTypes",
            "ec2:DescribeLaunchTemplates",
            "ec2:DescribeSecurityGroups",
            "ec2:DescribeSpotPriceHistory",
            "ec2:DescribeSubnets",
            "ec2:RunInstances",
            "ec2:TerminateInstances",
            "iam:AddRoleToInstanceProfile",
            "iam:CreateInstanceProfile",
            "iam:DeleteInstanceProfile",
            "iam:GetInstanceProfile",
            "iam:RemoveRoleFromInstanceProfile",
            "iam:TagInstanceProfile",
            "pricing:GetProducts",
            "ssm:GetParameter",
        ]
      },
      {
        Effect   = "Allow"
        Resource = aws_sqs_queue.terraform_queue.arn
        Sid      = "AllowInterruptionQueueActions"
        Action   = [
            "sqs:ChangeMessageVisibility",
            "sqs:DeleteMessage",
            "sqs:GetQueueAttributes",
            "sqs:GetQueueUrl",
            "sqs:ReceiveMessage",
        ]
      },
      {
        Effect   = "Allow"
        Action   = "iam:PassRole"
        Resource = aws_iam_role.karpenter_node_role.arn
        Sid      = "PassNodeIAMRole"
      },
      {
        Effect = "Allow"
        Action = "eks:DescribeCluster"
        Resource = module.eks.cluster_arn
        Sid      = "EKSClusterEndpointLookup"
      },
    ]
  })
}

resource "aws_iam_role_policy_attachment" "test-attach" {
  role       = aws_iam_role.karpenter_controler_role.name
  policy_arn = aws_iam_policy.karpenter_controler_policy.arn
}
