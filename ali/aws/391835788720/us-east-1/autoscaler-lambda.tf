# TODO create this app and import the secret to the account
data "aws_secretsmanager_secret_version" "app_creds" {
  secret_id = "pytorch-gha-infra-app-secrets"
}

resource "random_password" "webhook_secret" {
  length = 28
}

module "autoscaler-lambda" {
  source = "../../../tf-modules/terraform-aws-github-runner"

  aws_region           = local.aws_region
  aws_region_instances = var.ali_aws_region
  vpc_ids = [
    for module_def in module.ali_runners_vpc :
    { vpc = module_def.vpc_id, region = local.aws_region }
  ]
  vpc_sgs = []
  subnet_vpc_ids = flatten([
    for module_def in module.ali_runners_vpc :
    flatten([
      for public_subnet in module_def.public_subnets :
      { subnet = public_subnet, vpc = module_def.vpc_id }
    ])
  ])
  subnet_azs = flatten([
    for module_def in module.ali_runners_vpc :
    module_def.subnet_azs
  ])
  vpc_cidrs           = [
    for module_def in module.ali_runners_vpc:
      {vpc = module_def.vpc_id, cidr = module_def.vpc_cidr}
  ]
  lambda_subnet_ids         = module.ali_runners_vpc[var.aws_vpc_suffixes[0]].private_subnets

  environment = var.ali_prod_environment
  tags = {
    Project = "pytorch/pytorch"
  }

  instance_type = "c5.4xlarge"

  key_name         = "pet-instances-skeleton-key-v2"

  github_app = {
    key_base64     = ""
    id             = ""
    client_id      = ""
    client_secret  = ""
    webhook_secret = random_password.webhook_secret.result
  }

  webhook_lambda_zip                = "../../../assets/lambdas-download/webhook.zip"
  runner_binaries_syncer_lambda_zip = "../../../assets/lambdas-download/runner-binaries-syncer.zip"
  runners_lambda_zip                = "../../../assets/lambdas-download/runners.zip"
  enable_organization_runners       = true
  minimum_running_time_in_minutes   = 10
  runner_extra_labels               = "pytorch.runners"
  runners_scale_down_lambda_timeout = 600
  runners_scale_up_lambda_timeout         = 600
  runners_scale_up_sqs_visibility_timeout = 600
  runners_scale_up_sqs_max_retry          = 1
  runners_scale_up_sqs_message_ret_s      = 7200
  scale_down_schedule_expression          = "cron(*/15 * * * ? *)"
  cant_have_issues_labels                 = ["Use Canary Lambdas"]
  scale_config_repo_path                  = ".github/lf-scale-config.yml"

  encrypt_secrets           = false
  secretsmanager_secrets_id = data.aws_secretsmanager_secret_version.app_creds.secret_id

  # TODO This won't work, we need to copy the windows AMI to this account
  ami_owners_windows = ["amazon"]
  ami_filter_windows = {
    name = ["Windows_Server-2022-English-Full-Base-2024.04.10"]
  }

  ami_owners_linux = ["amazon"]
  ami_filter_linux = {
    name = ["amzn2-ami-hvm-2.0.20240306.2-x86_64-ebs"]
  }

  enable_ssm_on_runners = true
  block_device_mappings = {
    volume_size = 100
  }

  runner_iam_role_managed_policy_arns = [
    # TODO Here we have all the policies for the runners for the implicit access
    # aws_iam_policy.allow_ecr_on_gha_runners.arn,
    # aws_iam_policy.allow_s3_sccache_access_on_gha_runners.arn,
    # aws_iam_policy.allow_lambda_on_gha_runners.arn
  ]

  userdata_post_install = file("${path.module}/scripts/linux_post_install.sh")

  scale_up_lambda_concurrency                = 60
  scale_up_provisioned_concurrent_executions = 15
}