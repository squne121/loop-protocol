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
// Issue #1409: default runtime state lives under the hook-owned subtree
// artifacts/session-manifest-runtime/ (events/ + locks/), which the
// privileged skill runtime executor (scripts/agent-guards/skill_runtime_exec.py) excludes from its
// before/after repo-wide snapshot diff so this hook's own async writes
// are never misattributed to a concurrently-running privileged child
// command as an unauthorized_write_path violation.
const STATE_DIR = process.env.SESSION_MANIFEST_DEBOUNCE_DIR ?? join(REPO_ROOT, 'artifacts', 'session-manifest-runtime')
const EVENTS_DIR = join(STATE_DIR, 'events')
const LOCKS_DIR = join(STATE_DIR, 'locks')
const WORKER_LOCK = join(LOCKS_DIR, 'worker.lock')
const WINDOW_MS = Number.parseInt(process.env.SESSION_MANIFEST_DEBOUNCE_WINDOW_MS ?? '400', 10)
const FLUSH_WAIT_MS = Number.parseInt(process.env.SESSION_MANIFEST_DEBOUNCE_FLUSH_WAIT_MS ?? '3000', 10)
const LOCK_POLL_MS = Number.parseInt(process.env.SESSION_MANIFEST_DEBOUNCE_LOCK_POLL_MS ?? '100', 10)
const PRODUCER_TIMEOUT_MS = Number.parseInt(process.env.SESSION_MANIFEST_DEBOUNCE_PRODUCER_TIMEOUT_MS ?? '5000', 10)
const WORKER_STALE_MS = Number.parseInt(
  process.env.SESSION_MANIFEST_DEBOUNCE_WORKER_STALE_MS ?? String(Math.max(PRODUCER_TIMEOUT_MS * 2, WINDOW_MS + 3000)),
  10,
)
const PRODUCER_MAX_BUFFER_BYTES = Number.parseInt(
  process.env.SESSION_MANIFEST_DEBOUNCE_PRODUCER_MAX_BUFFER_BYTES ?? '1048576',
  10,
)
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
  mkdirSync(LOCKS_DIR, { recursive: true })
}

function nowMs() {
  return Date.now()
}

function buildLockMetadata(role, overrides = {}) {
  const current = nowMs()
  return {
    owner_pid: process.pid,
    role,
    started_at_ms: current,
    heartbeat_at_ms: current,
    ...overrides,
  }
}

function readJsonFile(pathname) {
  try {
    return JSON.parse(readFileSync(pathname, 'utf8'))
  } catch {
    return null
  }
}

function writeLockMetadata(pathname, metadata) {
  writeFileSync(pathname, JSON.stringify(metadata), 'utf8')
}

function processExists(pid) {
  if (!Number.isInteger(pid) || pid <= 0) return false
  try {
    process.kill(pid, 0)
    return true
  } catch {
    return false
  }
}

function isStaleLock(metadata) {
  if (!metadata || typeof metadata !== 'object') return true
  const heartbeat = Number(metadata.heartbeat_at_ms ?? metadata.started_at_ms ?? 0)
  if (!Number.isFinite(heartbeat) || heartbeat <= 0) return true
  if (nowMs() - heartbeat > WORKER_STALE_MS) return true
  if (metadata.owner_pid != null && !processExists(Number(metadata.owner_pid))) return true
  return false
}

function recoverStaleLock(pathname) {
  if (!existsSync(pathname)) return false
  const metadata = readJsonFile(pathname)
  if (!isStaleLock(metadata)) return false
  release(pathname)
  return true
}

function tryAcquire(pathname, role, overrides = {}) {
  ensureStateDir()
  recoverStaleLock(pathname)
  try {
    const fd = openSync(pathname, 'wx')
    closeSync(fd)
    writeLockMetadata(pathname, buildLockMetadata(role, overrides))
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

function refreshLock(pathname, updates = {}) {
  if (!existsSync(pathname)) return
  const existing = readJsonFile(pathname) ?? {}
  writeLockMetadata(pathname, {
    ...existing,
    ...updates,
    heartbeat_at_ms: nowMs(),
  })
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

function tokenizeShellCommand(command) {
  return command.match(/(?:[^\s"']+|"[^"]*"|'[^']*')+/g) ?? []
}

function unquoteToken(token) {
  return token.replace(/^['"]|['"]$/g, '')
}

function looksLikeInlineEnvAssignment(token) {
  return /^[A-Za-z_][A-Za-z0-9_]*=.*/.test(token)
}

function optionStartsMutation(token, shortFlag) {
  return token === shortFlag || token.startsWith(`${shortFlag}=`) || new RegExp(`^-[^-]*${shortFlag.slice(1)}`).test(token)
}

function classifyBash(command) {
  if (typeof command !== 'string' || !command.trim()) return { kind: 'mutation_bash', mutationType: 'bash_unknown' }
  if (/[;><`]|&&|\|\||\$\(|\{|\}/.test(command)) return { kind: 'mutation_bash', mutationType: 'bash_complex' }
  const trimmed = command.trim()
  const tokens = tokenizeShellCommand(trimmed).map(unquoteToken)
  if (tokens.length === 0) return { kind: 'mutation_bash', mutationType: 'bash_unknown' }
  if (looksLikeInlineEnvAssignment(tokens[0])) return { kind: 'mutation_bash', mutationType: 'mutation_bash_unknown' }

  const [tool, ...args] = tokens
  if (tool === 'rg' || tool === 'cat' || tool === 'ls' || tool === 'pwd' || tool === 'test') {
    return { kind: 'readonly_bash', mutationType: 'readonly_bash' }
  }
  if (tool === 'sed') {
    if (args.some((token) => optionStartsMutation(token, '-i') || token === '--in-place' || token.startsWith('--in-place='))) {
      return { kind: 'mutation_bash', mutationType: 'bash_mutation' }
    }
    return { kind: 'readonly_bash', mutationType: 'readonly_bash' }
  }
  if (tool === 'find') {
    if (args.some((token) => ['-delete', '-exec', '-execdir', '-ok', '-okdir'].includes(token))) {
      return { kind: 'mutation_bash', mutationType: 'bash_mutation' }
    }
    return { kind: 'readonly_bash', mutationType: 'readonly_bash' }
  }
  if (tool === 'git') {
    const subcommand = args[0] ?? ''
    if (['status', 'log', 'branch', 'rev-parse', 'ls-files'].includes(subcommand)) {
      return { kind: 'readonly_bash', mutationType: 'readonly_bash' }
    }
    if (['diff', 'show'].includes(subcommand)) {
      if (args.some((token) => token === '--output' || token.startsWith('--output='))) {
        return { kind: 'mutation_bash', mutationType: 'bash_mutation' }
      }
      return { kind: 'readonly_bash', mutationType: 'readonly_bash' }
    }
    return { kind: 'mutation_bash', mutationType: 'mutation_bash_unknown' }
  }
  if (tool === 'gh' && ['issue', 'pr'].includes(args[0] ?? '') && ['view', 'list', 'checks', 'diff'].includes(args[1] ?? '')) {
    return { kind: 'readonly_bash', mutationType: 'readonly_bash' }
  }
  return { kind: 'mutation_bash', mutationType: 'mutation_bash_unknown' }
}

function buildDelta(hookCtx) {
  const toolName = hookCtx?.tool_name ?? hookCtx?.tool ?? ''
  const cwd = hookCtx?.cwd ?? null
  if (toolName === 'Bash') {
    const command = hookCtx?.tool_input?.command ?? ''
    const classification = classifyBash(command)
    const paths = tokenizeShellCommand(command)
      .map(unquoteToken)
      .filter((token) => token && /[./\\]/.test(token))
      .map((token) => sanitizeRelativePath(token, cwd))
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
      issue_number: hookCtx?.issue_number ?? hookCtx?.issue?.number ?? null,
      tool_name: hookCtx?.tool_name ?? hookCtx?.tool ?? null,
      session_manifest_delta: delta,
    }),
    'utf8',
  )
  return { queued: true, kind, delta }
}

function listEventFiles() {
  if (!existsSync(EVENTS_DIR)) return []
  return readdirSync(EVENTS_DIR)
    .filter((name) => name.endsWith('.json'))
    .sort()
    .map((name) => join(EVENTS_DIR, name))
}

function pendingEventCount() {
  return listEventFiles().length
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
    timeout: PRODUCER_TIMEOUT_MS,
    killSignal: 'SIGKILL',
    maxBuffer: PRODUCER_MAX_BUFFER_BYTES,
  })
  const lines = summarizeStderr(result.stderr ?? '')
  if (lines.length > 0) {
    process.stderr.write(`${lines.join('\n')}\n`)
  }
  const timedOut = result.error?.code === 'ETIMEDOUT' || result.signal === 'SIGKILL'
  const reasonCode = timedOut ? 'producer_timeout' : result.status === 0 ? null : 'producer_failed'
  process.stderr.write(
    `SESSION_MANIFEST_DEBOUNCE_RESULT_V1=${JSON.stringify({
      status: result.status === 0 && !timedOut ? 'ok' : 'warn',
      producer_status: result.status ?? 0,
      reason_code: reasonCode,
      stderr_lines: lines.length,
      timed_out: timedOut,
    })}\n`,
  )
  return { ...result, timedOut, reasonCode }
}

/**
 * runFlushLoopCore — DI-injectable state machine for the debounce flush loop.
 *
 * Extracted from flushLoop to enable virtual-clock unit testing (Issue #1141
 * AC4).  Production callers pass filesystem/real-time implementations.
 * Test callers pass virtual clock and in-memory event lists.
 *
 * @param {object} deps
 * @param {() => number} deps.now
 *   Returns current time in ms (wall-clock or virtual).
 * @param {(ms: number) => Promise<void>} deps.sleep
 *   Async sleep; advances clock in tests.
 * @param {() => Array<{timestamp: number}>} deps.listEvents
 *   Returns pending events as {timestamp} objects for quiet-window check.
 * @param {() => Array<object>} deps.readEvents
 *   Returns full event payloads for delta aggregation.
 * @param {(payload: object) => void} deps.runProducer
 *   Invokes the producer with the aggregated payload.
 * @param {() => void} deps.removeEvents
 *   Removes (clears) all pending events after a successful producer call.
 * @param {number} deps.windowMs
 *   Debounce quiet-window in ms.
 *
 * @param {object} [opts]
 * @param {boolean} [opts.force=false]
 *   When true, skips quiet-window check and flushes immediately.
 * @param {number} [opts.maxIterations=Infinity]
 *   Safety iteration cap for unit tests (prevents infinite loops in tests).
 */
export async function runFlushLoopCore(
  deps,
  { force = false, maxIterations = Infinity } = {},
) {
  const { now, sleep, listEvents, readEvents, runProducer: callProducer, removeEvents, windowMs } = deps
  let iteration = 0
  while (true) {
    if (iteration++ >= maxIterations) break
    const events = listEvents()
    if (events.length === 0) break
    if (!force) {
      const newestTimestamp = Math.max(...events.map((e) => e.timestamp))
      const quietForMs = now() - newestTimestamp
      if (quietForMs < windowMs) {
        await sleep(windowMs - quietForMs)
        continue
      }
    }
    const payloads = readEvents()
    const aggregatedDelta = []
    for (const event of payloads) {
      for (const item of event.session_manifest_delta ?? event.delta ?? []) {
        const signature = JSON.stringify(item)
        if (!aggregatedDelta.some((existing) => JSON.stringify(existing) === signature)) {
          aggregatedDelta.push(item)
        }
      }
    }
    const latest = payloads[payloads.length - 1]
    callProducer({
      hook_event_name: 'PostToolUse',
      tool_name: 'DebounceBatch',
      session_id: latest?.session_id ?? null,
      issue_number: latest?.issue_number ?? null,
      session_manifest_delta: aggregatedDelta,
      debounce_event_count: payloads.length,
      debounce_flush_reason: force ? 'forced_flush' : 'debounced',
    })
    removeEvents()
    if (force) continue
  }
}

async function flushLoop(opts = {}) {
  const { force = false } = opts;
  return runFlushLoopCore({
    now: () => Date.now(),
    sleep: async (ms) => {
      refreshLock(WORKER_LOCK);
      await new Promise((resolvePromise) => globalThis.setTimeout(resolvePromise, ms));
      refreshLock(WORKER_LOCK);
    },
    listEvents: () =>
      listEventFiles().map((file) => ({
        timestamp: extractTimestampFromEventFile(file),
        file,
      })),
    readEvents: () =>
      listEventFiles().map((file) => JSON.parse(readFileSync(file, 'utf8'))),
    runProducer: (payload) => {
      refreshLock(WORKER_LOCK);
      runProducer(payload);
    },
    removeEvents: () => {
      for (const file of listEventFiles()) {
        rmSync(file, { force: true });
      }
    },
    windowMs: WINDOW_MS,
    force,
  });
}

async function flushWithLockHandling() {
  const deadline = nowMs() + FLUSH_WAIT_MS
  while (true) {
    if (tryAcquire(WORKER_LOCK, 'flush')) {
      try {
        await flushLoop({ force: true })
      } finally {
        release(WORKER_LOCK)
      }
      return 0
    }

    if (pendingEventCount() === 0) {
      process.stderr.write(
        `SESSION_MANIFEST_DEBOUNCE_RESULT_V1=${JSON.stringify({
          status: 'ok',
          reason_code: 'flush_completed_by_worker',
          stderr_lines: 0,
        })}\n`,
      )
      return 0
    }

    if (recoverStaleLock(WORKER_LOCK)) continue

    if (nowMs() >= deadline) {
      process.stderr.write(
        `SESSION_MANIFEST_DEBOUNCE_RESULT_V1=${JSON.stringify({
          status: 'warn',
          reason_code: 'flush_pending_timeout_lock_held',
          pending_event_count: pendingEventCount(),
          stderr_lines: 0,
        })}\n`,
      )
      return 124
    }

    await new Promise((resolvePromise) => globalThis.setTimeout(resolvePromise, LOCK_POLL_MS))
  }
}

async function main() {
  if (process.argv[2] === '--worker') {
    try {
      refreshLock(WORKER_LOCK, { role: 'worker', owner_pid: process.pid })
      await flushLoop({ force: false })
    } finally {
      release(WORKER_LOCK)
    }
    return
  }

  if (process.argv[2] === '--flush') {
    process.exitCode = await flushWithLockHandling()
    return
  }

  const hookCtx = await readStdin()
  if (!hookCtx || (hookCtx.hook_event_name ?? hookCtx.type) !== 'PostToolUse') return
  const result = queueEvent(hookCtx)
  if (!result.queued) return
  if (tryAcquire(WORKER_LOCK, 'worker')) {
    try {
      const child = spawn(process.execPath, [SCRIPT_PATH, '--worker'], {
        detached: true,
        stdio: 'ignore',
        env: process.env,
      })
      writeLockMetadata(
        WORKER_LOCK,
        buildLockMetadata('worker', {
          owner_pid: child.pid,
        }),
      )
      child.unref()
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

// Guard: only run main() when executed directly, not when imported as a module.
// This allows test files to import `runFlushLoopCore` without side effects.
const isDirectRun =
  process.argv[1] != null &&
  fileURLToPath(import.meta.url) === resolve(process.argv[1])

if (isDirectRun) {
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
}
