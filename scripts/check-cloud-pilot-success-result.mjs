#!/usr/bin/env node
// check-cloud-pilot-success-result.mjs
//
// Fail-closed checker for cloud_pilot_success_result/v1 (Issue #1364, follow-up of
// ADR 0005 hybrid_reference_from_agent_run_report, parent #1330 / #1153).
//
// Responsibilities are split (OWNER Blocker 2, #1364 issue body):
//   1. schema validation (Ajv 2020-12, closed shape via unevaluatedProperties:false)
//   2. semantic checks: digest recomputation / marker constraints / gate evidence
//      field-level evidence / forbidden field recursive scan
//
// This is a single-file checker (Allowed Paths / Stop Condition: no scripts/lib
// helper split for this Issue).
//
// Usage:
//   node scripts/check-cloud-pilot-success-result.mjs <file-or-glob ...>
//   pnpm run cloud-pilot-success-result:check
//
// Markdown candidate structure (see docs/adr/0005-cloud-pilot-success-result-artifact-placement.md):
//
//   <!-- CLOUD_PILOT_SUCCESS_RESULT_V1 repo=<owner/repo> target=<kind:number> parent_issue=<n> result_id=<id> -->
//   ```json
//   { ...cloud_pilot_success_result/v1 payload... }
//   ```
//   <!-- CLOUD_PILOT_SUCCESS_RESULT_DIGEST_V1 sha256=<64hex> -->

import { createHash } from 'node:crypto'
import { existsSync, readFileSync } from 'node:fs'
import { glob as fsGlob, stat } from 'node:fs/promises'
import { dirname, resolve } from 'node:path'
import { fileURLToPath } from 'node:url'

const __dirname = dirname(fileURLToPath(import.meta.url))
export const REPO_ROOT = resolve(__dirname, '..')

let Ajv2020
try {
  const mod = await import('ajv/dist/2020.js')
  Ajv2020 = mod.default
} catch (err) {
  console.error('Error: ajv must be installed as a devDependency')
  console.error(err instanceof Error ? err.message : String(err))
  process.exit(1)
}

const SCHEMA_FILE = resolve(REPO_ROOT, 'docs/schemas/cloud-pilot-success-result.schema.json')
const OUTER_MARKER_NAME = 'CLOUD_PILOT_SUCCESS_RESULT_V1'
const DIGEST_MARKER_NAME = 'CLOUD_PILOT_SUCCESS_RESULT_DIGEST_V1'

const OUTER_MARKER_RE = new RegExp(
  `<!--\\s*${OUTER_MARKER_NAME}\\s+repo=(\\S+)\\s+target=(\\S+)\\s+parent_issue=(\\S+)\\s+result_id=(\\S+)\\s*-->`,
)
const DIGEST_MARKER_RE = new RegExp(`<!--\\s*${DIGEST_MARKER_NAME}\\s+sha256=([0-9a-f]{64})\\s*-->`, 'i')

// ── raw_trace_body_publication_forbidden_fields (ADR 0005) ──────────────────
const BASE_FORBIDDEN_FIELDS = [
  'raw_trace_body',
  'raw_span_body',
  'raw_event_body',
  'span_attributes_raw',
  'resource_attributes_raw',
  'request_body',
  'response_body',
  'request_headers',
  'response_headers',
  'authorization',
  'cookie',
  'set_cookie',
  'api_key',
  'credential',
  'raw_prompt',
  'full_prompt',
  'system_prompt',
  'tool_input',
  'tool_output',
  'command_line',
  'argv',
  'env',
  'stdout',
  'stderr',
  'full_command_output',
  'local_path',
  'shell_history',
  'terminal_scrollback',
  'provider_console_url_unredacted',
]

// raw_trace_body_publication_forbidden_fields_extension (ADR 0005, OWNER Blocker 7)
const EXTENDED_FORBIDDEN_FIELDS = [
  'tracestate',
  'trace_state',
  'baggage',
  'span_links',
  'exception.stacktrace',
  'exception.message',
  'db.statement',
  'http.request.body',
  'http.response.body',
  'url.full',
  'server.address',
  'client.address',
  'user.id',
  'enduser.id',
  'session.id',
]

function normalizeFieldName(name) {
  return String(name).toLowerCase().replace(/[^a-z0-9]/g, '')
}

const FORBIDDEN_FIELD_SET = new Set(
  [...BASE_FORBIDDEN_FIELDS, ...EXTENDED_FORBIDDEN_FIELDS].map(normalizeFieldName),
)

// Longest forbidden entry has 3 dotted segments (http.request.body).
const MAX_FORBIDDEN_SUFFIX_SEGMENTS = 3

// W3C trace-id: 32 lowercase hex chars. All-zero is explicitly invalid.
const RAW_TRACE_ID_RE = /\b[0-9a-f]{32}\b/i
const ALL_ZERO_TRACE_ID_RE = /\b0{32}\b/

function createAjv() {
  return new Ajv2020({ strict: true, allErrors: true })
}

function loadSchema() {
  return JSON.parse(readFileSync(SCHEMA_FILE, 'utf-8'))
}

function classifySchemaError(error) {
  if (error.keyword === 'required') {
    return 'schema.required'
  }
  if (error.keyword === 'additionalProperties' || error.keyword === 'unevaluatedProperties') {
    return 'schema.unevaluated_property'
  }
  if (typeof error.instancePath === 'string' && error.instancePath.startsWith('/target')) {
    return 'target.marker_mismatch'
  }
  return 'schema.invalid'
}

export function validatePayloadAgainstSchema(payload) {
  const schema = loadSchema()
  const ajv = createAjv()
  const validate = ajv.compile(schema)
  const valid = validate(payload)
  if (valid) {
    return { valid: true, errors: [] }
  }
  const errors = (validate.errors || []).map((error) => ({
    path: error.instancePath || 'root',
    code: classifySchemaError(error),
    message: error.message || 'schema validation failed',
  }))
  return { valid: false, errors }
}

// ── canonicalization (digest_policy.canonicalization_profile, ADR 0005) ─────
// name: cloud_pilot_success_result_public_projection_c14n/v1
//   - unicode_normalization: NFC
//   - object_key_order: lexical_by_unicode_codepoint
//   - preserve_array_order: true
//   - explicit null is preserved (NOT the same as an absent optional key)
function canonicalizeValue(value) {
  if (typeof value === 'string') {
    return value.normalize('NFC')
  }
  if (Array.isArray(value)) {
    return value.map((entry) => canonicalizeValue(entry))
  }
  if (value !== null && typeof value === 'object') {
    const sortedKeys = Object.keys(value)
      .map((key) => key.normalize('NFC'))
      .sort((a, b) => (a < b ? -1 : a > b ? 1 : 0))
    const out = {}
    for (const key of sortedKeys) {
      out[key] = canonicalizeValue(value[key])
    }
    return out
  }
  return value
}

export function canonicalizeCloudPilotSuccessResultPublicProjection(payload) {
  return canonicalizeValue(payload)
}

export function computeCloudPilotSuccessResultDigest(payload) {
  const canonical = canonicalizeCloudPilotSuccessResultPublicProjection(payload)
  const jsonText = JSON.stringify(canonical)
  const hash = createHash('sha256').update(jsonText, 'utf-8').digest('hex')
  return `sha256:${hash}`
}

// ── forbidden field recursive scanner (OWNER Blocker 7) ─────────────────────
// Detects: dotted literal keys (e.g. "exception.stacktrace" as one key string),
// camelCase variants (e.g. "traceState"), truly nested keys (exception -> stacktrace),
// keys holding array values (e.g. span_links), and raw trace-id-like string values.
function scanForbiddenFields(value, path = 'root', keyChain = []) {
  const errors = []

  if (Array.isArray(value)) {
    value.forEach((entry, index) => {
      errors.push(...scanForbiddenFields(entry, `${path}[${index}]`, keyChain))
    })
    return errors
  }

  if (value !== null && typeof value === 'object') {
    for (const [key, entry] of Object.entries(value)) {
      const keyPath = `${path}.${key}`
      const nextChain = [...keyChain, key]

      if (FORBIDDEN_FIELD_SET.has(normalizeFieldName(key))) {
        errors.push({
          path: keyPath,
          code: 'forbidden_field',
          message: `forbidden field "${key}" is not allowed in cloud_pilot_success_result payload`,
        })
      } else {
        // Nested dotted-path suffix check (e.g. exception -> stacktrace
        // forms the forbidden suffix "exception.stacktrace").
        let matchedSuffix = null
        const maxLen = Math.min(MAX_FORBIDDEN_SUFFIX_SEGMENTS, nextChain.length)
        for (let len = 2; len <= maxLen; len += 1) {
          const suffix = nextChain.slice(nextChain.length - len).join('.')
          if (FORBIDDEN_FIELD_SET.has(normalizeFieldName(suffix))) {
            matchedSuffix = suffix
            break
          }
        }
        if (matchedSuffix) {
          errors.push({
            path: keyPath,
            code: 'forbidden_field',
            message: `forbidden nested field path "${matchedSuffix}" is not allowed in cloud_pilot_success_result payload`,
          })
        }
      }

      errors.push(...scanForbiddenFields(entry, keyPath, nextChain))
    }
    return errors
  }

  if (typeof value === 'string') {
    if (RAW_TRACE_ID_RE.test(value)) {
      errors.push({
        path,
        code: 'trace_id.raw_forbidden',
        message: `raw trace-id-like string detected at ${path}; must be transformed to public_correlation_id`,
      })
    }
    if (ALL_ZERO_TRACE_ID_RE.test(value)) {
      errors.push({
        path,
        code: 'trace_id.raw_forbidden',
        message: `all-zero trace-id is invalid and forbidden at ${path}`,
      })
    }
  }

  return errors
}

export function scanCloudPilotSuccessResultForbiddenFields(payload) {
  const errors = scanForbiddenFields(payload)
  return { valid: errors.length === 0, errors }
}

// ── gate_refs field-level evidence checks (OWNER Blocker 1 / issue AC6/AC12) ─
function checkGateRefs(payload) {
  const errors = []
  const gateRefs = payload?.gate_refs
  if (!gateRefs || typeof gateRefs !== 'object') {
    return errors
  }

  const srs = gateRefs.session_recording_smoke
  if (srs && srs.verdict !== 'pass') {
    errors.push({
      path: 'gate_refs.session_recording_smoke.verdict',
      code: 'gate_ref.evidence_mismatch',
      message: 'session_recording_smoke (#246) gate_ref requires verdict = "pass"',
    })
  }

  const lrpd = gateRefs.latitude_real_pilot_decision
  if (lrpd && lrpd.decision !== 'approve_timeboxed_real_pilot') {
    errors.push({
      path: 'gate_refs.latitude_real_pilot_decision.decision',
      code: 'gate_ref.evidence_mismatch',
      message: 'latitude_real_pilot_decision (#1220) gate_ref requires decision = "approve_timeboxed_real_pilot"',
    })
  }

  const ldg = gateRefs.latitude_distribution_gate
  if (ldg) {
    if (ldg.argv_exposure_state !== 'absent_verified') {
      errors.push({
        path: 'gate_refs.latitude_distribution_gate.argv_exposure_state',
        code: 'gate_ref.evidence_mismatch',
        message: 'latitude_distribution_gate (#1261) gate_ref requires argv_exposure_state = "absent_verified"',
      })
    }
    if (ldg.remote_cleanup_state !== 'machine_verified') {
      errors.push({
        path: 'gate_refs.latitude_distribution_gate.remote_cleanup_state',
        code: 'gate_ref.evidence_mismatch',
        message: 'latitude_distribution_gate (#1261) gate_ref requires remote_cleanup_state = "machine_verified"',
      })
    }
  }

  const scc = gateRefs.success_contract_checker
  if (scc && scc.presented_as_real_target === true) {
    if (scc.state !== 'completed' || scc.checker_result !== 'pass') {
      errors.push({
        path: 'gate_refs.success_contract_checker',
        code: 'gate_ref.not_completed',
        message: 'success_contract_checker (#1326) gate_ref presented as a real target requires state="completed" and checker_result="pass"',
      })
    }
  }

  return errors
}

// ── evidence_mode / adoption-readiness guard (OWNER Blocker 1 / AC11) ───────
function checkEvidenceMode(payload) {
  const errors = []
  if (payload?.decision_ready !== true) {
    if (payload?.decision === 'adopt_cloud') {
      errors.push({
        path: 'decision',
        code: 'evidence_mode.violation',
        message: 'decision "adopt_cloud" requires decision_ready = true (real adoption-ready evidence)',
      })
    }
    if (payload?.cloud_adoption_allowed_now === true) {
      errors.push({
        path: 'cloud_adoption_allowed_now',
        code: 'evidence_mode.violation',
        message: 'cloud_adoption_allowed_now = true requires decision_ready = true (real adoption-ready evidence)',
      })
    }
  }
  return errors
}

// ── target kind/number vs outer marker cross-check (AC18) ───────────────────
function checkTargetKindMismatch(payload, markerTarget) {
  const errors = []
  if (!markerTarget || !payload?.target) {
    return errors
  }
  const match = /^(issue|pull_request):([1-9][0-9]*)$/.exec(markerTarget)
  if (!match) {
    return errors
  }
  const [, kind, number] = match
  if (payload.target.kind !== kind || String(payload.target.number) !== number) {
    errors.push({
      path: 'target',
      code: 'target.kind_mismatch',
      message: `outer marker target=${markerTarget} does not match payload.target (kind=${payload.target.kind}, number=${payload.target.number})`,
    })
  }
  return errors
}

// ── upsert_probe declarative simulation (OWNER Blocker 6 / AC18) ───────────
// Real GitHub comment list/upsert is Out of Scope for this Issue (see
// docs/adr/0005-...md github_comment_upsert_policy). Because the checker only
// validates a single artifact snapshot (no GitHub API calls), duplicate/stale
// detection is exercised via an optional declarative `upsert_probe` field that
// simulates the pre-computed result of a list_comments call. This keeps the
// negative fixtures deterministic and fixture-local while still exercising the
// checker's fail-closed branches for these two ADR policies.
function checkUpsertProbe(payload) {
  const errors = []
  const probe = payload?.upsert_probe
  if (!probe || typeof probe !== 'object') {
    return errors
  }
  if (
    Object.prototype.hasOwnProperty.call(probe, 'existing_comment_match_count')
    && probe.existing_comment_match_count !== 0
    && probe.existing_comment_match_count !== 1
  ) {
    errors.push({
      path: 'upsert_probe.existing_comment_match_count',
      code: 'marker.duplicate_comment',
      message: 'github_comment_upsert_policy requires zero_matches=create or one_match=update; multiple_matches is fail_closed',
    })
  }
  return errors
}

// ── markdown extraction ──────────────────────────────────────────────────────
function countOccurrences(haystack, needle) {
  return haystack.split(needle).length - 1
}

function extractFencedBlocks(markdown) {
  const lines = markdown.split('\n')
  const fenceLineIndices = []
  for (let i = 0; i < lines.length; i += 1) {
    if (/^```/.test(lines[i])) {
      fenceLineIndices.push(i)
    }
  }
  return { lines, fenceLineIndices }
}

export function extractCloudPilotSuccessResultCandidate(markdown) {
  const outerCount = countOccurrences(markdown, OUTER_MARKER_NAME)
  const digestCount = countOccurrences(markdown, DIGEST_MARKER_NAME)

  if (outerCount !== 1) {
    return {
      ok: false,
      error: {
        path: 'markdown.outer_marker',
        code: 'marker.constraint_violation',
        message: `outer marker "${OUTER_MARKER_NAME}" must appear exactly once (found ${outerCount})`,
      },
    }
  }
  if (digestCount !== 1) {
    return {
      ok: false,
      error: {
        path: 'markdown.digest_marker',
        code: 'marker.constraint_violation',
        message: `digest marker "${DIGEST_MARKER_NAME}" must appear exactly once (found ${digestCount})`,
      },
    }
  }

  const { lines, fenceLineIndices } = extractFencedBlocks(markdown)
  if (fenceLineIndices.length !== 2) {
    return {
      ok: false,
      error: {
        path: 'markdown.fence',
        code: 'marker.constraint_violation',
        message: `exactly one fenced code block is required (found ${fenceLineIndices.length} fence delimiter lines)`,
      },
    }
  }

  const [openIdx, closeIdx] = fenceLineIndices
  const openMatch = /^```(\w+)?\s*$/.exec(lines[openIdx])
  const lang = openMatch && openMatch[1] ? openMatch[1].toLowerCase() : ''

  const outerMatch = OUTER_MARKER_RE.exec(markdown)
  const digestMatch = DIGEST_MARKER_RE.exec(markdown)

  if (!outerMatch) {
    return {
      ok: false,
      error: {
        path: 'markdown.outer_marker',
        code: 'marker.constraint_violation',
        message: 'outer marker present but malformed (repo/target/parent_issue/result_id attributes required)',
      },
    }
  }
  if (!digestMatch) {
    return {
      ok: false,
      error: {
        path: 'markdown.digest_marker',
        code: 'marker.constraint_violation',
        message: 'digest marker present but malformed (sha256=<64hex> required)',
      },
    }
  }

  if (lang !== 'json') {
    return {
      ok: false,
      error: {
        path: 'markdown.fence',
        code: 'payload.non_json_rejected',
        message: `payload code fence must be JSON; found language "${lang || '<none>'}" (YAML payload is out of scope, follow-up)`,
      },
    }
  }

  const jsonText = lines.slice(openIdx + 1, closeIdx).join('\n')
  let payload
  try {
    payload = JSON.parse(jsonText)
  } catch (err) {
    return {
      ok: false,
      error: {
        path: 'markdown.json',
        code: 'schema.invalid',
        message: err instanceof Error ? err.message : String(err),
      },
    }
  }

  return {
    ok: true,
    payload,
    markerRepo: outerMatch[1],
    markerTarget: outerMatch[2],
    markerParentIssue: outerMatch[3],
    markerResultId: outerMatch[4],
    digest: digestMatch[1].toLowerCase(),
  }
}

// ── full pipeline ────────────────────────────────────────────────────────────
export function validateCloudPilotSuccessResultMarkdown(markdown) {
  const extraction = extractCloudPilotSuccessResultCandidate(markdown)
  if (!extraction.ok) {
    return { valid: false, errors: [extraction.error] }
  }

  const { payload, markerTarget, digest } = extraction
  const errors = []

  const schemaResult = validatePayloadAgainstSchema(payload)
  errors.push(...schemaResult.errors)

  errors.push(...checkTargetKindMismatch(payload, markerTarget))
  errors.push(...checkGateRefs(payload))
  errors.push(...checkEvidenceMode(payload))
  errors.push(...checkUpsertProbe(payload))
  errors.push(...scanForbiddenFields(payload))

  const recomputedDigest = computeCloudPilotSuccessResultDigest(payload)
  const storedDigest = `sha256:${digest}`
  if (recomputedDigest !== storedDigest) {
    const isStale = payload?.upsert_probe?.digest_freshness === 'stale'
    errors.push({
      path: 'markdown.digest_marker',
      code: isStale ? 'digest.stale' : 'digest.mismatch',
      message: `recomputed digest ${recomputedDigest} does not match stored digest ${storedDigest}`,
    })
  }

  return { valid: errors.length === 0, errors }
}

// ── CLI ──────────────────────────────────────────────────────────────────────
function isWithinRepo(filePath) {
  const resolved = resolve(filePath)
  return resolved === REPO_ROOT || resolved.startsWith(`${REPO_ROOT}/`)
}

async function expandPatterns(patterns) {
  const files = []
  for (const pattern of patterns) {
    const absPattern = resolve(REPO_ROOT, pattern)
    if (existsSync(absPattern)) {
      const stats = await stat(absPattern)
      if (stats.isFile()) {
        files.push(absPattern)
        continue
      }
    }
    try {
      const matches = await Array.fromAsync(fsGlob(absPattern))
      files.push(...matches.map((match) => resolve(match)))
    } catch {
      // fall through
    }
  }
  return [...new Set(files)].sort()
}

function getDefaultCheckPatterns() {
  return ['tests/fixtures/cloud-pilot-success-result/*.md']
}

async function main() {
  const explicitTargets = process.argv.slice(2)
  const patterns = explicitTargets.length > 0 ? explicitTargets : getDefaultCheckPatterns()
  const files = await expandPatterns(patterns)

  if (files.length === 0) {
    if (explicitTargets.length > 0 || process.env.CI === 'true') {
      console.error('cloud-pilot-success-result:check: no files found')
      process.exit(1)
    }
    console.log('cloud-pilot-success-result:check: no files found (default targets) - skipped')
    process.exit(0)
  }

  const outsideRepoFile = files.find((file) => !isWithinRepo(file))
  if (outsideRepoFile) {
    console.error(`FAIL ${outsideRepoFile}`)
    console.error('  - path: file')
    console.error('    code: file.outside_repo')
    console.error('    message: resolved target is outside the repository root')
    process.exit(1)
  }

  let failures = 0
  for (const file of files) {
    const shortPath = file.replace(`${REPO_ROOT}/`, '')
    const markdown = readFileSync(file, 'utf-8')
    const result = validateCloudPilotSuccessResultMarkdown(markdown)
    if (result.valid) {
      console.log(`PASS ${shortPath}`)
      continue
    }
    failures += 1
    console.error(`FAIL ${shortPath}`)
    for (const error of result.errors) {
      console.error(`  - path: ${error.path}`)
      console.error(`    code: ${error.code}`)
      console.error(`    message: ${error.message}`)
    }
  }

  process.exit(failures === 0 ? 0 : 1)
}

const isMain = process.argv[1] && resolve(process.argv[1]) === resolve(fileURLToPath(import.meta.url))
if (isMain) {
  main().catch((err) => {
    console.error(err instanceof Error ? err.message : String(err))
    process.exit(1)
  })
}
