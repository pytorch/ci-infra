"""Tests for cni_constants module."""

from cni_constants import ENI_CONFIG_LABEL


class TestEniConfigLabel:
    def test_is_string(self):
        assert isinstance(ENI_CONFIG_LABEL, str)

    def test_value(self):
        # The literal value MUST stay in lockstep with:
        #   - base/scripts/bootstrap/eks-base-pre-nodeadm-az-label.sh
        #   - the future VPC CNI addon ENI_CONFIG_LABEL_DEF setting
        #   - the future Karpenter NodePool generator's static label
        assert ENI_CONFIG_LABEL == "ipam.osdc.internal/eni-config"

    def test_is_qualified_label_key(self):
        # Kubernetes label keys with a domain prefix have exactly one '/'.
        assert ENI_CONFIG_LABEL.count("/") == 1
        prefix, name = ENI_CONFIG_LABEL.split("/")
        assert prefix
        assert name
