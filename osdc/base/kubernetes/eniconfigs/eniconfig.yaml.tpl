# Generic ENIConfig template for VPC CNI Custom Networking.
#
# Rendered by base/kubernetes/eniconfigs/deploy.sh in two flavors:
#   - AZ-named ENIConfigs (one per AZ; for base nodes), with __SUBNET_ID__
#     taken from the base terraform `private_subnets_by_az` output.
#   - Bucket-prefixed ENIConfigs (bucket-${N}-${AZ}; for Karpenter workload
#     nodes), with __SUBNET_ID__ taken from `pod_subnets_by_bucket_az`.
# __ENICONFIG_NAME__ is substituted with the resource name.
#
# securityGroups is intentionally omitted: when absent, VPC CNI inherits
# the security groups attached to the node's primary ENI, which is the
# AWS-recommended default and works for both base nodes and Karpenter
# workload nodes.
#
# These resources are inert until VPC CNI Custom Networking is enabled
# (AWS_VPC_K8S_CNI_CUSTOM_NETWORK_CFG=true on the aws-node DaemonSet) in
# a later PR. Removal criterion: only delete after Custom Networking is
# permanently disabled cluster-wide.
---
apiVersion: crd.k8s.amazonaws.com/v1alpha1
kind: ENIConfig
metadata:
  name: __ENICONFIG_NAME__
spec:
  subnet: __SUBNET_ID__
