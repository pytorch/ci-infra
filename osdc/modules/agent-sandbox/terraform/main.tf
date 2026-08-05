# Read-only IRSA role for the aws-sigv4-proxy service account. This is the ONLY
# AWS identity in the sandbox, and it lives on the trusted proxy — never on the
# agent. Scoped to Bedrock model invocation plus read-only CloudWatch/Logs so the
# agent (via the proxy) can query metrics/logs and call an LLM, and nothing else.
#
# Created in the cluster's own state: `just deploy-module <cluster> agent-sandbox`
# runs this (terraform phase) before the k8s phase.

terraform {
  required_version = ">= 1.7"

  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 5.0"
    }
  }
}

provider "aws" {
  region = var.aws_region
}

data "terraform_remote_state" "base" {
  backend = "s3"

  config = {
    bucket         = var.state_bucket
    key            = "${var.cluster_id}/base/terraform.tfstate"
    region         = "us-west-2"
    dynamodb_table = "ciforge-terraform-locks"
  }
}

data "aws_caller_identity" "current" {}

locals {
  oidc_provider_arn = data.terraform_remote_state.base.outputs.oidc_provider_arn
  oidc_provider     = data.terraform_remote_state.base.outputs.oidc_provider

  namespace       = "ai-sandbox"
  service_account = "sigv4-proxy"

  account_id = data.aws_caller_identity.current.account_id

  # Bedrock foundation models + inference profiles in this region. Scoped to the
  # region/account; tighten to specific model IDs once the agent's model is fixed.
  bedrock_resources = [
    "arn:aws:bedrock:${var.aws_region}::foundation-model/*",
    "arn:aws:bedrock:${var.aws_region}:${local.account_id}:inference-profile/*",
  ]

  tags = {
    Cluster = var.cluster_name
    Project = "ciforge"
    Module  = "agent-sandbox"
  }
}

resource "aws_iam_role" "sigv4_proxy" {
  name = "${var.cluster_name}-agent-sandbox-sigv4-proxy"

  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Action = "sts:AssumeRoleWithWebIdentity"
      Effect = "Allow"
      Principal = {
        Federated = local.oidc_provider_arn
      }
      Condition = {
        StringEquals = {
          "${local.oidc_provider}:aud" = "sts.amazonaws.com"
          "${local.oidc_provider}:sub" = "system:serviceaccount:${local.namespace}:${local.service_account}"
        }
      }
    }]
  })

  tags = local.tags
}

resource "aws_iam_policy" "sigv4_proxy" {
  name = "${var.cluster_name}-agent-sandbox-sigv4-proxy"

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid    = "BedrockInvoke"
        Effect = "Allow"
        Action = [
          "bedrock:InvokeModel",
          "bedrock:InvokeModelWithResponseStream",
        ]
        Resource = local.bedrock_resources
      },
      {
        Sid    = "ReadOnlyObservability"
        Effect = "Allow"
        Action = [
          "cloudwatch:GetMetricData",
          "cloudwatch:GetMetricStatistics",
          "cloudwatch:ListMetrics",
          "logs:GetLogEvents",
          "logs:FilterLogEvents",
          "logs:DescribeLogGroups",
          "logs:DescribeLogStreams",
        ]
        Resource = "*"
      },
    ]
  })

  tags = local.tags
}

resource "aws_iam_role_policy_attachment" "sigv4_proxy" {
  policy_arn = aws_iam_policy.sigv4_proxy.arn
  role       = aws_iam_role.sigv4_proxy.name
}
