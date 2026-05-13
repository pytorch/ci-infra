"""Shared VPC CNI / IPAM constants used across OSDC generators and smoke tests.

Single source of truth for label keys consumed by the AWS VPC CNI ``aws-node``
DaemonSet (Custom Networking). Centralized here so the bootstrap script,
nodepool generator, smoke tests, and tofu addon configuration all reference
the same literal value without drift.

Exports:
- ``ENI_CONFIG_LABEL`` — per-node label key the VPC CNI reads to pick the
  matching ``ENIConfig`` CR.
- ``bucket_eniconfig_name(bucket, az)`` — render the per-(bucket, AZ)
  ``ENIConfig`` CR name from a validated bucket string and AZ. Used by both
  the deploy script for bucket ENIConfig CRs and the NodePool generator so
  both sides emit identical names.
- ``BUCKET_NAME_RE`` — bucket-N name pattern (single source of truth; the
  bucket-architecture allocates pod IPs into 4 buckets named bucket-1..bucket-4).
- ``AZ_NAME_RE`` — canonical AWS AZ name pattern (e.g. ``us-east-2a``).

Importers today:
- ``base/kubernetes/tests/smoke/test_base_eniconfigs.py`` (smoke check)
- ``modules/nodepools/scripts/python/generate_nodepools.py`` (Karpenter
  NodePool generator — emits ``ENI_CONFIG_LABEL`` on each NodePool with the
  per-(bucket, AZ) value rendered via ``bucket_eniconfig_name``)
- ``scripts/cluster-config.py`` (validates ``base.pod_cidr_buckets`` shape
  via ``BUCKET_NAME_RE`` / ``AZ_NAME_RE``)

Also referenced as a string literal (cannot import Python here):
- ``base/scripts/bootstrap/eks-base-pre-nodeadm-az-label.sh`` (base MNG
  userData drop-in writing the kubelet ``--node-labels=`` flag at first boot)
- ``modules/eks/terraform/modules/eks/main.tf`` (VPC CNI addon
  ``configuration_values.ENI_CONFIG_LABEL_DEF``)
- ``base/kubernetes/eniconfigs/deploy.sh`` (renders per-(bucket, AZ)
  ``ENIConfig`` CR names via the same ``bucket_eniconfig_name`` convention)
"""

import re

# Custom node label whose value selects the matching ``ENIConfig`` CR for
# AWS VPC CNI Custom Networking. Set per-node by:
#   - userData bootstrap (base MNG nodes; value = node's AZ)
#   - Karpenter NodePool ``spec.template.metadata.labels`` (workload nodes;
#     value = ``bucket-${N}-${AZ}``)
# Read by aws-node ipamd via the addon's ``ENI_CONFIG_LABEL_DEF`` env var.
ENI_CONFIG_LABEL = "ipam.osdc.internal/eni-config"

# Bucket name pattern. Single source of truth — the bucket-architecture
# allocates pod IPs into 4 buckets named bucket-1..bucket-4.
BUCKET_NAME_RE = re.compile(r"^bucket-[1-4]$")

# Canonical AWS AZ name pattern (e.g. us-east-2a, ap-southeast-1c).
AZ_NAME_RE = re.compile(r"^[a-z]{2}-[a-z]+-\d[a-z]$")


def bucket_eniconfig_name(bucket, az):
    """Render the per-(bucket, AZ) ENIConfig CR name from a validated bucket string and AZ.

    The bucket is the full validated string (e.g. ``"bucket-1"``); the AZ is a
    canonical AWS AZ name (e.g. ``"us-east-2a"``). Returns ``"bucket-1-us-east-2a"``.

    Used by the bucket ENIConfig deploy script (``base/kubernetes/eniconfigs/deploy.sh``)
    and the Karpenter NodePool generator
    (``modules/nodepools/scripts/python/generate_nodepools.py``) so both sides emit
    identical names — the NodePool's per-node label value MUST equal the ENIConfig
    CR name for VPC CNI ipamd to find the matching subnet.
    """
    return f"{bucket}-{az}"
