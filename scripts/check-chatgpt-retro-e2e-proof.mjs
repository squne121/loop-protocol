#!/usr/bin/env node
// check-chatgpt-retro-e2e-proof.mjs
//
// Fail-closed checker for chatgpt_retro_execution_proof/v1 (Issue #1405, parent #1153).
//
// Markdown candidate structure:
//
//   <!-- RETRO_E2E_PROOF_V1 start -->
//   ```json
//   { ...chatgpt_retro_execution_proof/v1 payload... }
//   ```
//   <!-- RETRO_E2E_PROOF_V1 end -->
//
//   <!-- CHATGPT_RETROSPECTIVE_RESULT_V1 start -->
//   ```json
//   { ...chatgpt_retrospective_result/v1 payload referenced by the proof... }
//   ```
//   <!-- CHATGPT_RETROSPECTIVE_RESULT_V1 end -->
//
// Responsibilities (Issue #1405 AC4, In Scope bullet "proof checker の追加"):
//   1. schema validation pass for both the proof artifact and the referenced
//      chatgpt_retrospective_result/v1 payload
//   2. scanPublicSafety pass (reused from agent-run-report-validation.mjs)
//   3. input_marker_digest == proof.chatgpt_context.marker_digest
//   4. target tuple consistency between proof.target / retrospective_result.target
//   5. evidence_refs resolvability (operation_index_ref / marker comment /
//      run report comment / retro index comment / allowed repo file / cited web_doc)
//   6. resolver_status != resolved -> verdict: approve forbidden
//   7. evidence_mode: synthetic_route_proof -> real runtime capture claims forbidden
//      when verdict is approve
//   8. follow_up_issue_candidates[].body / findings[].claim / findings[].recommendation
//      recursive injection + forbidden-field scan
//   9. fixture-mode staleness re-verification of resolved_comment_set_digest
//
// This is a single-file checker (Allowed Paths for #1405 does not include a
// scripts/lib/ helper split for this checker).
//
// Usage:
//   node scripts/check-chatgpt-retro-e2e-proof.mjs <file-or-glob ...>
//   pnpm run chatgpt-retro-e2e-proof:check

import { createHash } from 'node:crypto'
import { existsSync, readFileSync } from 'node:fs'
import { glob as fsGlob, stat } from 'node:fs/promises'
import { dirname, resolve } from 'node:path'
import { fileURLToPath } from 'node:url'
import {
  scanPublicSafety,
  validateChatgptRetrospectiveResultAgainstSchema,
} from './lib/agent-run-report-validation.mjs'
import {
  computeAgentOperationSessionIndexPayloadDigest,
  validateAgentOperationSessionIndex,
} from './check-agent-operation-session-index.mjs'

const __dirname = dirname(fileURLToPath(import.meta.url))
export const REPO_ROOT = resolve(__dirname, '..')

let Ajv2020, ajvFormats
try {
  const mod = await import('ajv/dist/2020.js')
  Ajv2020 = mod.default
  const formatsModule = await import('ajv-formats')
  ajvFormats = formatsModule.default
} catch (err) {
  console.error('Error: ajv and ajv-formats must be installed as devDependencies')
  console.error(err instanceof Error ? err.message : String(err))
  process.exit(1)
}

const SCHEMA_FILE = resolve(REPO_ROOT, 'docs/schemas/chatgpt-retro-execution-proof.schema.json')

const PROOF_START_MARKER = '<!-- RETRO_E2E_PROOF_V1 start -->'
const PROOF_END_MARKER = '<!-- RETRO_E2E_PROOF_V1 end -->'
const RESULT_START_MARKER = '<!-- CHATGPT_RETROSPECTIVE_RESULT_V1 start -->'
const RESULT_END_MARKER = '<!-- CHATGPT_RETROSPECTIVE_RESULT_V1 end -->'

// forbidden_keys_extra (Issue #1405 In Scope, OWNER review indication 7)
const FORBIDDEN_KEYS_EXTRA = [
  'messages',
  'prompt',
  'system_prompt',
  'tool_input',
  'tool_output',
  'tool_result',
  'request_body',
  'response_body',
  'stdout',
  'stderr',
  'authorization',
  'cookie',
  'set_cookie',
  'api_key',
  'token',
  'trace_state',
  'tracestate',
  'baggage',
  'span_links',
  'exception.message',
  'exception.stacktrace',
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

const FORBIDDEN_FIELD_SET = new Set(FORBIDDEN_KEYS_EXTRA.map(normalizeFieldName))
const MAX_FORBIDDEN_SUFFIX_SEGMENTS = 3

// Injection / leakage patterns applied to free-form text fields (findings[].claim,
// findings[].recommendation, follow_up_issue_candidates[].body). These fields are
// untrusted evidence per proof.safety.issue_or_pr_body_treated_as_untrusted_evidence.
const INJECTION_PATTERNS = [
  /\bignore previous instructions\b/i,
  /\bforget all previous\b/i,
  /\bact as\b.*\bsystem\b/i,
  /\bsystem:\s/i,
  /<script[\s>]/i,
  /<iframe[\s>]/i,
  /<!--/,
  /\[\[(?:SYSTEM|USER|ASSISTANT)\]\]/i,
  /\0/,
]

const RUNTIME_CAPTURE_CLAIM_PATTERNS = [
  /\blatitude cloud (?:trace|pilot)\b/i,
  /\bentirecli checkpoint\b/i,
  /\bnpm package (?:distribution|install|published)\b/i,
  /\breal runtime capture\b/i,
  /\breal trace export\b/i,
  /\bcloud pilot (?:success|adoption)\b/i,
]

const RAW_TRACE_ID_RE = /\b[0-9a-f]{32}\b/i

function createAjv() {
  const ajv = new Ajv2020({ strict: true, allErrors: true })
  ajvFormats(ajv)
  return ajv
}

function loadSchema() {
  return JSON.parse(readFileSync(SCHEMA_FILE, 'utf-8'))
}

function classifySchemaError(error) {
  if (error.keyword === 'required') {
    return 'schema.required'
  }
  if (error.keyword === 'additionalProperties') {
    return 'schema.unevaluated_property'
  }
  return 'schema.invalid'
}

export function validateChatgptRetroExecutionProofAgainstSchema(payload) {
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

// ── canonical-json-v1 digest (docs_dev digest_profile, Issue #1405) ─────────
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

export function canonicalizeChatgptRetroExecutionProofPayload(payload) {
  return canonicalizeValue(payload)
}

export function computeChatgptRetroExecutionProofDigest(payload) {
  const canonical = canonicalizeChatgptRetroExecutionProofPayload(payload)
  const jsonText = JSON.stringify(canonical)
  const hash = createHash('sha256').update(jsonText, 'utf-8').digest('hex')
  return `sha256:${hash}`
}

// ── forbidden field recursive scanner (forbidden_keys_extra) ───────────────
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
          message: `forbidden field "${key}" is not allowed in chatgpt retro e2e proof artifacts`,
        })
      } else {
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
            message: `forbidden nested field path "${matchedSuffix}" is not allowed in chatgpt retro e2e proof artifacts`,
          })
        }
      }

      errors.push(...scanForbiddenFields(entry, keyPath, nextChain))
    }
    return errors
  }

  if (typeof value === 'string' && RAW_TRACE_ID_RE.test(value)) {
    errors.push({
      path,
      code: 'trace_id.raw_forbidden',
      message: `raw trace-id-like string detected at ${path}`,
    })
  }

  return errors
}

export function scanChatgptRetroE2eProofForbiddenFields(payload) {
  const errors = scanForbiddenFields(payload)
  return { valid: errors.length === 0, errors }
}

// ── free-form text injection scan (findings[].claim/recommendation,
// follow_up_issue_candidates[].body). These are untrusted-evidence text fields. ──
function collectInjectionErrors(text, path) {
  const errors = []
  for (const pattern of INJECTION_PATTERNS) {
    if (pattern.test(text)) {
      errors.push({
        path,
        code: 'injection.follow_up_body',
        message: `untrusted free-form text at ${path} matched injection pattern: ${pattern.source}`,
      })
    }
  }
  return errors
}

export function scanChatgptRetrospectiveResultFreeFormText(retroResult) {
  const errors = []
  for (const [index, finding] of (retroResult?.findings ?? []).entries()) {
    if (typeof finding?.claim === 'string') {
      errors.push(...collectInjectionErrors(finding.claim, `findings[${index}].claim`))
    }
    if (typeof finding?.recommendation === 'string') {
      errors.push(...collectInjectionErrors(finding.recommendation, `findings[${index}].recommendation`))
    }
  }
  for (const [index, candidate] of (retroResult?.follow_up_issue_candidates ?? []).entries()) {
    if (typeof candidate?.body === 'string') {
      errors.push(...collectInjectionErrors(candidate.body, `follow_up_issue_candidates[${index}].body`))
    }
  }
  return { valid: errors.length === 0, errors }
}

function textMatchesRuntimeCaptureClaim(text) {
  return RUNTIME_CAPTURE_CLAIM_PATTERNS.some((pattern) => pattern.test(text))
}

function collectRuntimeCaptureClaimTexts(retroResult) {
  const texts = []
  for (const finding of retroResult?.findings ?? []) {
    if (typeof finding?.claim === 'string') {
      texts.push(finding.claim)
    }
    if (typeof finding?.recommendation === 'string') {
      texts.push(finding.recommendation)
    }
  }
  for (const candidate of retroResult?.follow_up_issue_candidates ?? []) {
    if (typeof candidate?.body === 'string') {
      texts.push(candidate.body)
    }
  }
  return texts
}

function filterPublicSafetyErrors(errors) {
  return errors.filter((error) => {
    if (error.code !== 'secret.token_like_hex40') {
      return true
    }
    return ![
      'operation_index_ref.embedded_payload.operation.source.commit_id',
      'operation_index_ref.embedded_payload.verification.operation_source_resolver.target_commit',
    ].includes(error.path)
  })
}

// ── markdown extraction (marker uniqueness + fence matching) ───────────────
function extractMarkedJsonBlock(markdown, startMarker, endMarker) {
  const lines = markdown.split('\n')
  let startCount = 0
  let endCount = 0
  let startLine = -1
  let endLine = -1

  for (let i = 0; i < lines.length; i += 1) {
    if (lines[i].includes(startMarker)) {
      startCount += 1
      startLine = i
    }
    if (lines[i].includes(endMarker)) {
      endCount += 1
      endLine = i
    }
  }

  if (startCount !== 1) {
    return {
      ok: false,
      error: {
        path: 'markdown.start_marker',
        code: 'marker.duplicate_comment',
        message: `start marker "${startMarker}" appears ${startCount} times`,
      },
    }
  }
  if (endCount !== 1) {
    return {
      ok: false,
      error: {
        path: 'markdown.end_marker',
        code: 'marker.duplicate_comment',
        message: `end marker "${endMarker}" appears ${endCount} times`,
      },
    }
  }
  if (startLine >= endLine) {
    return {
      ok: false,
      error: {
        path: 'markdown.marker_order',
        code: 'marker.constraint_violation',
        message: 'start marker must appear before end marker',
      },
    }
  }

  let openingFenceLine = -1
  let fenceLength = 0
  for (let i = startLine + 1; i < endLine; i += 1) {
    const match = lines[i].match(/^(`{3,})json\s*$/)
    if (match) {
      openingFenceLine = i
      fenceLength = match[1].length
      break
    }
  }
  if (openingFenceLine === -1) {
    return {
      ok: false,
      error: {
        path: 'markdown.fence',
        code: 'marker.constraint_violation',
        message: 'opening json fence not found between markers',
      },
    }
  }

  let closingFenceLine = -1
  for (let i = openingFenceLine + 1; i < endLine; i += 1) {
    if (lines[i] === '`'.repeat(fenceLength)) {
      closingFenceLine = i
      break
    }
  }
  if (closingFenceLine === -1) {
    return {
      ok: false,
      error: {
        path: 'markdown.fence',
        code: 'marker.constraint_violation',
        message: `closing fence not found for ${fenceLength} backticks`,
      },
    }
  }

  const jsonText = lines.slice(openingFenceLine + 1, closingFenceLine).join('\n')
  try {
    return { ok: true, payload: JSON.parse(jsonText) }
  } catch (err) {
    return {
      ok: false,
      error: {
        path: 'markdown.json',
        code: 'marker.constraint_violation',
        message: err instanceof Error ? err.message : String(err),
      },
    }
  }
}

function extractCommentUrlNumber(url, kindSegment) {
  if (typeof url !== 'string') {
    return null
  }
  const match = url.match(new RegExp(`^https://github\\.com/squne121/loop-protocol/${kindSegment}/([0-9]+)#issuecomment-[0-9]+$`))
  return match ? Number(match[1]) : null
}

// Allowed path prefixes for evidence_refs kind: repo_file (P0-4). Bounded to the
// docs/tests/scripts trees this checker's own artifacts live under; a repo_file
// reference outside these prefixes is not resolvable evidence for a retro proof.
const ALLOWED_REPO_FILE_PREFIXES = ['docs/', 'tests/fixtures/', 'scripts/']
const SHA256_DIGEST_RE = /^sha256:[0-9a-f]{64}$/

// isResolvableEvidenceRef (P0-4, Issue #1405 OWNER review): narrowed allowlist-closed
// resolver. github_comment refs must exactly match one of the two comment URLs the
// proof itself declares AND carry a digest equal to the corresponding payload digest
// (operation_index_ref.payload_digest / chatgpt_context.marker_digest). Arbitrary
// issue/pull comment URLs are no longer accepted as a generic fallback.
//
// Note (bounded scope): marker refs.run_reports[*].comment_url / refs.retro_index
// resolution described in the OWNER review requires a live-resolved marker payload
// that is not part of chatgpt_retro_execution_proof/v1's current schema shape (no
// "refs" object is carried on the proof). Extending resolution to those additional
// marker-derived comment URLs is deferred to a follow-up issue that also extends the
// marker payload schema; this checker only resolves what the proof itself declares.
function isResolvableEvidenceRef(ref, proof) {
  if (!ref || typeof ref !== 'object' || typeof ref.ref !== 'string') {
    return false
  }
  if (ref.kind === 'github_comment') {
    if (ref.ref === proof?.operation_index_ref?.comment_url) {
      return typeof ref.digest === 'string' && ref.digest === proof?.operation_index_ref?.payload_digest
    }
    if (ref.ref === proof?.chatgpt_context?.marker_comment_url) {
      return typeof ref.digest === 'string' && ref.digest === proof?.chatgpt_context?.marker_digest
    }
    return false
  }
  if (ref.kind === 'github_issue' || ref.kind === 'github_pr') {
    return /^https:\/\/github\.com\/squne121\/loop-protocol\/(issues|pull)\/[0-9]+$/.test(ref.ref)
  }
  if (ref.kind === 'repo_file') {
    if (typeof ref.digest !== 'string' || !SHA256_DIGEST_RE.test(ref.digest)) {
      return false
    }
    if (!ALLOWED_REPO_FILE_PREFIXES.some((prefix) => ref.ref.startsWith(prefix))) {
      return false
    }
    const candidate = resolve(REPO_ROOT, ref.ref)
    return candidate.startsWith(`${REPO_ROOT}/`) && existsSync(candidate)
  }
  if (ref.kind === 'web_doc') {
    if (typeof ref.digest !== 'string' || !SHA256_DIGEST_RE.test(ref.digest)) {
      return false
    }
    return /^https:\/\//.test(ref.ref)
  }
  return false
}

// validateChatgptContextGovernanceInvariants (P0-2/P0-3, Issue #1405 OWNER review):
// explicit semantic gate for the connector-only / synthetic-route invariants, in
// addition to (not instead of) the schema-level const constraints. Kept as an
// independent check so a future schema loosening cannot silently reopen these
// invariants without this checker also failing closed.
export function validateChatgptContextGovernanceInvariants(proof) {
  const errors = []
  const ctx = proof?.chatgpt_context
  if (ctx) {
    if (ctx.local_file_access_used !== false) {
      errors.push({
        path: 'chatgpt_context.local_file_access_used',
        code: 'chatgpt_context.local_file_access_forbidden',
        message: 'chatgpt_context.local_file_access_used must be false (GitHub-connector-only proof)',
      })
    }
    if (ctx.latitude_direct_access_used !== false) {
      errors.push({
        path: 'chatgpt_context.latitude_direct_access_used',
        code: 'chatgpt_context.latitude_direct_access_forbidden',
        message: 'chatgpt_context.latitude_direct_access_used must be false (GitHub-connector-only proof)',
      })
    }
    if (ctx.raw_trace_access_used !== false) {
      errors.push({
        path: 'chatgpt_context.raw_trace_access_used',
        code: 'chatgpt_context.raw_trace_access_forbidden',
        message: 'chatgpt_context.raw_trace_access_used must be false (GitHub-connector-only proof)',
      })
    }
    if (ctx.github_connector_only !== true) {
      errors.push({
        path: 'chatgpt_context.github_connector_only',
        code: 'chatgpt_context.github_connector_only_required',
        message: 'chatgpt_context.github_connector_only must be true (GitHub-connector-only proof)',
      })
    }
  }
  const mode = proof?.evidence_mode
  if (mode) {
    if (mode.value !== 'synthetic_route_proof') {
      errors.push({
        path: 'evidence_mode.value',
        code: 'evidence_mode.non_synthetic_scope_forbidden',
        message: 'evidence_mode.value must be synthetic_route_proof (real pilot route is out of scope for this proof kind)',
      })
    }
    if (mode.marker_prerequisite_evidence_mode !== 'synthetic_only') {
      errors.push({
        path: 'evidence_mode.marker_prerequisite_evidence_mode',
        code: 'evidence_mode.non_synthetic_prerequisite_forbidden',
        message: 'evidence_mode.marker_prerequisite_evidence_mode must be synthetic_only',
      })
    }
    for (const field of ['real_runtime_capture_claimed', 'real_pilot_verified_claimed', 'allowed_real_pilot_upgrade', 'cloud_pilot_claimed']) {
      if (mode[field] !== false) {
        errors.push({
          path: `evidence_mode.${field}`,
          code: 'evidence_mode.real_pilot_flag_forbidden',
          message: `evidence_mode.${field} must be false (real pilot claims are out of scope for this proof kind)`,
        })
      }
    }
  }
  return { valid: errors.length === 0, errors }
}

// ── digest_profile: fixture-mode comment-universe recomputation (AC10) ─────
// Deferred per Runtime Verification Applicability (decision: deferred): live
// resolver re-execution is out of unit-test scope. In fixture mode we
// recompute the comment-universe digest from the two comment URLs the proof
// itself declares, so a stale/mutated URL is detected without a live fetch.
export function computeFixtureModeResolvedCommentSetDigest(proof) {
  const urls = [
    proof?.operation_index_ref?.comment_url,
    proof?.chatgpt_context?.marker_comment_url,
  ].filter((url) => typeof url === 'string').sort()
  return computeChatgptRetroExecutionProofDigest(urls)
}

export function validateChatgptRetroExecutionProof(proof, retroResult) {
  const errors = []

  const proofSchemaResult = validateChatgptRetroExecutionProofAgainstSchema(proof)
  errors.push(...proofSchemaResult.errors)

  const retroSchemaResult = validateChatgptRetrospectiveResultAgainstSchema(retroResult)
  errors.push(...retroSchemaResult.errors.map((error) => ({
    ...error,
    path: `retrospective_result_payload${error.path === 'root' ? '' : `.${error.path}`}`,
  })))

  errors.push(...filterPublicSafetyErrors(scanPublicSafety(proof).errors))
  errors.push(...scanPublicSafety(retroResult).errors)
  errors.push(...scanChatgptRetroE2eProofForbiddenFields(proof).errors)
  errors.push(...scanChatgptRetroE2eProofForbiddenFields(retroResult).errors)
  errors.push(...scanChatgptRetrospectiveResultFreeFormText(retroResult).errors)
  errors.push(...validateChatgptContextGovernanceInvariants(proof).errors)

  if (proofSchemaResult.valid && retroSchemaResult.valid) {
    // digest.marker_mismatch
    if (retroResult.input_marker_digest !== proof.chatgpt_context.marker_digest) {
      errors.push({
        path: 'retrospective_result_payload.input_marker_digest',
        code: 'digest.marker_mismatch',
        message: 'input_marker_digest does not match proof.chatgpt_context.marker_digest',
      })
    }

    // digest.retrospective_result_mismatch
    const recomputedDigest = computeChatgptRetroExecutionProofDigest(retroResult)
    if (proof.retrospective_result.payload_digest !== recomputedDigest) {
      errors.push({
        path: 'retrospective_result.payload_digest',
        code: 'digest.retrospective_result_mismatch',
        message: 'proof.retrospective_result.payload_digest does not match the recomputed digest of the referenced payload',
      })
    }

    if (proof.operation_index_ref?.embedded_payload) {
      const operationIndexValidation = validateAgentOperationSessionIndex(proof.operation_index_ref.embedded_payload)
      if (!operationIndexValidation.valid) {
        for (const error of operationIndexValidation.errors) {
          errors.push({
            path: `operation_index_ref.embedded_payload${error.path === 'root' ? '' : `.${error.path}`}`,
            code: 'operation_index.validation_failed',
            message: error.message,
          })
        }
      }

      const recomputedOperationIndexDigest = computeAgentOperationSessionIndexPayloadDigest(proof.operation_index_ref.embedded_payload)
      if (proof.operation_index_ref.payload_digest !== recomputedOperationIndexDigest) {
        errors.push({
          path: 'operation_index_ref.payload_digest',
          code: 'operation_index.payload_digest_mismatch',
          message: 'operation_index_ref.payload_digest does not match the embedded operation index payload digest',
        })
      }

      if (proof.operation_index_ref.embedded_payload?.verification?.operation_source_resolver?.status !== 'resolved') {
        errors.push({
          path: 'operation_index_ref.embedded_payload.verification.operation_source_resolver.status',
          code: 'operation_index.source_resolver_unresolved',
          message: 'embedded operation index payload must carry verification.operation_source_resolver.status = "resolved"',
        })
      }
    }

    // target.mismatch
    const targetSegment = proof.target.kind === 'issue' ? 'issue' : 'pull_request'
    if (retroResult.target.type !== targetSegment || retroResult.target.number !== proof.target.number || retroResult.target.repo !== proof.repo) {
      errors.push({
        path: 'retrospective_result_payload.target',
        code: 'target.mismatch',
        message: 'retrospective_result target does not match proof.target/proof.repo',
      })
    }

    // evidence_refs.unresolvable
    for (const [findingIndex, finding] of (retroResult.findings ?? []).entries()) {
      for (const [refIndex, ref] of (finding.evidence_refs ?? []).entries()) {
        if (!isResolvableEvidenceRef(ref, proof)) {
          errors.push({
            path: `retrospective_result_payload.findings[${findingIndex}].evidence_refs[${refIndex}]`,
            code: 'evidence_refs.unresolvable',
            message: 'evidence_ref does not resolve to operation_index_ref / marker comment / run report comment / retro index comment / allowed repo file / cited web_doc',
          })
        }
      }
    }

    // verdict.resolver_not_resolved
    if (proof.chatgpt_context.resolve_live_status !== 'resolved' && retroResult.verdict === 'approve') {
      errors.push({
        path: 'retrospective_result_payload.verdict',
        code: 'verdict.resolver_not_resolved',
        message: 'verdict "approve" is forbidden when chatgpt_context.resolve_live_status is not "resolved"',
      })
    }

    // verdict.real_capture_claim_forbidden
    if (proof.evidence_mode.value === 'synthetic_route_proof' && retroResult.verdict === 'approve') {
      const claimTexts = collectRuntimeCaptureClaimTexts(retroResult)
      if (claimTexts.some(textMatchesRuntimeCaptureClaim)) {
        errors.push({
          path: 'retrospective_result_payload',
          code: 'verdict.real_capture_claim_forbidden',
          message: 'verdict "approve" is forbidden when evidence_mode is synthetic_route_proof and a real runtime capture claim is present (forbidden_or_out_of_scope_runtime_claim)',
        })
      }
    }

    // evidence_mode.real_pilot_verified_without_approval
    if (proof.evidence_mode.real_pilot_verified_claimed === true && proof.evidence_mode.allowed_real_pilot_upgrade !== true) {
      errors.push({
        path: 'evidence_mode.real_pilot_verified_claimed',
        code: 'evidence_mode.real_pilot_verified_without_approval',
        message: 'real_pilot_verified_claimed requires allowed_real_pilot_upgrade = true (#1220 approve_timeboxed_real_pilot)',
      })
    }

    // digest.stale (fixture-mode comment-universe recomputation, AC10)
    const recomputedCommentSetDigest = computeFixtureModeResolvedCommentSetDigest(proof)
    if (proof.chatgpt_context.resolver_evidence.resolved_comment_set_digest !== recomputedCommentSetDigest) {
      errors.push({
        path: 'chatgpt_context.resolver_evidence.resolved_comment_set_digest',
        code: 'digest.stale',
        message: 'resolved_comment_set_digest does not match the fixture-mode recomputed comment-universe digest',
      })
    }
  }

  return { valid: errors.length === 0, errors }
}

export function validateChatgptRetroE2eProofMarkdown(markdown) {
  const proofExtraction = extractMarkedJsonBlock(markdown, PROOF_START_MARKER, PROOF_END_MARKER)
  if (!proofExtraction.ok) {
    return { valid: false, errors: [proofExtraction.error] }
  }
  const resultExtraction = extractMarkedJsonBlock(markdown, RESULT_START_MARKER, RESULT_END_MARKER)
  if (!resultExtraction.ok) {
    return { valid: false, errors: [resultExtraction.error] }
  }
  return validateChatgptRetroExecutionProof(proofExtraction.payload, resultExtraction.payload)
}

function printUsage() {
  console.error('Usage: check-chatgpt-retro-e2e-proof.mjs [file-or-glob ...]')
}

function getDefaultCheckPatterns() {
  return [
    'tests/fixtures/chatgpt-retro-e2e-proof/valid-*.md',
    'artifacts/chatgpt-retro-e2e-proof*.md',
  ]
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

async function main() {
  const args = process.argv.slice(2)
  const explicitTargets = args.length > 0
  const patterns = explicitTargets ? args : getDefaultCheckPatterns()
  const files = await expandPatterns(patterns)

  if (files.length === 0) {
    if (explicitTargets || process.env.CI === 'true') {
      printUsage()
      console.error('chatgpt-retro-e2e-proof:check: no files found')
      process.exit(1)
    }
    console.log('chatgpt-retro-e2e-proof:check: no files found (default targets) - skipped')
    process.exit(0)
  }

  let failures = 0
  for (const file of files) {
    const shortPath = file.replace(`${REPO_ROOT}/`, '')
    const markdown = readFileSync(file, 'utf-8')
    const result = validateChatgptRetroE2eProofMarkdown(markdown)
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
