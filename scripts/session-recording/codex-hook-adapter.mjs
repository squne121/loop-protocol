#!/usr/bin/env node

import { appendFile, mkdir } from 'node:fs/promises'
import { Buffer } from 'node:buffer'
import { homedir } from 'node:os'
import { resolve } from 'node:path'
import { clearTimeout, setTimeout } from 'node:timers'

const ALLOWED_EVENTS = new Set(['SessionEnd', 'SubagentStop'])
const MAX_STDIN_BYTES = 64 * 1024
const WRITE_BUDGET_MS = 1000

function selectedEvent(argv) {
  const index = argv.indexOf('--event')
  const event = index >= 0 ? argv[index + 1] : ''
  return ALLOWED_EVENTS.has(event) ? event : null
}

async function readBoundedStdin() {
  const chunks = []
  let size = 0
  for await (const chunk of process.stdin) {
    size += chunk.length
    if (size > MAX_STDIN_BYTES) return null
    chunks.push(chunk)
  }
  if (chunks.length === 0) return {}
  try {
    const value = JSON.parse(Buffer.concat(chunks).toString('utf8'))
    return value && typeof value === 'object' && !Array.isArray(value) ? value : null
  } catch {
    return null
  }
}

function safeIdentifier(value) {
  return typeof value === 'string' && /^[A-Za-z0-9._:-]{1,128}$/.test(value)
    ? value
    : undefined
}

function advisoryRecord(event, payload) {
  const record = {
    schema: 'codex_passive_session_record/v1',
    event,
    recorded_at: new Date().toISOString(),
  }
  // Explicit allowlist: never retain transcript, messages, commands, tool
  // inputs/outputs, decisions, additional context, or arbitrary payload keys.
  for (const key of ['session_id', 'thread_id', 'agent_id']) {
    const value = safeIdentifier(payload?.[key])
    if (value !== undefined) record[key] = value
  }
  return record
}

function recordingDirectory() {
  return process.env.CODEX_PASSIVE_RECORDING_DIR
    ? resolve(process.env.CODEX_PASSIVE_RECORDING_DIR)
    : resolve(homedir(), '.codex', 'session-recording')
}

async function recordBestEffort(event, payload) {
  const directory = recordingDirectory()
  await mkdir(directory, { recursive: true, mode: 0o700 })
  await appendFile(
    resolve(directory, 'passive-events.jsonl'),
    `${JSON.stringify(advisoryRecord(event, payload))}\n`,
    { encoding: 'utf8', mode: 0o600 },
  )
}

async function withinWriteBudget(promise) {
  let timer
  try {
    await Promise.race([
      promise,
      new Promise((resolveTimeout) => {
        timer = setTimeout(resolveTimeout, WRITE_BUDGET_MS)
        timer.unref?.()
      }),
    ])
  } finally {
    if (timer) clearTimeout(timer)
  }
}

async function main() {
  const event = selectedEvent(process.argv)
  if (!event) return

  const payload = await readBoundedStdin()
  if (payload !== null) {
    try {
      await withinWriteBudget(recordBestEffort(event, payload))
    } catch {
      // Advisory recorder: EACCES, ENOSPC, EIO, schema and all other errors
      // are deliberately fail-open and produce no diagnostic payload.
    }
  }

  if (event === 'SubagentStop') {
    process.stdout.write('{"continue":true}')
  }
}

main().catch(() => {
  if (selectedEvent(process.argv) === 'SubagentStop') {
    process.stdout.write('{"continue":true}')
  }
  process.exitCode = 0
})
