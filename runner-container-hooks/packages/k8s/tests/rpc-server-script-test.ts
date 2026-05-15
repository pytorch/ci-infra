// Tests for the embedded Python RPC server script — specifically the new
// /kill endpoint introduced for cancellation forwarding.
//
// Approach: write the embedded script to a temp file, spawn `python3` against
// it on an OS-assigned port, capture the port from stdout, and drive the
// endpoints over real HTTP. This exercises the actual Python handler logic
// (not a JS reimplementation), so any regression in /kill, the auth check,
// or the state-reset behaviour is caught.
//
// Prerequisites: `python3` must be on PATH (already required by deployRpcServer
// and by the kind CI image).

import { ChildProcess, spawn } from 'child_process'
import * as crypto from 'crypto'
import * as fs from 'fs'
import * as os from 'os'
import * as path from 'path'
import { RPC_SERVER_SCRIPT } from '../src/k8s/rpc-server-script'

const TOKEN = 'test-token-rpc-server-script'
const SERVER_START_TIMEOUT_MS = 15000
const KILL_SETTLE_MS = 500

describe('rpc-server.py /kill endpoint', () => {
  let serverProcess: ChildProcess | null = null
  let serverPort = 0
  let scriptPath = ''
  let workDir = ''

  beforeAll(async () => {
    workDir = fs.mkdtempSync(path.join(os.tmpdir(), 'rpc-server-test-'))
    scriptPath = path.join(workDir, 'rpc-server.py')
    fs.writeFileSync(scriptPath, RPC_SERVER_SCRIPT)
    fs.chmodSync(scriptPath, 0o755)

    // -u: unbuffered stdout. Python defaults to block-buffering when stdout
    // is a pipe, which hides the "RPC server listening on …" line we parse
    // for the bound port until the process exits.
    serverProcess = spawn(
      'python3',
      ['-u', scriptPath, '--port', '0', '--token', TOKEN],
      { stdio: ['ignore', 'pipe', 'pipe'] }
    )

    serverPort = await new Promise<number>((resolve, reject) => {
      const timer = setTimeout(
        () => reject(new Error('python rpc server did not start in time')),
        SERVER_START_TIMEOUT_MS
      )
      let buf = ''
      serverProcess!.stdout!.on('data', (chunk: Buffer) => {
        buf += chunk.toString()
        const m = buf.match(/RPC server listening on 0\.0\.0\.0:(\d+)/)
        if (m) {
          clearTimeout(timer)
          resolve(parseInt(m[1], 10))
        }
      })
      serverProcess!.on('exit', code => {
        clearTimeout(timer)
        reject(new Error(`python rpc server exited prematurely (code=${code})`))
      })
    })
  }, SERVER_START_TIMEOUT_MS + 5000)

  afterAll(async () => {
    if (serverProcess && serverProcess.pid != null) {
      try {
        process.kill(serverProcess.pid, 'SIGTERM')
      } catch {
        // Process may have already exited
      }
    }
    try {
      fs.rmSync(workDir, { recursive: true, force: true })
    } catch {
      // Best-effort cleanup
    }
    try {
      fs.unlinkSync('/tmp/rpc-server.port')
    } catch {
      // Port file may not exist
    }
  })

  function url(p: string): string {
    return `http://127.0.0.1:${serverPort}${p}`
  }

  function writeShellScript(body: string): string {
    const p = path.join(workDir, `${crypto.randomUUID()}.sh`)
    fs.writeFileSync(p, body)
    fs.chmodSync(p, 0o755)
    return p
  }

  async function exec(scriptBody: string, id?: string): Promise<Response> {
    return fetch(url('/exec'), {
      method: 'POST',
      headers: {
        'X-Auth-Token': TOKEN,
        'Content-Type': 'application/json'
      },
      body: JSON.stringify({
        id: id ?? crypto.randomUUID(),
        path: writeShellScript(scriptBody)
      })
    })
  }

  async function statusOnce(): Promise<{
    status: string
    id: string | null
    exit_code: number | null
  }> {
    const r = await fetch(url('/status'), {
      headers: { 'X-Auth-Token': TOKEN }
    })
    return (await r.json()) as {
      status: string
      id: string | null
      exit_code: number | null
    }
  }

  async function waitForStatus(
    predicate: (s: string) => boolean,
    timeoutMs = 5000
  ): Promise<{ status: string; exit_code: number | null }> {
    const start = Date.now()
    while (Date.now() - start < timeoutMs) {
      const s = await statusOnce()
      if (predicate(s.status)) return s
      await new Promise(r => setTimeout(r, 50))
    }
    throw new Error('timed out waiting for status predicate')
  }

  // -----------------------------------------------------------------
  // auth + idempotency
  // -----------------------------------------------------------------

  it('/kill rejects requests with no auth token', async () => {
    const r = await fetch(url('/kill'), { method: 'POST' })
    expect(r.status).toBe(403)
  })

  it('/kill rejects requests with a wrong auth token', async () => {
    const r = await fetch(url('/kill'), {
      method: 'POST',
      headers: { 'X-Auth-Token': 'wrong-token' }
    })
    expect(r.status).toBe(403)
  })

  it('/kill is idempotent on an idle server', async () => {
    // Server is in "idle" state at this point (or "completed"/"failed" from a
    // previous test) — /kill must not error out either way.
    const r = await fetch(url('/kill'), {
      method: 'POST',
      headers: { 'X-Auth-Token': TOKEN }
    })
    expect(r.status).toBe(200)
    const body = (await r.json()) as { status: string }
    expect(['idle', 'completed', 'failed']).toContain(body.status)
  })

  it('/kill can be called twice in a row without changing state', async () => {
    const r1 = await fetch(url('/kill'), {
      method: 'POST',
      headers: { 'X-Auth-Token': TOKEN }
    })
    const r2 = await fetch(url('/kill'), {
      method: 'POST',
      headers: { 'X-Auth-Token': TOKEN }
    })
    expect(r1.status).toBe(200)
    expect(r2.status).toBe(200)
  })

  // -----------------------------------------------------------------
  // /kill terminates a running subprocess
  // -----------------------------------------------------------------

  it('/kill terminates a running subprocess', async () => {
    // Make sure we start from a clean slate.
    await fetch(url('/kill'), {
      method: 'POST',
      headers: { 'X-Auth-Token': TOKEN }
    })

    const startResp = await exec('#!/bin/sh\nsleep 30\n')
    expect(startResp.status).toBe(200)

    // Confirm the subprocess is running before we kill it.
    const running = await waitForStatus(s => s === 'running')
    expect(running.status).toBe('running')

    const killResp = await fetch(url('/kill'), {
      method: 'POST',
      headers: { 'X-Auth-Token': TOKEN }
    })
    expect(killResp.status).toBe(200)

    // Let the wait()-thread observe the SIGTERM exit and update state.
    await new Promise(r => setTimeout(r, KILL_SETTLE_MS))

    const after = await waitForStatus(s => s === 'failed', 5000)
    expect(after.status).toBe('failed')
    // SIGTERM exits with a non-zero code (the server records the negative
    // signal via the wait() thread, or our explicit -1 sentinel). Either
    // way it must not be 0.
    expect(after.exit_code).not.toBe(0)
  })

  // -----------------------------------------------------------------
  // /exec succeeds after /kill (state was reset)
  // -----------------------------------------------------------------

  it('/exec can start a new job after /kill', async () => {
    // Kick off another long sleep…
    const sleepResp = await exec('#!/bin/sh\nsleep 30\n')
    expect(sleepResp.status).toBe(200)
    await waitForStatus(s => s === 'running')

    // …kill it…
    await fetch(url('/kill'), {
      method: 'POST',
      headers: { 'X-Auth-Token': TOKEN }
    })
    await new Promise(r => setTimeout(r, KILL_SETTLE_MS))

    // …and verify a follow-up /exec is accepted (no 409 "A job is already
    // running"). This is the property the cancellation-forwarding patch
    // relies on so post-cleanup steps don't fail after a cancelled step.
    const followup = await exec('#!/bin/sh\nexit 0\n')
    expect(followup.status).toBe(200)

    const done = await waitForStatus(s => s === 'completed' || s === 'failed')
    expect(done.status).toBe('completed')
    expect(done.exit_code).toBe(0)
  })

  // -----------------------------------------------------------------
  // /exec still 409s while another job is in flight (no regression)
  // -----------------------------------------------------------------

  it('/exec returns 409 "A job is already running" while a prior job is in flight', async () => {
    // Ensure clean state from any prior test.
    await fetch(url('/kill'), {
      method: 'POST',
      headers: { 'X-Auth-Token': TOKEN }
    })
    await new Promise(r => setTimeout(r, KILL_SETTLE_MS))

    const first = await exec('#!/bin/sh\nsleep 30\n')
    expect(first.status).toBe(200)
    await waitForStatus(s => s === 'running')

    const second = await exec('#!/bin/sh\nexit 0\n')
    expect(second.status).toBe(409)
    const body = (await second.json()) as { error: string }
    expect(body.error).toMatch(/A job is already running/)

    // Cleanup.
    await fetch(url('/kill'), {
      method: 'POST',
      headers: { 'X-Auth-Token': TOKEN }
    })
  })
})
