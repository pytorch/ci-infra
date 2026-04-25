// ---------------------------------------------------------------------------
// Module mocks — must be declared before importing the module under test
// ---------------------------------------------------------------------------

const MOCK_UUID = 'aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee'

const mockExecPodStep = jest.fn()
const mockExecPodStepOutput = jest.fn()
const mockExecPodStepOutputWithRetry = jest.fn()
const mockGetPodByName = jest.fn()

jest.mock('../src/k8s', () => ({
  execPodStep: (...args) => mockExecPodStep(...args),
  execPodStepOutput: (...args) => mockExecPodStepOutput(...args),
  execPodStepOutputWithRetry: (...args) =>
    mockExecPodStepOutputWithRetry(...args),
  getPodByName: (...args) => mockGetPodByName(...args)
}))

jest.mock('../src/k8s/rpc-server-script', () => ({
  RPC_SERVER_SCRIPT: 'fake-rpc-server-script'
}))

jest.mock('@actions/core', () => ({
  debug: jest.fn(),
  info: jest.fn(),
  warning: jest.fn(),
  error: jest.fn()
}))

// Mock sleep to be instant in tests
jest.mock('../src/k8s/utils', () => ({
  sleep: jest.fn().mockResolvedValue(undefined)
}))

// Mock crypto.randomUUID — the property is non-configurable so jest.spyOn
// cannot redefine it. Use a module-level mock with passthrough for other methods.
jest.mock('crypto', () => {
  const actual = jest.requireActual('crypto')
  return {
    ...actual,
    randomUUID: jest.fn().mockReturnValue(MOCK_UUID)
  }
})

import { deployRpcServer, rpcPodStep } from '../src/k8s/rpc'

// ---------------------------------------------------------------------------
// Test helpers
// ---------------------------------------------------------------------------

/** Build a minimal Response-like object that satisfies the fetch API. */
function fakeResponse(
  status: number,
  body?: unknown,
  opts?: { arrayBuffer?: ArrayBuffer }
): Response {
  const bodyStr = body !== undefined ? JSON.stringify(body) : ''
  return {
    ok: status >= 200 && status < 300,
    status,
    statusText: `${status}`,
    headers: new Headers({ 'content-type': 'application/json' }),
    text: jest.fn().mockResolvedValue(bodyStr),
    json: jest.fn().mockResolvedValue(body),
    arrayBuffer: jest
      .fn()
      .mockResolvedValue(
        opts?.arrayBuffer ?? new TextEncoder().encode(bodyStr).buffer
      ),
    body: null,
    bodyUsed: false,
    redirected: false,
    type: 'basic' as ResponseType,
    url: '',
    clone: jest.fn()
  } as unknown as Response
}

// ---------------------------------------------------------------------------
// Tests
// ---------------------------------------------------------------------------

describe('deployRpcServer', () => {
  const DISCOVERED_PORT = 45678

  type StepResult = { exitCode: number; stdout: string }

  /**
   * Route mocks by command content.
   * execPodStepOutputWithRetry: python3 check, script write, server start.
   * execPodStepOutput: port discovery, server log capture.
   * Pass an Error instance to make that step throw (simulating exhausted retries).
   */
  function setupDefaultMocks(overrides?: {
    python3?: StepResult | Error
    write?: StepResult | Error
    start?: StepResult | Error
    port?: StepResult
    log?: StepResult
  }): void {
    const defaults = {
      python3: { exitCode: 0, stdout: 'Python 3.10.0' } as StepResult | Error,
      write: { exitCode: 0, stdout: '' } as StepResult | Error,
      start: { exitCode: 0, stdout: '12345' } as StepResult | Error,
      port: { exitCode: 0, stdout: String(DISCOVERED_PORT) },
      log: { exitCode: 0, stdout: '(no log)' }
    }
    const cfg = { ...defaults, ...overrides }

    mockExecPodStepOutputWithRetry.mockImplementation(async (cmd: string[]) => {
      const cmdStr = cmd.join(' ')
      let result: StepResult | Error
      if (cmdStr.includes('python3') && cmdStr.includes('--version')) {
        result = cfg.python3
      } else if (
        cmdStr.includes('base64 -d') &&
        cmdStr.includes('rpc-server.py')
      ) {
        result = cfg.write
      } else if (
        cmdStr.includes('nohup python3') &&
        cmdStr.includes('rpc-server.py')
      ) {
        result = cfg.start
      } else {
        result = cfg.start
      }
      if (result instanceof Error) throw result
      return result
    })

    mockExecPodStepOutput.mockImplementation(async (cmd: string[]) => {
      const cmdStr = cmd.join(' ')
      if (cmdStr.includes('tail') && cmdStr.includes('rpc-server.log')) {
        return cfg.log
      }
      return cfg.port
    })
  }

  beforeEach(() => {
    jest.clearAllMocks()

    // Default: getPodByName returns a pod with an IP
    mockGetPodByName.mockResolvedValue({
      status: { podIP: '10.0.0.1' }
    })

    // Default: cleanup and health check succeed
    mockExecPodStep.mockResolvedValue(0)

    // Default: retried steps and port discovery routed by command
    setupDefaultMocks()
  })

  afterEach(() => {
    jest.restoreAllMocks()
  })

  it('should check python3 availability via execPodStepOutputWithRetry', async () => {
    await deployRpcServer('my-pod', 'my-container', 'tok-123')

    const pythonCall = mockExecPodStepOutputWithRetry.mock.calls.find(
      ([cmd]) => cmd.includes('python3') && cmd.includes('--version')
    )
    expect(pythonCall).toBeDefined()
    expect(pythonCall[1]).toBe('my-pod')
    expect(pythonCall[2]).toBe('my-container')
    expect(pythonCall[3]).toBe('check python3')
  })

  it('should throw if python3 is not available', async () => {
    setupDefaultMocks({
      python3: new Error(
        'non-zero exit code 127 (stdout: sh: python3: not found)'
      )
    })

    await expect(
      deployRpcServer('my-pod', 'my-container', 'tok-123')
    ).rejects.toThrow('image not compatible: python3 is a required dependency')
  })

  it('should throw if python3 check has infrastructure error', async () => {
    setupDefaultMocks({
      python3: new Error('WebSocket closed')
    })

    await expect(
      deployRpcServer('my-pod', 'my-container', 'tok-123')
    ).rejects.toThrow('image not compatible: python3 is a required dependency')
  })

  it('should throw if pod has no IP address', async () => {
    mockGetPodByName.mockResolvedValue({ status: {} })

    await expect(
      deployRpcServer('my-pod', 'my-container', 'tok-123')
    ).rejects.toThrow('Pod my-pod has no IP address')
  })

  it('should deploy the server script via base64 encoding', async () => {
    await deployRpcServer('my-pod', 'my-container', 'tok-123')

    const writeCall = mockExecPodStepOutputWithRetry.mock.calls.find(
      ([cmd]) =>
        cmd.join(' ').includes('base64 -d') &&
        cmd.join(' ').includes('rpc-server.py')
    )
    expect(writeCall).toBeDefined()
    expect(writeCall[0][0]).toBe('sh')
    expect(writeCall[0][1]).toBe('-c')
    expect(writeCall[0][2]).toContain('base64 -d')
    expect(writeCall[0][2]).toContain('/tmp/rpc-server.py')
    expect(writeCall[3]).toBe('write rpc server')
  })

  it('should throw if script write fails', async () => {
    setupDefaultMocks({
      write: new Error('non-zero exit code 1 (stdout: Permission denied)')
    })

    await expect(
      deployRpcServer('my-pod', 'my-container', 'tok-123')
    ).rejects.toThrow('RPC server script write failed')
  })

  it('should start the server with --port 0 and correct --token args', async () => {
    await deployRpcServer('my-pod', 'my-container', 'tok-123')

    const startCall = mockExecPodStepOutputWithRetry.mock.calls.find(([cmd]) =>
      cmd.join(' ').includes('nohup python3 /tmp/rpc-server.py')
    )
    expect(startCall).toBeDefined()
    expect(startCall[0][2]).toContain('--port 0')
    expect(startCall[0][2]).toContain('--token tok-123')
    expect(startCall[3]).toBe('start rpc server')
  })

  it('should continue to next attempt if server start fails', async () => {
    let startCallCount = 0
    mockExecPodStepOutputWithRetry.mockImplementation(async (cmd: string[]) => {
      const cmdStr = cmd.join(' ')
      if (cmdStr.includes('python3') && cmdStr.includes('--version')) {
        return { exitCode: 0, stdout: 'Python 3.10.0' }
      }
      if (cmdStr.includes('base64 -d')) {
        return { exitCode: 0, stdout: '' }
      }
      if (
        cmdStr.includes('nohup python3') &&
        cmdStr.includes('rpc-server.py')
      ) {
        startCallCount++
        if (startCallCount === 1) {
          throw new Error('start rpc server failed after 6 attempts')
        }
        return { exitCode: 0, stdout: '12345' }
      }
      return { exitCode: 0, stdout: '' }
    })

    const result = await deployRpcServer('my-pod', 'my-container', 'tok-123')
    expect(result.port).toBe(DISCOVERED_PORT)
    expect(startCallCount).toBe(2)
  })

  it('should poll health until server is ready and return podIp and port', async () => {
    // First health check fails, second succeeds
    mockExecPodStep
      .mockResolvedValueOnce(0) // cleanup
      .mockResolvedValueOnce(1) // health check fail
      .mockResolvedValueOnce(0) // health check pass

    const result = await deployRpcServer('my-pod', 'my-container', 'tok-123')

    expect(result).toEqual({ podIp: '10.0.0.1', port: DISCOVERED_PORT })
  })

  it('should return on first health check success', async () => {
    const result = await deployRpcServer('my-pod', 'my-container', 'tok-123')

    expect(result).toEqual({ podIp: '10.0.0.1', port: DISCOVERED_PORT })
  })

  it('should retry if port discovery fails on first attempt', async () => {
    const SECOND_PORT = 55555
    let portCallCount = 0

    const realDateNow = Date.now
    const startTime = realDateNow()
    let timeOffset = 0
    jest.spyOn(Date, 'now').mockImplementation(() => startTime + timeOffset)

    // Override port discovery via execPodStepOutput
    mockExecPodStepOutput.mockImplementation(async (cmd: string[]) => {
      const cmdStr = cmd.join(' ')
      if (cmdStr.includes('tail') && cmdStr.includes('rpc-server.log')) {
        return { exitCode: 0, stdout: '(no log)' }
      }
      // Port discovery
      portCallCount++
      if (portCallCount <= 1) {
        timeOffset += 6000
        return { exitCode: 1, stdout: '' }
      }
      return { exitCode: 0, stdout: String(SECOND_PORT) }
    })

    const result = await deployRpcServer('my-pod', 'my-container', 'tok-123')

    expect(result.podIp).toBe('10.0.0.1')
    expect(result.port).toBe(SECOND_PORT)
  })

  it('should throw after max deploy attempts exhausted with diagnostics', async () => {
    const realDateNow = Date.now
    const startTime = realDateNow()
    let timeOffset = 0

    jest.spyOn(Date, 'now').mockImplementation(() => startTime + timeOffset)

    mockExecPodStep.mockImplementation(async () => {
      // Advance time beyond health timeout on each call
      timeOffset += 31000
      return 1
    })

    await expect(
      deployRpcServer('my-pod', 'my-container', 'tok-123')
    ).rejects.toThrow('RPC server failed after 3 attempts')
  })

  it('should include per-attempt diagnostics in final error', async () => {
    const realDateNow = Date.now
    const startTime = realDateNow()
    let timeOffset = 0

    jest.spyOn(Date, 'now').mockImplementation(() => startTime + timeOffset)

    // Health checks always fail (timeout)
    mockExecPodStep.mockImplementation(async () => {
      timeOffset += 31000
      return 1
    })

    setupDefaultMocks({
      log: { exitCode: 0, stdout: 'Traceback: ImportError' }
    })

    try {
      await deployRpcServer('my-pod', 'my-container', 'tok-123')
      fail('should have thrown')
    } catch (err) {
      const msg = (err as Error).message
      expect(msg).toContain('attempt 1:')
      expect(msg).toContain('attempt 2:')
      expect(msg).toContain('attempt 3:')
      expect(msg).toContain('health check timed out')
    }
  })

  it('should handle health check network errors', async () => {
    const realDateNow = Date.now
    const startTime = realDateNow()
    let timeOffset = 0

    jest.spyOn(Date, 'now').mockImplementation(() => startTime + timeOffset)

    mockExecPodStep.mockImplementation(async (cmd: string[]) => {
      const cmdStr = cmd[2] || ''
      if (cmdStr.includes('pkill') || cmdStr.includes('rm -f')) {
        return 0
      }
      // Health check calls throw network error
      timeOffset += 31000
      throw new Error('exec failed')
    })

    await expect(
      deployRpcServer('my-pod', 'my-container', 'tok-123')
    ).rejects.toThrow('RPC server failed after 3 attempts')
  })

  it('should include details in python3 error message', async () => {
    setupDefaultMocks({
      python3: new Error(
        'non-zero exit code 127 (stdout: sh: python3: not found)'
      )
    })

    await expect(
      deployRpcServer('my-pod', 'my-container', 'tok-123')
    ).rejects.toThrow('sh: python3: not found')
  })

  it('should include details in script write error message', async () => {
    setupDefaultMocks({
      write: new Error('non-zero exit code 1 (stdout: Read-only file system)')
    })

    await expect(
      deployRpcServer('my-pod', 'my-container', 'tok-123')
    ).rejects.toThrow('Read-only file system')
  })
})

describe('rpcPodStep', () => {
  const POD_IP = '10.0.0.1'
  const PORT = 8080
  const SCRIPT_PATH = '/__w/_temp/run.sh'
  const TOKEN = 'test-token-abc'
  const POD_NAME = 'test-pod'
  const CONTAINER_NAME = 'job'

  let fetchMock: jest.Mock
  let stdoutWriteSpy: jest.SpyInstance
  let stderrWriteSpy: jest.SpyInstance

  beforeEach(() => {
    jest.clearAllMocks()
    fetchMock = jest.fn()
    global.fetch = fetchMock

    // Default: getPodByName returns a pod in Unknown state (for diagnostics)
    mockGetPodByName.mockResolvedValue({
      status: { phase: 'Running', podIP: POD_IP }
    })

    // Suppress actual stdout/stderr writes during tests
    stdoutWriteSpy = jest
      .spyOn(process.stdout, 'write')
      .mockImplementation(() => true)
    stderrWriteSpy = jest
      .spyOn(process.stderr, 'write')
      .mockImplementation(() => true)
  })

  afterEach(() => {
    jest.restoreAllMocks()
  })

  /**
   * Helper: set up fetchMock to handle the standard /exec -> /logs -> /status
   * flow in a single pass. Returns the exit code provided.
   */
  function setupSimpleExecFlow(exitCode: number, status = 'completed'): void {
    fetchMock.mockImplementation(async (url: string, init?: RequestInit) => {
      const urlStr = url.toString()

      // /exec POST
      if (urlStr.includes('/exec') && init?.method === 'POST') {
        return Promise.resolve(
          fakeResponse(200, { id: MOCK_UUID, status: 'running' })
        )
      }

      // /logs GET
      if (urlStr.includes('/logs')) {
        return Promise.resolve(
          fakeResponse(200, null, { arrayBuffer: new ArrayBuffer(0) })
        )
      }

      // /status GET
      if (urlStr.includes('/status')) {
        return Promise.resolve(
          fakeResponse(200, { id: MOCK_UUID, status, exit_code: exitCode })
        )
      }

      // /heartbeat POST
      if (urlStr.includes('/heartbeat')) {
        return Promise.resolve(fakeResponse(200, { status: 'ok' }))
      }

      return Promise.reject(new Error(`Unexpected fetch: ${urlStr}`))
    })
  }

  it('should send POST /exec with correct body and auth token', async () => {
    setupSimpleExecFlow(0)

    await rpcPodStep(POD_IP, PORT, SCRIPT_PATH, TOKEN, POD_NAME, CONTAINER_NAME)

    // Find the /exec call
    const execCall = fetchMock.mock.calls.find(
      ([url, opts]) => url.includes('/exec') && opts?.method === 'POST'
    )
    expect(execCall).toBeDefined()

    const [url, opts] = execCall
    expect(url).toBe(`http://${POD_IP}:${PORT}/exec`)
    expect(opts.headers['X-Auth-Token']).toBe(TOKEN)
    expect(opts.headers['Content-Type']).toBe('application/json')

    const body = JSON.parse(opts.body)
    expect(body.id).toBe(MOCK_UUID)
    expect(body.path).toBe(SCRIPT_PATH)
  })

  it('should poll /status until completed and return exit code 0', async () => {
    setupSimpleExecFlow(0, 'completed')

    const exitCode = await rpcPodStep(
      POD_IP,
      PORT,
      SCRIPT_PATH,
      TOKEN,
      POD_NAME,
      CONTAINER_NAME
    )

    expect(exitCode).toBe(0)
  })

  it('should return exit code on failure (non-zero)', async () => {
    setupSimpleExecFlow(1, 'failed')

    const exitCode = await rpcPodStep(
      POD_IP,
      PORT,
      SCRIPT_PATH,
      TOKEN,
      POD_NAME,
      CONTAINER_NAME
    )

    expect(exitCode).toBe(1)
  })

  it('should return exit code 137 for killed process', async () => {
    setupSimpleExecFlow(137, 'failed')

    const exitCode = await rpcPodStep(
      POD_IP,
      PORT,
      SCRIPT_PATH,
      TOKEN,
      POD_NAME,
      CONTAINER_NAME
    )

    expect(exitCode).toBe(137)
  })

  it('should return -1 when exit_code is null', async () => {
    fetchMock.mockImplementation(async (url: string, init?: RequestInit) => {
      const urlStr = url.toString()
      if (urlStr.includes('/exec') && init?.method === 'POST') {
        return Promise.resolve(fakeResponse(200, { status: 'running' }))
      }
      if (urlStr.includes('/logs')) {
        return Promise.resolve(
          fakeResponse(200, null, { arrayBuffer: new ArrayBuffer(0) })
        )
      }
      if (urlStr.includes('/status')) {
        return Promise.resolve(
          fakeResponse(200, {
            id: 'test',
            status: 'completed',
            exit_code: null
          })
        )
      }
      if (urlStr.includes('/heartbeat')) {
        return Promise.resolve(fakeResponse(200, { status: 'ok' }))
      }
      return Promise.reject(new Error(`Unexpected: ${urlStr}`))
    })

    const exitCode = await rpcPodStep(
      POD_IP,
      PORT,
      SCRIPT_PATH,
      TOKEN,
      POD_NAME,
      CONTAINER_NAME
    )

    expect(exitCode).toBe(-1)
  })

  it('should stream stdout via GET /logs?stream=stdout', async () => {
    const stdoutData = new TextEncoder().encode('hello stdout\n')
    let statusCallCount = 0

    fetchMock.mockImplementation(async (url: string, init?: RequestInit) => {
      const urlStr = url.toString()
      if (urlStr.includes('/exec') && init?.method === 'POST') {
        return Promise.resolve(fakeResponse(200, { status: 'running' }))
      }
      if (urlStr.includes('/logs') && urlStr.includes('stream=stdout')) {
        // Return data on first call, empty on subsequent
        if (statusCallCount === 0) {
          return Promise.resolve(
            fakeResponse(200, null, {
              arrayBuffer: stdoutData.buffer.slice(
                stdoutData.byteOffset,
                stdoutData.byteOffset + stdoutData.byteLength
              )
            })
          )
        }
        return Promise.resolve(
          fakeResponse(200, null, { arrayBuffer: new ArrayBuffer(0) })
        )
      }
      if (urlStr.includes('/logs') && urlStr.includes('stream=stderr')) {
        return Promise.resolve(
          fakeResponse(200, null, { arrayBuffer: new ArrayBuffer(0) })
        )
      }
      if (urlStr.includes('/status')) {
        statusCallCount++
        if (statusCallCount >= 2) {
          return Promise.resolve(
            fakeResponse(200, { status: 'completed', exit_code: 0 })
          )
        }
        return Promise.resolve(
          fakeResponse(200, { status: 'running', exit_code: null })
        )
      }
      if (urlStr.includes('/heartbeat')) {
        return Promise.resolve(fakeResponse(200, { status: 'ok' }))
      }
      return Promise.reject(new Error(`Unexpected: ${urlStr}`))
    })

    await rpcPodStep(POD_IP, PORT, SCRIPT_PATH, TOKEN, POD_NAME, CONTAINER_NAME)

    // Verify stdout was fetched with correct query params
    const stdoutCalls = fetchMock.mock.calls.filter(
      ([url]) =>
        url.includes('/logs') &&
        url.includes('stream=stdout') &&
        url.includes('offset=')
    )
    expect(stdoutCalls.length).toBeGreaterThanOrEqual(1)

    // Verify process.stdout.write was called with the log data
    expect(stdoutWriteSpy).toHaveBeenCalled()
  })

  it('should stream stderr via GET /logs?stream=stderr', async () => {
    const stderrData = new TextEncoder().encode('error output\n')
    let statusCallCount = 0

    fetchMock.mockImplementation(async (url: string, init?: RequestInit) => {
      const urlStr = url.toString()
      if (urlStr.includes('/exec') && init?.method === 'POST') {
        return Promise.resolve(fakeResponse(200, { status: 'running' }))
      }
      if (urlStr.includes('/logs') && urlStr.includes('stream=stdout')) {
        return Promise.resolve(
          fakeResponse(200, null, { arrayBuffer: new ArrayBuffer(0) })
        )
      }
      if (urlStr.includes('/logs') && urlStr.includes('stream=stderr')) {
        if (statusCallCount === 0) {
          return Promise.resolve(
            fakeResponse(200, null, {
              arrayBuffer: stderrData.buffer.slice(
                stderrData.byteOffset,
                stderrData.byteOffset + stderrData.byteLength
              )
            })
          )
        }
        return Promise.resolve(
          fakeResponse(200, null, { arrayBuffer: new ArrayBuffer(0) })
        )
      }
      if (urlStr.includes('/status')) {
        statusCallCount++
        if (statusCallCount >= 2) {
          return Promise.resolve(
            fakeResponse(200, { status: 'completed', exit_code: 0 })
          )
        }
        return Promise.resolve(
          fakeResponse(200, { status: 'running', exit_code: null })
        )
      }
      if (urlStr.includes('/heartbeat')) {
        return Promise.resolve(fakeResponse(200, { status: 'ok' }))
      }
      return Promise.reject(new Error(`Unexpected: ${urlStr}`))
    })

    await rpcPodStep(POD_IP, PORT, SCRIPT_PATH, TOKEN, POD_NAME, CONTAINER_NAME)

    const stderrCalls = fetchMock.mock.calls.filter(
      ([url]) => url.includes('/logs') && url.includes('stream=stderr')
    )
    expect(stderrCalls.length).toBeGreaterThanOrEqual(1)

    expect(stderrWriteSpy).toHaveBeenCalled()
  })

  it('should track offset correctly with large log responses', async () => {
    const chunk1 = new TextEncoder().encode('first chunk data\n')
    const chunk2 = new TextEncoder().encode('second chunk data\n')
    let stdoutCallIdx = 0
    let statusCallCount = 0

    fetchMock.mockImplementation(async (url: string, init?: RequestInit) => {
      const urlStr = url.toString()
      if (urlStr.includes('/exec') && init?.method === 'POST') {
        return Promise.resolve(fakeResponse(200, { status: 'running' }))
      }
      if (urlStr.includes('/logs') && urlStr.includes('stream=stdout')) {
        stdoutCallIdx++
        if (stdoutCallIdx === 1) {
          // First call: offset=0, return chunk1
          expect(urlStr).toContain('offset=0')
          return Promise.resolve(
            fakeResponse(200, null, {
              arrayBuffer: chunk1.buffer.slice(
                chunk1.byteOffset,
                chunk1.byteOffset + chunk1.byteLength
              )
            })
          )
        } else if (stdoutCallIdx === 2) {
          // Second call: offset should be chunk1.byteLength
          expect(urlStr).toContain(`offset=${chunk1.byteLength}`)
          return Promise.resolve(
            fakeResponse(200, null, {
              arrayBuffer: chunk2.buffer.slice(
                chunk2.byteOffset,
                chunk2.byteOffset + chunk2.byteLength
              )
            })
          )
        }
        return Promise.resolve(
          fakeResponse(200, null, { arrayBuffer: new ArrayBuffer(0) })
        )
      }
      if (urlStr.includes('/logs') && urlStr.includes('stream=stderr')) {
        return Promise.resolve(
          fakeResponse(200, null, { arrayBuffer: new ArrayBuffer(0) })
        )
      }
      if (urlStr.includes('/status')) {
        statusCallCount++
        if (statusCallCount >= 3) {
          return Promise.resolve(
            fakeResponse(200, { status: 'completed', exit_code: 0 })
          )
        }
        return Promise.resolve(
          fakeResponse(200, { status: 'running', exit_code: null })
        )
      }
      if (urlStr.includes('/heartbeat')) {
        return Promise.resolve(fakeResponse(200, { status: 'ok' }))
      }
      return Promise.reject(new Error(`Unexpected: ${urlStr}`))
    })

    const exitCode = await rpcPodStep(
      POD_IP,
      PORT,
      SCRIPT_PATH,
      TOKEN,
      POD_NAME,
      CONTAINER_NAME
    )

    expect(exitCode).toBe(0)
    // stdout was written at least twice (chunk1 and chunk2)
    expect(stdoutWriteSpy.mock.calls.length).toBeGreaterThanOrEqual(2)
  })

  it('should handle empty log responses', async () => {
    setupSimpleExecFlow(0)

    const exitCode = await rpcPodStep(
      POD_IP,
      PORT,
      SCRIPT_PATH,
      TOKEN,
      POD_NAME,
      CONTAINER_NAME
    )

    expect(exitCode).toBe(0)
  })

  it('should retry /exec on network error up to 3 times then throw', async () => {
    const netErr = new Error('ECONNREFUSED')
    fetchMock
      .mockRejectedValueOnce(netErr)
      .mockRejectedValueOnce(netErr)
      .mockRejectedValueOnce(netErr)

    await expect(
      rpcPodStep(POD_IP, PORT, SCRIPT_PATH, TOKEN, POD_NAME, CONTAINER_NAME)
    ).rejects.toThrow('RPC /exec network error after 3 attempts')
  })

  it('should succeed if /exec network error recovers on retry', async () => {
    const netErr = new Error('ECONNREFUSED')

    let execCallCount = 0
    fetchMock.mockImplementation(async (url: string, init?: RequestInit) => {
      const urlStr = url.toString()
      if (urlStr.includes('/exec') && init?.method === 'POST') {
        // Fail first, succeed second
        execCallCount++
        if (execCallCount === 1) {
          return Promise.reject(netErr)
        }
        return Promise.resolve(fakeResponse(200, { status: 'running' }))
      }
      if (urlStr.includes('/logs')) {
        return Promise.resolve(
          fakeResponse(200, null, { arrayBuffer: new ArrayBuffer(0) })
        )
      }
      if (urlStr.includes('/status')) {
        return Promise.resolve(
          fakeResponse(200, { status: 'completed', exit_code: 0 })
        )
      }
      if (urlStr.includes('/heartbeat')) {
        return Promise.resolve(fakeResponse(200, { status: 'ok' }))
      }
      return Promise.reject(new Error(`Unexpected: ${urlStr}`))
    })

    const exitCode = await rpcPodStep(
      POD_IP,
      PORT,
      SCRIPT_PATH,
      TOKEN,
      POD_NAME,
      CONTAINER_NAME
    )

    expect(exitCode).toBe(0)
  })

  it('should retry /exec on 5xx up to 3 times then throw', async () => {
    fetchMock
      .mockResolvedValueOnce(fakeResponse(500, 'internal error'))
      .mockResolvedValueOnce(fakeResponse(502, 'bad gateway'))
      .mockResolvedValueOnce(fakeResponse(503, 'service unavailable'))

    await expect(
      rpcPodStep(POD_IP, PORT, SCRIPT_PATH, TOKEN, POD_NAME, CONTAINER_NAME)
    ).rejects.toThrow('RPC /exec failed after 3 attempts')
  })

  it('should succeed if /exec 5xx recovers on retry', async () => {
    let execCallCount = 0
    fetchMock.mockImplementation(async (url: string, init?: RequestInit) => {
      const urlStr = url.toString()
      if (urlStr.includes('/exec') && init?.method === 'POST') {
        execCallCount++
        if (execCallCount === 1) {
          return Promise.resolve(fakeResponse(500, 'error'))
        }
        return Promise.resolve(fakeResponse(200, { status: 'running' }))
      }
      if (urlStr.includes('/logs')) {
        return Promise.resolve(
          fakeResponse(200, null, { arrayBuffer: new ArrayBuffer(0) })
        )
      }
      if (urlStr.includes('/status')) {
        return Promise.resolve(
          fakeResponse(200, { status: 'completed', exit_code: 0 })
        )
      }
      if (urlStr.includes('/heartbeat')) {
        return Promise.resolve(fakeResponse(200, { status: 'ok' }))
      }
      return Promise.reject(new Error(`Unexpected: ${urlStr}`))
    })

    const exitCode = await rpcPodStep(
      POD_IP,
      PORT,
      SCRIPT_PATH,
      TOKEN,
      POD_NAME,
      CONTAINER_NAME
    )

    expect(exitCode).toBe(0)
  })

  it('should throw immediately on /exec 4xx without retry', async () => {
    fetchMock.mockResolvedValueOnce(fakeResponse(400, 'bad request'))

    await expect(
      rpcPodStep(POD_IP, PORT, SCRIPT_PATH, TOKEN, POD_NAME, CONTAINER_NAME)
    ).rejects.toThrow('RPC /exec failed (400)')

    // Only one /exec call should have been made (no retries)
    const execCalls = fetchMock.mock.calls.filter(
      ([url, opts]) => url.includes('/exec') && opts?.method === 'POST'
    )
    expect(execCalls).toHaveLength(1)
  })

  it('should throw immediately on /exec 403', async () => {
    fetchMock.mockResolvedValueOnce(fakeResponse(403, 'forbidden'))

    await expect(
      rpcPodStep(POD_IP, PORT, SCRIPT_PATH, TOKEN, POD_NAME, CONTAINER_NAME)
    ).rejects.toThrow('RPC /exec failed (403)')
  })

  it('should throw immediately on /exec 409', async () => {
    fetchMock.mockResolvedValueOnce(
      fakeResponse(409, 'A job is already running')
    )

    await expect(
      rpcPodStep(POD_IP, PORT, SCRIPT_PATH, TOKEN, POD_NAME, CONTAINER_NAME)
    ).rejects.toThrow('RPC /exec failed (409)')
  })

  it('should send heartbeats during execution', async () => {
    // setInterval fires heartbeats every 3000ms in the source code.
    // We need the main polling loop to stay alive long enough for a heartbeat
    // to fire. We use fake timers and make sleep return a real timer-based
    // promise so that advanceTimersByTimeAsync progresses both setInterval
    // and sleep.
    jest.useFakeTimers()

    // eslint-disable-next-line @typescript-eslint/no-require-imports, @typescript-eslint/no-var-requires
    const { sleep: mockSleep } = require('../src/k8s/utils') as {
      sleep: jest.Mock
    }
    mockSleep.mockImplementation(
      async (ms: number) => new Promise(r => setTimeout(r, ms))
    )

    let statusCallCount = 0

    fetchMock.mockImplementation(async (url: string, init?: RequestInit) => {
      const urlStr = url.toString()
      if (urlStr.includes('/exec') && init?.method === 'POST') {
        return Promise.resolve(fakeResponse(200, { status: 'running' }))
      }
      if (urlStr.includes('/logs')) {
        return Promise.resolve(
          fakeResponse(200, null, { arrayBuffer: new ArrayBuffer(0) })
        )
      }
      if (urlStr.includes('/status')) {
        statusCallCount++
        // Keep running for several polls so heartbeat interval fires
        if (statusCallCount >= 20) {
          return Promise.resolve(
            fakeResponse(200, { status: 'completed', exit_code: 0 })
          )
        }
        return Promise.resolve(
          fakeResponse(200, { status: 'running', exit_code: null })
        )
      }
      if (urlStr.includes('/heartbeat') && init?.method === 'POST') {
        return Promise.resolve(fakeResponse(200, { status: 'ok' }))
      }
      return Promise.reject(new Error(`Unexpected: ${urlStr}`))
    })

    const promise = rpcPodStep(
      POD_IP,
      PORT,
      SCRIPT_PATH,
      TOKEN,
      POD_NAME,
      CONTAINER_NAME
    )

    // Advance fake clock in small steps to let both sleep and setInterval fire.
    // LOG_POLL_INTERVAL_MS = 200, HEARTBEAT_INTERVAL_MS = 3000
    // 20 polls * 200ms = 4000ms total, which is >3000 so heartbeat fires.
    for (let i = 0; i < 50; i++) {
      await jest.advanceTimersByTimeAsync(200)
    }

    const exitCode = await promise

    expect(exitCode).toBe(0)

    // Check that heartbeat calls were made
    const heartbeatCalls = fetchMock.mock.calls.filter(
      ([url, opts]) => url.includes('/heartbeat') && opts?.method === 'POST'
    )
    expect(heartbeatCalls.length).toBeGreaterThanOrEqual(1)

    // Verify heartbeat uses correct auth header
    expect(heartbeatCalls[0][1].headers['X-Auth-Token']).toBe(TOKEN)

    // Restore sleep mock to instant for other tests
    mockSleep.mockResolvedValue(undefined)
    jest.useRealTimers()
  })

  it('should clear heartbeat interval after completion', async () => {
    jest.useFakeTimers()

    setupSimpleExecFlow(0)

    const clearIntervalSpy = jest.spyOn(global, 'clearInterval')

    const promise = rpcPodStep(
      POD_IP,
      PORT,
      SCRIPT_PATH,
      TOKEN,
      POD_NAME,
      CONTAINER_NAME
    )
    await jest.advanceTimersByTimeAsync(500)
    await promise

    expect(clearIntervalSpy).toHaveBeenCalled()

    jest.useRealTimers()
  })

  it('should throw on heartbeat timeout with OOM diagnostic', async () => {
    const realDateNow = Date.now
    const startTime = realDateNow()
    let timeOffset = 0

    jest.spyOn(Date, 'now').mockImplementation(() => startTime + timeOffset)

    // Pod shows OOMKilled container
    mockGetPodByName.mockResolvedValue({
      status: {
        phase: 'Failed',
        podIP: POD_IP,
        containerStatuses: [
          {
            name: CONTAINER_NAME,
            state: {
              terminated: { reason: 'OOMKilled', exitCode: 137 }
            }
          }
        ]
      }
    })

    fetchMock.mockImplementation(async (url: string, init?: RequestInit) => {
      const urlStr = url.toString()
      if (urlStr.includes('/exec') && init?.method === 'POST') {
        return Promise.resolve(fakeResponse(200, { status: 'running' }))
      }
      if (urlStr.includes('/logs')) {
        return Promise.resolve(
          fakeResponse(200, null, { arrayBuffer: new ArrayBuffer(0) })
        )
      }
      if (urlStr.includes('/status')) {
        timeOffset += 61000
        return Promise.resolve(
          fakeResponse(200, { status: 'running', exit_code: null })
        )
      }
      if (urlStr.includes('/heartbeat')) {
        return Promise.reject(new Error('connection refused'))
      }
      return Promise.reject(new Error(`Unexpected: ${urlStr}`))
    })

    await expect(
      rpcPodStep(POD_IP, PORT, SCRIPT_PATH, TOKEN, POD_NAME, CONTAINER_NAME)
    ).rejects.toThrow('OOMKilled')
  })

  it('should throw on heartbeat timeout with eviction diagnostic', async () => {
    const realDateNow = Date.now
    const startTime = realDateNow()
    let timeOffset = 0

    jest.spyOn(Date, 'now').mockImplementation(() => startTime + timeOffset)

    mockGetPodByName.mockResolvedValue({
      status: {
        phase: 'Failed',
        reason: 'Evicted',
        message: 'The node was low on resource: memory.',
        podIP: POD_IP
      }
    })

    fetchMock.mockImplementation(async (url: string, init?: RequestInit) => {
      const urlStr = url.toString()
      if (urlStr.includes('/exec') && init?.method === 'POST') {
        return Promise.resolve(fakeResponse(200, { status: 'running' }))
      }
      if (urlStr.includes('/logs')) {
        return Promise.resolve(
          fakeResponse(200, null, { arrayBuffer: new ArrayBuffer(0) })
        )
      }
      if (urlStr.includes('/status')) {
        timeOffset += 61000
        return Promise.resolve(
          fakeResponse(200, { status: 'running', exit_code: null })
        )
      }
      if (urlStr.includes('/heartbeat')) {
        return Promise.reject(new Error('connection refused'))
      }
      return Promise.reject(new Error(`Unexpected: ${urlStr}`))
    })

    await expect(
      rpcPodStep(POD_IP, PORT, SCRIPT_PATH, TOKEN, POD_NAME, CONTAINER_NAME)
    ).rejects.toThrow('Pod was evicted')
  })

  it('should throw on heartbeat timeout with node-failure diagnostic', async () => {
    const realDateNow = Date.now
    const startTime = realDateNow()
    let timeOffset = 0

    jest.spyOn(Date, 'now').mockImplementation(() => startTime + timeOffset)

    mockGetPodByName.mockResolvedValue({
      status: {
        phase: 'Unknown',
        message: 'Node lost contact',
        podIP: POD_IP
      }
    })

    fetchMock.mockImplementation(async (url: string, init?: RequestInit) => {
      const urlStr = url.toString()
      if (urlStr.includes('/exec') && init?.method === 'POST') {
        return Promise.resolve(fakeResponse(200, { status: 'running' }))
      }
      if (urlStr.includes('/logs')) {
        return Promise.resolve(
          fakeResponse(200, null, { arrayBuffer: new ArrayBuffer(0) })
        )
      }
      if (urlStr.includes('/status')) {
        timeOffset += 61000
        return Promise.resolve(
          fakeResponse(200, { status: 'running', exit_code: null })
        )
      }
      if (urlStr.includes('/heartbeat')) {
        return Promise.reject(new Error('connection refused'))
      }
      return Promise.reject(new Error(`Unexpected: ${urlStr}`))
    })

    await expect(
      rpcPodStep(POD_IP, PORT, SCRIPT_PATH, TOKEN, POD_NAME, CONTAINER_NAME)
    ).rejects.toThrow('possible node failure')
  })

  it('should fall back to generic message when diagnostics fail', async () => {
    const realDateNow = Date.now
    const startTime = realDateNow()
    let timeOffset = 0

    jest.spyOn(Date, 'now').mockImplementation(() => startTime + timeOffset)

    // getPodByName throws — K8s API unreachable
    mockGetPodByName.mockRejectedValue(new Error('API server unreachable'))

    fetchMock.mockImplementation(async (url: string, init?: RequestInit) => {
      const urlStr = url.toString()
      if (urlStr.includes('/exec') && init?.method === 'POST') {
        return Promise.resolve(fakeResponse(200, { status: 'running' }))
      }
      if (urlStr.includes('/logs')) {
        return Promise.resolve(
          fakeResponse(200, null, { arrayBuffer: new ArrayBuffer(0) })
        )
      }
      if (urlStr.includes('/status')) {
        timeOffset += 61000
        return Promise.resolve(
          fakeResponse(200, { status: 'running', exit_code: null })
        )
      }
      if (urlStr.includes('/heartbeat')) {
        return Promise.reject(new Error('connection refused'))
      }
      return Promise.reject(new Error(`Unexpected: ${urlStr}`))
    })

    await expect(
      rpcPodStep(POD_IP, PORT, SCRIPT_PATH, TOKEN, POD_NAME, CONTAINER_NAME)
    ).rejects.toThrow('RPC heartbeat failed for 60s')
  })

  it('should detect RPC process death when container is still running', async () => {
    const realDateNow = Date.now
    const startTime = realDateNow()
    let timeOffset = 0

    jest.spyOn(Date, 'now').mockImplementation(() => startTime + timeOffset)

    // Pod is still Running but RPC server died
    mockGetPodByName.mockResolvedValue({
      status: {
        phase: 'Running',
        podIP: POD_IP,
        containerStatuses: [
          {
            name: CONTAINER_NAME,
            state: { running: { startedAt: '2025-01-01T00:00:00Z' } }
          }
        ]
      }
    })

    // execPodStepOutput returns the server log
    mockExecPodStepOutput.mockResolvedValue({
      exitCode: 0,
      stdout: 'Killed'
    })

    fetchMock.mockImplementation(async (url: string, init?: RequestInit) => {
      const urlStr = url.toString()
      if (urlStr.includes('/exec') && init?.method === 'POST') {
        return Promise.resolve(fakeResponse(200, { status: 'running' }))
      }
      if (urlStr.includes('/logs')) {
        return Promise.resolve(
          fakeResponse(200, null, { arrayBuffer: new ArrayBuffer(0) })
        )
      }
      if (urlStr.includes('/status')) {
        timeOffset += 61000
        return Promise.resolve(
          fakeResponse(200, { status: 'running', exit_code: null })
        )
      }
      if (urlStr.includes('/heartbeat')) {
        return Promise.reject(new Error('connection refused'))
      }
      return Promise.reject(new Error(`Unexpected: ${urlStr}`))
    })

    await expect(
      rpcPodStep(POD_IP, PORT, SCRIPT_PATH, TOKEN, POD_NAME, CONTAINER_NAME)
    ).rejects.toThrow('RPC server process died')
  })

  it('should handle /status fetch failures gracefully (retry)', async () => {
    let statusCallCount = 0

    fetchMock.mockImplementation(async (url: string, init?: RequestInit) => {
      const urlStr = url.toString()
      if (urlStr.includes('/exec') && init?.method === 'POST') {
        return Promise.resolve(fakeResponse(200, { status: 'running' }))
      }
      if (urlStr.includes('/logs')) {
        return Promise.resolve(
          fakeResponse(200, null, { arrayBuffer: new ArrayBuffer(0) })
        )
      }
      if (urlStr.includes('/status')) {
        statusCallCount++
        if (statusCallCount === 1) {
          // First status call fails
          return Promise.reject(new Error('timeout'))
        }
        // Second call succeeds with completion
        return Promise.resolve(
          fakeResponse(200, { status: 'completed', exit_code: 0 })
        )
      }
      if (urlStr.includes('/heartbeat')) {
        return Promise.resolve(fakeResponse(200, { status: 'ok' }))
      }
      return Promise.reject(new Error(`Unexpected: ${urlStr}`))
    })

    const exitCode = await rpcPodStep(
      POD_IP,
      PORT,
      SCRIPT_PATH,
      TOKEN,
      POD_NAME,
      CONTAINER_NAME
    )

    expect(exitCode).toBe(0)
    expect(statusCallCount).toBe(2)
  })

  it('should handle /logs fetch failures gracefully (continue polling)', async () => {
    let logsCallCount = 0

    fetchMock.mockImplementation(async (url: string, init?: RequestInit) => {
      const urlStr = url.toString()
      if (urlStr.includes('/exec') && init?.method === 'POST') {
        return Promise.resolve(fakeResponse(200, { status: 'running' }))
      }
      if (urlStr.includes('/logs')) {
        logsCallCount++
        if (logsCallCount <= 2) {
          // First two log fetches fail
          return Promise.reject(new Error('network error'))
        }
        return Promise.resolve(
          fakeResponse(200, null, { arrayBuffer: new ArrayBuffer(0) })
        )
      }
      if (urlStr.includes('/status')) {
        if (logsCallCount >= 3) {
          return Promise.resolve(
            fakeResponse(200, { status: 'completed', exit_code: 0 })
          )
        }
        return Promise.resolve(
          fakeResponse(200, { status: 'running', exit_code: null })
        )
      }
      if (urlStr.includes('/heartbeat')) {
        return Promise.resolve(fakeResponse(200, { status: 'ok' }))
      }
      return Promise.reject(new Error(`Unexpected: ${urlStr}`))
    })

    // Should complete despite log fetch failures
    const exitCode = await rpcPodStep(
      POD_IP,
      PORT,
      SCRIPT_PATH,
      TOKEN,
      POD_NAME,
      CONTAINER_NAME
    )

    expect(exitCode).toBe(0)
  })

  it('should do a final log flush after status becomes completed', async () => {
    const finalStdout = new TextEncoder().encode('final output\n')
    let statusCallCount = 0
    let postCompletionLogFetch = false

    fetchMock.mockImplementation(async (url: string, init?: RequestInit) => {
      const urlStr = url.toString()
      if (urlStr.includes('/exec') && init?.method === 'POST') {
        return Promise.resolve(fakeResponse(200, { status: 'running' }))
      }
      if (urlStr.includes('/logs') && urlStr.includes('stream=stdout')) {
        if (statusCallCount >= 1 && !postCompletionLogFetch) {
          // This is the final flush — return remaining data
          postCompletionLogFetch = true
          return Promise.resolve(
            fakeResponse(200, null, {
              arrayBuffer: finalStdout.buffer.slice(
                finalStdout.byteOffset,
                finalStdout.byteOffset + finalStdout.byteLength
              )
            })
          )
        }
        return Promise.resolve(
          fakeResponse(200, null, { arrayBuffer: new ArrayBuffer(0) })
        )
      }
      if (urlStr.includes('/logs') && urlStr.includes('stream=stderr')) {
        return Promise.resolve(
          fakeResponse(200, null, { arrayBuffer: new ArrayBuffer(0) })
        )
      }
      if (urlStr.includes('/status')) {
        statusCallCount++
        return Promise.resolve(
          fakeResponse(200, { status: 'completed', exit_code: 0 })
        )
      }
      if (urlStr.includes('/heartbeat')) {
        return Promise.resolve(fakeResponse(200, { status: 'ok' }))
      }
      return Promise.reject(new Error(`Unexpected: ${urlStr}`))
    })

    const exitCode = await rpcPodStep(
      POD_IP,
      PORT,
      SCRIPT_PATH,
      TOKEN,
      POD_NAME,
      CONTAINER_NAME
    )

    expect(exitCode).toBe(0)

    // After status shows completed, there should be log fetches for final flush
    const allLogCalls = fetchMock.mock.calls.filter(([url]) =>
      url.includes('/logs')
    )
    // At minimum: during-poll logs + final flush = 4 calls (2 streams x 2 phases)
    expect(allLogCalls.length).toBeGreaterThanOrEqual(4)
  })

  it('should handle non-200 /logs response (return empty)', async () => {
    fetchMock.mockImplementation(async (url: string, init?: RequestInit) => {
      const urlStr = url.toString()
      if (urlStr.includes('/exec') && init?.method === 'POST') {
        return Promise.resolve(fakeResponse(200, { status: 'running' }))
      }
      if (urlStr.includes('/logs')) {
        // Server returns 404 for logs
        return Promise.resolve(
          fakeResponse(404, null, { arrayBuffer: new ArrayBuffer(0) })
        )
      }
      if (urlStr.includes('/status')) {
        return Promise.resolve(
          fakeResponse(200, { status: 'completed', exit_code: 0 })
        )
      }
      if (urlStr.includes('/heartbeat')) {
        return Promise.resolve(fakeResponse(200, { status: 'ok' }))
      }
      return Promise.reject(new Error(`Unexpected: ${urlStr}`))
    })

    const exitCode = await rpcPodStep(
      POD_IP,
      PORT,
      SCRIPT_PATH,
      TOKEN,
      POD_NAME,
      CONTAINER_NAME
    )

    expect(exitCode).toBe(0)
  })

  it('should pass abort signal with timeout to fetch calls', async () => {
    setupSimpleExecFlow(0)

    await rpcPodStep(POD_IP, PORT, SCRIPT_PATH, TOKEN, POD_NAME, CONTAINER_NAME)

    // Verify all fetch calls include a signal
    for (const [, opts] of fetchMock.mock.calls) {
      if (opts) {
        expect(opts.signal).toBeDefined()
      }
    }
  })

  it('should include exec error body text in thrown error message', async () => {
    fetchMock.mockResolvedValueOnce(
      fakeResponse(422, 'Unprocessable: missing required field "id"')
    )

    await expect(
      rpcPodStep(POD_IP, PORT, SCRIPT_PATH, TOKEN, POD_NAME, CONTAINER_NAME)
    ).rejects.toThrow('RPC /exec failed (422)')
  })

  it('should handle /exec response where text() throws', async () => {
    const badResp = {
      ok: false,
      status: 400,
      statusText: '400',
      headers: new Headers(),
      text: jest.fn().mockRejectedValue(new Error('stream consumed')),
      json: jest.fn().mockRejectedValue(new Error('stream consumed')),
      arrayBuffer: jest.fn(),
      body: null,
      bodyUsed: true,
      redirected: false,
      type: 'basic' as ResponseType,
      url: '',
      clone: jest.fn()
    } as unknown as Response

    fetchMock.mockResolvedValueOnce(badResp)

    await expect(
      rpcPodStep(POD_IP, PORT, SCRIPT_PATH, TOKEN, POD_NAME, CONTAINER_NAME)
    ).rejects.toThrow('RPC /exec failed (400)')
  })

  // -------------------------------------------------------------------
  // Scenario tests — long-running, cancellation, OOM, sudden pod death
  // -------------------------------------------------------------------

  it('should stream stdout and stderr over many poll cycles (long-running script)', async () => {
    // Simulates a process running for many poll iterations, producing
    // new stdout/stderr chunks periodically, with heartbeats succeeding.
    const totalPolls = 30
    let statusCallCount = 0
    let stdoutCallCount = 0
    let stderrCallCount = 0

    fetchMock.mockImplementation(async (url: string, init?: RequestInit) => {
      const urlStr = url.toString()
      if (urlStr.includes('/exec') && init?.method === 'POST') {
        return Promise.resolve(fakeResponse(200, { status: 'running' }))
      }
      if (urlStr.includes('/logs') && urlStr.includes('stream=stdout')) {
        stdoutCallCount++
        // Produce a chunk every 5 polls
        if (stdoutCallCount % 5 === 0 && statusCallCount < totalPolls) {
          const chunk = new TextEncoder().encode(
            `stdout line ${stdoutCallCount}\n`
          )
          return Promise.resolve(
            fakeResponse(200, null, {
              arrayBuffer: chunk.buffer.slice(
                chunk.byteOffset,
                chunk.byteOffset + chunk.byteLength
              )
            })
          )
        }
        return Promise.resolve(
          fakeResponse(200, null, { arrayBuffer: new ArrayBuffer(0) })
        )
      }
      if (urlStr.includes('/logs') && urlStr.includes('stream=stderr')) {
        stderrCallCount++
        // Produce stderr every 7 polls
        if (stderrCallCount % 7 === 0 && statusCallCount < totalPolls) {
          const chunk = new TextEncoder().encode(
            `stderr warning ${stderrCallCount}\n`
          )
          return Promise.resolve(
            fakeResponse(200, null, {
              arrayBuffer: chunk.buffer.slice(
                chunk.byteOffset,
                chunk.byteOffset + chunk.byteLength
              )
            })
          )
        }
        return Promise.resolve(
          fakeResponse(200, null, { arrayBuffer: new ArrayBuffer(0) })
        )
      }
      if (urlStr.includes('/status')) {
        statusCallCount++
        if (statusCallCount >= totalPolls) {
          return Promise.resolve(
            fakeResponse(200, { status: 'completed', exit_code: 0 })
          )
        }
        return Promise.resolve(
          fakeResponse(200, { status: 'running', exit_code: null })
        )
      }
      if (urlStr.includes('/heartbeat')) {
        return Promise.resolve(fakeResponse(200, { status: 'ok' }))
      }
      return Promise.reject(new Error(`Unexpected: ${urlStr}`))
    })

    const exitCode = await rpcPodStep(
      POD_IP,
      PORT,
      SCRIPT_PATH,
      TOKEN,
      POD_NAME,
      CONTAINER_NAME
    )

    expect(exitCode).toBe(0)
    // Verify multiple stdout and stderr writes occurred
    expect(stdoutWriteSpy.mock.calls.length).toBeGreaterThanOrEqual(5)
    expect(stderrWriteSpy.mock.calls.length).toBeGreaterThanOrEqual(3)
  })

  it('should handle process killed mid-execution (cancelled job)', async () => {
    // Process runs for several polls producing logs, then is killed externally.
    // Status changes from running to failed with exit code 143 (SIGTERM).
    let statusCallCount = 0
    const killAfterPolls = 5

    fetchMock.mockImplementation(async (url: string, init?: RequestInit) => {
      const urlStr = url.toString()
      if (urlStr.includes('/exec') && init?.method === 'POST') {
        return Promise.resolve(fakeResponse(200, { status: 'running' }))
      }
      if (urlStr.includes('/logs') && urlStr.includes('stream=stdout')) {
        if (statusCallCount < killAfterPolls) {
          const chunk = new TextEncoder().encode('running...\n')
          return Promise.resolve(
            fakeResponse(200, null, {
              arrayBuffer: chunk.buffer.slice(
                chunk.byteOffset,
                chunk.byteOffset + chunk.byteLength
              )
            })
          )
        }
        return Promise.resolve(
          fakeResponse(200, null, { arrayBuffer: new ArrayBuffer(0) })
        )
      }
      if (urlStr.includes('/logs') && urlStr.includes('stream=stderr')) {
        return Promise.resolve(
          fakeResponse(200, null, { arrayBuffer: new ArrayBuffer(0) })
        )
      }
      if (urlStr.includes('/status')) {
        statusCallCount++
        if (statusCallCount > killAfterPolls) {
          // Process was killed — SIGTERM = exit code 143 (128 + 15)
          return Promise.resolve(
            fakeResponse(200, { status: 'failed', exit_code: 143 })
          )
        }
        return Promise.resolve(
          fakeResponse(200, { status: 'running', exit_code: null })
        )
      }
      if (urlStr.includes('/heartbeat')) {
        return Promise.resolve(fakeResponse(200, { status: 'ok' }))
      }
      return Promise.reject(new Error(`Unexpected: ${urlStr}`))
    })

    const exitCode = await rpcPodStep(
      POD_IP,
      PORT,
      SCRIPT_PATH,
      TOKEN,
      POD_NAME,
      CONTAINER_NAME
    )

    expect(exitCode).toBe(143)
    // Some stdout was produced before the kill
    expect(stdoutWriteSpy).toHaveBeenCalled()
  })

  it('should handle OOM kill on the script (exit code 137 with log streaming)', async () => {
    // Script runs, producing logs, then is OOM killed. The RPC server
    // catches the exit and reports status: failed, exit_code: 137.
    let statusCallCount = 0
    const oomAfterPolls = 8

    fetchMock.mockImplementation(async (url: string, init?: RequestInit) => {
      const urlStr = url.toString()
      if (urlStr.includes('/exec') && init?.method === 'POST') {
        return Promise.resolve(fakeResponse(200, { status: 'running' }))
      }
      if (urlStr.includes('/logs') && urlStr.includes('stream=stdout')) {
        if (statusCallCount <= oomAfterPolls) {
          const chunk = new TextEncoder().encode('allocating memory...\n')
          return Promise.resolve(
            fakeResponse(200, null, {
              arrayBuffer: chunk.buffer.slice(
                chunk.byteOffset,
                chunk.byteOffset + chunk.byteLength
              )
            })
          )
        }
        // After OOM, no more output
        return Promise.resolve(
          fakeResponse(200, null, { arrayBuffer: new ArrayBuffer(0) })
        )
      }
      if (urlStr.includes('/logs') && urlStr.includes('stream=stderr')) {
        return Promise.resolve(
          fakeResponse(200, null, { arrayBuffer: new ArrayBuffer(0) })
        )
      }
      if (urlStr.includes('/status')) {
        statusCallCount++
        if (statusCallCount > oomAfterPolls) {
          // SIGKILL from OOM killer: 128 + 9 = 137
          return Promise.resolve(
            fakeResponse(200, { status: 'failed', exit_code: 137 })
          )
        }
        return Promise.resolve(
          fakeResponse(200, { status: 'running', exit_code: null })
        )
      }
      if (urlStr.includes('/heartbeat')) {
        return Promise.resolve(fakeResponse(200, { status: 'ok' }))
      }
      return Promise.reject(new Error(`Unexpected: ${urlStr}`))
    })

    const exitCode = await rpcPodStep(
      POD_IP,
      PORT,
      SCRIPT_PATH,
      TOKEN,
      POD_NAME,
      CONTAINER_NAME
    )

    expect(exitCode).toBe(137)
    expect(stdoutWriteSpy).toHaveBeenCalled()
  })

  it('should handle OOM kill on workflow pod (all fetches fail, then diagnostic)', async () => {
    // Pod OOM: after some successful polls, ALL fetches start failing.
    // Heartbeat grace period elapses, then diagnostic reveals OOMKilled.
    const realDateNow = Date.now
    const startTime = realDateNow()
    let timeOffset = 0
    let statusCallCount = 0
    const oomAfterPolls = 3

    jest.spyOn(Date, 'now').mockImplementation(() => startTime + timeOffset)

    mockGetPodByName.mockResolvedValue({
      status: {
        phase: 'Failed',
        podIP: POD_IP,
        containerStatuses: [
          {
            name: CONTAINER_NAME,
            state: {
              terminated: { reason: 'OOMKilled', exitCode: 137 }
            }
          }
        ]
      }
    })

    fetchMock.mockImplementation(async (url: string, init?: RequestInit) => {
      const urlStr = url.toString()
      if (urlStr.includes('/exec') && init?.method === 'POST') {
        return Promise.resolve(fakeResponse(200, { status: 'running' }))
      }

      // After OOM, all fetches fail (server is gone)
      if (statusCallCount >= oomAfterPolls) {
        if (urlStr.includes('/status')) {
          // Advance time to trigger heartbeat grace expiry
          timeOffset += 25000
          return Promise.reject(new Error('ECONNREFUSED'))
        }
        if (urlStr.includes('/heartbeat')) {
          return Promise.reject(new Error('ECONNREFUSED'))
        }
        if (urlStr.includes('/logs')) {
          return Promise.reject(new Error('ECONNREFUSED'))
        }
      }

      if (urlStr.includes('/logs')) {
        return Promise.resolve(
          fakeResponse(200, null, { arrayBuffer: new ArrayBuffer(0) })
        )
      }
      if (urlStr.includes('/status')) {
        statusCallCount++
        return Promise.resolve(
          fakeResponse(200, { status: 'running', exit_code: null })
        )
      }
      if (urlStr.includes('/heartbeat')) {
        return Promise.resolve(fakeResponse(200, { status: 'ok' }))
      }
      return Promise.reject(new Error(`Unexpected: ${urlStr}`))
    })

    await expect(
      rpcPodStep(POD_IP, PORT, SCRIPT_PATH, TOKEN, POD_NAME, CONTAINER_NAME)
    ).rejects.toThrow('OOMKilled')
  })

  it('should handle workflow pod sudden death with generic termination', async () => {
    // Pod dies for a non-OOM reason (e.g., preemption, crash).
    // All fetches fail, heartbeat times out, diagnostic shows terminated
    // with reason: Error.
    const realDateNow = Date.now
    const startTime = realDateNow()
    let timeOffset = 0

    jest.spyOn(Date, 'now').mockImplementation(() => startTime + timeOffset)

    mockGetPodByName.mockResolvedValue({
      status: {
        phase: 'Failed',
        podIP: POD_IP,
        containerStatuses: [
          {
            name: CONTAINER_NAME,
            state: {
              terminated: {
                reason: 'Error',
                exitCode: 1,
                message: 'container exited unexpectedly'
              }
            }
          }
        ]
      }
    })

    fetchMock.mockImplementation(async (url: string, init?: RequestInit) => {
      const urlStr = url.toString()
      if (urlStr.includes('/exec') && init?.method === 'POST') {
        return Promise.resolve(fakeResponse(200, { status: 'running' }))
      }
      if (urlStr.includes('/logs')) {
        return Promise.resolve(
          fakeResponse(200, null, { arrayBuffer: new ArrayBuffer(0) })
        )
      }
      if (urlStr.includes('/status')) {
        timeOffset += 61000
        return Promise.resolve(
          fakeResponse(200, { status: 'running', exit_code: null })
        )
      }
      if (urlStr.includes('/heartbeat')) {
        return Promise.reject(new Error('connection refused'))
      }
      return Promise.reject(new Error(`Unexpected: ${urlStr}`))
    })

    await expect(
      rpcPodStep(POD_IP, PORT, SCRIPT_PATH, TOKEN, POD_NAME, CONTAINER_NAME)
    ).rejects.toThrow('container exited unexpectedly')
  })
})
