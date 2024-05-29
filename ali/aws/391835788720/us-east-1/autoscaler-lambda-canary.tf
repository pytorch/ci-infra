resource "random_password" "webhook_secret_canary" {
  length = 28
}

data "aws_secretsmanager_secret_version" "canary_app_creds" {
  secret_id = "pytorch-gha-canary-infra-app-vars"
}


module "autoscaler-lambda-canary" {
  source = "../../../tf-modules/terraform-aws-github-runner"

  aws_region           = local.aws_region
  aws_region_instances = var.ali_aws_region
  vpc_ids              = [
    for module_def in module.ali_runners_canary_vpc:
      {vpc = module_def.vpc_id, region = local.aws_region}
  ]
  vpc_sgs = []
  subnet_vpc_ids       = flatten([
    for module_def in module.ali_runners_canary_vpc:
      flatten([
        for public_subnet in module_def.public_subnets:
          {subnet = public_subnet, vpc = module_def.vpc_id}
      ])
  ])
  subnet_azs = flatten([
    for module_def in module.ali_runners_canary_vpc :
    module_def.subnet_azs
  ])
  vpc_cidrs           = [
    for module_def in module.ali_runners_canary_vpc:
      {vpc = module_def.vpc_id, cidr = module_def.vpc_cidr}
  ]

  lambda_subnet_ids         = module.ali_runners_canary_vpc[var.aws_vpc_suffixes[0]].private_subnets
  # lambda_security_group_ids = flatten([
  #   for sg in data.aws_security_groups.canary_additional_sgs[0].ids:
  #     sg
  # ])

  environment = var.ali_canary_environment
  tags = {
    Project = "pytorch/pytorch-canary"
  }

  instance_type = "c5.4xlarge"

  key_name         = "pet-instances-skeleton-key-v2"

  github_app = {
    key_base64     = ""
    id             = ""
    client_id      = ""
    client_secret  = ""
    webhook_secret = random_password.webhook_secret_canary.result
  }

  webhook_lambda_zip                = "../../../assets/lambdas-download-canary/webhook.zip"
  runner_binaries_syncer_lambda_zip = "../../../assets/lambdas-download-canary/runner-binaries-syncer.zip"
  runners_lambda_zip                = "../../../assets/lambdas-download-canary/runners.zip"
  enable_organization_runners       = false
  minimum_running_time_in_minutes   = 10
  runner_extra_labels               = "pytorch.runners"
  // scale-down regularly times out at 60 seconds, since we support a lot of runners let's up this
  // to 5 minutes to be safe
  runners_scale_down_lambda_timeout = 600
  runners_scale_up_lambda_timeout   = 600
  scale_down_schedule_expression    = "cron(*/15 * * * ? *)"
  must_have_issues_labels           = ["Use Canary Lambdas"]

  encrypt_secrets           = false
  secretsmanager_secrets_id = data.aws_secretsmanager_secret_version.canary_app_creds.secret_id

  ami_owners_windows = ["amazon"]
  ami_filter_windows = {
    name = var.ami_filter_windows
  }

  ami_owners_linux = ["amazon"]
  ami_filter_linux = {
    name = var.ami_filter_linux
  }

  # enable access to the runners via SSM
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

  scale_up_lambda_concurrency = 10
  scale_up_provisioned_concurrent_executions = 0
}
