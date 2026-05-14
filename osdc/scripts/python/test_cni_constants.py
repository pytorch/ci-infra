"""Tests for cni_constants module."""

from cni_constants import AZ_NAME_RE, BUCKET_NAME_RE, ENI_CONFIG_LABEL, bucket_eniconfig_name


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


class TestBucketEniconfigName:
    def test_renders_typical_inputs(self):
        # Renders identically across the deploy script and NodePool generator.
        assert bucket_eniconfig_name("bucket-1", "us-east-2a") == "bucket-1-us-east-2a"
        assert bucket_eniconfig_name("bucket-4", "us-west-1c") == "bucket-4-us-west-1c"

    def test_uses_dash_join(self):
        # Format is {bucket}-{az} — the NodePool's per-node label value MUST
        # equal this exact name for ipamd to find the matching ENIConfig CR.
        assert bucket_eniconfig_name("bucket-2", "eu-central-1b") == "bucket-2-eu-central-1b"


class TestBucketNameRe:
    def test_accepts_valid_buckets(self):
        for name in ("bucket-1", "bucket-2", "bucket-3", "bucket-4"):
            assert BUCKET_NAME_RE.match(name), name

    def test_rejects_invalid_buckets(self):
        for name in ("bucket-0", "bucket-5", "bucket-10", "bucket1", "Bucket-1", "bucket-", "", "bucket-1a"):
            assert BUCKET_NAME_RE.match(name) is None, name


class TestAzNameRe:
    def test_accepts_valid_azs(self):
        for az in ("us-east-2a", "us-west-1c", "eu-central-1b", "ap-southeast-1a"):
            assert AZ_NAME_RE.match(az), az

    def test_rejects_invalid_azs(self):
        for az in ("us-east-2", "useast2a", "us-east-2-a", "USEAST2A", "us_east_2a", "us--east-2a", "", "us-east-2aa"):
            assert AZ_NAME_RE.match(az) is None, az
