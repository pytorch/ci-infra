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

# Reserved-ENI count under VPC CNI Custom Networking. Single source of truth.
#
# MUST equal ``settings.reservedENIs`` in ``modules/karpenter/helm/values.yaml``.
# Karpenter's scheduler subtracts this many ENIs from each instance's pod-IP
# capacity (the primary ENI carries node-only traffic and cannot host pods).
# The NodePool generator subtracts the same count in ``compute_pd_max_pods()``
# so the kubelet ``maxPods`` ceiling matches Karpenter's scheduling envelope —
# if these two diverge, the generator emits ceilings that overcommit what the
# scheduler actually allows, leading to pending pods.
#
# The Helm values.yaml cannot import Python; the link is enforced at test time
# by ``scripts/python/test_karpenter_helm_values.py``, which loads this
# constant and asserts the YAML matches.
RESERVED_ENIS_COUNT = 1
