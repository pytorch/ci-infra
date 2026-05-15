githubConfigUrl: "{{GITHUB_CONFIG_URL}}"
githubConfigSecret: "{{GITHUB_SECRET_NAME}}"
runnerScaleSetName: "{{RUNNER_NAME_PREFIX}}{{RUNNER_NAME}}"

minRunners: 0
{{MAX_RUNNERS_LINE}}

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
    gha_capacity_hud_requests_total:
      labels:
        - enterprise
        - organization
        - repository
        - name
        - namespace
        - result
    gha_capacity_pair_creates_total:
      labels:
        - enterprise
        - organization
        - repository
        - name
        - namespace
        - result
    gha_capacity_pair_deletes_total:
      labels:
        - enterprise
        - organization
        - repository
        - name
        - namespace
        - reason
        - result
    gha_capacity_reconcile_skips_total:
      labels:
        - enterprise
        - organization
        - repository
        - name
        - namespace
        - reason
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
    gha_capacity_proactive_capacity:
      labels: ["enterprise", "organization", "repository", "name", "namespace"]
    gha_capacity_hud_enabled:
      labels: ["enterprise", "organization", "repository", "name", "namespace"]
    gha_capacity_queued_jobs:
      labels: ["enterprise", "organization", "repository", "name", "namespace"]
    gha_capacity_desired_pairs:
      labels: ["enterprise", "organization", "repository", "name", "namespace"]
    gha_capacity_pairs:
      labels: ["enterprise", "organization", "repository", "name", "namespace"]
    gha_capacity_running_pairs:
      labels: ["enterprise", "organization", "repository", "name", "namespace"]
    gha_capacity_placeholder_pods:
      labels: ["enterprise", "organization", "repository", "name", "namespace", "role", "phase"]
    gha_capacity_advertised_max_runners:
      labels: ["enterprise", "organization", "repository", "name", "namespace"]
    gha_capacity_reconcile_last_success_timestamp_seconds:
      labels: ["enterprise", "organization", "repository", "name", "namespace", "phase"]
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
    gha_capacity_reconcile_duration_seconds:
      labels:
        - enterprise
        - organization
        - repository
        - name
        - namespace
        - phase
      buckets:
        [0.1, 0.25, 0.5, 1, 2, 5, 10, 30, 60]
    gha_capacity_hud_request_duration_seconds:
      labels:
        - enterprise
        - organization
        - repository
        - name
        - namespace
        - result
      buckets:
        [0.05, 0.1, 0.25, 0.5, 1, 2, 5, 10, 30]

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
        env:
          - name: CAPACITY_AWARE_ENABLED
            value: "true"
          - name: CAPACITY_AWARE_PROACTIVE_CAPACITY
            value: "{{PROACTIVE_CAPACITY}}"
          - name: CAPACITY_AWARE_MAX_BURST_CAPACITY
            value: "{{MAX_BURST_CAPACITY}}"
          # CAPACITY_AWARE_RECALCULATE_INTERVAL is intentionally unset so the
          # listener's compiled-in default wins. Old chart (<= jeanschmidt.9):
          # default is 30s. New chart (jeanschmidt.10+, after the PR at
          # jeanschmidt/actions-runner-controller#5 lands): default is 60s plus
          # startup jitter to desync the ~50 listeners after a rollout.
          - name: CAPACITY_AWARE_PLACEHOLDER_TIMEOUT
            value: "20m"
          - name: CAPACITY_AWARE_WORKFLOW_CPU
            value: "{{VCPU}}"
          - name: CAPACITY_AWARE_WORKFLOW_MEMORY
            value: "{{MEMORY}}"
          - name: CAPACITY_AWARE_WORKFLOW_GPU
            value: "{{GPU_COUNT}}"
          - name: CAPACITY_AWARE_WORKFLOW_DISK
            value: "{{DISK_SIZE}}"
          # Must match the runner container resources below (requests/limits)
          - name: CAPACITY_AWARE_RUNNER_CPU
            value: "750m"
          - name: CAPACITY_AWARE_RUNNER_MEMORY
            value: "1Gi"
          - name: CAPACITY_AWARE_NODE_FLEET
            value: "{{NODE_FLEET}}"
          - name: CAPACITY_AWARE_RUNNER_NODE_FLEET
            value: "c7i-runner"
          - name: CAPACITY_AWARE_RUNNER_CLASS
            value: "{{RUNNER_CLASS}}"
          - name: CAPACITY_AWARE_HUD_API_URL
            value: "https://hud.pytorch.org/api/clickhouse/queued_jobs_aggregate?parameters=%7B%22queuedThresholdMinutes%22%3A0%2C%22maxAgeDays%22%3A3%2C%22orgs%22%3A%5B%22pytorch%22%5D%2C%22repo%22%3A%22%22%7D"
          - name: CAPACITY_AWARE_HUD_API_TOKEN
            valueFrom:
              secretKeyRef:
                name: pytorch-hud-token
                key: token
                optional: true

template:
  metadata:
    annotations:
      karpenter.sh/do-not-disrupt: "true"
  spec:
    serviceAccountName: arc-runner
    # Priority 0 — preempts placeholder-runner (-10), does NOT preempt
    # placeholder-workflow (10). Required for the proactive-capacity
    # preemption ladder to behave deterministically.
    priorityClassName: arc-runner

    # Pin runner pods to the dedicated c7i-runner pool (separate from the
    # workflow pool defined by {{NODE_FLEET}} on the ConfigMap below). This
    # topology split is what makes preemption victim selection deterministic
    # — see PROACTIVE_CAPACITY.md "Dedicated Runner NodePool".
    # Runner-class isolation (osdc.io/runner-class) is a workflow-pool
    # concern only; c7i-runner nodes carry no runner-class label.
    nodeSelector:
      workload-type: github-runner
      node-fleet: "c7i-runner"

    # Tolerate node-fleet + instance-type taints. The c7i-runner NodePool
    # inherits the git-cache-not-ready startupTaint from the unconditional
    # generator emission; the git-cache-warmer DaemonSet does not run on
    # this pool, so the taint is never cleared. Tolerating it lets runner
    # pods schedule despite the persistent taint. The runner pod itself
    # does NOT use the git-cache mount — only the toleration is required
    # for scheduling.
    tolerations:
      - key: node-fleet
        operator: Equal
        value: "c7i-runner"
        effect: NoSchedule
      - key: instance-type
        operator: Exists
        effect: NoSchedule
      - key: git-cache-not-ready
        operator: Exists
        effect: NoSchedule

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
          # See: https://github.com/jeanschmidt/runner-container-hooks/releases/tag/v0.8.12
          - name: ACTIONS_RUNNER_CONTAINER_HOOKS
            value: /home/runner/hook-extensions/wrapper.js
          # PyTorch CI workflows depend on Docker images built in parallel by
          # pytorch/pytorch's docker-builds.yml. Consumer jobs use test-infra's
          # calculate-docker-image action, which polls ECR for the built image
          # for up to 2h. While the image is still building, the workflow pod
          # sits in ImagePullBackOff and the container hook stays in
          # prepare_job's waitForPodPhases. Both runner and workflow pods are
          # already protected from node disruption via the
          # karpenter.sh/do-not-disrupt annotation set on each template; this
          # timeout is the last knob that would otherwise fail the job before
          # the image arrives.
          - name: ACTIONS_RUNNER_PREPARE_JOB_TIMEOUT_SECONDS
            value: "7200"
          # Memory management: the runner pod has 1Gi shared between the .NET
          # runner agent and Node.js container hooks. Without explicit caps,
          # .NET claims 75% (768Mi) and Node.js claims 50% (512Mi) of the
          # cgroup — combined 1280Mi > 1024Mi, still guaranteed OOM.
          # Bumped from 512Mi to give native Node.js stdio buffers (held open
          # by slow CRI exec during pod-density bursts) headroom; observed
          # OOMs trace back to native buffers, not V8 heap or .NET managed heap.
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
          # workspace tar copy/extract and permission fixups.
          # Must match CAPACITY_AWARE_RUNNER_CPU/MEMORY on the listener.
          limits:
            cpu: "750m"
            memory: "1Gi"
          requests:
            cpu: "750m"
            memory: "1Gi"
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
      # Priority 20 — preempts placeholder-workflow (10) so workflow pods
      # can claim the capacity reserved by the placeholders they replace.
      priorityClassName: arc-workflow

      # Prefer scheduling job pods on same node fleet as runner.
      # Tolerations enforce node-fleet constraints (every NodePool taints
      # with node-fleet=<fleet>:NoSchedule), so nodeSelector is not needed.
      # This weight-50 preference biases the scheduler toward same-fleet
      # nodes when multiple fleets could host the workflow pod.
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
            # K8s default /dev/shm is 64Mi (container runtime tmpfs). NCCL
            # blows past that on multi-GPU workloads; PyTorch docker-based CI
            # also runs with --shm-size=1g-2g, so match that ceiling for all
            # workflow pods.
            - name: dshm
              mountPath: /dev/shm
      volumes:
        - name: git-cache
          hostPath:
            path: /mnt/git-cache
            type: DirectoryOrCreate
        - name: dshm
          emptyDir:
            medium: Memory
            sizeLimit: 2Gi
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

    // Result shape: { code, signal }. When the child is killed by a signal
    // Node.js reports code === null and signal !== null; we preserve both so
    // callers can distinguish cancellation (signal) from script failure (code).
    function spawnReal(stdinData) {
      return new Promise((resolve) => {
        const child = spawn(process.execPath, [REAL_HOOKS], {
          stdio: ['pipe', 'inherit', 'inherit'],
        });
        activeChild = child;
        child.stdin.write(stdinData);
        child.stdin.end();
        child.on('close', (code, signal) => {
          activeChild = null;
          if (signal) {
            resolve({ code: null, signal: signal });
            return;
          }
          resolve({ code: code !== null ? code : 1, signal: null });
        });
        child.on('error', (err) => {
          activeChild = null;
          emit('warning', '[OSDC] Hook spawn error: ' + err.message);
          resolve({ code: 1, signal: null });
        });
      });
    }

    // Coerce a {code, signal} result into a numeric exit code for the parent.
    // SIGINT -> 130, SIGTERM -> 143 (POSIX 128+signum convention).
    function exitCodeFor(result) {
      if (result.signal === 'SIGINT') return 130;
      if (result.signal === 'SIGTERM') return 143;
      if (result.signal) return 128;
      return result.code;
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
        return exitCodeFor(await spawnReal(stdinData));
      }

      const cmd = input.command;
      if (cmd !== 'run_script_step' && cmd !== 'run_container_step') {
        return exitCodeFor(await spawnReal(stdinData));
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
      //
      // Three outcomes worth distinguishing in the log:
      //   1. Cancellation: child was killed by a signal we forwarded from
      //      GitHub Actions, OR the child handled the signal itself and
      //      exited with the canonical 128+signum convention. The hook
      //      (runner-container-hooks v0.8.12+) installs SIGTERM/SIGINT
      //      handlers that run cleanup and then process.exit(143|130),
      //      which collapses signal info to a numeric code. We treat those
      //      two exact codes as cancellation. NOT a script/workflow error.
      //   2. Script failure: child exited with non-zero code (any other).
      //   3. Success: nothing to print.
      const result = await spawnReal(stdinData);
      const cancelSignal =
        result.signal ||
        (result.code === 130 ? 'SIGINT' :
         result.code === 143 ? 'SIGTERM' : null);
      if (cancelSignal) {
        emit('error',
          '[OSDC] Step cancelled by GitHub Actions (received ' + cancelSignal + '). ' +
          'This typically means matrix fail-fast, a manual cancel, a workflow ' +
          'concurrency override, or a run-level cancellation — NOT an OSDC ' +
          'infrastructure failure. Check the workflow-run page for the actual ' +
          'cause; the in-pod subprocess is terminated by the hook so post-cleanup ' +
          'steps can run.'
        );
        return exitCodeFor(result);
      }
      if (result.code !== 0) {
        emit('error',
          '[OSDC] Step script exited with code ' + result.code + '. ' +
          'This is a script/workflow error, not an infrastructure issue. ' +
          'Check the step logs above for the actual failure.'
        );
      }
      return result.code;
    }

    // Fail-open: read stdin once, run main, fall back on crash
    readStdin().then(async (stdinData) => {
      try {
        process.exit(await main(stdinData));
      } catch (err) {
        emit('warning', '[OSDC] Wrapper error (fail-open): ' + err.message);
        try {
          process.exit(exitCodeFor(await spawnReal(stdinData)));
        } catch (_) {
          process.exit(1);
        }
      }
    });
