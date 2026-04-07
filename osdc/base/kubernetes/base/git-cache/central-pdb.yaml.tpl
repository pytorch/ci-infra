apiVersion: policy/v1
kind: PodDisruptionBudget
metadata:
  name: git-cache-central
  namespace: kube-system
  labels:
    app: git-cache-central
spec:
  minAvailable: __MIN_AVAILABLE__
  selector:
    matchLabels:
      app: git-cache-central
