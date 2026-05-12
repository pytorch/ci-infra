# Generic ENIConfig template for VPC CNI Custom Networking.
#
# Rendered by a deploy script (currently base/kubernetes/eniconfigs/deploy.sh,
# which uses the AZ name as the ENIConfig name). The same template is reusable
# by other deploy scripts that need bucket-prefixed names (e.g.
# bucket-${N}-${AZ}). The placeholder __ENICONFIG_NAME__ is substituted with
# the resource name and __SUBNET_ID__ with the matching private subnet ID
# from the base terraform `private_subnets_by_az` output.
#
# securityGroups is intentionally omitted: when absent, VPC CNI inherits
# the security groups attached to the node's primary ENI, which is the
# AWS-recommended default and matches the existing node SG.
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
