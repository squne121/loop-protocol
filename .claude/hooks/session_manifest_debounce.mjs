#!/usr/bin/env node
/* global process */

import { randomUUID } from 'node:crypto'
import { existsSync, mkdirSync, openSync, closeSync, readFileSync, readdirSync, rmSync, unlinkSync, writeFileSync } from 'node:fs'
import { basename, dirname, isAbsolute, join, relative, resolve } from 'node:path'
import { fileURLToPath } from 'node:url'
import { spawn, spawnSync } from 'node:child_process'

const __dirname = dirname(fileURLToPath(import.meta.url))
const SCRIPT_PATH = fileURLToPath(import.meta.url)
const REPO_ROOT = resolve(process.env.CLAUDE_PROJECT_DIR ?? resolve(__dirname, '..', '..'))
const STATE_DIR = process.env.SESSION_MANIFEST_DEBOUNCE_DIR ?? join(REPO_ROOT, 'artifacts', 'session-manifest-debounce')
const EVENTS_DIR = join(STATE_DIR, 'events')
const WORKER_LOCK = join(STATE_DIR, 'worker.lock')
const WINDOW_MS = Number.parseInt(process.env.SESSION_MANIFEST_DEBOUNCE_WINDOW_MS ?? '400', 10)
const PRODUCER_CMD = process.env.SESSION_MANIFEST_DEBOUNCE_PRODUCER_CMD ?? process.execPath
const PRODUCER_ARGS = process.env.SESSION_MANIFEST_DEBOUNCE_PRODUCER_ARGS_JSON
  ? JSON.parse(process.env.SESSION_MANIFEST_DEBOUNCE_PRODUCER_ARGS_JSON)
  : [join(REPO_ROOT, '.claude', 'hooks', 'generate_session_manifest_from_hook.mjs')]

function readStdin() {
  return new Promise((resolvePromise) => {
    let data = ''
    process.stdin.setEncoding('utf8')
    process.stdin.on('data', (chunk) => {
      data += chunk
    })
    process.stdin.on('end', () => {
      if (!data.trim()) {
        resolvePromise(null)
        return
      }
      try {
        resolvePromise(JSON.parse(data))
      } catch {
        resolvePromise(null)
      }
    })
    process.stdin.on('error', () => resolvePromise(null))
  })
}

function ensureStateDir() {
  mkdirSync(EVENTS_DIR, { recursive: true })
}

function tryAcquire(pathname) {
  try {
    const fd = openSync(pathname, 'wx')
    closeSync(fd)
    return true
  } catch {
    return false
  }
}

function release(pathname) {
  try {
    unlinkSync(pathname)
  } catch {
    // ignore
  }
}

function sanitizeForStderr(msg) {
  return String(msg)
    .replace(/[A-Za-z]:\\[^\s"']+/g, '<path>')
    .replace(/\/mnt\/[A-Za-z]\/[^\s"']+/g, '<path>')
    .replace(/\/[^\s"']+/g, '<path>')
}

function sanitizeRelativePath(rawPath, cwd) {
  if (typeof rawPath !== 'string' || !rawPath.trim()) return null
  const trimmed = rawPath.trim()
  const normalized = trimmed.replace(/\\/g, '/')
  const cwdNormalized = typeof cwd === 'string' ? cwd.replace(/\\/g, '/') : null
  if (cwdNormalized && isAbsolute(normalized)) {
    const rel = relative(cwdNormalized, normalized).replace(/\\/g, '/')
    if (rel && !rel.startsWith('..')) return rel
  }
  if (!isAbsolute(normalized) && !normalized.startsWith('..')) return normalized
  return basename(normalized)
}

function extractPathCandidates(input) {
  if (!input || typeof input !== 'object') return []
  const candidates = []
  for (const key of ['path', 'file_path', 'old_path', 'new_path', 'target_path']) {
    if (typeof input[key] === 'string') candidates.push(input[key])
  }
  if (Array.isArray(input.paths)) {
    for (const entry of input.paths) {
      if (typeof entry === 'string') candidates.push(entry)
    }
  }
  return candidates
}

function classifyBash(command) {
  if (typeof command !== 'string' || !command.trim()) return { kind: 'mutation_bash', mutationType: 'bash_unknown' }
  if (/[;><`]|&&|\|\||\$\(|\{|\}/.test(command)) return { kind: 'mutation_bash', mutationType: 'bash_complex' }
  const trimmed = command.trim()
  const readonlyPatterns = [
    /^rg\b/,
    /^sed\b/,
    /^cat\b/,
    /^ls\b/,
    /^pwd\b/,
    /^find\b/,
    /^test\b/,
    /^git (status|diff|log|show|branch|rev-parse|ls-files)\b/,
    /^gh (issue|pr) (view|list|checks|diff)\b/,
  ]
  if (readonlyPatterns.some((pattern) => pattern.test(trimmed))) {
    return { kind: 'readonly_bash', mutationType: 'readonly_bash' }
  }
  return { kind: 'mutation_bash', mutationType: 'bash_mutation' }
}

function buildDelta(hookCtx) {
  const toolName = hookCtx?.tool_name ?? hookCtx?.tool ?? ''
  const cwd = hookCtx?.cwd ?? null
  if (toolName === 'Bash') {
    const command = hookCtx?.tool_input?.command ?? ''
    const classification = classifyBash(command)
    const paths = command
      .split(/\s+/)
      .filter((token) => token && /[./\\]/.test(token))
      .map((token) => sanitizeRelativePath(token.replace(/^['"]|['"]$/g, ''), cwd))
      .filter(Boolean)
    return { kind: classification.kind, delta: [{ mutation_type: classification.mutationType, relative_paths: [...new Set(paths)] }] }
  }
  if (toolName === 'Edit' || toolName === 'Write') {
    const sanitizedPaths = extractPathCandidates(hookCtx?.tool_input)
      .map((candidate) => sanitizeRelativePath(candidate, cwd))
      .filter(Boolean)
    return {
      kind: 'mutation_tool',
      delta: [{ mutation_type: toolName.toLowerCase(), relative_paths: [...new Set(sanitizedPaths)] }],
    }
  }
  return { kind: 'mutation_tool', delta: [{ mutation_type: String(toolName || 'unknown').toLowerCase(), relative_paths: [] }] }
}

function queueEvent(hookCtx) {
  ensureStateDir()
  const { kind, delta } = buildDelta(hookCtx)
  if (kind === 'readonly_bash') return { queued: false, kind, delta }
  const eventPath = join(EVENTS_DIR, `${Date.now()}-${randomUUID()}.json`)
  writeFileSync(
    eventPath,
    JSON.stringify({
      hook_event_name: hookCtx?.hook_event_name ?? 'PostToolUse',
      session_id: hookCtx?.session_id ?? null,
      cwd: hookCtx?.cwd ?? null,
      issue_number: hookCtx?.issue_number ?? hookCtx?.issue?.number ?? null,
      tool_name: hookCtx?.tool_name ?? hookCtx?.tool ?? null,
      delta,
    }),
    'utf8',
  )
  return { queued: true, kind, delta }
}

function spawnWorker() {
  const child = spawn(process.execPath, [SCRIPT_PATH, '--worker'], {
    detached: true,
    stdio: 'ignore',
    env: process.env,
  })
  child.unref()
}

function listEventFiles() {
  if (!existsSync(EVENTS_DIR)) return []
  return readdirSync(EVENTS_DIR)
    .filter((name) => name.endsWith('.json'))
    .sort()
    .map((name) => join(EVENTS_DIR, name))
}

function extractTimestampFromEventFile(file) {
  const stem = basename(file).split('-', 1)[0]
  const parsed = Number.parseInt(stem, 10)
  return Number.isFinite(parsed) ? parsed : Date.now()
}

function summarizeStderr(raw) {
  const lines = sanitizeForStderr(raw)
    .split(/\r?\n/)
    .map((line) => line.trim())
    .filter(Boolean)
    .slice(0, 8)
  return lines
}

function runProducer(payload) {
  const result = spawnSync(PRODUCER_CMD, PRODUCER_ARGS, {
    input: JSON.stringify(payload),
    encoding: 'utf8',
    stdio: ['pipe', 'pipe', 'pipe'],
    env: process.env,
  })
  const lines = summarizeStderr(result.stderr ?? '')
  if (lines.length > 0) {
    process.stderr.write(`${lines.join('\n')}\n`)
  }
  process.stderr.write(
    `SESSION_MANIFEST_DEBOUNCE_RESULT_V1=${JSON.stringify({
      status: result.status === 0 ? 'ok' : 'warn',
      producer_status: result.status ?? 0,
      reason_code: result.status === 0 ? null : 'producer_failed',
      stderr_lines: lines.length,
    })}\n`,
  )
}

async function flushLoop({ force = false }) {
  while (true) {
    const files = listEventFiles()
    if (files.length === 0) break
    if (!force) {
      const newestTimestamp = Math.max(...files.map((file) => extractTimestampFromEventFile(file)))
      const quietForMs = Date.now() - newestTimestamp
      if (quietForMs < WINDOW_MS) {
        await new Promise((resolvePromise) => globalThis.setTimeout(resolvePromise, WINDOW_MS - quietForMs))
        continue
      }
    }
    const events = files.map((file) => JSON.parse(readFileSync(file, 'utf8')))
    const aggregatedDelta = []
    for (const event of events) {
      for (const item of event.delta ?? []) {
        const signature = JSON.stringify(item)
        if (!aggregatedDelta.some((existing) => JSON.stringify(existing) === signature)) {
          aggregatedDelta.push(item)
        }
      }
    }
    const latest = events[events.length - 1]
    runProducer({
      hook_event_name: 'PostToolUse',
      tool_name: 'DebounceBatch',
      session_id: latest?.session_id ?? null,
      issue_number: latest?.issue_number ?? null,
      session_manifest_delta: aggregatedDelta,
      debounce_event_count: events.length,
      debounce_flush_reason: force ? 'forced_flush' : 'debounced',
    })
    for (const file of files) {
      rmSync(file, { force: true })
    }
    if (force) continue
  }
}

async function main() {
  if (process.argv[2] === '--worker') {
    try {
      await flushLoop({ force: false })
    } finally {
      release(WORKER_LOCK)
    }
    return
  }

  if (process.argv[2] === '--flush') {
    const lockAcquired = tryAcquire(WORKER_LOCK)
    if (!lockAcquired) {
      process.stderr.write(
        `SESSION_MANIFEST_DEBOUNCE_RESULT_V1=${JSON.stringify({
          status: 'ok',
          reason_code: 'flush_skipped_lock_held',
          stderr_lines: 0,
        })}\n`,
      )
      return
    }
    try {
      await flushLoop({ force: true })
    } finally {
      release(WORKER_LOCK)
    }
    return
  }

  const hookCtx = await readStdin()
  if (!hookCtx || (hookCtx.hook_event_name ?? hookCtx.type) !== 'PostToolUse') return
  const result = queueEvent(hookCtx)
  if (!result.queued) return
  if (tryAcquire(WORKER_LOCK)) {
    try {
      spawnWorker()
    } catch (error) {
      release(WORKER_LOCK)
      process.stderr.write(
        `SESSION_MANIFEST_DEBOUNCE_RESULT_V1=${JSON.stringify({
          status: 'warn',
          reason_code: 'spawn_failed',
          detail: sanitizeForStderr(error.message),
        })}\n`,
      )
    }
  }
}

main().catch((error) => {
  process.stderr.write(
    `SESSION_MANIFEST_DEBOUNCE_RESULT_V1=${JSON.stringify({
      status: 'warn',
      reason_code: 'unexpected_error',
      detail: sanitizeForStderr(error.message),
    })}\n`,
  )
  process.exit(0)
})
