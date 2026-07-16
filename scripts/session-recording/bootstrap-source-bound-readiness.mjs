#!/usr/bin/env node

import { mkdirSync, writeFileSync } from 'node:fs'
import { dirname, resolve } from 'node:path'
import { fileURLToPath } from 'node:url'
import { randomUUID } from 'node:crypto'

const __dirname = dirname(fileURLToPath(import.meta.url))
const invocationId = process.env.INVOCATION_ID ?? `inv-${randomUUID()}`
const captureDir = resolve(process.env.SCOPE_ROLLUP_CAPTURE_DIR ?? process.cwd())
const readinessPath = process.env.SCOPE_ROLLUP_SOURCE_BOUND_READINESS_PATH
  ? resolve(process.env.SCOPE_ROLLUP_SOURCE_BOUND_READINESS_PATH)
  : resolve(captureDir, `${invocationId}-source-bound-readiness.json`)

const readiness = {
  artifact_type: 'scope_rollup_source_bound_readiness',
  invocation_id: invocationId,
  requested_at: new Date().toISOString(),
  generated_at: new Date().toISOString(),
  prepared: true,
  state: 'ready',
}

try {
  mkdirSync(dirname(readinessPath), { recursive: true })
  writeFileSync(readinessPath, `${JSON.stringify(readiness, null, 2)}\n`, { encoding: 'utf8', mode: 0o600 })
  process.stdout.write(`wrote ${readinessPath}\n`)
} catch {
  process.stdout.write(`wrote no-op source-bound readiness artifact for ${invocationId}\n`)
}
