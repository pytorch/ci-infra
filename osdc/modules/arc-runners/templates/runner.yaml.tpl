githubConfigUrl: "{{GITHUB_CONFIG_URL}}"
githubConfigSecret: "{{GITHUB_SECRET_NAME}}"
runnerScaleSetName: "{{RUNNER_NAME_PREFIX}}{{RUNNER_NAME}}"

minRunners: 0

runnerGroup: "default"

# Listener metrics cardinality control
# Exclude high-cardinality labels (job_name, event_name, job_workflow_ref,
# job_workflow_target) that create unbounded series. Keep labels useful for
# dashboards and alerting: repository, organization, enterprise, job_workflow_name.
listenerMetrics:
  counters:
    gha_started_jobs_total:
      labels:
        - repository
        - organization
        - enterprise
        - job_workflow_name
    gha_completed_jobs_total:
      labels:
        - repository
        - organization
        - enterprise
        - job_result
        - job_workflow_name
  gauges:
    gha_assigned_jobs:
      labels: ["name", "namespace", "repository", "organization", "enterprise"]
    gha_running_jobs:
      labels: ["name", "namespace", "repository", "organization", "enterprise"]
    gha_registered_runners:
      labels: ["name", "namespace", "repository", "organization", "enterprise"]
    gha_busy_runners:
      labels: ["name", "namespace", "repository", "organization", "enterprise"]
    gha_min_runners:
      labels: ["name", "namespace", "repository", "organization", "enterprise"]
    gha_max_runners:
      labels: ["name", "namespace", "repository", "organization", "enterprise"]
    gha_desired_runners:
      labels: ["name", "namespace", "repository", "organization", "enterprise"]
    gha_idle_runners:
      labels: ["name", "namespace", "repository", "organization", "enterprise"]
  histograms:
    gha_job_startup_duration_seconds:
      labels:
        - repository
        - organization
        - enterprise
        - job_workflow_name
      buckets:
        [5, 10, 30, 60, 120, 300, 600, 1200, 1800, 3600]
    gha_job_execution_duration_seconds:
      labels:
        - repository
        - organization
        - enterprise
        - job_result
        - job_workflow_name
      buckets:
        [5, 10, 30, 60, 120, 300, 600, 1200, 1800, 3600]

containerMode:
  type: "kubernetes-novolume"

controllerServiceAccount:
  namespace: arc-systems
  name: arc-gha-rs-controller

listenerTemplate:
  spec:
    tolerations:
      - key: CriticalAddonsOnly
        operator: Equal
        value: "true"
        effect: NoSchedule
    containers:
      - name: listener
        resources:
          limits:
            cpu: "200m"
            memory: "256Mi"
          requests:
            cpu: "100m"
            memory: "128Mi"

template:
  metadata:
    annotations:
      karpenter.sh/do-not-disrupt: "true"
  spec:
    serviceAccountName: arc-runner

    # Schedule runner pods on compute nodes
    nodeSelector:
      workload-type: github-runner
      instance-type: "{{INSTANCE_TYPE}}"

    # Tolerate instance-type taint + git-cache startup taint
    tolerations:
      - key: instance-type
        operator: Equal
        value: "{{INSTANCE_TYPE}}"
        effect: NoSchedule
      - key: git-cache-not-ready
        operator: Equal
        value: "true"
        effect: NoSchedule{{GPU_TOLERATIONS}}

    # Wait for patched hooks to be available on the node (placed by
    # runner-hooks-warmer DaemonSet). Polls every 10s for the index.js.
    # Remove this when upstream merges the fix into actions/runner-container-hooks
    initContainers:
      - name: wait-for-hooks
        image: public.ecr.aws/docker/library/alpine:3.21
        command:
          - /bin/sh
          - -c
          - |
            TIMEOUT=300
            ELAPSED=0
            echo "Waiting for patched hooks at /opt/runner-hooks/dist/index.js..."
            while [ ! -f /opt/runner-hooks/dist/index.js ]; do
              if [ "$ELAPSED" -ge "$TIMEOUT" ]; then
                echo "ERROR: Timed out waiting for patched hooks after ${TIMEOUT}s"
                exit 1
              fi
              sleep 10
              ELAPSED=$((ELAPSED + 10))
            done
            echo "Patched hooks found."
        volumeMounts:
          - name: patched-hooks
            mountPath: /opt/runner-hooks
            readOnly: true

    containers:
      - name: runner
        image: {{RUNNER_IMAGE}}
        command: ["/home/runner/run.sh"]
        env:
          - name: RUNNER_FEATURE_FLAG_EPHEMERAL
            value: "true"
          # Point to hook template for job pod customization
          - name: ACTIONS_RUNNER_CONTAINER_HOOK_TEMPLATE
            value: /home/runner/hook-extensions/job-pod.yaml
          # Use patched hooks from DaemonSet instead of baked-in ones
          # See: https://github.com/jeanschmidt/runner-container-hooks/releases/tag/v0.8.4
          - name: ACTIONS_RUNNER_CONTAINER_HOOKS
            value: /opt/runner-hooks/dist/index.js
          # Allow more time for workflow pods to come online during demand surges.
          # Default is 600s (10 min), which is exceeded when node provisioning +
          # git-cache sync takes longer than expected under concurrent load.
          - name: ACTIONS_RUNNER_PREPARE_JOB_TIMEOUT_SECONDS
            value: "900"
          # Wait for startup taints to clear before creating workflow pods.
          # Prevents Karpenter-scheduler deadlock on fresh nodes where the
          # runner tolerates the taint but the workflow pod does not.
          - name: ACTIONS_RUNNER_WAIT_FOR_NODE_TAINTS
            value: "git-cache-not-ready"
          - name: ACTIONS_RUNNER_WAIT_FOR_NODE_TAINTS_TIMEOUT_SECONDS
            value: "720"
        resources:
          # Runner pod needs enough CPU for the k8s-novolume hook's
          # workspace copy verification (find -exec stat over all files)
          limits:
            cpu: "750m"
            memory: "512Mi"
          requests:
            cpu: "750m"
            memory: "512Mi"
        volumeMounts:
          - name: hook-extensions
            mountPath: /home/runner/hook-extensions
          - name: patched-hooks
            mountPath: /opt/runner-hooks
            readOnly: true

    volumes:
      - name: patched-hooks
        hostPath:
          path: /mnt/runner-container-hooks
          type: DirectoryOrCreate
      - name: hook-extensions
        configMap:
          name: arc-runner-hook-{{RUNNER_NAME_NORMALIZED}}
          items:
            - key: job-pod.yaml
              path: job-pod.yaml
---
# ConfigMap: Job Pod Hook Template for {{RUNNER_NAME}}
# Defines resource requests for workflow job containers in Kubernetes mode
# Runner pod is lightweight; job pods get the heavy resources

apiVersion: v1
kind: ConfigMap
metadata:
  name: arc-runner-hook-{{RUNNER_NAME_NORMALIZED}}
  namespace: arc-runners
  labels:
    osdc.io/module: {{MODULE_NAME}}
data:
  job-pod.yaml: |
    metadata:
      annotations:
        karpenter.sh/do-not-disrupt: "true"
    spec:
      # Job pods run untrusted user code — no Kubernetes API access
      serviceAccountName: arc-workflow
      automountServiceAccountToken: false

      # Prefer scheduling job pods on same instance type as runner.
      # Tolerations enforce instance-type constraints (every NodePool taints
      # with instance-type=<type>:NoSchedule), so nodeSelector is not needed.
      # The hooks inject a weight-100 same-node preference at runtime;
      # this weight-50 preference is the fallback for same-instance-type nodes.
      affinity:
        nodeAffinity:
          preferredDuringSchedulingIgnoredDuringExecution:
            - weight: 50
              preference:
                matchExpressions:
                  - key: instance-type
                    operator: In
                    values:
                      - "{{INSTANCE_TYPE}}"
                  - key: workload-type
                    operator: In
                    values:
                      - github-runner{{GPU_NODE_SELECTOR_AFFINITY}}

      # Tolerate instance-type taint
      tolerations:
        - key: instance-type
          operator: Equal
          value: "{{INSTANCE_TYPE}}"
          effect: NoSchedule{{GPU_JOB_TOLERATIONS}}

      containers:
        - name: "$job"
          # Git reference cache: workflow steps use the CHECKOUT_GIT_CACHE_DIR env
          # var to compose a per-repo reference-repository path for actions/checkout.
          # Examples:
          #   reference-repository: $CHECKOUT_GIT_CACHE_DIR/pytorch      (non-bare, has submodules)
          #   reference-repository: $CHECKOUT_GIT_CACHE_DIR/test-infra.git  (bare)
          # The checkout action uses git alternates + dissociate to borrow objects
          # from the local cache, then repacks locally so the checkout has no
          # runtime dependency on the cache. The DaemonSet git-cache-warmer
          # keeps the cache warm at /mnt/git-cache on each node.
          #
          # GIT_CONFIG_SYSTEM points to a gitconfig with safe.directory=* so that
          # git trusts the root-owned cache repos when resolving alternates.
          env:
            - name: CHECKOUT_GIT_CACHE_DIR
              value: "/opt/git-cache/pytorch"
            - name: GIT_CONFIG_SYSTEM
              value: "/opt/git-cache/.gitconfig"
            - name: PIP_INDEX_URL
              value: "http://pypi-cache-cpu.pypi-cache.svc.cluster.local:8080/simple/"
            - name: PIP_TRUSTED_HOST
              value: "pypi-cache-cpu.pypi-cache.svc.cluster.local"
            - name: UV_DEFAULT_INDEX
              value: "http://pypi-cache-cpu.pypi-cache.svc.cluster.local:8080/simple/"
            - name: UV_INSECURE_HOST
              value: "pypi-cache-cpu.pypi-cache.svc.cluster.local:8080"
            - name: PIP_EXTRA_INDEX_URL
              value: "http://pypi-cache-cpu.pypi-cache.svc.cluster.local:8080/whl/cpu/"
            - name: UV_INDEX
              value: "http://pypi-cache-cpu.pypi-cache.svc.cluster.local:8080/whl/cpu/"
            - name: TORCH_CI_MAX_MEMORY
              value: "{{MEMORY_BYTES}}"
          # Workflow container gets the actual compute resources
          resources:
            requests:
              cpu: "{{VCPU}}"
              memory: "{{MEMORY}}"
              ephemeral-storage: "{{DISK_SIZE}}"{{GPU_REQUEST}}
            limits:
              cpu: "{{VCPU}}"
              memory: "{{MEMORY}}"
              ephemeral-storage: "{{DISK_SIZE}}"{{GPU_LIMIT}}
          volumeMounts:
            - name: git-cache
              mountPath: /opt/git-cache
              readOnly: true
      volumes:
        - name: git-cache
          hostPath:
            path: /mnt/git-cache
            type: DirectoryOrCreate
