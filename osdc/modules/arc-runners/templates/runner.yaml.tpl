githubConfigUrl: "{{GITHUB_CONFIG_URL}}"
githubConfigSecret: "{{GITHUB_SECRET_NAME}}"
runnerScaleSetName: "{{RUNNER_NAME_PREFIX}}{{RUNNER_NAME}}"

minRunners: 0

runnerGroup: "{{RUNNER_GROUP}}"

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
      node-fleet: "{{NODE_FLEET}}"
{{RUNNER_CLASS_NODE_SELECTOR}}
{{RUNNER_CLASS_AFFINITY}}
    # Tolerate node-fleet + instance-type taints and git-cache startup taint
    tolerations:
      - key: node-fleet
        operator: Equal
        value: "{{NODE_FLEET}}"
        effect: NoSchedule
      - key: instance-type
        operator: Exists
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
            set -e
            TIMEOUT=300
            ELAPSED=0
            echo "Waiting for patched hooks at /mnt/host-hooks/dist/index.js..."
            while [ ! -f /mnt/host-hooks/dist/index.js ]; do
              if [ "$ELAPSED" -ge "$TIMEOUT" ]; then
                echo "ERROR: Timed out waiting for patched hooks after ${TIMEOUT}s"
                exit 1
              fi
              sleep 10
              ELAPSED=$((ELAPSED + 10))
            done

            # Snapshot: copy hooks from hostPath to emptyDir
            cp -a /mnt/host-hooks/dist/ /opt/runner-hooks/dist/
            if [ -f /mnt/host-hooks/.version ]; then
              cp /mnt/host-hooks/.version /opt/runner-hooks/.version
            fi

            # Verify the snapshot
            if [ ! -f /opt/runner-hooks/dist/index.js ]; then
              echo "ERROR: Snapshot failed — index.js missing after copy"
              exit 1
            fi

            VERSION=$(cat /opt/runner-hooks/.version 2>/dev/null || echo "unknown")
            SIZE=$(wc -c < /opt/runner-hooks/dist/index.js)
            echo "Hooks v${VERSION} snapshot complete (${SIZE} bytes)."
        volumeMounts:
          - name: patched-hooks
            mountPath: /mnt/host-hooks
            readOnly: true
          - name: hooks-snapshot
            mountPath: /opt/runner-hooks

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
          # Use OSDC wrapper that validates env vars and surfaces errors
          # clearly, then delegates to patched hooks from DaemonSet.
          # See: https://github.com/jeanschmidt/runner-container-hooks/releases/tag/v0.8.4
          - name: ACTIONS_RUNNER_CONTAINER_HOOKS
            value: /home/runner/hook-extensions/wrapper.js
          # Allow more time for workflow pods to come online during demand surges.
          # Default is 600s (10 min), which is exceeded when node provisioning +
          # git-cache sync takes longer than expected under concurrent load.
          - name: ACTIONS_RUNNER_PREPARE_JOB_TIMEOUT_SECONDS
            value: "1500"
          # Wait for startup taints to clear before creating workflow pods.
          # Prevents Karpenter-scheduler deadlock on fresh nodes where the
          # runner tolerates the taint but the workflow pod does not.
          - name: ACTIONS_RUNNER_WAIT_FOR_NODE_TAINTS
            value: "git-cache-not-ready"
          - name: ACTIONS_RUNNER_WAIT_FOR_NODE_TAINTS_TIMEOUT_SECONDS
            value: "720"
          # Memory management: the runner pod shares 512Mi between the .NET
          # runner agent and Node.js container hooks. Without explicit caps,
          # .NET claims 75% (384Mi) and Node.js claims 50% (256Mi) of the
          # cgroup — combined 640Mi > 512Mi, guaranteed OOM.
          # GCHeapHardLimit is hex: 0xC800000 = 200 MiB managed heap
          - name: DOTNET_GCHeapHardLimit
            value: "C800000"
          # Scale 0-9: higher = more aggressive GC, less memory, more pauses
          - name: DOTNET_GCConserveMemory
            value: "5"
          # Cap Node.js V8 old-space (hooks process). Note: does not cover
          # native allocations (tar stream buffers), only V8 heap.
          - name: NODE_OPTIONS
            value: "--max-old-space-size=128"
          # Re-enable post-copy hash verification for workspace tar copies.
          # Without verification, corrupted copies silently break $GITHUB_ENV
          # propagation between steps (env vars from prior steps are lost).
          - name: ACTIONS_RUNNER_COPY_VERIFY_ENABLED
            value: "true"
          - name: ACTIONS_RUNNER_COPY_VERIFY_RETRIES
            value: "3"
        resources:
          # Runner pod needs enough CPU for the k8s-novolume hook's
          # workspace tar copy/extract and permission fixups
          limits:
            cpu: "750m"
            memory: "512Mi"
          requests:
            cpu: "750m"
            memory: "512Mi"
        volumeMounts:
          - name: hook-extensions
            mountPath: /home/runner/hook-extensions
          - name: hooks-snapshot
            mountPath: /opt/runner-hooks
            readOnly: true

    volumes:
      - name: patched-hooks
        hostPath:
          path: /mnt/runner-container-hooks
          type: DirectoryOrCreate
      - name: hooks-snapshot
        emptyDir:
          sizeLimit: 50Mi
      - name: hook-extensions
        configMap:
          name: arc-runner-hook-{{RUNNER_NAME_NORMALIZED}}
          items:
            - key: job-pod.yaml
              path: job-pod.yaml
            - key: wrapper.js
              path: wrapper.js
          defaultMode: 0755
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

      # Prefer scheduling job pods on same node fleet as runner.
      # Tolerations enforce node-fleet constraints (every NodePool taints
      # with node-fleet=<fleet>:NoSchedule), so nodeSelector is not needed.
      # The hooks inject a weight-100 same-node preference at runtime;
      # this weight-50 preference is the fallback for same-fleet nodes.
      affinity:
        nodeAffinity:
{{RUNNER_CLASS_JOB_AFFINITY}}          preferredDuringSchedulingIgnoredDuringExecution:
            - weight: 50
              preference:
                matchExpressions:
                  - key: node-fleet
                    operator: In
                    values:
                      - "{{NODE_FLEET}}"
                  - key: workload-type
                    operator: In
                    values:
                      - github-runner{{GPU_NODE_SELECTOR_AFFINITY}}

      # Tolerate node-fleet + instance-type taints
      tolerations:
        - key: node-fleet
          operator: Equal
          value: "{{NODE_FLEET}}"
          effect: NoSchedule
        - key: instance-type
          operator: Exists
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
            - name: PIP_EXTRA_INDEX_URL
              value: "http://pypi-cache-cpu.pypi-cache.svc.cluster.local:8080/whl/cpu/"
            - name: UV_DEFAULT_INDEX
              value: "http://pypi-cache-cpu.pypi-cache.svc.cluster.local:8080/simple/"
            - name: UV_INSECURE_HOST
              value: "pypi-cache-cpu.pypi-cache.svc.cluster.local:8080"
            - name: UV_INDEX
              value: "http://pypi-cache-cpu.pypi-cache.svc.cluster.local:8080/whl/cpu/"
            - name: UV_INDEX_STRATEGY
              value: "unsafe-best-match"
            - name: PYPI_CACHE_SIMPLE_URL
              value: "http://pypi-cache-cpu.pypi-cache.svc.cluster.local:8080/simple/"
            - name: PYPI_CACHE_WHL_URL
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
  wrapper.js: |
    #!/usr/bin/env node
    'use strict';
    //
    // OSDC Hook Wrapper — Enhanced error surfacing for runner-container-hooks
    //
    // Intercepts run_script_step and run_container_step to:
    //   1. Validate environment variables for known bad patterns (API errors,
    //      rate limit responses, HTML error pages) before script execution
    //   2. Surface actual exit codes with clear "script error, not infra" messages
    //
    // All other commands (prepare_job, cleanup_job) pass through unchanged.
    // Fail-open: if this wrapper crashes, it exits with a warning — never blocks jobs.
    //
    const { spawn } = require('child_process');
    const REAL_HOOKS = '/opt/runner-hooks/dist/index.js';

    // GitHub context vars that legitimately contain JSON — skip validation
    const SKIP_VARS = new Set([
      'GITHUB_EVENT', 'GITHUB_CONTEXT', 'GITHUB_EVENT_PATH',
      'RUNNER_CONTEXT', 'STEPS_CONTEXT', 'NEEDS_CONTEXT',
      'INPUTS_CONTEXT', 'MATRIX_CONTEXT', 'STRATEGY_CONTEXT',
      'ENV_CONTEXT', 'VARS_CONTEXT', 'JOB_CONTEXT',
    ]);

    // Bad patterns indicating corrupted/error content in env vars.
    // Order matters: specific patterns before generic documentation_url catch-all.
    const BAD_PATTERNS = [
      { re: /"message"\s*:\s*"API rate limit exceeded/, label: 'GitHub API rate limit error' },
      { re: /"message"\s*:\s*"Bad credentials"/, label: 'GitHub auth error' },
      { re: /"message"\s*:\s*"Not Found"/, label: 'GitHub 404 error' },
      { re: /"documentation_url"\s*:\s*"https:\/\/docs\.github\.com/, label: 'GitHub API error response' },
      { re: /<!DOCTYPE\s+html>/i, label: 'HTML error page' },
      { re: /<html[\s>]/i, label: 'HTML content' },
    ];

    const MIN_LENGTH = 30;

    function validateEnvVars(envVars) {
      const problems = [];
      if (!envVars || typeof envVars !== 'object') return problems;
      for (const [name, value] of Object.entries(envVars)) {
        if (SKIP_VARS.has(name)) continue;
        if (typeof value !== 'string' || value.length < MIN_LENGTH) continue;
        for (const { re, label } of BAD_PATTERNS) {
          if (re.test(value)) {
            problems.push({ name, label, snippet: value.slice(0, 200) });
            break;
          }
        }
      }
      return problems;
    }

    function emit(level, msg) {
      process.stdout.write('::' + level + '::' + msg.replace(/\n/g, '%0A') + '\n');
    }

    // Track child process for signal forwarding during pod eviction/cancellation
    let activeChild = null;

    function forwardSignal(signal) {
      if (activeChild) {
        activeChild.kill(signal);
      }
    }
    process.on('SIGTERM', () => forwardSignal('SIGTERM'));
    process.on('SIGINT', () => forwardSignal('SIGINT'));

    function spawnReal(stdinData) {
      return new Promise((resolve) => {
        const child = spawn(process.execPath, [REAL_HOOKS], {
          stdio: ['pipe', 'inherit', 'inherit'],
        });
        activeChild = child;
        child.stdin.write(stdinData);
        child.stdin.end();
        child.on('close', (code) => {
          activeChild = null;
          resolve(code !== null ? code : 1);
        });
        child.on('error', (err) => {
          activeChild = null;
          emit('warning', '[OSDC] Hook spawn error: ' + err.message);
          resolve(1);
        });
      });
    }

    async function readStdin() {
      let data = '';
      for await (const chunk of process.stdin) data += chunk;
      return data;
    }

    async function main(stdinData) {
      let input;
      try {
        input = JSON.parse(stdinData);
      } catch (_) {
        return await spawnReal(stdinData);
      }

      const cmd = input.command;
      if (cmd !== 'run_script_step' && cmd !== 'run_container_step') {
        return await spawnReal(stdinData);
      }

      // Feature 2: Validate environment variables
      const envVars = input.args && input.args.environmentVariables;
      const problems = validateEnvVars(envVars);
      if (problems.length > 0) {
        const details = problems
          .map((p) => '  ' + p.name + ': ' + p.label + '%0A    Content: ' + p.snippet + '...')
          .join('%0A');
        emit('error',
          '[OSDC] Corrupted environment variables detected — this is an upstream ' +
          'workflow issue, not an infrastructure problem.%0A%0A' +
          'The following variables contain error responses instead of expected values:%0A' +
          details + '%0A%0A' +
          'This typically happens when a GitHub API call fails (e.g., rate limiting) ' +
          'and the error response is captured into a variable without validation. ' +
          'The upstream workflow needs to add error checking (set -o pipefail, ' +
          'validate API responses before using them).'
        );
        return 1;
      }

      // Feature 1: Enhanced exit code surfacing
      const code = await spawnReal(stdinData);
      if (code !== 0) {
        emit('error',
          '[OSDC] Step script exited with code ' + code + '. ' +
          'This is a script/workflow error, not an infrastructure issue. ' +
          'Check the step logs above for the actual failure.'
        );
      }
      return code;
    }

    // Fail-open: read stdin once, run main, fall back on crash
    readStdin().then(async (stdinData) => {
      try {
        process.exit(await main(stdinData));
      } catch (err) {
        emit('warning', '[OSDC] Wrapper error (fail-open): ' + err.message);
        try {
          process.exit(await spawnReal(stdinData));
        } catch (_) {
          process.exit(1);
        }
      }
    });
