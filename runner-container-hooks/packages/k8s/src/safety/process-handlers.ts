import * as core from '@actions/core'

let installed = false

function fatal(msg: string): void {
  core.error(msg)
  process.stderr.write(`\n[runner-container-hooks] FATAL: ${msg}\n`)
}

export function installProcessHandlers(): void {
  if (installed) return
  installed = true

  process.on('SIGTERM', () => {
    fatal('received SIGTERM')
    process.exit(143)
  })

  process.on('uncaughtException', err => {
    const msg = err instanceof Error ? err.stack || err.message : String(err)
    fatal(`uncaughtException: ${msg}`)
    process.exit(1)
  })

  process.on('unhandledRejection', reason => {
    const msg =
      reason instanceof Error ? reason.stack || reason.message : String(reason)
    fatal(`unhandledRejection: ${msg}`)
    process.exit(1)
  })
}
