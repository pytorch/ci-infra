import * as core from '@actions/core'
import * as path from 'path'
import { spawn } from 'child_process'
import * as k8s from '@kubernetes/client-node'
import tar from 'tar-fs'
import * as stream from 'stream'
import { WritableStreamBuffer } from 'stream-buffers'
import { createHash } from 'crypto'
import type { ContainerInfo, Registry } from 'hooklib'
import {
  getSecretName,
  JOB_CONTAINER_NAME,
  RunnerInstanceLabel
} from '../hooks/constants'
import {
  PodPhase,
  mergePodSpecWithOptions,
  mergeObjectMeta,
  fixArgs,
  listDirAllCommand,
  sleep,
  EXTERNALS_VOLUME_NAME,
  GITHUB_VOLUME_NAME,
  WORK_VOLUME
} from './utils'
import * as shlex from 'shlex'

const kc = new k8s.KubeConfig()

kc.loadFromDefault()

const k8sApi = kc.makeApiClient(k8s.CoreV1Api)
const k8sBatchV1Api = kc.makeApiClient(k8s.BatchV1Api)
const k8sAuthorizationV1Api = kc.makeApiClient(k8s.AuthorizationV1Api)

const DEFAULT_WAIT_FOR_POD_TIME_SECONDS = 10 * 60 // 10 min

const ENV_WAIT_FOR_NODE_TAINTS = 'ACTIONS_RUNNER_WAIT_FOR_NODE_TAINTS'
const ENV_WAIT_FOR_NODE_TAINTS_TIMEOUT =
  'ACTIONS_RUNNER_WAIT_FOR_NODE_TAINTS_TIMEOUT_SECONDS'
const DEFAULT_WAIT_FOR_NODE_TAINTS_TIMEOUT_SECONDS = 300

// Workspace copy verification: disabled by default to reduce runner pod memory
// pressure. The verification loop runs `find -exec stat` across all workspace
// files, accumulates the output as a JS string, and retries — each retry
// re-running the full scan. For large repos this causes OOM kills in the
// 512Mi runner pod. The verification is best-effort anyway (silently continues
// after exhausting retries), so disabling it loses no hard guarantees.
//
// WARNING: If re-enabling verification, ensure NODE_OPTIONS --max-old-space-size
// is >= 256 MB, otherwise the `find` output accumulation will crash V8 with a
// FATAL ERROR for large workspaces (e.g. pytorch/pytorch).
const COPY_VERIFY_ENABLED =
  (process.env.ACTIONS_RUNNER_COPY_VERIFY_ENABLED || 'false').toLowerCase() ===
  'true'
const COPY_VERIFY_RETRIES = parseInt(
  process.env.ACTIONS_RUNNER_COPY_VERIFY_RETRIES || '3',
  10
)

/**
 * Wait for configurable startup taints to be removed from the runner's node.
 *
 * Reads ACTIONS_RUNNER_WAIT_FOR_NODE_TAINTS (comma-separated taint keys) and
 * polls the node until none of those taints remain. This prevents the
 * Karpenter-scheduler deadlock where workflow pods are created before startup
 * taints (e.g. git-cache-not-ready) have cleared.
 *
 * Non-fatal if runner pod or node lookup fails. Returns immediately if the
 * env var is unset.
 */
export async function waitForNodeTaintsRemoval(): Promise<void> {
  const taintKeysRaw = process.env[ENV_WAIT_FOR_NODE_TAINTS]
  if (!taintKeysRaw || taintKeysRaw.trim() === '') {
    return
  }

  const taintKeys = taintKeysRaw
    .split(',')
    .map(k => k.trim())
    .filter(k => k.length > 0)
  if (taintKeys.length === 0) {
    return
  }

  const timeoutStr = process.env[ENV_WAIT_FOR_NODE_TAINTS_TIMEOUT]
  const timeoutSeconds = timeoutStr
    ? parseInt(timeoutStr, 10)
    : DEFAULT_WAIT_FOR_NODE_TAINTS_TIMEOUT_SECONDS
  const effectiveTimeout =
    !timeoutSeconds || timeoutSeconds <= 0
      ? DEFAULT_WAIT_FOR_NODE_TAINTS_TIMEOUT_SECONDS
      : timeoutSeconds

  const runnerPodName = process.env.ACTIONS_RUNNER_POD_NAME
  if (!runnerPodName) {
    core.warning('ACTIONS_RUNNER_POD_NAME not set, skipping node taint wait')
    return
  }

  let nodeName: string | undefined
  try {
    const runnerPod = await k8sApi.readNamespacedPod({
      name: runnerPodName,
      namespace: namespace()
    })
    nodeName = runnerPod.spec?.nodeName
  } catch (err) {
    core.warning(`Could not look up runner pod for node taint wait: ${err}`)
    return
  }

  if (!nodeName) {
    core.warning(
      'Runner pod has no nodeName assigned, skipping node taint wait'
    )
    return
  }

  const pollIntervalMs = 5000
  let elapsed = 0

  core.info(
    `Waiting for node ${nodeName} taints to clear: [${taintKeys.join(', ')}] (timeout: ${effectiveTimeout}s)`
  )

  while (elapsed < effectiveTimeout) {
    try {
      const node = await k8sApi.readNode({ name: nodeName })
      const activeTaints = (node.spec?.taints || [])
        .filter(t => taintKeys.includes(t.key))
        .map(t => t.key)

      if (activeTaints.length === 0) {
        core.info(
          `All configured taints cleared on node ${nodeName} after ${elapsed}s`
        )
        return
      }

      core.info(
        `Node ${nodeName} still has taints: [${activeTaints.join(', ')}] (${elapsed}s/${effectiveTimeout}s)`
      )
    } catch (err) {
      core.warning(`Failed to read node ${nodeName}: ${err}`)
    }

    await sleep(pollIntervalMs)
    elapsed += pollIntervalMs / 1000
  }

  throw new Error(
    `Timed out after ${effectiveTimeout}s waiting for node ${nodeName} taints to clear: [${taintKeys.join(', ')}]`
  )
}

export const requiredPermissions = [
  {
    group: '',
    verbs: ['get', 'list', 'create', 'delete'],
    resource: 'pods',
    subresource: ''
  },
  {
    group: '',
    verbs: ['get', 'create'],
    resource: 'pods',
    subresource: 'exec'
  },
  {
    group: '',
    verbs: ['get', 'list', 'watch'],
    resource: 'pods',
    subresource: 'log'
  },
  {
    group: '',
    verbs: ['create', 'delete', 'get', 'list'],
    resource: 'secrets',
    subresource: ''
  }
]

export async function createJobPod(
  name: string,
  jobContainer?: k8s.V1Container,
  services?: k8s.V1Container[],
  registry?: Registry,
  extension?: k8s.V1PodTemplateSpec
): Promise<k8s.V1Pod> {
  const containers: k8s.V1Container[] = []
  if (jobContainer) {
    containers.push(jobContainer)
  }
  if (services?.length) {
    containers.push(...services)
  }

  const appPod = new k8s.V1Pod()

  appPod.apiVersion = 'v1'
  appPod.kind = 'Pod'

  appPod.metadata = new k8s.V1ObjectMeta()
  appPod.metadata.name = name

  const instanceLabel = new RunnerInstanceLabel()
  appPod.metadata.labels = {
    [instanceLabel.key]: instanceLabel.value
  }
  appPod.metadata.annotations = {}

  appPod.spec = new k8s.V1PodSpec()
  appPod.spec.containers = containers
  appPod.spec.securityContext = {
    fsGroup: 1001
  }

  // Extract working directory from GITHUB_WORKSPACE
  // GITHUB_WORKSPACE is like /__w/repo-name/repo-name
  const githubWorkspace = process.env.GITHUB_WORKSPACE
  const workingDirPath = githubWorkspace?.split('/').slice(-2).join('/') ?? ''

  const initCommands = [
    'mkdir -p /mnt/externals',
    'mkdir -p /mnt/work',
    'mkdir -p /mnt/github',
    'mv /home/runner/externals/* /mnt/externals/'
  ]

  if (workingDirPath) {
    initCommands.push(`mkdir -p /mnt/work/${workingDirPath}`)
  }

  appPod.spec.initContainers = [
    {
      name: 'fs-init',
      image:
        process.env.ACTIONS_RUNNER_IMAGE ||
        'ghcr.io/actions/actions-runner:latest',
      command: ['sh', '-c', initCommands.join(' && ')],
      securityContext: {
        runAsGroup: 1001,
        runAsUser: 1001
      },
      volumeMounts: [
        {
          name: EXTERNALS_VOLUME_NAME,
          mountPath: '/mnt/externals'
        },
        {
          name: WORK_VOLUME,
          mountPath: '/mnt/work'
        },
        {
          name: GITHUB_VOLUME_NAME,
          mountPath: '/mnt/github'
        }
      ]
    }
  ]

  appPod.spec.restartPolicy = 'Never'

  appPod.spec.volumes = [
    {
      name: EXTERNALS_VOLUME_NAME,
      emptyDir: {}
    },
    {
      name: GITHUB_VOLUME_NAME,
      emptyDir: {}
    },
    {
      name: WORK_VOLUME,
      emptyDir: {}
    }
  ]

  if (registry) {
    const secret = await createDockerSecret(registry)
    if (!secret?.metadata?.name) {
      throw new Error(`created secret does not have secret.metadata.name`)
    }
    const secretReference = new k8s.V1LocalObjectReference()
    secretReference.name = secret.metadata.name
    appPod.spec.imagePullSecrets = [secretReference]
  }

  if (extension?.metadata) {
    mergeObjectMeta(appPod, extension.metadata)
  }

  if (extension?.spec) {
    mergePodSpecWithOptions(appPod.spec, extension.spec)
  }

  return await k8sApi.createNamespacedPod({
    namespace: namespace(),
    body: appPod
  })
}

export async function createContainerStepPod(
  name: string,
  container: k8s.V1Container,
  extension?: k8s.V1PodTemplateSpec
): Promise<k8s.V1Pod> {
  const appPod = new k8s.V1Pod()

  appPod.apiVersion = 'v1'
  appPod.kind = 'Pod'

  appPod.metadata = new k8s.V1ObjectMeta()
  appPod.metadata.name = name

  const instanceLabel = new RunnerInstanceLabel()
  appPod.metadata.labels = {
    [instanceLabel.key]: instanceLabel.value
  }
  appPod.metadata.annotations = {}

  appPod.spec = new k8s.V1PodSpec()
  appPod.spec.containers = [container]

  appPod.spec.restartPolicy = 'Never'

  appPod.spec.volumes = [
    {
      name: EXTERNALS_VOLUME_NAME,
      emptyDir: {}
    },
    {
      name: GITHUB_VOLUME_NAME,
      emptyDir: {}
    },
    {
      name: WORK_VOLUME,
      emptyDir: {}
    }
  ]

  if (extension?.metadata) {
    mergeObjectMeta(appPod, extension.metadata)
  }

  if (extension?.spec) {
    mergePodSpecWithOptions(appPod.spec, extension.spec)
  }

  return await k8sApi.createNamespacedPod({
    namespace: namespace(),
    body: appPod
  })
}

export async function deletePod(name: string): Promise<void> {
  await k8sApi.deleteNamespacedPod({
    name,
    namespace: namespace(),
    gracePeriodSeconds: 0
  })
}

/**
 * Extract the numeric exit code from a K8s exec V1Status response.
 * The exit code is buried in `details.causes` with `reason: "ExitCode"`.
 * Returns null if no exit code is found (infrastructure error, not a process exit).
 */
export function extractExitCode(resp: k8s.V1Status): number | null {
  if (!resp?.details?.causes) return null
  const cause = resp.details.causes.find(c => c.reason === 'ExitCode')
  if (!cause?.message) return null
  const code = parseInt(cause.message, 10)
  return Number.isNaN(code) ? null : code
}

export async function execPodStepWithRetry(
  command: string[],
  podName: string,
  containerName: string,
  description: string,
  maxAttempts = 6,
  initialDelayMs = 1000
): Promise<number> {
  let lastError: Error | undefined
  for (let attempt = 1; attempt <= maxAttempts; attempt++) {
    try {
      return await execPodStep(command, podName, containerName)
    } catch (error) {
      lastError = error instanceof Error ? error : new Error(String(error))
      if (attempt < maxAttempts) {
        const delay = initialDelayMs * Math.pow(3, attempt - 1)
        core.debug(
          `execPodStepWithRetry(${description}): attempt ${attempt}/${maxAttempts} failed (${lastError.message}), retrying in ${delay}ms`
        )
        await sleep(delay)
      }
    }
  }
  throw new Error(
    `execPodStepWithRetry(${description}) failed after ${maxAttempts} attempts: ${lastError?.message}`
  )
}

export async function execPodStepOutputWithRetry(
  command: string[],
  podName: string,
  containerName: string,
  description: string,
  options: {
    retryOnNonZeroExit?: boolean
    maxAttempts?: number
    initialDelayMs?: number
  } = {}
): Promise<{ exitCode: number; stdout: string }> {
  const {
    retryOnNonZeroExit = false,
    maxAttempts = 6,
    initialDelayMs = 1000
  } = options
  let lastError: Error | undefined
  for (let attempt = 1; attempt <= maxAttempts; attempt++) {
    try {
      const result = await execPodStepOutput(command, podName, containerName)
      if (!retryOnNonZeroExit || result.exitCode === 0) {
        return result
      }
      lastError = new Error(
        `non-zero exit code ${result.exitCode} (stdout: ${result.stdout || 'none'})`
      )
    } catch (error) {
      lastError = error instanceof Error ? error : new Error(String(error))
    }
    if (attempt < maxAttempts) {
      const delay = initialDelayMs * Math.pow(3, attempt - 1)
      core.debug(
        `execPodStepOutputWithRetry(${description}): attempt ${attempt}/${maxAttempts} failed (${lastError?.message}), retrying in ${delay}ms`
      )
      await sleep(delay)
    }
  }
  throw new Error(
    `execPodStepOutputWithRetry(${description}) failed after ${maxAttempts} attempts: ${lastError?.message}`
  )
}

export async function execPodStep(
  command: string[],
  podName: string,
  containerName: string,
  stdin?: stream.Readable
): Promise<number> {
  const exec = new k8s.Exec(kc)

  command = fixArgs(command)
  return await new Promise(function (resolve, reject) {
    let settled = false
    exec
      .exec(
        namespace(),
        podName,
        containerName,
        command,
        process.stdout,
        process.stderr,
        stdin ?? null,
        false /* tty */,
        resp => {
          settled = true
          core.debug(`execPodStep response: ${JSON.stringify(resp)}`)
          if (resp.status === 'Success') {
            resolve(0)
          } else {
            core.debug(
              JSON.stringify({
                message: resp?.message,
                details: resp?.details
              })
            )
            // Extract exit code from the Failure response. K8s returns exit
            // codes in details.causes with reason "ExitCode". If found,
            // resolve with the code (not reject) so callers can propagate it.
            // Only reject for true infrastructure errors (no exit code).
            const exitCode = extractExitCode(resp)
            if (exitCode !== null) {
              resolve(exitCode)
            } else {
              reject(new Error(resp?.message || 'execPodStep failed'))
            }
          }
        }
      )
      .then(ws => {
        // Handle WebSocket disconnect without a status callback.
        // This happens when the container is killed (e.g. cgroup OOM) —
        // the WebSocket drops before K8s can send a status response.
        if (ws) {
          ws.on('close', () => {
            if (!settled) {
              settled = true
              reject(
                new Error(
                  'execPodStep: WebSocket closed without status response — container may have been killed'
                )
              )
            }
          })
          ws.on('error', (err: Error) => {
            if (!settled) {
              settled = true
              reject(
                new Error(
                  `execPodStep: WebSocket error without status response: ${err.message}`
                )
              )
            }
          })
        }
      })
      .catch(e => {
        if (!settled) {
          settled = true
          reject(e)
        }
      })
  })
}

export async function execPodStepOutput(
  command: string[],
  podName: string,
  containerName: string
): Promise<{ exitCode: number; stdout: string }> {
  const exec = new k8s.Exec(kc)
  const stdoutBuffer = new WritableStreamBuffer()

  command = fixArgs(command)
  return new Promise(function (resolve, reject) {
    let settled = false
    exec
      .exec(
        namespace(),
        podName,
        containerName,
        command,
        stdoutBuffer,
        process.stderr,
        null,
        false /* tty */,
        resp => {
          settled = true
          const stdout = (stdoutBuffer.getContentsAsString('utf8') || '').trim()
          if (resp.status === 'Success') {
            resolve({ exitCode: 0, stdout })
          } else {
            const exitCode = extractExitCode(resp)
            if (exitCode !== null) {
              resolve({ exitCode, stdout })
            } else {
              reject(new Error(resp?.message || 'execPodStepOutput failed'))
            }
          }
        }
      )
      .then(ws => {
        if (ws) {
          ws.on('close', () => {
            if (!settled) {
              settled = true
              reject(
                new Error(
                  'execPodStepOutput: WebSocket closed without status response — container may have been killed'
                )
              )
            }
          })
          ws.on('error', (err: Error) => {
            if (!settled) {
              settled = true
              reject(
                new Error(
                  `execPodStepOutput: WebSocket error without status response: ${err.message}`
                )
              )
            }
          })
        }
      })
      .catch(e => {
        if (!settled) {
          settled = true
          reject(e)
        }
      })
  })
}

export async function execCalculateOutputHashSorted(
  podName: string,
  containerName: string,
  command: string[]
): Promise<string> {
  const exec = new k8s.Exec(kc)

  let output = ''
  const outputWriter = new stream.Writable({
    write(chunk, _enc, cb) {
      try {
        output += chunk.toString('utf8')
        cb()
      } catch (e) {
        cb(e as Error)
      }
    }
  })

  await new Promise<void>((resolve, reject) => {
    exec
      .exec(
        namespace(),
        podName,
        containerName,
        command,
        outputWriter, // capture stdout
        process.stderr,
        null,
        false /* tty */,
        resp => {
          core.debug(`internalExecOutput response: ${JSON.stringify(resp)}`)
          if (resp.status === 'Success') {
            resolve()
          } else {
            core.debug(
              JSON.stringify({
                message: resp?.message,
                details: resp?.details
              })
            )
            reject(new Error(resp?.message || 'internalExecOutput failed'))
          }
        }
      )
      .catch(e => reject(e))
  })

  outputWriter.end()

  // Sort lines for consistent ordering across platforms
  const sortedOutput =
    output
      .split('\n')
      .filter(line => line.length > 0)
      .sort()
      .join('\n') + '\n'

  const hash = createHash('sha256')
  hash.update(sortedOutput)
  return hash.digest('hex')
}

export async function localCalculateOutputHashSorted(
  commands: string[]
): Promise<string> {
  return await new Promise<string>((resolve, reject) => {
    const child = spawn(commands[0], commands.slice(1), {
      stdio: ['ignore', 'pipe', 'ignore']
    })

    let output = ''
    child.stdout.on('data', chunk => {
      output += chunk.toString('utf8')
    })
    child.on('error', reject)
    child.on('close', (code: number) => {
      if (code === 0) {
        // Sort lines for consistent ordering across distributions/platforms
        const sortedOutput =
          output
            .split('\n')
            .filter(line => line.length > 0)
            .sort()
            .join('\n') + '\n'

        const hash = createHash('sha256')
        hash.update(sortedOutput)
        resolve(hash.digest('hex'))
      } else {
        reject(new Error(`child process exited with code ${code}`))
      }
    })
  })
}

export async function execCpToPod(
  podName: string,
  runnerPath: string,
  containerPath: string
): Promise<void> {
  core.debug(`Copying ${runnerPath} to pod ${podName} at ${containerPath}`)

  let attempt = 0
  while (true) {
    try {
      const exec = new k8s.Exec(kc)
      // Use tar to extract with --no-same-owner to avoid ownership issues.
      // Then use find to fix permissions. The -m flag helps but we also need to fix permissions after.
      const command = [
        'sh',
        '-c',
        `tar xf - --no-same-owner -C ${shlex.quote(containerPath)} 2>/dev/null; ` +
          `find ${shlex.quote(containerPath)} -type f -exec chmod u+rw {} \\; 2>/dev/null; ` +
          `find ${shlex.quote(containerPath)} -type d -exec chmod u+rwx {} \\; 2>/dev/null`
      ]
      const EXEC_TIMEOUT_MS = 120_000
      const readStream = tar.pack(runnerPath)
      const errStream = new WritableStreamBuffer()
      let wsRef: WebSocket | null = null
      let execTimer: ReturnType<typeof setTimeout>
      try {
        await Promise.race([
          new Promise((resolve, reject) => {
            let settled = false
            exec
              .exec(
                namespace(),
                podName,
                JOB_CONTAINER_NAME,
                command,
                null,
                errStream,
                readStream,
                false,
                async status => {
                  if (settled) return
                  settled = true
                  if (errStream.size()) {
                    reject(
                      new Error(
                        `Error from execCpToPod - status: ${status.status}, details: \n ${errStream.getContentsAsString()}`
                      )
                    )
                  }
                  resolve(status)
                }
              )
              .then(ws => {
                wsRef = ws
                if (ws) {
                  ws.on('close', () => {
                    if (!settled) {
                      settled = true
                      reject(
                        new Error(
                          'execCpToPod: WebSocket closed without status response'
                        )
                      )
                    }
                  })
                  ws.on('error', (err: Error) => {
                    if (!settled) {
                      settled = true
                      reject(
                        new Error(
                          `execCpToPod: WebSocket error: ${err.message}`
                        )
                      )
                    }
                  })
                }
              })
              .catch(e => {
                if (!settled) {
                  settled = true
                  reject(e)
                }
              })
          }),
          new Promise((_, reject) => {
            execTimer = setTimeout(() => {
              reject(new Error('execCpToPod: exec timed out after 120s'))
            }, EXEC_TIMEOUT_MS)
          })
        ])
      } finally {
        clearTimeout(execTimer!)
        if (wsRef) {
          try {
            ;(wsRef as WebSocket).close()
          } catch {
            // already closed
          }
        }
        readStream.destroy()
      }
      break
    } catch (error) {
      attempt++
      const msg = `cpToPod: Attempt ${attempt}/30 failed: ${error instanceof Error ? error.message : String(error)}`
      if (attempt >= 3) {
        core.warning(msg)
      } else {
        core.debug(msg)
      }
      if (attempt >= 30) {
        throw new Error(
          `cpToPod failed after ${attempt} attempts: ${error instanceof Error ? error.message : String(error)}`
        )
      }
      await sleep(1000)
    }
  }

  // Workspace copy verification: disabled by default to avoid OOM in memory-constrained
  // runner pods (512Mi shared by .NET + Node.js). The verification runs `find -exec stat`
  // across all workspace files, accumulates the output as a JS string, and retries —
  // each retry re-runs the full scan on both sides. This is best-effort (silently
  // continues after exhausting retries) and is the biggest memory spike contributor.
  // WARNING: If re-enabling, ensure NODE_OPTIONS --max-old-space-size >= 256 or
  // V8 will crash (FATAL ERROR) instead of graceful OOM on large workspaces.
  if (COPY_VERIFY_ENABLED) {
    const delay = 1000
    for (let i = 0; i < COPY_VERIFY_RETRIES; i++) {
      try {
        const want = await localCalculateOutputHashSorted([
          'sh',
          '-c',
          listDirAllCommand(runnerPath)
        ])

        const got = await execCalculateOutputHashSorted(
          podName,
          JOB_CONTAINER_NAME,
          ['sh', '-c', listDirAllCommand(containerPath)]
        )

        if (got !== want) {
          core.debug(
            `The hash of the directory does not match the expected value; want='${want}' got='${got}'`
          )
          await sleep(delay)
          continue
        }

        break
      } catch (error) {
        core.debug(`Attempt ${i + 1} failed: ${error}`)
        await sleep(delay)
      }
    }
  }
}

export async function execCpFromPod(
  podName: string,
  containerPath: string,
  parentRunnerPath: string
): Promise<void> {
  const targetRunnerPath = `${parentRunnerPath}/${path.basename(containerPath)}`
  core.debug(
    `Copying from pod ${podName} ${containerPath} to ${targetRunnerPath}`
  )

  let attempt = 0
  while (true) {
    try {
      // make temporary directory
      const exec = new k8s.Exec(kc)
      const containerPaths = containerPath.split('/')
      const dirname = containerPaths.pop() as string
      const command = [
        'tar',
        'cf',
        '-',
        '-C',
        containerPaths.join('/') || '/',
        dirname
      ]
      const EXEC_TIMEOUT_MS = 120_000
      const writerStream = tar.extract(parentRunnerPath)
      const errStream = new WritableStreamBuffer()
      let wsRef: WebSocket | null = null
      let execTimer: ReturnType<typeof setTimeout>
      let execSucceeded = false
      try {
        await Promise.race([
          new Promise((resolve, reject) => {
            let settled = false
            exec
              .exec(
                namespace(),
                podName,
                JOB_CONTAINER_NAME,
                command,
                writerStream,
                errStream,
                null,
                false,
                async status => {
                  if (settled) return
                  settled = true
                  if (errStream.size()) {
                    reject(
                      new Error(
                        `Error from cpFromPod - details: \n ${errStream.getContentsAsString()}`
                      )
                    )
                  }
                  resolve(status)
                }
              )
              .then(ws => {
                wsRef = ws
                if (ws) {
                  ws.on('close', () => {
                    if (!settled) {
                      settled = true
                      reject(
                        new Error(
                          'execCpFromPod: WebSocket closed without status response'
                        )
                      )
                    }
                  })
                  ws.on('error', (err: Error) => {
                    if (!settled) {
                      settled = true
                      reject(
                        new Error(
                          `execCpFromPod: WebSocket error: ${err.message}`
                        )
                      )
                    }
                  })
                }
              })
              .catch(e => {
                if (!settled) {
                  settled = true
                  reject(e)
                }
              })
          }),
          new Promise((_, reject) => {
            execTimer = setTimeout(() => {
              reject(new Error('execCpFromPod: exec timed out after 120s'))
            }, EXEC_TIMEOUT_MS)
          })
        ])
        execSucceeded = true
      } finally {
        clearTimeout(execTimer!)
        if (!execSucceeded) {
          if (wsRef) {
            try {
              ;(wsRef as WebSocket).close()
            } catch {
              // already closed
            }
          }
          writerStream.destroy()
        }
      }

      // Wait for the tar extraction stream to finish writing all data
      // to disk. The K8s exec status callback fires when the remote
      // command exits, but stdout data may still be in transit through
      // the WebSocket pipeline. Without this, callers that immediately
      // access the extracted files can hit ENOENT race conditions.
      if (!writerStream.writableFinished) {
        await new Promise<void>((resolve, reject) => {
          let settled = false
          const done = (err?: Error | null): void => {
            if (settled) return
            settled = true
            cleanupFn()
            clearTimeout(timer)
            if (err) {
              reject(err)
            } else {
              resolve()
            }
          }
          const cleanupFn = stream.finished(writerStream, err => {
            done(err)
          })
          const timer = setTimeout(() => {
            writerStream.destroy()
            done(new Error('tar extract stream drain timed out after 90s'))
          }, 90000)
        })
      }

      if (wsRef) {
        try {
          ;(wsRef as WebSocket).close()
        } catch {
          // already closed
        }
      }

      break
    } catch (error) {
      attempt++
      const msg = `cpFromPod: Attempt ${attempt}/30 failed: ${error instanceof Error ? error.message : String(error)}`
      if (attempt >= 3) {
        core.warning(msg)
      } else {
        core.debug(msg)
      }
      if (attempt >= 30) {
        throw new Error(
          `execCpFromPod failed after ${attempt} attempts: ${error instanceof Error ? error.message : String(error)}`
        )
      }
      await sleep(1000)
    }
  }

  // Workspace copy verification: see comment in execCpToPod for rationale.
  if (COPY_VERIFY_ENABLED) {
    const delay = 1000
    for (let i = 0; i < COPY_VERIFY_RETRIES; i++) {
      try {
        const want = await execCalculateOutputHashSorted(
          podName,
          JOB_CONTAINER_NAME,
          ['sh', '-c', listDirAllCommand(containerPath)]
        )

        const got = await localCalculateOutputHashSorted([
          'sh',
          '-c',
          listDirAllCommand(targetRunnerPath)
        ])

        if (got !== want) {
          core.debug(
            `The hash of the directory does not match the expected value; want='${want}' got='${got}'`
          )
          await sleep(delay)
          continue
        }

        break
      } catch (error) {
        core.debug(`Attempt ${i + 1} failed: ${error}`)
        await sleep(delay)
      }
    }
  }
}

export async function waitForJobToComplete(jobName: string): Promise<void> {
  const backOffManager = new BackOffManager()
  while (true) {
    try {
      if (await isJobSucceeded(jobName)) {
        return
      }
    } catch (error) {
      throw new Error(
        `job ${jobName} has failed: ${error instanceof Error ? error.message : String(error)}`
      )
    }
    await backOffManager.backOff()
  }
}

export async function createDockerSecret(
  registry: Registry
): Promise<k8s.V1Secret> {
  const authContent = {
    auths: {
      [registry.serverUrl || 'https://index.docker.io/v1/']: {
        username: registry.username,
        password: registry.password,
        auth: Buffer.from(`${registry.username}:${registry.password}`).toString(
          'base64'
        )
      }
    }
  }

  const runnerInstanceLabel = new RunnerInstanceLabel()

  const secretName = getSecretName()
  const secret = new k8s.V1Secret()
  secret.immutable = true
  secret.apiVersion = 'v1'
  secret.metadata = new k8s.V1ObjectMeta()
  secret.metadata.name = secretName
  secret.metadata.namespace = namespace()
  secret.metadata.labels = {
    [runnerInstanceLabel.key]: runnerInstanceLabel.value
  }
  secret.type = 'kubernetes.io/dockerconfigjson'
  secret.kind = 'Secret'
  secret.data = {
    '.dockerconfigjson': Buffer.from(JSON.stringify(authContent)).toString(
      'base64'
    )
  }

  return await k8sApi.createNamespacedSecret({
    namespace: namespace(),
    body: secret
  })
}

export async function createSecretForEnvs(envs: {
  [key: string]: string
}): Promise<string> {
  const runnerInstanceLabel = new RunnerInstanceLabel()

  const secret = new k8s.V1Secret()
  const secretName = getSecretName()
  secret.immutable = true
  secret.apiVersion = 'v1'
  secret.metadata = new k8s.V1ObjectMeta()
  secret.metadata.name = secretName

  secret.metadata.labels = {
    [runnerInstanceLabel.key]: runnerInstanceLabel.value
  }
  secret.kind = 'Secret'
  secret.data = {}
  for (const [key, value] of Object.entries(envs)) {
    secret.data[key] = Buffer.from(value).toString('base64')
  }

  await k8sApi.createNamespacedSecret({
    namespace: namespace(),
    body: secret
  })
  return secretName
}

export async function deleteSecret(name: string): Promise<void> {
  await k8sApi.deleteNamespacedSecret({
    name,
    namespace: namespace()
  })
}

export async function pruneSecrets(): Promise<void> {
  const secretList = await k8sApi.listNamespacedSecret({
    namespace: namespace(),
    labelSelector: new RunnerInstanceLabel().toString()
  })
  if (!secretList.items.length) {
    return
  }

  await Promise.all(
    secretList.items.map(
      async secret =>
        secret.metadata?.name && (await deleteSecret(secret.metadata.name))
    )
  )
}

export async function waitForPodPhases(
  podName: string,
  awaitingPhases: Set<PodPhase>,
  backOffPhases: Set<PodPhase>,
  maxTimeSeconds = DEFAULT_WAIT_FOR_POD_TIME_SECONDS
): Promise<void> {
  const backOffManager = new BackOffManager(maxTimeSeconds)
  const startTime = Date.now()
  let phase: PodPhase = PodPhase.UNKNOWN
  let lastLogTime = 0
  try {
    while (true) {
      phase = await getPodPhase(podName)
      if (awaitingPhases.has(phase)) {
        return
      }

      if (!backOffPhases.has(phase)) {
        throw new Error(
          `Pod ${podName} is unhealthy with phase status ${phase}`
        )
      }

      const elapsed = Math.round((Date.now() - startTime) / 1000)
      if (Date.now() - lastLogTime >= 10_000) {
        lastLogTime = Date.now()
        try {
          const pod = await getPodByName(podName)
          const conditions = pod.status?.conditions
            ?.map(c => `${c.type}=${c.status}`)
            .join(', ')
          const containerWaiting = pod.status?.containerStatuses
            ?.filter(cs => cs.state?.waiting)
            ?.map(cs => `${cs.name}: ${cs.state!.waiting!.reason}`)
            ?.join('; ')
          core.info(
            `Pod ${podName}: phase=${phase}, conditions=[${conditions || 'none'}], waiting=[${containerWaiting || 'none'}] (${elapsed}s/${maxTimeSeconds}s)`
          )
        } catch {
          core.info(
            `Pod ${podName}: phase=${phase} (${elapsed}s/${maxTimeSeconds}s)`
          )
        }
      }

      await backOffManager.backOff()
    }
  } catch (error) {
    throw new Error(
      `Pod ${podName} is unhealthy with phase status ${phase}: ${error instanceof Error ? error.message : String(error)}`
    )
  }
}

export function getPrepareJobTimeoutSeconds(): number {
  const envTimeoutSeconds =
    process.env['ACTIONS_RUNNER_PREPARE_JOB_TIMEOUT_SECONDS']

  if (!envTimeoutSeconds) {
    return DEFAULT_WAIT_FOR_POD_TIME_SECONDS
  }

  const timeoutSeconds = parseInt(envTimeoutSeconds, 10)
  if (!timeoutSeconds || timeoutSeconds <= 0) {
    core.warning(
      `Prepare job timeout is invalid ("${timeoutSeconds}"): use an int > 0`
    )
    return DEFAULT_WAIT_FOR_POD_TIME_SECONDS
  }

  return timeoutSeconds
}

async function getPodPhase(name: string): Promise<PodPhase> {
  const podPhaseLookup = new Set<string>([
    PodPhase.PENDING,
    PodPhase.RUNNING,
    PodPhase.SUCCEEDED,
    PodPhase.FAILED,
    PodPhase.UNKNOWN
  ])
  const pod = await k8sApi.readNamespacedPod({
    name,
    namespace: namespace()
  })

  if (!pod.status?.phase || !podPhaseLookup.has(pod.status.phase)) {
    return PodPhase.UNKNOWN
  }
  return pod.status?.phase as PodPhase
}

async function isJobSucceeded(name: string): Promise<boolean> {
  const job = await k8sBatchV1Api.readNamespacedJob({
    name,
    namespace: namespace()
  })
  if (job.status?.failed) {
    throw new Error(`job ${name} has failed`)
  }
  return !!job.status?.succeeded
}

export async function getPodLogs(
  podName: string,
  containerName: string
): Promise<void> {
  const log = new k8s.Log(kc)
  const logStream = new stream.PassThrough()
  logStream.on('data', chunk => {
    // use write rather than console.log to prevent double line feed
    process.stdout.write(chunk)
  })

  logStream.on('error', err => {
    process.stderr.write(err.message)
  })

  await log.log(namespace(), podName, containerName, logStream, {
    follow: true,
    pretty: false,
    timestamps: false
  })
  await new Promise(resolve => logStream.on('end', () => resolve(null)))
}

export async function prunePods(): Promise<void> {
  const podList = await k8sApi.listNamespacedPod({
    namespace: namespace(),
    labelSelector: new RunnerInstanceLabel().toString()
  })
  if (!podList.items.length) {
    return
  }

  await Promise.all(
    podList.items.map(
      async pod => pod.metadata?.name && (await deletePod(pod.metadata.name))
    )
  )
}

export async function getPodStatus(
  name: string
): Promise<k8s.V1PodStatus | undefined> {
  const pod = await k8sApi.readNamespacedPod({
    name,
    namespace: namespace()
  })
  return pod.status
}

export async function isAuthPermissionsOK(): Promise<boolean> {
  const sar = new k8s.V1SelfSubjectAccessReview()
  const asyncs: Promise<k8s.V1SelfSubjectAccessReview>[] = []
  for (const resource of requiredPermissions) {
    for (const verb of resource.verbs) {
      sar.spec = new k8s.V1SelfSubjectAccessReviewSpec()
      sar.spec.resourceAttributes = new k8s.V1ResourceAttributes()
      sar.spec.resourceAttributes.verb = verb
      sar.spec.resourceAttributes.namespace = namespace()
      sar.spec.resourceAttributes.group = resource.group
      sar.spec.resourceAttributes.resource = resource.resource
      sar.spec.resourceAttributes.subresource = resource.subresource
      asyncs.push(
        k8sAuthorizationV1Api.createSelfSubjectAccessReview({ body: sar })
      )
    }
  }
  const responses = await Promise.all(asyncs)
  return responses.every(resp => resp.status?.allowed)
}

export async function isPodContainerAlpine(
  podName: string,
  containerName: string
): Promise<boolean> {
  const exitCode = await execPodStepWithRetry(
    [
      'sh',
      '-c',
      `[ $(cat /etc/*release* | grep -i -e "^ID=*alpine*" -c) != 0 ] || exit 1`
    ],
    podName,
    containerName,
    'detect alpine'
  )
  return exitCode === 0
}

export function namespace(): string {
  if (process.env['ACTIONS_RUNNER_KUBERNETES_NAMESPACE']) {
    return process.env['ACTIONS_RUNNER_KUBERNETES_NAMESPACE']
  }

  const context = kc.getContexts().find(ctx => ctx.namespace)
  if (!context?.namespace) {
    throw new Error(
      'Failed to determine namespace, falling back to `default`. Namespace should be set in context, or in env variable "ACTIONS_RUNNER_KUBERNETES_NAMESPACE"'
    )
  }
  return context.namespace
}

class BackOffManager {
  private backOffSeconds = 1
  totalTime = 0
  constructor(private throwAfterSeconds?: number) {
    if (!throwAfterSeconds || throwAfterSeconds < 0) {
      this.throwAfterSeconds = undefined
    }
  }

  async backOff(): Promise<void> {
    await new Promise(resolve =>
      setTimeout(resolve, this.backOffSeconds * 1000)
    )
    this.totalTime += this.backOffSeconds
    if (this.throwAfterSeconds && this.throwAfterSeconds < this.totalTime) {
      throw new Error('backoff timeout')
    }
    if (this.backOffSeconds < 20) {
      this.backOffSeconds *= 2
    }
    if (this.backOffSeconds > 20) {
      this.backOffSeconds = 20
    }
  }
}

export function containerPorts(
  container: ContainerInfo
): k8s.V1ContainerPort[] {
  const ports: k8s.V1ContainerPort[] = []
  if (!container.portMappings?.length) {
    return ports
  }
  for (const portDefinition of container.portMappings) {
    const portProtoSplit = portDefinition.split('/')
    if (portProtoSplit.length > 2) {
      throw new Error(`Unexpected port format: ${portDefinition}`)
    }

    const port = new k8s.V1ContainerPort()
    port.protocol =
      portProtoSplit.length === 2 ? portProtoSplit[1].toUpperCase() : 'TCP'

    const portSplit = portProtoSplit[0].split(':')
    if (portSplit.length > 2) {
      throw new Error('ports should have at most one ":" separator')
    }

    const parsePort = (p: string): number => {
      const num = Number(p)
      if (!Number.isInteger(num) || num < 1 || num > 65535) {
        throw new Error(`invalid container port: ${p}`)
      }
      return num
    }

    if (portSplit.length === 1) {
      port.containerPort = parsePort(portSplit[0])
    } else {
      port.hostPort = parsePort(portSplit[0])
      port.containerPort = parsePort(portSplit[1])
    }

    ports.push(port)
  }
  return ports
}

export async function getPodByName(name: string): Promise<k8s.V1Pod> {
  return await k8sApi.readNamespacedPod({
    name,
    namespace: namespace()
  })
}
