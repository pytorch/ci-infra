"""Shared VPC CNI / IPAM constants used across OSDC generators and smoke tests.

Single source of truth for label keys consumed by the AWS VPC CNI ``aws-node``
DaemonSet (Custom Networking). Centralized here so the bootstrap script,
nodepool generator, smoke tests, and tofu addon configuration all reference
the same literal value without drift.

Importers today:
- ``base/kubernetes/tests/smoke/test_base_eniconfigs.py`` (PR 14 smoke check)
- ``modules/nodepools/scripts/python/generate_nodepools.py`` (future PR 9
  Karpenter NodePool generator — emits ``ENI_CONFIG_LABEL`` on each NodePool)

Also referenced as a string literal (cannot import Python here):
- ``base/scripts/bootstrap/eks-base-pre-nodeadm-az-label.sh`` (PR 14 base MNG
  userData drop-in writing the kubelet ``--node-labels=`` flag at first boot)
- ``modules/eks/terraform/modules/eks/main.tf`` (future PR 7 VPC CNI addon
  ``configuration_values.ENI_CONFIG_LABEL_DEF``)
"""

# Custom node label whose value selects the matching ``ENIConfig`` CR for
# AWS VPC CNI Custom Networking. Set per-node by:
#   - userData bootstrap (base MNG nodes; value = node's AZ)
#   - Karpenter NodePool ``spec.template.metadata.labels`` (workload nodes;
#     value = ``bucket-${N}-${AZ}``)
# Read by aws-node ipamd via the addon's ``ENI_CONFIG_LABEL_DEF`` env var.
ENI_CONFIG_LABEL = "ipam.osdc.internal/eni-config"
