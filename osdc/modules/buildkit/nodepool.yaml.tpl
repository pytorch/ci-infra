# Karpenter NodePool + EC2NodeClass: buildkit-__ARCH__
# Template — expanded by deploy-buildkit for each architecture
#
# Instance type: __INSTANCE_TYPE__
# 2 buildkitd pods per node

apiVersion: karpenter.sh/v1
kind: NodePool
metadata:
  name: buildkit-__ARCH__
spec:
  # Limits (prevents runaway scaling)
  limits:
    cpu: "__CPU_LIMIT__"
    memory: "__MEMORY_LIMIT__"

  # Disruption settings — conservative, never evict running builds
  disruption:
    consolidationPolicy: WhenEmpty
    consolidateAfter: 5m
    budgets:
      - nodes: "0"

  # Template for nodes
  template:
    metadata:
      labels:
        workload-type: buildkit
        instance-type: "__INSTANCE_LABEL__"

    spec:
      # Node requirements
      requirements:
        - key: kubernetes.io/arch
          operator: In
          values: ["__ARCH__"]
        - key: kubernetes.io/os
          operator: In
          values: ["linux"]
        - key: karpenter.sh/capacity-type
          operator: In
          values: ["on-demand"]
        - key: node.kubernetes.io/instance-type
          operator: In
          values:
            - __INSTANCE_TYPE__

      # Reference to EC2NodeClass
      nodeClassRef:
        group: karpenter.k8s.aws
        kind: EC2NodeClass
        name: buildkit-__ARCH__

      # Taint — only buildkitd pods (which tolerate this) schedule here
      taints:
        - key: instance-type
          value: "__INSTANCE_TYPE__"
          effect: NoSchedule

      # Startup taint: removed by git-cache-warmer after cache is warm.
      # buildkitd pods do NOT tolerate this, so they wait for a warm cache.
      startupTaints:
        - key: git-cache-not-ready
          value: "true"
          effect: NoSchedule

---
apiVersion: karpenter.k8s.aws/v1
kind: EC2NodeClass
metadata:
  name: buildkit-__ARCH__
spec:
  # AMI Selection (Amazon Linux 2023 EKS-optimized)
  amiSelectorTerms:
    - alias: al2023@latest

  # Subnet selection (tagged by terraform with karpenter.sh/discovery)
  subnetSelectorTerms:
    - tags:
        karpenter.sh/discovery: "CLUSTER_NAME_PLACEHOLDER"

  # Security group selection
  securityGroupSelectorTerms:
    - tags:
        karpenter.sh/discovery: "CLUSTER_NAME_PLACEHOLDER"

  # IAM Instance Profile (created by EKS module)
  role: "CLUSTER_NAME_PLACEHOLDER-node-role"

  # User data (bootstrap script with MIME multipart format)
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
          # CPU Manager: Static allocation for Guaranteed QoS pods
          cpuManagerPolicy: static

          # Topology Manager: NUMA-aware pod placement
          topologyManagerPolicy: single-numa-node
          topologyManagerScope: pod

      instance:
        # Post-bootstrap script: NVMe setup, registry mirrors, CPU tuning
        localUserData: |
__LOCAL_USERDATA__

    --==BOUNDARY==--

  # Block device mappings — small root EBS (data lives on NVMe)
  blockDeviceMappings:
    - deviceName: /dev/xvda
      ebs:
        volumeSize: 100Gi
        volumeType: gp3
        iops: 3000
        throughput: 125
        deleteOnTermination: true
        encrypted: true

  # Metadata options (IMDSv2 required)
  metadataOptions:
    httpEndpoint: enabled
    httpProtocolIPv6: disabled
    httpPutResponseHopLimit: 1
    httpTokens: required

  # Tags
  tags:
    Name: "CLUSTER_NAME_PLACEHOLDER-buildkit-__ARCH__"
    ManagedBy: "karpenter"
    NodePool: "buildkit-__ARCH__"
    InstanceType: "__INSTANCE_TYPE__"
    Architecture: "__ARCH__"
