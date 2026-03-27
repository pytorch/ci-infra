# pypi-cache EC2NodeClass template.
# Configures AWS node properties for pypi-cache Karpenter nodes.
# Populated by scripts/python/generate_manifests.py from clusters.yaml config.
# Placeholders: __INSTANCE_TYPE__
# CLUSTER_NAME_PLACEHOLDER is sed-substituted at deploy time.
apiVersion: karpenter.k8s.aws/v1
kind: EC2NodeClass
metadata:
  name: pypi-cache
spec:
  amiSelectorTerms:
    - alias: al2023@latest

  subnetSelectorTerms:
    - tags:
        karpenter.sh/discovery: "CLUSTER_NAME_PLACEHOLDER"

  securityGroupSelectorTerms:
    - tags:
        karpenter.sh/discovery: "CLUSTER_NAME_PLACEHOLDER"

  role: "CLUSTER_NAME_PLACEHOLDER-node-role"

  userData: |
    MIME-Version: 1.0
    Content-Type: multipart/mixed; boundary="==BOUNDARY=="

    --==BOUNDARY==
    Content-Type: application/node.eks.aws

    ---
    apiVersion: node.eks.aws/v1alpha1
    kind: NodeConfig
    spec:
      kubelet:
        config:
          containerLogMaxSize: 50Mi
          containerLogMaxFiles: 5

    --==BOUNDARY==--

  blockDeviceMappings:
    - deviceName: /dev/xvda
      ebs:
        volumeSize: 200Gi
        volumeType: gp3
        iops: 3000
        throughput: 125
        deleteOnTermination: true
        encrypted: true

  metadataOptions:
    httpEndpoint: enabled
    httpProtocolIPv6: disabled
    httpPutResponseHopLimit: 1
    httpTokens: required

  tags:
    Name: "CLUSTER_NAME_PLACEHOLDER-pypi-cache"
    ManagedBy: "karpenter"
    NodePool: "pypi-cache"
    InstanceType: "__INSTANCE_TYPE__"
