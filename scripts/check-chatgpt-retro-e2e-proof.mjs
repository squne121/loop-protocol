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
import { GhCliIssueCommentsClient } from './agent-logs/lib/github-comments.mjs'
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
const OPERATION_INDEX_COMMENT_START_MARKER = '<!-- AGENT_OPERATION_SESSION_INDEX_V1 start -->'
const OPERATION_INDEX_COMMENT_END_MARKER = '<!-- AGENT_OPERATION_SESSION_INDEX_V1 end -->'
const PR_REVIEW_SURFACE_LIVE_PROOF_START_MARKER = '<!-- PR_REVIEW_SURFACE_LIVE_PROOF_V1 start -->'
const PR_REVIEW_SURFACE_LIVE_PROOF_END_MARKER = '<!-- PR_REVIEW_SURFACE_LIVE_PROOF_V1 end -->'

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
    if (
      error.path === 'operation_index_ref.embedded_payload.operation.source.commit_id'
      || error.path === 'operation_index_ref.embedded_payload.verification.operation_source_resolver.target_commit'
      || error.path === 'pr_review_surface_live_proof_ref.proof_target_head_sha'
      || /^operation_index_ref\.embedded_payload\.verification\.operation_source_resolver\.object_catalog\.(reviews_by_id|review_comments_by_id)\.[^.]+\.commit_id$/u.test(error.path)
    ) {
      return false
    }
    return true
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

function extractLiveGithubCommentFixtures(markdown) {
  const lines = markdown.split('\n')
  const fixtures = new Map()
  for (let i = 0; i < lines.length; i += 1) {
    const startMatch = lines[i].match(/^<!-- LIVE_GITHUB_COMMENT_FIXTURE url=(?<url>\S+) start -->$/u)
    if (!startMatch?.groups?.url) {
      continue
    }
    const openFenceLine = lines[i + 1] ?? ''
    const openFenceMatch = openFenceLine.match(/^(`{3,})text\s*$/u)
    if (!openFenceMatch) {
      continue
    }
    const fence = openFenceMatch[1]
    let closingFenceLine = -1
    let endMarkerLine = -1
    for (let j = i + 2; j < lines.length; j += 1) {
      if (closingFenceLine === -1 && lines[j] === fence) {
        closingFenceLine = j
        continue
      }
      if (closingFenceLine !== -1 && lines[j] === '<!-- LIVE_GITHUB_COMMENT_FIXTURE end -->') {
        endMarkerLine = j
        break
      }
    }
    if (closingFenceLine === -1 || endMarkerLine === -1) {
      continue
    }
    fixtures.set(startMatch.groups.url, lines.slice(i + 2, closingFenceLine).join('\n'))
    i = endMarkerLine
  }
  return fixtures
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
    if (ref.ref === proof?.pr_review_surface_live_proof_ref?.comment_url) {
      return typeof ref.digest === 'string' && ref.digest === proof?.pr_review_surface_live_proof_ref?.payload_digest
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

function parseGitHubCommentUrl(url) {
  if (typeof url !== 'string') {
    return null
  }
  const match = url.match(/^https:\/\/github\.com\/(?<repo>[A-Za-z0-9_.-]+\/[A-Za-z0-9_.-]+)\/(?<segment>issues|pull)\/(?<number>[0-9]+)#issuecomment-(?<commentId>[0-9]+)$/u)
  if (!match?.groups) {
    return null
  }
  return {
    repo: match.groups.repo,
    targetKind: match.groups.segment === 'issues' ? 'issue' : 'pull_request',
    targetNumber: Number(match.groups.number),
    commentId: Number(match.groups.commentId),
  }
}

function isPrReviewOperationKind(kind) {
  return [
    'pr_review_submitted',
    'pr_review_comment_created',
    'pr_review_thread_resolved',
  ].includes(kind)
}

function getPrReviewOperationKind(proof) {
  if (isPrReviewOperationKind(proof?.operation_index_ref?.embedded_payload?.operation?.kind)) {
    return proof.operation_index_ref.embedded_payload.operation.kind
  }
  return 'pr_review_comment_created'
}

function isPrReviewLiveProofRequired(proof) {
  return proof?.target?.kind === 'pull_request'
}

function normalizeValidationError(error) {
  return {
    path: error.path,
    code: error.code,
    message: error.message,
  }
}

async function resolveGithubCommentBody(commentUrl, fixtureBodies, githubClient) {
  if (fixtureBodies.has(commentUrl)) {
    return { ok: true, body: fixtureBodies.get(commentUrl), source: 'fixture' }
  }
  const parsed = parseGitHubCommentUrl(commentUrl)
  if (!parsed) {
    return {
      ok: false,
      error: {
        path: 'comment_url',
        code: 'comment_url.invalid',
        message: `comment_url is not a valid GitHub issue comment URL: ${commentUrl}`,
      },
    }
  }
  if (!githubClient) {
    return {
      ok: false,
      error: {
        path: 'comment_url',
        code: 'comment_fetch.unavailable',
        message: `live GitHub comment fetch is required for ${commentUrl}`,
      },
    }
  }
  try {
    const response = await githubClient.getIssueComment({
      repo: parsed.repo,
      commentId: parsed.commentId,
    })
    return {
      ok: true,
      body: typeof response?.body === 'string' ? response.body : '',
      source: 'github',
    }
  } catch (error) {
    return {
      ok: false,
      error: {
        path: 'comment_url',
        code: 'comment_fetch.failed',
        message: error instanceof Error ? error.message : String(error),
      },
    }
  }
}

function validatePrReviewSurfaceLiveProofShape(payload) {
  const errors = []
  const topLevelKeys = new Set([
    'schema',
    'repo',
    'proof_target_pr',
    'proof_target_head_sha',
    'contract_snapshot_url',
    'evidence_target',
    'operation_index',
    'selected_objects',
    'pagination',
    'projection_digest',
    'target_commit',
    'public_safe',
  ])
  function requireString(path, value) {
    if (typeof value !== 'string' || value.length === 0) {
      errors.push({ path, code: 'schema.invalid', message: `${path} must be a non-empty string` })
    }
  }
  function requireBoolean(path, value) {
    if (typeof value !== 'boolean') {
      errors.push({ path, code: 'schema.invalid', message: `${path} must be a boolean` })
    }
  }
  function requireInteger(path, value) {
    if (!Number.isInteger(value) || value <= 0) {
      errors.push({ path, code: 'schema.invalid', message: `${path} must be a positive integer` })
    }
  }
  function requireExactKeys(path, value, allowedKeys) {
    if (!value || typeof value !== 'object' || Array.isArray(value)) {
      errors.push({ path, code: 'schema.invalid', message: `${path} must be an object` })
      return
    }
    for (const key of Object.keys(value)) {
      if (!allowedKeys.has(key)) {
        errors.push({ path: `${path}.${key}`, code: 'schema.invalid', message: `${path}.${key} is not allowed` })
      }
    }
  }

  if (payload?.schema !== 'PR_REVIEW_SURFACE_LIVE_PROOF_V1') {
    errors.push({ path: 'schema', code: 'schema.invalid', message: 'schema must be PR_REVIEW_SURFACE_LIVE_PROOF_V1' })
  }
  requireExactKeys('root', payload, topLevelKeys)
  requireString('repo', payload?.repo)
  requireInteger('proof_target_pr', payload?.proof_target_pr)
  requireString('proof_target_head_sha', payload?.proof_target_head_sha)
  requireString('contract_snapshot_url', payload?.contract_snapshot_url)
  requireExactKeys('evidence_target', payload?.evidence_target, new Set(['kind', 'number']))
  requireString('evidence_target.kind', payload?.evidence_target?.kind)
  requireInteger('evidence_target.number', payload?.evidence_target?.number)
  requireExactKeys('operation_index', payload?.operation_index, new Set([
    'comment_url',
    'payload_digest',
    'revalidated',
    'payload_digest_valid',
    'schema_valid',
    'semantic_valid',
    'target_tuple_valid',
  ]))
  requireString('operation_index.comment_url', payload?.operation_index?.comment_url)
  requireString('operation_index.payload_digest', payload?.operation_index?.payload_digest)
  requireBoolean('operation_index.revalidated', payload?.operation_index?.revalidated)
  requireBoolean('operation_index.payload_digest_valid', payload?.operation_index?.payload_digest_valid)
  requireBoolean('operation_index.schema_valid', payload?.operation_index?.schema_valid)
  requireBoolean('operation_index.semantic_valid', payload?.operation_index?.semantic_valid)
  requireBoolean('operation_index.target_tuple_valid', payload?.operation_index?.target_tuple_valid)
  requireExactKeys('selected_objects', payload?.selected_objects, new Set(['review_id', 'review_comment_id', 'review_thread_node_id']))
  requireInteger('selected_objects.review_id', payload?.selected_objects?.review_id)
  requireInteger('selected_objects.review_comment_id', payload?.selected_objects?.review_comment_id)
  requireString('selected_objects.review_thread_node_id', payload?.selected_objects?.review_thread_node_id)
  requireExactKeys('pagination', payload?.pagination, new Set(['reviews_complete', 'review_comments_complete', 'review_threads_complete', 'thread_comments_complete']))
  requireString('projection_digest', payload?.projection_digest)
  requireString('target_commit', payload?.target_commit)
  requireBoolean('public_safe', payload?.public_safe)
  for (const key of ['reviews_complete', 'review_comments_complete', 'review_threads_complete', 'thread_comments_complete']) {
    requireBoolean(`pagination.${key}`, payload?.pagination?.[key])
  }
  return { valid: errors.length === 0, errors }
}

function compareLiveProofAgainstOperationIndex(liveProofPayload, operationIndexPayload, proof, fetchedDigest) {
  const errors = []
  const resolver = operationIndexPayload?.verification?.operation_source_resolver
  const source = operationIndexPayload?.operation?.source
  const opKind = operationIndexPayload?.operation?.kind
  const reviewObject = resolver?.object_catalog?.reviews_by_id?.[String(source?.review_id)]
  const reviewCommentObject = resolver?.object_catalog?.review_comments_by_id?.[String(source?.comment_id)]
  const threadObject = resolver?.object_catalog?.review_threads_by_node_id?.[source?.thread_node_id ?? resolver?.source_catalog?.review_thread_node_ids?.[0]]

  if (liveProofPayload?.repo !== proof.repo) {
    errors.push({ path: 'repo', code: 'live_proof.repo_mismatch', message: 'live proof repo must match proof.repo' })
  }
  if (liveProofPayload?.evidence_target?.kind !== proof.target.kind || liveProofPayload?.evidence_target?.number !== proof.target.number) {
    errors.push({ path: 'evidence_target', code: 'live_proof.target_mismatch', message: 'live proof evidence_target must match proof.target' })
  }
  if (liveProofPayload?.operation_index?.comment_url !== proof.operation_index_ref.comment_url) {
    errors.push({ path: 'operation_index.comment_url', code: 'live_proof.operation_index_url_mismatch', message: 'live proof operation_index.comment_url must match proof.operation_index_ref.comment_url' })
  }
  if (liveProofPayload?.operation_index?.payload_digest !== proof.operation_index_ref.payload_digest) {
    errors.push({ path: 'operation_index.payload_digest', code: 'live_proof.operation_index_digest_mismatch', message: 'live proof operation_index.payload_digest must match proof.operation_index_ref.payload_digest' })
  }
  if (liveProofPayload?.operation_index?.payload_digest !== fetchedDigest) {
    errors.push({ path: 'operation_index.payload_digest', code: 'live_proof.fetched_digest_mismatch', message: 'live proof operation_index.payload_digest must match the fetched operation index payload digest' })
  }
  if (liveProofPayload?.projection_digest !== resolver?.evidence_projection_digest) {
    errors.push({ path: 'projection_digest', code: 'live_proof.projection_digest_mismatch', message: 'live proof projection_digest must match operation source resolver evidence_projection_digest' })
  }
  if (liveProofPayload?.target_commit !== resolver?.target_commit) {
    errors.push({ path: 'target_commit', code: 'live_proof.target_commit_mismatch', message: 'live proof target_commit must match operation source resolver target_commit' })
  }
  if (liveProofPayload?.selected_objects?.review_id !== reviewObject?.id) {
    errors.push({ path: 'selected_objects.review_id', code: 'live_proof.review_id_mismatch', message: 'live proof selected review_id must match object catalog' })
  }
  const expectedReviewCommentId = opKind === 'pr_review_comment_created' ? source?.comment_id : resolver?.source_catalog?.review_comment_ids?.[0]
  if (liveProofPayload?.selected_objects?.review_comment_id !== expectedReviewCommentId) {
    errors.push({ path: 'selected_objects.review_comment_id', code: 'live_proof.review_comment_id_mismatch', message: 'live proof selected review_comment_id must match object catalog' })
  }
  const expectedThreadId = opKind === 'pr_review_thread_resolved' ? source?.thread_node_id : resolver?.source_catalog?.review_thread_node_ids?.[0]
  if (liveProofPayload?.selected_objects?.review_thread_node_id !== expectedThreadId) {
    errors.push({ path: 'selected_objects.review_thread_node_id', code: 'live_proof.review_thread_id_mismatch', message: 'live proof selected review_thread_node_id must match object catalog' })
  }
  for (const key of ['reviews_complete', 'review_comments_complete', 'review_threads_complete', 'thread_comments_complete']) {
    if (liveProofPayload?.pagination?.[key] !== resolver?.pagination?.[key]) {
      errors.push({ path: `pagination.${key}`, code: 'live_proof.pagination_mismatch', message: `live proof pagination.${key} must match operation source resolver` })
    }
  }
  if (liveProofPayload?.public_safe !== true) {
    errors.push({ path: 'public_safe', code: 'live_proof.public_safe_required', message: 'live proof public_safe must be true' })
  }
  if (threadObject && liveProofPayload?.selected_objects?.review_thread_node_id !== threadObject.thread_node_id) {
    errors.push({ path: 'selected_objects.review_thread_node_id', code: 'live_proof.review_thread_object_mismatch', message: 'live proof review thread id must match fetched thread object' })
  }
  if (reviewCommentObject && expectedReviewCommentId === reviewCommentObject.id && liveProofPayload?.selected_objects?.review_comment_id !== reviewCommentObject.id) {
    errors.push({ path: 'selected_objects.review_comment_id', code: 'live_proof.review_comment_object_mismatch', message: 'live proof review comment id must match fetched review comment object' })
  }
  return { valid: errors.length === 0, errors }
}

async function revalidateOperationIndexFromComment(proof, fixtureBodies, githubClient) {
  const fetchedComment = await resolveGithubCommentBody(proof.operation_index_ref.comment_url, fixtureBodies, githubClient)
  if (!fetchedComment.ok) {
    return {
      valid: false,
      errors: [{
        path: 'operation_index_ref.comment_url',
        code: 'operation_index.comment_fetch_failed',
        message: fetchedComment.error.message,
      }],
    }
  }
  const extraction = extractMarkedJsonBlock(fetchedComment.body, OPERATION_INDEX_COMMENT_START_MARKER, OPERATION_INDEX_COMMENT_END_MARKER)
  if (!extraction.ok) {
    return {
      valid: false,
      errors: [{
        path: 'operation_index_ref.comment_url',
        code: 'operation_index.comment_marker_missing',
        message: extraction.error.message,
      }],
    }
  }
  const fetchedPayload = extraction.payload
  const validation = validateAgentOperationSessionIndex(fetchedPayload)
  const errors = validation.errors.map((error) => ({
    path: `operation_index_ref.live_payload${error.path === 'root' ? '' : `.${error.path}`}`,
    code: 'operation_index.validation_failed',
    message: error.message,
  }))
  const recomputedDigest = computeAgentOperationSessionIndexPayloadDigest(fetchedPayload)
  if (proof.operation_index_ref.payload_digest !== recomputedDigest) {
    errors.push({
      path: 'operation_index_ref.payload_digest',
      code: 'operation_index.payload_digest_mismatch',
      message: 'operation_index_ref.payload_digest does not match the live-fetched operation index payload digest',
    })
  }
  if (fetchedPayload?.repo !== proof.repo || fetchedPayload?.parent_issue !== proof.parent_issue) {
    errors.push({
      path: 'operation_index_ref.live_payload',
      code: 'operation_index.identity_mismatch',
      message: 'live-fetched operation index payload must match proof.repo and proof.parent_issue',
    })
  }
  if (fetchedPayload?.target?.kind !== proof.target?.kind || fetchedPayload?.target?.number !== proof.target?.number) {
    errors.push({
      path: 'operation_index_ref.live_payload.target',
      code: 'operation_index.target_mismatch',
      message: 'live-fetched operation index payload target must match proof.target',
    })
  }
  return {
    valid: errors.length === 0,
    errors,
    payload: fetchedPayload,
    payloadDigest: recomputedDigest,
  }
}

async function revalidatePrReviewSurfaceLiveProofArtifact(proof, operationIndexPayload, operationIndexPayloadDigest, fixtureBodies, githubClient) {
  if (!proof?.pr_review_surface_live_proof_ref) {
    return {
      valid: false,
      errors: [{
        path: 'pr_review_surface_live_proof_ref',
        code: 'live_proof.required',
        message: 'pr_review_surface_live_proof_ref is required for PR review live proof validation',
      }],
    }
  }
  const fetchedComment = await resolveGithubCommentBody(proof.pr_review_surface_live_proof_ref.comment_url, fixtureBodies, githubClient)
  if (!fetchedComment.ok) {
    return {
      valid: false,
      errors: [{
        path: 'pr_review_surface_live_proof_ref.comment_url',
        code: 'live_proof.comment_fetch_failed',
        message: fetchedComment.error.message,
      }],
    }
  }
  const extraction = extractMarkedJsonBlock(fetchedComment.body, PR_REVIEW_SURFACE_LIVE_PROOF_START_MARKER, PR_REVIEW_SURFACE_LIVE_PROOF_END_MARKER)
  if (!extraction.ok) {
    return {
      valid: false,
      errors: [{
        path: 'pr_review_surface_live_proof_ref.comment_url',
        code: 'live_proof.comment_marker_missing',
        message: extraction.error.message,
      }],
    }
  }
  const payload = extraction.payload
  const errors = validatePrReviewSurfaceLiveProofShape(payload).errors
  const recomputedDigest = computeChatgptRetroExecutionProofDigest(payload)
  if (proof.pr_review_surface_live_proof_ref.payload_digest !== recomputedDigest) {
    errors.push({
      path: 'pr_review_surface_live_proof_ref.payload_digest',
      code: 'live_proof.payload_digest_mismatch',
      message: 'pr_review_surface_live_proof_ref.payload_digest must match the fetched live proof payload digest',
    })
  }
  if (proof.pr_review_surface_live_proof_ref.proof_target_head_sha !== payload.proof_target_head_sha) {
    errors.push({
      path: 'proof_target_head_sha',
      code: 'live_proof.head_sha_mismatch',
      message: 'live proof proof_target_head_sha must match pr_review_surface_live_proof_ref.proof_target_head_sha',
    })
  }
  if (proof.pr_review_surface_live_proof_ref.contract_snapshot_url !== payload.contract_snapshot_url) {
    errors.push({
      path: 'contract_snapshot_url',
      code: 'live_proof.contract_snapshot_mismatch',
      message: 'live proof contract_snapshot_url must match pr_review_surface_live_proof_ref.contract_snapshot_url',
    })
  }
  const liveProofCommentTarget = parseGitHubCommentUrl(proof.pr_review_surface_live_proof_ref.comment_url)
  if (!liveProofCommentTarget || liveProofCommentTarget.repo !== proof.repo || liveProofCommentTarget.targetKind !== 'pull_request' || liveProofCommentTarget.targetNumber !== payload.proof_target_pr) {
    errors.push({
      path: 'pr_review_surface_live_proof_ref.comment_url',
      code: 'live_proof.comment_url_target_mismatch',
      message: 'pr_review_surface_live_proof_ref.comment_url must match proof.repo and payload.proof_target_pr',
    })
  }
  errors.push(...compareLiveProofAgainstOperationIndex(payload, operationIndexPayload, proof, operationIndexPayloadDigest).errors)
  return {
    valid: errors.length === 0,
    errors,
    payload,
  }
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
    const operationIndexRevalidationMode = proof.operation_index_ref?.revalidation_mode ?? 'legacy_comment_digest_only'
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

    if (operationIndexRevalidationMode === 'embedded_payload' && !proof.operation_index_ref?.embedded_payload) {
      errors.push({
        path: 'operation_index_ref.embedded_payload',
        code: 'operation_index.embedded_payload_required',
        message: 'operation_index_ref.embedded_payload is required until live comment fetch/revalidation is implemented',
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

      if (
        isPrReviewOperationKind(proof.operation_index_ref.embedded_payload?.operation?.kind)
        && proof.operation_index_ref.embedded_payload?.verification?.operation_source_resolver?.status !== 'resolved'
      ) {
        errors.push({
          path: 'operation_index_ref.embedded_payload.verification.operation_source_resolver.status',
          code: 'operation_index.source_resolver_unresolved',
          message: 'embedded operation index payload must carry verification.operation_source_resolver.status = "resolved"',
        })
      }

      if (
        (operationIndexRevalidationMode === 'embedded_payload' || isPrReviewOperationKind(proof.operation_index_ref.embedded_payload?.operation?.kind))
        && (proof.operation_index_ref.embedded_payload?.repo !== proof.repo || proof.operation_index_ref.embedded_payload?.parent_issue !== proof.parent_issue)
      ) {
        errors.push({
          path: 'operation_index_ref.embedded_payload',
          code: 'operation_index.identity_mismatch',
          message: 'embedded operation index payload must match proof.repo and proof.parent_issue',
        })
      }

      if (
        (operationIndexRevalidationMode === 'embedded_payload' || isPrReviewOperationKind(proof.operation_index_ref.embedded_payload?.operation?.kind))
        && (proof.operation_index_ref.embedded_payload?.target?.kind !== proof.target?.kind || proof.operation_index_ref.embedded_payload?.target?.number !== proof.target?.number)
      ) {
        errors.push({
          path: 'operation_index_ref.embedded_payload.target',
          code: 'operation_index.target_mismatch',
          message: 'embedded operation index payload target must match proof.target',
        })
      }
    }

    const operationIndexCommentTarget = parseGitHubCommentUrl(proof.operation_index_ref?.comment_url)
    if (!operationIndexCommentTarget || operationIndexCommentTarget.repo !== proof.repo || operationIndexCommentTarget.targetKind !== proof.target.kind || operationIndexCommentTarget.targetNumber !== proof.target.number) {
      errors.push({
        path: 'operation_index_ref.comment_url',
        code: 'operation_index.comment_url_target_mismatch',
        message: 'operation_index_ref.comment_url must match proof.repo and proof.target',
      })
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

    if (proof.chatgpt_context.resolve_live_status !== proof.chatgpt_context.resolver_evidence.status) {
      errors.push({
        path: 'chatgpt_context.resolver_evidence.status',
        code: 'resolver_evidence.status_mismatch',
        message: 'chatgpt_context.resolve_live_status must match chatgpt_context.resolver_evidence.status',
      })
    }

    if (proof.chatgpt_context.resolve_live_status === 'resolved' && proof.chatgpt_context.resolver_evidence.page_budget_exhausted !== false) {
      errors.push({
        path: 'chatgpt_context.resolver_evidence.page_budget_exhausted',
        code: 'resolver_evidence.page_budget_exhausted',
        message: 'page_budget_exhausted must be false when chatgpt_context.resolve_live_status is "resolved"',
      })
    }

    if (proof.chatgpt_context.resolve_live_status === 'resolved' && proof.chatgpt_context.resolver_evidence.reference_page_budget_exhausted !== false) {
      errors.push({
        path: 'chatgpt_context.resolver_evidence.reference_page_budget_exhausted',
        code: 'resolver_evidence.reference_page_budget_exhausted',
        message: 'reference_page_budget_exhausted must be false when chatgpt_context.resolve_live_status is "resolved"',
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

export async function validateChatgptRetroE2eProofMarkdownLive(markdown, { githubClient = null } = {}) {
  const proofExtraction = extractMarkedJsonBlock(markdown, PROOF_START_MARKER, PROOF_END_MARKER)
  if (!proofExtraction.ok) {
    return { valid: false, errors: [proofExtraction.error] }
  }
  const resultExtraction = extractMarkedJsonBlock(markdown, RESULT_START_MARKER, RESULT_END_MARKER)
  if (!resultExtraction.ok) {
    return { valid: false, errors: [resultExtraction.error] }
  }
  const baseResult = validateChatgptRetroExecutionProof(proofExtraction.payload, resultExtraction.payload)
  const proof = proofExtraction.payload
  const fixtureBodies = extractLiveGithubCommentFixtures(markdown)
  const client = githubClient ?? (fixtureBodies.size === 0 ? new GhCliIssueCommentsClient() : null)
  const errors = [...baseResult.errors]

  if (proof?.operation_index_ref?.revalidation_mode === 'live_comment_fetch') {
    const operationIndexResult = await revalidateOperationIndexFromComment(proof, fixtureBodies, client)
    errors.push(...operationIndexResult.errors)
    if (operationIndexResult.valid && isPrReviewLiveProofRequired(proof)) {
      const liveProofResult = await revalidatePrReviewSurfaceLiveProofArtifact(
        proof,
        operationIndexResult.payload,
        operationIndexResult.payloadDigest,
        fixtureBodies,
        client,
      )
      errors.push(...liveProofResult.errors)
    } else if (isPrReviewLiveProofRequired(proof) && !proof?.pr_review_surface_live_proof_ref) {
      errors.push({
        path: 'pr_review_surface_live_proof_ref',
        code: 'live_proof.required',
        message: 'pr_review_surface_live_proof_ref is required for PR review live proof validation',
      })
    }
  }

  return { valid: errors.length === 0, errors: errors.map(normalizeValidationError) }
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
    const result = await validateChatgptRetroE2eProofMarkdownLive(markdown)
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
