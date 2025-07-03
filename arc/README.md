# GitHub Actions Runner Controller (ARC)

This directory contains the GitHub ARC deployment managed by the
multicloud working group.

To manage the ARC Cluster we need to assume role.

`aws eks update-kubeconfig --region us-east-1 --name lf-arc-dev --role-arn arn:aws:iam::391835788720:role/pytorch-arc-admins`

## Terminating the EKS Cluster

Because the kubernetes_cluster_role_binding resource is bootstrapped by the
Terraform CI Job. It will cause an "Unauthorized" failure when we try to
terminate the EKS cluster if not using the job. If that's the case run
`tofu state list` to get a list of all available state resources and remove
the resource with `tofu state rm <resource>` to remove the admin_binding
cluster role.

## TODO

* Centralized access control: We currently use AWS IAM but that only works in
  AWS. Considering folks participating in LF projects should have an LFID it
  would be interesting if we can somehow leverage that existing ID system for
  auth into pytorch infra like EKS Cluster.
* Network access to EKS API currently on public network. For more security we
  should eventually move this to a private network.
