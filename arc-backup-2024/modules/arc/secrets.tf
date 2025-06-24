resource "aws_secretsmanager_secret" "pytorch_internal_docker_registry_auth" {
  name        = "pytorch_internal_docker_registry_auth-${local.cluster_name}"
  description = "used by nodes to authenticate to the internal docker registry"
}

data "external" "pytorch_internal_docker_registry_auth_secret" {
  program = ["/bin/bash", "-c", "echo \"{\\\"secret\\\": \\\"$DOCKER_REGISTRY_PASSWORDS\\\"}\""]
}

resource "aws_secretsmanager_secret_version" "pytorch_internal_docker_registry_auth" {
  secret_id     = aws_secretsmanager_secret.pytorch_internal_docker_registry_auth.id
  secret_string = data.external.pytorch_internal_docker_registry_auth_secret.result["secret"]
}
