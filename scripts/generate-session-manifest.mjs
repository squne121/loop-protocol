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
import {
  validateManifest,
  detectSecretPatterns,
  detectSecretsInMarkdown,
  validateProducerMetadataSafety,
  validateProducerContractForIssue377,
  PRODUCER_ACTOR_TYPES,
  PRODUCER_EVIDENCE_SOURCE_KINDS,
  PRODUCER_KIND_BY_EVIDENCE_SOURCE,
} from './lib/agent-session-manifest-validation.mjs'

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
        // Support repeated flags (store as array)
        if (key in result) {
          if (Array.isArray(result[key])) {
            result[key].push(value)
          } else {
            result[key] = [result[key], value]
          }
        } else {
          result[key] = value
        }
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

REQUIRED OPTIONS:
  --repository REPO              Repository name (e.g., squne121/loop-protocol)
  --phase-main-loop PHASE        Main loop phase: issue_create, issue_review, impl, pr_open, pr_review, merge, followup_create
  --phase-instance-id ID         Phase instance ID format: issue-<N>:<phase>:<seq>
  --actor-type TYPE              Actor type (producer limited to): ai_agent, github_action
  --actor-name NAME              Actor name
  --evidence-source-kind KIND    Evidence source kind (producer limited to): hook_jsonl, ci_check, artifact
  --evidence-source-ref REF      Evidence source reference (URL or path)
  --evidence-visibility VIS      Evidence visibility: public_github_comment, private_artifact, local_only

OPTIONAL OPTIONS:
  --issue NUMBER                 Issue number (must match ^[1-9][0-9]*$) [default: null]
  --pr NUMBER                    PR number (must match ^[1-9][0-9]*$) [default: null]
  --phase-ledger-phase PHASE     Ledger phase (optional, defaults to null)
  --actor-session-id ID          Actor session ID [optional]
  --format FORMAT                Output format: json, github-comment [default: json]
  --manifest-id UUID             Override manifest_id (asm-<UUIDv4> format) [optional, auto-generated if omitted]
  --recorded-at ISO8601          Override recorded_at timestamp [optional, auto-generated if omitted]
  --validate                     Run schema + semantic validation before output (default: false for json, true for github-comment)
  --no-validate                  Skip all validation (escape hatch)
  --allow-local-path             Allow local absolute paths in output (downgrades to warning, default: fail-closed)
  --strict-redaction             Force redaction scan on all formats [optional]
  --dry-run                      Output to stdout only, no side effects [no-op for script producer]
  --verification-overall STATUS  Overall verification result: pass|fail|partial|n_a [optional]
  --verification-ac-result AC=STATUS  AC verdict (repeatable, format: AC7=pass) [optional]
  --verification-skipped-count N  Verification skipped count [optional]
  --verification-fallback-detected BOOL  Fallback detected flag: true|false [optional]
  --verification-evidence-ref REF  Evidence reference (repeatable) [optional]
  --human-intervention-required BOOL  Human intervention flag: true|false [optional, default: false]
  --human-intervention-reason TEXT  Reason for human intervention [optional]
  --help                         Show this help message

DETERMINISM NOTES:
  Non-deterministic fields (when not overridden):
  - manifest_id: auto-generated UUIDv4 (use --manifest-id to fix)
  - recorded_at: current ISO 8601 timestamp (use --recorded-at to fix)

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
    --format json \\
    --validate

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
    --format github-comment \\
    --validate
`)
}

// ============================================================================
// UUIDv4 Generation
// ============================================================================

function generateManifestId(override) {
  if (override) {
    // Validate format
    const pattern = /^asm-[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/
    if (!pattern.test(override)) {
      throw new Error(`Invalid manifest_id format: ${override}. Expected: asm-<UUIDv4>`)
    }
    return override
  }
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
  // B1 iter2: Producer is limited to PRODUCER_ACTOR_TYPES subset
  if (!PRODUCER_ACTOR_TYPES.includes(value)) {
    throw new Error(`Invalid actor.type for producer: ${value}. Producer is limited to: ${PRODUCER_ACTOR_TYPES.join(', ')}`)
  }
}

function validateEvidenceSourceKind(value) {
  // B1 iter2: Producer is limited to PRODUCER_EVIDENCE_SOURCE_KINDS subset
  if (!PRODUCER_EVIDENCE_SOURCE_KINDS.includes(value)) {
    throw new Error(`Invalid evidence.source_kind for producer: ${value}. Producer is limited to: ${PRODUCER_EVIDENCE_SOURCE_KINDS.join(', ')}`)
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

function validateIssueNumber(value) {
  const pattern = /^[1-9][0-9]*$/
  if (!pattern.test(value)) {
    throw new Error(`Invalid issue number: ${value}. Must match ^[1-9][0-9]*$`)
  }
}

function validatePrNumber(value) {
  const pattern = /^[1-9][0-9]*$/
  if (!pattern.test(value)) {
    throw new Error(`Invalid PR number: ${value}. Must match ^[1-9][0-9]*$`)
  }
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

  // M1: Validate issue/pr numbers if provided
  if (opts.issue) {
    validateIssueNumber(opts.issue)
  }
  if (opts.pr) {
    validatePrNumber(opts.pr)
  }

  const manifest = {
    schema: 'agent_session_manifest/v1',
    manifest_id: generateManifestId(opts['manifest-id']),
    recorded_at: opts['recorded-at'] || new Date().toISOString(),
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
    producer: {
      kind: PRODUCER_KIND_BY_EVIDENCE_SOURCE[opts['evidence-source-kind']],
      version: null,
      command: 'node scripts/generate-session-manifest.mjs',
      source_ref: null,
    },
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

  // Default to null if not provided
  if (!manifest.phase.ledger_phase) {
    manifest.phase.ledger_phase = null
  }

  // M2: Add verification with semantic rules
  if (opts['verification-overall']) {
    const acResults = []
    // Parse repeated --verification-ac-result AC=STATUS
    const acResultEntries = Array.isArray(opts['verification-ac-result'])
      ? opts['verification-ac-result']
      : opts['verification-ac-result']
        ? [opts['verification-ac-result']]
        : []
    for (const entry of acResultEntries) {
      const [ac, verdict] = entry.split('=')
      if (ac && verdict) {
        acResults.push({ ac, verdict })
      }
    }

    manifest.verification = {
      overall: opts['verification-overall'],
      skipped_count: parseInt(opts['verification-skipped-count'] || '0', 10),
      fallback_detected: opts['verification-fallback-detected'] === 'true',
      ac_results: acResults,
    }
  }

  // B7: Add human_intervention
  const humanInterventionRequired = opts['human-intervention-required'] === 'true'
  manifest.human_intervention = {
    required: humanInterventionRequired,
    type: humanInterventionRequired ? 'escalation' : 'none',
    summary: opts['human-intervention-reason'] || null,
  }

  return manifest
}

// ============================================================================
// Backtick Fence Calculation (B6)
// ============================================================================

function calculateFenceLength(manifest) {
  // Find max consecutive backtick run in JSON representation
  const jsonStr = JSON.stringify(manifest, null, 2)
  const backtickMatches = jsonStr.match(/`+/g) || []
  const maxRun = backtickMatches.reduce((max, match) => Math.max(max, match.length), 0)
  // Use max(maxRun + 1, 4) for fence length
  return Math.max(maxRun + 1, 4)
}

// ============================================================================
// Output Formatting
// ============================================================================

function formatAsJson(manifest) {
  return JSON.stringify(manifest, null, 2)
}

function formatAsGithubComment(manifest) {
  const jsonStr = JSON.stringify(manifest, null, 2)
  const fenceLength = calculateFenceLength(manifest)
  const fence = '`'.repeat(fenceLength)
  return (
    `<!-- agent_session_manifest:v1 start -->\n` +
    `${fence}json\n` +
    `${jsonStr}\n` +
    `${fence}\n` +
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

    const format = opts.format || 'json'

    // M1 iter2: Run validation by default (unless --no-validate is set)
    // Validation is mandatory for github-comment format
    const skipValidation = opts['no-validate'] === true
    const shouldValidate = !skipValidation

    if (shouldValidate) {
      // B3 iter2: Run both schema and semantic validation
      const validationResult = validateManifest(manifest)
      if (!validationResult.valid) {
        console.error('Validation failed:')
        for (const error of validationResult.errors) {
          console.error(`  ${error.path}: ${error.message}`)
        }
        process.exit(1)
      }

      // B1 iter2: Validate producer contract (subset enforcement)
      const producerContractResult = validateProducerContractForIssue377(manifest)
      if (!producerContractResult.valid) {
        console.error('Producer contract validation failed:')
        for (const error of producerContractResult.errors) {
          console.error(`  ${error.path}: ${error.message}`)
        }
        process.exit(1)
      }
    }

    // B2 iter2: Secret detection - fail-closed by default (default behavior unless --allow-local-path)
    const allowLocalPath = opts['allow-local-path'] === true
    const secretPattern = detectSecretPatterns(manifest)
    if (secretPattern) {
      const producerMetadataSafety = validateProducerMetadataSafety(manifest)
      const absolutePathOnly = secretPattern === 'absolute path detected' && producerMetadataSafety.valid
      if (allowLocalPath && absolutePathOnly) {
        // Downgrade only for local path usage; token/private-key patterns remain fail-closed.
        console.warn(`Warning: Secret pattern detected (allowed by --allow-local-path): ${secretPattern}`)
      } else {
        // Fail-closed (default)
        console.error(`Error: Secret pattern detected: ${secretPattern}. Use --allow-local-path to allow local paths.`)
        process.exit(1)
      }
    }

    // Format output
    let output
    if (format === 'github-comment') {
      output = formatAsGithubComment(manifest)

      // B2 iter2: Scan markdown output for secrets as well (fail-closed by default)
      const markdownSecrets = detectSecretsInMarkdown(output)
      if (markdownSecrets) {
        const absolutePathOnly = markdownSecrets === 'absolute path detected in markdown'
        if (allowLocalPath && absolutePathOnly) {
          console.warn(`Warning: Secret pattern detected in output (allowed by --allow-local-path): ${markdownSecrets}`)
        } else {
          console.error(`Error: Secret pattern detected in output: ${markdownSecrets}. Use --allow-local-path to allow local paths.`)
          process.exit(1)
        }
      }
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
