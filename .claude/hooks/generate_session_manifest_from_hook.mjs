#!/usr/bin/env node
/* global process */

/**
 * generate_session_manifest_from_hook.mjs
 *
 * Claude Code hook wrapper for agent_session_manifest/v1 producer.
 * Invoked by Stop / SubagentStop / scoped PostToolUse hooks.
 *
 * Reads hook context from stdin (JSON), constructs producer CLI arguments,
 * invokes scripts/generate-session-manifest.mjs, and writes the manifest
 * atomically to an artifacts/ file.
 *
 * Design:
 * - stdout is SILENT (no manifest JSON on stdout)
 * - transcript_path / cwd absolute paths are NOT included in any public output
 * - artifact file write is atomic (temp + rename)
 * - duplicate events (same stable key) are skipped
 * - producer failure exits 0 (best-effort telemetry — session is NOT blocked)
 *
 * Artifact naming: private-agent-session-manifest-{eventName}-{timestamp}.json
 *   timestamp = Date.now() milliseconds
 *   No content hash in filename (avoids circular reference with evidenceSourceRef)
 *   content hash stored as artifact_sha256 field via producer-generated manifest
 *
 * Duplicate stable key: {hookEventName}:{sessionId}:{toolName}:{phase}:{payloadDigest}
 *   Scanned from existing artifact filenames in artifacts/ dir.
 *   Filename encodes key via URL-safe base64 segment.
 *   payload_digest: sha256 of serialized stdin payload (first 16 hex chars)
 *   Same payload digest → skip (throttle identical events from duplicate triggers)
 *
 * stdin: Claude hook context JSON (varies by event type)
 * stdout: empty (silent)
 * stderr: diagnostic messages only (no manifest content, no absolute paths)
 * exit 0: always (best-effort — producer failure does not block session)
 */

import { createHash, randomUUID } from 'node:crypto'
import {
  existsSync,
  mkdirSync,
  readdirSync,
  renameSync,
  writeFileSync,
  openSync,
  closeSync,
  unlinkSync,
} from 'node:fs'
import { basename, dirname, isAbsolute, join, relative, resolve } from 'node:path'
import { fileURLToPath } from 'node:url'
import { execFileSync } from 'node:child_process'

const __dirname = dirname(fileURLToPath(import.meta.url))

// ============================================================================
// Configuration
// ============================================================================

const REPO_ROOT = resolve(process.env.CLAUDE_PROJECT_DIR ?? resolve(__dirname, '..', '..'))
const PRODUCER_SCRIPT = process.env.SESSION_MANIFEST_PRODUCER_SCRIPT ?? join(REPO_ROOT, 'scripts', 'generate-session-manifest.mjs')
const ARTIFACTS_DIR = process.env.SESSION_MANIFEST_ARTIFACTS_DIR ?? join(REPO_ROOT, 'artifacts')
const REPOSITORY = 'squne121/loop-protocol'

// Event-type to phase mapping
const EVENT_PHASE_MAP = {
  Stop: { mainLoop: 'impl', ledgerPhase: 'post_commit_verification' },
  SubagentStop: { mainLoop: 'impl', ledgerPhase: 'post_commit_verification' },
  PostToolUse: { mainLoop: 'impl', ledgerPhase: 'implementation' },
}

// ============================================================================
// Helpers
// ============================================================================

/**
 * Read and parse stdin as JSON.
 * Returns null if stdin is empty or not valid JSON.
 */
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

/**
 * Compute SHA-256 hash of a string.
 */
function sha256(content) {
  return createHash('sha256').update(content).digest('hex')
}

/**
 * Strip absolute path patterns from a string for safe stderr output.
 */
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

function normalizePayloadForDigest(payload) {
  if (!payload || typeof payload !== 'object') return payload
  const normalized = { ...payload }
  delete normalized.tool_use_id
  delete normalized.transcript_path
  delete normalized.timestamp
  delete normalized.invocation_id
  if (typeof normalized.cwd === 'string') {
    normalized.cwd = sanitizeRelativePath(normalized.cwd, normalized.cwd) ?? '<cwd>'
  }
  if (normalized.hook_event_name === 'PostToolUse' && normalized.tool_name === 'Bash') {
    const command = normalized.tool_input?.command
    if (typeof command === 'string') {
      normalized.tool_input = {
        ...normalized.tool_input,
        command: command.replace(/[A-Za-z]:\\[^\s"']+/g, '<path>').replace(/\/[^\s"']+/g, '<path>'),
      }
    }
  }
  return normalized
}

/**
 * Ensure artifacts directory exists.
 */
function ensureArtifactsDir() {
  if (!existsSync(ARTIFACTS_DIR)) {
    mkdirSync(ARTIFACTS_DIR, { recursive: true })
  }
}

/**
 * Recursively sort all object keys to produce a canonical JSON string.
 * Arrays are preserved in order; object keys are sorted at every depth.
 */
function canonicalJson(value) {
  if (value === null || typeof value !== 'object') {
    return JSON.stringify(value)
  }
  if (Array.isArray(value)) {
    return '[' + value.map(canonicalJson).join(',') + ']'
  }
  const sorted = Object.keys(value)
    .sort()
    .map((k) => JSON.stringify(k) + ':' + canonicalJson(value[k]))
  return '{' + sorted.join(',') + '}'
}

/**
 * Compute SHA-256 payload digest for throttle deduplication.
 * Uses recursive canonical JSON serialization to preserve nested objects.
 * Returns first 16 hex chars of sha256(canonicalJson(payload)).
 */
function computePayloadDigest(payload) {
  if (payload == null) return 'nullpayload'
  try {
    const serialized = canonicalJson(normalizePayloadForDigest(payload))
    return sha256(serialized).slice(0, 16)
  } catch {
    return 'digestfail'
  }
}

/**
 * Build a stable key segment by hashing all key material together.
 * Avoids base64 truncation that would drop payloadDigest from the tail.
 * Returns first 32 hex chars of sha256(canonicalJson(keyMaterial)).
 */
function buildStableKey(hookEventName, sessionId, toolName, ledgerPhase, payloadDigest, loopStateHash) {
  const keyMaterial = {
    hookEventName,
    sessionId: sessionId || 'nosession',
    triggerOrTool: toolName || '',
    ledgerPhase,
    payloadDigest: payloadDigest || 'nodigest',
    loopStateHash: loopStateHash || '',
  }
  return sha256(canonicalJson(keyMaterial)).slice(0, 32)
}

/**
 * Try to claim an exclusive lock for a given stable key.
 * Uses O_EXCL (openSync with 'wx') to atomically create the lock file.
 * Returns true if the lock was acquired, false if another process holds it.
 */
function tryAcquireLock(lockPath) {
  try {
    const fd = openSync(lockPath, 'wx')
    closeSync(fd)
    return true
  } catch {
    // EEXIST — lock already held
    return false
  }
}

/**
 * Release a previously acquired lock file.
 */
function releaseLock(lockPath) {
  try {
    unlinkSync(lockPath)
  } catch {
    // ignore cleanup errors
  }
}

/**
 * Check if a duplicate artifact exists by scanning for stable key segment in filenames.
 * Must be called while holding the lock for the given key.
 */
function hasDuplicateArtifact(stableKeySegment) {
  try {
    if (!existsSync(ARTIFACTS_DIR)) return false
    const files = readdirSync(ARTIFACTS_DIR)
    return files.some((f) => f.includes(stableKeySegment) && f.endsWith('.json'))
  } catch {
    return false
  }
}

// ============================================================================
// Issue Identity Resolution
// ============================================================================

/** Token-boundary regex for issue number extraction from branch/path strings. */
const ISSUE_BRANCH_REGEX = /(?:^|[/_-])issue-([1-9][0-9]*)(?=$|[/_-])/

/**
 * Return true if value is a valid positive integer issue number (no leading zeros, > 0).
 * Rejects: 0, negative, decimal, empty string, non-numeric.
 */
export function isValidIssueNumberValue(value) {
  if (value === null || value === undefined || value === '') return false
  const str = String(value)
  if (!/^[1-9][0-9]*$/.test(str)) return false
  const num = Number(str)
  return Number.isInteger(num) && num > 0
}

/**
 * Extract issue number from a string (branch name or path) using the token-boundary regex.
 * Returns null if no valid issue number is found.
 */
export function extractIssueFromString(str) {
  if (!str) return null
  const match = ISSUE_BRANCH_REGEX.exec(str)
  if (!match) return null
  const num = parseInt(match[1], 10)
  return num > 0 ? num : null
}

/**
 * Extract issue number from trusted payload keys only.
 * Forbidden: arbitrary key scanning, tool_input.command, transcript/message fields.
 */
function extractFromPayload(hookCtx) {
  if (!hookCtx || typeof hookCtx !== 'object') return null
  const candidates = [
    hookCtx.issue_number,
    hookCtx.issue?.number,
    hookCtx.issueNumber,
    hookCtx.loop?.issue_number,
  ]
  for (const value of candidates) {
    if (isValidIssueNumberValue(value)) return parseInt(String(value), 10)
  }
  return null
}

/**
 * Resolve issue number for phase_instance_id construction.
 *
 * Priority: trusted payload keys → git branch → cwd/worktree path → null (→ issue-0 sentinel)
 *
 * @param {object|null} hookCtx - Parsed hook stdin payload
 * @param {{ branchName?: string|null, cwdPath?: string|null }} opts
 * @returns {number|null} Resolved positive issue number, or null if unresolved
 */
export function resolveIssueNumber(hookCtx, { branchName = null, cwdPath = null } = {}) {
  // 1. Trusted payload keys (highest priority)
  const fromPayload = extractFromPayload(hookCtx)
  if (fromPayload !== null) return fromPayload

  // 2. Git branch name
  const fromBranch = extractIssueFromString(branchName)
  if (fromBranch !== null) return fromBranch

  // 3. cwd / worktree path
  const fromCwd = extractIssueFromString(cwdPath)
  if (fromCwd !== null) return fromCwd

  return null
}

/**
 * Build producer CLI arguments array from resolved context.
 * Exported for unit testing so that --phase-instance-id and --issue presence
 * can be verified without spawning a subprocess.
 *
 * @param {{ producerScript: string, repository: string, phaseInfo: {mainLoop:string,ledgerPhase:string},
 *            phaseInstanceId: string, actorType: string, actorName: string,
 *            evidenceSourceKind: string, evidenceSourceRef: string, evidenceVisibility: string,
 *            sessionId: string|null, resolvedIssueNumber: number|null }} params
 * @returns {string[]}
 */
export function buildProducerArgs({
  producerScript,
  repository,
  phaseInfo,
  phaseInstanceId,
  actorType,
  actorName,
  evidenceSourceKind,
  evidenceSourceRef,
  evidenceVisibility,
  sessionId,
  resolvedIssueNumber,
}) {
  const args = [
    producerScript,
    '--repository',
    repository,
    '--phase-main-loop',
    phaseInfo.mainLoop,
    '--phase-ledger-phase',
    phaseInfo.ledgerPhase,
    '--phase-instance-id',
    phaseInstanceId,
    '--actor-type',
    actorType,
    '--actor-name',
    actorName,
    '--evidence-source-kind',
    evidenceSourceKind,
    '--evidence-source-ref',
    evidenceSourceRef,
    '--evidence-visibility',
    evidenceVisibility,
    '--format',
    'json',
    '--validate',
  ]
  if (sessionId) {
    args.push('--actor-session-id', sessionId)
  }
  if (resolvedIssueNumber !== null) {
    args.push('--issue', String(resolvedIssueNumber))
  }
  return args
}

// ============================================================================
// Main
// ============================================================================

// Module-level variable so that the catch handler can release any held lock
let _activeLockPath = null

async function main() {
  // Read hook context from stdin
  const hookCtx = await readStdin()

  if (!hookCtx) {
    process.stderr.write('[generate_session_manifest_from_hook] warn: no stdin context\n')
  }

  // Extract fields from hook stdin
  const hookEventName = hookCtx?.hook_event_name ?? hookCtx?.type ?? 'Stop'
  // session_id: may not be present in all hook payloads; use cwd-derived fallback
  const sessionId = hookCtx?.session_id ?? null
  // PostToolUse specific fields
  const toolName = hookCtx?.tool_name ?? hookCtx?.tool ?? null
  // tool_use_id: read for potential future use; not currently passed to producer CLI
  // (producer does not have a --tool-use-id argument; encoded in phase-instance-id instead)
  const _toolUseId = hookCtx?.tool_use_id ?? null
  void _toolUseId // explicitly unused — suppress lint warning
  // SubagentStop specific fields
  const agentId = hookCtx?.agent_id ?? hookCtx?.subagent_id ?? null

  // Map event to phase info
  const phaseInfo = EVENT_PHASE_MAP[hookEventName] ?? EVENT_PHASE_MAP['Stop']

  // Compute payload digest for throttle (same payload → same digest → skip)
  const payloadDigest = computePayloadDigest(hookCtx)

  // Build stable duplicate key — hash of all key material (no truncation of payloadDigest)
  const stableKeySegment = buildStableKey(hookEventName, sessionId, toolName, phaseInfo.ledgerPhase, payloadDigest, null)

  // Ensure artifacts dir exists before attempting lock
  ensureArtifactsDir()

  // Acquire exclusive lock for this stable key to prevent parallel duplicate generation (B3)
  const lockPath = join(ARTIFACTS_DIR, `.lock-${stableKeySegment}`)
  _activeLockPath = lockPath
  const lockAcquired = tryAcquireLock(lockPath)
  if (!lockAcquired) {
    process.stderr.write(
      `[generate_session_manifest_from_hook] info: lock held by another process — skipping (key=${stableKeySegment}, event=${hookEventName})\n`,
    )
    process.exit(0)
  }

  // Double-check for duplicate after acquiring the lock (lock-then-check pattern)
  if (hasDuplicateArtifact(stableKeySegment)) {
    releaseLock(lockPath)
    process.stderr.write(
      `[generate_session_manifest_from_hook] info: duplicate skip (key=${stableKeySegment}, event=${hookEventName})\n`,
    )
    process.exit(0)
  }

  // Resolve issue identity: payload → branch → cwd → issue-0 sentinel
  // B1: use hookCtx.cwd (the active worktree cwd from hook stdin) rather than
  // process.cwd() / REPO_ROOT so that worktrees are identified correctly.
  const hookCwd = typeof hookCtx?.cwd === 'string' ? hookCtx.cwd : null
  const gitCwd = hookCwd ?? REPO_ROOT

  let currentBranchName = null
  try {
    currentBranchName =
      execFileSync('git', ['branch', '--show-current'], {
        encoding: 'utf8',
        cwd: gitCwd,
        stdio: ['pipe', 'pipe', 'pipe'],
      }).trim() || null
  } catch {
    // git unavailable or not a repo — fall through to cwd extraction
  }
  const resolvedIssueNumber = resolveIssueNumber(hookCtx, {
    branchName: currentBranchName,
    cwdPath: hookCwd ?? process.cwd(),
  })

  // Build sequence ID: must be 3-digit zero-padded number (producer validates /^[0-9]{3}$/)
  // Use last 3 digits of epoch seconds to get a stable-ish 3-digit number
  const seqNum = String(Math.floor(Date.now() / 1000) % 1000).padStart(3, '0')
  const issuePrefix = resolvedIssueNumber !== null ? `issue-${resolvedIssueNumber}` : 'issue-0'
  const phaseInstanceId = `${issuePrefix}:${phaseInfo.mainLoop}:${seqNum}`

  // Artifact filename: timestamp-based with stable key segment (no content hash — avoids circular ref)
  const timestamp = Date.now()
  const eventNameLower = hookEventName.toLowerCase()
  const baseFilename = `private-agent-session-manifest-${eventNameLower}-${timestamp}-${stableKeySegment}.json`

  // Evidence source ref uses relative path (no absolute paths in public output)
  const evidenceSourceRef = `artifacts/${baseFilename}`

  // Build actor name enriched with available context
  // Limit to reasonable length to avoid producer validation issues
  let actorName = 'claude-code-hook'
  if (agentId) {
    actorName = `claude-code-subagent-${agentId.slice(0, 8)}`
  } else if (toolName) {
    // Truncate tool name to avoid excessively long actor names
    const safeToolName = toolName.toLowerCase().replace(/[^a-z0-9-]/g, '-').slice(0, 32)
    actorName = `claude-code-hook-${safeToolName}`
  }

  // Build producer CLI arguments via exported helper (testable separately)
  const producerArgs = buildProducerArgs({
    producerScript: PRODUCER_SCRIPT,
    repository: REPOSITORY,
    phaseInfo,
    phaseInstanceId,
    actorType: 'ai_agent',
    actorName,
    evidenceSourceKind: 'artifact',
    evidenceSourceRef,
    evidenceVisibility: 'private_artifact',
    sessionId,
    resolvedIssueNumber,
  })

  let manifestJson
  try {
    // Invoke producer and capture stdout (manifest JSON)
    // stdout is captured but NOT forwarded to our process stdout
    manifestJson = execFileSync(process.execPath, producerArgs, {
      encoding: 'utf8',
      stdio: ['pipe', 'pipe', 'pipe'],
    })
  } catch (err) {
    // Best-effort: producer failure does NOT block the session
    process.stderr.write(
      `[generate_session_manifest_from_hook] warn: producer failed (best-effort, continuing): ${sanitizeForStderr(err.message)}\n`,
    )
    releaseLock(lockPath)
    process.exit(0)
  }

  // Compute content hash for storage as metadata (not used in filename)
  const contentHash = sha256(manifestJson)

  // Atomic write: write to temp file, then rename to final filename
  const finalPath = join(ARTIFACTS_DIR, baseFilename)
  const tmpPath = join(ARTIFACTS_DIR, `.tmp-${randomUUID()}`)

  try {
    writeFileSync(tmpPath, manifestJson, { encoding: 'utf8', flag: 'wx' })
    renameSync(tmpPath, finalPath)
    process.stderr.write(
      `[generate_session_manifest_from_hook] info: artifact written (event=${hookEventName}, sha256=${contentHash.slice(0, 16)})\n`,
    )
  } catch (err) {
    // Best-effort: artifact write failure does NOT block the session
    process.stderr.write(
      `[generate_session_manifest_from_hook] warn: artifact write failed (best-effort, continuing): ${sanitizeForStderr(err.message)}\n`,
    )
    // Attempt cleanup of temp file (ignore errors)
    try {
      renameSync(tmpPath, `${tmpPath}.failed`)
    } catch {
      // ignore cleanup error
    }
    releaseLock(lockPath)
    process.exit(0)
  }

  // Release lock after successful artifact write
  releaseLock(lockPath)

  // stdout remains empty (no manifest content on stdout)
  process.exit(0)
}

// Run main() only when executed directly (not imported for unit testing)
if (fileURLToPath(import.meta.url) === process.argv[1]) {
  main().catch((err) => {
    // Best-effort: uncaught error does NOT block the session
    process.stderr.write(
      `[generate_session_manifest_from_hook] warn: fatal (best-effort, continuing): ${sanitizeForStderr(err.message)}\n`,
    )
    // Release any lock we may be holding
    if (_activeLockPath) {
      releaseLock(_activeLockPath)
      _activeLockPath = null
    }
    process.exit(0)
  })
}
