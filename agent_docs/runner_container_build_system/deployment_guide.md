# Deployment Guide

This guide covers production deployment of the GitHub Actions Runner Container Build System using Kubernetes and other container orchestration platforms.

## 🚀 Production Deployment Overview

The runner container system is designed for production deployment with:
- **Security**: Non-root containers with proper permissions
- **Scalability**: Horizontal scaling via Kubernetes
- **Monitoring**: Health checks and observability
- **Configuration**: Environment-based configuration management
- **Updates**: Rolling updates without downtime

## 🐳 Container Registry Setup

### Building and Publishing Images

```bash
# Build and tag images
docker build -t ghcr.io/yourorg/github-runner:latest runners/base/
docker build -t ghcr.io/yourorg/github-runner:$(date +%Y%m%d) runners/base/

# Push to registry
docker push ghcr.io/yourorg/github-runner:latest
docker push ghcr.io/yourorg/github-runner:$(date +%Y%m%d)

# Multi-platform builds
docker buildx build --platform linux/amd64,linux/arm64 \
  -t ghcr.io/yourorg/github-runner:latest \
  --push runners/base/
```

### Automated Building with GitHub Actions

```yaml
# .github/workflows/build-images.yml
name: Build and Push Images

on:
  push:
    branches: [main]
  pull_request:
    branches: [main]

env:
  REGISTRY: ghcr.io
  IMAGE_NAME: ${{ github.repository }}/github-runner

jobs:
  build:
    runs-on: ubuntu-latest
    permissions:
      contents: read
      packages: write

    steps:
    - name: Checkout
      uses: actions/checkout@v4

    - name: Setup Docker Buildx
      uses: docker/setup-buildx-action@v3

    - name: Login to Container Registry
      uses: docker/login-action@v3
      with:
        registry: ${{ env.REGISTRY }}
        username: ${{ github.actor }}
        password: ${{ secrets.GITHUB_TOKEN }}

    - name: Extract metadata
      id: meta
      uses: docker/metadata-action@v5
      with:
        images: ${{ env.REGISTRY }}/${{ env.IMAGE_NAME }}
        tags: |
          type=ref,event=branch
          type=ref,event=pr
          type=sha
          type=raw,value=latest,enable={{is_default_branch}}

    - name: Build and push
      uses: docker/build-push-action@v5
      with:
        context: runners/base
        platforms: linux/amd64,linux/arm64
        push: true
        tags: ${{ steps.meta.outputs.tags }}
        labels: ${{ steps.meta.outputs.labels }}
        cache-from: type=gha
        cache-to: type=gha,mode=max
```

## ☸️ Kubernetes Deployment

### Basic Kubernetes Deployment

```yaml
# k8s/runner-deployment.yaml
apiVersion: apps/v1
kind: Deployment
metadata:
  name: github-runner
  namespace: github-runners
  labels:
    app: github-runner
    version: v1.0.0
spec:
  replicas: 3
  strategy:
    type: RollingUpdate
    rollingUpdate:
      maxUnavailable: 1
      maxSurge: 1
  selector:
    matchLabels:
      app: github-runner
  template:
    metadata:
      labels:
        app: github-runner
        version: v1.0.0
      annotations:
        prometheus.io/scrape: "true"
        prometheus.io/port: "8080"
    spec:
      serviceAccountName: github-runner
      securityContext:
        runAsNonRoot: true
        runAsUser: 1000
        fsGroup: 1000
      containers:
      - name: runner
        image: ghcr.io/yourorg/github-runner:latest
        imagePullPolicy: Always
        
        env:
        - name: GITHUB_URL
          valueFrom:
            configMapKeyRef:
              name: runner-config
              key: github_url
        - name: RUNNER_TOKEN
          valueFrom:
            secretKeyRef:
              name: runner-secret
              key: token
        - name: RUNNER_NAME
          value: "$(HOSTNAME)"
        - name: RUNNER_LABELS
          valueFrom:
            configMapKeyRef:
              name: runner-config
              key: labels
        - name: RUNNER_FEATURES
          valueFrom:
            configMapKeyRef:
              name: runner-config
              key: features
        - name: RUST_LOG
          value: "info"
        
        resources:
          requests:
            memory: "512Mi"
            cpu: "500m"
          limits:
            memory: "2Gi"
            cpu: "2000m"
            
        securityContext:
          allowPrivilegeEscalation: false
          readOnlyRootFilesystem: false
          capabilities:
            drop:
            - ALL
            add:
            - SETUID
            - SETGID
            
        livenessProbe:
          exec:
            command:
            - /bin/bash
            - -c
            - "pgrep -f 'Runner.Listener' || exit 1"
          initialDelaySeconds: 60
          periodSeconds: 30
          timeoutSeconds: 10
          failureThreshold: 3
          
        readinessProbe:
          exec:
            command:
            - /bin/bash
            - -c
            - "pgrep -f 'Runner.Listener' && test -f /home/runner/.runner"
          initialDelaySeconds: 30
          periodSeconds: 10
          timeoutSeconds: 5
          failureThreshold: 3
          
        volumeMounts:
        - name: runner-work
          mountPath: /home/runner/_work
        - name: docker-socket
          mountPath: /var/run/docker.sock
          readOnly: false
          
      volumes:
      - name: runner-work
        emptyDir:
          sizeLimit: 10Gi
      - name: docker-socket
        hostPath:
          path: /var/run/docker.sock
          type: Socket
          
      restartPolicy: Always
      terminationGracePeriodSeconds: 60
      
      nodeSelector:
        kubernetes.io/os: linux
        
      tolerations:
      - key: "github-runner"
        operator: "Equal"
        value: "true"
        effect: "NoSchedule"

---
apiVersion: v1
kind: ServiceAccount
metadata:
  name: github-runner
  namespace: github-runners
  labels:
    app: github-runner

---
apiVersion: rbac.authorization.k8s.io/v1
kind: Role
metadata:
  name: github-runner
  namespace: github-runners
rules:
- apiGroups: [""]
  resources: ["pods", "pods/log"]
  verbs: ["get", "list", "watch"]
- apiGroups: [""]
  resources: ["secrets"]
  verbs: ["get"]

---
apiVersion: rbac.authorization.k8s.io/v1
kind: RoleBinding
metadata:
  name: github-runner
  namespace: github-runners
subjects:
- kind: ServiceAccount
  name: github-runner
  namespace: github-runners
roleRef:
  kind: Role
  name: github-runner
  apiGroup: rbac.authorization.k8s.io
```

### Configuration Management

```yaml
# k8s/configmap.yaml
apiVersion: v1
kind: ConfigMap
metadata:
  name: runner-config
  namespace: github-runners
data:
  github_url: "https://github.com/yourorg/yourrepo"
  labels: "self-hosted,Linux,X64,kubernetes"
  features: "nodejs,python,docker"
  runner_group: "default"
  work_directory: "_work"
  
---
apiVersion: v1
kind: Secret
metadata:
  name: runner-secret
  namespace: github-runners
type: Opaque
stringData:
  token: "YOUR_GITHUB_RUNNER_TOKEN_HERE"
  
# Alternative: Use external secrets operator
---
apiVersion: external-secrets.io/v1beta1
kind: ExternalSecret
metadata:
  name: github-runner-token
  namespace: github-runners
spec:
  secretStoreRef:
    name: vault-backend
    kind: SecretStore
  target:
    name: runner-secret
    creationPolicy: Owner
  data:
  - secretKey: token
    remoteRef:
      key: github-runner
      property: token
```

### Namespace Setup

```yaml
# k8s/namespace.yaml
apiVersion: v1
kind: Namespace
metadata:
  name: github-runners
  labels:
    name: github-runners
    purpose: ci-cd
    
---
apiVersion: v1
kind: ResourceQuota
metadata:
  name: runner-quota
  namespace: github-runners
spec:
  hard:
    requests.cpu: "10"
    requests.memory: "20Gi"
    limits.cpu: "20"
    limits.memory: "40Gi"
    pods: "20"
    
---
apiVersion: v1
kind: LimitRange
metadata:
  name: runner-limits
  namespace: github-runners
spec:
  limits:
  - default:
      cpu: "2"
      memory: "2Gi"
    defaultRequest:
      cpu: "500m"
      memory: "512Mi"
    type: Container
```

## 🔄 Auto-scaling Configuration

### Horizontal Pod Autoscaler

```yaml
# k8s/hpa.yaml
apiVersion: autoscaling/v2
kind: HorizontalPodAutoscaler
metadata:
  name: github-runner-hpa
  namespace: github-runners
spec:
  scaleTargetRef:
    apiVersion: apps/v1
    kind: Deployment
    name: github-runner
  minReplicas: 2
  maxReplicas: 10
  metrics:
  - type: Resource
    resource:
      name: cpu
      target:
        type: Utilization
        averageUtilization: 70
  - type: Resource
    resource:
      name: memory
      target:
        type: Utilization
        averageUtilization: 80
  behavior:
    scaleDown:
      stabilizationWindowSeconds: 300
      policies:
      - type: Percent
        value: 50
        periodSeconds: 60
    scaleUp:
      stabilizationWindowSeconds: 60
      policies:
      - type: Percent
        value: 100
        periodSeconds: 60
```

### Vertical Pod Autoscaler (Optional)

```yaml
# k8s/vpa.yaml
apiVersion: autoscaling.k8s.io/v1
kind: VerticalPodAutoscaler
metadata:
  name: github-runner-vpa
  namespace: github-runners
spec:
  targetRef:
    apiVersion: apps/v1
    kind: Deployment
    name: github-runner
  updatePolicy:
    updateMode: "Auto"
  resourcePolicy:
    containerPolicies:
    - containerName: runner
      maxAllowed:
        cpu: "4"
        memory: "8Gi"
      minAllowed:
        cpu: "100m"
        memory: "128Mi"
```

## 📊 Monitoring and Observability

### Service Monitor for Prometheus

```yaml
# k8s/monitoring.yaml
apiVersion: v1
kind: Service
metadata:
  name: github-runner-metrics
  namespace: github-runners
  labels:
    app: github-runner
spec:
  ports:
  - name: metrics
    port: 8080
    targetPort: 8080
  selector:
    app: github-runner
    
---
apiVersion: monitoring.coreos.com/v1
kind: ServiceMonitor
metadata:
  name: github-runner
  namespace: github-runners
  labels:
    app: github-runner
spec:
  selector:
    matchLabels:
      app: github-runner
  endpoints:
  - port: metrics
    interval: 30s
    path: /metrics
```

### Grafana Dashboard Configuration

```json
{
  "dashboard": {
    "title": "GitHub Actions Runners",
    "panels": [
      {
        "title": "Runner Pods",
        "type": "stat",
        "targets": [
          {
            "expr": "sum(kube_pod_status_ready{namespace=\"github-runners\", condition=\"true\"})"
          }
        ]
      },
      {
        "title": "CPU Usage",
        "type": "graph",
        "targets": [
          {
            "expr": "rate(container_cpu_usage_seconds_total{namespace=\"github-runners\"}[5m])"
          }
        ]
      },
      {
        "title": "Memory Usage",
        "type": "graph", 
        "targets": [
          {
            "expr": "container_memory_usage_bytes{namespace=\"github-runners\"}"
          }
        ]
      }
    ]
  }
}
```

## 🚨 Alerting Rules

```yaml
# k8s/alerts.yaml
apiVersion: monitoring.coreos.com/v1
kind: PrometheusRule
metadata:
  name: github-runner-alerts
  namespace: github-runners
spec:
  groups:
  - name: github-runner
    rules:
    - alert: RunnerPodDown
      expr: sum(kube_pod_status_ready{namespace="github-runners", condition="true"}) == 0
      for: 2m
      labels:
        severity: critical
      annotations:
        summary: "No GitHub runner pods available"
        description: "All GitHub runner pods are down in namespace {{ $labels.namespace }}"
        
    - alert: RunnerHighCPU
      expr: rate(container_cpu_usage_seconds_total{namespace="github-runners"}[5m]) > 0.8
      for: 5m
      labels:
        severity: warning
      annotations:
        summary: "High CPU usage in runner pod"
        description: "Runner pod {{ $labels.pod }} has high CPU usage: {{ $value }}"
        
    - alert: RunnerHighMemory
      expr: container_memory_usage_bytes{namespace="github-runners"} / container_spec_memory_limit_bytes > 0.9
      for: 5m
      labels:
        severity: warning
      annotations:
        summary: "High memory usage in runner pod"
        description: "Runner pod {{ $labels.pod }} has high memory usage: {{ $value }}%"
```

## 🔧 Deployment Scripts

### Deploy Script

```bash
#!/bin/bash
# scripts/deploy.sh

set -e

NAMESPACE="github-runners"
GITHUB_URL="${GITHUB_URL:-https://github.com/yourorg/yourrepo}"
RUNNER_TOKEN="${RUNNER_TOKEN:-}"
FEATURES="${RUNNER_FEATURES:-nodejs,python,docker}"

if [ -z "$RUNNER_TOKEN" ]; then
    echo "Error: RUNNER_TOKEN environment variable is required"
    exit 1
fi

echo "=== Deploying GitHub Actions Runners ==="

# Create namespace
kubectl create namespace "$NAMESPACE" --dry-run=client -o yaml | kubectl apply -f -

# Apply RBAC
kubectl apply -f k8s/namespace.yaml
kubectl apply -f k8s/rbac.yaml

# Create/update configmap
kubectl create configmap runner-config \
    --from-literal=github_url="$GITHUB_URL" \
    --from-literal=features="$FEATURES" \
    --from-literal=labels="self-hosted,Linux,X64,kubernetes" \
    --namespace="$NAMESPACE" \
    --dry-run=client -o yaml | kubectl apply -f -

# Create/update secret
kubectl create secret generic runner-secret \
    --from-literal=token="$RUNNER_TOKEN" \
    --namespace="$NAMESPACE" \
    --dry-run=client -o yaml | kubectl apply -f -

# Deploy runners
kubectl apply -f k8s/runner-deployment.yaml

# Deploy monitoring (if available)
if kubectl get crd servicemonitors.monitoring.coreos.com >/dev/null 2>&1; then
    kubectl apply -f k8s/monitoring.yaml
    echo "✓ Monitoring configured"
fi

# Deploy autoscaling
kubectl apply -f k8s/hpa.yaml

echo "=== Deployment completed ==="
echo "Check status: kubectl get pods -n $NAMESPACE"
echo "View logs: kubectl logs -f deployment/github-runner -n $NAMESPACE"
```

### Update Script

```bash
#!/bin/bash
# scripts/update.sh

set -e

NAMESPACE="github-runners"
IMAGE_TAG="${IMAGE_TAG:-latest}"

echo "=== Updating GitHub Actions Runners ==="

# Update image
kubectl set image deployment/github-runner \
    runner=ghcr.io/yourorg/github-runner:$IMAGE_TAG \
    --namespace="$NAMESPACE"

# Wait for rollout
kubectl rollout status deployment/github-runner --namespace="$NAMESPACE" --timeout=300s

echo "=== Update completed ==="
echo "Current status:"
kubectl get pods -n "$NAMESPACE"
```

## 🏗️ Advanced Deployment Patterns

### Blue-Green Deployment

```yaml
# k8s/blue-green-deployment.yaml
apiVersion: argoproj.io/v1alpha1
kind: Rollout
metadata:
  name: github-runner
  namespace: github-runners
spec:
  replicas: 5
  strategy:
    blueGreen:
      activeService: github-runner-active
      previewService: github-runner-preview
      autoPromotionEnabled: false
      scaleDownDelaySeconds: 30
      prePromotionAnalysis:
        templates:
        - templateName: success-rate
        args:
        - name: service-name
          value: github-runner-preview
      postPromotionAnalysis:
        templates:
        - templateName: success-rate
        args:
        - name: service-name
          value: github-runner-active
  selector:
    matchLabels:
      app: github-runner
  template:
    metadata:
      labels:
        app: github-runner
    spec:
      # ... (same as regular deployment)
```

### Canary Deployment

```yaml
# k8s/canary-deployment.yaml
apiVersion: argoproj.io/v1alpha1
kind: Rollout
metadata:
  name: github-runner
  namespace: github-runners
spec:
  replicas: 10
  strategy:
    canary:
      steps:
      - setWeight: 20
      - pause: {duration: 10m}
      - setWeight: 40
      - pause: {duration: 10m}
      - setWeight: 60
      - pause: {duration: 10m}
      - setWeight: 80
      - pause: {duration: 10m}
      canaryService: github-runner-canary
      stableService: github-runner-stable
      trafficRouting:
        istio:
          virtualService:
            name: github-runner-vs
  selector:
    matchLabels:
      app: github-runner
  template:
    # ... (same as regular deployment)
```

## 🔐 Security Considerations

### Pod Security Standards

```yaml
# k8s/pod-security-policy.yaml
apiVersion: v1
kind: Namespace
metadata:
  name: github-runners
  labels:
    pod-security.kubernetes.io/enforce: restricted
    pod-security.kubernetes.io/audit: restricted
    pod-security.kubernetes.io/warn: restricted
```

### Network Policies

```yaml
# k8s/network-policy.yaml
apiVersion: networking.k8s.io/v1
kind: NetworkPolicy
metadata:
  name: github-runner-netpol
  namespace: github-runners
spec:
  podSelector:
    matchLabels:
      app: github-runner
  policyTypes:
  - Ingress
  - Egress
  ingress:
  - from:
    - namespaceSelector:
        matchLabels:
          name: monitoring
    ports:
    - protocol: TCP
      port: 8080
  egress:
  - {} # Allow all egress for GitHub API access
```

## 🔄 Backup and Disaster Recovery

### Backup Configuration

```bash
#!/bin/bash
# scripts/backup.sh

NAMESPACE="github-runners"
BACKUP_DIR="backups/$(date +%Y%m%d)"

mkdir -p "$BACKUP_DIR"

# Backup configurations
kubectl get configmap runner-config -n "$NAMESPACE" -o yaml > "$BACKUP_DIR/configmap.yaml"
kubectl get secret runner-secret -n "$NAMESPACE" -o yaml > "$BACKUP_DIR/secret.yaml"
kubectl get deployment github-runner -n "$NAMESPACE" -o yaml > "$BACKUP_DIR/deployment.yaml"

echo "Backup completed in $BACKUP_DIR"
```

### Disaster Recovery Plan

1. **Backup Strategy**: Daily automated backups of configurations
2. **Multi-Region Deployment**: Deploy in multiple Kubernetes clusters
3. **External Secrets**: Use external secret management (Vault, AWS Secrets Manager)
4. **Infrastructure as Code**: All configurations in Git
5. **Monitoring**: 24/7 monitoring with automated alerts

This deployment guide provides a comprehensive foundation for production deployment of the GitHub Actions Runner Container Build System with enterprise-grade security, monitoring, and scalability features. 