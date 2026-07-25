#!/usr/bin/env node
// check-retro-live-verification.mjs
//
// Verifier for the `retro_live_verification/v2` canonical comment (Issue
// #1709, prerequisite of #1415 / parent #1153).
//
// Fail-closed checks:
//   - the manifest itself validates against
//     docs/schemas/retro-live-verification.schema.json
//   - exactly one canonical comment exists on the target issue, its author
//     is a member of the manifest's trusted_actor_allowlist, its digest
//     marker matches the manifest's canonical_comment.body_digest, AND the
//     fenced JSON payload embedded in the comment is extracted, parsed, and
//     its canonical-json-v1 digest is *recomputed* (never trusted from the
//     marker line an attacker controls) and required to equal
//     canonical_comment.body_digest byte-for-byte. Trailing content after
//     the closing JSON fence is rejected (Issue #1709 PR review P0-3).
//   - the parsed payload's context_assertions / pr_review_binding are
//     required to deep-equal the manifest's own context_assertions /
//     pr_review_binding (defense in depth on top of the digest binding).
//   - for a pull_request target, the manifest's context_assertions
//     (repo/target/parent_issue/marker_comment_url/expected_digest/
//     expected_payload_digest/expected_matched_comment_count) are checked
//     against a live re-resolution by delegating to the existing, hardened
//     `chatgpt-retro-context:resolve-live` / `chatgpt-retro-context:assert-
//     live` implementation (scripts/assert-chatgpt-retro-context-live.mjs)
//     rather than re-implementing a second, weaker GraphQL pagination walk
//     (Issue #1709 PR review P0-4). This script never treats a subprocess
//     spawn failure, non-JSON stdout, or a non-"pass" assertion_status as a
//     passing result.
//   - the manifest's pr_review_binding (reviewed_head_sha / selected_
//     review_id / review_artifact_ref) is checked against a live-fetched
//     PR review object; a null/missing/malformed live response is treated
//     as a failure, never defaulted to a pass (Issue #1709 PR review P1-2).
//   - issue-comment listing distinguishes `pagination_exhausted` (link-
//     header pagination never reached a terminal page) from
//     `page_budget_exhausted` (a fixed page-count budget was hit before a
//     terminal page was observed) and fails closed on *either* flag (Issue
//     #1709 PR review P1-3).
//
// Usage (live):
//   node scripts/check-retro-live-verification.mjs \
//     --manifest-json artifacts/retro-live-verification-manifest.json
//
// Usage (fixture, for negative-path tests / CI without network):
//   node scripts/check-retro-live-verification.mjs \
//     --manifest-json <path> --execution-profile fixture \
//     --fixture-comments-json <path> \
//     --fixture-resolve-result-json <path> \
//     --fixture-pr-review-json <path>
//
// pnpm run retro-live-verification:verify -- <flags above>
//
// Usage (argument-free wrapper, Issue #1709 PR review P0-5 / AC7): the
// `retro-live-verification:verify` package.json script itself must remain
// the bare, argument-free CLI invocation the Issue #1709 Verification
// Command matches verbatim (`pnpm_gate_registry.py`'s exact-two-token gate
// never forwards extra argv). When invoked with *zero* CLI arguments, this
// script falls back to `DEFAULT_CHECK_ARGS` -- fixture execution profile
// against the checked-in
// `tests/fixtures/retro-live-verification/gate-manifest.json` /
// `gate-comments.json` -- so the bare invocation is a read-only,
// deterministic fixture check rather than a required-option usage error.
// Any explicit CLI argument still takes precedence and is used verbatim.

import { spawnSync } from 'node:child_process'
import { readFile } from 'node:fs/promises'
import { dirname, resolve as resolvePath } from 'node:path'
import { fileURLToPath } from 'node:url'

import { CliError, parseArgs, usageError } from './agent-logs/lib/args.mjs'
import {
  GhCliIssueCommentsClient,
  listAllIssueCommentsStructured,
} from './agent-logs/lib/github-comments.mjs'
import { canonicalJsonStringify, sha256Hex, validateManifestWithAjv } from './generate-retro-live-verification.mjs'

export const SCHEMA = 'retro_live_verification_check_result/v1'
const SCRIPT_DIR = dirname(fileURLToPath(import.meta.url))
const ASSERT_LIVE_SCRIPT_PATH = resolvePath(SCRIPT_DIR, 'assert-chatgpt-retro-context-live.mjs')
const DEFAULT_SUBPROCESS_TIMEOUT_MS = 30000
const DEFAULT_MAX_BUFFER = 10 * 1024 * 1024
const JSON_FENCE_PATTERN = /```json\r?\n(?<payload>[\s\S]*?)\r?\n```/u

// Argument-free wrapper default (Issue #1709 PR review P0-5 / AC7): mirrors
// the checked-in fixture pair used by the `retro-live-verification:verify`
// negative-path test suite so a bare `node
// scripts/check-retro-live-verification.mjs` (no CLI args) is a read-only,
// deterministic fixture verification rather than a required-option usage
// error. Only used when `process.argv` supplies zero flags; any explicit
// CLI argument overrides this default entirely.
export const DEFAULT_CHECK_ARGS = Object.freeze([
  '--manifest-json', 'tests/fixtures/retro-live-verification/gate-manifest.json',
  '--execution-profile', 'fixture',
  '--fixture-comments-json', 'tests/fixtures/retro-live-verification/gate-comments.json',
  // Issue #1415 AC2: the context-assertions binding now runs unconditionally
  // (both issue and pull_request targets), so the argument-free fixture
  // default must supply a matching fixture-resolve-result-json too.
  '--fixture-resolve-result-json', 'tests/fixtures/retro-live-verification/gate-resolve-result-issue.json',
])

const CLI_OPTION_SPEC = {
  '--manifest-json': { key: 'manifestJson', required: true },
  '--execution-profile': { key: 'executionProfile', defaultValue: 'live' },
  '--fixture-comments-json': { key: 'fixtureCommentsJson' },
  '--fixture-resolve-result-json': { key: 'fixtureResolveResultJson' },
  '--fixture-pr-review-json': { key: 'fixturePrReviewJson' },
}

/**
 * Fail-closed on *either* pagination completeness flag (Issue #1709 PR
 * review P1-3): `pagination_exhausted` (link-header pagination never
 * reached a terminal page) and `page_budget_exhausted` (a fixed page-count
 * budget was hit before a terminal page was observed) are distinct failure
 * modes and a caller that only checks one can silently miss a duplicate
 * marker sitting on a page beyond the checked budget.
 */
export function isPaginationRejected(listing) {
  return listing?.pagination_exhausted === true || listing?.page_budget_exhausted === true
}

function normalizeExecutionProfile(value) {
  if (value !== 'live' && value !== 'fixture') {
    throw usageError('retro_live_verification_check.execution_profile', 'execution-profile must be live or fixture')
  }
  return value
}

function parseCanonicalCommentMarkers(comment, ownershipMarker) {
  const body = typeof comment?.body === 'string' ? comment.body : ''
  const lines = body.split('\n').map((line) => line.trim()).filter((line) => line.length > 0)
  const [firstLine, secondLine] = lines
  if (firstLine !== ownershipMarker) {
    return { matches: false, malformed: false }
  }
  const digestMatch = (secondLine ?? '').match(/^<!-- retro_live_verification_digest:v1 sha256=(?<digest>[a-f0-9]{64}) -->$/u)
  if (!digestMatch?.groups) {
    return { matches: true, malformed: true, body }
  }
  return { matches: true, malformed: false, digest: digestMatch.groups.digest, authorLogin: comment?.user?.login ?? null, body }
}

/**
 * Extracts and parses the fenced JSON payload from a canonical comment
 * body, requiring the payload to be the *only* content following the
 * digest marker line (aside from a single blank separator line): no extra
 * Markdown or ChatGPT-directed instructions may trail the closing fence
 * (Issue #1709 PR review P0-3, negative test 2).
 */
function extractCanonicalPayload(body, ownershipMarker) {
  const ownershipIndex = body.indexOf(ownershipMarker)
  if (ownershipIndex === -1) {
    return { ok: false, code: 'retro_live_verification_check.payload_ownership_marker_missing' }
  }
  const afterOwnership = body.slice(ownershipIndex + ownershipMarker.length)
  const fenceMatch = afterOwnership.match(JSON_FENCE_PATTERN)
  if (!fenceMatch?.groups) {
    return { ok: false, code: 'retro_live_verification_check.payload_json_fence_missing' }
  }
  const fenceEnd = afterOwnership.indexOf(fenceMatch[0]) + fenceMatch[0].length
  const trailing = afterOwnership.slice(fenceEnd)
  if (trailing.trim().length > 0) {
    return { ok: false, code: 'retro_live_verification_check.payload_trailing_content' }
  }
  let payload
  try {
    payload = JSON.parse(fenceMatch.groups.payload)
  } catch {
    return { ok: false, code: 'retro_live_verification_check.payload_json_invalid' }
  }
  return { ok: true, payload, rawPayloadText: fenceMatch.groups.payload }
}

function deepEqual(left, right) {
  return canonicalJsonStringify(left) === canonicalJsonStringify(right)
}

/**
 * Pure domain check over an already-fetched (or fixture-loaded) comment
 * list. Never converts "no canonical comment found" / "malformed" /
 * "untrusted author" / "stale digest" / "tampered payload" / "trailing
 * content" into a pass. The digest marker line's *claimed* value is never
 * trusted on its own -- the payload digest is always recomputed from the
 * extracted JSON and compared against the manifest's expectation.
 */
export function checkCanonicalComment({ manifest, comments }) {
  const errors = []
  const { ownership_marker: ownershipMarker, body_digest: expectedDigest } = manifest.canonical_comment
  const parsed = comments.map((comment) => ({ comment, ...parseCanonicalCommentMarkers(comment, ownershipMarker) }))
  const matches = parsed.filter((entry) => entry.matches)

  if (matches.some((entry) => entry.malformed)) {
    errors.push({ code: 'retro_live_verification_check.malformed_canonical_comment', message: 'a comment matches the ownership marker but its digest marker is malformed' })
    return { ok: false, errors }
  }
  if (matches.length !== 1) {
    errors.push({ code: 'retro_live_verification_check.matched_comment_count_mismatch', message: `expected exactly 1 canonical comment, found ${matches.length}` })
    return { ok: false, errors }
  }

  const [match] = matches
  if (!manifest.execution_boundary.trusted_actor_allowlist.includes(match.authorLogin)) {
    errors.push({ code: 'retro_live_verification_check.untrusted_marker_author', message: `canonical comment author ${match.authorLogin} is not in the trusted_actor_allowlist` })
  }
  if (match.digest !== expectedDigest) {
    errors.push({ code: 'retro_live_verification_check.stale_digest', message: `canonical comment digest marker ${match.digest} does not match manifest expectation ${expectedDigest}` })
  }

  const extraction = extractCanonicalPayload(match.body, ownershipMarker)
  if (!extraction.ok) {
    errors.push({ code: extraction.code, message: 'failed to extract a single, well-formed fenced JSON payload from the canonical comment body' })
    return { ok: false, errors }
  }

  const recomputedDigest = sha256Hex(canonicalJsonStringify(extraction.payload))
  if (recomputedDigest !== expectedDigest) {
    errors.push({
      code: 'retro_live_verification_check.payload_digest_recompute_mismatch',
      message: `recomputed canonical-json-v1 digest of the live payload (${recomputedDigest}) does not match manifest expectation (${expectedDigest}); the comment body was likely tampered with`,
    })
  }

  const payloadContextAssertions = extraction.payload?.context_assertions
  const payloadPrReviewBinding = extraction.payload?.pr_review_binding
  if (!deepEqual(payloadContextAssertions, manifest.context_assertions)) {
    errors.push({ code: 'retro_live_verification_check.payload_context_assertions_mismatch', message: 'live comment context_assertions does not deep-equal the manifest context_assertions' })
  }
  if (!deepEqual(payloadPrReviewBinding, manifest.pr_review_binding)) {
    errors.push({ code: 'retro_live_verification_check.payload_pr_review_binding_mismatch', message: 'live comment pr_review_binding does not deep-equal the manifest pr_review_binding' })
  }

  return { ok: errors.length === 0, errors }
}

// -- context_assertions live binding (delegates to the existing, hardened
// chatgpt-retro-context resolve-live / assert-live implementation instead
// of re-implementing pagination/null-safety a second time; Issue #1709 PR
// review P0-4). --

function classifySpawnFailure(spawnResult) {
  if (spawnResult.error) {
    return { code: 'retro_live_verification_check.context_assertions_spawn_failed', message: `assert-live subprocess failed to spawn or exceeded its timeout: ${spawnResult.error.message}` }
  }
  if (spawnResult.signal) {
    return { code: 'retro_live_verification_check.context_assertions_signal_terminated', message: `assert-live subprocess was terminated by signal ${spawnResult.signal}` }
  }
  return null
}

/**
 * Pure evaluation of an already-completed `assert-chatgpt-retro-context-
 * live.mjs` subprocess result. Dependency-injectable so spawn failure /
 * malformed-JSON / non-"pass" assertion_status handling is unit-testable
 * without an OS subprocess.
 */
export function evaluateContextAssertionsBinding(spawnResult) {
  const spawnFailure = classifySpawnFailure(spawnResult)
  if (spawnFailure) {
    return { ok: false, errors: [spawnFailure] }
  }
  const stdout = typeof spawnResult.stdout === 'string' ? spawnResult.stdout.trim() : ''
  let parsed = null
  if (stdout.length > 0) {
    try {
      parsed = JSON.parse(stdout)
    } catch {
      parsed = null
    }
  }
  if (parsed === null) {
    return {
      ok: false,
      errors: [{ code: 'retro_live_verification_check.context_assertions_invalid_json_output', message: 'assert-live subprocess did not emit a single parsable JSON object on stdout' }],
    }
  }
  if (parsed.assertion_status !== 'pass') {
    return {
      ok: false,
      errors: [{
        code: 'retro_live_verification_check.context_assertions_live_binding_failed',
        message: `assert-live reported assertion_status ${JSON.stringify(parsed.assertion_status)} (expected "pass")`,
        details: parsed.errors ?? [],
      }],
    }
  }
  return { ok: true, errors: [] }
}

function runContextAssertionsBindingSubprocess({ contextAssertions, executionProfile, fixtureResolveResultJson, timeoutMs = DEFAULT_SUBPROCESS_TIMEOUT_MS }) {
  const args = [
    ASSERT_LIVE_SCRIPT_PATH,
    '--repo', contextAssertions.repo,
    '--target-type', contextAssertions.target_type,
    '--target-number', String(contextAssertions.target_number),
    '--parent-issue', String(contextAssertions.parent_issue),
    '--marker-comment-url', contextAssertions.marker_comment_url,
    '--expected-digest', contextAssertions.expected_digest,
    '--expected-payload-digest', contextAssertions.expected_payload_digest,
    '--expected-matched-comment-count', String(contextAssertions.expected_matched_comment_count),
    '--execution-profile', executionProfile,
  ]
  if (executionProfile === 'fixture') {
    args.push('--fixture-resolve-result-json', fixtureResolveResultJson)
  }
  return spawnSync(process.execPath, args, {
    encoding: 'utf-8',
    stdio: ['ignore', 'pipe', 'pipe'],
    timeout: timeoutMs,
    maxBuffer: DEFAULT_MAX_BUFFER,
  })
}

// -- pr_review_binding live check (reviewed_head_sha / selected_review_id /
// review_artifact_ref). A null/missing live review response is a failure,
// never a default pass (Issue #1709 PR review P1-2). --

/**
 * Pure domain check. `review` is either a live-fetched (or fixture-loaded)
 * GitHub PR review object, or `null`/malformed if the live fetch failed or
 * returned an unexpected shape -- which must never be treated as "no
 * binding to check" when `selected_review_id` is non-null.
 */
export function verifyPrReviewBindingLive({ prReviewBinding, review }) {
  if (prReviewBinding.selected_review_id === null) {
    return { ok: true, errors: [], skipped: true }
  }
  const errors = []
  if (review === null || typeof review !== 'object' || Array.isArray(review)) {
    errors.push({ code: 'retro_live_verification_check.pr_review_binding_missing', message: 'expected a PR review object from the live API but received null/malformed shape' })
    return { ok: false, errors }
  }
  if (review.id !== prReviewBinding.selected_review_id) {
    errors.push({ code: 'retro_live_verification_check.pr_review_binding_id_mismatch', message: `live review id ${JSON.stringify(review.id)} does not match manifest selected_review_id ${prReviewBinding.selected_review_id}` })
  }
  if (review.commit_id !== prReviewBinding.reviewed_head_sha) {
    errors.push({ code: 'retro_live_verification_check.pr_review_binding_head_sha_mismatch', message: `live review commit_id ${JSON.stringify(review.commit_id)} does not match manifest reviewed_head_sha ${prReviewBinding.reviewed_head_sha}` })
  }
  const reviewUrl = typeof review.html_url === 'string' ? review.html_url : null
  if (reviewUrl !== prReviewBinding.review_artifact_ref) {
    errors.push({ code: 'retro_live_verification_check.pr_review_binding_artifact_ref_mismatch', message: `live review html_url ${JSON.stringify(reviewUrl)} does not match manifest review_artifact_ref ${prReviewBinding.review_artifact_ref}` })
  }
  return { ok: errors.length === 0, errors }
}

function fetchPullRequestReviewLive({ repo, pullNumber, reviewId }) {
  const result = spawnSync('gh', [
    'api',
    '-H', 'Accept: application/vnd.github+json',
    '-H', 'X-GitHub-Api-Version: 2022-11-28',
    `repos/${repo}/pulls/${pullNumber}/reviews/${reviewId}`,
  ], { encoding: 'utf-8' })
  if (result.status !== 0) {
    return null
  }
  try {
    const parsed = JSON.parse(result.stdout)
    return (parsed && typeof parsed === 'object' && !Array.isArray(parsed)) ? parsed : null
  } catch {
    return null
  }
}

async function loadJsonFile(filePath, code) {
  let raw
  try {
    raw = await readFile(filePath, 'utf-8')
  } catch (error) {
    throw new CliError(code, `failed to read ${filePath}: ${error.message}`, 2)
  }
  try {
    return JSON.parse(raw)
  } catch (error) {
    throw new CliError(code, `${filePath} is not valid JSON: ${error.message}`, 2)
  }
}

async function runCli() {
  const rawArgs = process.argv.slice(2)
  const options = parseArgs(rawArgs.length === 0 ? DEFAULT_CHECK_ARGS : rawArgs, CLI_OPTION_SPEC)
  const executionProfile = normalizeExecutionProfile(options.executionProfile)

  const manifestText = await readFile(options.manifestJson, 'utf-8')
  const manifest = JSON.parse(manifestText)
  const schemaValidation = await validateManifestWithAjv(manifest)
  if (!schemaValidation.valid) {
    process.stdout.write(`${JSON.stringify({ schema: SCHEMA, verification_status: 'error', error_code: 'retro_live_verification_check.manifest_schema_invalid', errors: schemaValidation.errors })}\n`)
    process.exitCode = 2
    return
  }

  let comments
  if (executionProfile === 'fixture') {
    if (!options.fixtureCommentsJson) {
      throw usageError('retro_live_verification_check.fixture_comments_json_required', '--fixture-comments-json is required when --execution-profile is fixture')
    }
    comments = await loadJsonFile(options.fixtureCommentsJson, 'retro_live_verification_check.fixture_comments_json_invalid')
  } else {
    const client = new GhCliIssueCommentsClient()
    const listing = await listAllIssueCommentsStructured(client, { repo: manifest.canonical_comment.repo, issueNumber: manifest.canonical_comment.issue_number })
    if (isPaginationRejected(listing)) {
      process.stdout.write(`${JSON.stringify({ schema: SCHEMA, verification_status: 'error', error_code: 'retro_live_verification_check.pagination_exhausted', errors: [] })}\n`)
      process.exitCode = 2
      return
    }
    comments = listing.comments
  }

  const commentCheck = checkCanonicalComment({ manifest, comments })
  const errors = [...commentCheck.errors]

  // Issue #1415 AC2: the assert-live context-assertion binding must run for
  // *both* target types. Prior to this fix, this block only executed when
  // `target_type === 'pull_request'`, so an issue-target manifest silently
  // skipped context-assertion verification entirely (contextAssertionsCheck
  // stayed at its `{ ok: true, errors: [] }` default) -- the exact defect
  // Issue #1415 names explicitly ("現行の『PR targetのみcontext assertionを
  // 実行』の挙動").
  if (executionProfile === 'fixture' && !options.fixtureResolveResultJson) {
    throw usageError('retro_live_verification_check.fixture_resolve_result_json_required', '--fixture-resolve-result-json is required when --execution-profile is fixture')
  }
  const contextAssertionsSpawnResult = runContextAssertionsBindingSubprocess({
    contextAssertions: manifest.context_assertions,
    executionProfile,
    fixtureResolveResultJson: options.fixtureResolveResultJson,
  })
  const contextAssertionsCheck = evaluateContextAssertionsBinding(contextAssertionsSpawnResult)
  errors.push(...contextAssertionsCheck.errors)

  let prReviewBindingCheck = { ok: true, errors: [] }
  if (manifest.context_assertions.target_type === 'pull_request' && manifest.pr_review_binding.selected_review_id !== null) {
    let review
    if (executionProfile === 'fixture') {
      if (!options.fixturePrReviewJson) {
        throw usageError('retro_live_verification_check.fixture_pr_review_json_required', '--fixture-pr-review-json is required when --execution-profile is fixture, target-type is pull_request, and selected_review_id is non-null')
      }
      review = await loadJsonFile(options.fixturePrReviewJson, 'retro_live_verification_check.fixture_pr_review_json_invalid')
    } else {
      review = fetchPullRequestReviewLive({
        repo: manifest.canonical_comment.repo,
        pullNumber: manifest.context_assertions.target_number,
        reviewId: manifest.pr_review_binding.selected_review_id,
      })
    }
    prReviewBindingCheck = verifyPrReviewBindingLive({ prReviewBinding: manifest.pr_review_binding, review })
    errors.push(...prReviewBindingCheck.errors)
  }

  const ok = commentCheck.ok && contextAssertionsCheck.ok && prReviewBindingCheck.ok
  process.stdout.write(`${JSON.stringify({
    schema: SCHEMA,
    verification_status: ok ? 'pass' : 'fail',
    execution_profile: executionProfile,
    checked_at: new Date().toISOString(),
    errors,
  })}\n`)
  process.exitCode = ok ? 0 : 1
}

const isDirectExecution = process.argv[1] === fileURLToPath(import.meta.url)
if (isDirectExecution) {
  runCli().catch((error) => {
    const isCliError = error instanceof CliError
    process.stdout.write(`${JSON.stringify({
      schema: SCHEMA,
      verification_status: 'error',
      error_code: isCliError ? error.code : 'retro_live_verification_check.unexpected_error',
      error_message: error?.message ?? 'unexpected runtime failure',
      errors: [],
    })}\n`)
    process.exitCode = isCliError ? error.exitCode : 2
  })
}
