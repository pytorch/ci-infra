# IRSA wiring for user-facing gpu-dev pods.
#
# Goal: when a user SSHs into their CPU dev pod (or any gpu-dev pod) and runs
# `gpu-dev submit ...`, boto3 picks up temporary AWS credentials via the
# IAM-roles-for-service-accounts mechanism — no manual `aws sso login` needed.
#
# Identity preservation: Lambda sets AWS_ROLE_SESSION_NAME=<user identity>
# on the pod env, so STS GetCallerIdentity returns
#   arn:aws:sts::<acct>:assumed-role/<role>/<user>
# and the existing `authenticate_user` ARN-tail parsing keeps working unchanged.

# Policy mirrors cli-tools/gpu-dev-cli/minimal-iam-policy.json — same scope a
# user gets when they `aws sso login` from their laptop.
resource "aws_iam_role" "gpu_dev_pod_role" {
  name = "gpu-dev-pod-role-${local.current_config.environment}"

  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Effect = "Allow"
        Principal = {
          Federated = aws_iam_openid_connect_provider.eks.arn
        }
        Action = "sts:AssumeRoleWithWebIdentity"
        Condition = {
          StringEquals = {
            "${replace(aws_iam_openid_connect_provider.eks.url, "https://", "")}:sub" = "system:serviceaccount:gpu-dev:gpu-dev-pod-sa"
            "${replace(aws_iam_openid_connect_provider.eks.url, "https://", "")}:aud" = "sts.amazonaws.com"
          }
        }
      }
    ]
  })

  tags = {
    Name        = "GPU Dev Pod IRSA Role"
    Environment = local.current_config.environment
  }
}

resource "aws_iam_role_policy" "gpu_dev_pod_policy" {
  name = "gpu-dev-pod-policy"
  role = aws_iam_role.gpu_dev_pod_role.id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Effect = "Allow"
        Action = [
          "sqs:SendMessage",
          "sqs:GetQueueUrl",
          "sqs:GetQueueAttributes"
        ]
        Resource = "arn:aws:sqs:*:*:pytorch-gpu-dev-reservation-queue"
      },
      {
        Effect = "Allow"
        Action = [
          "dynamodb:GetItem",
          "dynamodb:Query",
          "dynamodb:Scan"
        ]
        Resource = [
          "arn:aws:dynamodb:*:*:table/pytorch-gpu-dev-reservations",
          "arn:aws:dynamodb:*:*:table/pytorch-gpu-dev-reservations/index/*",
          "arn:aws:dynamodb:*:*:table/pytorch-gpu-dev-gpu-availability"
        ]
      },
      {
        Effect = "Allow"
        Action = "sts:GetCallerIdentity"
        Resource = "*"
      },
      {
        Effect = "Allow"
        Action = [
          "bedrock:InvokeModel",
          "bedrock:InvokeModelWithResponseStream"
        ]
        Resource = "*"
      }
    ]
  })
}

resource "kubernetes_service_account" "gpu_dev_pod" {
  metadata {
    name      = "gpu-dev-pod-sa"
    namespace = kubernetes_namespace.gpu_dev.metadata[0].name
    annotations = {
      "eks.amazonaws.com/role-arn" = aws_iam_role.gpu_dev_pod_role.arn
    }
  }
}
