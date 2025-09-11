# This file includes basic k8s infrastructure managed via helm
# Other k8s services must depend on resources defined here

# Policy copied from https://raw.githubusercontent.com/kubernetes-sigs/aws-load-balancer-controller/v2.13.3/docs/install/iam_policy.json
resource "aws_iam_policy" "aws_load_balancer_controller" {
  name   = "AWSLoadBalancerControllerIAMPolicy-${local.cluster_name}"
  policy = file("${path.module}/resources/aws-load-balancer-controller-policy.json")
}

# Role that allows the load balancer controller SA to benefit from the policy
# See https://docs.aws.amazon.com/eks/latest/userguide/associate-service-account-role.html
# "aws-load-balancer-controller" is the name of service account provisioned by the helm chart
resource "aws_iam_role" "aws_load_balancer_controller" {
  name = "AWSLoadBalancerControllerRole-${local.cluster_name}"

  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect = "Allow"
      Principal = {
        Federated = local.oidc_provider_arn
      }
      Action = "sts:AssumeRoleWithWebIdentity"
      Condition = {
        StringEquals = {
          "${local.oidc_provider}:aud" = "sts.amazonaws.com"
          "${local.oidc_provider}:sub" = "system:serviceaccount:kube-system:aws-load-balancer-controller"
        }
      }
    }]
  })
}

# Connect the role to the policy
resource "aws_iam_role_policy_attachment" "aws_load_balancer_controller" {
  policy_arn = aws_iam_policy.aws_load_balancer_controller.arn
  role       = aws_iam_role.aws_load_balancer_controller.name
}

# Deploy the AWS LB Controller
resource "helm_release" "aws_load_balancer_controller" {
  name       = "aws-load-balancer-controller"
  repository = "https://aws.github.io/eks-charts"
  chart      = "aws-load-balancer-controller"
  version    = "1.13.4"
  namespace  = "kube-system"

  values = [
    templatefile("${path.module}/values/aws-load-balancer-controller.yaml.tftpl", {
        cluster_name = local.cluster_name
        vpc_id       = local.vpc_id
        role_arn     = aws_iam_role.aws_load_balancer_controller.arn
    })
  ]

  depends_on = [aws_iam_role_policy_attachment.aws_load_balancer_controller]
}


# Only install after the AWS LB controller is up and running
# The nginx service is of type LB, and this helm deployment waits until
# a public IP is assigned to the service
resource "helm_release" "ingress_nginx" {
  name             = "ingress-nginx"
  repository       = "https://kubernetes.github.io/ingress-nginx"
  chart            = "ingress-nginx"
  namespace        = "ingress-nginx"
  version          = "4.13.2"
  create_namespace = true

  # Load values from dedicated file
  values = [
    file("${path.module}/values/ingress-nginx.yaml")
  ]

  depends_on = [ helm_release.aws_load_balancer_controller ]
}

resource "helm_release" "cert_manager" {
  name             = "cert-manager"
  repository       = "oci://quay.io/jetstack/charts"
  chart            = "cert-manager"
  namespace        = "cert-manager"
  version          = "v1.18.2"
  create_namespace = true

  values = [
    file("${path.module}/values/cert-manager.yaml")
  ]
}

/*
 * kubernetes_manifest has a limitation when it comes to CRDs and CRs.
 * kubernetes_manifest fails planning a CR if the corresponding CRD is not on
 * the cluster yet. The kubectl_manifest solves this, but unfortunately
 * the kubectl provider is not maintained anymore.
 *
 * Deploying the ClusterIssuer using ArgoCD direcly would not be ideal as
 * it would postpone having a fully functional ingress to later on in the
 * infrastructure deployment.
 *
 * Short of deploying the CRD manually on the cluster, the only option left
 * is to install the ClusterIssuer via kubectl, which doesn't provide
 * much validation at terraform level, but it prevents the apply-time issue
 */

resource "null_resource" "letsencrypt_prod" {
  provisioner "local-exec" {
    command = <<-EOT
      aws eks update-kubeconfig --region ${local.aws_region} --name ${local.cluster_name}
      cat <<'EOF' | kubectl apply -f -
${templatefile("${path.module}/resources/clusterissuer.yaml.tftpl", {
  letsencrypt_issuer = var.letsencrypt_issuer
})}
EOF
    EOT
  }
}


# This gives a single depends_on target for all other apps installed on k8s
resource "null_resource" "k8s_infra_ready" {
  depends_on = [
    helm_release.aws_load_balancer_controller,
    helm_release.ingress_nginx,
    helm_release.cert_manager,
    null_resource.letsencrypt_prod,
  ]
}
