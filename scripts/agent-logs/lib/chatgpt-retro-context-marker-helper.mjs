import { readFile } from 'fs/promises'
import { Buffer } from 'buffer'
import { fileURLToPath } from 'url'

import {
  extractPayloadFromMarkdown,
  renderPublicMarkdown,
  scanPublicSafety,
  validateAgentRetroIndex,
  validateAgentRunReport,
  validateChatgptRetroContextMarker,
  validateMarkdownCandidate,
} from '../../lib/agent-run-report-validation.mjs'
import {
  GhCliIssueCommentsClient,
  listAllIssueCommentsStructured,
  parseMarkerComment,
  sha256Hex,
} from './github-comments.mjs'
import { buildSourceCommentSetDigest } from './retro-index-builder.mjs'
import {
  parseRetroDigestMarker,
  parseRetroOwnershipMarker,
  validateRetroCommentBody,
} from './retro-index-comment-helper.mjs'
import { parseArgs, printCliError, runtimeError } from './args.mjs'

export const CHATGPT_RETRO_CONTEXT_OWNERSHIP_PATTERN = /^<!--\s*CHATGPT_RETRO_CONTEXT_V1 repo=(?<repo>[A-Za-z0-9_.-]+\/[A-Za-z0-9_.-]+) target=(?<targetType>issue|pull_request):(?<targetNumber>[0-9]+) parent_issue=(?<parentIssue>[0-9]+)\s*-->$/u
export const CHATGPT_RETRO_CONTEXT_DIGEST_PATTERN = /^<!--\s*CHATGPT_RETRO_CONTEXT_DIGEST_V1 sha256=(?<digest>[a-f0-9]{64})\s*-->$/iu

const OWNERSHIP_SCAN = /<!--\s*CHATGPT_RETRO_CONTEXT_V1\s+repo=[^>]*-->/gu
const DIGEST_SCAN = /<!--\s*CHATGPT_RETRO_CONTEXT_DIGEST_V1\b[^>]*-->/giu
const MARKER_INTENT_PREFIX = /^<!--\s*CHATGPT_RETRO_CONTEXT_(?:V1|DIGEST_V1)\b/u
const MAX_GITHUB_COMMENT_BYTES = 65536
const CLI_OPTION_SPEC = {
  '--command': { key: 'command', required: true },
  '--repo': { key: 'repo' },
  '--target-type': { key: 'targetType' },
  '--target-number': { key: 'targetNumber' },
  '--parent-issue': { key: 'parentIssue' },
  '--payload-markdown-file': { key: 'payloadMarkdownFile' },
  '--marker-comment-json': { key: 'markerCommentJson' },
  '--github-comments-json': { key: 'githubCommentsJson', multiple: true },
  '--marker-comment-url': { key: 'markerCommentUrl' },
  '--expected-supersedes-digest': { key: 'expectedSupersedesDigest' },
  '--dry-run': { key: 'dryRun', defaultValue: 'true' },
  '--confirm-live': { key: 'confirmLive', defaultValue: 'false' },
}

function countMatches(text, pattern) {
  return Array.from(text.matchAll(pattern)).length
}

function normalizeIssueNumber(value, fieldName) {
  const numberValue = Number(value)
  if (!Number.isInteger(numberValue) || numberValue <= 0) {
    throw runtimeError(`chatgpt_retro_context.${fieldName}`, `${fieldName} must be a positive integer`)
  }
  return numberValue
}

function normalizeTargetType(value) {
  if (value !== 'issue' && value !== 'pull_request') {
    throw runtimeError('chatgpt_retro_context.target_type', 'target type must be issue or pull_request')
  }
  return value
}

function normalizeRepo(repo) {
  if (typeof repo !== 'string' || !/^[A-Za-z0-9_.-]+\/[A-Za-z0-9_.-]+$/u.test(repo)) {
    throw runtimeError('chatgpt_retro_context.repo', 'repo must be an owner/name string')
  }
  return repo
}

function parseBooleanFlag(value, optionName) {
  if (value === 'true') {
    return true
  }
  if (value === 'false') {
    return false
  }
  throw runtimeError('chatgpt_retro_context.invalid_flag', `${optionName} must be true or false`)
}

export function buildChatgptRetroContextOwnership(input) {
  return {
    repo: normalizeRepo(input.repo),
    targetType: normalizeTargetType(input.targetType),
    targetNumber: normalizeIssueNumber(input.targetNumber, 'target_number'),
    parentIssue: normalizeIssueNumber(input.parentIssue, 'parent_issue'),
  }
}

export function formatChatgptRetroContextOwnershipMarker(input) {
  const ownership = buildChatgptRetroContextOwnership(input)
  return `<!-- CHATGPT_RETRO_CONTEXT_V1 repo=${ownership.repo} target=${ownership.targetType}:${ownership.targetNumber} parent_issue=${ownership.parentIssue} -->`
}

export function formatChatgptRetroContextDigestMarker(digest) {
  if (typeof digest !== 'string' || !/^[a-f0-9]{64}$/iu.test(digest)) {
    throw runtimeError('chatgpt_retro_context.digest', 'sha256 digest must be 64 hex characters')
  }
  return `<!-- CHATGPT_RETRO_CONTEXT_DIGEST_V1 sha256=${digest.toLowerCase()} -->`
}

export function parseChatgptRetroContextOwnershipMarker(line) {
  const match = line.trim().match(CHATGPT_RETRO_CONTEXT_OWNERSHIP_PATTERN)
  if (!match?.groups) {
    return null
  }
  return {
    repo: match.groups.repo,
    targetType: match.groups.targetType,
    targetNumber: Number(match.groups.targetNumber),
    parentIssue: Number(match.groups.parentIssue),
  }
}

export function parseChatgptRetroContextDigestMarker(line) {
  const match = line.trim().match(CHATGPT_RETRO_CONTEXT_DIGEST_PATTERN)
  if (!match?.groups) {
    return null
  }
  return match.groups.digest.toLowerCase()
}

function ownershipEquals(left, right) {
  return left.repo === right.repo
    && left.targetType === right.targetType
    && left.targetNumber === right.targetNumber
    && left.parentIssue === right.parentIssue
}

// CommonMark 0.31.2 defines a blank line as a line containing nothing but
// U+0020 SPACE / U+0009 TAB characters (or nothing at all). `String.prototype.trim()`
// strips a much wider set of Unicode whitespace (NBSP U+00A0, em space U+2003,
// form feed U+000C, etc.), which would incorrectly classify those lines as
// blank and let a later line be mistaken for the "first non-empty line".
const COMMONMARK_BLANK_LINE_PATTERN = /^[\t ]*$/u

function findColumnZeroNonEmptyLines(body, count) {
  const rawLines = typeof body === 'string' ? body.split('\n') : []
  const result = []
  for (const rawLine of rawLines) {
    const stripped = rawLine.replace(/\r$/u, '')
    if (COMMONMARK_BLANK_LINE_PATTERN.test(stripped)) {
      continue
    }
    result.push(stripped)
    if (result.length >= count) {
      break
    }
  }
  return result
}

/**
 * Shared marker candidate classifier used by every read/write path that
 * needs to decide whether a GitHub comment body is attempting to be a
 * `CHATGPT_RETRO_CONTEXT_V1` ownership marker.
 *
 * Only the first non-empty line, at column 0 (no leading whitespace, not
 * inside a fenced code block / blockquote / list item / indented code
 * block -- any of those prefixes mean the raw line does not literally
 * start with `<!--` at offset 0), is ever inspected. Marker names
 * mentioned in prose, inline code, or fenced code never reach this far
 * because the first non-empty line of the comment is not itself the
 * marker attempt.
 *
 * Returns one of:
 * - `not_marker`: the first non-empty line does not attempt marker syntax
 * - `valid_marker`: the first non-empty line is a canonical ownership marker
 * - `malformed_marker_intent`: the first non-empty line clearly intends to
 *   be a `CHATGPT_RETRO_CONTEXT_V1` / `CHATGPT_RETRO_CONTEXT_DIGEST_V1`
 *   marker (starts with the literal prefix) but fails strict validation
 */
export function classifyChatgptRetroContextMarkerCandidate(body) {
  const [firstLine] = findColumnZeroNonEmptyLines(body, 1)
  if (firstLine === undefined) {
    return { state: 'not_marker', line: null }
  }
  if (!MARKER_INTENT_PREFIX.test(firstLine)) {
    return { state: 'not_marker', line: firstLine }
  }
  const trimmed = firstLine.trim()
  if (CHATGPT_RETRO_CONTEXT_OWNERSHIP_PATTERN.test(trimmed)) {
    return { state: 'valid_marker', line: firstLine }
  }
  return { state: 'malformed_marker_intent', line: firstLine }
}

export function validateChatgptRetroContextCommentBody(body, {
  expectedOwnership = null,
  expectedDigest = null,
  maxBytes = MAX_GITHUB_COMMENT_BYTES,
} = {}) {
  const [firstLine, secondLine] = findColumnZeroNonEmptyLines(body, 2)
  const classification = classifyChatgptRetroContextMarkerCandidate(body)
  const ownership = classification.state === 'valid_marker'
    ? parseChatgptRetroContextOwnershipMarker(firstLine)
    : null
  const digest = secondLine !== undefined ? parseChatgptRetroContextDigestMarker(secondLine) : null
  const errors = []

  if (countMatches(body, OWNERSHIP_SCAN) !== 1) {
    errors.push({
      path: 'body',
      code: 'chatgpt_retro_context.ownership_marker_count',
      message: 'ownership marker must appear exactly once',
    })
  }
  if (countMatches(body, DIGEST_SCAN) !== 1) {
    errors.push({
      path: 'body',
      code: 'chatgpt_retro_context.digest_marker_count',
      message: 'digest marker must appear exactly once',
    })
  }
  if (!ownership) {
    errors.push({
      path: 'body.first_non_empty_line',
      code: 'chatgpt_retro_context.ownership_marker_position',
      message: 'first non-empty line must be a valid context ownership marker',
    })
  }
  if (!digest) {
    errors.push({
      path: 'body.second_non_empty_line',
      code: 'chatgpt_retro_context.digest_marker_position',
      message: 'second non-empty line must be a valid context digest marker',
    })
  }
  if (expectedOwnership && ownership && !ownershipEquals(ownership, buildChatgptRetroContextOwnership(expectedOwnership))) {
    errors.push({
      path: 'body.first_non_empty_line',
      code: 'chatgpt_retro_context.ownership_mismatch',
      message: 'ownership marker does not match the expected repo / target / parent issue tuple',
    })
  }
  if (expectedDigest && digest && digest !== expectedDigest.toLowerCase()) {
    errors.push({
      path: 'body.second_non_empty_line',
      code: 'chatgpt_retro_context.digest_mismatch',
      message: 'digest marker does not match the canonical payload markdown digest',
    })
  }

  const byteLength = Buffer.byteLength(body, 'utf8')
  if (byteLength > maxBytes) {
    errors.push({
      path: 'body',
      code: 'chatgpt_retro_context.body_too_large',
      message: `comment body exceeds ${maxBytes} UTF-8 bytes`,
    })
  }

  return {
    valid: errors.length === 0,
    errors,
    byteLength,
    ownership,
    digest,
    classificationState: classification.state,
  }
}

export function computeChatgptRetroContextPayloadDigest(payload) {
  const normalizedPayload = JSON.parse(JSON.stringify(payload))
  if (normalizedPayload?.canonicalization) {
    normalizedPayload.canonicalization.payload_digest = 'sha256:0000000000000000000000000000000000000000000000000000000000000000'
  }
  return `sha256:${sha256Hex(stableStringify(normalizedPayload))}`
}

function stableStringify(value) {
  if (Array.isArray(value)) {
    return `[${value.map((entry) => stableStringify(entry)).join(',')}]`
  }
  if (value && typeof value === 'object') {
    return `{${Object.keys(value).sort().map((key) => `${JSON.stringify(key)}:${stableStringify(value[key])}`).join(',')}}`
  }
  return JSON.stringify(value)
}

export function buildChatgptRetroContextCommentBody({ ownership, payloadMarkdown }) {
  const validation = validateMarkdownCandidate(payloadMarkdown, 'chatgpt_retro_context_marker/v1')
  if (!validation.valid) {
    const [firstError] = validation.errors
    throw runtimeError(firstError.code, firstError.message)
  }

  const payloadDigest = sha256Hex(payloadMarkdown)
  const body = [
    formatChatgptRetroContextOwnershipMarker(ownership),
    formatChatgptRetroContextDigestMarker(payloadDigest),
    '',
    payloadMarkdown,
  ].join('\n')

  const bodyValidation = validateChatgptRetroContextCommentBody(body, {
    expectedOwnership: ownership,
    expectedDigest: payloadDigest,
  })
  if (!bodyValidation.valid) {
    const [firstError] = bodyValidation.errors
    throw runtimeError(firstError.code, firstError.message)
  }

  return {
    body,
    digest: `sha256:${payloadDigest}`,
    byteLength: bodyValidation.byteLength,
    ownership: buildChatgptRetroContextOwnership(ownership),
  }
}

export function parseChatgptRetroContextComment(comment) {
  const body = typeof comment?.body === 'string' ? comment.body : ''
  const validation = validateChatgptRetroContextCommentBody(body)
  if (!validation.ownership) {
    return {
      ok: false,
      // A comment whose first non-empty line clearly intends to be a
      // CHATGPT_RETRO_CONTEXT_V1 marker but fails the strict ownership
      // pattern (classification `malformed_marker_intent`) must still be
      // reported as malformed even when ownership could not be parsed at
      // all -- otherwise it silently falls through as "not a marker" and
      // upsert/post-write callers would treat the write path as clear
      // while resolve-live (which classifies independently of ownership)
      // blocks on the very same comment (split-brain, see #1501 P0 review).
      malformed: validation.classificationState === 'malformed_marker_intent',
      classificationState: validation.classificationState,
      body,
      comment,
    }
  }
  return {
    ok: validation.valid,
    malformed: !validation.valid,
    classificationState: validation.classificationState,
    body,
    comment,
    ownership: validation.ownership,
    digest: validation.digest ? `sha256:${validation.digest}` : null,
    byteLength: validation.byteLength,
    errors: validation.errors,
  }
}

// Finds any comment in the given list whose first non-empty line clearly
// intends to be a CHATGPT_RETRO_CONTEXT_V1 / CHATGPT_RETRO_CONTEXT_DIGEST_V1
// marker but fails strict validation, regardless of whether ownership could
// be parsed from it. Mirrors the unconditional (not ownership-filtered)
// scan resolveChatgptRetroContextLive() already performs, so upsert/readback
// and resolve-live never disagree about a malformed marker intent comment
// (see #1501 P0 review: split-brain between post and resolve paths).
function findMalformedMarkerIntentComment(comments) {
  return comments.find((comment) => (
    classifyChatgptRetroContextMarkerCandidate(comment?.body).state === 'malformed_marker_intent'
  )) ?? null
}

// Lists every issue comment for a write-path pre-check or post-write
// readback. Uses the structured pagination result and refuses to proceed
// (fail-closed) whenever pagination did not fully complete, instead of the
// thin `listAllIssueComments()` wrapper which only throws on the
// `pagination_exhausted` link-header case and silently swallows
// `page_budget_exhausted` (fixed-page-budget clients), which could hide a
// later page containing a duplicate or malformed ownership marker.
async function listAllCommentsForWrite(client, { repo, issueNumber }) {
  const listed = await listAllIssueCommentsStructured(client, { repo, issueNumber })
  if (listed.page_budget_exhausted || listed.pagination_exhausted) {
    throw runtimeError('chatgpt_retro_context.blocked_page_budget_exhausted', 'comment pagination did not complete before write; refusing to write to avoid missing an existing marker on a later page')
  }
  return listed.comments
}

/**
 * Post-write readback (AC12): re-lists all issue comments and confirms
 * that exactly one comment matches the stable ownership marker after a
 * create or supersede write, and that the comment actually written
 * matches the candidate that was supposed to be written (comment id,
 * digest, ok/malformed state). This guards against a concurrent write
 * (race) creating a duplicate ownership marker, and against a readback
 * that merely counts ownership matches without confirming the write's
 * own content landed correctly.
 */
async function verifySingleOwnershipMarkerAfterWrite(client, {
  repo,
  issueNumber,
  ownership,
  candidateDigest,
  writtenCommentId,
}) {
  if (typeof client.getIssueComment === 'function' && writtenCommentId !== null && writtenCommentId !== undefined) {
    const directComment = await client.getIssueComment({ repo, commentId: writtenCommentId })
    const directParsed = parseChatgptRetroContextComment(directComment)
    if (
      !directParsed.ok
      || directParsed.malformed
      || !directParsed.ownership
      || !ownershipEquals(directParsed.ownership, ownership)
      || directParsed.digest !== candidateDigest
      || directParsed.comment?.id !== writtenCommentId
    ) {
      throw runtimeError('chatgpt_retro_context.blocked_post_write_mismatch', 'post-write direct readback of the written comment id does not match the candidate that was written (ok/malformed/ownership/digest/comment id)')
    }
  }

  const comments = await listAllCommentsForWrite(client, { repo, issueNumber })
  const malformedIntent = findMalformedMarkerIntentComment(comments)
  if (malformedIntent) {
    throw runtimeError('chatgpt_retro_context.blocked_malformed_marker_syntax', 'post-write readback found a comment with malformed marker intent syntax')
  }
  const matches = comments
    .map((comment) => parseChatgptRetroContextComment(comment))
    .filter((entry) => entry.ownership && ownershipEquals(entry.ownership, ownership))
  if (matches.length === 0) {
    throw runtimeError('chatgpt_retro_context.blocked_post_write_missing', 'post-write readback found no ownership marker comment after writing')
  }
  if (matches.length >= 2) {
    throw runtimeError('chatgpt_retro_context.blocked_post_write_duplicate', 'post-write readback found more than one ownership marker comment after writing')
  }
  const [singleMatch] = matches
  if (
    !singleMatch.ok
    || singleMatch.malformed
    || singleMatch.digest !== candidateDigest
    || singleMatch.comment?.id !== writtenCommentId
  ) {
    throw runtimeError('chatgpt_retro_context.blocked_post_write_mismatch', 'post-write readback found a comment for this ownership but it does not match what was written (ok/malformed/digest/comment id)')
  }
  return singleMatch
}

export async function upsertChatgptRetroContextComment(client, {
  repo,
  targetType,
  targetNumber,
  parentIssue,
  payloadMarkdown,
  dryRun = true,
  expectedSupersedesDigest = null,
}) {
  const ownership = buildChatgptRetroContextOwnership({ repo, targetType, targetNumber, parentIssue })
  const candidate = buildChatgptRetroContextCommentBody({ ownership, payloadMarkdown })
  const comments = await listAllCommentsForWrite(client, {
    repo,
    issueNumber: ownership.targetNumber,
  })
  // Reject unconditionally on malformed_marker_intent (not filtered by
  // ownership) *before* any ownership-scoped matching. Without this, a
  // comment whose ownership cannot be parsed (e.g. missing parent_issue)
  // classifies as malformed_marker_intent but has no `ownership` tuple, so
  // the ownership-scoped `matches` filter below would never see it and
  // upsert would fall through to `create` -- while resolve-live classifies
  // the very same comment independently and blocks with
  // blocked_malformed_marker_syntax. See #1501 P0 review.
  const malformedIntent = findMalformedMarkerIntentComment(comments)
  if (malformedIntent) {
    throw runtimeError('chatgpt_retro_context.blocked_malformed_marker_syntax', 'existing comment on this target has malformed marker intent syntax; refusing to write until it is resolved')
  }
  const parsedComments = comments.map((comment) => parseChatgptRetroContextComment(comment))
  const malformedMatch = parsedComments.find((entry) => entry.ownership && ownershipEquals(entry.ownership, ownership) && entry.malformed)
  if (malformedMatch) {
    throw runtimeError('chatgpt_retro_context.blocked_malformed', 'existing context marker comment is malformed; refusing to update')
  }
  const matches = parsedComments.filter((entry) => entry.ownership && ownershipEquals(entry.ownership, ownership))
  if (matches.length >= 2) {
    throw runtimeError('chatgpt_retro_context.blocked_duplicate', 'multiple existing context marker comments match the stable ownership marker')
  }
  if (matches.length === 0) {
    if (dryRun) {
      return { action: 'create', digest: candidate.digest, comment_id: null, comment_url: null }
    }
    const created = await client.createIssueComment({
      repo,
      issueNumber: ownership.targetNumber,
      body: candidate.body,
    })
    await verifySingleOwnershipMarkerAfterWrite(client, {
      repo,
      issueNumber: ownership.targetNumber,
      ownership,
      candidateDigest: candidate.digest,
      writtenCommentId: created?.id ?? null,
    })
    return { action: 'create', digest: candidate.digest, comment_id: created?.id ?? null, comment_url: created?.html_url ?? created?.url ?? null }
  }

  const [existing] = matches
  if (existing.digest === candidate.digest) {
    return {
      action: 'noop',
      digest: candidate.digest,
      comment_id: existing.comment?.id ?? null,
      comment_url: existing.comment?.html_url ?? existing.comment?.url ?? null,
    }
  }
  if (expectedSupersedesDigest === null) {
    throw runtimeError('chatgpt_retro_context.blocked_missing_supersedes_digest', 'expectedSupersedesDigest is required before updating an existing context marker')
  }
  if (existing.digest !== expectedSupersedesDigest) {
    throw runtimeError('chatgpt_retro_context.blocked_stale_write', 'existing context marker digest changed before update')
  }

  if (dryRun) {
    return {
      action: 'supersede',
      digest: candidate.digest,
      comment_id: existing.comment?.id ?? null,
      comment_url: existing.comment?.html_url ?? existing.comment?.url ?? null,
    }
  }
  if (typeof client.getIssueComment === 'function') {
    const refreshedComment = await client.getIssueComment({
      repo,
      commentId: existing.comment.id,
    })
    const refreshedParsed = parseChatgptRetroContextComment(refreshedComment)
    if (!refreshedParsed.ownership || !ownershipEquals(refreshedParsed.ownership, ownership) || refreshedParsed.malformed) {
      throw runtimeError('chatgpt_retro_context.blocked_malformed', 'existing context marker comment became malformed before update')
    }
    if (refreshedParsed.digest !== expectedSupersedesDigest) {
      throw runtimeError('chatgpt_retro_context.blocked_stale_write', 'existing context marker digest changed before update')
    }
  }
  const updated = await client.updateIssueComment({
    repo,
    commentId: existing.comment.id,
    body: candidate.body,
  })
  await verifySingleOwnershipMarkerAfterWrite(client, {
    repo,
    issueNumber: ownership.targetNumber,
    ownership,
    candidateDigest: candidate.digest,
    writtenCommentId: updated?.id ?? existing.comment?.id ?? null,
  })
  return {
    action: 'supersede',
    digest: candidate.digest,
    comment_id: updated?.id ?? existing.comment?.id ?? null,
    comment_url: updated?.html_url ?? updated?.url ?? existing.comment?.html_url ?? null,
    superseded_digest: expectedSupersedesDigest,
  }
}

function commentUrlFromResponse(response) {
  return response?.html_url ?? response?.url ?? null
}

function parseIssueNumberFromCommentUrl(url) {
  const match = typeof url === 'string'
    ? url.match(/\/(?:issues|pull)\/(?<issueNumber>[0-9]+)#issuecomment-[0-9]+$/u)
    : null
  return match?.groups ? Number(match.groups.issueNumber) : null
}

async function listCommentsByIssueNumber(client, { repo, issueNumber }) {
  return listAllIssueCommentsStructured(client, { repo, issueNumber })
}

async function loadReferencedCommentUniverse(client, ownership, matchCommentUrl) {
  const issueNumbers = new Set([ownership.targetNumber, ownership.parentIssue])
  if (typeof matchCommentUrl === 'string') {
    const parsedIssueNumber = parseIssueNumberFromCommentUrl(matchCommentUrl)
    if (parsedIssueNumber !== null) {
      issueNumbers.add(parsedIssueNumber)
    }
  }
  const pages = []
  for (const issueNumber of issueNumbers) {
    pages.push(await listCommentsByIssueNumber(client, {
      repo: ownership.repo,
      issueNumber,
    }))
  }
  return {
    comments: pages.flatMap((entry) => entry.comments),
    scannedComments: pages.reduce((sum, entry) => sum + entry.scannedComments, 0),
    pageCount: pages.reduce((sum, entry) => sum + entry.pageCount, 0),
    page_budget_exhausted: pages.some((entry) => entry.page_budget_exhausted),
    pagination_exhausted: pages.some((entry) => entry.pagination_exhausted),
    pagination_mode: pages.map((entry) => entry.pagination_mode).filter(Boolean),
  }
}

function buildReferenceChainFromParsedMarker(parsedMarker, comments) {
  const extraction = extractPayloadFromMarkdown(parsedMarker.body, 'chatgpt_retro_context_marker/v1')
  if (!extraction.ok) {
    throw runtimeError(extraction.error.code, extraction.error.message)
  }
  const payload = extraction.payload
  assertValidation(validateChatgptRetroContextMarker(payload), 'chatgpt_retro_context.marker_payload_invalid')
  const expectedPayloadDigest = computeChatgptRetroContextPayloadDigest(payload)
  if (payload.canonicalization?.payload_digest !== expectedPayloadDigest) {
    throw runtimeError('chatgpt_retro_context.payload_digest_mismatch', 'payload canonicalization digest does not match the canonical marker payload')
  }
  if (
    payload.repo !== parsedMarker.ownership.repo
    || payload.target?.type !== parsedMarker.ownership.targetType
    || payload.target?.number !== parsedMarker.ownership.targetNumber
    || payload.parent_issue !== parsedMarker.ownership.parentIssue
  ) {
    throw runtimeError('chatgpt_retro_context.payload_ownership_mismatch', 'marker ownership comment and embedded payload ownership must match')
  }

  const reportPayloads = []
  const evidenceRefs = []
  const sourceCommentRefs = []
  for (const reportRef of payload.refs.run_reports) {
    const reportComment = findCommentByUrl(comments, reportRef.comment_url)
    if (!reportComment) {
      throw runtimeError('chatgpt_retro_context.report_comment_missing', `referenced run report comment not found: ${reportRef.comment_url}`)
    }
    const parsedReport = parseMarkerComment(reportComment)
    if (!parsedReport.ok) {
      throw runtimeError('chatgpt_retro_context.report_comment_invalid', `referenced run report comment is invalid: ${reportRef.comment_url}`)
    }
    const normalizedReportDigest = parsedReport.digest ? `sha256:${parsedReport.digest}` : null
    if (normalizedReportDigest !== reportRef.payload_digest) {
      throw runtimeError('chatgpt_retro_context.report_digest_mismatch', `run report digest mismatch: ${reportRef.comment_url}`)
    }
    const reportExtraction = extractPayloadFromMarkdown(parsedReport.body, 'agent_run_report/v1')
    if (!reportExtraction.ok) {
      throw runtimeError(reportExtraction.error.code, reportExtraction.error.message)
    }
    assertValidation(validateAgentRunReport(reportExtraction.payload), 'chatgpt_retro_context.report_payload_invalid')
    reportPayloads.push(reportExtraction.payload)
    sourceCommentRefs.push({
      comment_url: reportRef.comment_url,
      source_kind: 'issues',
      source_number: payload.target.number,
      body_digest: reportRef.payload_digest,
    })
    evidenceRefs.push({
      kind: 'github_comment',
      ref: reportRef.comment_url,
      digest: reportRef.payload_digest,
      validation_verdict: reportRef.validation_verdict,
    })
  }

  const retroComment = findCommentByUrl(comments, payload.refs.retro_index.comment_url)
  if (!retroComment) {
    throw runtimeError('chatgpt_retro_context.retro_comment_missing', `referenced retro index comment not found: ${payload.refs.retro_index.comment_url}`)
  }
  const parsedRetro = parseRetroMarkerComment(retroComment)
  if (!parsedRetro.ok) {
    throw runtimeError('chatgpt_retro_context.retro_comment_invalid', 'referenced retro index comment is invalid')
  }
  if (parsedRetro.digest?.canonicalDigest !== payload.refs.retro_index.payload_digest) {
    throw runtimeError('chatgpt_retro_context.retro_digest_mismatch', 'retro index payload digest mismatch')
  }
  if (parsedRetro.digest?.sourceSetDigest !== payload.refs.retro_index.source_set_digest) {
    throw runtimeError('chatgpt_retro_context.source_set_digest_mismatch', 'retro index source-set digest mismatch')
  }
  if (
    parsedRetro.ownership?.repo !== payload.repo
    || parsedRetro.ownership?.parentIssue !== payload.parent_issue
  ) {
    throw runtimeError('chatgpt_retro_context.retro_ownership_mismatch', 'retro index ownership must match marker repo / parent issue')
  }
  const retroExtraction = extractPayloadFromMarkdown(parsedRetro.body, 'agent_retro_index/v1')
  if (!retroExtraction.ok) {
    throw runtimeError(retroExtraction.error.code, retroExtraction.error.message)
  }
  assertValidation(validateAgentRetroIndex(retroExtraction.payload), 'chatgpt_retro_context.retro_payload_invalid')
  sourceCommentRefs.push({
    comment_url: payload.refs.retro_index.comment_url,
    source_kind: 'issues',
    source_number: payload.parent_issue,
    body_digest: payload.refs.retro_index.payload_digest,
  })
  const recomputedSourceSetDigest = buildSourceCommentSetDigest(sourceCommentRefs)
  if (recomputedSourceSetDigest !== payload.refs.retro_index.source_set_digest) {
    throw runtimeError('chatgpt_retro_context.source_set_digest_recompute_mismatch', 'retro index source-set digest must match the recomputed referenced comment set')
  }

  const safetyScan = scanPublicSafety(payload)
  if (!safetyScan.valid) {
    const [firstError] = safetyScan.errors
    throw runtimeError(firstError.code, firstError.message)
  }

  return {
    payload,
    sources: {
      parent_issue_json: { number: payload.parent_issue, title: `Parent Issue #${payload.parent_issue}` },
      target_issue_json: { number: payload.target.number, title: `Target ${payload.target.type} #${payload.target.number}` },
      retro_index_json: retroExtraction.payload,
      source_set_json: buildSyntheticSourceSet(payload),
      run_reports: reportPayloads,
      evidence_refs: evidenceRefs,
    },
    manifest: [
      { source_kind: 'chatgpt_retro_context_marker', source_ref: 'marker-comment', canonical_digest: parsedMarker.digest, body_digest: parsedMarker.digest },
      ...payload.refs.run_reports.map((entry, index) => ({
        source_kind: 'run_report_comment',
        source_ref: `run_report_comment[${index}]`,
        canonical_digest: entry.payload_digest,
        body_digest: entry.payload_digest,
      })),
      {
        source_kind: 'retro_index_comment',
        source_ref: 'retro_index_comment[0]',
        canonical_digest: payload.refs.retro_index.payload_digest,
        body_digest: payload.refs.retro_index.payload_digest,
      },
    ],
  }
}

function computePublicSafeProjectionDigest(payload) {
  return `sha256:${sha256Hex(stableStringify(payload))}`
}

function withProjectionDigest(payload) {
  return {
    ...payload,
    digest: computePublicSafeProjectionDigest(payload),
  }
}

async function listAllPullRequestReviewsStructured(client, {
  repo,
  pullNumber,
  perPage = 100,
  maxPages = 100,
}) {
  const reviews = []
  for (let page = 1; page <= maxPages; page += 1) {
    const pageResult = await client.listPullRequestReviewsPage({ repo, pullNumber, page, perPage })
    reviews.push(...(pageResult.items ?? []))
    if (pageResult.hasNextPage === false) {
      return { reviews, pageCount: page, pageBudgetExhausted: false }
    }
  }
  return { reviews, pageCount: maxPages, pageBudgetExhausted: true }
}

async function listAllPullRequestReviewCommentsStructured(client, {
  repo,
  pullNumber,
  perPage = 100,
  maxPages = 100,
}) {
  const comments = []
  for (let page = 1; page <= maxPages; page += 1) {
    const pageResult = await client.listPullRequestReviewCommentsPage({ repo, pullNumber, page, perPage })
    comments.push(...(pageResult.items ?? []))
    if (pageResult.hasNextPage === false) {
      return { comments, pageCount: page, pageBudgetExhausted: false }
    }
  }
  return { comments, pageCount: maxPages, pageBudgetExhausted: true }
}

async function listAllPullRequestReviewThreadsStructured(client, {
  repo,
  pullNumber,
  first = 100,
  maxPages = 100,
}) {
  const threads = []
  let after = null
  for (let page = 1; page <= maxPages; page += 1) {
    const pageResult = await client.listPullRequestReviewThreadsPage({ repo, pullNumber, first, after })
    threads.push(...(pageResult.items ?? []))
    if (pageResult.hasNextPage !== true) {
      return { threads, pageCount: page, pageBudgetExhausted: false }
    }
    after = pageResult.endCursor
  }
  return { threads, pageCount: maxPages, pageBudgetExhausted: true }
}

function buildPrReviewSurfaceSummary({ pullNumber, reviews, reviewComments, reviewThreads }) {
  const normalizedReviews = reviews.map((review) => withProjectionDigest({
    id: review.id,
    node_id: review.node_id,
    pull_number: pullNumber,
    state: review.state,
    commit_id: review.commit_id,
    submitted_at: review.submitted_at,
    html_url: review.html_url,
  }))
  const normalizedReviewComments = reviewComments.map((comment) => withProjectionDigest({
    id: comment.id,
    node_id: comment.node_id,
    pull_request_review_id: comment.pull_request_review_id,
    pull_number: pullNumber,
    path: comment.path,
    line: comment.line,
    commit_id: comment.commit_id,
    created_at: comment.created_at,
    updated_at: comment.updated_at,
    html_url: comment.html_url,
  }))
  const normalizedReviewThreads = reviewThreads.map((thread) => withProjectionDigest({
    thread_node_id: thread.id,
    pull_number: pullNumber,
    path: thread.path,
    line: thread.line,
    is_resolved: thread.isResolved,
    is_outdated: thread.isOutdated,
    subject_type: thread.subjectType,
    observed_at: thread.observed_at ?? null,
    comment_count: thread.comments?.totalCount ?? 0,
    thread_comments_complete: thread.comments?.pageInfo?.hasNextPage !== true,
  }))
  return {
    review_ids: normalizedReviews.map((review) => review.id),
    review_comment_ids: normalizedReviewComments.map((comment) => comment.id),
    review_thread_node_ids: normalizedReviewThreads.map((thread) => thread.thread_node_id),
    review_count: normalizedReviews.length,
    review_comment_count: normalizedReviewComments.length,
    review_thread_count: normalizedReviewThreads.length,
    resolved_thread_count: normalizedReviewThreads.filter((thread) => thread.is_resolved).length,
    sample_review: normalizedReviews[0] ?? null,
    sample_review_comment: normalizedReviewComments[0] ?? null,
    sample_review_thread: normalizedReviewThreads.find((thread) => thread.is_resolved) ?? normalizedReviewThreads[0] ?? null,
    object_catalog: {
      reviews_by_id: Object.fromEntries(normalizedReviews.map((review) => [String(review.id), review])),
      review_comments_by_id: Object.fromEntries(normalizedReviewComments.map((comment) => [String(comment.id), {
        review_id: comment.pull_request_review_id,
        ...comment,
      }])),
      review_threads_by_node_id: Object.fromEntries(normalizedReviewThreads.map((thread) => [thread.thread_node_id, thread])),
    },
    projection_digest: computePublicSafeProjectionDigest({
      reviews: normalizedReviews,
      review_comments: normalizedReviewComments,
      review_threads: normalizedReviewThreads,
    }),
  }
}

async function resolvePullRequestReviewSurfaceLive(client, { repo, pullNumber }) {
  const reviewsResult = await listAllPullRequestReviewsStructured(client, { repo, pullNumber })
  const commentsResult = await listAllPullRequestReviewCommentsStructured(client, { repo, pullNumber })
  const threadsResult = await listAllPullRequestReviewThreadsStructured(client, { repo, pullNumber })

  const threadCommentsComplete = threadsResult.threads.every((thread) => thread.comments?.pageInfo?.hasNextPage !== true)
  const pagination = {
    reviews_complete: !reviewsResult.pageBudgetExhausted,
    review_comments_complete: !commentsResult.pageBudgetExhausted,
    review_threads_complete: !threadsResult.pageBudgetExhausted,
    thread_comments_complete: threadCommentsComplete,
  }
  const complete = Object.values(pagination).every((value) => value === true)
  const surfaceSummary = buildPrReviewSurfaceSummary({
    pullNumber,
    reviews: reviewsResult.reviews,
    reviewComments: commentsResult.comments,
    reviewThreads: threadsResult.threads,
  })

  return {
    status: complete ? 'resolved' : 'blocked_page_budget_exhausted',
    pagination: { ...pagination, complete },
    ...surfaceSummary,
  }
}

// Placeholder pr_review_surface component object returned for issue targets so
// that resolveChatgptRetroContextLive always returns the same total-result
// shape (comment_chain + pr_review_surface) regardless of target type / status.
const NOT_APPLICABLE_PR_REVIEW_SURFACE = Object.freeze({
  status: 'not_applicable',
  pagination: null,
  review_ids: [],
  review_comment_ids: [],
  review_thread_node_ids: [],
  review_count: 0,
  review_comment_count: 0,
  review_thread_count: 0,
  resolved_thread_count: 0,
  sample_review: null,
  sample_review_comment: null,
  sample_review_thread: null,
  object_catalog: {
    reviews_by_id: {},
    review_comments_by_id: {},
    review_threads_by_node_id: {},
  },
  projection_digest: null,
})

function aggregatePullRequestContextStatus(commentChainStatus, prReviewSurfaceStatus) {
  if (commentChainStatus === 'resolved' && prReviewSurfaceStatus === 'resolved') {
    return 'resolved'
  }
  if (commentChainStatus !== 'resolved') {
    return commentChainStatus
  }
  return prReviewSurfaceStatus
}

function buildCommentChainPagination(listed, referenceUniverse = null) {
  return {
    page_count: listed.pageCount,
    scanned_comments: listed.scannedComments,
    max_pages: listed.maxPages,
    per_page: listed.perPage,
    endpoint: listed.endpoint,
    pagination_mode: listed.pagination_mode,
    comments_complete: !listed.page_budget_exhausted,
    reference_page_count: referenceUniverse ? referenceUniverse.pageCount : null,
    reference_scanned_comments: referenceUniverse ? referenceUniverse.scannedComments : null,
    reference_pagination_mode: referenceUniverse ? referenceUniverse.pagination_mode : null,
    reference_comments_complete: referenceUniverse
      ? (!referenceUniverse.page_budget_exhausted && !referenceUniverse.pagination_exhausted)
      : null,
  }
}

function buildCommentChainResult({
  status,
  listed,
  referenceUniverse = null,
  commentCount,
  matchedCommentCount,
  markerComment = null,
  digest = null,
  payloadDigest = null,
  evidenceRefCount = 0,
  sourceManifestCount = 0,
  errorCode = null,
  errorMessage = null,
}) {
  return {
    status,
    pagination: buildCommentChainPagination(listed, referenceUniverse),
    comment_count: commentCount,
    matched_comment_count: matchedCommentCount,
    marker_comment: markerComment,
    digest,
    payload_digest: payloadDigest,
    evidence_ref_count: evidenceRefCount,
    source_manifest_count: sourceManifestCount,
    error_code: errorCode,
    error_message: errorMessage,
  }
}

/**
 * Resolves the live CHATGPT_RETRO_CONTEXT_V1 marker chain for a target
 * (issue or pull request). The return value is always a total result:
 * a `comment_chain` component object and a `pr_review_surface` component
 * object (the latter is the `NOT_APPLICABLE_PR_REVIEW_SURFACE` placeholder
 * for issue targets) are present for every status, including the
 * `resolved` happy path.
 */
export async function resolveChatgptRetroContextLive(client, {
  repo,
  targetType,
  targetNumber,
  parentIssue,
  markerCommentUrl = null,
}) {
  const ownership = buildChatgptRetroContextOwnership({ repo, targetType, targetNumber, parentIssue })
  const listed = await listAllIssueCommentsStructured(client, {
    repo: ownership.repo,
    issueNumber: ownership.targetNumber,
  })
  const target = {
    type: ownership.targetType,
    number: ownership.targetNumber,
    endpoint_kind: ownership.targetType === 'pull_request' ? 'issue_comments_for_pull_request' : 'issue_comments_for_issue',
  }
  const prReviewSurface = ownership.targetType === 'pull_request'
    ? await resolvePullRequestReviewSurfaceLive(client, {
        repo: ownership.repo,
        pullNumber: ownership.targetNumber,
      })
    : NOT_APPLICABLE_PR_REVIEW_SURFACE

  function finalize(commentChain, topMarkerCommentUrl) {
    const status = ownership.targetType === 'pull_request'
      ? aggregatePullRequestContextStatus(commentChain.status, prReviewSurface.status)
      : commentChain.status
    return {
      status,
      repo: ownership.repo,
      target,
      parent_issue: ownership.parentIssue,
      marker_comment_url: topMarkerCommentUrl,
      comment_chain: commentChain,
      pr_review_surface: prReviewSurface,
    }
  }

  if (listed.page_budget_exhausted) {
    return finalize(buildCommentChainResult({
      status: 'blocked_page_budget_exhausted',
      listed,
      commentCount: listed.comments.length,
      matchedCommentCount: 0,
    }), markerCommentUrl)
  }

  const parsedComments = listed.comments.map((comment) => parseChatgptRetroContextComment(comment))
  const malformedMarkerLike = listed.comments.find((comment) => (
    classifyChatgptRetroContextMarkerCandidate(comment?.body).state === 'malformed_marker_intent'
  ))
  if (malformedMarkerLike) {
    return finalize(buildCommentChainResult({
      status: 'blocked_malformed_marker_syntax',
      listed,
      commentCount: listed.comments.length,
      matchedCommentCount: 0,
      markerComment: {
        id: malformedMarkerLike?.id ?? null,
        url: commentUrlFromResponse(malformedMarkerLike),
      },
    }), commentUrlFromResponse(malformedMarkerLike))
  }
  const matches = parsedComments.filter((entry) => entry.ownership && ownershipEquals(entry.ownership, ownership))
  const malformed = matches.find((entry) => entry.malformed)
  if (malformed) {
    return finalize(buildCommentChainResult({
      status: 'blocked_malformed',
      listed,
      commentCount: listed.comments.length,
      matchedCommentCount: matches.length,
      markerComment: {
        id: malformed.comment?.id ?? null,
        url: commentUrlFromResponse(malformed.comment),
      },
      digest: malformed.digest,
    }), commentUrlFromResponse(malformed.comment))
  }

  if (matches.length >= 2) {
    return finalize(buildCommentChainResult({
      status: 'blocked_duplicate',
      listed,
      commentCount: listed.comments.length,
      matchedCommentCount: matches.length,
    }), markerCommentUrl)
  }

  const [match] = matches
  if (!match) {
    return finalize(buildCommentChainResult({
      status: 'missing',
      listed,
      commentCount: listed.comments.length,
      matchedCommentCount: 0,
    }), markerCommentUrl)
  }

  const resolvedUrl = commentUrlFromResponse(match.comment)
  if (markerCommentUrl !== null && resolvedUrl !== markerCommentUrl) {
    return finalize(buildCommentChainResult({
      status: 'blocked_stale_write',
      listed,
      commentCount: listed.comments.length,
      matchedCommentCount: 1,
      markerComment: { id: match.comment?.id ?? null, url: resolvedUrl },
      digest: match.digest,
    }), markerCommentUrl)
  }

  const referenceUniverse = await loadReferencedCommentUniverse(client, ownership, resolvedUrl)
  if (referenceUniverse.page_budget_exhausted || referenceUniverse.pagination_exhausted) {
    return finalize(buildCommentChainResult({
      status: 'blocked_page_budget_exhausted',
      listed,
      referenceUniverse,
      commentCount: listed.comments.length,
      matchedCommentCount: 1,
      markerComment: { id: match.comment?.id ?? null, url: resolvedUrl },
      digest: match.digest,
    }), resolvedUrl)
  }

  try {
    const referenceChain = buildReferenceChainFromParsedMarker(match, referenceUniverse.comments)
    return finalize(buildCommentChainResult({
      status: 'resolved',
      listed,
      referenceUniverse,
      commentCount: listed.comments.length,
      matchedCommentCount: 1,
      markerComment: { id: match.comment?.id ?? null, url: resolvedUrl },
      digest: match.digest,
      payloadDigest: referenceChain.payload.canonicalization?.payload_digest ?? null,
      evidenceRefCount: referenceChain.sources.evidence_refs.length,
      sourceManifestCount: referenceChain.manifest.length,
    }), resolvedUrl)
  } catch (error) {
    return finalize(buildCommentChainResult({
      status: 'blocked_invalid_reference_chain',
      listed,
      referenceUniverse,
      commentCount: listed.comments.length,
      matchedCommentCount: 1,
      markerComment: { id: match.comment?.id ?? null, url: resolvedUrl },
      digest: match.digest,
      errorCode: error?.code ?? 'chatgpt_retro_context.invalid_reference_chain',
      errorMessage: error?.message ?? 'failed to validate live reference chain',
    }), resolvedUrl)
  }
}

function normalizeCommentListPayload(payload) {
  if (Array.isArray(payload)) {
    return payload
  }
  if (payload && typeof payload === 'object' && typeof payload.body === 'string') {
    return [payload]
  }
  throw runtimeError('chatgpt_retro_context.invalid_comment_fixture', 'comment fixture must be an object or array of comment objects')
}

async function readJsonFile(filePath, code) {
  try {
    return JSON.parse(await readFile(filePath, 'utf-8'))
  } catch (error) {
    throw runtimeError(code, `failed to parse JSON file: ${filePath}`)
  }
}

function findCommentByUrl(comments, url) {
  return comments.find((comment) => comment?.html_url === url || comment?.url === url) ?? null
}

function buildSyntheticSourceSet(payload) {
  return {
    schema: 'source_set/v1',
    sources: [
      ...payload.refs.run_reports.map((entry) => entry.comment_url),
      payload.refs.retro_index.comment_url,
    ],
  }
}

function assertValidation(result, code) {
  if (!result.valid) {
    const [firstError] = result.errors
    throw runtimeError(code, firstError?.message ?? code)
  }
}

function parseRetroMarkerComment(comment) {
  const body = typeof comment?.body === 'string' ? comment.body : ''
  const validation = validateRetroCommentBody(body)
  const lines = body.split('\n').map((line) => line.trim()).filter((line) => line.length > 0)
  const ownership = parseRetroOwnershipMarker(lines[0] ?? '')
  const digest = parseRetroDigestMarker(lines[1] ?? '')
  if (!ownership) {
    return { ok: false, malformed: false, comment, body }
  }
  return {
    ok: validation.valid,
    malformed: !validation.valid,
    comment,
    body,
    ownership,
    digest: digest
      ? {
          canonicalDigest: `sha256:${digest.canonicalDigest}`,
          sourceSetDigest: `sha256:${digest.sourceSetDigest}`,
        }
      : null,
  }
}

export async function resolveChatgptRetroContextFromFixtures({
  markerCommentJson,
  githubCommentsJson = [],
}) {
  const markerComment = await readJsonFile(markerCommentJson, 'chatgpt_retro_context.marker_comment_parse')
  const commentPages = await Promise.all(githubCommentsJson.map((filePath) => readJsonFile(filePath, 'chatgpt_retro_context.comments_parse')))
  const comments = commentPages.flatMap((payload) => normalizeCommentListPayload(payload))

  const parsedMarker = parseChatgptRetroContextComment(markerComment)
  if (!parsedMarker.ok) {
    throw runtimeError('chatgpt_retro_context.marker_invalid', 'marker comment fixture must contain a valid context marker')
  }
  const result = buildReferenceChainFromParsedMarker(parsedMarker, comments)
  return {
    sources: result.sources,
    manifest: result.manifest,
  }
}

function formatCliErrorResult(command, error) {
  if (error?.code?.startsWith('chatgpt_retro_context.blocked_')) {
    return {
      command,
      status: error.code.replace('chatgpt_retro_context.', ''),
      error_code: error.code,
      error_message: error.message,
    }
  }
  return {
    command,
    status: 'error',
    error_code: error?.code ?? 'chatgpt_retro_context.unexpected_error',
    error_message: error?.message ?? 'unexpected runtime failure',
  }
}

async function runCli() {
  const options = parseArgs(process.argv.slice(2), CLI_OPTION_SPEC)

  if (options.command === 'resolve-fixture') {
    const result = await resolveChatgptRetroContextFromFixtures({
      markerCommentJson: options.markerCommentJson,
      githubCommentsJson: options.githubCommentsJson ?? [],
    })
    console.log(JSON.stringify(result))
    return
  }

  if (options.command === 'resolve-live') {
    const result = await resolveChatgptRetroContextLive(new GhCliIssueCommentsClient(), {
      repo: options.repo,
      targetType: options.targetType,
      targetNumber: options.targetNumber,
      parentIssue: options.parentIssue,
      markerCommentUrl: options.markerCommentUrl ?? null,
    })
    console.log(JSON.stringify(result))
    return
  }

  if (options.command === 'post') {
    const dryRun = parseBooleanFlag(options.dryRun, '--dry-run')
    const confirmLive = parseBooleanFlag(options.confirmLive, '--confirm-live')
    if (!dryRun && !confirmLive) {
      throw runtimeError('chatgpt_retro_context.live_confirmation_required', 'live posting requires --dry-run false and --confirm-live true')
    }
    const payloadMarkdown = await readFile(options.payloadMarkdownFile, 'utf-8')
    const result = await upsertChatgptRetroContextComment(new GhCliIssueCommentsClient(), {
      repo: options.repo,
      targetType: options.targetType,
      targetNumber: options.targetNumber,
      parentIssue: options.parentIssue,
      payloadMarkdown,
      dryRun,
      expectedSupersedesDigest: options.expectedSupersedesDigest ?? null,
    })
    console.log(JSON.stringify(result))
    return
  }

  throw runtimeError('chatgpt_retro_context.unknown_command', 'command must be post, resolve-fixture, or resolve-live')
}

export {
  GhCliIssueCommentsClient,
}

const isDirectExecution = process.argv[1] === fileURLToPath(import.meta.url)
if (isDirectExecution) {
  runCli().catch((error) => {
    console.log(JSON.stringify(formatCliErrorResult(process.argv.includes('--command') ? process.argv[process.argv.indexOf('--command') + 1] ?? null : null, error)))
    process.exit(printCliError('chatgpt-retro-context', error))
  })
}
