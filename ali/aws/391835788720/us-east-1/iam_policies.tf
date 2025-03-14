resource "aws_iam_role" "ossci_gha_terraform" {
  name = "ossci_gha_terraform"

  max_session_duration = 18000
  description = "used by pytorch/ci-infra workflows to deploy terraform configs"
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
            "token.actions.githubusercontent.com:sub" = "repo:pytorch/ci-infra:*"
          }
        }
      }
    ]
  })

  tags = {
    project = var.ali_prod_environment
    environment = "${var.ali_prod_environment}-workflows"
  }
}

resource "aws_iam_role_policy_attachment" "ossci_gha_terraform_admin" {
  role       = aws_iam_role.ossci_gha_terraform.name
  #checkov:skip=CKV_AWS_274:Terraform needs AdministratorAccess to run
  policy_arn = "arn:aws:iam::aws:policy/AdministratorAccess"
}

// Taken from https://docs.aws.amazon.com/AmazonECR/latest/userguide/ecr_managed_policies.html
resource "aws_iam_policy" "allow_ecr_on_gha_runners" {
  name        = "${var.ali_prod_environment}_allow_ecr_on_gha_runners"
  description = "Allows ECR to be accessed by our GHA EC2 runners"
  policy      = <<EOT
{
    "Version": "2012-10-17",
    "Statement": [{
        "Effect": "Allow",
        "Action": [
            "ecr:BatchCheckLayerAvailability",
            "ecr:BatchGetImage",
            "ecr:CompleteLayerUpload",
            "ecr:DescribeImageScanFindings",
            "ecr:DescribeImages",
            "ecr:DescribeRepositories",
            "ecr:GetAuthorizationToken",
            "ecr:GetDownloadUrlForLayer",
            "ecr:GetLifecyclePolicy",
            "ecr:GetLifecyclePolicyPreview",
            "ecr:GetRepositoryPolicy",
            "ecr:InitiateLayerUpload",
            "ecr:ListImages",
            "ecr:ListTagsForResource",
            "ecr:PutImage",
            "ecr:UploadLayerPart"
        ],
        "Resource": "*"
    }]
}
EOT
}

resource "aws_iam_policy" "allow_secretmanager_docker_hub_token_on_gha_runners" {
  name        = "${var.ali_prod_environment}_allow_secretmanager_docker_hub_token_on_gha_runners"
  description = "Allows our GHA EC2 runners access to the read-only docker.io token"
  policy      = <<EOT
{
    "Version": "2012-10-17",
    "Statement": [{
        "Effect": "Allow",
        "Action": [
            "secretsmanager:GetSecretValue"
        ],
        "Resource": "arn:aws:secretsmanager:us-east-1:391835788720:secret:docker_hub_readonly_token-V74gSU"
    }]
}
EOT
}

// ossci-compiler-cache-circleci-v2 = linux sccache
// ossci-compiler-cache = windows sccache
resource "aws_iam_policy" "allow_s3_sccache_access_on_gha_runners" {
  name        = "${var.ali_prod_environment}_allow_s3_sccache_access_on_gha_runners"
  description = "Allows S3 bucket access for sccache for GHA EC2 runners"
  policy      = <<EOT
{
    "Version": "2012-10-17",
    "Statement": [
        {
            "Sid": "ListObjectsInBucketLinuxXLA",
            "Effect": "Allow",
            "Action": ["s3:ListBucket"],
            "Resource": ["arn:aws:s3:::ossci-compiler-clang-cache-circleci-xla"]
        },
        {
            "Sid": "AllObjectActionsLinuxXLA",
            "Effect": "Allow",
            "Action": "s3:*Object",
            "Resource": ["arn:aws:s3:::ossci-compiler-clang-cache-circleci-xla/*"]
        },
        {
            "Sid": "ListObjectsInBucketLinux",
            "Effect": "Allow",
            "Action": ["s3:ListBucket"],
            "Resource": ["arn:aws:s3:::ossci-compiler-cache-circleci-v2"]
        },
        {
            "Sid": "AllObjectActionsLinux",
            "Effect": "Allow",
            "Action": "s3:*Object",
            "Resource": ["arn:aws:s3:::ossci-compiler-cache-circleci-v2/*"]
        },
        {
            "Sid": "ListObjectsInBucketWindows",
            "Effect": "Allow",
            "Action": ["s3:ListBucket"],
            "Resource": ["arn:aws:s3:::ossci-compiler-cache"]
        },
        {
            "Sid": "AllObjectActionsWindows",
            "Effect": "Allow",
            "Action": "s3:*Object",
            "Resource": ["arn:aws:s3:::ossci-compiler-cache/*"]
        },
        {
            "Sid": "ListObjectsInBucketECRBackup",
            "Effect": "Allow",
            "Action": ["s3:ListBucket"],
            "Resource": ["arn:aws:s3:::ossci-linux-build"]
        },
        {
            "Sid": "AllObjectActionsECRBackup",
            "Effect": "Allow",
            "Action": "s3:*Object",
            "Resource": ["arn:aws:s3:::ossci-linux-build/*"]
        },
        {
            "Sid": "ListObjectsInBucketGHAArtifacts",
            "Effect": "Allow",
            "Action": ["s3:ListBucket"],
            "Resource": ["arn:aws:s3:::gha-artifacts"]
        },
        {
            "Sid": "AllObjectActionsGHAArtifacts",
            "Effect": "Allow",
            "Action": "s3:*",
            "Resource": ["arn:aws:s3:::gha-artifacts/*"]
        },
        {
            "Sid": "AllObjectActionsOssciMetrics",
            "Effect": "Allow",
            "Action": "s3:*Object",
            "Resource": ["arn:aws:s3:::ossci-metrics/*"]
        },
        {
            "Sid": "AllObjectActionsOssciRawJobStatus",
            "Effect": "Allow",
            "Action": "s3:*Object",
            "Resource": ["arn:aws:s3:::ossci-raw-job-status/*"]
        },
        {
            "Sid": "AllObjectActionsTorchciAggregatedStats",
            "Effect": "Allow",
            "Action": "s3:*Object",
            "Resource": ["arn:aws:s3:::torchci-aggregated-stats/*"]
        },
        {
            "Sid": "AllObjectActionsContributionData",
            "Effect": "Allow",
            "Action": "s3:*Object",
            "Resource": ["arn:aws:s3:::torchci-contribution-data/*"]
        },
        {
            "Sid": "AllObjectActionsAlerts",
            "Effect": "Allow",
            "Action": "s3:*Object",
            "Resource": ["arn:aws:s3:::torchci-alerts/*"]
        },
        {
            "Sid": "ListObjectsInBucketDocPreviews",
            "Effect": "Allow",
            "Action": ["s3:ListBucket"],
            "Resource": ["arn:aws:s3:::doc-previews"]
        },
        {
            "Sid": "AllObjectActionsDocPreviews",
            "Effect": "Allow",
            "Action": "s3:*Object",
            "Resource": ["arn:aws:s3:::doc-previews/*"]
        },
        {
            "Sid": "AllObjectActionsTutorialsPR",
            "Effect": "Allow",
            "Action": "s3:*Object*",
            "Resource": ["arn:aws:s3:::pytorch-tutorial-build-pull-request/*"]
        },
        {
            "Sid": "ReadTargetDeterminatorAssets",
            "Effect": "Allow",
            "Action": [
                "s3:GetObject*",
                "s3:GetBucket*",
                "s3:ListBucket*"
            ],
            "Resource": [
                "arn:aws:s3:::target-determinator-assets/*",
                "arn:aws:s3:::target-determinator-assets"
            ]
        },
        {
            "Sid": "ReadListOssciWindowsAssets",
            "Effect": "Allow",
            "Action": [
                "s3:Get*",
                "s3:ListBucket*"
            ],
            "Resource": [
                "arn:aws:s3:::ossci-windows/*"
            ]
        },
        {
            "Sid": "AllObjectActionsOssciBenchmarks",
            "Effect": "Allow",
            "Action": "s3:*Object",
            "Resource": ["arn:aws:s3:::ossci-benchmarks/*"]
        },
        {
            "Sid": "AllObjectActionsOssciUtilization",
            "Effect": "Allow",
            "Action": "s3:*Object",
            "Resource": ["arn:aws:s3:::ossci-utilization/*"]
        }
    ]
}
EOT
}

resource "aws_iam_policy" "allow_lambda_on_gha_runners" {
  name        = "${var.ali_prod_environment}_allow_lambda_on_gha_runners"
  description = "Allows some lambda to be invoked by our GHA EC2 runners"
  policy      = <<EOT
{
    "Version": "2012-10-17",
    "Statement": [
        {
            "Sid": "AllowGHARunnersToScribeProxyLambda",
            "Effect": "Allow",
            "Action": "lambda:InvokeFunction",
            "Resource": "arn:aws:lambda:us-east-1::function:gh-ci-scribe-proxy"
        },
        {
            "Sid": "AllowGHARunnersToRDSLambda",
            "Effect": "Allow",
            "Action": "lambda:InvokeFunction",
            "Resource": "arn:aws:lambda:us-east-1::function:rds-proxy"
        }
    ]
}
EOT
}

resource "aws_iam_policy" "allow_torchci_metrics_on_gha_runners" {
  name        = "${var.ali_prod_environment}_allow_torchci_metrics_on_gha_runners"
  description = "Allows TorchCI Metrics to be modifiable by our GHA EC2 runners"
  policy      = <<EOT
{
    "Version": "2012-10-17",
    "Statement": [
        {
            "Sid": "AllowUploadingTorchciMetrics",
            "Effect": "Allow",
            "Action": [
                "dynamodb:GetItem",
                "dynamodb:PutItem",
                "dynamodb:UpdateItem",
                "dynamodb:DeleteItem",
                "dynamodb:Scan",
                "dynamodb:Query"
            ],
            "Resource": "arn:aws:dynamodb:*:308535385114:table/torchci-metrics"
        }
    ]
}
EOT
}
