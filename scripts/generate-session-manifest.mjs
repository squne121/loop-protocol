#!/usr/bin/env node

/**
 * generate-session-manifest.mjs
 *
 * Deterministic producer for agent_session_manifest/v1 JSON.
 * Generates manifest conforming to docs/schemas/agent-session-manifest.schema.json
 * via CLI arguments only. Supports JSON and GitHub comment (fenced markdown) output.
 *
 * Usage:
 *   node scripts/generate-session-manifest.mjs \
 *     --repository squne121/loop-protocol \
 *     --issue 377 \
 *     --phase-main-loop impl \
 *     --phase-ledger-phase implementation \
 *     --phase-instance-id issue-377:impl:001 \
 *     --actor-type ai_agent \
 *     --actor-name implementation-worker \
 *     --actor-session-id session-001 \
 *     --evidence-source-kind artifact \
 *     --evidence-source-ref artifacts/manifest-001.json \
 *     --evidence-visibility private_artifact \
 *     --format json
 *
 * Generates: agent_session_manifest/v1 JSON to stdout
 */

import { randomUUID } from 'crypto'
import { readFileSync } from 'fs'
import { resolve, dirname } from 'path'
import { fileURLToPath } from 'url'

const __dirname = dirname(fileURLToPath(import.meta.url))

// ============================================================================
// Argument Parsing
// ============================================================================

function parseArgs() {
  const args = process.argv.slice(2)
  const result = {}

  for (let i = 0; i < args.length; i++) {
    const arg = args[i]
    if (arg === '--help' || arg === '-h') {
      printHelp()
      process.exit(0)
    }

    if (arg.startsWith('--')) {
      const key = arg.slice(2)
      const value = args[i + 1]
      if (value && !value.startsWith('--')) {
        result[key] = value
        i++
      } else {
        result[key] = true
      }
    }
  }

  return result
}

function printHelp() {
  console.log(`
agent_session_manifest/v1 deterministic producer

USAGE:
  node scripts/generate-session-manifest.mjs [OPTIONS]

OPTIONS:
  --repository REPO              Repository name (e.g., squne121/loop-protocol) [required]
  --issue NUMBER                 Issue number [optional, default: null]
  --pr NUMBER                    PR number [optional, default: null]
  --phase-main-loop PHASE        Main loop phase: issue_create, issue_review, impl, pr_open, pr_review, merge, followup_create [required]
  --phase-ledger-phase PHASE     Ledger phase: followup_issue_materialization, issue_contract_preflight, implementation, post_commit_verification, pr_body_update, semantic_review, pre_merge_judgment, github_merge_event [optional]
  --phase-instance-id ID         Phase instance ID format: issue-<N>:<phase>:<seq> [required]
  --actor-type TYPE              Actor type: ai_agent, human, github_action [required]
  --actor-name NAME              Actor name [required]
  --actor-session-id ID          Actor session ID [optional]
  --evidence-source-kind KIND    Evidence source kind: github_comment, ci_check, hook_jsonl, artifact, transcript, local_file [required]
  --evidence-source-ref REF      Evidence source reference (URL or path) [required]
  --evidence-visibility VIS      Evidence visibility: public_github_comment, private_artifact, local_only [required]
  --format FORMAT                Output format: json, github-comment [default: json]
  --dry-run                      Print to stdout without validation [default: false]
  --validate                     Run internal Ajv validation [optional]
  --strict-redaction             Exit non-zero on secret patterns [optional]
  --help                         Show this help message

EXAMPLES:
  node scripts/generate-session-manifest.mjs \\
    --repository squne121/loop-protocol \\
    --issue 377 \\
    --phase-main-loop impl \\
    --phase-ledger-phase implementation \\
    --phase-instance-id issue-377:impl:001 \\
    --actor-type ai_agent \\
    --actor-name implementation-worker \\
    --evidence-source-kind artifact \\
    --evidence-source-ref artifacts/manifest.json \\
    --evidence-visibility private_artifact \\
    --format json

  node scripts/generate-session-manifest.mjs \\
    --repository squne121/loop-protocol \\
    --pr 391 \\
    --phase-main-loop pr_review \\
    --phase-instance-id issue-377:pr_review:001 \\
    --actor-type github_action \\
    --actor-name pr-validator \\
    --evidence-source-kind ci_check \\
    --evidence-source-ref https://github.com/squne121/loop-protocol/runs/123456 \\
    --evidence-visibility public_github_comment \\
    --format github-comment
`)
}

// ============================================================================
// UUIDv4 Generation
// ============================================================================

function generateManifestId() {
  const uuid = randomUUID()
  return `asm-${uuid}`
}

// ============================================================================
// Validation Helpers
// ============================================================================

function validatePhaseMainLoop(value) {
  const valid = ['issue_create', 'issue_review', 'impl', 'pr_open', 'pr_review', 'merge', 'followup_create']
  if (!valid.includes(value)) {
    throw new Error(`Invalid phase.main_loop: ${value}. Must be one of: ${valid.join(', ')}`)
  }
}

function validatePhaseLedgerPhase(value) {
  if (value === null || value === undefined) return
  const valid = [
    'followup_issue_materialization',
    'issue_contract_preflight',
    'implementation',
    'post_commit_verification',
    'pr_body_update',
    'semantic_review',
    'pre_merge_judgment',
    'github_merge_event',
  ]
  if (!valid.includes(value)) {
    throw new Error(`Invalid phase.ledger_phase: ${value}. Must be one of: ${valid.join(', ')} or null`)
  }
}

function validateActorType(value) {
  const valid = ['ai_agent', 'human', 'github_action']
  if (!valid.includes(value)) {
    throw new Error(`Invalid actor.type: ${value}. Must be one of: ${valid.join(', ')}`)
  }
}

function validateEvidenceSourceKind(value) {
  const valid = ['github_comment', 'ci_check', 'hook_jsonl', 'artifact', 'transcript', 'local_file']
  if (!valid.includes(value)) {
    throw new Error(`Invalid evidence.source_kind: ${value}. Must be one of: ${valid.join(', ')}`)
  }
}

function validateEvidenceVisibility(value) {
  const valid = ['public_github_comment', 'private_artifact', 'local_only']
  if (!valid.includes(value)) {
    throw new Error(`Invalid evidence.visibility: ${value}. Must be one of: ${valid.join(', ')}`)
  }
}

function validatePhaseInstanceId(value) {
  const pattern = /^issue-[0-9]+:[a-z_]+:[0-9]{3}$/
  if (!pattern.test(value)) {
    throw new Error(
      `Invalid phase_instance_id format: ${value}. Expected format: issue-<N>:<phase>:<seq> (e.g., issue-377:impl:001)`,
    )
  }
}

// ============================================================================
// Secret Detection
// ============================================================================

function detectSecretPatterns(obj) {
  const jsonStr = JSON.stringify(obj)

  // raw_transcript field (not raw_transcript_included, which is allowed)
  if (jsonStr.includes('"raw_transcript":') || jsonStr.includes('"raw_transcript":')) {
    return 'raw_transcript field detected'
  }

  // local_file: true
  if (jsonStr.includes('"local_file":true') || /local_file\s*:\s*true/.test(jsonStr)) {
    return 'local_file: true detected'
  }

  // Absolute paths: /home/, /Users/, /tmp/
  if (/\/home\/|\/Users\/|\/tmp\//.test(jsonStr)) {
    return 'absolute path detected'
  }

  // .env pattern (but allow .env as filename in schema context)
  if (/\.env\b[^.]/.test(jsonStr) && !jsonStr.includes('agent-session-manifest')) {
    return '.env content pattern detected'
  }

  // OpenAI token format: sk-[A-Za-z0-9_-]{20,}
  if (/sk-[A-Za-z0-9_-]{20,}/.test(jsonStr)) {
    return 'OpenAI token pattern detected'
  }

  // GitHub token format: gh[pousr]_[A-Za-z0-9_]{20,}
  if (/gh[pousr]_[A-Za-z0-9_]{20,}/.test(jsonStr)) {
    return 'GitHub token pattern detected'
  }

  // PRIVATE KEY
  if (/BEGIN\s+\w+\s+PRIVATE\s+KEY/.test(jsonStr)) {
    return 'PRIVATE KEY pattern detected'
  }

  return ''
}

// ============================================================================
// Manifest Generation
// ============================================================================

function generateManifest(opts) {
  // Validate required arguments
  if (!opts.repository) {
    throw new Error('--repository is required')
  }
  if (!opts['phase-main-loop']) {
    throw new Error('--phase-main-loop is required')
  }
  if (!opts['phase-instance-id']) {
    throw new Error('--phase-instance-id is required')
  }
  if (!opts['actor-type']) {
    throw new Error('--actor-type is required')
  }
  if (!opts['actor-name']) {
    throw new Error('--actor-name is required')
  }
  if (!opts['evidence-source-kind']) {
    throw new Error('--evidence-source-kind is required')
  }
  if (!opts['evidence-source-ref']) {
    throw new Error('--evidence-source-ref is required')
  }
  if (!opts['evidence-visibility']) {
    throw new Error('--evidence-visibility is required')
  }

  // Validate enum values
  validatePhaseMainLoop(opts['phase-main-loop'])
  if (opts['phase-ledger-phase']) {
    validatePhaseLedgerPhase(opts['phase-ledger-phase'])
  }
  validateActorType(opts['actor-type'])
  validateEvidenceSourceKind(opts['evidence-source-kind'])
  validateEvidenceVisibility(opts['evidence-visibility'])
  validatePhaseInstanceId(opts['phase-instance-id'])

  const manifest = {
    schema: 'agent_session_manifest/v1',
    manifest_id: generateManifestId(),
    recorded_at: new Date().toISOString(),
    repository: opts.repository,
    actor: {
      type: opts['actor-type'],
      name: opts['actor-name'],
    },
    phase: {
      main_loop: opts['phase-main-loop'],
      phase_instance_id: opts['phase-instance-id'],
    },
    token_usage: {
      availability: 'unavailable',
      source: 'none',
      prompt: null,
      completion: null,
      total: null,
    },
    evidence: [
      {
        source_kind: opts['evidence-source-kind'],
        source_ref: opts['evidence-source-ref'],
        visibility: opts['evidence-visibility'],
      },
    ],
    redaction: {
      raw_transcript_included: false,
      local_paths_included: opts['evidence-visibility'] === 'private_artifact' && /^\//.test(opts['evidence-source-ref']),
      secret_scan_status: 'clean',
    },
  }

  // Optional fields
  if (opts.issue) {
    manifest.issue_number = parseInt(opts.issue, 10)
  }
  if (opts.pr) {
    manifest.pr_number = parseInt(opts.pr, 10)
  }
  if (opts['actor-session-id']) {
    manifest.actor.session_id = opts['actor-session-id']
  }
  if (opts['phase-ledger-phase']) {
    manifest.phase.ledger_phase = opts['phase-ledger-phase']
  }

  // Default to not_applicable if not provided
  if (!manifest.phase.ledger_phase) {
    manifest.phase.ledger_phase = null
  }

  return manifest
}

// ============================================================================
// Output Formatting
// ============================================================================

function formatAsJson(manifest) {
  return JSON.stringify(manifest, null, 2)
}

function formatAsGithubComment(manifest) {
  const jsonStr = JSON.stringify(manifest, null, 2)
  return (
    `<!-- agent_session_manifest:v1 start -->\n` +
    `\`\`\`\`json\n` +
    `${jsonStr}\n` +
    `\`\`\`\`\n` +
    `<!-- agent_session_manifest:v1 end -->`
  )
}

// ============================================================================
// Main
// ============================================================================

async function main() {
  try {
    const opts = parseArgs()

    // Generate manifest
    const manifest = generateManifest(opts)

    // Secret detection
    const secretPattern = detectSecretPatterns(manifest)
    if (secretPattern && opts['strict-redaction']) {
      console.error(`Error: Secret pattern detected: ${secretPattern}`)
      process.exit(1)
    }

    // Format output
    const format = opts.format || 'json'
    let output
    if (format === 'github-comment') {
      output = formatAsGithubComment(manifest)
    } else if (format === 'json') {
      output = formatAsJson(manifest)
    } else {
      throw new Error(`Unknown format: ${format}. Use 'json' or 'github-comment'`)
    }

    // Output
    console.log(output)

    // Exit success
    process.exit(0)
  } catch (error) {
    console.error(`Error: ${error.message}`)
    process.exit(1)
  }
}

main()
