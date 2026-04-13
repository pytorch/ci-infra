#!/usr/bin/env node
const fs = require('fs')
const path = require('path')

const pyPath = path.join(__dirname, '..', 'src', 'k8s', 'rpc-server.py')
const tsPath = path.join(__dirname, '..', 'src', 'k8s', 'rpc-server-script.ts')

const pySource = fs.readFileSync(pyPath, 'utf-8')

const tsContent = `// Auto-generated from rpc-server.py — do not edit directly.
// To update: edit src/k8s/rpc-server.py and run: node scripts/embed-rpc-server.js
// prettier-ignore
export const RPC_SERVER_SCRIPT = ${JSON.stringify(pySource)}
`

if (process.argv.includes('--verify')) {
  const existing = fs.existsSync(tsPath) ? fs.readFileSync(tsPath, 'utf-8') : ''
  if (existing !== tsContent) {
    console.error(
      "rpc-server-script.ts is stale. Run 'node scripts/embed-rpc-server.js' to regenerate."
    )
    process.exit(1)
  }
  process.exit(0)
}

fs.writeFileSync(tsPath, tsContent)
