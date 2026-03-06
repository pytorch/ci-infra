githubConfigUrl: "{{GITHUB_CONFIG_URL}}"
githubConfigSecret: "{{GITHUB_SECRET_NAME}}"
runnerScaleSetName: "{{RUNNER_NAME_PREFIX}}{{RUNNER_NAME}}"

minRunners: 0

runnerGroup: "default"

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

    containers:
      - name: runner
        image: ghcr.io/actions/actions-runner:latest
        command: ["/home/runner/run.sh"]
        env:
          - name: RUNNER_FEATURE_FLAG_EPHEMERAL
            value: "true"
          # Point to hook template for job pod customization
          - name: ACTIONS_RUNNER_CONTAINER_HOOK_TEMPLATE
            value: /home/runner/hook-extensions/job-pod.yaml
        resources:
          # LIGHTWEIGHT runner pod - job pods get the heavy resources
          # Runner is just an orchestrator, doesn't do the actual work
          limits:
            cpu: "200m"
            memory: "512Mi"
          requests:
            cpu: "200m"
            memory: "512Mi"
        volumeMounts:
          - name: hook-extensions
            mountPath: /home/runner/hook-extensions

    volumes:
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
data:
  job-pod.yaml: |
    spec:
      # Job pods run untrusted user code — no Kubernetes API access
      serviceAccountName: arc-workflow
      automountServiceAccountToken: false

      # Schedule job pods on same instance type as runner
      nodeSelector:
        workload-type: github-runner
        instance-type: "{{INSTANCE_TYPE}}"{{GPU_NODE_SELECTOR}}

      # Tolerate instance-type taint
      tolerations:
        - key: instance-type
          operator: Equal
          value: "{{INSTANCE_TYPE}}"
          effect: NoSchedule{{GPU_JOB_TOLERATIONS}}

      containers:
        - name: "$job"
          # Git object cache: actions/checkout uses git init + fetch (not clone),
          # so --reference is not applicable. Instead we set GIT_ALTERNATE_OBJECT_DIRECTORIES
          # which makes git look for objects in the local bare clone cache before
          # downloading from the remote. This is transparent to workflows — no
          # changes needed in actions/checkout steps. The DaemonSet git-cache-warmer
          # keeps the cache warm at /mnt/git-cache on each node.
          #
          # GIT_CONFIG_SYSTEM points to a gitconfig with safe.directory=* so that
          # git trusts the root-owned cache repos during fetch negotiation (the
          # for_each_alternate_ref subprocess needs to open them to enumerate refs).
          env:
            - name: GIT_ALTERNATE_OBJECT_DIRECTORIES
              value: "/opt/git-cache/pytorch/pytorch.git/objects:/opt/git-cache/pytorch/test-infra.git/objects"
            - name: GIT_CONFIG_SYSTEM
              value: "/opt/git-cache/.gitconfig"
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
