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
  fetchPullRequestReviewSurfaceLive,
  GhCliIssueCommentsClient,
  listAllIssueCommentsStructured,
} from './agent-logs/lib/github-comments.mjs'
import {
  CANONICALIZATION_PROFILE_LOOP_V1,
  canonicalJsonStringify,
  computeResolvedCommentSetDigest,
  loadAjv,
  sha256Hex,
  validateManifestWithAjv,
} from './generate-retro-live-verification.mjs'

export const SCHEMA = 'retro_live_verification_check_result/v1'
const SCRIPT_DIR = dirname(fileURLToPath(import.meta.url))
const REPO_ROOT = resolvePath(SCRIPT_DIR, '..')
const ASSERT_LIVE_SCRIPT_PATH = resolvePath(SCRIPT_DIR, 'assert-chatgpt-retro-context-live.mjs')
// Issue #1415 AC10-11 (additive, optional gate).
const RETROSPECTIVE_RESULT_SCHEMA_FILE = resolvePath(REPO_ROOT, 'docs/schemas/chatgpt-retrospective-result.schema.json')
const RETROSPECTIVE_RESULT_SCHEMA_ID = 'chatgpt_retrospective_result/v1'
// Issue #1415 (dual-target bundle, additive standalone mode): a distinct
// schema/file from retro_live_verification/v2 (retro-live-verification.
// schema.json) -- v3 is never a mutation of a v2 manifest, so v2 producers/
// consumers are unaffected by this constant's existence.
const DUAL_TARGET_BUNDLE_SCHEMA_FILE = resolvePath(REPO_ROOT, 'docs/schemas/retro-live-verification-dual-target.schema.json')
const DUAL_TARGET_BUNDLE_SCHEMA_ID = 'retro_live_verification/v3'
// Boilerplate placeholder phrases that must not, by themselves, satisfy
// no_findings_rationale -- a caller writing one of these (with or without
// surrounding whitespace/trailing punctuation) is not providing a real
// rationale, just rubber-stamping the empty-findings case.
const NO_FINDINGS_RATIONALE_BOILERPLATE_PATTERN = /^(no findings|nothing to report|n\/a|none)\.?$/iu
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
  // Issue #1415 AC10-11: not marked `required: true` here because the
  // standalone --schema mode (below) does not use a retro_live_verification
  // manifest at all; runCli() enforces manifest-json's presence explicitly
  // for the (default) manifest-checking mode instead.
  '--manifest-json': { key: 'manifestJson' },
  '--execution-profile': { key: 'executionProfile', defaultValue: 'live' },
  '--fixture-comments-json': { key: 'fixtureCommentsJson' },
  '--fixture-resolve-result-json': { key: 'fixtureResolveResultJson' },
  '--fixture-pr-review-json': { key: 'fixturePrReviewJson' },
  // Issue #1415 AC5-7 (additive, optional gate).
  '--refetch-pr-review-surface': { key: 'refetchPrReviewSurface', defaultValue: 'false' },
  '--fixture-pr-review-surface-json': { key: 'fixturePrReviewSurfaceJson' },
  // Issue #1415 AC13 (additive, optional gate): when passed, require
  // runtime_provenance's locally-derivable fields to be non-null.
  '--require-runtime-provenance': { key: 'requireRuntimeProvenance', defaultValue: 'false' },
  // Issue #1415 AC15 (additive, optional gate): when passed, fail-closed if
  // the elapsed time between runtime_provenance.generated_at (capture) and
  // --posted-at (or "now" when omitted) exceeds the given number of seconds.
  '--max-capture-to-post-lag-seconds': { key: 'maxCaptureToPostLagSeconds' },
  '--posted-at': { key: 'postedAt' },
  // Issue #1415 AC8-9 (additive, optional gate): recompute
  // resolved_comment_set_digest from an independently-supplied comment-set
  // JSON file and compare against the value stored in the manifest.
  '--recompute-digests': { key: 'recomputeDigests', defaultValue: 'false' },
  '--comment-set-json': { key: 'commentSetJson' },
  // Issue #1415 AC10-11 (additive, standalone mode -- these two flags run a
  // single independent schema/content check over --input and exit, they do
  // not combine with the retro_live_verification manifest checks above).
  '--schema': { key: 'targetSchema' },
  '--require-findings-or-rationale': { key: 'requireFindingsOrRationale', defaultValue: 'false' },
  '--check-execution-boundary': { key: 'checkExecutionBoundary', defaultValue: 'false' },
  '--input': { key: 'standaloneInput' },
  // Issue #1415 dual-target bundle (--schema retro_live_verification/v3):
  // per-target fixture resolve-result files for the assert-live context-
  // assertion binding that this standalone mode runs unconditionally for
  // BOTH issue_target and pull_request_target (mirrors the AC2 fix, applied
  // to both targets rather than just the manifest-mode single target).
  '--fixture-resolve-result-json-issue-target': { key: 'fixtureResolveResultJsonIssueTarget' },
  '--fixture-resolve-result-json-pull-request-target': { key: 'fixtureResolveResultJsonPullRequestTarget' },
  // Issue #1415 P0-2 fix_delta: when passed with --schema
  // retro_live_verification/v3, additionally re-derive every comment URL /
  // proof_payload_digest / target_head_sha claim in the bundle from the
  // live GitHub API (see verifyDualTargetTargetLiveArtifacts). Without this
  // flag the dual-target standalone check remains structural-only
  // (schema + context-assertion binding), matching pre-fix_delta behavior
  // exactly -- existing fixture-based tests are unaffected.
  '--live': { key: 'live', defaultValue: 'false' },
}

/**
 * Issue #1415 AC13: pure presence/type check over an already-loaded
 * manifest's runtime_provenance. node_version/pnpm_version/
 * pnpm_lockfile_digest/gh_cli_version are always locally derivable and must
 * be non-null; github_api_version/workflow_run_identity may legitimately be
 * null outside a live-API / GitHub-Actions context, but the keys themselves
 * must be present (not simply absent from an old-shaped manifest).
 */
export function checkRuntimeProvenanceComplete(runtimeProvenance) {
  const errors = []
  const requiredNonNullKeys = ['node_version', 'pnpm_version', 'pnpm_lockfile_digest', 'gh_cli_version']
  for (const key of requiredNonNullKeys) {
    if (!Object.prototype.hasOwnProperty.call(runtimeProvenance ?? {}, key) || runtimeProvenance[key] === null || runtimeProvenance[key] === undefined) {
      errors.push({ code: 'retro_live_verification_check.runtime_provenance_missing_field', message: `runtime_provenance.${key} is required and must be non-null` })
    }
  }
  for (const key of ['github_api_version', 'workflow_run_identity']) {
    if (!Object.prototype.hasOwnProperty.call(runtimeProvenance ?? {}, key)) {
      errors.push({ code: 'retro_live_verification_check.runtime_provenance_missing_field', message: `runtime_provenance.${key} key must be present (may be null)` })
    }
  }
  return { ok: errors.length === 0, errors }
}

/**
 * Issue #1415 AC15: pure freshness/max-lag check. `captureIso` is the
 * manifest's runtime_provenance.generated_at; `postedIso` is either an
 * explicit --posted-at value or the current time. A negative lag (posted
 * before captured) is also fail-closed -- it indicates a tampered or
 * inconsistent manifest/post timeline, never a pass.
 */
export function checkCaptureToPostLag({ captureIso, postedIso, maxLagSeconds }) {
  const captureMs = Date.parse(captureIso)
  const postedMs = Date.parse(postedIso)
  if (Number.isNaN(captureMs)) {
    return { ok: false, errors: [{ code: 'retro_live_verification_check.capture_timestamp_invalid', message: `runtime_provenance.generated_at is not a valid timestamp: ${JSON.stringify(captureIso)}` }] }
  }
  if (Number.isNaN(postedMs)) {
    return { ok: false, errors: [{ code: 'retro_live_verification_check.posted_timestamp_invalid', message: `--posted-at is not a valid timestamp: ${JSON.stringify(postedIso)}` }] }
  }
  const lagSeconds = (postedMs - captureMs) / 1000
  if (lagSeconds < 0) {
    return { ok: false, errors: [{ code: 'retro_live_verification_check.capture_to_post_lag_negative', message: `posted timestamp (${postedIso}) is earlier than capture timestamp (${captureIso})` }] }
  }
  if (lagSeconds > maxLagSeconds) {
    return { ok: false, errors: [{ code: 'retro_live_verification_check.capture_to_post_lag_exceeded', message: `capture-to-post lag ${lagSeconds}s exceeds max-capture-to-post-lag-seconds ${maxLagSeconds}s` }] }
  }
  return { ok: true, errors: [], lagSeconds }
}

function normalizeMaxLagSeconds(value) {
  if (!/^(0|[1-9][0-9]*)$/u.test(String(value))) {
    throw usageError('retro_live_verification_check.max_capture_to_post_lag_seconds', 'max-capture-to-post-lag-seconds must be a non-negative integer')
  }
  return Number(value)
}

/**
 * Issue #1415 AC8-9: recomputes `resolved_comment_set_digest` from an
 * independently-supplied comment-set (never from the manifest's own stored
 * digest, and never from the same resolver invocation that produced the
 * manifest -- the caller is expected to have fetched `commentSet` fresh) and
 * compares it byte-exact against `manifest.resolved_comment_set_digest`.
 * Fails closed if the manifest field is absent (nothing to recompute
 * against) or the canonicalization_profile does not match.
 */
export function evaluateResolvedCommentSetDigest({ manifest, commentSet }) {
  const stored = manifest?.resolved_comment_set_digest
  if (stored === null || stored === undefined) {
    return { ok: false, errors: [{ code: 'retro_live_verification_check.resolved_comment_set_digest_missing', message: 'manifest.resolved_comment_set_digest is required when --recompute-digests is passed' }] }
  }
  if (stored.canonicalization_profile !== CANONICALIZATION_PROFILE_LOOP_V1) {
    return { ok: false, errors: [{ code: 'retro_live_verification_check.resolved_comment_set_digest_profile_mismatch', message: `manifest.resolved_comment_set_digest.canonicalization_profile ${JSON.stringify(stored.canonicalization_profile)} does not match expected ${JSON.stringify(CANONICALIZATION_PROFILE_LOOP_V1)}` }] }
  }
  const recomputed = computeResolvedCommentSetDigest({
    repoId: commentSet?.repo_id ?? null,
    targetNodeId: commentSet?.target_node_id ?? null,
    comments: commentSet?.comments ?? [],
  })
  if (recomputed !== stored.digest) {
    return { ok: false, errors: [{ code: 'retro_live_verification_check.resolved_comment_set_digest_mismatch', message: `recomputed resolved_comment_set_digest ${recomputed} does not match manifest value ${stored.digest}` }] }
  }
  return { ok: true, errors: [] }
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

function parseBooleanFlag(value) {
  return value === 'true'
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
  const authorLogin = comment?.user?.login ?? null
  const authorId = typeof comment?.user?.id === 'number' && Number.isInteger(comment.user.id) ? comment.user.id : null
  if (!digestMatch?.groups) {
    return { matches: true, malformed: true, authorLogin, authorId, body }
  }
  return { matches: true, malformed: false, digest: digestMatch.groups.digest, authorLogin, authorId, body }
}

/**
 * Issue #1415 AC4: trust judgment must be evaluated before malformed/
 * duplicate candidate counting, and author association (login) alone is not
 * a sufficient trust anchor on its own. When the manifest declares an
 * optional `trusted_actor_id_allowlist` (Issue #1709's `trusted_actor_allowlist`
 * remains login-only for v2 backward compatibility), an author must match
 * BOTH an allowed login AND an allowed numeric GitHub user id to be trusted;
 * a bare login match is insufficient once a numeric allowlist is declared
 * (defeats a renamed/impersonating account reusing a trusted login string).
 * When `trusted_actor_id_allowlist` is absent entirely, this degrades to the
 * v2 login-only check unchanged.
 */
export function isTrustedMarkerAuthor({ authorLogin, authorId }, executionBoundary) {
  const loginTrusted = typeof authorLogin === 'string' && executionBoundary.trusted_actor_allowlist.includes(authorLogin)
  const idAllowlist = executionBoundary.trusted_actor_id_allowlist
  if (!Array.isArray(idAllowlist) || idAllowlist.length === 0) {
    return loginTrusted
  }
  const idTrusted = typeof authorId === 'number' && idAllowlist.includes(authorId)
  return loginTrusted && idTrusted
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
  const allMatches = parsed.filter((entry) => entry.matches)

  // Issue #1415 AC4: trust is judged first. Untrusted candidates are
  // excluded from the canonical/duplicate candidate count entirely -- they
  // must never be able to trigger a false "multiple canonical comments"
  // block, nor (conversely) hide a genuine trusted duplicate by diluting the
  // count. Malformed-marker and duplicate-count fail-closed checks below
  // therefore only ever look at the trusted subset.
  const trustedMatches = allMatches.filter((entry) => isTrustedMarkerAuthor(entry, manifest.execution_boundary))

  if (trustedMatches.length === 0) {
    if (allMatches.length === 0) {
      errors.push({ code: 'retro_live_verification_check.matched_comment_count_mismatch', message: 'expected exactly 1 canonical comment, found 0' })
    } else {
      const [untrusted] = allMatches
      errors.push({ code: 'retro_live_verification_check.untrusted_marker_author', message: `canonical comment author ${untrusted.authorLogin} (id ${untrusted.authorId}) is not in the trusted_actor_allowlist / trusted_actor_id_allowlist` })
    }
    return { ok: false, errors }
  }
  if (trustedMatches.some((entry) => entry.malformed)) {
    errors.push({ code: 'retro_live_verification_check.malformed_canonical_comment', message: 'a trusted-author comment matches the ownership marker but its digest marker is malformed' })
    return { ok: false, errors }
  }
  if (trustedMatches.length !== 1) {
    errors.push({ code: 'retro_live_verification_check.matched_comment_count_mismatch', message: `expected exactly 1 trusted canonical comment, found ${trustedMatches.length}` })
    return { ok: false, errors }
  }

  const [match] = trustedMatches
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

/**
 * Issue #1415 AC5-7: pure cross-validation over an already-fetched (or
 * fixture-loaded) PR review surface (`{ headRefOid, reviews, reviewThreads }`
 * from `fetchPullRequestReviewSurfaceLive`). Never defaults an absent/null
 * surface field to "no binding to check" when the manifest's
 * `pr_review_binding` declares a non-null selector for it.
 *
 * Requires (when `selected_review_id` is non-null):
 *   - review_count >= 1, review_comment_count >= 1 (across all threads),
 *     resolved_thread_count >= 1
 *   - a review with databaseId === selected_review_id exists, and its
 *     commit.oid equals both manifest.reviewed_head_sha AND the live
 *     surface's headRefOid (stale-PR-head detection)
 *   - when selected_review_comment_id is non-null: a review comment with
 *     that databaseId exists, and its pullRequestReview.databaseId equals
 *     selected_review_id
 *   - when selected_review_thread_node_id is non-null: a thread with that
 *     node id exists, is isResolved === true, and its comments include the
 *     selected review comment
 */
export function evaluatePrReviewSurfaceBinding({ surface, prReviewBinding }) {
  const errors = []
  const reviews = Array.isArray(surface?.reviews) ? surface.reviews : []
  const reviewThreads = Array.isArray(surface?.reviewThreads) ? surface.reviewThreads : []
  const allComments = reviewThreads.flatMap((thread) => (Array.isArray(thread?.comments?.nodes) ? thread.comments.nodes : []))
  const resolvedThreads = reviewThreads.filter((thread) => thread?.isResolved === true)

  if (reviews.length < 1) {
    errors.push({ code: 'retro_live_verification_check.pr_review_surface_zero_reviews', message: 'expected at least 1 review, found 0' })
  }
  if (allComments.length < 1) {
    errors.push({ code: 'retro_live_verification_check.pr_review_surface_zero_review_comments', message: 'expected at least 1 review comment, found 0' })
  }
  if (resolvedThreads.length < 1) {
    errors.push({ code: 'retro_live_verification_check.pr_review_surface_zero_resolved_threads', message: 'expected at least 1 resolved review thread, found 0' })
  }

  if (prReviewBinding.selected_review_id === null || prReviewBinding.selected_review_id === undefined) {
    return { ok: errors.length === 0, errors }
  }

  const selectedReview = reviews.find((review) => review?.databaseId === prReviewBinding.selected_review_id)
  if (!selectedReview) {
    errors.push({ code: 'retro_live_verification_check.pr_review_surface_selected_review_missing', message: `no live review with databaseId ${prReviewBinding.selected_review_id} was found` })
  } else {
    const reviewCommitOid = selectedReview.commit?.oid ?? null
    if (reviewCommitOid !== prReviewBinding.reviewed_head_sha) {
      errors.push({ code: 'retro_live_verification_check.pr_review_surface_reviewed_head_sha_mismatch', message: `selected review commit.oid ${JSON.stringify(reviewCommitOid)} does not match manifest reviewed_head_sha ${prReviewBinding.reviewed_head_sha}` })
    }
    if (surface.headRefOid !== null && surface.headRefOid !== undefined && surface.headRefOid !== prReviewBinding.reviewed_head_sha) {
      errors.push({ code: 'retro_live_verification_check.pr_review_surface_stale_pr_head', message: `live PR headRefOid ${JSON.stringify(surface.headRefOid)} no longer matches manifest reviewed_head_sha ${prReviewBinding.reviewed_head_sha}` })
    }
  }

  let selectedComment = null
  if (prReviewBinding.selected_review_comment_id !== null && prReviewBinding.selected_review_comment_id !== undefined) {
    selectedComment = allComments.find((comment) => comment?.databaseId === prReviewBinding.selected_review_comment_id) ?? null
    if (!selectedComment) {
      errors.push({ code: 'retro_live_verification_check.pr_review_surface_selected_comment_missing', message: `no live review comment with databaseId ${prReviewBinding.selected_review_comment_id} was found` })
    } else if (selectedComment.pullRequestReview?.databaseId !== prReviewBinding.selected_review_id) {
      errors.push({ code: 'retro_live_verification_check.pr_review_surface_comment_review_mismatch', message: `selected review comment's pullRequestReview.databaseId ${JSON.stringify(selectedComment.pullRequestReview?.databaseId)} does not match selected_review_id ${prReviewBinding.selected_review_id}` })
    }
  }

  if (prReviewBinding.selected_review_thread_node_id !== null && prReviewBinding.selected_review_thread_node_id !== undefined) {
    const selectedThread = reviewThreads.find((thread) => thread?.id === prReviewBinding.selected_review_thread_node_id)
    if (!selectedThread) {
      errors.push({ code: 'retro_live_verification_check.pr_review_surface_selected_thread_missing', message: `no live review thread with id ${prReviewBinding.selected_review_thread_node_id} was found` })
    } else {
      if (selectedThread.isResolved !== true) {
        errors.push({ code: 'retro_live_verification_check.pr_review_surface_thread_unresolved', message: `selected review thread ${prReviewBinding.selected_review_thread_node_id} is not resolved` })
      }
      if (selectedComment) {
        const threadCommentIds = new Set((selectedThread.comments?.nodes ?? []).map((comment) => comment?.databaseId))
        if (!threadCommentIds.has(selectedComment.databaseId)) {
          errors.push({ code: 'retro_live_verification_check.pr_review_surface_comment_not_in_thread', message: `selected review comment ${selectedComment.databaseId} is not a member of selected review thread ${prReviewBinding.selected_review_thread_node_id}` })
        }
      }
    }
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

/**
 * Issue #1415 AC10: pure check over an already schema-valid
 * chatgpt_retrospective_result/v1 payload. Fails closed when findings is
 * empty and no_findings_rationale is either absent or matches a boilerplate
 * placeholder phrase (schema minLength alone cannot reject e.g. "no findings
 * at all, nothing to report here" style padding designed only to clear a
 * length check -- this additionally rejects the exact boilerplate phrases).
 */
export function evaluateFindingsOrRationale(payload) {
  const findings = Array.isArray(payload?.findings) ? payload.findings : []
  if (findings.length > 0) {
    return { ok: true, errors: [] }
  }
  const rationale = payload?.no_findings_rationale
  if (typeof rationale !== 'string' || rationale.trim().length === 0) {
    return { ok: false, errors: [{ code: 'retro_live_verification_check.no_findings_rationale_missing', message: 'findings is empty and no_findings_rationale is absent/empty' }] }
  }
  if (NO_FINDINGS_RATIONALE_BOILERPLATE_PATTERN.test(rationale.trim())) {
    return { ok: false, errors: [{ code: 'retro_live_verification_check.no_findings_rationale_boilerplate', message: `no_findings_rationale is a rejected boilerplate placeholder: ${JSON.stringify(rationale)}` }] }
  }
  return { ok: true, errors: [] }
}

/**
 * Issue #1415 AC11: the operator_attested/machine_verified distinction is
 * enforced structurally by docs/schemas/chatgpt-retro-execution-proof.schema.json
 * (proof_strength.{connector_only_execution,local_file_non_use,latitude_direct_non_use}
 * are a closed enum of exactly "machine_verified"/"declared_by_session_operator",
 * and machine_verifies_actual_chatgpt_tool_boundary is pinned `const: false`) --
 * this function is a thin ajv-validation wrapper so --check-execution-boundary
 * can be driven from this CLI without duplicating that schema's constraints.
 */
export async function validateAgainstSchemaFile(schemaFilePath, payload) {
  const { Ajv2020: AjvCtor, addFormats: applyFormats } = await loadAjv()
  const ajv = new AjvCtor({ strict: true, allErrors: true })
  applyFormats(ajv)
  const schemaText = await readFile(schemaFilePath, 'utf-8')
  const schema = JSON.parse(schemaText)
  const validate = ajv.compile(schema)
  const valid = validate(payload)
  return {
    valid,
    errors: valid ? [] : (validate.errors ?? []).map((error) => ({
      path: error.instancePath || '/',
      keyword: error.keyword,
      message: error.message ?? 'schema validation failed',
    })),
  }
}

/**
 * Issue #1415 (dual-target bundle): runs the assert-live context-assertion
 * binding for a single target's context_assertions object, reusing the same
 * hardened subprocess delegation the v2 manifest path uses (never a second,
 * weaker re-implementation). Returns the same shape as
 * evaluateContextAssertionsBinding's input errors array, prefixed with which
 * target failed so a dual-target failure is never ambiguous about which side
 * broke.
 */
function evaluateDualTargetContextAssertions({ targetKey, contextAssertions, executionProfile, fixtureResolveResultJson }) {
  if (executionProfile === 'fixture' && !fixtureResolveResultJson) {
    throw usageError(
      'retro_live_verification_check.dual_target_fixture_resolve_result_json_required',
      `--fixture-resolve-result-json-${targetKey === 'issue_target' ? 'issue-target' : 'pull-request-target'} is required when --execution-profile is fixture`,
    )
  }
  const spawnResult = runContextAssertionsBindingSubprocess({ contextAssertions, executionProfile, fixtureResolveResultJson })
  const check = evaluateContextAssertionsBinding(spawnResult)
  return {
    ok: check.ok,
    errors: check.errors.map((error) => ({ ...error, message: `[${targetKey}] ${error.message}` })),
  }
}

/**
 * Issue #1415 P0-2 fix_delta (post-#1747 adversarial review): parses a
 * GitHub issue/PR comment `html_url` (the shape `commentUrl` in the v3
 * schema requires) into the `owner/repo` + numeric comment id the REST
 * issue-comments endpoint needs. Returns null for anything that doesn't
 * match -- never guesses.
 */
function parseIssueCommentUrl(url) {
  const match = String(url ?? '').match(/^https:\/\/github\.com\/([^/]+)\/([^/]+)\/(?:issues|pull)\/\d+#issuecomment-(\d+)$/u)
  if (!match) return null
  const [, owner, repoName, commentId] = match
  return { repo: `${owner}/${repoName}`, commentId }
}

/**
 * Issue #1415 P0-2 fix_delta: live-fetches a single issue/PR comment by id
 * (PR conversation comments and issue comments share the same REST
 * endpoint). A non-zero gh exit or non-JSON stdout is a failure, never a
 * silently-skipped pass.
 */
function fetchIssueCommentLive({ repo, commentId }) {
  const result = spawnSync('gh', [
    'api',
    '-H', 'Accept: application/vnd.github+json',
    '-H', 'X-GitHub-Api-Version: 2022-11-28',
    `repos/${repo}/issues/comments/${commentId}`,
  ], { encoding: 'utf-8', maxBuffer: DEFAULT_MAX_BUFFER })
  if (result.status !== 0) {
    return { ok: false, error: (result.stderr || result.stdout || 'gh api exited non-zero').trim() }
  }
  try {
    const parsed = JSON.parse(result.stdout)
    if (!parsed || typeof parsed !== 'object' || Array.isArray(parsed)) {
      return { ok: false, error: 'gh api returned a non-object payload' }
    }
    return { ok: true, comment: parsed }
  } catch (error) {
    return { ok: false, error: `gh api returned invalid JSON: ${error.message}` }
  }
}

function extractFirstFencedJson(body) {
  const match = String(body ?? '').match(JSON_FENCE_PATTERN)
  if (!match?.groups) return { ok: false }
  try {
    return { ok: true, payload: JSON.parse(match.groups.payload) }
  } catch {
    return { ok: false }
  }
}

/**
 * Issue #1415 P0-2 fix_delta: `--live` mode for the dual-target bundle.
 * Schema validation alone lets every comment URL/digest/status field in a
 * v3 payload be an unverified self-declaration -- this function makes the
 * bundle's claims falsifiable by re-deriving them from the live GitHub API:
 *   - every required comment URL for both targets is live-fetched and must
 *     resolve (a 404/deleted/never-posted comment is a failure, not a skip)
 *   - `proof_comment_url`'s body must contain a fenced JSON payload whose
 *     recomputed canonical-json-v1 digest equals `proof_payload_digest`
 *     byte-for-byte (the digest field itself is never trusted on its own)
 *   - `pull_request_target.target_head_sha` must equal the PR's live head
 *     SHA (stale-head detection)
 * Digest/URL fields that this check cannot yet independently re-derive
 * (readback.canonical_comment_count / duplicate_count against the parent
 * issue's own comment stream, and a v3-native runtime_provenance object --
 * the schema has no `canonical_comment.ownership_marker` or
 * `runtime_provenance` field to bind against) are explicitly NOT claimed
 * as verified here; they remain open follow-up work, not a silent gap.
 */
async function verifyDualTargetTargetLiveArtifacts({ repo, targetKey, target }) {
  const errors = []
  const urlFields = ['operation_index_comment_url', 'marker_comment_url', 'proof_comment_url', 'retrospective_result_comment_url']
  if (targetKey === 'pull_request_target') urlFields.push('review_surface_proof_comment_url')

  for (const field of urlFields) {
    const url = target[field]
    const parsed = parseIssueCommentUrl(url)
    if (!parsed) {
      errors.push({ code: 'retro_live_verification_check.dual_target_live_url_unparseable', message: `[${targetKey}] ${field} is not a well-formed GitHub comment URL: ${JSON.stringify(url)}` })
      continue
    }
    if (parsed.repo.toLowerCase() !== repo.toLowerCase()) {
      errors.push({ code: 'retro_live_verification_check.dual_target_live_url_repo_mismatch', message: `[${targetKey}] ${field} points at repo ${parsed.repo}, expected ${repo}` })
      continue
    }
    const fetched = fetchIssueCommentLive(parsed)
    if (!fetched.ok) {
      errors.push({ code: 'retro_live_verification_check.dual_target_live_artifact_not_found', message: `[${targetKey}] ${field} could not be live-fetched: ${fetched.error}` })
      continue
    }
    if (field === 'proof_comment_url') {
      const extraction = extractFirstFencedJson(fetched.comment.body)
      if (!extraction.ok) {
        errors.push({ code: 'retro_live_verification_check.dual_target_proof_payload_unparseable', message: `[${targetKey}] proof_comment_url body does not contain a well-formed fenced JSON payload` })
        continue
      }
      const recomputed = `sha256:${sha256Hex(canonicalJsonStringify(extraction.payload))}`
      if (recomputed !== target.proof_payload_digest) {
        errors.push({ code: 'retro_live_verification_check.dual_target_proof_payload_digest_mismatch', message: `[${targetKey}] recomputed proof payload digest ${recomputed} does not match target.proof_payload_digest ${target.proof_payload_digest}` })
      }
    }
  }

  if (targetKey === 'pull_request_target') {
    const prView = spawnSync('gh', ['pr', 'view', String(target.number), '--repo', repo, '--json', 'headRefOid'], { encoding: 'utf-8' })
    if (prView.status !== 0) {
      errors.push({ code: 'retro_live_verification_check.dual_target_pr_head_fetch_failed', message: `[${targetKey}] gh pr view failed: ${(prView.stderr || prView.stdout || '').trim()}` })
    } else {
      try {
        const { headRefOid } = JSON.parse(prView.stdout)
        if (headRefOid !== target.target_head_sha) {
          errors.push({ code: 'retro_live_verification_check.dual_target_stale_pr_head', message: `[${targetKey}] live PR head ${headRefOid} does not match target.target_head_sha ${target.target_head_sha} (stale PR head)` })
        }
      } catch (error) {
        errors.push({ code: 'retro_live_verification_check.dual_target_pr_head_response_invalid', message: `[${targetKey}] gh pr view returned invalid JSON: ${error.message}` })
      }
    }
  }

  return { ok: errors.length === 0, errors }
}

/**
 * Issue #1415 (dual-target bundle): structural + context-assertion checks
 * for a retro_live_verification/v3 payload. Schema validation alone can
 * confirm both targets' comment URLs/digests are present and shaped
 * correctly, but per the AC2 fix this also unconditionally runs the
 * assert-live binding for BOTH issue_target.context_assertions and
 * pull_request_target.context_assertions -- there is no PR-only special
 * case here, matching the manifest-mode fix.
 *
 * Issue #1415 P0-2 fix_delta: when `live` is true, this additionally
 * re-derives every comment URL/digest/head-SHA claim in the bundle from the
 * live GitHub API instead of trusting the payload's self-declared values
 * (see verifyDualTargetTargetLiveArtifacts doc comment for exactly what is,
 * and is not yet, independently verified).
 */
export async function checkDualTargetBundle(payload, { executionProfile, fixtureResolveResultJsonIssueTarget, fixtureResolveResultJsonPullRequestTarget, live = false }) {
  const errors = []
  const schemaValidation = await validateAgainstSchemaFile(DUAL_TARGET_BUNDLE_SCHEMA_FILE, payload)
  if (!schemaValidation.valid) {
    errors.push(...schemaValidation.errors.map((error) => ({ code: 'retro_live_verification_check.dual_target_bundle_schema_invalid', message: `${error.path}: ${error.message}` })))
    // A structurally invalid bundle cannot be safely dereferenced further
    // (e.g. issue_target may be entirely absent) -- fail closed here rather
    // than risk a TypeError-as-crash or an undefined-context-assertions
    // false pass.
    return { ok: false, errors }
  }

  const issueCheck = evaluateDualTargetContextAssertions({
    targetKey: 'issue_target',
    contextAssertions: payload.issue_target.context_assertions,
    executionProfile,
    fixtureResolveResultJson: fixtureResolveResultJsonIssueTarget,
  })
  errors.push(...issueCheck.errors)

  const pullRequestCheck = evaluateDualTargetContextAssertions({
    targetKey: 'pull_request_target',
    contextAssertions: payload.pull_request_target.context_assertions,
    executionProfile,
    fixtureResolveResultJson: fixtureResolveResultJsonPullRequestTarget,
  })
  errors.push(...pullRequestCheck.errors)

  if (live) {
    const issueLive = await verifyDualTargetTargetLiveArtifacts({ repo: payload.repo, targetKey: 'issue_target', target: payload.issue_target })
    errors.push(...issueLive.errors)
    const pullRequestLive = await verifyDualTargetTargetLiveArtifacts({ repo: payload.repo, targetKey: 'pull_request_target', target: payload.pull_request_target })
    errors.push(...pullRequestLive.errors)
  }

  return { ok: errors.length === 0, errors }
}

async function runStandaloneSchemaCheck(options) {
  if (!options.standaloneInput) {
    throw usageError('retro_live_verification_check.input_required', '--input is required when --schema is passed')
  }
  const payload = await loadJsonFile(options.standaloneInput, 'retro_live_verification_check.input_json_invalid')
  const errors = []

  if (options.targetSchema === DUAL_TARGET_BUNDLE_SCHEMA_ID) {
    const executionProfile = normalizeExecutionProfile(options.executionProfile)
    const bundleCheck = await checkDualTargetBundle(payload, {
      executionProfile,
      fixtureResolveResultJsonIssueTarget: options.fixtureResolveResultJsonIssueTarget,
      fixtureResolveResultJsonPullRequestTarget: options.fixtureResolveResultJsonPullRequestTarget,
      live: parseBooleanFlag(options.live),
    })
    errors.push(...bundleCheck.errors)
    const ok = bundleCheck.ok
    process.stdout.write(`${JSON.stringify({ schema: SCHEMA, verification_status: ok ? 'pass' : 'fail', execution_profile: executionProfile, checked_at: new Date().toISOString(), errors }, null, 2)}\n`)
    process.exitCode = ok ? 0 : 1
    return
  }

  if (options.targetSchema === RETROSPECTIVE_RESULT_SCHEMA_ID) {
    const schemaValidation = await validateAgainstSchemaFile(RETROSPECTIVE_RESULT_SCHEMA_FILE, payload)
    if (!schemaValidation.valid) {
      errors.push(...schemaValidation.errors.map((error) => ({ code: 'retro_live_verification_check.retrospective_result_schema_invalid', message: `${error.path}: ${error.message}` })))
    }
    if (parseBooleanFlag(options.requireFindingsOrRationale)) {
      const findingsCheck = evaluateFindingsOrRationale(payload)
      errors.push(...findingsCheck.errors)
    }
  } else if (parseBooleanFlag(options.checkExecutionBoundary)) {
    const schemaValidation = await validateAgainstSchemaFile(resolvePath(REPO_ROOT, 'docs/schemas/chatgpt-retro-execution-proof.schema.json'), payload)
    if (!schemaValidation.valid) {
      errors.push(...schemaValidation.errors.map((error) => ({ code: 'retro_live_verification_check.execution_boundary_schema_invalid', message: `${error.path}: ${error.message}` })))
    }
  } else {
    throw usageError('retro_live_verification_check.unknown_standalone_schema', `unsupported --schema value: ${JSON.stringify(options.targetSchema)}`)
  }

  const ok = errors.length === 0
  process.stdout.write(`${JSON.stringify({ schema: SCHEMA, verification_status: ok ? 'pass' : 'fail', checked_at: new Date().toISOString(), errors }, null, 2)}\n`)
  process.exitCode = ok ? 0 : 1
}

async function runCli() {
  const rawArgs = process.argv.slice(2)
  const options = parseArgs(rawArgs.length === 0 ? DEFAULT_CHECK_ARGS : rawArgs, CLI_OPTION_SPEC)

  // Issue #1415 AC10-11 (additive, standalone mode): --schema short-circuits
  // the retro_live_verification manifest checks entirely.
  if (options.targetSchema || parseBooleanFlag(options.checkExecutionBoundary)) {
    await runStandaloneSchemaCheck(options)
    return
  }
  if (!options.manifestJson) {
    throw usageError('retro_live_verification_check.manifest_json_required', '--manifest-json is required unless --schema/--check-execution-boundary standalone mode is used')
  }

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

  // Issue #1415 AC5-7 (additive, optional gate).
  let prReviewSurfaceCheck = { ok: true, errors: [] }
  if (parseBooleanFlag(options.refetchPrReviewSurface)) {
    if (manifest.context_assertions.target_type !== 'pull_request') {
      throw usageError('retro_live_verification_check.refetch_pr_review_surface_requires_pull_request', '--refetch-pr-review-surface requires context_assertions.target_type to be pull_request')
    }
    let surface
    if (executionProfile === 'fixture') {
      if (!options.fixturePrReviewSurfaceJson) {
        throw usageError('retro_live_verification_check.fixture_pr_review_surface_json_required', '--fixture-pr-review-surface-json is required when --execution-profile is fixture and --refetch-pr-review-surface is true')
      }
      surface = await loadJsonFile(options.fixturePrReviewSurfaceJson, 'retro_live_verification_check.fixture_pr_review_surface_json_invalid')
      if (surface?.ok !== true) {
        prReviewSurfaceCheck = { ok: false, errors: [{ code: surface?.errorCode ?? 'retro_live_verification_check.pr_review_surface_fetch_failed', message: `pr review surface fetch reported failure: ${JSON.stringify(surface?.errors ?? [])}` }] }
        errors.push(...prReviewSurfaceCheck.errors)
        surface = null
      }
    } else {
      const client = new GhCliIssueCommentsClient()
      const fetched = await fetchPullRequestReviewSurfaceLive(client, {
        repo: manifest.canonical_comment.repo,
        pullNumber: manifest.context_assertions.target_number,
      })
      if (!fetched.ok) {
        prReviewSurfaceCheck = { ok: false, errors: [{ code: fetched.errorCode, message: `pr review surface fetch reported failure: ${JSON.stringify(fetched.errors)}` }] }
        errors.push(...prReviewSurfaceCheck.errors)
        surface = null
      } else {
        surface = fetched
      }
    }
    if (surface) {
      prReviewSurfaceCheck = evaluatePrReviewSurfaceBinding({ surface, prReviewBinding: manifest.pr_review_binding })
      errors.push(...prReviewSurfaceCheck.errors)
    }
  }

  // Issue #1415 AC13 (additive, optional gate).
  let runtimeProvenanceCheck = { ok: true, errors: [] }
  if (parseBooleanFlag(options.requireRuntimeProvenance)) {
    runtimeProvenanceCheck = checkRuntimeProvenanceComplete(manifest.runtime_provenance)
    errors.push(...runtimeProvenanceCheck.errors)
  }

  // Issue #1415 AC15 (additive, optional gate).
  let captureToPostLagCheck = { ok: true, errors: [] }
  if (options.maxCaptureToPostLagSeconds !== undefined) {
    const maxLagSeconds = normalizeMaxLagSeconds(options.maxCaptureToPostLagSeconds)
    captureToPostLagCheck = checkCaptureToPostLag({
      captureIso: manifest.runtime_provenance?.generated_at,
      postedIso: options.postedAt ?? new Date().toISOString(),
      maxLagSeconds,
    })
    errors.push(...captureToPostLagCheck.errors)
  }

  // Issue #1415 AC8-9 (additive, optional gate).
  let resolvedCommentSetDigestCheck = { ok: true, errors: [] }
  if (parseBooleanFlag(options.recomputeDigests)) {
    if (!options.commentSetJson) {
      throw usageError('retro_live_verification_check.comment_set_json_required', '--comment-set-json is required when --recompute-digests is true')
    }
    const commentSet = await loadJsonFile(options.commentSetJson, 'retro_live_verification_check.comment_set_json_invalid')
    resolvedCommentSetDigestCheck = evaluateResolvedCommentSetDigest({ manifest, commentSet })
    errors.push(...resolvedCommentSetDigestCheck.errors)
  }

  const ok = commentCheck.ok && contextAssertionsCheck.ok && prReviewBindingCheck.ok && prReviewSurfaceCheck.ok && runtimeProvenanceCheck.ok && captureToPostLagCheck.ok && resolvedCommentSetDigestCheck.ok
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
