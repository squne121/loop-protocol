#!/usr/bin/env node
// generate-retro-live-verification.mjs
//
// Trusted expectation manifest generator for `retro_live_verification/v2`
// (Issue #1709, prerequisite of #1415 / parent #1153).
//
// This script never re-derives its expectation values from a resolver's own
// live execution output at verification time. Instead, it accepts already
// -resolved values (obtained once, out of band, e.g. from a prior
// `chatgpt-retro-context:resolve-live` run whose output an operator has
// reviewed) as CLI input and freezes them into a schema-conformant manifest
// file that `post-retro-live-verification.mjs` / `check-retro-live-
// verification.mjs` consume later. This breaks the circularity of a
// resolver asserting against its own most-recent output.
//
// Usage:
//   node scripts/generate-retro-live-verification.mjs \
//     --repo owner/name --target-type issue --target-number 123 \
//     --parent-issue 1153 --marker-comment-url https://... \
//     --expected-digest <64hex> --expected-payload-digest <64hex> \
//     --expected-matched-comment-count 3 \
//     --review-artifact-ref https://... --reviewed-head-sha <40hex> \
//     --issue-number 1415 --trusted-actor github-actions[bot],squne121 \
//     --out artifacts/retro-live-verification-manifest.json
//
// pnpm run retro-live-verification:generate -- <flags above>

import { createHash } from 'node:crypto'
import { spawnSync } from 'node:child_process'
import { mkdir, readFile, writeFile } from 'node:fs/promises'
import { dirname, resolve as resolvePath } from 'node:path'
import { fileURLToPath } from 'node:url'

import { CliError, parseArgs, usageError } from './agent-logs/lib/args.mjs'

export const SCHEMA = 'retro_live_verification/v2'
const SCRIPT_DIR = dirname(fileURLToPath(import.meta.url))
export const REPO_ROOT = resolvePath(SCRIPT_DIR, '..')
const SCHEMA_FILE = resolvePath(REPO_ROOT, 'docs/schemas/retro-live-verification.schema.json')
export const GENERATOR_VERSION = 'retro-live-verification-generator/1'

// Allowed Paths declared in Issue #1709 (Machine-Readable Contract). Frozen
// here as the `execution_boundary.allowed_paths` value embedded in every
// manifest so producer/verifier consumers can assert they never operate
// outside the contract that generated them.
export const RETRO_LIVE_VERIFICATION_ALLOWED_PATHS = Object.freeze([
  'package.json',
  'docs/schemas/retro-live-verification.schema.json',
  'scripts/generate-retro-live-verification.mjs',
  'scripts/check-retro-live-verification.mjs',
  'scripts/post-retro-live-verification.mjs',
  'tests/retro-live-verification.test.ts',
  'tests/fixtures/retro-live-verification/',
  '.github/workflows/retro-live-verification.yml',
  '.claude/skills/issue-contract-review/scripts/pnpm_gate_registry.py',
  '.claude/skills/issue-contract-review/tests/test_pnpm_gate_security_boundary.py',
])

const GITHUB_LOGIN_PATTERN = /^[A-Za-z0-9](?:[A-Za-z0-9-]{0,37}[A-Za-z0-9])?(?:\[bot\])?$/u

let Ajv2020
let addFormats
async function loadAjv() {
  if (Ajv2020 && addFormats) {
    return { Ajv2020, addFormats }
  }
  const ajvMod = await import('ajv/dist/2020.js')
  const formatsMod = await import('ajv-formats')
  Ajv2020 = ajvMod.default
  addFormats = formatsMod.default
  return { Ajv2020, addFormats }
}

/**
 * Deterministic canonical-json-v1 serialization: object keys are sorted
 * recursively (depth-first) before stringification, arrays preserve order.
 * This is intentionally a minimal canonicalization profile (not full RFC
 * 8785); the schema's `canonicalization_profile.algorithm` field records
 * this choice explicitly so a future RFC 8785 migration is a visible,
 * versioned change rather than a silent digest drift (Issue #1709 Out of
 * Scope / OWNER review P1-6 follow-up).
 */
export function canonicalize(value) {
  if (Array.isArray(value)) {
    return value.map((item) => canonicalize(item))
  }
  if (value !== null && typeof value === 'object') {
    const sortedKeys = Object.keys(value).sort()
    const result = {}
    for (const key of sortedKeys) {
      result[key] = canonicalize(value[key])
    }
    return result
  }
  return value
}

export function canonicalJsonStringify(value) {
  return JSON.stringify(canonicalize(value))
}

export function sha256Hex(text) {
  return createHash('sha256').update(text, 'utf8').digest('hex')
}

export function buildOwnershipMarker({ repo, issueNumber }) {
  return `<!-- retro_live_verification:v1 repo=${repo} issue=${issueNumber} -->`
}

/**
 * Builds the canonical comment body posted by post-retro-live-verification
 * and re-checked byte-exact by check-retro-live-verification: a stable
 * ownership marker line, a digest marker line binding the canonicalized
 * assertion payload, and the fenced JSON payload itself.
 */
export function computeCanonicalCommentBody({ contextAssertions, prReviewBinding, ownershipMarker }) {
  const payload = { context_assertions: contextAssertions, pr_review_binding: prReviewBinding }
  const canonicalPayloadJson = canonicalJsonStringify(payload)
  const bodyDigest = sha256Hex(canonicalPayloadJson)
  const body = [
    ownershipMarker,
    `<!-- retro_live_verification_digest:v1 sha256=${bodyDigest} -->`,
    '',
    '```json',
    JSON.stringify(payload, null, 2),
    '```',
  ].join('\n')
  return { body, bodyDigest, canonicalPayloadJson }
}

export async function validateManifestWithAjv(manifest) {
  const { Ajv2020: AjvCtor, addFormats: applyFormats } = await loadAjv()
  const ajv = new AjvCtor({ strict: true, allErrors: true })
  applyFormats(ajv)
  const schemaText = await readFile(SCHEMA_FILE, 'utf-8')
  const schema = JSON.parse(schemaText)
  const validate = ajv.compile(schema)
  const valid = validate(manifest)
  return {
    valid,
    errors: valid ? [] : (validate.errors ?? []).map((error) => ({
      path: error.instancePath || '/',
      keyword: error.keyword,
      message: error.message ?? 'schema validation failed',
    })),
  }
}

function resolveGeneratorCommit() {
  const result = spawnSync('git', ['-C', REPO_ROOT, 'rev-parse', '--verify', 'HEAD^{commit}'], { encoding: 'utf-8' })
  if (result.status !== 0 || typeof result.stdout !== 'string') {
    return null
  }
  const commit = result.stdout.trim()
  return /^[0-9a-f]{40}$/u.test(commit) ? commit : null
}

function normalizePositiveInteger(value, code) {
  if (!/^[1-9][0-9]*$/u.test(String(value))) {
    throw usageError(code, `${code} must be a positive integer`)
  }
  return Number(value)
}

function normalizeNonNegativeInteger(value, code) {
  if (!/^(0|[1-9][0-9]*)$/u.test(String(value))) {
    throw usageError(code, `${code} must be a non-negative integer`)
  }
  return Number(value)
}

function normalizeTargetType(value) {
  if (value !== 'issue' && value !== 'pull_request') {
    throw usageError('retro_live_verification.target_type', 'target-type must be issue or pull_request')
  }
  return value
}

function normalizeSha256Hex(value, code) {
  if (typeof value !== 'string' || !/^[a-f0-9]{64}$/iu.test(value)) {
    throw usageError(code, `${code} must be a 64-character lowercase hex sha256 digest`)
  }
  return value.toLowerCase()
}

/**
 * Normalizes a digest value to the `sha256:<64hex>` representation used by
 * the existing `chatgpt-retro-context:assert-live` consumer (Issue #1709 PR
 * review, P0-4). Accepts either a bare 64-hex digest or an already-prefixed
 * `sha256:<64hex>` value as input so CLI callers do not need to know which
 * representation an upstream resolver happened to emit, but always emits
 * the prefixed form so producer and consumer never disagree on wire format.
 */
export function toSha256Prefixed(value) {
  if (typeof value === 'string' && /^sha256:[a-f0-9]{64}$/iu.test(value)) {
    return `sha256:${value.slice('sha256:'.length).toLowerCase()}`
  }
  if (typeof value === 'string' && /^[a-f0-9]{64}$/iu.test(value)) {
    return `sha256:${value.toLowerCase()}`
  }
  return null
}

export function fromSha256Prefixed(value) {
  if (typeof value !== 'string') {
    return null
  }
  const match = value.match(/^sha256:(?<hex>[a-f0-9]{64})$/iu)
  return match?.groups ? match.groups.hex.toLowerCase() : null
}

function normalizeSha256PrefixedDigest(value, code) {
  const normalized = toSha256Prefixed(value)
  if (normalized === null) {
    throw usageError(code, `${code} must be a 64-character hex sha256 digest, optionally prefixed with "sha256:"`)
  }
  return normalized
}

function normalizeCommitSha(value, code) {
  if (typeof value !== 'string' || !/^[0-9a-f]{40}$/iu.test(value)) {
    throw usageError(code, `${code} must be a 40-character lowercase hex commit sha`)
  }
  return value.toLowerCase()
}

export function normalizeTrustedActorAllowlist(rawValue) {
  const entries = String(rawValue ?? '')
    .split(',')
    .map((entry) => entry.trim())
    .filter((entry) => entry.length > 0)
  if (entries.length === 0) {
    throw usageError('retro_live_verification.trusted_actor_allowlist', 'trusted-actor allowlist must contain at least one entry')
  }
  const deduplicated = [...new Set(entries)]
  for (const entry of deduplicated) {
    if (!GITHUB_LOGIN_PATTERN.test(entry)) {
      throw usageError('retro_live_verification.trusted_actor_allowlist', `trusted-actor entry is not a valid GitHub login: ${entry}`)
    }
  }
  return deduplicated
}

const CLI_OPTION_SPEC = {
  '--repo': { key: 'repo', required: true },
  '--target-type': { key: 'targetType', required: true },
  '--target-number': { key: 'targetNumber', required: true },
  '--parent-issue': { key: 'parentIssue', required: true },
  '--marker-comment-url': { key: 'markerCommentUrl', required: true },
  '--expected-digest': { key: 'expectedDigest', required: true },
  '--expected-payload-digest': { key: 'expectedPayloadDigest', required: true },
  '--expected-matched-comment-count': { key: 'expectedMatchedCommentCount', required: true },
  '--review-artifact-ref': { key: 'reviewArtifactRef', required: true },
  '--reviewed-head-sha': { key: 'reviewedHeadSha', required: true },
  '--selected-review-id': { key: 'selectedReviewId' },
  '--issue-number': { key: 'issueNumber', required: true },
  '--trusted-actor': { key: 'trustedActor', required: true },
  '--expected-previous-digest': { key: 'expectedPreviousDigest' },
  '--out': { key: 'out', required: true },
}

export function buildManifest(options) {
  const repo = options.repo
  const targetType = normalizeTargetType(options.targetType)
  const targetNumber = normalizePositiveInteger(options.targetNumber, 'retro_live_verification.target_number')
  const parentIssue = normalizePositiveInteger(options.parentIssue, 'retro_live_verification.parent_issue')
  const issueNumber = normalizePositiveInteger(options.issueNumber, 'retro_live_verification.issue_number')
  const expectedDigest = normalizeSha256PrefixedDigest(options.expectedDigest, 'retro_live_verification.expected_digest')
  const expectedPayloadDigest = normalizeSha256PrefixedDigest(options.expectedPayloadDigest, 'retro_live_verification.expected_payload_digest')
  const expectedMatchedCommentCount = normalizeNonNegativeInteger(options.expectedMatchedCommentCount, 'retro_live_verification.expected_matched_comment_count')
  const reviewedHeadSha = normalizeCommitSha(options.reviewedHeadSha, 'retro_live_verification.reviewed_head_sha')
  const selectedReviewId = options.selectedReviewId === undefined || options.selectedReviewId === null
    ? null
    : normalizePositiveInteger(options.selectedReviewId, 'retro_live_verification.selected_review_id')
  const trustedActorAllowlist = normalizeTrustedActorAllowlist(options.trustedActor)
  const expectedPreviousDigest = options.expectedPreviousDigest === undefined || options.expectedPreviousDigest === null || options.expectedPreviousDigest === ''
    ? null
    : normalizeSha256Hex(options.expectedPreviousDigest, 'retro_live_verification.expected_previous_digest')

  const contextAssertions = {
    repo,
    target_type: targetType,
    target_number: targetNumber,
    parent_issue: parentIssue,
    marker_comment_url: options.markerCommentUrl,
    expected_digest: expectedDigest,
    expected_payload_digest: expectedPayloadDigest,
    expected_matched_comment_count: expectedMatchedCommentCount,
  }
  const prReviewBinding = {
    review_artifact_ref: options.reviewArtifactRef,
    reviewed_head_sha: reviewedHeadSha,
    selected_review_id: selectedReviewId,
  }
  const ownershipMarker = buildOwnershipMarker({ repo, issueNumber })
  const { bodyDigest } = computeCanonicalCommentBody({ contextAssertions, prReviewBinding, ownershipMarker })

  return {
    schema: SCHEMA,
    context_assertions: contextAssertions,
    pr_review_binding: prReviewBinding,
    execution_boundary: {
      mode: 'generate',
      allowed_paths: [...RETRO_LIVE_VERIFICATION_ALLOWED_PATHS],
      trusted_actor_allowlist: trustedActorAllowlist,
    },
    canonical_comment: {
      repo,
      issue_number: issueNumber,
      ownership_marker: ownershipMarker,
      body_digest: bodyDigest,
      expected_previous_digest: expectedPreviousDigest,
    },
    runtime_provenance: {
      generated_at: new Date().toISOString(),
      generator_commit: resolveGeneratorCommit(),
      generator_version: GENERATOR_VERSION,
    },
    canonicalization_profile: {
      algorithm: 'canonical-json-v1',
      digest_encoding: 'sha256-hex',
    },
  }
}

async function runCli() {
  const options = parseArgs(process.argv.slice(2), CLI_OPTION_SPEC)
  const manifest = buildManifest(options)
  const validation = await validateManifestWithAjv(manifest)
  if (!validation.valid) {
    process.stdout.write(`${JSON.stringify({
      schema: SCHEMA,
      status: 'error',
      error_code: 'retro_live_verification.manifest_schema_invalid',
      errors: validation.errors,
    })}\n`)
    process.exitCode = 1
    return
  }

  if (options.out === '-') {
    process.stdout.write(`${JSON.stringify(manifest, null, 2)}\n`)
    return
  }
  const outPath = resolvePath(process.cwd(), options.out)
  await mkdir(dirname(outPath), { recursive: true })
  await writeFile(outPath, `${JSON.stringify(manifest, null, 2)}\n`, 'utf-8')
  process.stdout.write(`${JSON.stringify({ schema: SCHEMA, status: 'ok', out: outPath })}\n`)
}

const isDirectExecution = process.argv[1] === fileURLToPath(import.meta.url)
if (isDirectExecution) {
  runCli().catch((error) => {
    const isCliError = error instanceof CliError
    process.stdout.write(`${JSON.stringify({
      schema: SCHEMA,
      status: 'error',
      error_code: isCliError ? error.code : 'retro_live_verification.unexpected_error',
      error_message: error?.message ?? 'unexpected runtime failure',
    })}\n`)
    process.exitCode = isCliError ? error.exitCode : 2
  })
}
