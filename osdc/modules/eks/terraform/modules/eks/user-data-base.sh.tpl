MIME-Version: 1.0
Content-Type: multipart/mixed; boundary="==BOUNDARY=="

--==BOUNDARY==
Content-Type: application/node.eks.aws

---
apiVersion: node.eks.aws/v1alpha1
kind: NodeConfig
spec:
  cluster:
    name: ${cluster_name}
    apiServerEndpoint: ${cluster_endpoint}
    certificateAuthority: ${cluster_ca_data}
    cidr: ${service_cidr}
  kubelet:
    config:
      maxPods: 110
    flags:
      - --register-with-taints=CriticalAddonsOnly=true:NoSchedule

--==BOUNDARY==
Content-Type: text/x-shellscript; charset="us-ascii"

${post_bootstrap_script}

--==BOUNDARY==--
