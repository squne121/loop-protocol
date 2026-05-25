#!/usr/bin/env node

/**
 * agent-session-manifest-validation.mjs
 *
 * Common validation module for agent_session_manifest/v1.
 * Provides both JSON Schema validation (Ajv 2020-12) and semantic validation.
 *
 * Exports:
 *   validateManifest(json) → {valid, errors[]}
 *   detectSecretPatterns(obj) → string (error description or empty string)
 */

import { readFileSync } from 'fs'
import { resolve, dirname } from 'path'
import { fileURLToPath } from 'url'

const __dirname = dirname(fileURLToPath(import.meta.url))

// ============================================================================
// Ajv Setup
// ============================================================================

let Ajv2020, ajvFormats

try {
  const ajv2020Module = await import('ajv/dist/2020.js')
  Ajv2020 = ajv2020Module.default
  const formatsModule = await import('ajv-formats')
  ajvFormats = formatsModule.default
} catch (err) {
  console.error('Error: ajv and ajv-formats must be installed as devDependencies')
  process.exit(1)
}

// ============================================================================
// Schema Loading
// ============================================================================

function loadSchema() {
  const schemaPath = resolve(__dirname, '../../docs/schemas/agent-session-manifest.schema.json')
  try {
    const schemaContent = readFileSync(schemaPath, 'utf-8')
    const schema = JSON.parse(schemaContent)
    // Keep $schema for draft-2020-12 validation
    return schema
  } catch (err) {
    const errorMsg = err instanceof Error ? err.message : String(err)
    throw new Error(`Failed to load schema from ${schemaPath}: ${errorMsg}`, { cause: err })
  }
}

// ============================================================================
// Producer Contract Constants (B1 iter2)
// ============================================================================

// Producer is restricted to these actor types (validator allows all in schema)
export const PRODUCER_ACTOR_TYPES = ['ai_agent', 'github_action']

// Producer is restricted to these evidence source kinds (validator allows all in schema)
export const PRODUCER_EVIDENCE_SOURCE_KINDS = ['hook_jsonl', 'ci_check', 'artifact']

export const PRODUCER_KIND_BY_EVIDENCE_SOURCE = {
  artifact: 'script_generated',
  hook_jsonl: 'hook_generated',
  ci_check: 'github_action_generated',
}

// ============================================================================
// Semantic Validation
// ============================================================================

function validateSemantics(manifest) {
  const errors = []

  // B4: token_usage.availability === "unavailable" semantics
  if (manifest.token_usage?.availability === 'unavailable') {
    if (manifest.token_usage.source !== 'none') {
      errors.push({
        path: 'token_usage.source',
        message: 'When availability is "unavailable", source must be "none"',
      })
    }
    if (manifest.token_usage.prompt !== null) {
      errors.push({
        path: 'token_usage.prompt',
        message: 'When availability is "unavailable", prompt must be null (not 0)',
      })
    }
    if (manifest.token_usage.completion !== null) {
      errors.push({
        path: 'token_usage.completion',
        message: 'When availability is "unavailable", completion must be null (not 0)',
      })
    }
    if (manifest.token_usage.total !== null) {
      errors.push({
        path: 'token_usage.total',
        message: 'When availability is "unavailable", total must be null (not 0)',
      })
    }
  }

  // B3: Evidence visibility constraints
  // If visibility is public_github_comment, source_kind must not be transcript or local_file
  for (let i = 0; i < (manifest.evidence || []).length; i++) {
    const evidence = manifest.evidence[i]
    if (evidence.visibility === 'public_github_comment') {
      if (evidence.source_kind === 'transcript' || evidence.source_kind === 'local_file') {
        errors.push({
          path: `evidence[${i}].source_kind`,
          message: `When visibility is "public_github_comment", source_kind cannot be "${evidence.source_kind}"`,
        })
      }
    }
  }

  // actor.type constraints: must be ai_agent or github_action (human not allowed in this implementation)
  if (manifest.actor?.type === 'human') {
    errors.push({
      path: 'actor.type',
      message: 'In current implementation scope, actor.type must be "ai_agent" or "github_action" (human not allowed)',
    })
  }

  return errors
}

// ============================================================================
// Schema-Only Validation (for validator script)
// ============================================================================

export function validateManifestAgainstSchema(json) {
  try {
    const schema = loadSchema()

    const ajv = new Ajv2020({
      strict: true,
      allErrors: true,
    })
    ajvFormats(ajv)

    let validate
    try {
      validate = ajv.compile(schema)
    } catch (err) {
      return {
        valid: false,
        errors: [
          {
            path: 'schema',
            message: `Schema compilation error: ${err.message}`,
          },
        ],
      }
    }

    const schemaValid = validate(json)
    const schemaErrors = !schemaValid ? (validate.errors || []) : []

    return {
      valid: schemaErrors.length === 0,
      errors: schemaErrors.map((err) => ({
        path: err.instancePath || 'root',
        message: err.message,
      })),
    }
  } catch (err) {
    return {
      valid: false,
      errors: [
        {
          path: 'validation',
          message: `Unexpected validation error: ${err.message}`,
        },
      ],
    }
  }
}

// ============================================================================
// Semantic-Only Validation
// ============================================================================

export function validateManifestSemantics(json) {
  try {
    const semanticErrors = validateSemantics(json)
    return {
      valid: semanticErrors.length === 0,
      errors: semanticErrors,
    }
  } catch (err) {
    return {
      valid: false,
      errors: [
        {
          path: 'semantics',
          message: `Unexpected semantic validation error: ${err.message}`,
        },
      ],
    }
  }
}

// ============================================================================
// Producer Contract Validation (for producer script only)
// ============================================================================

export function validateProducerContractForIssue377(manifest, opts = {}) {
  const errors = []

  // B1 iter2: actor.type must be in PRODUCER_ACTOR_TYPES
  if (manifest.actor?.type && !PRODUCER_ACTOR_TYPES.includes(manifest.actor.type)) {
    errors.push({
      path: 'actor.type',
      message: `actor.type must be one of: ${PRODUCER_ACTOR_TYPES.join(', ')}. Got: ${manifest.actor.type}`,
    })
  }

  // B1 iter2: all evidence.source_kind must be in PRODUCER_EVIDENCE_SOURCE_KINDS
  for (let i = 0; i < (manifest.evidence || []).length; i++) {
    const evidence = manifest.evidence[i]
    if (evidence.source_kind && !PRODUCER_EVIDENCE_SOURCE_KINDS.includes(evidence.source_kind)) {
      errors.push({
        path: `evidence[${i}].source_kind`,
        message: `evidence.source_kind must be one of: ${PRODUCER_EVIDENCE_SOURCE_KINDS.join(', ')}. Got: ${evidence.source_kind}`,
      })
    }
  }

  const primaryEvidence = manifest.evidence?.[0]
  if (manifest.producer) {
    const expectedKind = primaryEvidence ? PRODUCER_KIND_BY_EVIDENCE_SOURCE[primaryEvidence.source_kind] : undefined
    if (!manifest.producer.kind) {
      errors.push({
        path: 'producer.kind',
        message: 'producer.kind is required when producer is present',
      })
    } else if (expectedKind && manifest.producer.kind !== expectedKind) {
      errors.push({
        path: 'producer.kind',
        message: `producer.kind must match evidence.source_kind mapping. Expected: ${expectedKind}. Got: ${manifest.producer.kind}`,
      })
    }
  }

  // M2 iter2: verification semantic rules
  if (manifest.verification?.skipped_count && manifest.verification.skipped_count > 0 && manifest.verification.overall === 'pass') {
    errors.push({
      path: 'verification',
      message: 'verification.skipped_count > 0 is incoherent with verification.overall === "pass"',
    })
  }

  if (manifest.verification?.fallback_detected === true && manifest.verification.overall === 'pass') {
    errors.push({
      path: 'verification',
      message: 'verification.fallback_detected === true is incoherent with verification.overall === "pass" (PR #314 schema description)',
    })
  }

  return {
    valid: errors.length === 0,
    errors,
  }
}

// ============================================================================
// Secret Detection
// ============================================================================

export function detectSecretPatterns(obj) {
  const jsonStr = JSON.stringify(obj)

  // raw_transcript field (not raw_transcript_included)
  if (/"raw_transcript"\s*:\s*["{}]/.test(jsonStr)) {
    return 'raw_transcript field detected'
  }

  // local_file: true
  if (jsonStr.includes('"local_file":true')) {
    return 'local_file: true detected'
  }

  // Absolute paths: /home/, /Users/, /tmp/
  if (/\/home\/|\/Users\/|\/tmp\//.test(jsonStr)) {
    return 'absolute path detected'
  }

  // .env pattern (but allow as filename in schema context)
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
// Fenced Markdown Secret Detection
// ============================================================================

export function detectSecretsInMarkdown(markdown) {
  // Same patterns as detectSecretPatterns, but applied to the markdown string directly
  if (/\/home\/|\/Users\/|\/tmp\//.test(markdown)) {
    return 'absolute path detected in markdown'
  }

  if (/sk-[A-Za-z0-9_-]{20,}/.test(markdown)) {
    return 'OpenAI token pattern detected in markdown'
  }

  if (/gh[pousr]_[A-Za-z0-9_]{20,}/.test(markdown)) {
    return 'GitHub token pattern detected in markdown'
  }

  if (/BEGIN\s+\w+\s+PRIVATE\s+KEY/.test(markdown)) {
    return 'PRIVATE KEY pattern detected in markdown'
  }

  return ''
}

// ============================================================================
// Main Validation Function (combines schema + semantic for backward compat)
// ============================================================================

export function validateManifest(json) {
  const schemaResult = validateManifestAgainstSchema(json)
  const semanticResult = validateManifestSemantics(json)

  const allErrors = [...schemaResult.errors, ...semanticResult.errors]

  return {
    valid: allErrors.length === 0,
    errors: allErrors,
  }
}

export default {
  validateManifest,
  validateManifestAgainstSchema,
  validateManifestSemantics,
  validateProducerContractForIssue377,
  detectSecretPatterns,
  detectSecretsInMarkdown,
  PRODUCER_ACTOR_TYPES,
  PRODUCER_EVIDENCE_SOURCE_KINDS,
  PRODUCER_KIND_BY_EVIDENCE_SOURCE,
}
