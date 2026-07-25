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
//     is a member of the manifest's trusted_actor_allowlist, and its digest
//     marker matches the manifest's canonical_comment.body_digest
//   - for a pull_request target, PR review-thread pagination is walked to
//     completion (`hasNextPage: false`) and every page's GraphQL response is
//     checked for a body-level `errors` array (fail-closed: GraphQL can
//     return HTTP 200 with partial `errors`, which a caller that only checks
//     HTTP status would silently accept)
//
// Usage (live):
//   node scripts/check-retro-live-verification.mjs \
//     --manifest-json artifacts/retro-live-verification-manifest.json
//
// Usage (fixture, for negative-path tests / CI without network):
//   node scripts/check-retro-live-verification.mjs \
//     --manifest-json <path> --execution-profile fixture \
//     --fixture-comments-json <path> --fixture-review-pages-json <path>
//
// pnpm run retro-live-verification:verify -- <flags above>

import { spawnSync } from 'node:child_process'
import { readFile } from 'node:fs/promises'
import { fileURLToPath } from 'node:url'

import { CliError, parseArgs, usageError } from './agent-logs/lib/args.mjs'
import {
  GhCliIssueCommentsClient,
  listAllIssueCommentsStructured,
} from './agent-logs/lib/github-comments.mjs'
import { validateManifestWithAjv } from './generate-retro-live-verification.mjs'

export const SCHEMA = 'retro_live_verification_check_result/v1'
const MAX_REVIEW_THREAD_PAGES = 100

const CLI_OPTION_SPEC = {
  '--manifest-json': { key: 'manifestJson', required: true },
  '--execution-profile': { key: 'executionProfile', defaultValue: 'live' },
  '--fixture-comments-json': { key: 'fixtureCommentsJson' },
  '--fixture-review-pages-json': { key: 'fixtureReviewPagesJson' },
}

function normalizeExecutionProfile(value) {
  if (value !== 'live' && value !== 'fixture') {
    throw usageError('retro_live_verification_check.execution_profile', 'execution-profile must be live or fixture')
  }
  return value
}

function parseCanonicalComment(comment, ownershipMarker) {
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
  return { matches: true, malformed: false, digest: digestMatch.groups.digest, authorLogin: comment?.user?.login ?? null }
}

/**
 * Pure domain check over an already-fetched (or fixture-loaded) comment
 * list. Never converts "no canonical comment found" / "malformed" /
 * "untrusted author" / "stale digest" into a pass.
 */
export function checkCanonicalComment({ manifest, comments }) {
  const errors = []
  const { ownership_marker: ownershipMarker, body_digest: expectedDigest } = manifest.canonical_comment
  const parsed = comments.map((comment) => ({ comment, ...parseCanonicalComment(comment, ownershipMarker) }))
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
    errors.push({ code: 'retro_live_verification_check.stale_digest', message: `canonical comment digest ${match.digest} does not match manifest expectation ${expectedDigest}` })
  }

  return { ok: errors.length === 0, errors }
}

/**
 * Pure domain check over an already-fetched (or fixture-loaded) sequence of
 * GraphQL review-thread pages. Each page is shaped like
 * `{ errors?: [...], hasNextPage: boolean, threadCount: number }`.
 * Fails closed on: any page carrying a non-empty `errors` array, or the
 * sequence never reaching `hasNextPage: false` within the page budget.
 */
export function checkReviewThreadPagination(pages) {
  const errors = []
  let sawTerminalPage = false
  let totalThreadCount = 0

  for (const [index, page] of pages.entries()) {
    if (Array.isArray(page.errors) && page.errors.length > 0) {
      errors.push({ code: 'retro_live_verification_check.graphql_errors_present', message: `GraphQL page ${index} returned a non-empty errors array` })
      return { ok: false, errors, totalThreadCount, pagesChecked: index + 1 }
    }
    totalThreadCount += Number(page.threadCount ?? 0)
    if (page.hasNextPage === false) {
      sawTerminalPage = true
      break
    }
  }

  if (!sawTerminalPage) {
    errors.push({ code: 'retro_live_verification_check.pagination_exhausted', message: `review-thread pagination did not reach hasNextPage:false within ${pages.length} page(s)` })
    return { ok: false, errors, totalThreadCount, pagesChecked: pages.length }
  }

  return { ok: true, errors: [], totalThreadCount, pagesChecked: pages.length }
}

function runReviewThreadGraphqlQuery({ repo, pullNumber, after }) {
  const [owner, name] = String(repo).split('/')
  const query = [
    'query($owner: String!, $name: String!, $pullNumber: Int!, $after: String) {',
    '  repository(owner: $owner, name: $name) {',
    '    pullRequest(number: $pullNumber) {',
    '      reviewThreads(first: 100, after: $after) {',
    '        nodes { id }',
    '        pageInfo { hasNextPage endCursor }',
    '      }',
    '    }',
    '  }',
    '}',
  ].join('\n')
  const result = spawnSync('gh', [
    'api', 'graphql',
    '-f', `query=${query}`,
    '-F', `owner=${owner}`,
    '-F', `name=${name}`,
    '-F', `pullNumber=${pullNumber}`,
    '-F', `after=${after ?? ''}`,
  ], { encoding: 'utf-8' })
  if (result.status !== 0) {
    throw new Error(`gh api graphql failed: ${(result.stderr ?? '').trim() || (result.stdout ?? '').trim()}`)
  }
  const parsed = JSON.parse(result.stdout)
  const reviewThreads = parsed?.data?.repository?.pullRequest?.reviewThreads
  return {
    errors: Array.isArray(parsed?.errors) ? parsed.errors : [],
    threadCount: Array.isArray(reviewThreads?.nodes) ? reviewThreads.nodes.length : 0,
    hasNextPage: reviewThreads?.pageInfo?.hasNextPage === true,
    endCursor: reviewThreads?.pageInfo?.endCursor ?? null,
  }
}

async function fetchLiveReviewThreadPages({ repo, pullNumber }) {
  const pages = []
  let after = null
  for (let page = 0; page < MAX_REVIEW_THREAD_PAGES; page += 1) {
    const result = runReviewThreadGraphqlQuery({ repo, pullNumber, after })
    pages.push(result)
    if (result.errors.length > 0 || result.hasNextPage === false) {
      return pages
    }
    after = result.endCursor
  }
  return pages
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
  const options = parseArgs(process.argv.slice(2), CLI_OPTION_SPEC)
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
    if (listing.pagination_exhausted) {
      process.stdout.write(`${JSON.stringify({ schema: SCHEMA, verification_status: 'error', error_code: 'retro_live_verification_check.pagination_exhausted', errors: [] })}\n`)
      process.exitCode = 2
      return
    }
    comments = listing.comments
  }

  const commentCheck = checkCanonicalComment({ manifest, comments })
  const errors = [...commentCheck.errors]

  let reviewThreadCheck = { ok: true, errors: [], totalThreadCount: 0, pagesChecked: 0 }
  if (manifest.context_assertions.target_type === 'pull_request') {
    let pages
    if (executionProfile === 'fixture') {
      if (!options.fixtureReviewPagesJson) {
        throw usageError('retro_live_verification_check.fixture_review_pages_json_required', '--fixture-review-pages-json is required when --execution-profile is fixture and target-type is pull_request')
      }
      pages = await loadJsonFile(options.fixtureReviewPagesJson, 'retro_live_verification_check.fixture_review_pages_json_invalid')
    } else {
      pages = await fetchLiveReviewThreadPages({ repo: manifest.canonical_comment.repo, pullNumber: manifest.context_assertions.target_number })
    }
    reviewThreadCheck = checkReviewThreadPagination(pages)
    errors.push(...reviewThreadCheck.errors)
  }

  const ok = commentCheck.ok && reviewThreadCheck.ok
  process.stdout.write(`${JSON.stringify({
    schema: SCHEMA,
    verification_status: ok ? 'pass' : 'fail',
    execution_profile: executionProfile,
    checked_at: new Date().toISOString(),
    review_thread_pagination: {
      pages_checked: reviewThreadCheck.pagesChecked,
      total_thread_count: reviewThreadCheck.totalThreadCount,
    },
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
