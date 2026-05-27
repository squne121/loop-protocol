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
 * Guarantees:
 * - stdout is SILENT (no manifest JSON on stdout)
 * - transcript_path / cwd absolute paths are NOT included in any public output
 * - artifact file write is atomic (temp + rename)
 * - duplicate sessions (same content hash) are skipped
 *
 * stdin: Claude hook context JSON (varies by event type)
 * stdout: empty (silent)
 * stderr: diagnostic messages only (no manifest content, no absolute paths)
 * exit 0: success or duplicate skip
 * exit 1: error (logged to stderr without sensitive path info)
 */

import { createHash, randomUUID } from 'node:crypto'
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
 * Compute SHA-256 hash of a string for duplicate detection.
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

// ============================================================================
// Main
// ============================================================================

async function main() {
  // Read hook context from stdin
  const hookCtx = await readStdin()

  if (!hookCtx) {
    process.stderr.write('[generate_session_manifest_from_hook] warn: no stdin context\n')
  }

  // Determine event type from hook context
  const eventType = hookCtx?.hook_event_name ?? hookCtx?.type ?? 'Stop'

  // Map event to phase info
  const phaseInfo = EVENT_PHASE_MAP[eventType] ?? EVENT_PHASE_MAP['Stop']

  // Build a short timestamp suffix for sequence ID (base-36 last 6 chars)
  const seqTs = Date.now().toString(36).slice(-6)
  const phaseInstanceId = `issue-402:${phaseInfo.mainLoop}:${seqTs}`

  // Build initial artifact filename (without hash; hash added after generation)
  const timestamp = new Date().toISOString().replace(/[:.]/g, '-').slice(0, 19)
  const baseFilename = `private-agent-session-manifest-${eventType.toLowerCase()}-${timestamp}`

  // Evidence source ref uses relative path (no absolute paths in public output)
  const evidenceSourceRef = `artifacts/${baseFilename}.json`

  // Build producer CLI arguments — no hookCtx fields with absolute paths
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
    'claude-code-hook',
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

  let manifestJson
  try {
    // Invoke producer and capture stdout (manifest JSON)
    // stdout is captured but NOT forwarded to our process stdout
    manifestJson = execFileSync(process.execPath, producerArgs, {
      encoding: 'utf8',
      stdio: ['pipe', 'pipe', 'pipe'],
    })
  } catch (err) {
    process.stderr.write(
      `[generate_session_manifest_from_hook] error: producer failed: ${sanitizeForStderr(err.message)}\n`,
    )
    process.exit(1)
  }

  // Duplicate detection via content hash
  const contentHash = sha256(manifestJson)
  const contentHashShort = contentHash.slice(0, 16)

  // Check for existing artifact with same hash prefix
  try {
    ensureArtifactsDir()
    const existingFiles = readdirSync(ARTIFACTS_DIR)
    const hasDuplicate = existingFiles.some((f) => f.includes(contentHashShort))
    if (hasDuplicate) {
      process.stderr.write(
        `[generate_session_manifest_from_hook] info: duplicate skip (hash=${contentHashShort})\n`,
      )
      process.exit(0)
    }
  } catch (err) {
    process.stderr.write(
      `[generate_session_manifest_from_hook] warn: duplicate check failed: ${sanitizeForStderr(err.message)}\n`,
    )
    // Proceed with write
  }

  // Final artifact filename includes hash for duplicate detection
  const finalFilename = `${baseFilename}-${contentHashShort}.json`
  const finalPath = join(ARTIFACTS_DIR, finalFilename)
  const tmpPath = join(ARTIFACTS_DIR, `.tmp-${randomUUID()}`)

  // Atomic write: write to temp file, then rename
  try {
    ensureArtifactsDir()
    writeFileSync(tmpPath, manifestJson, { encoding: 'utf8', flag: 'wx' })
    renameSync(tmpPath, finalPath)
    process.stderr.write(
      `[generate_session_manifest_from_hook] info: artifact written (event=${eventType})\n`,
    )
  } catch (err) {
    process.stderr.write(
      `[generate_session_manifest_from_hook] error: artifact write failed: ${sanitizeForStderr(err.message)}\n`,
    )
    // Attempt cleanup of temp file
    try {
      renameSync(tmpPath, `${tmpPath}.failed`)
    } catch {
      // ignore cleanup error
    }
    process.exit(1)
  }

  // stdout remains empty (no manifest content on stdout)
  process.exit(0)
}

main().catch((err) => {
  process.stderr.write(
    `[generate_session_manifest_from_hook] fatal: ${sanitizeForStderr(err.message)}\n`,
  )
  process.exit(1)
})
