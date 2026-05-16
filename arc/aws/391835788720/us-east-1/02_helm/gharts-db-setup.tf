/*
 * Database user setup for gharts with IAM authentication.
 *
 * IMPORTANT: This setup is IAM-specific and assumes:
 * - RDS instance has iam_database_authentication_enabled = true (hardcoded in 01_infra)
 * - Helm values have database.iamAuth.enabled = true (hardcoded in values template)
 *
 * For password-based authentication, this file would need to be modified to:
 * 1. Create user WITH PASSWORD instead of passwordless
 * 2. Remove the GRANT rds_iam statement
 * 3. Store password in Kubernetes Secret
 *
 * Creates the PostgreSQL user 'gharts' without password and grants rds_iam role
 * to enable IAM token-based authentication. This is a one-time setup that runs
 * before the Helm chart deployment.
 */

data "aws_db_instance" "gharts" {
  db_instance_identifier = "lf-arc-dev-gharts"
}

data "aws_secretsmanager_secret" "gharts_rds_master" {
  name = data.aws_db_instance.gharts.master_user_secret[0].secret_arn
}

data "aws_secretsmanager_secret_version" "gharts_rds_master" {
  secret_id = data.aws_secretsmanager_secret.gharts_rds_master.id
}

locals {
  gharts_master_credentials = jsondecode(data.aws_secretsmanager_secret_version.gharts_rds_master.secret_string)
  gharts_master_username    = local.gharts_master_credentials.username
  gharts_master_password    = sensitive(local.gharts_master_credentials.password)
  gharts_db_setup_sql       = "GRANT rds_iam TO gharts;"
}

# Create IAM-enabled database user
resource "null_resource" "gharts_db_user_setup" {
  triggers = {
    sql          = sha256(local.gharts_db_setup_sql)
    rds_endpoint = local.gharts_rds_host
    db_name      = "gharts"
    username     = "gharts"
  }

  provisioner "local-exec" {
    command = <<-EOT
      until psql -h ${local.gharts_rds_host} -U ${local.gharts_master_username} -d postgres -c '\q' 2>/dev/null; do
        echo "Waiting for RDS to be available..."
        sleep 5
      done

      psql -h ${local.gharts_rds_host} -U ${local.gharts_master_username} -d postgres -c "${local.gharts_db_setup_sql}"
    EOT

    environment = {
      PGPASSWORD = local.gharts_master_password
    }
    interpreter = ["bash", "-c"]
  }

  depends_on = [
    kubernetes_namespace.gharts
  ]
}
