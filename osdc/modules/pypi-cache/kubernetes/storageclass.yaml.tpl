# pypi-cache StorageClass template.
# Provisions EFS-backed access points for shared wheel storage.
# Populated by scripts/python/generate_manifests.py from clusters.yaml config.
# Placeholders: __EFS_FILESYSTEM_ID__
apiVersion: storage.k8s.io/v1
kind: StorageClass
metadata:
  name: efs-pypi-cache
provisioner: efs.csi.aws.com
parameters:
  provisioningMode: efs-ap
  fileSystemId: __EFS_FILESYSTEM_ID__
  directoryPerms: "755"
  basePath: "/pypi-cache"
reclaimPolicy: Retain
volumeBindingMode: Immediate
