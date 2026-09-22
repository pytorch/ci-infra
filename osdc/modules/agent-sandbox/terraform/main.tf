# Read-only IRSA role for the aws-sigv4-proxy service account. This is the ONLY
# AWS identity in the sandbox, and it lives on the trusted proxy — never on the
# agent. Scoped to invoking an Anthropic model through one of this account's
# inference profiles, and nothing else.
#
# The proxy runs with no --name, so the AWS service it talks to comes from each
# request's Host header (kubernetes/base/sigv4-proxy.yaml). It contributes no scope
# of its own: this policy IS the boundary between the sandboxed agent and AWS, so
# every statement here has to be justified by something that exists. Read-only
# CloudWatch/Logs access was granted here before anything consumed it — the agent
# only ever POSTs /model/<id>/invoke — and is deliberately gone. When a data source
# does arrive, it wants a resource-scoped statement naming the log groups the agent
# may see (the metric actions have no resource-level form and do need "*"), or a
# vetted MCP server as the README's future direction describes.
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

  # Bedrock invoke targets.
  #
  # Every current Anthropic model on Bedrock is INFERENCE_PROFILE-only — the
  # Claude 3 generation still listed as ON_DEMAND is refused by the provider
  # ("marked by provider as Legacy"), so the agent must call a cross-region
  # profile (e.g. us.anthropic.claude-haiku-4-5-...). Such a call is authorized
  # against BOTH the inference-profile ARN in the calling region AND the
  # underlying foundation-model ARN in whichever region the profile routes to, so
  # dropping the foundation-model ARN turns every cross-region hop into
  # AccessDenied.
  #
  # The region wildcard is a choice, not a requirement: a `us.` profile lands in
  # us-east-1/us-east-2/us-west-2 today, and AWS's own example enumerates exactly
  # those three, which is tighter and fails closed the day a fourth is added. The
  # wildcard is kept because the profile condition below — not the region list — is
  # what blocks direct invocation, so a new destination region keeps working
  # instead of turning into an outage nobody attributes to IAM.
  #
  # The provider prefix is the narrowing that costs nothing: the routed-region
  # authorization uses the BASE model id, so `anthropic.*` keeps the routing
  # argument intact without pinning a model or a version. The model id is picked by
  # the caller — server.py takes it from the /run body — so this is the only thing
  # bounding which model that can be.
  bedrock_foundation_models  = "arn:aws:bedrock:*::foundation-model/anthropic.*"
  bedrock_inference_profiles = "arn:aws:bedrock:${var.aws_region}:${local.account_id}:inference-profile/us.anthropic.*"

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
        # The profile is the intended entry point, so this one is unconditioned.
        Sid    = "BedrockInvokeInferenceProfile"
        Effect = "Allow"
        Action = [
          "bedrock:InvokeModel",
          "bedrock:InvokeModelWithResponseStream",
        ]
        Resource = [local.bedrock_inference_profiles]
      },
      {
        # The routed foundation model, allowed ONLY as the tail of a profile invoke.
        # Without the condition this statement separately permits direct on-demand
        # invocation of any matching model in every region, independently of any
        # profile. A direct invoke carries no bedrock:InferenceProfileArn in its
        # request context, so the condition evaluates false and nothing else grants
        # it — the profile becomes the only path, and it fails closed.
        #
        # ArnLike, where AWS's example uses StringEquals against one exact profile
        # ARN: the model id arrives in the /run body, so there is no single ARN to
        # pin. Pin it (and switch operator) once the fleet is on one model — that
        # also removes the per-request override the worker allows today.
        # Verify both halves after apply; IAM accepts an unknown condition key
        # silently, and a misspelling denies every invoke rather than erroring:
        #   1. a routed invoke through the profile succeeds
        #   2. a direct anthropic.claude-... invoke returns AccessDenied
        # One call on its own cannot tell a wrong key from a wrong request.
        Sid    = "BedrockInvokeProfileRoutedModel"
        Effect = "Allow"
        Action = [
          "bedrock:InvokeModel",
          "bedrock:InvokeModelWithResponseStream",
        ]
        Resource = [local.bedrock_foundation_models]
        Condition = {
          ArnLike = {
            "bedrock:InferenceProfileArn" = local.bedrock_inference_profiles
          }
        }
      },
    ]
  })

  tags = local.tags
}

resource "aws_iam_role_policy_attachment" "sigv4_proxy" {
  policy_arn = aws_iam_policy.sigv4_proxy.arn
  role       = aws_iam_role.sigv4_proxy.name
}
