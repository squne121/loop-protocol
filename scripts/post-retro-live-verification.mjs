#!/usr/bin/env node
// post-retro-live-verification.mjs
//
// Single-writer producer for the `retro_live_verification/v2` canonical
// verification comment (Issue #1709, prerequisite of #1415 / parent #1153).
//
// Safety properties (Issue #1709 In Scope):
//   - the posting actor must be a member of the manifest's
//     `execution_boundary.trusted_actor_allowlist` (fail-closed otherwise)
//   - `canonical_comment.expected_previous_digest` is a compare-and-swap
//     guard against the live GitHub state: a create requires no existing
//     canonical comment, an update requires the existing comment's digest to
//     match exactly
//   - after a real (non-dry-run) write, the comment is read back from the
//     GitHub API and its body/digest must match the manifest byte-for-byte
//     before the run is declared successful
//
// Usage:
//   node scripts/post-retro-live-verification.mjs \
//     --manifest-json artifacts/retro-live-verification-manifest.json \
//     --current-actor squne121 --dry-run false
//
// pnpm run retro-live-verification:post -- <flags above>

import { readFile } from 'node:fs/promises'
import { fileURLToPath } from 'node:url'

import { CliError, parseArgs, usageError } from './agent-logs/lib/args.mjs'
import {
  GhCliIssueCommentsClient,
  GithubApiError,
  listAllIssueCommentsStructured,
  summarizeGithubApiError,
} from './agent-logs/lib/github-comments.mjs'
import {
  computeCanonicalCommentBody,
  sha256Hex,
  validateManifestWithAjv,
} from './generate-retro-live-verification.mjs'

export const SCHEMA = 'retro_live_verification_post_result/v1'

const CLI_OPTION_SPEC = {
  '--manifest-json': { key: 'manifestJson', required: true },
  '--current-actor': { key: 'currentActor', required: true },
  '--dry-run': { key: 'dryRun', defaultValue: 'true' },
}

/**
 * Fail-closed on *either* pagination completeness flag (Issue #1709 PR
 * review P1-3). See scripts/check-retro-live-verification.mjs for the
 * matching verifier-side helper and rationale.
 */
export function isPaginationRejected(listing) {
  return listing?.pagination_exhausted === true || listing?.page_budget_exhausted === true
}

/**
 * Pure decision core for the post-write full re-enumeration uniqueness
 * check (Issue #1709 PR review P1-1). Given every comment observed by a
 * fresh, complete re-list after a write, requires that exactly one of them
 * matches the ownership marker and that it is the comment this run itself
 * just wrote (by id) with the intended digest -- so two concurrent writers
 * that both observed "no existing comment" and both created a comment can
 * never both report success.
 */
export function evaluatePostWriteUniqueness({ postWriteComments, ownershipMarker, expectedCommentId, expectedDigest }) {
  const matches = postWriteComments
    .map((comment) => ({ comment, ...parseExistingCanonicalComment(comment, ownershipMarker) }))
    .filter((entry) => entry.matches && !entry.malformed)
  if (matches.length !== 1 || matches[0].comment?.id !== expectedCommentId) {
    return {
      ok: false,
      errorCode: 'retro_live_verification.post_write_duplicate_detected',
      errorMessage: `expected exactly 1 canonical comment with id ${expectedCommentId} after write, found ${matches.length} matching comment(s)`,
    }
  }
  if (matches[0].digest !== expectedDigest) {
    return {
      ok: false,
      errorCode: 'retro_live_verification.post_write_digest_mismatch',
      errorMessage: 'post-write full re-list digest did not match the intended canonical comment digest',
    }
  }
  return { ok: true }
}

function parseBooleanFlag(value, code) {
  if (value === 'true') {
    return true
  }
  if (value === 'false') {
    return false
  }
  throw usageError(code, `${code} must be true or false`)
}

/**
 * Parses the ownership marker line out of an existing comment body, using
 * the same digest-marker-second-line convention the manifest's canonical
 * comment format follows, so an existing non-canonical comment (or a
 * malformed prior canonical comment) is never silently overwritten.
 */
export function parseExistingCanonicalComment(comment, ownershipMarker) {
  const body = typeof comment?.body === 'string' ? comment.body : ''
  const lines = body.split('\n').map((line) => line.trim()).filter((line) => line.length > 0)
  const [firstLine, secondLine] = lines
  if (firstLine !== ownershipMarker) {
    return { matches: false, malformed: false }
  }
  const digestMatch = (secondLine ?? '').match(/^<!-- retro_live_verification_digest:v1 sha256=(?<digest>[a-f0-9]{64}) -->$/u)
  if (!digestMatch?.groups) {
    return { matches: true, malformed: true }
  }
  return { matches: true, malformed: false, digest: digestMatch.groups.digest }
}

/**
 * Pure decision core (dependency-injectable: `existingComments` and
 * `currentActor` are plain inputs), so the compare-and-swap / trusted-actor
 * / dry-run branches are unit-testable without a live GitHub call.
 */
export function planPost({ manifest, existingComments, currentActor }) {
  if (!manifest.execution_boundary.trusted_actor_allowlist.includes(currentActor)) {
    return { ok: false, errorCode: 'retro_live_verification.untrusted_actor', errorMessage: `actor ${currentActor} is not in the trusted_actor_allowlist` }
  }

  const { ownership_marker: ownershipMarker, body_digest: bodyDigest, expected_previous_digest: expectedPreviousDigest } = manifest.canonical_comment
  const parsedComments = existingComments.map((comment) => ({ comment, ...parseExistingCanonicalComment(comment, ownershipMarker) }))
  const matches = parsedComments.filter((entry) => entry.matches)
  const malformedMatch = matches.find((entry) => entry.malformed)
  if (malformedMatch) {
    return { ok: false, errorCode: 'retro_live_verification.existing_comment_malformed', errorMessage: 'existing canonical comment is malformed; refusing to overwrite' }
  }
  if (matches.length >= 2) {
    return { ok: false, errorCode: 'retro_live_verification.duplicate_marker', errorMessage: 'multiple existing comments match the canonical ownership marker' }
  }

  if (matches.length === 0) {
    if (expectedPreviousDigest !== null) {
      return { ok: false, errorCode: 'retro_live_verification.expected_previous_digest_mismatch', errorMessage: 'expected_previous_digest is non-null but no canonical comment exists yet' }
    }
    return { ok: true, action: 'create', existing: null, bodyDigest }
  }

  const [existing] = matches
  if (existing.digest !== expectedPreviousDigest) {
    return { ok: false, errorCode: 'retro_live_verification.expected_previous_digest_mismatch', errorMessage: `expected_previous_digest ${expectedPreviousDigest} does not match live digest ${existing.digest}` }
  }
  if (existing.digest === bodyDigest) {
    return { ok: true, action: 'noop', existing: existing.comment, bodyDigest }
  }
  return { ok: true, action: 'update', existing: existing.comment, bodyDigest }
}

async function runCli() {
  const options = parseArgs(process.argv.slice(2), CLI_OPTION_SPEC)
  const dryRun = parseBooleanFlag(options.dryRun, 'retro_live_verification.dry_run')

  const manifestText = await readFile(options.manifestJson, 'utf-8')
  const manifest = JSON.parse(manifestText)
  const validation = await validateManifestWithAjv(manifest)
  if (!validation.valid) {
    process.stdout.write(`${JSON.stringify({ schema: SCHEMA, status: 'error', error_code: 'retro_live_verification.manifest_schema_invalid', errors: validation.errors })}\n`)
    process.exitCode = 1
    return
  }

  const client = new GhCliIssueCommentsClient()
  const { repo, issue_number: issueNumber } = manifest.canonical_comment
  const listing = await listAllIssueCommentsStructured(client, { repo, issueNumber })
  if (isPaginationRejected(listing)) {
    process.stdout.write(`${JSON.stringify({ schema: SCHEMA, status: 'error', error_code: 'retro_live_verification.pagination_exhausted', errors: [] })}\n`)
    process.exitCode = 2
    return
  }

  const plan = planPost({ manifest, existingComments: listing.comments, currentActor: options.currentActor })
  if (!plan.ok) {
    process.stdout.write(`${JSON.stringify({ schema: SCHEMA, status: 'error', error_code: plan.errorCode, error_message: plan.errorMessage })}\n`)
    process.exitCode = 1
    return
  }

  const { ownership_marker: ownershipMarker } = manifest.canonical_comment
  const { body } = computeCanonicalCommentBody({
    contextAssertions: manifest.context_assertions,
    prReviewBinding: manifest.pr_review_binding,
    ownershipMarker,
  })

  if (dryRun || plan.action === 'noop') {
    process.stdout.write(`${JSON.stringify({
      schema: SCHEMA,
      status: 'ok',
      action: plan.action,
      dry_run: dryRun,
      comment_id: plan.existing?.id ?? null,
      comment_url: plan.existing?.html_url ?? plan.existing?.url ?? null,
      body_digest: plan.bodyDigest,
      readback_verified: false,
    })}\n`)
    return
  }

  let written
  if (plan.action === 'create') {
    written = await client.createIssueComment({ repo, issueNumber, body })
  } else {
    written = await client.updateIssueComment({ repo, commentId: plan.existing.id, body })
  }

  const commentId = written?.id ?? plan.existing?.id ?? null
  if (commentId === null) {
    process.stdout.write(`${JSON.stringify({ schema: SCHEMA, status: 'error', error_code: 'retro_live_verification.write_response_missing_id', error_message: 'GitHub API write response did not include a comment id' })}\n`)
    process.exitCode = 2
    return
  }

  const readback = await client.getIssueComment({ repo, commentId })
  const readbackBody = typeof readback?.body === 'string' ? readback.body : ''
  const readbackDigestOk = readbackBody === body && sha256Hex(readbackBody) === sha256Hex(body)
  if (!readbackDigestOk) {
    process.stdout.write(`${JSON.stringify({ schema: SCHEMA, status: 'error', error_code: 'retro_live_verification.post_write_readback_mismatch', error_message: 'post-write readback body did not match the intended canonical comment body byte-for-byte' })}\n`)
    process.exitCode = 2
    return
  }

  // Full re-enumeration + exactly-one-match uniqueness check (Issue #1709
  // PR review P1-1). A single-comment GET readback (above) cannot detect a
  // concurrent second writer that also observed "no existing comment" and
  // also created one: both writers would each see their own write succeed.
  // Re-listing every comment on the target and requiring the ownership
  // marker to match *exactly this* comment_id closes that gap.
  const postWriteListing = await listAllIssueCommentsStructured(client, { repo, issueNumber })
  if (isPaginationRejected(postWriteListing)) {
    process.stdout.write(`${JSON.stringify({ schema: SCHEMA, status: 'error', error_code: 'retro_live_verification.post_write_pagination_exhausted', errors: [] })}\n`)
    process.exitCode = 2
    return
  }
  const uniqueness = evaluatePostWriteUniqueness({
    postWriteComments: postWriteListing.comments,
    ownershipMarker,
    expectedCommentId: commentId,
    expectedDigest: plan.bodyDigest,
  })
  if (!uniqueness.ok) {
    process.stdout.write(`${JSON.stringify({ schema: SCHEMA, status: 'error', error_code: uniqueness.errorCode, error_message: uniqueness.errorMessage })}\n`)
    process.exitCode = 2
    return
  }

  process.stdout.write(`${JSON.stringify({
    schema: SCHEMA,
    status: 'ok',
    action: plan.action,
    dry_run: false,
    comment_id: commentId,
    comment_url: readback?.html_url ?? readback?.url ?? written?.html_url ?? written?.url ?? null,
    body_digest: plan.bodyDigest,
    readback_verified: true,
  })}\n`)
}

const isDirectExecution = process.argv[1] === fileURLToPath(import.meta.url)
if (isDirectExecution) {
  runCli().catch((error) => {
    const isCliError = error instanceof CliError
    const githubSummary = summarizeGithubApiError(error)
    process.stdout.write(`${JSON.stringify({
      schema: SCHEMA,
      status: 'error',
      error_code: isCliError ? error.code : (githubSummary ? `retro_live_verification.github_api_${githubSummary.reason_code}` : 'retro_live_verification.unexpected_error'),
      error_message: error?.message ?? 'unexpected runtime failure',
      github: githubSummary,
    })}\n`)
    process.exitCode = isCliError ? error.exitCode : (error instanceof GithubApiError ? 2 : 2)
  })
}
