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
import { Buffer } from 'node:buffer'
import {
  existsSync,
  mkdirSync,
  readdirSync,
  renameSync,
  writeFileSync,
} from 'node:fs'
import { join, resolve, dirname } from 'node:path'
import { fileURLToPath } from 'node:url'
import { execFileSync } from 'node:child_process'

const __dirname = dirname(fileURLToPath(import.meta.url))

// ============================================================================
// Configuration
// ============================================================================

const REPO_ROOT = resolve(__dirname, '..', '..')
const PRODUCER_SCRIPT = join(REPO_ROOT, 'scripts', 'generate-session-manifest.mjs')
const ARTIFACTS_DIR = join(REPO_ROOT, 'artifacts')
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
  return String(msg).replace(/\/[^\s"']+/g, '<path>')
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
 * Compute SHA-256 payload digest for throttle deduplication.
 * Returns first 16 hex chars of sha256(serializedPayload).
 * Empty string if payload is null/undefined.
 */
function computePayloadDigest(payload) {
  if (payload == null) return 'nullpayload'
  try {
    const serialized = JSON.stringify(payload, Object.keys(payload).sort())
    return sha256(serialized).slice(0, 16)
  } catch {
    return 'digestfail'
  }
}

/**
 * Build a URL-safe stable key segment from event info.
 * Key format: {hookEventName}:{sessionId}:{toolName}:{phase}:{payloadDigest}
 * Encoded as URL-safe base64 for use in filename.
 */
function buildStableKey(hookEventName, sessionId, toolName, ledgerPhase, payloadDigest) {
  const rawKey = `${hookEventName}:${sessionId || 'nosession'}:${toolName || ''}:${ledgerPhase}:${payloadDigest || 'nodigest'}`
  return Buffer.from(rawKey).toString('base64url').slice(0, 40)
}

/**
 * Check if a duplicate artifact exists by scanning for stable key segment in filenames.
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
// Main
// ============================================================================

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

  // Build stable duplicate key (not timestamp-dependent, includes payload_digest)
  const stableKeySegment = buildStableKey(hookEventName, sessionId, toolName, phaseInfo.ledgerPhase, payloadDigest)

  // Check for duplicate before doing any work
  if (hasDuplicateArtifact(stableKeySegment)) {
    process.stderr.write(
      `[generate_session_manifest_from_hook] info: duplicate skip (key=${stableKeySegment}, event=${hookEventName})\n`,
    )
    process.exit(0)
  }

  // Build sequence ID: must be 3-digit zero-padded number (producer validates /^[0-9]{3}$/)
  // Use last 3 digits of epoch seconds to get a stable-ish 3-digit number
  const seqNum = String(Math.floor(Date.now() / 1000) % 1000).padStart(3, '0')
  const phaseInstanceId = `issue-402:${phaseInfo.mainLoop}:${seqNum}`

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

  // Build producer CLI arguments
  // Only pass arguments the producer CLI accepts (see --help output)
  // PostToolUse tool_name / tool_use_id / SubagentStop agent_id are not
  // producer CLI options — they are encoded in actor-name and phase-instance-id
  const producerArgs = [
    PRODUCER_SCRIPT,
    '--repository',
    REPOSITORY,
    '--phase-main-loop',
    phaseInfo.mainLoop,
    '--phase-ledger-phase',
    phaseInfo.ledgerPhase,
    '--phase-instance-id',
    phaseInstanceId,
    '--actor-type',
    'ai_agent',
    '--actor-name',
    actorName,
    '--evidence-source-kind',
    'artifact',
    '--evidence-source-ref',
    evidenceSourceRef,
    '--evidence-visibility',
    'private_artifact',
    '--format',
    'json',
    '--validate',
  ]

  // Add session ID as actor-session-id if available
  if (sessionId) {
    producerArgs.push('--actor-session-id', sessionId)
  }

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
    process.exit(0)
  }

  // Compute content hash for storage as metadata (not used in filename)
  const contentHash = sha256(manifestJson)

  // Atomic write: write to temp file, then rename to final filename
  const finalPath = join(ARTIFACTS_DIR, baseFilename)
  const tmpPath = join(ARTIFACTS_DIR, `.tmp-${randomUUID()}`)

  try {
    ensureArtifactsDir()
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
    process.exit(0)
  }

  // stdout remains empty (no manifest content on stdout)
  process.exit(0)
}

main().catch((err) => {
  // Best-effort: uncaught error does NOT block the session
  process.stderr.write(
    `[generate_session_manifest_from_hook] warn: fatal (best-effort, continuing): ${sanitizeForStderr(err.message)}\n`,
  )
  process.exit(0)
})
