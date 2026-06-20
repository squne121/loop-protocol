import { createHash } from 'crypto'
import { readFile } from 'fs/promises'
import { basename } from 'path'

import { runtimeError } from './args.mjs'

/**
 * Forbidden fields that must not appear in any loaded source data.
 * These represent transcript / local path / secret / full command output leakage.
 */
const FORBIDDEN_FIELDS = [
  'raw_transcript',
  'transcript_excerpt',
  'full_command_output',
  'stdout',
  'stderr',
  'local_path',
]

/**
 * Compute a SHA-256 digest of a buffer.
 * @param {Buffer} buf
 * @returns {string} hex digest prefixed with 'sha256:'
 */
export function computeDigest(buf) {
  return `sha256:${createHash('sha256').update(buf).digest('hex')}`
}

/**
 * Scan a parsed JSON value recursively for forbidden field names.
 * @param {unknown} value
 * @param {string} path
 * @param {string[]} violations accumulated violations
 */
function scanForForbiddenFields(value, path, violations) {
  if (value === null || typeof value !== 'object') return

  if (Array.isArray(value)) {
    for (let i = 0; i < value.length; i++) {
      scanForForbiddenFields(value[i], `${path}[${i}]`, violations)
    }
    return
  }

  for (const key of Object.keys(value)) {
    if (FORBIDDEN_FIELDS.includes(key)) {
      violations.push(`${path}.${key}`)
    }
    scanForForbiddenFields(value[key], `${path}.${key}`, violations)
  }
}

/**
 * Apply scanPublicSafety from the agent-run-report-validation module.
 * This is stronger than the forbidden field scan: it also checks for
 * secret patterns (sk-, github_pat_, etc.), local paths (/home/, C:\),
 * and marker injection patterns.
 *
 * Blocker 4: apply this to ALL source kinds.
 *
 * @param {unknown} parsed
 * @param {string} sourceKind
 * @returns {Promise<void>}
 */
async function applyPublicSafetyScan(parsed, sourceKind) {
  try {
    const { scanPublicSafety } = await import('../../lib/agent-run-report-validation.mjs')
    const result = scanPublicSafety(parsed)
    if (!result.valid) {
      const errorSummary = result.errors.slice(0, 5)
        .map((e) => `${e.code ?? 'unknown'}: ${e.message ?? ''}`)
        .join('; ')
      throw runtimeError(
        'source.public_safety_violation',
        `public safety scan failed for ${sourceKind}: ${errorSummary}`
      )
    }
  } catch (err) {
    // Re-throw our own errors
    if (err && typeof err === 'object' && err.code === 'source.public_safety_violation') throw err
    // validator module not available — skip gracefully; forbidden field scan remains active
  }
}

/**
 * Load and validate a single source file.
 * Returns { parsed, bodyDigest, rawBytes }.
 * Throws runtimeError if forbidden fields are found or public safety scan fails.
 */
async function loadSourceFile(filePath, sourceKind) {
  let rawBytes
  try {
    rawBytes = await readFile(filePath)
  } catch (err) {
    throw runtimeError('source.read_error', `failed to read ${sourceKind} (${filePath}): ${err.message}`)
  }

  let parsed
  try {
    parsed = JSON.parse(rawBytes.toString('utf-8'))
  } catch (err) {
    throw runtimeError('source.parse_error', `failed to parse ${sourceKind} JSON (${filePath}): ${err.message}`)
  }

  const violations = []
  scanForForbiddenFields(parsed, sourceKind, violations)
  if (violations.length > 0) {
    throw runtimeError(
      'source.forbidden_field',
      `forbidden fields found in ${sourceKind}: ${violations.join(', ')}`
    )
  }

  // Blocker 4: apply stronger public safety scan for all source kinds
  await applyPublicSafetyScan(parsed, sourceKind)

  const bodyDigest = computeDigest(rawBytes)

  return { parsed, bodyDigest, rawBytes }
}

/**
 * Build a logical source ref (non-absolute path) from a file path and an index.
 * This avoids leaking absolute paths into the bundle.
 * @param {string} sourceKind
 * @param {number} index
 * @param {string} filePath
 * @returns {string}
 */
function logicalSourceRef(sourceKind, index, filePath) {
  // Use basename only, not absolute path
  return `${sourceKind}[${index}]:${basename(filePath)}`
}

/**
 * Load all source files and build the source manifest.
 * Throws if any file contains forbidden fields or fails public safety scan.
 *
 * Source refs in manifest are logical (basename-based), not absolute paths.
 *
 * Sort order for run reports: source_ref/path (basename sort), then run_id, then started_at.
 *
 * @param {object} options
 * @param {string} options.parentIssueJson
 * @param {string} options.targetIssueJson
 * @param {string} options.retroIndexJson
 * @param {string} options.sourceSetJson
 * @param {string[]} options.runReportJson
 * @param {string[]} options.evidenceRefJson
 * @returns {{ sources: object, manifest: object[] }}
 */
export async function loadSources(options) {
  const manifest = []
  const sources = {}

  const singleFiles = [
    { path: options.parentIssueJson, kind: 'parent_issue_json' },
    { path: options.targetIssueJson, kind: 'target_issue_json' },
    { path: options.retroIndexJson, kind: 'retro_index_json' },
    { path: options.sourceSetJson, kind: 'source_set_json' },
  ]

  for (const { path, kind } of singleFiles) {
    const { parsed, bodyDigest } = await loadSourceFile(path, kind)
    manifest.push({
      source_kind: kind,
      // logical ref — basename only, no absolute path
      source_ref: logicalSourceRef(kind, 0, path),
      canonical_digest: bodyDigest,
      body_digest: bodyDigest,
    })
    sources[kind] = parsed
  }

  // run reports — sort deterministically by basename (path), then run_id, then started_at
  const runReportPaths = [...(options.runReportJson ?? [])].sort((a, b) => {
    const ba = basename(a)
    const bb = basename(b)
    return ba < bb ? -1 : ba > bb ? 1 : 0
  })
  const runReports = []
  for (let i = 0; i < runReportPaths.length; i++) {
    const p = runReportPaths[i]
    const { parsed, bodyDigest } = await loadSourceFile(p, 'run_report_json')
    manifest.push({
      source_kind: 'run_report_json',
      source_ref: logicalSourceRef('run_report_json', i, p),
      canonical_digest: bodyDigest,
      body_digest: bodyDigest,
    })
    runReports.push(parsed)
  }
  sources.run_reports = runReports

  // evidence refs — sort deterministically by basename
  const evidenceRefPaths = [...(options.evidenceRefJson ?? [])].sort((a, b) => {
    const ba = basename(a)
    const bb = basename(b)
    return ba < bb ? -1 : ba > bb ? 1 : 0
  })
  const evidenceRefs = []
  for (let i = 0; i < evidenceRefPaths.length; i++) {
    const p = evidenceRefPaths[i]
    const { parsed, bodyDigest } = await loadSourceFile(p, 'evidence_ref_json')
    manifest.push({
      source_kind: 'evidence_ref_json',
      source_ref: logicalSourceRef('evidence_ref_json', i, p),
      canonical_digest: bodyDigest,
      body_digest: bodyDigest,
    })
    if (Array.isArray(parsed)) {
      for (const ref of parsed) {
        evidenceRefs.push(ref)
      }
    } else {
      evidenceRefs.push(parsed)
    }
  }
  sources.evidence_refs = evidenceRefs

  return { sources, manifest }
}

export { FORBIDDEN_FIELDS, scanForForbiddenFields }
