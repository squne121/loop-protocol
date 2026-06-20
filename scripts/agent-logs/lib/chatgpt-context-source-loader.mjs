import { createHash } from 'crypto'
import { readFile } from 'fs/promises'

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
 * Load and validate a single source file.
 * Returns { parsed, bodyDigest, rawBytes }.
 * Throws runtimeError if forbidden fields are found.
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

  const bodyDigest = computeDigest(rawBytes)

  return { parsed, bodyDigest, rawBytes }
}

/**
 * Load all source files and build the source manifest.
 * Throws if any file contains forbidden fields.
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
      source_ref: path,
      canonical_digest: bodyDigest,
      body_digest: bodyDigest,
    })
    sources[kind] = parsed
  }

  // run reports — sort deterministically by path, then load
  const runReportPaths = [...(options.runReportJson ?? [])].sort()
  const runReports = []
  for (const p of runReportPaths) {
    const { parsed, bodyDigest } = await loadSourceFile(p, 'run_report_json')
    manifest.push({
      source_kind: 'run_report_json',
      source_ref: p,
      canonical_digest: bodyDigest,
      body_digest: bodyDigest,
    })
    runReports.push(parsed)
  }
  sources.run_reports = runReports

  // evidence refs — sort deterministically by path
  const evidenceRefPaths = [...(options.evidenceRefJson ?? [])].sort()
  const evidenceRefs = []
  for (const p of evidenceRefPaths) {
    const { parsed, bodyDigest } = await loadSourceFile(p, 'evidence_ref_json')
    manifest.push({
      source_kind: 'evidence_ref_json',
      source_ref: p,
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
