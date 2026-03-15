# TFLint configuration
# https://github.com/terraform-linters/tflint

config {
  # Don't require tofu init (module sources need init to resolve)
  call_module_type = "none"
}

plugin "terraform" {
  enabled = true
  preset  = "recommended"
}

plugin "aws" {
  enabled = true
  version = "0.38.0"
  source  = "github.com/terraform-linters/tflint-ruleset-aws"
}
