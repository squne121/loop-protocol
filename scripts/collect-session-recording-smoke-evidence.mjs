#!/usr/bin/env node

/**
 * collect-session-recording-smoke-evidence.mjs
 *
 * Collects evidence for session_recording_smoke_verdict/v1 from:
 *   - GitHub Issue / PR comments (agent_session_manifest markdown fixtures)
 *   - generated artifacts (agent_session_manifest JSON)
 *   - manifest comments
 *   - negative control fixtures
 *
 * Legacy evidence exclusion (Issue #1312 AC5):
 *   agent_session_manifest entries that lack the `secret_policy` field
 *   (produced before secret_policy was made mandatory) are classified as
 *   "legacy" and excluded from authoritative_count. They are NOT treated
 *   as invalid — they are soft-excluded.
 *
 * Authoritative generator classification (Issue #1312 AC6):
 *   generate-session-manifest.mjs output is only treated as authoritative
 *   when BOTH --manifest-id and --recorded-at were explicitly supplied to
 *   the generator invocation (default UUID / current-time generation is
 *   non-deterministic and MUST NOT be counted as authoritative).
 *
 * Negative control handling (Issue #1312, In Scope):
 *   entries that fail full agent_session_manifest schema + semantic
 *   validation for reasons OTHER than a missing secret_policy field
 *   (e.g. public_github_comment visibility combined with source_kind
 *   transcript) are classified as "invalid". In --strict mode (default)
 *   the CLI exits non-zero when any invalid entry is present.
 *
 * Usage:
 *   node scripts/collect-session-recording-smoke-evidence.mjs --evidence-input <file.json> [--out <file.json>] [--no-strict]
 *   node scripts/collect-session-recording-smoke-evidence.mjs --help
 *
 * Evidence input format (JSON array), each entry:
 *   {
 *     "source_kind": "agent_session_manifest_script" | "agent_session_manifest_generic"
 *                    | "artifact" | "github_comment" | "negative_control",
 *     "source_ref": "<url-or-path>",
 *     "manifest": { ... agent_session_manifest/v1 JSON ... },
 *     "generation_argv": ["--manifest-id", "asm-...", "--recorded-at", "2026-..."]  (optional)
 *   }
 *
 * Exit codes:
 *   0: collection succeeded (no invalid entries, or --no-strict)
 *   1: --strict (default) and at least one entry failed validation for a
 *      reason other than missing secret_policy
 *   2: usage / input error
 */

import { readFileSync, writeFileSync } from 'fs'
import { resolve, dirname } from 'path'
import { fileURLToPath } from 'url'

import { validateManifest } from './lib/agent-session-manifest-validation.mjs'

const __dirname = dirname(fileURLToPath(import.meta.url))

// ============================================================================
// Argument Parsing
// ============================================================================

function parseArgs(argv) {
  const result = { strict: true }
  for (let i = 0; i < argv.length; i++) {
    const arg = argv[i]
    if (arg === '--help' || arg === '-h') {
      result.help = true
      continue
    }
    if (arg === '--no-strict') {
      result.strict = false
      continue
    }
    if (arg === '--strict') {
      result.strict = true
      continue
    }
    if (arg === '--evidence-input') {
      result.evidenceInput = argv[++i]
      continue
    }
    if (arg === '--out') {
      result.out = argv[++i]
      continue
    }
  }
  return result
}

function printHelp() {
  console.log(`
collect-session-recording-smoke-evidence.mjs

Collects and classifies evidence sources for session_recording_smoke_verdict/v1
(Issue #1312, follow-up of Issue #246).

USAGE:
  node scripts/collect-session-recording-smoke-evidence.mjs --evidence-input <file.json> [OPTIONS]

OPTIONS:
  --evidence-input FILE   JSON array of evidence source entries (required unless --help)
  --out FILE              Write the resulting collection summary as JSON to FILE (optional)
  --strict                Fail (exit 1) if any entry is classified as invalid (default)
  --no-strict             Do not fail on invalid entries; report only
  --help, -h              Show this help message

Evidence entry shape:
  {
    "source_kind": "agent_session_manifest_script" | "agent_session_manifest_generic"
                   | "artifact" | "github_comment" | "negative_control",
    "source_ref": "<url-or-path>",
    "manifest": { ...agent_session_manifest/v1 JSON... },
    "generation_argv": ["--manifest-id", "asm-...", "--recorded-at", "2026-..."]
  }

Classification rules:
  - Entries whose manifest lacks "secret_policy" are legacy (excluded from
    authoritative_count, not treated as invalid).
  - source_kind "agent_session_manifest_script" entries are authoritative
    only when generation_argv contains BOTH --manifest-id and --recorded-at.
  - Entries that fail agent_session_manifest schema/semantic validation for
    reasons other than a missing secret_policy field are invalid.
`)
}

// ============================================================================
// Classification
// ============================================================================

/**
 * Return true if a manifest lacks the secret_policy field entirely
 * (legacy evidence produced before secret_policy was mandatory).
 */
export function isLegacyManifest(manifest) {
  if (!manifest || typeof manifest !== 'object') return false
  return !Object.prototype.hasOwnProperty.call(manifest, 'secret_policy')
}

/**
 * Return true if a generate-session-manifest.mjs invocation explicitly
 * supplied BOTH --manifest-id and --recorded-at (Issue #1312 AC6).
 */
export function isAuthoritativeGeneratedManifest(generationArgv) {
  if (!Array.isArray(generationArgv)) return false
  return generationArgv.includes('--manifest-id') && generationArgv.includes('--recorded-at')
}

/**
 * Classify a single evidence entry.
 *
 * Returns:
 *   {
 *     ok: boolean,               // false only for genuinely invalid entries
 *     legacy: boolean,           // true when secret_policy is absent
 *     authoritative: boolean,    // counted in authoritative_count
 *     errors: [{path, message}]  // validation errors (only when !ok)
 *   }
 */
export function classifyEvidenceEntry(entry) {
  const sourceKind = entry?.source_kind
  const manifest = entry?.manifest

  // Non-manifest evidence sources (raw github_comment / artifact refs without
  // a structured manifest payload) are treated as authoritative-by-default
  // supporting evidence: they are not schema-validated here.
  if (!manifest) {
    return { ok: true, legacy: false, authoritative: true, errors: [] }
  }

  const legacy = isLegacyManifest(manifest)

  const validation = validateManifest(manifest)
  const nonSecretPolicyErrors = validation.errors.filter((err) => {
    const path = String(err.path || '')
    const message = String(err.message || '')
    return !path.includes('secret_policy') && !message.includes('secret_policy')
  })

  // If the ONLY validation failures are secret_policy related, treat as legacy
  // (soft-exclude) rather than invalid.
  if (legacy && nonSecretPolicyErrors.length === 0) {
    return { ok: true, legacy: true, authoritative: false, errors: [] }
  }

  if (!validation.valid) {
    return { ok: false, legacy, authoritative: false, errors: validation.errors }
  }

  if (sourceKind === 'agent_session_manifest_script') {
    const authoritative = isAuthoritativeGeneratedManifest(entry.generation_argv)
    return { ok: true, legacy: false, authoritative, errors: [] }
  }

  return { ok: true, legacy: false, authoritative: true, errors: [] }
}

/**
 * Collect and classify a full set of evidence entries.
 *
 * Returns a summary object suitable for session_recording_smoke_verdict/v1
 * evidence_refs / authoritative_count / legacy_evidence_excluded fields.
 */
export function collectEvidence(entries) {
  const list = Array.isArray(entries) ? entries : []
  let authoritativeCount = 0
  let legacyExcluded = false
  const evidenceRefs = []
  const invalid = []

  for (const entry of list) {
    const classification = classifyEvidenceEntry(entry)
    if (entry?.source_ref) {
      evidenceRefs.push(entry.source_ref)
    }
    if (classification.legacy) {
      legacyExcluded = true
    }
    if (classification.authoritative) {
      authoritativeCount += 1
    }
    if (!classification.ok) {
      invalid.push({
        source_ref: entry?.source_ref ?? null,
        errors: classification.errors,
      })
    }
  }

  return {
    authoritative_count: authoritativeCount,
    legacy_evidence_excluded: legacyExcluded,
    evidence_refs: evidenceRefs,
    invalid,
  }
}

// ============================================================================
// Main
// ============================================================================

function main() {
  const args = parseArgs(process.argv.slice(2))

  if (args.help) {
    printHelp()
    process.exit(0)
  }

  if (!args.evidenceInput) {
    console.error('Error: --evidence-input <file.json> is required. Use --help for usage.')
    process.exit(2)
  }

  let entries
  try {
    const raw = readFileSync(resolve(args.evidenceInput), 'utf-8')
    entries = JSON.parse(raw)
  } catch (err) {
    console.error(`Error: failed to read/parse --evidence-input: ${err.message}`)
    process.exit(2)
  }

  if (!Array.isArray(entries)) {
    console.error('Error: --evidence-input must contain a JSON array of evidence entries')
    process.exit(2)
  }

  const summary = collectEvidence(entries)
  const output = JSON.stringify(summary, null, 2)

  if (args.out) {
    writeFileSync(resolve(args.out), output + '\n', 'utf-8')
  }
  console.log(output)

  if (summary.invalid.length > 0) {
    console.error(`\ncollect-session-recording-smoke-evidence: ${summary.invalid.length} invalid entr${summary.invalid.length === 1 ? 'y' : 'ies'} detected.`)
    if (args.strict) {
      process.exit(1)
    }
  }

  process.exit(0)
}

// Only run main() when executed directly (not when imported for tests).
if (import.meta.url === `file://${process.argv[1]}`) {
  main()
}

export default {
  isLegacyManifest,
  isAuthoritativeGeneratedManifest,
  classifyEvidenceEntry,
  collectEvidence,
}
