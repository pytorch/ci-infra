# pypi-cache Karpenter NodePool template.
# Provisions dedicated on-demand nodes for pypi-cache workloads.
# Populated by scripts/python/generate_manifests.py from clusters.yaml config.
# Placeholders: __INSTANCE_TYPE__, __CPU_LIMIT__, __MEMORY_LIMIT__
apiVersion: karpenter.sh/v1
kind: NodePool
metadata:
  name: pypi-cache
spec:
  limits:
    cpu: "__CPU_LIMIT__"
    memory: "__MEMORY_LIMIT__"

  disruption:
    consolidationPolicy: WhenEmpty
    consolidateAfter: 5m
    budgets:
      - nodes: "1"

  template:
    metadata:
      labels:
        workload-type: pypi-cache
        instance-type: "__INSTANCE_TYPE__"

    spec:
      requirements:
        - key: kubernetes.io/arch
          operator: In
          values: ["amd64"]
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

      nodeClassRef:
        group: karpenter.k8s.aws
        kind: EC2NodeClass
        name: pypi-cache

      taints:
        - key: workload
          value: "pypi-cache"
          effect: NoSchedule
